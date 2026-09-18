"""The ingestion pipeline (design doc §15, §16, §25, §26, §46).

Wiring, in one place: connector → normalised record → segments → knowledge
documents → segment metadata index, with the §25 state machine and the §26
manifest recorded as it goes.

What this module deliberately does **not** do
---------------------------------------------
It never calls an LLM, never computes an embedding, and knows nothing about
WeKnora. Those are later milestones and, per §17, the pipeline has to be
useful without them.

``--dry-run`` semantics
-----------------------
A dry run reads the source, segments it, builds every document in memory and
reports exactly what a real run would create, update or leave alone — then
writes **none** of it. The only thing it persists is the run manifest, so that
``knowmemo import status`` shows the dry run happened. Per-record state is not
touched, because nothing was processed.

This is stricter than the wording in ``docs/plan.md`` §3, which had the
acceptance criterion as "``--dry-run`` produces the report *and*
``data/knowledge/`` contains documents". Those two clauses are mutually
exclusive: a command that writes the documents is not a dry run. The criterion
is therefore split — ``--dry-run`` proves the plan, a real run materialises it,
and both are part of M2 acceptance. Recorded here because a criterion that
quietly changes meaning is worse than one that was wrong out loud.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Imported for its side effect: the module registers WeChatConnector with the
# registry. Keeping it explicit here means the set of known connectors is
# greppable in one place, which is the reason the registry is explicit too.
import knowmemo.connectors.wechat  # noqa: F401
from knowmemo.config.settings import Settings
from knowmemo.domain.conversation import Conversation, ConversationType
from knowmemo.domain.ids import content_hash
from knowmemo.domain.message import Message, MessageType
from knowmemo.domain.source import IngestionState, SourceStatus
from knowmemo.errors import (
    KnowMemoError,
    SchemaUnsupportedError,
    SourceNotFoundError,
    SourceUnreadableError,
)
from knowmemo.ingestion.interfaces import SourceConnector, registry
from knowmemo.knowledge.document_builder import build_document, render_markdown
from knowmemo.normalization.jsonl import write_messages
from knowmemo.processing.segmentation import SegmentationParams, segment_messages
from knowmemo.storage.database import (
    create_db_engine,
    data_root,
    init_db,
    make_session_factory,
    session_scope,
)
from knowmemo.storage.layout import knowledge_dir, knowledge_file, normalized_dir, normalized_file
from knowmemo.storage.models import SegmentRow
from knowmemo.storage.repositories import (
    ImportRunRepository,
    RecordStateRepository,
    SegmentRepository,
)

RECORD_KIND_MESSAGE = "message"
RECORD_KIND_SEGMENT = "segment"

ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_UNCHANGED = "unchanged"


class FailureNote(BaseModel):
    """One record-level failure, carried in the report rather than raised."""

    stage: str
    code: str
    detail: str
    record_id: str | None = None


class DocumentPreview(BaseModel):
    """A segment's fate, so a dry run is reviewable rather than a bare count."""

    segment_id: str
    document_path: str
    conversation_name: str
    conversation_type: str
    start_time: datetime
    end_time: datetime
    message_count: int
    participants: list[str]
    estimated_tokens: int
    action: str
    has_text: bool


class IngestionReport(BaseModel):
    """The §46 report."""

    run_id: str
    source_type: str
    source_id: str
    dry_run: bool
    status: str
    started_at: datetime
    finished_at: datetime

    conversations: int = 0
    #: Every message the connector produced.
    messages_scanned: int = 0
    #: Messages written to the normalised record — all of them, media included.
    messages_imported: int = 0
    #: Messages carrying indexable text. The gap to ``messages_imported`` is
    #: media-only traffic, reported rather than hidden.
    messages_with_text: int = 0
    messages_empty: int = 0
    unknown_message_types: int = 0

    segments: int = 0
    segments_empty: int = 0

    documents_created: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0

    errors: int = 0
    error_summary: str | None = None

    earliest: datetime | None = None
    latest: datetime | None = None

    normalized_dir: str | None = None
    knowledge_dir: str | None = None

    warnings: list[str] = Field(default_factory=list)
    failures: list[FailureNote] = Field(default_factory=list)
    preview: list[DocumentPreview] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.errors == 0

    @property
    def documents_touched(self) -> int:
        return self.documents_created + self.documents_updated


def build_connector(
    settings: Settings, source_type: str, *, overrides: dict[str, object] | None = None
) -> SourceConnector:
    """Instantiate the connector for ``source_type`` from settings.

    WeChat's options are passed explicitly rather than as a generic
    ``**settings`` dump, so a connector can never accidentally receive the
    WeKnora API key or the LLM configuration. ``overrides`` lets the caller
    supply a value that is not in the configuration file — the CLI uses it when
    a source was registered with ``source add`` rather than configured in YAML.
    """
    overrides = overrides or {}
    if source_type == "wechat":
        return registry.create(
            source_type,
            snapshot_path=overrides.get("snapshot_path", settings.wechat.snapshot_path),
            data_path=overrides.get("data_path", settings.wechat.data_path),
            account_id=overrides.get("account_id", settings.wechat.account_id),
        )
    return registry.create(source_type, **overrides)


