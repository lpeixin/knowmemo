"""The WeChat connector — first concrete :class:`SourceConnector` (§11, §12).

Input contract
--------------
This connector reads a **plaintext SQLite snapshot** directory
(``wechat.snapshot_path``). It never reads the live encrypted WeChat store and
never touches a running WeChat process.

That boundary is deliberate and is recorded in ``docs/plan.md`` (decision D4).
Producing the plaintext snapshot is somebody else's job, performed by the user
under their own judgement; KnowMemo's responsibility begins at "here is a
directory of readable SQLite files". This keeps the connector free of
privilege escalation, keeps it testable against synthetic fixtures, and keeps
its behaviour identical whether the snapshot came from a phone backup, a
migration, or a machine that happens to hold an unencrypted copy.

What the connector must not do (§11) — and does not: call an LLM, compute
embeddings, perform retrieval, build prompts, or know anything about WeKnora.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from knowmemo.connectors.wechat.database import ReadOnlyDatabase
from knowmemo.connectors.wechat.models import MessageTable
from knowmemo.connectors.wechat.parser import (
    ParseFailure,
    build_conversation,
    iter_table_rows,
    parse_table,
)
from knowmemo.connectors.wechat.schema import SnapshotSchema, introspect
from knowmemo.domain.source import Source, SourceStatus, SourceType
from knowmemo.errors import KnowMemoError
from knowmemo.ingestion.interfaces import (
    ExtractedConversation,
    HealthCheck,
    ScanResult,
    SourceConnector,
)

SOURCE_TYPE = "wechat"


def _safe_datetime(value: int | None) -> datetime | None:
    """Convert a bare Unix timestamp, or ``None`` if it is not representable."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


