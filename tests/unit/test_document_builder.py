"""Knowledge document rendering (design doc §16, plus finding F3)."""

from __future__ import annotations

from datetime import datetime, timedelta

import yaml

from knowmemo.domain.conversation import Conversation, ConversationType
from knowmemo.domain.ids import make_message_id
from knowmemo.domain.message import Message, MessageType
from knowmemo.domain.participant import Participant, ParticipantRole
from knowmemo.knowledge.document_builder import build_document, render_markdown
from knowmemo.processing.segmentation import SegmentationParams, segment_messages

CONVERSATION = "wechat:-:wxid_alice"
BASE = datetime(2025, 1, 1, 9, 0, 0)
PARAMS = SegmentationParams()


def message(
    index: int,
    *,
    minutes: float = 0,
    text: str | None = "hello",
    sender: str = "wxid_alice",
    sender_name: str | None = "Alice",
    message_type: MessageType = MessageType.TEXT,
    metadata: dict | None = None,
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
        metadata=metadata or {},
    )


def conversation(*, title: str = "Alice", **overrides: object) -> Conversation:
    return Conversation(
        id=CONVERSATION,
        source_type="wechat",
        source_id="wxid_alice",
        title=title,
        conversation_type=overrides.pop("conversation_type", ConversationType.DIRECT),
        participants=[
            Participant(
                id="wxid_me",
                source_type="wechat",
                display_name="Me",
                role=ParticipantRole.SELF,
            ),
            Participant(
                id="wxid_alice",
                source_type="wechat",
                display_name="Alice",
                role=ParticipantRole.OTHER,
            ),
        ],
        start_time=BASE,
        end_time=BASE + timedelta(minutes=10),
        message_count=2,
    )


def build(messages: list[Message], **kwargs: object):
    conv = conversation(**kwargs)
    segments = segment_messages(
        messages,
        conversation_id=conv.id,
        params=PARAMS,
        pinned_participants=conv.participant_names(),
    )
    assert segments
    return build_document(segments[0], conv)


class TestFrontmatter:
    def test_frontmatter_is_valid_yaml(self) -> None:
        markdown = render_markdown(build([message(1)]))
        _, frontmatter, _ = markdown.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        assert parsed["conversation_id"] == CONVERSATION
        assert parsed["source_type"] == "wechat"

    def test_every_frontmatter_value_is_string_or_scalar(self) -> None:
        """F3: WeKnora accepts ``map[string]string``, so lists must survive."""
        document = build([message(1)])
        for value in document.frontmatter().values():
            assert isinstance(value, (str, int, list)), value

    def test_weknora_metadata_flattens_lists(self) -> None:
        flat = build([message(1)]).weknora_metadata()
        assert isinstance(flat["participants"], str)
        assert "Alice" in flat["participants"]
        assert "participants_csv" in flat

    def test_a_hostile_title_does_not_break_the_frontmatter(self) -> None:
        """The reason frontmatter goes through PyYAML instead of f-strings."""
        document = build([message(1)], title='Bob: "the sequel"\n# not a heading')
        parsed = yaml.safe_load(render_markdown(document).split("---", 2)[1])
        assert parsed["conversation_name"] == 'Bob: "the sequel"\n# not a heading'


class TestBody:
    def test_the_body_repeats_the_context(self) -> None:
        """A chunk taken mid-transcript carries no frontmatter with it."""
        document = build([message(1)])
        assert "Alice" in document.body
        assert "2025-01-01 09:00:00" in document.body
        assert "## Transcript" in document.body

    def test_each_message_gets_a_speaker_header(self) -> None:
        document = build([message(1), message(2, minutes=5)])
        assert "**2025-01-01 09:00:00 · Alice**" in document.body
        assert "**2025-01-01 09:05:00 · Alice**" in document.body

    def test_timestamps_carry_no_timezone_claim(self) -> None:
        """The source stores a bare Unix timestamp; a zone would be invented."""
        document = build([message(1)])
        assert "+00:00" not in document.body
        assert "no zone recorded" in document.body

    def test_media_renders_as_a_placeholder(self) -> None:
        document = build([message(1, text=None, message_type=MessageType.IMAGE)])
        assert "[image]" in document.body

    def test_a_file_message_shows_its_name(self) -> None:
        document = build(
            [
                message(
                    1,
                    text=None,
                    message_type=MessageType.FILE,
                    metadata={"filename": "report.pdf"},
                )
            ]
        )
        assert "[file] report.pdf" in document.body

    def test_a_link_without_a_title_shows_its_url(self) -> None:
        document = build(
            [
                message(
                    1,
                    text=None,
                    message_type=MessageType.LINK,
                    metadata={"url": "https://example.com/a"},
                )
            ]
        )
        assert "https://example.com/a" in document.body

    def test_content_keeps_its_internal_newlines(self) -> None:
        document = build([message(1, text="line one\nline two")])
        assert "line one\nline two" in document.body

    def test_carriage_returns_are_normalised(self) -> None:
        document = build([message(1, text="a\r\nb")])
        assert "a\r\nb" not in document.body
        assert "a\nb" in document.body


class TestMetadata:
    def test_the_document_records_why_the_segment_ended(self) -> None:
        document = build([message(1)])
        assert document.metadata["boundary_reason"] == "end_of_input"

    def test_document_id_is_the_segment_id(self) -> None:
        document = build([message(1)])
        assert document.id == document.segment_id

    def test_participants_are_pinned_for_a_direct_conversation(self) -> None:
        document = build([message(1, sender="wxid_me", sender_name="Me")])
        assert document.participants == ["Me", "Alice"]