def _unavailable_error(source_status: SourceStatus, detail: str | None) -> KnowMemoError:
    """Map a discovery failure onto the §35 taxonomy.

    ``UNSUPPORTED`` is a schema problem, ``UNREADABLE`` is a permissions
    problem, and both deserve a different remedy in the message.
    """
    message = detail or "source is not available"
    if source_status is SourceStatus.UNREADABLE:
        return SourceUnreadableError(message)
    if source_status is SourceStatus.UNSUPPORTED:
        return SchemaUnsupportedError(message)
    return SourceNotFoundError(message)


def _new_run_id(started: datetime) -> str:
    """A run identifier.

    Randomness is acceptable here: §27's determinism rule governs *imported
    records*, and two runs started in the same second must not collide.
    """
    return f"run-{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _order(messages: list[Message]) -> list[Message]:
    return sorted(messages, key=lambda message: (message.timestamp, message.id))


def _status_for(report: IngestionReport, fatal: KnowMemoError | None) -> str:
    if fatal is not None:
        return "failed"
    return "partial" if report.errors else "success"


class IngestionPipeline:
    """Runs one import for one source."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_type: str = "wechat",
        connector: SourceConnector | None = None,
        params: SegmentationParams | None = None,
    ) -> None:
        self.settings = settings
        self.source_type = source_type
        self.connector = connector or build_connector(settings, source_type)
        self.params = params or SegmentationParams.from_settings(settings.segmentation)

    # -- public entry point -------------------------------------------------

    def run(self, *, dry_run: bool = False, preview_limit: int = 5) -> IngestionReport:
        started_at = datetime.now()
        source = self.connector.discover()
        if source.status is not SourceStatus.AVAILABLE:
            raise _unavailable_error(source.status, source.status_detail)

        engine = create_db_engine(self.settings)
        init_db(engine)
        root = data_root(self.settings)
        report = IngestionReport(
            run_id=_new_run_id(started_at),
            source_type=self.source_type,
            source_id=source.id,
            dry_run=dry_run,
            status="running",
            started_at=started_at,
            finished_at=started_at,
            normalized_dir=str(normalized_dir(root, self.source_type)),
            knowledge_dir=str(knowledge_dir(root, self.source_type)),
        )

        with session_scope(make_session_factory(engine)) as session:
            runs = ImportRunRepository(session)
            runs.start(
                report.run_id,
                self.source_type,
                source.id,
                dry_run=dry_run,
                started_at=started_at,
            )

            fatal: KnowMemoError | None = None
            try:
                self._consume(session, root, report, dry_run=dry_run, preview_limit=preview_limit)
            except KnowMemoError as exc:
                fatal = exc
                report.errors += 1
                report.failures.append(
                    FailureNote(stage="run", code=str(exc.code), detail=str(exc))
                )

            report.finished_at = datetime.now()
            report.status = _status_for(report, fatal)
            report.error_summary = self._summarise(report)
            runs.finish(
                report.run_id,
                finished_at=report.finished_at,
                status=report.status,
                messages_scanned=report.messages_scanned,
                messages_imported=report.messages_imported,
                documents_created=report.documents_created,
                documents_updated=report.documents_updated,
                errors=report.errors,
                error_summary=report.error_summary,
            )

        return report

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _summarise(report: IngestionReport) -> str | None:
        if not report.failures:
            return None
        by_code: dict[str, int] = {}
        for failure in report.failures:
            by_code[failure.code] = by_code.get(failure.code, 0) + 1
        return ", ".join(f"{code} x{count}" for code, count in sorted(by_code.items()))

    def _consume(
        self,
        session: Session,
        root: Path,
        report: IngestionReport,
        *,
        dry_run: bool,
        preview_limit: int,
    ) -> None:
        states = RecordStateRepository(session)
        segments_repo = SegmentRepository(session)
        self._collect_connector_warnings(report)

        for extracted in self.connector.extract():
            conversation = extracted.conversation
            report.conversations += 1
            try:
                self._consume_conversation(
                    conversation,
                    _order(extracted.messages),
                    root=root,
                    report=report,
                    states=states,
                    segments_repo=segments_repo,
                    dry_run=dry_run,
                    preview_limit=preview_limit,
                )
            except KnowMemoError as exc:
                report.errors += 1
                report.failures.append(
                    FailureNote(
                        stage="conversation",
                        code=str(exc.code),
                        detail=str(exc),
                        record_id=conversation.id,
                    )
                )
                if not dry_run:
                    states.mark(
                        conversation.id,
                        self.source_type,
                        RECORD_KIND_SEGMENT,
                        IngestionState.PROCESS_FAILED,
                        error_code=str(exc.code),
                        error_detail=str(exc),
                    )

    def _collect_connector_warnings(self, report: IngestionReport) -> None:
        warnings = getattr(self.connector, "warnings", None)
        if isinstance(warnings, list):
            report.warnings.extend(str(item) for item in warnings)
        failures = getattr(self.connector, "failures", None)
        if isinstance(failures, list):
            for failure in failures:
                report.failures.append(
                    FailureNote(
                        stage="parse",
                        code=str(failure.code),
                        detail=failure.detail,
                        record_id=str(failure.local_id) if failure.local_id is not None else None,
                    )
                )

    def _consume_conversation(
        self,
        conversation: Conversation,
        messages: list[Message],
        *,
        root: Path,
        report: IngestionReport,
        states: RecordStateRepository,
        segments_repo: SegmentRepository,
        dry_run: bool,
        preview_limit: int,
    ) -> None:
        report.messages_scanned += len(messages)
        report.messages_imported += len(messages)
        report.messages_with_text += sum(1 for m in messages if not m.is_empty)
        report.messages_empty += sum(1 for m in messages if m.is_empty)
        report.unknown_message_types += sum(
            1 for m in messages if m.message_type is MessageType.UNKNOWN
        )
        report.earliest, report.latest = _extend_range(report, messages)

        # Stage 1 — the normalised record (§2.3).
        path = normalized_file(
            root,
            source_type=conversation.source_type,
            conversation_id=conversation.id,
            conversation_name=conversation.title,
        )
        if not dry_run:
            write_messages(path, messages)
            for message in messages:
                states.mark(
                    message.id,
                    self.source_type,
                    RECORD_KIND_MESSAGE,
                    IngestionState.NORMALIZED,
                )

        # Stage 2 — segmentation (§15).
        pinned = (
            conversation.participant_names()
            if conversation.conversation_type is ConversationType.DIRECT
            else ()
        )
        segments = segment_messages(
            messages,
            conversation_id=conversation.id,
            params=self.params,
            pinned_participants=pinned,
        )
        report.segments += len(segments)

        # Stage 3 — knowledge documents (§16) and the metadata index (§12/F1).
        for segment in segments:
            document = build_document(segment, conversation)
            markdown = render_markdown(document)
            document_path = knowledge_file(
                root,
                source_type=conversation.source_type,
                segment_id=segment.id,
                conversation_name=conversation.title,
            )
            existing = segments_repo.get(segment.id)
            action = _decide_action(existing, content_hash(markdown), document_path)
            if segment.is_empty:
                report.segments_empty += 1

            if not dry_run:
                if action is not ACTION_UNCHANGED:
                    document_path.parent.mkdir(parents=True, exist_ok=True)
                    document_path.write_text(markdown, encoding="utf-8")
                segments_repo.upsert(
                    segment_id=segment.id,
                    document_id=document.id,
                    source_type=conversation.source_type,
                    conversation_id=conversation.id,
                    conversation_type=str(conversation.conversation_type),
                    conversation_name=conversation.title,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    participants=segment.participants,
                    message_count=segment.message_count,
                    segmentation_version=segment.segmentation_version,
                    document_path=str(document_path),
                    document_body=markdown,
                )
                states.mark(
                    segment.id,
                    self.source_type,
                    RECORD_KIND_SEGMENT,
                    IngestionState.PROCESSED,
                )

            _tally(report, action)
            if len(report.preview) < preview_limit:
                report.preview.append(
                    DocumentPreview(
                        segment_id=segment.id,
                        document_path=str(document_path),
                        conversation_name=conversation.title,
                        conversation_type=str(conversation.conversation_type),
                        start_time=segment.start_time,
                        end_time=segment.end_time,
                        message_count=segment.message_count,
                        participants=segment.participants,
                        estimated_tokens=segment.estimated_tokens,
                        action=action,
                        has_text=not segment.is_empty,
                    )
                )


def _extend_range(
    report: IngestionReport, messages: list[Message]
) -> tuple[datetime | None, datetime | None]:
    earliest = report.earliest
    latest = report.latest
    for message in messages:
        moment = message.timestamp
        earliest = moment if earliest is None or moment < earliest else earliest
        latest = moment if latest is None or moment > latest else latest
    return earliest, latest


def _decide_action(existing: SegmentRow | None, content_hash: str, document_path: Path) -> str:
    """Classify a segment as created, updated or unchanged.

    An existing row whose file has gone missing counts as ``created``: the
    metadata index and the filesystem disagree, and the file is the thing
    retrieval actually reads.
    """
    if existing is None:
        return ACTION_CREATED
    if existing.content_hash == content_hash and document_path.is_file():
        return ACTION_UNCHANGED
    return ACTION_UPDATED


def _tally(report: IngestionReport, action: str) -> None:
    if action == ACTION_CREATED:
        report.documents_created += 1
    elif action == ACTION_UPDATED:
        report.documents_updated += 1
    else:
        report.documents_unchanged += 1
