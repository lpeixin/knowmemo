"""Participant domain model (design doc §9)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ParticipantRole(StrEnum):
    """Whether a participant is the account owner or a counterparty.

    This matters for retrieval: "what did *I* say" and "what did Alice say"
    are different questions over the same message stream.
    """

    SELF = "self"
    OTHER = "other"
    UNKNOWN = "unknown"


class Participant(BaseModel):
    """A person or account taking part in a conversation.

    ``id`` is the source-level identity (a wxid, an email address), never a
    display name — display names change and collide.
    """

    id: str
    source_type: str
    display_name: str | None = None
    role: ParticipantRole = ParticipantRole.UNKNOWN
    metadata: dict[str, Any] = Field(default_factory=dict)
