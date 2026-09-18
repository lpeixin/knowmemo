"""Content decoding for WeChat message rows.

Three things happen here, in order:

1. **Decompression.** WCDB stores a per-value type indicator. A value of ``4``
   in ``WCDB_CT_message_content`` means the payload is zstd compressed, so the
   same column holds raw text in one row and compressed bytes in the next.
   Ignoring this is the single most common way a WeChat reader silently loses
   messages.
2. **Type mapping.** ``localType`` is a WeChat-internal integer. It is mapped
   to :class:`~knowmemo.domain.message.MessageType` and nothing else in the
   codebase is allowed to know the integers.
3. **Payload parsing.** App messages (``localType = 49``) carry an XML document
   whose ``<type>`` distinguishes a link from a file from a quote from a group
   announcement.

Nothing here raises on malformed input. A message whose XML is unparseable
still has a timestamp, a sender and a type; losing that would break the
timeline for the sake of one row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree

from knowmemo.domain.message import MessageType

#: ``WCDB_CT_message_content = 4`` marks a zstd-compressed payload.
WCDB_TYPE_ZSTD = 4

#: ``localType`` -> :class:`MessageType`. Values outside this map become
#: ``UNKNOWN`` and are counted in the scan report rather than dropped.
LOCAL_TYPE_MAP: dict[int, MessageType] = {
    1: MessageType.TEXT,
    3: MessageType.IMAGE,
    34: MessageType.VOICE,
    37: MessageType.SYSTEM,  # friend request
    42: MessageType.CONTACT,
    43: MessageType.VIDEO,
    47: MessageType.STICKER,
    48: MessageType.LOCATION,
    49: MessageType.UNKNOWN,  # app message — refined by parse_app_message
    50: MessageType.SYSTEM,  # VoIP call record
    10000: MessageType.SYSTEM,
    10002: MessageType.SYSTEM,
}

#: ``<appmsg><type>`` -> :class:`MessageType` for ``localType = 49``.
APP_TYPE_MAP: dict[int, MessageType] = {
    5: MessageType.LINK,
    6: MessageType.FILE,
    8: MessageType.STICKER,
    17: MessageType.LOCATION,
    19: MessageType.FILE,  # merged chat history
    24: MessageType.FILE,  # note
    33: MessageType.LINK,  # mini program
    36: MessageType.LINK,  # mini program
    51: MessageType.SYSTEM,  # video call
    52: MessageType.SYSTEM,  # voice call
    53: MessageType.SYSTEM,  # video call
    57: MessageType.TEXT,  # quote / reply
    62: MessageType.LINK,  # channels
    87: MessageType.SYSTEM,  # group announcement
    2000: MessageType.SYSTEM,  # transfer
    2001: MessageType.SYSTEM,  # red packet
}

SYSTEM_MARKER = "[系统消息]"


class ContentDecodingError(Exception):
    """Raised when a payload cannot be decoded at all.

    The parser catches this, records ``MESSAGE_PARSE_ERROR`` for the row and
    moves on (§35). One bad message must not end an import.
    """


def _require_zstandard():
    try:
        import zstandard  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ContentDecodingError(
            "this snapshot contains zstd-compressed messages; "
            "install the optional dependency with: pip install 'knowmemo[wechat]'"
        ) from exc
    return zstandard


def decompress_zstd(payload: bytes) -> bytes:
    """Decompress a zstd frame, tolerating a payload without a frame header.

    Some rows carry a bare deflate block rather than a complete zstd frame;
    the streaming decompressor handles both.
    """
    zstandard = _require_zstandard()
    try:
        return zstandard.ZstdDecompressor().decompressobj().decompress(payload)
    except Exception as exc:  # noqa: BLE001 - zstandard raises several types
        try:
            return zstandard.ZstdDecompressor().stream_reader(payload).read()
        except Exception:  # noqa: BLE001
            raise ContentDecodingError(f"zstd decompression failed: {exc}") from exc


def decode_payload(payload: bytes | str | None, type_code: int | None) -> str | None:
    """Turn a stored payload into text.

    ``type_code`` is the WCDB value-type indicator for the column. ``None``
    (column absent) is treated as uncompressed, which is the safe default:
    a snapshot without the column is one where nothing was compressed.
    """
    if payload is None:
        return None

    if isinstance(payload, str):
        return payload or None

    data = bytes(payload)
    if not data:
        return None

    if type_code == WCDB_TYPE_ZSTD:
        data = decompress_zstd(data)

    return data.decode("utf-8", errors="replace") or None


@dataclass
class AppMessage:
    """The parts of an ``<appmsg>`` document KnowMemo cares about."""

    app_type: int | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    filename: str | None = None
    file_size: int | None = None
    refer_content: str | None = None
    refer_server_id: str | None = None
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def best_text(self) -> str | None:
        """The most informative single string, for the searchable content field."""
        for candidate in (self.title, self.filename, self.description, self.refer_content):
            if candidate and candidate.strip():
                return candidate.strip()
        return None


def _text_of(node: ElementTree.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _int_of(node: ElementTree.Element | None) -> int | None:
    value = _text_of(node)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_app_message(raw: str) -> AppMessage:
    """Parse an app-message XML payload.

    Returns an empty :class:`AppMessage` rather than raising when the document
    is malformed — the caller still needs the row's timestamp and sender.
    """
    text = raw.strip()
    if not text.startswith("<"):
        # Some app messages store plain text with no XML envelope.
        return AppMessage(description=text or None)

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return AppMessage(description=text or None)

    appmsg = root.find("appmsg")
    if appmsg is None:
        appmsg = root if root.tag == "appmsg" else None
    if appmsg is None:
        return AppMessage(description=text or None)

    attach = appmsg.find("appattach")
    refer = appmsg.find("refermsg")

    message = AppMessage(
        app_type=_int_of(appmsg.find("type")),
        title=_text_of(appmsg.find("title")),
        description=_text_of(appmsg.find("des")),
        url=_text_of(appmsg.find("url")),
        filename=_text_of(attach.find("filename")) if attach is not None else None,
        file_size=_int_of(attach.find("totallen")) if attach is not None else None,
        refer_content=_text_of(refer.find("content")) if refer is not None else None,
        refer_server_id=_text_of(refer.find("svrid")) if refer is not None else None,
    )

    if message.filename is None and attach is not None:
        file_ext = _text_of(attach.find("fileext"))
        if file_ext:
            message.extras["fileext"] = file_ext

    return message


def map_message_type(local_type: int | None, app_message: AppMessage | None = None) -> MessageType:
    """Resolve a ``localType`` to a :class:`MessageType`.

    App messages are refined by their inner ``<type>``; anything unrecognised
    becomes ``UNKNOWN`` rather than being guessed at.
    """
    if local_type is None:
        return MessageType.UNKNOWN

    resolved = LOCAL_TYPE_MAP.get(local_type, MessageType.UNKNOWN)
    if local_type == 49 and app_message is not None and app_message.app_type is not None:
        return APP_TYPE_MAP.get(app_message.app_type, MessageType.UNKNOWN)
    return resolved


def extract_text(
    local_type: int | None,
    decoded: str | None,
    app_message: AppMessage | None,
) -> str | None:
    """Pick the searchable text for a message.

    Returns ``None`` for pure-media messages. That is deliberate: a transcript
    full of ``[图片]`` placeholders would pollute semantic search with tokens
    that match nothing. The document builder renders placeholders for display
    from ``message_type`` instead, so the timeline stays faithful without the
    index going stale.
    """
    message_type = map_message_type(local_type, app_message)

    if message_type in {
        MessageType.IMAGE,
        MessageType.VOICE,
        MessageType.VIDEO,
        MessageType.STICKER,
        MessageType.LOCATION,
        MessageType.CONTACT,
    }:
        if message_type is MessageType.LOCATION and app_message is not None:
            return app_message.title or app_message.description
        return None

    if app_message is not None:
        text = app_message.best_text
        if text:
            return text

    if decoded is None:
        return None

    if message_type is MessageType.SYSTEM:
        # System rows sometimes carry a JSON or XML blob instead of prose.
        return decoded if decoded.startswith("<") or decoded.startswith("{") else decoded

    return decoded
