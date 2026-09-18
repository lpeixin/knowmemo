"""Knowledge document construction (design doc §16).

One segment becomes one Markdown document with YAML frontmatter. The
frontmatter is the machine-readable half — it is what makes the document
filterable once it reaches WeKnora (``docs/plan.md`` finding F3). The body is
the human- and model-readable half.

Why the body repeats what the frontmatter already says
------------------------------------------------------
WeKnora chunks a document before embedding it. A chunk taken from the middle of
a long transcript carries no frontmatter with it, so a retrieved chunk would
otherwise arrive with no idea who was speaking, when, or in which
conversation. Every document therefore opens with a short context header that
repeats the essentials. This costs a few dozen tokens per document and is the
difference between a citation and a floating fragment.

Timestamps are rendered without a timezone on purpose. WeChat stores a bare
Unix timestamp and the connector refuses to invent a zone for it (see
``connectors/wechat/parser.py``); printing "10:00" without claiming a zone is
honest, printing "10:00+08:00" would not be.
"""

from __future__ import annotations

import yaml

from knowmemo.domain.conversation import Conversation, ConversationType
from knowmemo.domain.document import KnowledgeDocument
from knowmemo.domain.ids import make_document_id
from knowmemo.domain.message import Message, MessageType
from knowmemo.processing.segmentation import Segment

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Rendered in place of content for message kinds that have no text. Keeping
#: them in the transcript preserves timeline fidelity; the connector already
#: made sure they contribute nothing to semantic search.
MESSAGE_PLACEHOLDERS: dict[MessageType, str] = {
    MessageType.IMAGE: "[image]",
    MessageType.VOICE: "[voice message]",
    MessageType.VIDEO: "[video]",
    MessageType.FILE: "[file]",
    MessageType.LOCATION: "[location]",
    MessageType.CONTACT: "[contact card]",
    MessageType.STICKER: "[sticker]",
    MessageType.SYSTEM: "[system message]",
    MessageType.LINK: "[link]",
    MessageType.TEXT: "[empty message]",
    MessageType.UNKNOWN: "[unsupported message]",
}

CONVERSATION_TYPE_LABELS: dict[ConversationType, str] = {
    ConversationType.DIRECT: "direct message",
    ConversationType.GROUP: "group chat",
    ConversationType.UNKNOWN: "conversation",
}


def _clean(text: str | None) -> str:
    """Normalise line endings and trim trailing whitespace per line."""
    if not text:
        return ""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalised.split("\n")).strip()


def _placeholder(message: Message) -> str:
    base = MESSAGE_PLACEHOLDERS.get(message.message_type, "[unsupported message]")
    filename = message.metadata.get("filename")
    if filename:
        return f"{base} {filename}"
    url = message.metadata.get("url")
    if url:
        return f"{base} {url}"
    return base


def render_message(message: Message) -> str:
    """Render one message as a speaker header plus its content."""
    label = message.sender_name or message.sender_id
    header = f"**{message.timestamp.strftime(TIME_FORMAT)} · {label}**"
    content = _clean(message.content) or _placeholder(message)
    return f"{header}\n\n{content}"


def render_header(segment: Segment, conversation: Conversation) -> str:
    """The context block repeated at the top of every document."""
    label = CONVERSATION_TYPE_LABELS.get(conversation.conversation_type, "conversation")
    participants = ", ".join(segment.participants) or "unknown"
    return "\n".join(
        (
            f"# {conversation.title}",
            "",
            f"- Conversation: {conversation.title} ({label})",
            f"- Period: {segment.start_time.strftime(TIME_FORMAT)}"
            f" → {segment.end_time.strftime(TIME_FORMAT)} (local time, no zone recorded)",
            f"- Participants: {participants}",
            f"- Messages in this segment: {segment.message_count}"
            f" of {conversation.message_count} in the conversation",
            f"- Source: {conversation.source_type}",
        )
    )


def render_body(segment: Segment, conversation: Conversation) -> str:
    """Render the full Markdown body (everything below the frontmatter)."""
    parts = [render_header(segment, conversation), "", "## Transcript", ""]
    for index, message in enumerate(segment.messages):
        if index:
            parts.append("")
        parts.append(render_message(message))
    return "\n".join(parts).strip() + "\n"


def build_document(segment: Segment, conversation: Conversation) -> KnowledgeDocument:
    """Turn a segment into a :class:`KnowledgeDocument`."""
    return KnowledgeDocument(
        id=make_document_id(segment.id),
        segment_id=segment.id,
        source_type=conversation.source_type,
        conversation_id=conversation.id,
        conversation_type=conversation.conversation_type,
        conversation_name=conversation.title,
        start_time=segment.start_time,
        end_time=segment.end_time,
        participants=list(segment.participants),
        message_count=segment.message_count,
        segmentation_version=segment.segmentation_version,
        body=render_body(segment, conversation),
        metadata={
            "boundary_reason": str(segment.boundary_reason),
            "estimated_tokens": segment.estimated_tokens,
            "source_conversation_id": conversation.source_id,
        },
    )


def render_markdown(document: KnowledgeDocument) -> str:
    """Serialise a document to the on-disk ``.md`` form.

    The frontmatter is emitted by PyYAML rather than by hand so that a display
    name containing a colon, a quote or a newline cannot produce a file whose
    frontmatter is unparseable.
    """
    frontmatter = yaml.safe_dump(
        document.frontmatter(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{document.body.strip()}\n"
