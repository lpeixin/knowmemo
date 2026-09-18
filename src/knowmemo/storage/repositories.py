"""Repositories — the only place that knows SQL.

Application code talks to these classes, never to a :class:`Session` directly.
That keeps query logic testable and keeps SQLAlchemy out of the domain layer
(design doc §42 Rule 3).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from knowmemo.domain.ids import content_hash
from knowmemo.domain.source import IngestionState, Source, SourceStatus, SourceType
from knowmemo.storage.models import (
    CheckpointRow,
    ImportRunRow,
    RecordStateRow,
    SegmentKnowledgeRow,
    SegmentRow,
    SourceRow,
)


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[Source]:
        rows = self.session.scalars(select(SourceRow).order_by(SourceRow.id)).all()
        return [self._to_domain(row) for row in rows]

    def get(self, source_id: str) -> Source | None:
        row = self.session.get(SourceRow, source_id)
        return self._to_domain(row) if row else None

    def upsert(self, source: Source) -> Source:
        row = self.session.get(SourceRow, source.id)
        if row is None:
            row = SourceRow(id=source.id, source_type=str(source.source_type))
            self.session.add(row)
        row.display_name = source.display_name
        row.enabled = source.enabled
        row.path = source.path
        row.config_json = json.dumps(source.metadata, ensure_ascii=False)
        self.session.flush()
        return self._to_domain(row)

    def delete(self, source_id: str) -> bool:
        row = self.session.get(SourceRow, source_id)
        if row is None:
            return False
        self.session.delete(row)
        return True

    @staticmethod
    def _to_domain(row: SourceRow) -> Source:
        metadata: dict[str, Any] = {}
        if row.config_json:
            try:
                metadata = json.loads(row.config_json)
            except json.JSONDecodeError:
                metadata = {}
        return Source(
            id=row.id,
            source_type=SourceType(row.source_type),
            display_name=row.display_name,
            enabled=row.enabled,
            path=row.path,
            status=SourceStatus.AVAILABLE if row.path else SourceStatus.NOT_CONFIGURED,
            metadata=metadata,
        )


class RecordStateRepository:
    """Tracks the §25 state machine per record."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, record_id: str) -> RecordStateRow | None:
        return self.session.get(RecordStateRow, record_id)

    def state_of(self, record_id: str) -> IngestionState | None:
        row = self.get(record_id)
        return IngestionState(row.state) if row else None

    def known_ids(self, source_type: str, record_kind: str) -> set[str]:
        """All record IDs already seen, for cheap dedup checks."""
        rows = self.session.scalars(
            select(RecordStateRow.record_id).where(
                RecordStateRow.source_type == source_type,
                RecordStateRow.record_kind == record_kind,
            )
        ).all()
        return set(rows)

    def mark(
        self,
        record_id: str,
        source_type: str,
        record_kind: str,
        state: IngestionState,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        row = self.session.get(RecordStateRow, record_id)
        if row is None:
            row = RecordStateRow(
                record_id=record_id, source_type=source_type, record_kind=record_kind
            )
            self.session.add(row)
        row.state = str(state)
        row.error_code = error_code
        row.error_detail = error_detail
        self.session.flush()

    def count_by_state(self, source_type: str | None = None) -> dict[str, int]:
        query = select(RecordStateRow.state, RecordStateRow.record_id)
        if source_type:
            query = query.where(RecordStateRow.source_type == source_type)
        counts: dict[str, int] = {}
        for state, _ in self.session.execute(query):
            counts[state] = counts.get(state, 0) + 1
        return counts


class ImportRunRepository:
    """Persists the §26 manifest."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self,
        run_id: str,
        source_type: str,
        source_id: str,
        *,
        dry_run: bool,
        started_at: datetime,
    ) -> ImportRunRow:
        row = ImportRunRow(
            id=run_id,
            source_type=source_type,
            source_id=source_id,
            started_at=started_at,
            dry_run=dry_run,
            status="running",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        messages_scanned: int = 0,
        messages_imported: int = 0,
        documents_created: int = 0,
        documents_updated: int = 0,
        errors: int = 0,
        error_summary: str | None = None,
    ) -> None:
        row = self.session.get(ImportRunRow, run_id)
        if row is None:
            raise KeyError(f"unknown import run {run_id}")
        row.finished_at = finished_at
        row.status = status
        row.messages_scanned = messages_scanned
        row.messages_imported = messages_imported
        row.documents_created = documents_created
        row.documents_updated = documents_updated
        row.errors = errors
        row.error_summary = error_summary
        self.session.flush()

    def latest(self, source_type: str | None = None) -> ImportRunRow | None:
        query = select(ImportRunRow).order_by(ImportRunRow.started_at.desc()).limit(1)
        if source_type:
            query = query.where(ImportRunRow.source_type == source_type)
        return self.session.scalars(query).first()


class CheckpointRepository:
    """High-water marks for incremental sync (§26, with the F-fix from the plan)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, source_type: str, source_id: str) -> CheckpointRow | None:
        return self.session.get(CheckpointRow, (source_type, source_id))

    def advance(
        self,
        source_type: str,
        source_id: str,
        high_water_mark: datetime,
        *,
        overlap_seconds: int = 3600,
    ) -> None:
        row = self.get(source_type, source_id)
        if row is None:
            row = CheckpointRow(source_type=source_type, source_id=source_id)
            self.session.add(row)
        if row.high_water_mark is None or high_water_mark > row.high_water_mark:
            row.high_water_mark = high_water_mark
        row.overlap_seconds = overlap_seconds
        self.session.flush()


