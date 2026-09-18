"""Environment diagnostics (``knowmemo doctor``).

The assertions here encode a design rule, not just behaviour: the MVP
ingestion path must work without WeKnora, without Docker and without an LLM
(design doc §17), so none of those may ever be reported as a hard failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowmemo.config.settings import Settings
from knowmemo.health import CheckStatus, DoctorReport, run_doctor


@pytest.fixture
def offline_settings(tmp_path: Path) -> Settings:
    """Settings whose network endpoints are guaranteed unreachable."""
    return Settings.model_validate(
        {
            "storage": {"database_url": f"sqlite:///{tmp_path / 'data' / 'knowmemo.db'}"},
            "llm": {"base_url": "http://127.0.0.1:1"},
            "embedding": {"base_url": "http://127.0.0.1:1"},
            "weknora": {"base_url": "http://127.0.0.1:1"},
        }
    )


def outcome_for(report: DoctorReport, name: str):
    for outcome in report.outcomes:
        if outcome.name == name:
            return outcome
    raise AssertionError(f"no check named {name!r}; have {[o.name for o in report.outcomes]}")


class TestDoctorReport:
    def test_exit_code_is_zero_without_failures(self) -> None:
        report = DoctorReport()
        report.add("x", CheckStatus.OK, "fine")
        report.add("y", CheckStatus.WARN, "meh")
        assert report.exit_code == 0

    def test_exit_code_is_one_with_a_failure(self) -> None:
        report = DoctorReport()
        report.add("x", CheckStatus.FAIL, "broken")
        assert report.exit_code == 1

    def test_partitions_outcomes(self) -> None:
        report = DoctorReport()
        report.add("a", CheckStatus.OK, "")
        report.add("b", CheckStatus.WARN, "")
        report.add("c", CheckStatus.FAIL, "")
        assert report.ok_count == 1
        assert len(report.warnings) == 1
        assert len(report.failures) == 1


class TestOptionalDependenciesAreNeverFatal:
    def test_unreachable_weknora_is_a_warning(self, offline_settings: Settings) -> None:
        report = run_doctor(offline_settings)
        assert outcome_for(report, "WeKnora").status is CheckStatus.WARN

    def test_unreachable_llm_is_a_warning(self, offline_settings: Settings) -> None:
        report = run_doctor(offline_settings)
        assert outcome_for(report, "LLM runtime").status is CheckStatus.WARN

    def test_absent_docker_is_a_warning(self, offline_settings: Settings, monkeypatch) -> None:
        monkeypatch.setattr("knowmemo.health.shutil.which", lambda _: None)
        report = run_doctor(offline_settings)
        assert outcome_for(report, "Docker").status is CheckStatus.WARN

    def test_offline_environment_still_passes_doctor(
        self, offline_settings: Settings, monkeypatch
    ) -> None:
        """The whole point: a machine with no WeKnora and no Docker is fine."""
        monkeypatch.setattr("knowmemo.health.shutil.which", lambda _: None)
        report = run_doctor(offline_settings)
        assert report.failures == []
        assert report.exit_code == 0


class TestLocalChecks:
    def test_python_version_passes_on_a_supported_interpreter(
        self, offline_settings: Settings
    ) -> None:
        report = run_doctor(offline_settings)
        assert outcome_for(report, "Python").status is CheckStatus.OK

    def test_data_directories_are_created(self, offline_settings: Settings) -> None:
        run_doctor(offline_settings)
        root = offline_settings.storage.sqlite_path.parent  # type: ignore[union-attr]
        for name in ("raw", "normalized", "knowledge", "logs"):
            assert (root / name).is_dir()

    def test_database_tables_are_created(self, offline_settings: Settings) -> None:
        report = run_doctor(offline_settings)
        assert outcome_for(report, "Metadata database").status is CheckStatus.OK

    def test_missing_wechat_path_is_a_failure(self, tmp_path: Path) -> None:
        settings = Settings.model_validate(
            {
                "storage": {"database_url": f"sqlite:///{tmp_path / 'k.db'}"},
                "wechat": {"enabled": True, "data_path": str(tmp_path / "absent")},
                "llm": {"base_url": "http://127.0.0.1:1"},
                "weknora": {"base_url": "http://127.0.0.1:1"},
            }
        )
        assert outcome_for(run_doctor(settings), "WeChat data").status is CheckStatus.FAIL

    def test_wechat_check_is_skipped_when_disabled(self, offline_settings: Settings) -> None:
        assert outcome_for(run_doctor(offline_settings), "WeChat source").status is CheckStatus.SKIP

    def test_unconfigured_snapshot_gets_an_actionable_remedy(self, tmp_path: Path) -> None:
        settings = Settings.model_validate(
            {
                "storage": {"database_url": f"sqlite:///{tmp_path / 'k.db'}"},
                "wechat": {"enabled": True, "data_path": str(tmp_path)},
                "llm": {"base_url": "http://127.0.0.1:1"},
                "weknora": {"base_url": "http://127.0.0.1:1"},
            }
        )
        outcome = outcome_for(run_doctor(settings), "WeChat snapshot")
        assert outcome.status is CheckStatus.WARN
        assert outcome.remedy and "snapshot" in outcome.remedy


class TestRemedies:
    def test_every_failure_or_warning_carries_guidance(
        self, offline_settings: Settings, monkeypatch
    ) -> None:
        monkeypatch.setattr("knowmemo.health.shutil.which", lambda _: None)
        report = run_doctor(offline_settings)
        for outcome in report.warnings + report.failures:
            assert outcome.remedy, f"{outcome.name} has no remedy"
