"""Knowledge document domain model (design doc §16).

One conversation segment becomes exactly one document. The frontmatter is the
load-bearing part: it is what makes later filtering by person, conversation,
date range, source, group or message type possible at all.

Design note — WeKnora only accepts ``map[string]string`` metadata on the file
upload endpoint (``docs/plan.md`` finding F3). ``frontmatter()`` therefore
flattens lists into comma-joined strings and datetimes into ISO-8601 strings,
so the same values work both as YAML frontmatter in the Markdown body and as
WeKnora document metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from knowmemo.domain.conversation import ConversationType

KNOWMEMO_VERSION = "0.1"


class KnowledgeDocument(BaseModel):
    """A structured, self-describing conversation document."""

    id: str
    segment_id: str
    source_type: str
    conversation_id: str

    conversation_type: ConversationType = ConversationType.UNKNOWN
    conversation_name: str

    start_time: datetime
    end_time: datetime

    participants: list[str] = Field(default_factory=list)
    message_count: int = 0

    #: Bumped when the segmentation parameters that produced this document
    #: change. Recorded so a re-index is explainable after the fact.
    segmentation_version: int = 1

    #: The rendered Markdown body, without frontmatter.
    body: str = ""

    metadata: dict[str, Any] = Field(default_factory=dict)

    def frontmatter(self) -> dict[str, Any]:
        """Return frontmatter as flat, string-safe values.

        Every value is a ``str``, ``int`` or ``list[str]`` so the result is
        valid YAML and can also be sent to WeKnora without further coercion.
        """
        return {
            "knowmemo_version": KNOWMEMO_VERSION,
            "source_type": self.source_type,
            "conversation_id": self.conversation_id,
            "segment_id": self.segment_id,
            "conversation_type": str(self.conversation_type),
            "conversation_name": self.conversation_name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "participants": list(self.participants),
            "message_count": self.message_count,
            "segmentation_version": self.segmentation_version,
        }

    def weknora_metadata(self) -> dict[str, str]:
        """Return metadata in WeKnora's ``map[string]string`` shape.

        Lists are comma-joined and ``participants`` is duplicated as
        ``participants_csv`` so a substring match still works after flattening.
        """
        flat: dict[str, str] = {}
        for key, value in self.frontmatter().items():
            if isinstance(value, list):
                flat[key] = ",".join(str(item) for item in value)
            else:
                flat[key] = str(value)
        flat["participants_csv"] = ",".join(self.participants)
        return flat
