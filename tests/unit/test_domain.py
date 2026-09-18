"""Domain model behaviour (design doc §8, §9, §16)."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from knowmemo.domain.conversation import Conversation, ConversationType
from knowmemo.domain.document import KnowledgeDocument
from knowmemo.domain.message import Message, MessageType
from knowmemo.domain.participant import Participant, ParticipantRole


def make_message(**overrides: object) -> Message:
    payload = {
        "id": "wechat:acct:conv:msg1",
        "source_type": "wechat",
        "conversation_id": "wechat:acct:conv",
        "sender_id": "wxid_alice",
        "timestamp": datetime(2026, 1, 15, 10, 21),
        "message_type": MessageType.TEXT,
        "content": "最近在学习 AI Agent。",
    }
    payload.update(overrides)
    return Message(**payload)  # type: ignore[arg-type]


class TestMessage:
    def test_defaults_are_source_agnostic(self) -> None:
        message = make_message()
        assert message.source_account_id is None
        assert message.reply_to_id is None
        assert message.attachment_ids == []
        assert message.metadata == {}

    def test_accepts_a_naive_timestamp(self) -> None:
        """WeChat stores a bare Unix timestamp; no zone is invented."""
        assert make_message().timestamp.tzinfo is None

    def test_empty_content_is_flagged(self) -> None:
        assert make_message(content="   ").is_empty
        assert make_message(content=None).is_empty
        assert not make_message().is_empty

    def test_supports_message_types_beyond_the_mvp_subset(self) -> None:
        for message_type in (MessageType.VOICE, MessageType.STICKER, MessageType.LOCATION):
            message = make_message(message_type=message_type, content=None)
            assert message.message_type is message_type


class TestConversation:
    def test_participant_names_fall_back_to_ids(self) -> None:
        conversation = Conversation(
            id="wechat:acct:conv",
            source_type="wechat",
            source_id="conv",
            title="Alice",
            conversation_type=ConversationType.DIRECT,
            participants=[
                Participant(id="wxid_me", source_type="wechat", role=ParticipantRole.SELF),
                Participant(
                    id="wxid_alice",
                    source_type="wechat",
                    display_name="Alice",
                    role=ParticipantRole.OTHER,
                ),
            ],
            start_time=datetime(2026, 1, 15, 10, 20),
            end_time=datetime(2026, 1, 15, 10, 42),
            message_count=18,
        )
        assert conversation.participant_names() == ["wxid_me", "Alice"]
        assert conversation.duration_seconds == 22 * 60

    def test_rejects_an_inverted_time_range(self) -> None:
        with pytest.raises(ValidationError, match="precedes"):
            Conversation(
                id="c",
                source_type="wechat",
                source_id="c",
                title="t",
                start_time=datetime(2026, 1, 2),
                end_time=datetime(2026, 1, 1),
            )


def make_document() -> KnowledgeDocument:
    return KnowledgeDocument(
        id="seg_abc",
        segment_id="seg_abc",
        source_type="wechat",
        conversation_id="wechat:acct:conv",
        conversation_type=ConversationType.DIRECT,
        conversation_name="Alice",
        start_time=datetime(2026, 1, 15, 10, 20),
        end_time=datetime(2026, 1, 15, 10, 42),
        participants=["Me", "Alice"],
        message_count=18,
    )


class TestKnowledgeDocument:
    def test_frontmatter_carries_every_filterable_field(self) -> None:
        frontmatter = make_document().frontmatter()
        for key in (
            "knowmemo_version",
            "source_type",
            "conversation_id",
            "segment_id",
            "conversation_type",
            "conversation_name",
            "start_time",
            "end_time",
            "participants",
            "message_count",
        ):
            assert key in frontmatter

    def test_times_are_iso_strings(self) -> None:
        assert make_document().frontmatter()["start_time"] == "2026-01-15T10:20:00"

    def test_weknora_metadata_is_flat_strings(self) -> None:
        """WeKnora accepts only map[string]string (docs/plan.md F3)."""
        metadata = make_document().weknora_metadata()
        assert all(isinstance(value, str) for value in metadata.values())
        assert metadata["participants"] == "Me,Alice"
        assert metadata["participants_csv"] == "Me,Alice"

    def test_segmentation_version_is_recorded(self) -> None:
        assert make_document().frontmatter()["segmentation_version"] == 1
