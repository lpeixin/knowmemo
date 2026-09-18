"""Snapshot discovery and schema introspection.

This module replaces the version-adapter design sketched in the specification
(§13). That design assumed the variation between WeChat installations is a
matter of *version*, and proposed ``VersionAdapterA/B/C``. The actual variation
is per-table and per-row:

* databases are **sharded** — ``message_0.db``, ``message_1.db``, … plus
  ``contact.db``, ``session.db``, ``media_0.db``
* messages are **not** in one ``message`` table. Each contact and each group
  gets its own dynamically named table, ``Msg_<md5(username)>``
* the content column's meaning is per-row, signalled by a WCDB type code

So the reader's core competence is *discovery*, not version branching: find the
databases, find the message tables, resolve each table's md5 digest back to a
username through the contact list, and detect which columns are present.

Nothing here raises on an unexpected shape. Anything unrecognised is recorded
in :attr:`SnapshotSchema.warnings` and carried into the scan report, because a
snapshot that is 90% readable is worth more than an exception.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from knowmemo.connectors.wechat.database import ReadOnlyDatabase
from knowmemo.connectors.wechat.models import MessageTable, RawContact, RawSession
from knowmemo.errors import SchemaUnsupportedError, SourceNotFoundError, SourceUnreadableError

MESSAGE_TABLE_PREFIX = "msg_"
MAX_SCAN_DEPTH = 4

CONTACT_DB_HINTS = ("contact", "rcontact")
SESSION_DB_HINTS = ("session",)
MESSAGE_DB_HINTS = ("message", "msg", "media")

#: Logical field -> candidate column names, lowercased and compared with
#: separators stripped. Ordered by preference.
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "local_id": ("localid", "msgid", "id", "rowid"),
    "local_type": ("localtype", "msgtype", "type"),
    "create_time": ("createtime", "timestamp", "time", "msgtime"),
    "is_send": ("issend", "send", "fromme"),
    "content": ("messagecontent", "content", "msgcontent", "textcontent"),
    "content_type_code": ("wcdbctmessagecontent", "contenttypecode", "ctmessagecontent"),
    "server_id": ("serverid", "svrid", "msgsvrid"),
    "sort_seq": ("sortseq", "seq"),
    "status": ("status", "msgstatus"),
    "des": ("des", "description", "flag"),
    "compressed_content": ("compresscontent", "compressedcontent"),
    "compressed_type_code": ("wcdbctcompresscontent", "compresscontenttypecode"),
    "packed_info": ("packedinfodata", "packedinfo", "extracontent"),
}

REQUIRED_FIELDS = ("local_id", "create_time")


def _normalise(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


@dataclass(frozen=True)
class MessageColumns:
    """Which physical column backs each logical message field.

    A value of ``None`` means the column is absent from this snapshot. The
    parser checks for that explicitly rather than assuming a layout.
    """

    local_id: str | None = None
    local_type: str | None = None
    create_time: str | None = None
    is_send: str | None = None
    content: str | None = None
    content_type_code: str | None = None
    server_id: str | None = None
    sort_seq: str | None = None
    status: str | None = None
    des: str | None = None
    compressed_content: str | None = None
    compressed_type_code: str | None = None
    packed_info: str | None = None

    @classmethod
    def detect(cls, columns: list[str]) -> MessageColumns:
        by_normalised = {_normalise(column): column for column in columns}
        resolved: dict[str, str | None] = {}
        for logical, candidates in COLUMN_CANDIDATES.items():
            resolved[logical] = next(
                (by_normalised[c] for c in candidates if c in by_normalised), None
            )
        return cls(**resolved)  # type: ignore[arg-type]

    @property
    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED_FIELDS if getattr(self, name) is None]


@dataclass(frozen=True)
class SnapshotLayout:
    """Where the databases live inside a snapshot directory."""

    root: Path
    contact_db: Path | None = None
    session_db: Path | None = None
    message_dbs: tuple[Path, ...] = ()
    other_dbs: tuple[Path, ...] = ()

    @property
    def all_databases(self) -> tuple[Path, ...]:
        found = [p for p in (self.contact_db, self.session_db) if p is not None]
        return tuple(found) + self.message_dbs + self.other_dbs


@dataclass
class SnapshotSchema:
    """Everything the connector learned about a snapshot before parsing."""

    layout: SnapshotLayout
    contacts: dict[str, RawContact] = field(default_factory=dict)
    sessions: dict[str, RawSession] = field(default_factory=dict)
    message_tables: list[MessageTable] = field(default_factory=list)
    columns_by_table: dict[str, MessageColumns] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def unresolved_tables(self) -> list[MessageTable]:
        return [table for table in self.message_tables if not table.is_resolved]

    @property
    def total_messages(self) -> int:
        return sum(table.row_count for table in self.message_tables)

    def columns_for(self, table_name: str) -> MessageColumns:
        return self.columns_by_table.get(table_name, MessageColumns())


def locate_snapshot(root: Path) -> SnapshotLayout:
    """Find the databases in a snapshot directory.

    Tolerates both a nested layout (``contact/contact.db``,
    ``message/message_0.db``) and a flat one, because the layout produced by an
    external decryption step is not standardised.
    """
    if not root.exists():
        raise SourceNotFoundError("snapshot directory does not exist", path=str(root))
    if not root.is_dir():
        raise SourceNotFoundError("snapshot path is not a directory", path=str(root))

    try:
        next(root.iterdir(), None)
    except PermissionError as exc:
        raise SourceUnreadableError(
            "the OS denied access to the snapshot directory",
            path=str(root),
            remedy="Grant Full Disk Access to your terminal.",
        ) from exc

    contact_db: Path | None = None
    session_db: Path | None = None
    message_dbs: list[Path] = []
    other_dbs: list[Path] = []

    for path in sorted(_walk_databases(root)):
        stem = path.stem.lower()
        if any(hint in stem for hint in CONTACT_DB_HINTS):
            contact_db = contact_db or path
        elif any(hint in stem for hint in SESSION_DB_HINTS):
            session_db = session_db or path
        elif any(hint in stem for hint in MESSAGE_DB_HINTS):
            message_dbs.append(path)
        else:
            other_dbs.append(path)

    return SnapshotLayout(
        root=root,
        contact_db=contact_db,
        session_db=session_db,
        message_dbs=tuple(message_dbs),
        other_dbs=tuple(other_dbs),
    )


def _walk_databases(root: Path) -> list[Path]:
    """Collect ``*.db`` files, depth-limited, skipping SQLite sidecars."""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_SCAN_DEPTH:
            continue
        try:
            entries = sorted(directory.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                stack.append((entry, depth + 1))
            elif entry.suffix.lower() == ".db":
                found.append(entry)
    return found


def _md5(username: str) -> str:
    return hashlib.md5(username.encode("utf-8")).hexdigest()


def _find_contact_table(database: ReadOnlyDatabase) -> str | None:
    for table in database.tables():
        columns = {_normalise(c) for c in database.column_names(table)}
        if "username" in columns:
            return table
    return None


def _load_contacts(layout: SnapshotLayout, schema: SnapshotSchema) -> None:
    if layout.contact_db is None:
        schema.warnings.append("no contact database found; table names cannot be resolved")
        return

    with ReadOnlyDatabase(layout.contact_db) as database:
        table = _find_contact_table(database)
        if table is None:
            schema.warnings.append(f"{layout.contact_db.name}: no table with a username column")
            return

        available = _column_index(database.column_names(table))
        for row in database.iter_rows(table):
            username = row["username"]
            if not username:
                continue
            schema.contacts[username] = RawContact(
                username=username,
                nick_name=_pick(row, available, "nickname", "nick_name"),
                remark=_pick(row, available, "remark", "conremark"),
                alias=_pick(row, available, "alias"),
                local_type=_pick_int(row, available, "localtype", "type"),
            )


def _load_sessions(layout: SnapshotLayout, schema: SnapshotSchema) -> None:
    if layout.session_db is None:
        return
    with ReadOnlyDatabase(layout.session_db) as database:
        candidates = [
            table
            for table in database.tables()
            if "username" in {_normalise(c) for c in database.column_names(table)}
        ]
        if not candidates:
            schema.warnings.append(f"{layout.session_db.name}: no session table found")
            return
        table = candidates[0]
        available = _column_index(database.column_names(table))
        for row in database.iter_rows(table):
            username = row["username"]
            if not username:
                continue
            schema.sessions[username] = RawSession(
                username=username,
                last_timestamp=_pick_int(row, available, "lasttimestamp", "timestamp"),
                unread_count=_pick_int(row, available, "unreadcount", "unread"),
            )


def _discover_message_tables(layout: SnapshotLayout, schema: SnapshotSchema) -> None:
    """Find every ``Msg_<md5>`` table and resolve its conversation."""
    md5_index = {_md5(username): username for username in schema.contacts}

    for path in layout.message_dbs:
        with ReadOnlyDatabase(path) as database:
            for table in database.tables():
                if not table.lower().startswith(MESSAGE_TABLE_PREFIX):
                    continue
                digest = table[len(MESSAGE_TABLE_PREFIX) :].lower()
                try:
                    row_count = database.count(table)
                except Exception:  # noqa: BLE001 - a broken table must not stop the scan
                    schema.warnings.append(f"{path.name}:{table}: row count unavailable")
                    row_count = 0

                columns = MessageColumns.detect(database.column_names(table))
                missing = columns.missing_required
                if missing:
                    schema.warnings.append(
                        f"{path.name}:{table}: missing required column(s) "
                        f"{', '.join(missing)}; table will be skipped"
                    )
                    continue

                schema.columns_by_table[table] = columns
                schema.message_tables.append(
                    MessageTable(
                        table_name=table,
                        database_path=str(path),
                        md5=digest,
                        username=md5_index.get(digest),
                        row_count=row_count,
                    )
                )

    unresolved = schema.unresolved_tables
    if unresolved:
        schema.warnings.append(
            f"{len(unresolved)} message table(s) could not be matched to a contact "
            "(the md5 digest was not in the contact list)"
        )


def introspect(root: Path) -> SnapshotSchema:
    """Discover everything knowable about a snapshot without parsing messages."""
    layout = locate_snapshot(root)
    schema = SnapshotSchema(layout=layout)

    if not layout.all_databases:
        raise SourceNotFoundError("no SQLite databases found in the snapshot", path=str(root))

    _load_contacts(layout, schema)
    _load_sessions(layout, schema)
    _discover_message_tables(layout, schema)

    if not schema.message_tables:
        raise SchemaUnsupportedError(
            "no Msg_<md5> message tables found; this snapshot's schema is not supported",
            path=str(root),
            databases=len(layout.all_databases),
        )

    if layout.other_dbs:
        schema.warnings.append(
            f"{len(layout.other_dbs)} unrelated database(s) ignored: "
            + ", ".join(path.name for path in layout.other_dbs[:5])
        )

    return schema


def _column_index(columns: list[str]) -> dict[str, str]:
    """Map a normalised column name to the real one.

    Needed because the candidate lists above are written in one spelling while
    a snapshot may use another (``nick_name`` vs ``nickname``). Indexing a
    ``sqlite3.Row`` requires the actual name, so the normalised form is only
    ever used for lookup.
    """
    return {_normalise(column): column for column in columns}


def _pick(row: object, index: dict[str, str], *names: str) -> str | None:
    for name in names:
        actual = index.get(_normalise(name))
        if actual is None:
            continue
        value = row[actual]  # type: ignore[index]
        return str(value) if value is not None else None
    return None


def _pick_int(row: object, index: dict[str, str], *names: str) -> int | None:
    value = _pick(row, index, *names)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
