"""Raw snapshot rows to source-agnostic domain objects.

The parser is where a WeChat-specific shape stops existing. Everything it
returns is a :class:`~knowmemo.domain.message.Message` or
:class:`~knowmemo.domain.conversation.Conversation`, and nothing downstream
knows or cares where they came from (§2.2, §11).

Two honesty rules are enforced here rather than documented and forgotten:

* **Sender attribution in group chats is not guessed.** WeChat stores the real
  sender of a group message inside ``packed_info_data``, a packed binary blob
  this milestone does not decode. Own messages and direct conversations are
  attributable; a group message from someone else is marked
  ``sender_resolution = "unresolved"`` and keeps the conversation as its
  sender context. Filling in a plausible-looking name would be worse than
  leaving it blank — see the design document's own warning in §24 about
  systems that "easily produce plausible but false memories".
* **A single bad row never ends a run.** Failures are collected with their
  error code (§35) and reported at the end.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from knowmemo.connectors.wechat.decoders import (
    AppMessage,
    ContentDecodingError,
    decode_payload,
    extract_text,
    map_message_type,
    parse_app_message,
)
from knowmemo.connectors.wechat.models import RawContact, RawSession
from knowmemo.connectors.wechat.schema import MessageColumns
from knowmemo.domain.conversation import Conversation, ConversationType
from knowmemo.domain.ids import make_conversation_id, make_message_id
from knowmemo.domain.message import Message
from knowmemo.domain.participant import Participant, ParticipantRole
from knowmemo.errors import ErrorCode

GROUP_SUFFIX = "@chatroom"
SELF_PLACEHOLDER = "self"

#: How confidently a message's sender could be determined.
SENDER_SELF = "self"
SENDER_PARTNER = "partner"
SENDER_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ParseFailure:
    """One row that could not be turned into a domain object."""

    table: str
    local_id: int | None
    code: ErrorCode
    detail: str


@dataclass
class ParseOutcome:
    """Messages parsed from one conversation table, plus what went wrong."""

    messages: list[Message] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)
    skipped_unattributable: int = 0


def conversation_type_of(username: str) -> ConversationType:
    return ConversationType.GROUP if username.endswith(GROUP_SUFFIX) else ConversationType.DIRECT


def display_name_for(username: str, contact: RawContact | None) -> str:
    """Best available human label for a conversation or participant."""
    if contact is not None and contact.display_name:
        return contact.display_name
    return username


def _to_datetime(create_time: int | None) -> datetime | None:
    """WeChat stores a bare Unix timestamp in seconds, with no timezone.

    A naive local datetime is returned deliberately: inventing a zone would be
    a fabrication, and the segment builder only ever sorts on this value.
    """
    if create_time is None:
        return None
    try:
        return datetime.fromtimestamp(create_time)
    except (OverflowError, OSError, ValueError):
        return None


def _sender_for(
    is_send: bool | None,
    username: str,
    contact: RawContact | None,
    account_id: str | None,
) -> tuple[str, str | None, str]:
    """Return ``(sender_id, sender_name, resolution)``."""
    if is_send is None:
        return username, display_name_for(username, contact), SENDER_UNRESOLVED
    if is_send:
        return account_id or SELF_PLACEHOLDER, "Me", SENDER_SELF

    if conversation_type_of(username) is ConversationType.GROUP:
        # The real sender lives in packed_info_data, which is not decoded yet.
        return username, None, SENDER_UNRESOLVED
    return username, display_name_for(username, contact), SENDER_PARTNER


def parse_message(
    row: object,
    *,
    username: str,
    contact: RawContact | None,
    columns: MessageColumns,
    source_account_id: str | None,
    account_id: str | None,
) -> Message:
    """Convert one ``Msg_<md5>`` row into a :class:`Message`."""
    local_id = row[columns.local_id]  # type: ignore[index]
    create_time = row[columns.create_time]  # type: ignore[index]

    timestamp = _to_datetime(int(create_time) if create_time is not None else None)
    if timestamp is None:
        raise ValueError("row has no usable timestamp")

    local_type = (
        int(row[columns.local_type])  # type: ignore[index]
        if columns.local_type and row[columns.local_type] is not None  # type: ignore[index]
        else None
    )
    is_send = (
        bool(row[columns.is_send])  # type: ignore[index]
        if columns.is_send and row[columns.is_send] is not None  # type: ignore[index]
        else None
    )

    raw_content = row[columns.content] if columns.content else None  # type: ignore[index]
    type_code = (
        row[columns.content_type_code]  # type: ignore[index]
        if columns.content_type_code
        else None
    )
    decoded = decode_payload(raw_content, type_code)

    app_message: AppMessage | None = None
    if local_type == 49 and decoded:
        app_message = parse_app_message(decoded)

    text = extract_text(local_type, decoded, app_message)

    sender_id, sender_name, resolution = _sender_for(is_send, username, contact, account_id)

    conversation_id = make_conversation_id("wechat", source_account_id, username)
    message_id = make_message_id("wechat", source_account_id, conversation_id, str(local_id))

    metadata: dict[str, object] = {
        "local_type": local_type,
        "sender_resolution": resolution,
    }
    if columns.server_id and row[columns.server_id] is not None:  # type: ignore[index]
        metadata["server_id"] = row[columns.server_id]  # type: ignore[index]
    if columns.status and row[columns.status] is not None:  # type: ignore[index]
        metadata["status"] = row[columns.status]  # type: ignore[index]
    if app_message is not None:
        metadata["app_type"] = app_message.app_type
        if app_message.url:
            metadata["url"] = app_message.url
        if app_message.filename:
            metadata["filename"] = app_message.filename
        if app_message.file_size is not None:
            metadata["file_size"] = app_message.file_size
        metadata.update(app_message.extras)

    reply_to_id: str | None = None
    if app_message is not None and app_message.refer_server_id:
        reply_to_id = make_message_id(
            "wechat", source_account_id, conversation_id, app_message.refer_server_id
        )

    return Message(
        id=message_id,
        source_type="wechat",
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        sender_name=sender_name,
        timestamp=timestamp,
        message_type=map_message_type(local_type, app_message),
        content=text,
        reply_to_id=reply_to_id,
        attachment_ids=[],
        metadata=metadata,
    )


def parse_table(
    rows: Iterable[object],
    *,
    table_name: str,
    username: str,
    contact: RawContact | None,
    columns: MessageColumns,
    source_account_id: str | None,
    account_id: str | None,
) -> ParseOutcome:
    """Parse every row of one message table, collecting failures."""
    outcome = ParseOutcome()
    for row in rows:
        local_id: int | None
        try:
            local_id = int(row[columns.local_id])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            local_id = None

        try:
            outcome.messages.append(
                parse_message(
                    row,
                    username=username,
                    contact=contact,
                    columns=columns,
                    source_account_id=source_account_id,
                    account_id=account_id,
                )
            )
        except ContentDecodingError as exc:
            outcome.failures.append(
                ParseFailure(table_name, local_id, ErrorCode.MESSAGE_PARSE_ERROR, str(exc))
            )
        except ValueError as exc:
            outcome.failures.append(
                ParseFailure(table_name, local_id, ErrorCode.MESSAGE_PARSE_ERROR, str(exc))
            )
        except Exception as exc:  # noqa: BLE001 - never let one row end a run
            outcome.failures.append(
                ParseFailure(
                    table_name,
                    local_id,
                    ErrorCode.MESSAGE_PARSE_ERROR,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return outcome


def build_conversation(
    *,
    username: str,
    contact: RawContact | None,
    session: RawSession | None,
    messages: list[Message],
    source_account_id: str | None,
    account_id: str | None,
) -> Conversation | None:
    """Assemble a :class:`Conversation` from parsed messages.

    Returns ``None`` when there are no usable messages — an empty conversation
    carries no knowledge and would only add noise to the segment index.
    """
    if not messages:
        return None

    ordered = sorted(messages, key=lambda message: message.timestamp)
    conversation_type = conversation_type_of(username)
    conversation_id = make_conversation_id("wechat", source_account_id, username)
    title = display_name_for(username, contact)

    participants: list[Participant] = []
    seen: set[str] = set()
    for message in ordered:
        if message.sender_id in seen:
            continue
        seen.add(message.sender_id)
        role = (
            ParticipantRole.SELF
            if message.metadata.get("sender_resolution") == SENDER_SELF
            else ParticipantRole.OTHER
        )
        participants.append(
            Participant(
                id=message.sender_id,
                source_type="wechat",
                display_name=message.sender_name,
                role=role,
            )
        )

    unresolved = sum(
        1 for message in ordered if message.metadata.get("sender_resolution") == SENDER_UNRESOLVED
    )

    return Conversation(
        id=conversation_id,
        source_type="wechat",
        source_id=username,
        title=title,
        conversation_type=conversation_type,
        participants=participants,
        start_time=ordered[0].timestamp,
        end_time=ordered[-1].timestamp,
        message_count=len(ordered),
        metadata={
            "unresolved_senders": unresolved,
            "account_id": account_id,
            "session_last_timestamp": session.last_timestamp if session else None,
        },
    )


def iter_table_rows(database: object, table: str) -> Iterator[object]:
    """Stream a message table ordered by creation time.

    Ordering by ``createTime`` rather than ``localId`` matters: message ids are
    per-device and can interleave after a history migration, while the timestamp
    is what segmentation and retrieval both rely on.
    """
    order = ["createTime"] if "createTime" in database.column_names(table) else None  # type: ignore[attr-defined]
    if order is None:
        for candidate in ("create_time", "timestamp", "sortSeq", "localId"):
            if candidate in database.column_names(table):  # type: ignore[attr-defined]
                order = [candidate]
                break
    yield from database.iter_rows(table, order_by=order)  # type: ignore[attr-defined]