class SegmentRepository:
    """The metadata index that stands in for WeKnora's missing metadata filter."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, segment_id: str) -> SegmentRow | None:
        return self.session.get(SegmentRow, segment_id)

    def upsert(
        self,
        *,
        segment_id: str,
        document_id: str,
        source_type: str,
        conversation_id: str,
        conversation_type: str,
        conversation_name: str,
        start_time: datetime,
        end_time: datetime,
        participants: list[str],
        message_count: int,
        segmentation_version: int,
        document_path: str | None = None,
        document_body: str | None = None,
    ) -> SegmentRow:
        row = self.session.get(SegmentRow, segment_id)
        if row is None:
            row = SegmentRow(segment_id=segment_id, document_id=document_id)
            self.session.add(row)
        row.source_type = source_type
        row.conversation_id = conversation_id
        row.conversation_type = conversation_type
        row.conversation_name = conversation_name
        row.start_time = start_time
        row.end_time = end_time
        row.participants_csv = ",".join(participants)
        row.message_count = message_count
        row.segmentation_version = segmentation_version
        row.document_path = document_path
        if document_body is not None:
            row.content_hash = content_hash(document_body)
        self.session.flush()
        return row

    def find(
        self,
        *,
        participant: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        conversation_id: str | None = None,
        source_type: str | None = None,
        limit: int | None = None,
    ) -> list[SegmentRow]:
        """Resolve a metadata query to candidate segments.

        Time semantics: a segment matches when its ``[start_time, end_time]``
        interval *overlaps* the requested window, not when it is fully
        contained. A conversation that begins in December and runs into January
        is relevant to a query about either month.
        """
        query = select(SegmentRow)
        if participant:
            query = query.where(SegmentRow.participants_csv.contains(participant))
        if conversation_id:
            query = query.where(SegmentRow.conversation_id == conversation_id)
        if source_type:
            query = query.where(SegmentRow.source_type == source_type)
        if start is not None:
            query = query.where(SegmentRow.end_time >= start)
        if end is not None:
            query = query.where(SegmentRow.start_time <= end)
        query = query.order_by(SegmentRow.start_time)
        if limit:
            query = query.limit(limit)
        return list(self.session.scalars(query).all())

    def count(self) -> int:
        return self.session.scalar(select(func.count()).select_from(SegmentRow)) or 0

    def delete_all(self) -> int:
        result = self.session.execute(delete(SegmentRow))
        return result.rowcount or 0


class SegmentKnowledgeRepository:
    """Translation between KnowMemo segment IDs and WeKnora knowledge IDs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, segment_id: str) -> SegmentKnowledgeRow | None:
        return self.session.get(SegmentKnowledgeRow, segment_id)

    def record_upload(
        self,
        *,
        segment_id: str,
        knowledge_base_id: str,
        weknora_knowledge_id: str,
        uploaded_content_hash: str,
        parse_status: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> SegmentKnowledgeRow:
        row = self.get(segment_id)
        if row is None:
            row = SegmentKnowledgeRow(segment_id=segment_id)
            self.session.add(row)
        row.knowledge_base_id = knowledge_base_id
        row.weknora_knowledge_id = weknora_knowledge_id
        row.uploaded_content_hash = uploaded_content_hash
        row.parse_status = parse_status
        row.uploaded_at = uploaded_at
        self.session.flush()
        return row

    def knowledge_ids_for(self, segment_ids: list[str]) -> list[str]:
        """Map segment IDs to WeKnora knowledge IDs, dropping unmapped ones."""
        if not segment_ids:
            return []
        rows = self.session.scalars(
            select(SegmentKnowledgeRow.weknora_knowledge_id).where(
                SegmentKnowledgeRow.segment_id.in_(segment_ids)
            )
        ).all()
        return list(rows)