class WeChatConnector(SourceConnector):
    """Reads a plaintext WeChat snapshot and yields normalised conversations."""

    source_type = SOURCE_TYPE

    def __init__(
        self,
        *,
        snapshot_path: str | Path | None = None,
        data_path: str | Path | None = None,
        account_id: str | None = None,
        source_account_id: str | None = None,
        **options: object,
    ) -> None:
        super().__init__(**options)
        self.snapshot_path = Path(snapshot_path).expanduser() if snapshot_path else None
        #: The encrypted store's location. Reported for diagnostics only; never read.
        self.data_path = Path(data_path).expanduser() if data_path else None
        #: The account's own wxid, used to attribute outgoing messages.
        self.account_id = account_id or source_account_id
        self.source_account_id = source_account_id or account_id

        self.failures: list[ParseFailure] = []
        self.warnings: list[str] = []
        self._schema: SnapshotSchema | None = None

    @property
    def source_id(self) -> str:
        """Stable source identifier: ``wechat:<account>``.

        The account is part of the identity because two accounts on one machine
        are two sources, and the deterministic message IDs already carry it.
        """
        return f"{SOURCE_TYPE}:{self.account_id or 'default'}"

    # -- discovery ---------------------------------------------------------

    def discover(self) -> Source:
        if self.snapshot_path is None:
            return Source(
                id=self.source_id,
                source_type=SourceType.WECHAT,
                display_name="WeChat",
                enabled=True,
                path=None,
                status=SourceStatus.NOT_CONFIGURED,
                status_detail=(
                    "wechat.snapshot_path is not set. KnowMemo reads a plaintext "
                    "SQLite snapshot, never the live encrypted store."
                ),
            )

        if not self.snapshot_path.exists():
            return Source(
                id=self.source_id,
                source_type=SourceType.WECHAT,
                display_name="WeChat",
                enabled=True,
                path=str(self.snapshot_path),
                status=SourceStatus.NOT_FOUND,
                status_detail=f"{self.snapshot_path} does not exist",
            )

        try:
            schema = self.introspect()
        except KnowMemoError as exc:
            return Source(
                id=self.source_id,
                source_type=SourceType.WECHAT,
                display_name="WeChat",
                enabled=True,
                path=str(self.snapshot_path),
                status=SourceStatus.UNSUPPORTED,
                status_detail=str(exc),
            )

        return Source(
            id=self.source_id,
            source_type=SourceType.WECHAT,
            display_name="WeChat",
            enabled=True,
            path=str(self.snapshot_path),
            status=SourceStatus.AVAILABLE,
            status_detail=(
                f"{len(schema.contacts)} contacts, "
                f"{len(schema.message_tables)} conversation tables, "
                f"{schema.total_messages:,} messages"
            ),
            metadata={
                "databases": len(schema.layout.all_databases),
                "message_databases": len(schema.layout.message_dbs),
                "unresolved_tables": len(schema.unresolved_tables),
                "warnings": schema.warnings,
            },
        )

    # -- health ------------------------------------------------------------

    def health_check(self) -> list[HealthCheck]:
        checks: list[HealthCheck] = []

        if self.data_path is not None:
            checks.append(
                HealthCheck(
                    name="WeChat live store (not read)",
                    ok=True,
                    detail=f"{self.data_path} — recorded for reference only",
                    remedy=(
                        "KnowMemo never reads this directory. It is the encrypted "
                        "store; only the plaintext snapshot is read."
                    ),
                )
            )

        if self.snapshot_path is None:
            checks.append(
                HealthCheck(
                    name="WeChat snapshot",
                    ok=False,
                    detail="wechat.snapshot_path is not configured",
                    remedy=(
                        "Set wechat.snapshot_path to a directory of plaintext "
                        "SQLite databases. See docs/plan.md decision D4."
                    ),
                )
            )
            return checks

        if not self.snapshot_path.exists():
            checks.append(
                HealthCheck(
                    name="WeChat snapshot",
                    ok=False,
                    detail=f"{self.snapshot_path} does not exist",
                    remedy="Correct the configured path.",
                )
            )
            return checks

        try:
            schema = self.introspect()
        except KnowMemoError as exc:
            checks.append(
                HealthCheck(
                    name="WeChat snapshot",
                    ok=False,
                    detail=str(exc),
                    remedy="Verify the snapshot was fully decrypted and contains Msg_* tables.",
                )
            )
            return checks

        checks.append(
            HealthCheck(
                name="WeChat snapshot",
                ok=True,
                detail=(
                    f"{len(schema.message_tables)} conversation tables, "
                    f"{schema.total_messages:,} messages"
                ),
            )
        )
        checks.append(
            HealthCheck(
                name="Contact resolution",
                ok=not schema.unresolved_tables,
                detail=(
                    f"{len(schema.unresolved_tables)} table(s) unmatched"
                    if schema.unresolved_tables
                    else f"all {len(schema.message_tables)} tables matched to contacts"
                ),
                remedy=(
                    "Tables whose md5 digest is absent from the contact list will "
                    "be skipped; the contact database may be incomplete."
                )
                if schema.unresolved_tables
                else None,
            )
        )
        if self.account_id is None:
            checks.append(
                HealthCheck(
                    name="Account identity",
                    ok=False,
                    detail="wechat.account_id is not set",
                    remedy=(
                        "Outgoing messages will be attributed to a placeholder. "
                        "Set wechat.account_id to your own wxid for accurate attribution."
                    ),
                )
            )
        return checks

    # -- scan --------------------------------------------------------------

    def introspect(self) -> SnapshotSchema:
        if self._schema is None:
            if self.snapshot_path is None:
                raise KnowMemoError("wechat.snapshot_path is not configured")
            self._schema = introspect(self.snapshot_path)
            self.warnings = list(self._schema.warnings)
        return self._schema

    def scan(self) -> ScanResult:
        schema = self.introspect()
        earliest, latest = self._message_time_range(schema)

        return ScanResult(
            source_type=SOURCE_TYPE,
            source_id=self.source_account_id,
            path=str(self.snapshot_path),
            conversations=len(schema.message_tables),
            messages=schema.total_messages,
            earliest=earliest,
            latest=latest,
            details={
                "contacts": len(schema.contacts),
                "sessions": len(schema.sessions),
                "databases": len(schema.layout.all_databases),
                "message_databases": [path.name for path in schema.layout.message_dbs],
                "unresolved_tables": [t.table_name for t in schema.unresolved_tables],
                "warnings": schema.warnings,
                "unsupported_types": self._unsupported_type_counts(),
            },
        )

    def _message_time_range(
        self, schema: SnapshotSchema
    ) -> tuple[datetime | None, datetime | None]:
        """The true first and last message timestamps in the snapshot.

        Read from the message tables, not from ``session.db``. A session row
        records when a conversation was *last active*, so deriving the span
        from it would report a year of history as ending at a single instant.
        """
        earliest: datetime | None = None
        latest: datetime | None = None

        by_database: dict[str, list[MessageTable]] = {}
        for table in schema.message_tables:
            if schema.columns_for(table.table_name).create_time is None:
                continue
            by_database.setdefault(table.database_path, []).append(table)

        for database_path, tables in sorted(by_database.items()):
            with ReadOnlyDatabase(Path(database_path)) as database:
                for table in tables:
                    column = schema.columns_for(table.table_name).create_time
                    low, high = database.range_of(table.table_name, column)
                    for value in (low, high):
                        moment = _safe_datetime(value)
                        if moment is None:
                            continue
                        earliest = moment if earliest is None or moment < earliest else earliest
                        latest = moment if latest is None or moment > latest else latest
        return earliest, latest

    def _unsupported_type_counts(self) -> dict[int, int]:
        """Count ``localType`` values the connector has no mapping for.

        Reported rather than swallowed: an unmapped type is a gap in the
        connector, and the report should make it visible instead of letting
        those messages quietly become ``unknown``.
        """
        from knowmemo.connectors.wechat.decoders import LOCAL_TYPE_MAP

        schema = self.introspect()
        counts: dict[int, int] = {}
        for table in schema.message_tables:
            columns = schema.columns_for(table.table_name)
            if columns.local_type is None:
                continue
            with ReadOnlyDatabase(Path(table.database_path)) as database:
                grouped = database.count_by_column(table.table_name, columns.local_type)
            for value, count in grouped.items():
                if value is None:
                    continue
                local_type = int(value)
                if local_type in LOCAL_TYPE_MAP:
                    continue
                counts[local_type] = counts.get(local_type, 0) + count
        return dict(sorted(counts.items()))

    # -- extraction --------------------------------------------------------

    def extract(self) -> Iterator[ExtractedConversation]:
        schema = self.introspect()
        self.failures = []

        by_database: dict[str, list[MessageTable]] = {}
        for table in schema.message_tables:
            if not table.is_resolved:
                continue
            by_database.setdefault(table.database_path, []).append(table)

        for database_path, tables in sorted(by_database.items()):
            with ReadOnlyDatabase(Path(database_path)) as database:
                for table in sorted(tables, key=lambda item: item.table_name):
                    yield from self._extract_table(database, table)

    def _extract_table(
        self, database: ReadOnlyDatabase, table: MessageTable
    ) -> Iterator[ExtractedConversation]:
        schema = self.introspect()
        username = table.username
        if username is None:
            return

        contact = schema.contacts.get(username)
        columns = schema.columns_for(table.table_name)

        outcome = parse_table(
            iter_table_rows(database, table.table_name),
            table_name=table.table_name,
            username=username,
            contact=contact,
            columns=columns,
            source_account_id=self.source_account_id,
            account_id=self.account_id,
        )
        self.failures.extend(outcome.failures)

        conversation = build_conversation(
            username=username,
            contact=contact,
            session=schema.sessions.get(username),
            messages=outcome.messages,
            source_account_id=self.source_account_id,
            account_id=self.account_id,
        )
        if conversation is not None:
            yield ExtractedConversation(
                conversation=conversation,
                messages=sorted(outcome.messages, key=lambda message: message.timestamp),
            )
