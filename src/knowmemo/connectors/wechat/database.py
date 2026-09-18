"""Read-only access to a plaintext WeChat SQLite snapshot.

Two layers enforce that KnowMemo never writes to source data (§2.3):

1. The connection URI is ``mode=ro``. SQLite itself refuses writes.
2. This class exposes no write API at all — there is no ``execute`` that
   accepts arbitrary SQL from a caller, and no ``INSERT``/``UPDATE``/``DELETE``
   path anywhere in the module.

The ``immutable=1`` optimisation is applied only when no ``-wal`` sidecar file
is present. With a WAL present, ``immutable=1`` would make SQLite ignore it and
silently hide the newest rows, which is exactly the kind of quiet data loss
this project treats as worse than an error.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from knowmemo.errors import DatabaseInvalidError, SourceUnreadableError


@dataclass(frozen=True)
class ColumnInfo:
    """One column of a table, as reported by ``PRAGMA table_info``."""

    name: str
    declared_type: str
    not_null: bool
    primary_key: bool


class ReadOnlyDatabase:
    """A SQLite file opened strictly for reading."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def _uri(self) -> str:
        has_wal = self.path.with_name(self.path.name + "-wal").exists()
        params = "mode=ro" if has_wal else "mode=ro&immutable=1"
        return f"file:{self.path}?{params}"

    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        if not self.path.exists():
            raise SourceUnreadableError("database file does not exist", path=str(self.path))
        try:
            connection = sqlite3.connect(self._uri(), uri=True)
        except sqlite3.OperationalError as exc:
            raise SourceUnreadableError(
                f"cannot open database: {exc}", path=str(self.path)
            ) from exc
        connection.row_factory = sqlite3.Row
        self._connection = connection
        try:
            self.tables()
        except sqlite3.DatabaseError as exc:
            self.close()
            raise DatabaseInvalidError(
                f"not a readable SQLite database: {exc}", path=str(self.path)
            ) from exc
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> ReadOnlyDatabase:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[str]:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    def columns(self, table: str) -> list[ColumnInfo]:
        connection = self._require_connection()
        quoted = self._quote_identifier(table)
        rows = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        return [
            ColumnInfo(
                name=row["name"],
                declared_type=(row["type"] or "").upper(),
                not_null=bool(row["notnull"]),
                primary_key=bool(row["pk"]),
            )
            for row in rows
        ]

    def column_names(self, table: str) -> list[str]:
        return [column.name for column in self.columns(table)]

    def has_column(self, table: str, column: str) -> bool:
        return column in self.column_names(table)

    def count(self, table: str) -> int:
        connection = self._require_connection()
        quoted = self._quote_identifier(table)
        row = connection.execute(f"SELECT COUNT(*) AS n FROM {quoted}").fetchone()
        return int(row["n"])

    def iter_rows(
        self,
        table: str,
        *,
        order_by: Sequence[str] | None = None,
        where: str | None = None,
        parameters: Sequence[object] = (),
    ) -> Iterator[sqlite3.Row]:
        """Yield rows from ``table``.

        ``table`` and every entry of ``order_by`` are validated against the
        live schema before being interpolated, because SQLite cannot bind
        identifiers as parameters. ``where`` is a fixed literal supplied by
        this package, never by user input; values go through ``parameters``.
        """
        connection = self._require_connection()
        if table not in self.tables():
            raise DatabaseInvalidError("no such table", path=str(self.path), table=table)

        sql = f"SELECT * FROM {self._quote_identifier(table)}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            available = set(self.column_names(table))
            missing = [column for column in order_by if column not in available]
            if missing:
                raise DatabaseInvalidError(
                    "cannot order by missing column(s)",
                    table=table,
                    columns=",".join(missing),
                )
            sql += " ORDER BY " + ", ".join(self._quote_identifier(c) for c in order_by)

        cursor = connection.execute(sql, tuple(parameters))
        yield from cursor.fetchall()

    def count_by_column(self, table: str, column: str) -> dict[object, int]:
        """Group-count the distinct values of ``column``.

        Purpose-built rather than a general ``execute``: the connector needs
        this to report ``localType`` values it has no mapping for, and exposing
        arbitrary SQL would defeat the read-only guarantee this class exists to
        provide.
        """
        connection = self._require_connection()
        if table not in self.tables():
            raise DatabaseInvalidError("no such table", path=str(self.path), table=table)
        if column not in self.column_names(table):
            raise DatabaseInvalidError(
                "no such column", path=str(self.path), table=table, column=column
            )

        sql = (
            f"SELECT {self._quote_identifier(column)} AS value, COUNT(*) AS n "
            f"FROM {self._quote_identifier(table)} GROUP BY value"
        )
        return {row["value"]: int(row["n"]) for row in connection.execute(sql).fetchall()}

    def range_of(self, table: str, column: str) -> tuple[int | None, int | None]:
        """Return ``(MIN, MAX)`` of a numeric column.

        Used to report the true span of a snapshot. The session table's
        ``last_timestamp`` is *not* a substitute: it records when each
        conversation was last active, so a scan that read it would report a
        snapshot's entire history as ending at one instant.
        """
        connection = self._require_connection()
        if table not in self.tables():
            raise DatabaseInvalidError("no such table", path=str(self.path), table=table)
        if column not in self.column_names(table):
            raise DatabaseInvalidError(
                "no such column", path=str(self.path), table=table, column=column
            )

        quoted = self._quote_identifier(column)
        sql = (
            f"SELECT MIN({quoted}) AS lo, MAX({quoted}) AS hi FROM {self._quote_identifier(table)}"
        )
        row = connection.execute(sql).fetchone()
        if row is None or row["lo"] is None or row["hi"] is None:
            return None, None
        return int(row["lo"]), int(row["hi"])

    # -- helpers -----------------------------------------------------------

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            return self.connect()
        return self._connection

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        """Quote an identifier for interpolation.

        Doubling embedded quotes is the SQLite-documented escape. The caller
        has already validated the name against the live schema; this is the
        second line of defence.
        """
        return '"' + identifier.replace('"', '""') + '"'
