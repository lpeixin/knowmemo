"""Configuration loading (design doc §29).

Sources, in increasing precedence:

1. Built-in defaults declared below.
2. A YAML file — resolved from ``--config``, then ``$KNOWMEMO_CONFIG``, then
   ``./knowmemo.yaml``, ``./knowmemo.local.yaml``, then
   ``~/.config/knowmemo/config.yaml``.
3. Environment variables prefixed ``KNOWMEMO_`` with ``__`` as the nesting
   delimiter, e.g. ``KNOWMEMO_WECHAT__SNAPSHOT_PATH=/tmp/snap``.

No local machine path is hard-coded anywhere. ``wechat.data_path`` and
``wechat.snapshot_path`` ship as ``null`` on purpose.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from knowmemo.errors import ErrorCode, KnowMemoError

ENV_PREFIX = "KNOWMEMO_"
ENV_NESTED_DELIMITER = "__"
ENV_CONFIG_PATH = "KNOWMEMO_CONFIG"

DEFAULT_CONFIG_FILENAMES = ("knowmemo.yaml", "knowmemo.local.yaml")
USER_CONFIG_PATH = Path("~/.config/knowmemo/config.yaml")


class ConfigError(KnowMemoError):
    """Raised when configuration cannot be located or validated."""

    code = ErrorCode.SOURCE_NOT_FOUND


class AppSection(BaseModel):
    name: str = "KnowMemo"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True


class StorageSection(BaseModel):
    database_url: str = "sqlite:///./data/knowmemo.db"

    @property
    def sqlite_path(self) -> Path | None:
        """Return the on-disk path when ``database_url`` targets SQLite."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url[len(prefix) :]).expanduser()


class WeChatSection(BaseModel):
    enabled: bool = False
    #: Encrypted WeChat data directory. Only probed for availability, never read.
    data_path: Path | None = None
    #: Plaintext SQLite snapshot directory — the connector's real input contract.
    snapshot_path: Path | None = None
    #: The account's own wxid. Without it, outgoing messages are attributed to a
    #: placeholder rather than to a real identity.
    account_id: str | None = None


class WeKnoraSection(BaseModel):
    base_url: str = "http://localhost:8080"
    api_key: str | None = None
    knowledge_base_id: str | None = None
    timeout_seconds: float = 30.0


class LLMSection(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen3.5:9b"
    timeout_seconds: float = 120.0


class EmbeddingSection(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str | None = None


class ProcessingSection(BaseModel):
    enable_summary: bool = False
    enable_entity_extraction: bool = False


class SegmentationSection(BaseModel):
    inactivity_minutes: int = Field(default=60, gt=0)
    max_messages: int = Field(default=100, gt=0)
    max_tokens: int = Field(default=4000, gt=0)
    #: Participates in segment ID hashing. Bump it when the parameters above
    #: change so that re-tuning is an explicit re-index.
    version: int = Field(default=1, gt=0)


class Settings(BaseModel):
    """Fully resolved KnowMemo configuration."""

    app: AppSection = Field(default_factory=AppSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    wechat: WeChatSection = Field(default_factory=WeChatSection)
    weknora: WeKnoraSection = Field(default_factory=WeKnoraSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    embedding: EmbeddingSection = Field(default_factory=EmbeddingSection)
    processing: ProcessingSection = Field(default_factory=ProcessingSection)
    segmentation: SegmentationSection = Field(default_factory=SegmentationSection)

    #: Where this configuration was loaded from; ``None`` means defaults only.
    source_path: Path | None = Field(default=None, exclude=True)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _coerce(raw: str) -> Any:
    """Parse an environment variable value the way YAML would.

    Lets ``KNOWMEMO_WECHAT__ENABLED=false`` and ``...__DATA_PATH=null`` behave
    as a user expects instead of becoming the strings "false" and "null".
    """
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect ``KNOWMEMO_*`` variables into a nested mapping."""
    environ = environ if environ is not None else dict(os.environ)
    result: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or key == ENV_CONFIG_PATH:
            continue
        path = key[len(ENV_PREFIX) :].lower().split(ENV_NESTED_DELIMITER)
        cursor = result
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):  # pragma: no cover - defensive
                break
        else:
            cursor[path[-1]] = _coerce(value)
    return result


def find_config_file(explicit: Path | None = None) -> Path | None:
    """Resolve the configuration file to use, or ``None`` for pure defaults."""
    if explicit is not None:
        return explicit
    from_env = os.environ.get(ENV_CONFIG_PATH)
    if from_env:
        return Path(from_env).expanduser()
    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    user_candidate = USER_CONFIG_PATH.expanduser()
    return user_candidate if user_candidate.is_file() else None


def load_settings(
    config_path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Load and validate settings. Raises :class:`ConfigError` on bad input."""
    resolved = find_config_file(config_path)
    data: dict[str, Any] = {}

    if resolved is not None:
        if not resolved.is_file():
            raise ConfigError("configuration file does not exist", path=str(resolved))
        try:
            loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError("configuration file is not valid YAML", path=str(resolved)) from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError("configuration root must be a mapping", path=str(resolved))
        data = loaded or {}

    data = _deep_merge(data, env_overrides(environ))

    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            "configuration failed validation",
            path=str(resolved) if resolved else "<defaults>",
            detail=str(exc.error_count()) + " error(s)",
        ) from exc

    settings.source_path = resolved
    return settings
