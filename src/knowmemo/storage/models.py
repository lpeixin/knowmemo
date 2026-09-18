"""SQLAlchemy table definitions for KnowMemo's own metadata database.

Scope boundary (§6, §45): this database holds KnowMemo's *own* bookkeeping —
configured sources, ingestion state, checkpoints, and the segment-level index
used for metadata filtering. It is **not** a copy of the conversation corpus:
normalised records live on disk as JSONL (§2.3) and knowledge documents live in
``data/knowledge/``. WeKnora owns the retrieval index.

Two tables deserve explanation because the original design document does not
have them (see ``docs/plan.md`` findings F1 and F2):

``segments``
    The metadata index. WeKnora's ``/knowledge-search`` accepts no metadata
    filter, so a temporal or participant-scoped query must first be resolved to
    a candidate set of documents here, then pushed down via ``knowledge_ids``.

``segment_knowledge_map``
    WeKnora offers no client-specified-ID upsert, so the deterministic segment
    ID cannot travel to WeKnora. This table is the translation layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all KnowMemo tables."""


class SourceRow(Base):
    """A configured data source (§12)."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ImportRunRow(Base):
    """One ingestion run — the manifest of §26."""

    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: ``running`` | ``success`` | ``partial`` | ``failed``
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    messages_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_import_runs_source", "source_type", "source_id"),)


class RecordStateRow(Base):
    """Per-record pipeline state (§25).

    ``record_id`` is the deterministic ID from :mod:`knowmemo.domain.ids`, so a
    re-run can ask "have I seen this exact record before, and did it succeed?"
    without re-parsing anything.
    """

    __tablename__ = "record_states"

    record_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    record_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_record_states_state", "state"),
        Index("ix_record_states_source", "source_type", "record_kind"),
    )


class CheckpointRow(Base):
    """Incremental-sync checkpoint (§26).

    Deliberately not a bare ``last_scan_time``: WeChat orders by ``create_time``
    and a history migration can backfill *older* messages after a scan, so the
    checkpoint records the high-water mark **and** the overlap window used to
    re-check the boundary.
    """

    __tablename__ = "checkpoints"

    source_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    high_water_mark: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    overlap_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SegmentRow(Base):
    """Segment-level metadata index — the pre-filter for retrieval.

    WeKnora cannot filter on metadata (``docs/plan.md`` F1). This table answers
    "which segments involve Alice, in 2025?" so the answer can be handed to
    WeKnora as a ``knowledge_ids`` list.

    ``participants_csv`` is a denormalised comma-joined column rather than a
    join table: the access pattern is substring membership over a small list,
    and a join table would add a hop without buying anything at this scale.
    """

    __tablename__ = "segments"

    segment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)

    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(512), nullable=False)
    conversation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_name: Mapped[str] = mapped_column(String(255), nullable=False)

    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    participants_csv: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    segmentation_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Relative path under ``data/knowledge/``. Lets a retrieval result be
    #: rendered without a database round-trip per hit.
    document_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    knowledge: Mapped[SegmentKnowledgeRow | None] = relationship(
        back_populates="segment", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_segments_conversation", "conversation_id"),
        Index("ix_segments_time", "start_time", "end_time"),
        Index("ix_segments_source", "source_type"),
    )


class SegmentKnowledgeRow(Base):
    """Mapping from a KnowMemo segment to its WeKnora knowledge ID.

    Required because WeKnora has no upsert-by-client-ID (``docs/plan.md`` F2):
    the deterministic segment ID stops at this boundary, and the UUID WeKnora
    assigns on upload is remembered here.
    """

    __tablename__ = "segment_knowledge_map"

    segment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("segments.segment_id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[str] = mapped_column(String(255), nullable=False)
    weknora_knowledge_id: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Hash of the uploaded payload, so a re-run can tell "unchanged" from
    #: "changed" without asking WeKnora.
    uploaded_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    parse_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    #: ``score``-adjacent bookkeeping for later diagnostics.
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    segment: Mapped[SegmentRow] = relationship(back_populates="knowledge")

    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "weknora_knowledge_id", name="uq_kb_knowledge"),
    )
