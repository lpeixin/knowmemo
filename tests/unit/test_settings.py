"""Configuration loading (design doc §29)."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowmemo.config.settings import (
    ConfigError,
    env_overrides,
    find_config_file,
    load_settings,
)


class TestDefaults:
    def test_works_with_no_config_file_at_all(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KNOWMEMO_CONFIG", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = load_settings(environ={})
        assert settings.app.name == "KnowMemo"
        assert settings.source_path is None

    def test_no_machine_specific_path_is_assumed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = load_settings(environ={})
        assert settings.wechat.data_path is None
        assert settings.wechat.snapshot_path is None
        assert settings.weknora.knowledge_base_id is None
        assert settings.embedding.model is None

    def test_segmentation_defaults_match_the_design_document(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        segmentation = load_settings(environ={}).segmentation
        assert segmentation.inactivity_minutes == 60
        assert segmentation.max_messages == 100
        assert segmentation.max_tokens == 4000
        assert segmentation.version == 1

    def test_processing_is_off_by_default(self, tmp_path: Path, monkeypatch) -> None:
        """§17: the system must work without an LLM."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        processing = load_settings(environ={}).processing
        assert processing.enable_summary is False
        assert processing.enable_entity_extraction is False


class TestYamlLoading:
    def test_reads_an_explicit_file(self, tmp_path: Path) -> None:
        config = tmp_path / "knowmemo.yaml"
        config.write_text(
            "app:\n"
            "  log_level: DEBUG\n"
            "storage:\n"
            "  database_url: sqlite:///./data/test.db\n"
            "wechat:\n"
            "  enabled: true\n"
            "  snapshot_path: /tmp/snapshot\n",
            encoding="utf-8",
        )
        settings = load_settings(config, environ={})
        assert settings.app.log_level == "DEBUG"
        assert settings.wechat.enabled is True
        assert settings.wechat.snapshot_path == Path("/tmp/snapshot")
        assert settings.source_path == config

    def test_environment_overrides_the_file(self, tmp_path: Path) -> None:
        config = tmp_path / "knowmemo.yaml"
        config.write_text("app:\n  log_level: INFO\n", encoding="utf-8")
        settings = load_settings(config, environ={"KNOWMEMO_APP__LOG_LEVEL": "WARNING"})
        assert settings.app.log_level == "WARNING"

    def test_env_values_are_coerced_not_left_as_strings(self, tmp_path: Path) -> None:
        config = tmp_path / "knowmemo.yaml"
        config.write_text("app:\n  log_level: INFO\n", encoding="utf-8")
        settings = load_settings(
            config,
            environ={
                "KNOWMEMO_WECHAT__ENABLED": "true",
                "KNOWMEMO_SEGMENTATION__MAX_MESSAGES": "250",
            },
        )
        assert settings.wechat.enabled is True
        assert settings.segmentation.max_messages == 250

    def test_missing_explicit_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="does not exist"):
            load_settings(tmp_path / "nope.yaml", environ={})

    def test_invalid_yaml_is_an_error(self, tmp_path: Path) -> None:
        config = tmp_path / "knowmemo.yaml"
        config.write_text("app: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_settings(config, environ={})

    def test_invalid_values_are_rejected(self, tmp_path: Path) -> None:
        config = tmp_path / "knowmemo.yaml"
        config.write_text("segmentation:\n  max_messages: -5\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="failed validation"):
            load_settings(config, environ={})


class TestEnvOverrides:
    def test_nests_on_the_double_underscore(self) -> None:
        overrides = env_overrides({"KNOWMEMO_WECHAT__SNAPSHOT_PATH": "/tmp/x"})
        assert overrides == {"wechat": {"snapshot_path": "/tmp/x"}}

    def test_ignores_unrelated_variables(self) -> None:
        assert env_overrides({"PATH": "/usr/bin", "OTHER": "1"}) == {}

    def test_ignores_the_config_path_variable_itself(self) -> None:
        assert env_overrides({"KNOWMEMO_CONFIG": "/tmp/c.yaml"}) == {}


class TestFindConfigFile:
    def test_prefers_an_explicit_path(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom.yaml"
        assert find_config_file(explicit) == explicit

    def test_uses_the_environment_variable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KNOWMEMO_CONFIG", str(tmp_path / "env.yaml"))
        assert find_config_file() == tmp_path / "env.yaml"

    def test_returns_none_when_nothing_exists(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KNOWMEMO_CONFIG", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert find_config_file() is None


class TestSqlitePath:
    def test_derives_a_path_from_a_sqlite_url(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = load_settings(environ={})
        assert settings.storage.sqlite_path == Path("./data/knowmemo.db")

    def test_returns_none_for_a_non_sqlite_url(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        settings = load_settings(
            environ={"KNOWMEMO_STORAGE__DATABASE_URL": "postgresql://localhost/knowmemo"}
        )
        assert settings.storage.sqlite_path is None
