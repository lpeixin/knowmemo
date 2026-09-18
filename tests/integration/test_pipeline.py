"""End-to-end ingestion (§15, §16, §25, §26 — the §46 acceptance path).

Everything runs against generated synthetic snapshots: no Docker, no sudo, no
real WeChat data. The properties asserted here are the ones the milestone is
judged on — a dry run that predicts correctly and writes nothing, a real run
that materialises documents, and a re-run that is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowmemo.config.settings import Settings, StorageSection, WeChatSection
from knowmemo.errors import SourceNotFoundError
from knowmemo.ingestion.pipeline import IngestionPipeline
from knowmemo.storage.database import (
    create_db_engine,
    data_root,
    make_session_factory,
    session_scope,
)
from knowmemo.storage.repositories import (
    ImportRunRepository,
    RecordStateRepository,
    SegmentRepository,
)
from tests.fixtures.wechat.builder import build_alice_snapshot, build_mixed_snapshot


def settings_for(tmp_path: Path, snapshot: Path | None) -> Settings:
    return Settings(
        storage=StorageSection(database_url=f"sqlite:///{tmp_path / 'store' / 'knowmemo.db'}"),
        wechat=WeChatSection(enabled=True, snapshot_path=snapshot, account_id="wxid_me"),
    )


def pipeline_for(settings: Settings) -> IngestionPipeline:
    return IngestionPipeline(settings, source_type="wechat")


def alice(tmp_path: Path) -> tuple[Settings, IngestionPipeline]:
    settings = settings_for(tmp_path, build_alice_snapshot(tmp_path / "snap"))
    return settings, pipeline_for(settings)


def knowledge_files(settings: Settings) -> list[Path]:
    root = data_root(settings) / "knowledge"
    return sorted(root.rglob("*.md")) if root.exists() else []


def normalized_files(settings: Settings) -> list[Path]:
    root = data_root(settings) / "normalized"
    return sorted(root.rglob("*.jsonl")) if root.exists() else []


class TestDryRun:
    def test_a_dry_run_writes_no_documents(self, tmp_path: Path) -> None:
        """A command that writes the documents is not a dry run."""
        settings, pipeline = alice(tmp_path)
        report = pipeline.run(dry_run=True)

        assert report.dry_run
        assert report.documents_created == 2
        assert knowledge_files(settings) == []
        assert normalized_files(settings) == []

    def test_a_dry_run_predicts_what_a_real_run_does(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        predicted = pipeline.run(dry_run=True)

        _, real = alice(tmp_path)
        actual = real.run()

        assert predicted.documents_created == actual.documents_created
        assert predicted.segments == actual.segments
        assert predicted.messages_scanned == actual.messages_scanned

    def test_a_dry_run_is_still_recorded_in_the_manifest(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        pipeline.run(dry_run=True)

        engine = create_db_engine(settings)
        with session_scope(make_session_factory(engine)) as session:
            latest = ImportRunRepository(session).latest()
        assert latest is not None
        assert latest.dry_run is True
        assert latest.status == "success"

    def test_the_preview_is_capped(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, build_mixed_snapshot(tmp_path / "snap"))
        report = pipeline_for(settings).run(dry_run=True, preview_limit=2)
        assert len(report.preview) == 2
        assert report.segments == 3

    def test_preview_entries_name_their_conversation(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run(dry_run=True)
        assert {item.conversation_name for item in report.preview} == {"Alice"}
        assert all(item.action == "created" for item in report.preview)


class TestRealRun:
    def test_documents_are_materialised(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()

        assert not report.dry_run
        assert report.status == "success"
        assert report.documents_created == 2
        assert len(knowledge_files(settings)) == 2

    def test_the_normalised_record_holds_every_message(self, tmp_path: Path) -> None:
        """§2.3: keep what was actually read, media included."""
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()

        files = normalized_files(settings)
        assert len(files) == 1
        lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == report.messages_scanned == 5

    def test_documents_carry_their_frontmatter(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        pipeline.run()
        text = knowledge_files(settings)[0].read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "segment_id:" in text
        assert "## Transcript" in text

    def test_the_segment_index_is_populated(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()

        engine = create_db_engine(settings)
        with session_scope(make_session_factory(engine)) as session:
            repository = SegmentRepository(session)
            assert repository.count() == report.segments
            assert repository.find(participant="Alice")

    def test_the_state_machine_reaches_processed(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        pipeline.run()

        engine = create_db_engine(settings)
        with session_scope(make_session_factory(engine)) as session:
            counts = RecordStateRepository(session).count_by_state()
        assert counts["NORMALIZED"] == 5
        assert counts["PROCESSED"] == 2
        assert not any(state.endswith("_FAILED") for state in counts)

    def test_the_manifest_records_the_totals(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()

        engine = create_db_engine(settings)
        with session_scope(make_session_factory(engine)) as session:
            latest = ImportRunRepository(session).latest()
        assert latest is not None
        assert latest.dry_run is False
        assert latest.documents_created == report.documents_created
        assert latest.messages_scanned == 5

    def test_media_only_messages_are_counted_separately(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, build_mixed_snapshot(tmp_path / "snap"))
        report = pipeline_for(settings).run()

        assert report.conversations == 3
        assert report.messages_scanned == 10
        assert report.messages_empty >= 1
        assert report.messages_with_text + report.messages_empty == report.messages_scanned


class TestIdempotency:
    def test_a_second_run_changes_nothing(self, tmp_path: Path) -> None:
        """§2.4: importing twice must not create a second copy of anything."""
        settings, pipeline = alice(tmp_path)
        pipeline.run()

        second = pipeline_for(settings).run()
        assert second.documents_created == 0
        assert second.documents_updated == 0
        assert second.documents_unchanged == 2
        assert len(knowledge_files(settings)) == 2

    def test_a_changed_document_is_reported_as_updated(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        pipeline.run()

        engine = create_db_engine(settings)
        with session_scope(make_session_factory(engine)) as session:
            repository = SegmentRepository(session)
            row = repository.find()[0]
            repository.upsert(
                segment_id=row.segment_id,
                document_id=row.document_id,
                source_type=row.source_type,
                conversation_id=row.conversation_id,
                conversation_type=row.conversation_type,
                conversation_name=row.conversation_name,
                start_time=row.start_time,
                end_time=row.end_time,
                participants=row.participants_csv.split(","),
                message_count=row.message_count,
                segmentation_version=row.segmentation_version,
                document_path=row.document_path,
                document_body="stale content",
            )

        second = pipeline_for(settings).run()
        assert second.documents_updated == 1
        assert second.documents_unchanged == 1


class TestFailureModes:
    def test_an_unconfigured_source_raises(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, None)
        with pytest.raises(SourceNotFoundError):
            pipeline_for(settings).run()

    def test_a_missing_snapshot_raises(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, tmp_path / "nowhere")
        with pytest.raises(SourceNotFoundError, match="does not exist"):
            pipeline_for(settings).run()

    def test_the_report_stays_clean_on_success(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()
        assert report.is_clean
        assert report.failures == []
        assert report.error_summary is None


class TestSegmentationParameters:
    def test_the_reported_period_covers_the_snapshot(self, tmp_path: Path) -> None:
        settings, pipeline = alice(tmp_path)
        report = pipeline.run()
        assert report.earliest is not None
        assert report.latest is not None
        assert report.earliest < report.latest

    def test_retuning_the_threshold_changes_the_segmentation(self, tmp_path: Path) -> None:
        """And, because the version is part of the hash, it is visible."""
        settings, pipeline = alice(tmp_path)
        assert pipeline.run(dry_run=True).segments == 2

        relaxed = pipeline_for(settings)
        relaxed.params = relaxed.params.model_copy(update={"inactivity_minutes": 600, "version": 2})
        assert relaxed.run(dry_run=True).segments == 1
