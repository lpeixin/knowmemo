"""Conversation segmentation (design doc §15).

A conversation is not one document. A year of chat is unsearchable as a single
unit: the embedding averages everything, the retrieval result is too coarse to
cite, and a summary of it is useless. Segmentation cuts a conversation into
topical-ish chunks at natural pauses.

Three boundaries, checked in this order for each incoming message:

1. **Inactivity** — a gap longer than ``inactivity_minutes`` starts a new
   segment. This is the boundary that carries meaning: a conversation that
   resumes after a night is a different conversation.
2. **Message count** — ``max_messages`` bounds the number of turns.
3. **Token estimate** — ``max_tokens`` bounds the size fed to a downstream
   chunker.

The order matters and is asserted by tests: when several boundaries would fire
at the same message, the *earliest* rule in the list is the one recorded, so
the reported reason is the most meaningful one.

Two deliberate limitations
--------------------------
**No real tokenizer.** :func:`estimate_tokens` is a heuristic, not a
tokenizer, because §17 requires the pipeline to work with no LLM and no
embedding model present. Pulling in a provider's tokenizer would make
segmentation depend on a component the design explicitly makes optional.

**A message is never split.** A single message larger than ``max_tokens``
becomes a segment of its own. Splitting it would require inventing a
continuation marker, and a chunk that starts mid-sentence retrieves badly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from knowmemo.config.settings import SegmentationSection
from knowmemo.domain.ids import make_segment_id
from knowmemo.domain.message import Message

#: Codepoint ranges counted as one token per character. CJK text tokenizes at
#: roughly one token per character, while Latin text averages about four
#: characters per token; a single divisor would be wrong by 4x for one of them.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK radicals
    (0x3000, 0x303F),  # CJK punctuation
    (0x3040, 0x30FF),  # kana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xAC00, 0xD7AF),  # hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # fullwidth forms
)

CHARS_PER_LATIN_TOKEN = 4


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return any(low <= codepoint <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str | None) -> int:
    """Estimate the token count of ``text``.

    CJK characters count as one token each; everything else counts as one
    token per :data:`CHARS_PER_LATIN_TOKEN` characters, rounded up. The result
    is used only to decide where to cut, never to report a cost to a user.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    latin = len(text) - cjk
    return cjk + -(-latin // CHARS_PER_LATIN_TOKEN)


class BoundaryReason(StrEnum):
    """Why a segment ended. Recorded so a boundary can be explained later."""

    INACTIVITY = "inactivity"
    MAX_MESSAGES = "max_messages"
    MAX_TOKENS = "max_tokens"
    END_OF_INPUT = "end_of_input"


class SegmentationParams(BaseModel):
    """The §15 knobs, plus the version that makes them reproducible."""

    inactivity_minutes: int = Field(default=60, gt=0)
    max_messages: int = Field(default=100, gt=0)
    max_tokens: int = Field(default=4000, gt=0)
    #: Participates in segment ID hashing. Changing any parameter above without
    #: bumping this would move every boundary silently; bumping it turns the
    #: change into an explicit, visible re-index (see ``domain/ids.py``).
    version: int = Field(default=1, gt=0)

    @classmethod
    def from_settings(cls, section: SegmentationSection) -> SegmentationParams:
        return cls(
            inactivity_minutes=section.inactivity_minutes,
            max_messages=section.max_messages,
            max_tokens=section.max_tokens,
            version=section.version,
        )

    @property
    def inactivity(self) -> timedelta:
        return timedelta(minutes=self.inactivity_minutes)


class Segment(BaseModel):
    """One contiguous run of messages from a single conversation."""

    id: str
    conversation_id: str
    segmentation_version: int

    messages: list[Message] = Field(default_factory=list)

    #: Why this segment ended. ``END_OF_INPUT`` for the last one.
    boundary_reason: BoundaryReason = BoundaryReason.END_OF_INPUT
    estimated_tokens: int = 0

    #: Names that should match this segment in a metadata query. Usually the
    #: senders present, but a 1:1 conversation pins the counterparty so a
    #: segment in which only one side spoke is still findable by the other
    #: person's name.
    participants: list[str] = Field(default_factory=list)

    @property
    def start_time(self) -> datetime:
        return self.messages[0].timestamp

    @property
    def end_time(self) -> datetime:
        return self.messages[-1].timestamp

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def is_empty(self) -> bool:
        """True when no message carries indexable text."""
        return all(message.is_empty for message in self.messages)


def _sender_label(message: Message) -> str:
    return message.sender_name or message.sender_id


def _order(messages: Sequence[Message]) -> list[Message]:
    """Sort by timestamp, breaking ties on message ID.

    The tie-break is not cosmetic: without it, two messages sharing a timestamp
    could swap order between runs, which would change the segment's first and
    last message IDs and therefore its identity.
    """
    return sorted(messages, key=lambda message: (message.timestamp, message.id))


def segment_messages(
    messages: Sequence[Message],
    *,
    conversation_id: str,
    params: SegmentationParams,
    pinned_participants: Sequence[str] = (),
) -> list[Segment]:
    """Cut ``messages`` into segments according to ``params``.

    ``pinned_participants`` are added to every segment's participant list. Pass
    the counterparty for a 1:1 conversation; pass nothing for a group, where
    pinning every member to every segment would make participant filtering
    match everything.
    """
    ordered = _order(messages)
    if not ordered:
        return []

    segments: list[Segment] = []
    current: list[Message] = []
    current_tokens = 0

    def close(boundary: BoundaryReason) -> None:
        nonlocal current, current_tokens
        segments.append(
            _build_segment(
                current,
                conversation_id=conversation_id,
                params=params,
                boundary_reason=boundary,
                estimated_tokens=current_tokens,
                pinned_participants=pinned_participants,
            )
        )
        current = []
        current_tokens = 0

    for message in ordered:
        if current:
            gap = message.timestamp - current[-1].timestamp
            if gap > params.inactivity:
                close(BoundaryReason.INACTIVITY)
            elif len(current) >= params.max_messages:
                close(BoundaryReason.MAX_MESSAGES)
            else:
                message_tokens = estimate_tokens(message.content)
                if current_tokens + message_tokens > params.max_tokens:
                    close(BoundaryReason.MAX_TOKENS)

        current.append(message)
        current_tokens += estimate_tokens(message.content)

    if current:
        close(BoundaryReason.END_OF_INPUT)

    return segments


def _build_segment(
    messages: list[Message],
    *,
    conversation_id: str,
    params: SegmentationParams,
    boundary_reason: BoundaryReason,
    estimated_tokens: int,
    pinned_participants: Sequence[str],
) -> Segment:
    names: list[str] = []
    seen: set[str] = set()
    for name in list(pinned_participants) + [_sender_label(m) for m in messages]:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    return Segment(
        id=make_segment_id(
            conversation_id,
            messages[0].id,
            messages[-1].id,
            params.version,
        ),
        conversation_id=conversation_id,
        segmentation_version=params.version,
        messages=messages,
        boundary_reason=boundary_reason,
        estimated_tokens=estimated_tokens,
        participants=names,
    )
