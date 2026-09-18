"""Unified message domain model (design doc §8, §2.2).

This is the source-agnostic centre of KnowMemo. There is deliberately no
``WeChatMessage``: a connector's job is to produce *this* type, and nothing in
the core pipeline knows which connector produced it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MessageType(StrEnum):
    """Message kinds the schema supports.

    §8 notes the MVP processes a subset (text, image, file, link, system) but
    the schema carries all of them so connectors never need a schema change to
    start reporting a new kind.
    """

    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    LOCATION = "location"
    CONTACT = "contact"
    STICKER = "sticker"
    SYSTEM = "system"
    LINK = "link"
    UNKNOWN = "unknown"


class Message(BaseModel):
    """A single normalised message from any source."""

    id: str
    source_type: str
    source_account_id: str | None = None

    conversation_id: str
    sender_id: str
    sender_name: str | None = None

    timestamp: datetime

    message_type: MessageType = MessageType.UNKNOWN
    content: str | None = None

    reply_to_id: str | None = None

    attachment_ids: list[str] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _require_aware_or_naive(cls, value: datetime) -> datetime:
        """Accept both, but reject nothing — connectors vary.

        The WeChat connector emits naive local time because WeChat stores a
        bare Unix timestamp with no zone. We do not invent a zone here; the
        segment builder sorts on this value and nothing else.
        """
        return value

    @property
    def is_empty(self) -> bool:
        """True when there is no text to index.

        Media-only messages are legitimately empty; the pipeline keeps them
        for timeline fidelity but does not feed them to retrieval as content.
        """
        return not (self.content or "").strip()
