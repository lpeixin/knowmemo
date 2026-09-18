"""Conversation domain model (design doc §9)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from knowmemo.domain.participant import Participant


class ConversationType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    UNKNOWN = "unknown"


class Conversation(BaseModel):
    """A conversation, normalised across sources.

    ``source_id`` is the identifier the source itself uses (a WeChat chatroom
    id, a mail thread id). ``id`` is KnowMemo's deterministic composite. Keeping
    both means a connector can always be re-run against the original system.
    """

    id: str

    source_type: str
    source_id: str

    title: str

    conversation_type: ConversationType = ConversationType.UNKNOWN

    participants: list[Participant] = Field(default_factory=list)

    start_time: datetime
    end_time: datetime

    message_count: int = 0

    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_time_range(self) -> Conversation:
        if self.end_time < self.start_time:
            raise ValueError(
                f"end_time {self.end_time.isoformat()} precedes "
                f"start_time {self.start_time.isoformat()}"
            )
        return self

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    def participant_names(self) -> list[str]:
        """Display names, falling back to IDs, in stable order."""
        return [p.display_name or p.id for p in self.participants]
