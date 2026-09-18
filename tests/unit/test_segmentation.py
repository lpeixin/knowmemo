"""Segmentation behaviour (design doc §15).

The properties tested here are the ones the rest of the system depends on:
boundary precedence, determinism of segment identity, and the guarantee that
appending new messages never renumbers existing segments.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from knowmemo.domain.ids import make_message_id
from knowmemo.domain.message import Message, MessageType
from knowmemo.processing.segmentation import (
    BoundaryReason,
    SegmentationParams,
    estimate_tokens,
    segment_messages,
)

CONVERSATION = "wechat:-:wxid_alice"
BASE = datetime(2025, 1, 1, 9, 0, 0)

PARAMS = SegmentationParams(
    inactivity_minutes=60,
    max_messages=10,
    max_tokens=1000,
    version=1,
)


def message(
    index: int,
    *,
    minutes: float = 0,
    text: str | None = "hello",
    sender: str = "wxid_alice",
    sender_name: str = "Alice",
    message_type: MessageType = MessageType.TEXT,
) -> Message:
    return Message(
        id=make_message_id("wechat", None, CONVERSATION, str(index)),
        source_type="wechat",
        conversation_id=CONVERSATION,
        sender_id=sender,
        sender_name=sender_name,
        timestamp=BASE + timedelta(minutes=minutes),
        message_type=message_type,
        content=text,
    )


def segment(messages: list[Message], **overrides: object):
    params = PARAMS.model_copy(update=overrides) if overrides else PARAMS
    return segment_messages(messages, conversation_id=CONVERSATION, params=params)


class TestEstimateTokens:
    def test_empty_text_costs_nothing(self) -> None:
        assert estimate_tokens(None) == 0
        assert estimate_tokens("") == 0

    def test_cjk_counts_one_token_per_character(self) -> None:
        assert estimate_tokens("你好世界") == 4

    def test_latin_counts_four_characters_per_token(self) -> None:
        assert estimate_tokens("abcdefgh") == 2

    def test_latin_rounds_up(self) -> None:
        assert estimate_tokens("abcde") == 2

    def test_mixed_text_adds_both(self) -> None:
        assert estimate_tokens("你好abcd") == 3


class TestBoundaries:
    def test_no_messages_yields_no_segments(self) -> None:
        assert segment([]) == []

    def test_a_single_run_is_one_segment(self) -> None:
        result = segment([message(1), message(2, minutes=5)])
        assert len(result) == 1
        assert result[0].boundary_reason is BoundaryReason.END_OF_INPUT
        assert result[0].message_count == 2

    def test_a_long_gap_splits_the_conversation(self) -> None:
        result = segment([message(1), message(2, minutes=5), message(3, minutes=125)])
        assert len(result) == 2
        assert result[0].boundary_reason is BoundaryReason.INACTIVITY
        assert [m.id for m in result[1].messages] == [message(3).id]

    def test_a_gap_exactly_at_the_threshold_does_not_split(self) -> None:
        """The rule is "longer than", so an hour apart is still one sitting."""
        result = segment([message(1), message(2, minutes=60)])
        assert len(result) == 1

    def test_max_messages_splits(self) -> None:
        result = segment([message(i, minutes=i) for i in range(5)], max_messages=2)
        assert [s.message_count for s in result] == [2, 2, 1]
        assert result[0].boundary_reason is BoundaryReason.MAX_MESSAGES

    def test_max_tokens_splits(self) -> None:
        filler = "x" * 400  # 100 tokens each
        result = segment([message(i, minutes=i, text=filler) for i in range(5)], max_tokens=250)
        assert [s.message_count for s in result] == [2, 2, 1]
        assert result[0].boundary_reason is BoundaryReason.MAX_TOKENS

    def test_a_message_larger_than_the_token_cap_stands_alone(self) -> None:
        """Splitting a message would need an invented continuation marker."""
        huge = "x" * 4000
        result = segment(
            [message(1, text=huge), message(2, minutes=1)],
            max_tokens=100,
        )
        assert [s.message_count for s in result] == [1, 1]

    def test_inactivity_wins_over_the_size_caps(self) -> None:
        """Boundary precedence is asserted, not assumed."""
        result = segment(
            [message(1), message(2, minutes=500)],
            max_messages=1,
            max_tokens=1,
        )
        assert result[0].boundary_reason is BoundaryReason.INACTIVITY

    def test_media_only_messages_do_not_consume_the_token_budget(self) -> None:
        result = segment(
            [
                message(1, text=None, message_type=MessageType.IMAGE),
                message(2, minutes=1, text="ok"),
            ],
            max_tokens=1,
        )
        assert len(result) == 1


class TestIdentity:
    def test_segments_are_deterministic(self) -> None:
        messages = [message(1), message(2, minutes=5), message(3, minutes=200)]
        assert [s.id for s in segment(messages)] == [s.id for s in segment(messages)]

    def test_input_order_does_not_change_the_result(self) -> None:
        messages = [message(1), message(2, minutes=5), message(3, minutes=200)]
        assert [s.id for s in segment(list(reversed(messages)))] == [
            s.id for s in segment(messages)
        ]

    def test_appending_later_messages_leaves_earlier_segments_untouched(self) -> None:
        """The whole point of hashing the boundary messages (§27)."""
        first_run = segment([message(1), message(2, minutes=5)])
        second_run = segment([message(1), message(2, minutes=5), message(3, minutes=200)])
        assert second_run[0].id == first_run[0].id

    def test_bumping_the_version_changes_every_id(self) -> None:
        """Re-tuning must be an explicit re-index, never a silent one."""
        messages = [message(1), message(2, minutes=5)]
        assert segment(messages, version=1)[0].id != segment(messages, version=2)[0].id

    def test_segment_id_is_shared_with_the_document(self) -> None:
        from knowmemo.domain.ids import make_document_id

        assert make_document_id("seg_abc") == "seg_abc"


class TestParticipants:
    def test_speakers_are_collected_in_order(self) -> None:
        result = segment(
            [
                message(1, sender="wxid_me", sender_name="Me"),
                message(2, minutes=1),
            ]
        )
        assert result[0].participants == ["Me", "Alice"]

    def test_a_speaker_appears_once(self) -> None:
        result = segment([message(1), message(2, minutes=1), message(3, minutes=2)])
        assert result[0].participants == ["Alice"]

    def test_pinned_participants_come_first_and_survive_a_silent_segment(self) -> None:
        """A 1:1 segment where only one side spoke is still about both people."""
        result = segment(
            [message(1, sender="wxid_me", sender_name="Me")],
            pinned_participants=["Me", "Alice"],
        )
        assert result[0].participants == ["Me", "Alice"]

    def test_a_missing_display_name_falls_back_to_the_id(self) -> None:
        result = segment([message(1, sender_name=None)])
        assert result[0].participants == ["wxid_alice"]


class TestEmptySegments:
    def test_a_segment_of_media_only_is_flagged(self) -> None:
        result = segment([message(1, text=None, message_type=MessageType.IMAGE)])
        assert result[0].is_empty

    def test_a_segment_with_any_text_is_not_flagged(self) -> None:
        result = segment(
            [
                message(1, text=None, message_type=MessageType.IMAGE),
                message(2, minutes=1, text="hi"),
            ]
        )
        assert not result[0].is_empty
