"""Source descriptor and ingestion state (design doc §12, §25).

The connector registry is deliberately open: ``SourceType`` lists the sources
the architecture anticipates (§2.2, §37), not the ones implemented today.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    """Source kinds KnowMemo anticipates (§2.2, §37).

    Only ``WECHAT`` is implemented in the MVP; the rest exist so that adding a
    connector never requires touching the core domain.
    """

    WECHAT = "wechat"
    EMAIL = "email"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    OBSIDIAN = "obsidian"
    NOTION = "notion"
    MARKDOWN = "markdown"
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    FILESYSTEM = "filesystem"
    BROWSER = "browser"
    CALENDAR = "calendar"


class SourceStatus(StrEnum):
    """Outcome of a discovery probe (§12)."""

    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


class IngestionState(StrEnum):
    """Per-record pipeline state (§25).

    The happy path is ``DISCOVERED -> PARSED -> NORMALIZED -> PROCESSED ->
    INDEXED``. The three failure states are terminal for a given run but each
    stage is individually retryable.
    """

    DISCOVERED = "DISCOVERED"
    PARSED = "PARSED"
    NORMALIZED = "NORMALIZED"
    PROCESSED = "PROCESSED"
    INDEXED = "INDEXED"

    PARSE_FAILED = "PARSE_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"
    INDEX_FAILED = "INDEX_FAILED"

    @property
    def is_failure(self) -> bool:
        return self in {
            IngestionState.PARSE_FAILED,
            IngestionState.PROCESS_FAILED,
            IngestionState.INDEX_FAILED,
        }

    @property
    def is_terminal_success(self) -> bool:
        return self is IngestionState.INDEXED


class Source(BaseModel):
    """A configured data source.

    ``status`` is a discovery result, not persisted configuration — it is
    recomputed by ``knowmemo source scan``.
    """

    id: str
    source_type: SourceType
    display_name: str
    enabled: bool = True

    #: Where the source lives. ``None`` means "not configured yet" and is the
    #: correct state for a fresh install — no machine path is ever assumed.
    path: str | None = None

    status: SourceStatus = SourceStatus.NOT_CONFIGURED
    status_detail: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
