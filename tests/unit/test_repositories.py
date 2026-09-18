"""Repositories — the segment metadata index and the §25 state machine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from knowmemo.domain.source import IngestionState, Source, SourceType
from knowmemo.storage.database import create_db_engine, init_db
from knowmemo.storage.repositories import (
    CheckpointRepository,
    RecordStateRepository,
    SegmentKnowledgeRepository,
    SegmentRepository,
    SourceRepository,
)


@pytest.fixture
def session(tmp_path: Path):
    from knowmemo.config.settings import Settings

    settings = Settings.model_validate(
        {"storage": {"database_url": f"sqlite:///{tmp_path / 'test.db'}"}}
    )
    engine = create_db_engine(settings)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db


def add_segment(
    session: Session,
    *,
    segment_id: str,
    conversation_id: str = "wechat:acct:conv",
    conversation_name: str = "Alice",
    participants: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    SegmentRepository(session).upsert(
        segment_id=segment_id,
        document_id=segment_id,
        source_type="wechat",
        conversation_id=conversation_id,
        conversation_type="direct",
        conversation_name=conversation_name,
        start_time=start or datetime(2026, 1, 15, 10, 20),
        end_time=end or datetime(2026, 1, 15, 10, 42),
        participants=participants or ["Me", "Alice"],
        message_count=18,
        segmentation_version=1,
    )
    session.flush()


class TestSourceRepository:
    def test_round_trips_a_source(self, session: Session) -> None:
        repo = SourceRepository(session)
        repo.upsert(
            Source(
                id="wechat:default",
                source_type=SourceType.WECHAT,
                display_name="WeChat",
                path="/tmp/snapshot",
            )
        )
        session.flush()
        loaded = repo.get("wechat:default")
        assert loaded is not None
        assert loaded.path == "/tmp/snapshot"
        assert str(loaded.source_type) == "wechat"

    def test_upsert_updates_rather_than_duplicates(self, session: Session) -> None:
        repo = SourceRepository(session)
        for path in ("/tmp/a", "/tmp/b"):
            repo.upsert(Source(id="s", source_type=SourceType.WECHAT, display_name="W", path=path))
            session.flush()
        assert len(repo.list_all()) == 1
        assert repo.get("s").path == "/tmp/b"  # type: ignore[union-attr]

    def test_delete_reports_whether_anything_went(self, session: Session) -> None:
        repo = SourceRepository(session)
        assert repo.delete("nope") is False


class TestRecordStateRepository:
    def test_records_and_reads_back_a_state(self, session: Session) -> None:
        repo = RecordStateRepository(session)
        repo.mark("m1", "wechat", "message", IngestionState.NORMALIZED)
        session.flush()
        assert repo.state_of("m1") is IngestionState.NORMALIZED

    def test_unknown_record_has_no_state(self, session: Session) -> None:
        assert RecordStateRepository(session).state_of("nope") is None

    def test_retry_overwrites_a_failure(self, session: Session) -> None:
        repo = RecordStateRepository(session)
        repo.mark(
            "m1",
            "wechat",
            "message",
            IngestionState.PARSE_FAILED,
            error_code="MESSAGE_PARSE_ERROR",
        )
        repo.mark("m1", "wechat", "message", IngestionState.NORMALIZED)
        session.flush()
        row = repo.get("m1")
        assert row is not None
        assert row.state == "NORMALIZED"
        assert row.error_code is None

    def test_known_ids_supports_dedup(self, session: Session) -> None:
        repo = RecordStateRepository(session)
        repo.mark("m1", "wechat", "message", IngestionState.INDEXED)
        repo.mark("m2", "wechat", "message", IngestionState.INDEXED)
        session.flush()
        assert repo.known_ids("wechat", "message") == {"m1", "m2"}

    def test_counts_group_by_state(self, session: Session) -> None:
        repo = RecordStateRepository(session)
        repo.mark("m1", "wechat", "message", IngestionState.INDEXED)
        repo.mark("m2", "wechat", "message", IngestionState.INDEXED)
        repo.mark("m3", "wechat", "message", IngestionState.PARSE_FAILED)
        session.flush()
        counts = repo.count_by_state()
        assert counts["INDEXED"] == 2
        assert counts["PARSE_FAILED"] == 1

    def test_state_helpers_classify_correctly(self) -> None:
        assert IngestionState.PARSE_FAILED.is_failure
        assert not IngestionState.INDEXED.is_failure
        assert IngestionState.INDEXED.is_terminal_success


class TestCheckpointRepository:
    def test_advance_only_moves_forward(self, session: Session) -> None:
        repo = CheckpointRepository(session)
        repo.advance("wechat", "acct", datetime(2026, 5, 1))
        repo.advance("wechat", "acct", datetime(2026, 1, 1))
        session.flush()
        row = repo.get("wechat", "acct")
        assert row is not None
        assert row.high_water_mark == datetime(2026, 5, 1)

    def test_records_the_overlap_window(self, session: Session) -> None:
        """A history migration can backfill older messages past the mark."""
        repo = CheckpointRepository(session)
        repo.advance("wechat", "acct", datetime(2026, 5, 1), overlap_seconds=7200)
        session.flush()
        assert repo.get("wechat", "acct").overlap_seconds == 7200  # type: ignore[union-attr]


class TestSegmentRepository:
    def test_finds_by_participant(self, session: Session) -> None:
        add_segment(session, segment_id="seg_1", participants=["Me", "Alice"])
        add_segment(session, segment_id="seg_2", participants=["Me", "Bob"])
        found = SegmentRepository(session).find(participant="Alice")
        assert [row.segment_id for row in found] == ["seg_1"]

    def test_time_filter_uses_overlap_not_containment(self, session: Session) -> None:
        """A conversation spanning a boundary is relevant to both sides of it."""
        add_segment(
            session,
            segment_id="seg_spanning",
            start=datetime(2025, 12, 30),
            end=datetime(2026, 1, 3),
        )
        found = SegmentRepository(session).find(
            start=datetime(2026, 1, 1), end=datetime(2026, 12, 31)
        )
        assert [row.segment_id for row in found] == ["seg_spanning"]

    def test_excludes_segments_outside_the_window(self, session: Session) -> None:
        add_segment(
            session,
            segment_id="seg_old",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 2),
        )
        found = SegmentRepository(session).find(
            start=datetime(2026, 1, 1), end=datetime(2026, 12, 31)
        )
        assert found == []

    def test_combines_participant_and_time_filters(self, session: Session) -> None:
        add_segment(
            session,
            segment_id="seg_hit",
            participants=["Me", "Alice"],
            start=datetime(2025, 8, 12),
            end=datetime(2025, 8, 12, 1),
        )
        add_segment(
            session,
            segment_id="seg_wrong_person",
            participants=["Me", "Bob"],
            start=datetime(2025, 8, 12),
            end=datetime(2025, 8, 12, 1),
        )
        add_segment(
            session,
            segment_id="seg_wrong_year",
            participants=["Me", "Alice"],
            start=datetime(2024, 8, 12),
            end=datetime(2024, 8, 12, 1),
        )
        found = SegmentRepository(session).find(
            participant="Alice",
            start=datetime(2025, 1, 1),
            end=datetime(2026, 1, 1),
        )
        assert [row.segment_id for row in found] == ["seg_hit"]

    def test_records_the_content_hash_when_a_body_is_given(self, session: Session) -> None:
        from knowmemo.domain.ids import content_hash
        from knowmemo.storage.models import SegmentRow

        SegmentRepository(session).upsert(
            segment_id="seg_1",
            document_id="seg_1",
            source_type="wechat",
            conversation_id="c",
            conversation_type="direct",
            conversation_name="Alice",
            start_time=datetime(2026, 1, 1),
            end_time=datetime(2026, 1, 1, 1),
            participants=["Me"],
            message_count=1,
            segmentation_version=1,
            document_path="alice/seg_1.md",
            document_body="body text",
        )
        session.flush()
        row = session.get(SegmentRow, "seg_1")
        assert row is not None
        assert row.content_hash == content_hash("body text")
        assert row.document_path == "alice/seg_1.md"
        assert row.participants_csv == "Me"

    def test_counts_and_clears(self, session: Session) -> None:
        add_segment(session, segment_id="seg_1")
        add_segment(session, segment_id="seg_2")
        repo = SegmentRepository(session)
        assert repo.count() == 2
        assert repo.delete_all() == 2
        assert repo.count() == 0


class TestSegmentKnowledgeRepository:
    def test_maps_segments_to_weknora_ids(self, session: Session) -> None:
        add_segment(session, segment_id="seg_1")
        add_segment(session, segment_id="seg_2")
        repo = SegmentKnowledgeRepository(session)
        repo.record_upload(
            segment_id="seg_1",
            knowledge_base_id="kb1",
            weknora_knowledge_id="uuid-1",
            uploaded_content_hash="hash",
        )
        session.flush()
        assert repo.knowledge_ids_for(["seg_1", "seg_2"]) == ["uuid-1"]

    def test_handles_an_empty_input(self, session: Session) -> None:
        assert SegmentKnowledgeRepository(session).knowledge_ids_for([]) == []

    def test_reupload_updates_in_place(self, session: Session) -> None:
        add_segment(session, segment_id="seg_1")
        repo = SegmentKnowledgeRepository(session)
        repo.record_upload(
            segment_id="seg_1",
            knowledge_base_id="kb1",
            weknora_knowledge_id="uuid-1",
            uploaded_content_hash="h1",
        )
        repo.record_upload(
            segment_id="seg_1",
            knowledge_base_id="kb1",
            weknora_knowledge_id="uuid-1",
            uploaded_content_hash="h2",
        )
        session.flush()
        assert repo.get("seg_1").uploaded_content_hash == "h2"  # type: ignore[union-attr]
