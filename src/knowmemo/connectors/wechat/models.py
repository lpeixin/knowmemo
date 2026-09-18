"""Raw row shapes as they appear in a WeChat snapshot.

These are deliberately *not* the domain models. A ``RawMessage`` mirrors the
columns of a ``Msg_<md5>`` table, including its quirks; the parser's job is to
turn it into a source-agnostic :class:`~knowmemo.domain.message.Message`.

Every field beyond the handful that are structurally required is optional.
WeChat's schema varies between versions and platforms, and a connector that
refuses to run because one column is absent is worse than one that reports what
it could not read. Missing data becomes ``None`` and is recorded, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RawContact:
    """A row of ``contact.db``'s contact table."""

    username: str
    nick_name: str | None = None
    remark: str | None = None
    alias: str | None = None
    local_type: int | None = None

    @property
    def display_name(self) -> str | None:
        """Prefer the user's own remark, then the contact's nickname."""
        return self.remark or self.nick_name or self.alias or None


@dataclass(frozen=True)
class RawSession:
    """A row of ``session.db``'s session table."""

    username: str
    last_timestamp: int | None = None
    unread_count: int | None = None


@dataclass(frozen=True)
class RawMessage:
    """A row of a ``Msg_<md5(username)>`` table.

    ``content_type_code`` mirrors ``WCDB_CT_message_content``: WCDB stores a
    per-value type indicator, and a value of ``4`` means the payload is zstd
    compressed. ``None`` means the column was absent, which is treated as
    "uncompressed" — see :mod:`knowmemo.connectors.wechat.decoders`.
    """

    local_id: int
    local_type: int | None
    create_time: int | None
    is_send: bool | None
    content: bytes | None
    content_type_code: int | None = None
    server_id: int | None = None
    sort_seq: int | None = None
    status: int | None = None
    des: int | None = None
    compressed_content: bytes | None = None
    compressed_type_code: int | None = None
    packed_info: bytes | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_system(self) -> bool:
        """``des = 10000`` marks system notices inside a conversation."""
        return self.des == 10000 or self.local_type == 10000


@dataclass(frozen=True)
class MessageTable:
    """A discovered ``Msg_<md5>`` table and the conversation it belongs to."""

    table_name: str
    database_path: str
    md5: str
    username: str | None
    row_count: int

    @property
    def is_resolved(self) -> bool:
        """Whether the md5 digest was matched to a known contact."""
        return self.username is not None
