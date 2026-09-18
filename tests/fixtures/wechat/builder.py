"""Build synthetic WeChat snapshots for tests (§33).

The specification asks for a golden dataset and says development must not
depend on real WeChat data. In this project that is not merely a preference:
reading a real store needs Full Disk Access, and producing a plaintext snapshot
is an out-of-band step the user performs themselves (``docs/plan.md`` F8, D4).
So fixtures are the *only* way the connector can be exercised, and they carry
the full testing burden.

They are generated rather than checked in as binary ``.db`` files. Generating
them means the schema can be varied per test — a snapshot with no compression
column, a snapshot using snake_case columns, a snapshot split across several
message databases — which is exactly the robustness the connector's discovery
layer is supposed to provide, and which static fixtures could not express.

Schema provenance
-----------------
The default schema below is a *hypothesis*, assembled from public
documentation of WeChat's WCDB layout. It has not been validated against a real
snapshot, because none is available here. Per the specification's Rule 2, the
connector must therefore be written to discover rather than assume — which it
is — and the fixture schema must be corrected against real data before the
connector is trusted. This caveat is recorded rather than hidden.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Layout = Literal["nested", "flat"]
SchemaVariant = Literal["standard", "no_compression_column", "snake_case"]
ContentEncoding = Literal["plain", "zstd"]

WCDB_TYPE_ZSTD = 4

CONTACT_SCHEMA = """
CREATE TABLE contact (
    username   TEXT PRIMARY KEY,
    alias      TEXT,
    nick_name  TEXT,
    remark     TEXT,
    local_type INTEGER
)
"""

SESSION_SCHEMA = """
CREATE TABLE Session (
    username       TEXT PRIMARY KEY,
    last_timestamp INTEGER,
    unread_count   INTEGER
)
"""

MESSAGE_SCHEMA_STANDARD = """
CREATE TABLE "{table}" (
    localId                 INTEGER PRIMARY KEY,
    serverId                INTEGER,
    localType               INTEGER,
    createTime              INTEGER,
    sortSeq                 INTEGER,
    isSend                  INTEGER,
    status                  INTEGER,
    des                     INTEGER,
    message_content         BLOB,
    WCDB_CT_message_content INTEGER,
    compress_content        BLOB,
    WCDB_CT_compress_content INTEGER,
    packed_info_data        BLOB
)
"""

MESSAGE_SCHEMA_NO_COMPRESSION_COLUMN = """
CREATE TABLE "{table}" (
    localId         INTEGER PRIMARY KEY,
    serverId        INTEGER,
    localType       INTEGER,
    createTime      INTEGER,
    isSend          INTEGER,
    status          INTEGER,
    des             INTEGER,
    message_content BLOB
)
"""

MESSAGE_SCHEMA_SNAKE_CASE = """
CREATE TABLE "{table}" (
    local_id         INTEGER PRIMARY KEY,
    server_id        INTEGER,
    local_type       INTEGER,
    create_time      INTEGER,
    sort_seq         INTEGER,
    is_send          INTEGER,
    status           INTEGER,
    message_content  BLOB
)
"""

MESSAGE_SCHEMAS: dict[str, str] = {
    "standard": MESSAGE_SCHEMA_STANDARD,
    "no_compression_column": MESSAGE_SCHEMA_NO_COMPRESSION_COLUMN,
    "snake_case": MESSAGE_SCHEMA_SNAKE_CASE,
}


def md5_of(username: str) -> str:
    """Reproduce WeChat's ``Msg_<md5(username)>`` table naming."""
    return hashlib.md5(username.encode("utf-8")).hexdigest()


def message_table_name(username: str) -> str:
    return f"Msg_{md5_of(username)}"


@dataclass
class ContactSpec:
    username: str
    nick_name: str | None = None
    remark: str | None = None
    alias: str | None = None
    local_type: int = 1


@dataclass
class MessageSpec:
    conversation: str
    local_id: int
    create_time: int
    content: str | None = None
    local_type: int = 1
    is_send: int = 0
    server_id: int | None = None
    status: int = 0
    des: int = 0
    encoding: ContentEncoding = "plain"
    packed_info: bytes | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _compress(payload: bytes) -> bytes:
    import zstandard  # noqa: PLC0415 - optional dependency

    return zstandard.ZstdCompressor().compress(payload)


class SnapshotBuilder:
    """Assemble a synthetic plaintext WeChat snapshot on disk."""

    def __init__(
        self,
        root: Path,
        *,
        layout: Layout = "nested",
        schema_variant: SchemaVariant = "standard",
        shards: int = 1,
        account_id: str | None = "wxid_me",
        with_contact_db: bool = True,
        with_session_db: bool = True,
    ) -> None:
        self.root = root
        self.layout = layout
        self.schema_variant = schema_variant
        self.shards = max(1, shards)
        self.account_id = account_id
        self.with_contact_db = with_contact_db
        self.with_session_db = with_session_db

        self.contacts: list[ContactSpec] = []
        self.sessions: dict[str, int] = {}
        self.messages: list[MessageSpec] = []

    # -- population --------------------------------------------------------

    def add_contact(self, username: str, **kwargs: Any) -> SnapshotBuilder:
        self.contacts.append(ContactSpec(username=username, **kwargs))
        return self

    def add_session(self, username: str, last_timestamp: int) -> SnapshotBuilder:
        self.sessions[username] = last_timestamp
        return self

    def add_message(self, spec: MessageSpec) -> SnapshotBuilder:
        self.messages.append(spec)
        return self

    def add_text(
        self,
        conversation: str,
        local_id: int,
        create_time: int,
        content: str,
        *,
        is_send: int = 0,
        **kwargs: Any,
    ) -> SnapshotBuilder:
        return self.add_message(
            MessageSpec(
                conversation=conversation,
                local_id=local_id,
                create_time=create_time,
                content=content,
                local_type=1,
                is_send=is_send,
                **kwargs,
            )
        )

    # -- output ------------------------------------------------------------

    def _directory(self, name: str) -> Path:
        directory = self.root / name if self.layout == "nested" else self.root
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def build(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.with_contact_db:
            self._write_contacts()
        if self.with_session_db:
            self._write_sessions()
        self._write_messages()
        return self.root

    def _write_contacts(self) -> None:
        path = self._directory("contact") / "contact.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(CONTACT_SCHEMA)
            connection.executemany(
                "INSERT INTO contact (username, alias, nick_name, remark, local_type) "
                "VALUES (?, ?, ?, ?, ?)",
                [(c.username, c.alias, c.nick_name, c.remark, c.local_type) for c in self.contacts],
            )
            connection.commit()
        finally:
            connection.close()

    def _write_sessions(self) -> None:
        path = self._directory("session") / "session.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(SESSION_SCHEMA)
            connection.executemany(
                "INSERT INTO Session (username, last_timestamp, unread_count) VALUES (?, ?, ?)",
                [(username, timestamp, 0) for username, timestamp in self.sessions.items()],
            )
            connection.commit()
        finally:
            connection.close()

    def _write_messages(self) -> None:
        grouped: dict[str, list[MessageSpec]] = {}
        for spec in self.messages:
            grouped.setdefault(spec.conversation, []).append(spec)

        # Round-robin conversations across shards, mirroring how WeChat splits
        # a large history over message_0.db, message_1.db, …
        shard_of: dict[str, int] = {
            conversation: index % self.shards for index, conversation in enumerate(sorted(grouped))
        }

        connections: dict[int, sqlite3.Connection] = {}
        try:
            for conversation, specs in grouped.items():
                shard = shard_of[conversation]
                connection = connections.get(shard)
                if connection is None:
                    directory = self._directory("message")
                    connection = sqlite3.connect(directory / f"message_{shard}.db")
                    connections[shard] = connection

                table = message_table_name(conversation)
                connection.execute(MESSAGE_SCHEMAS[self.schema_variant].format(table=table))
                for spec in sorted(specs, key=lambda s: (s.create_time, s.local_id)):
                    self._insert_message(connection, table, spec)
            for connection in connections.values():
                connection.commit()
        finally:
            for connection in connections.values():
                connection.close()

    def _insert_message(
        self, connection: sqlite3.Connection, table: str, spec: MessageSpec
    ) -> None:
        payload: bytes | None = None
        type_code: int | None = None
        if spec.content is not None:
            raw = spec.content.encode("utf-8")
            if spec.encoding == "zstd":
                payload = _compress(raw)
                type_code = WCDB_TYPE_ZSTD
            else:
                payload = raw

        quoted = '"' + table.replace('"', '""') + '"'

        if self.schema_variant == "snake_case":
            columns = [
                "local_id",
                "server_id",
                "local_type",
                "create_time",
                "sort_seq",
                "is_send",
                "status",
                "message_content",
            ]
            values: list[Any] = [
                spec.local_id,
                spec.server_id,
                spec.local_type,
                spec.create_time,
                spec.local_id,
                spec.is_send,
                spec.status,
                payload,
            ]
        elif self.schema_variant == "no_compression_column":
            columns = [
                "localId",
                "serverId",
                "localType",
                "createTime",
                "isSend",
                "status",
                "des",
                "message_content",
            ]
            values = [
                spec.local_id,
                spec.server_id,
                spec.local_type,
                spec.create_time,
                spec.is_send,
                spec.status,
                spec.des,
                payload,
            ]
        else:
            columns = [
                "localId",
                "serverId",
                "localType",
                "createTime",
                "sortSeq",
                "isSend",
                "status",
                "des",
                "message_content",
                "WCDB_CT_message_content",
                "packed_info_data",
            ]
            values = [
                spec.local_id,
                spec.server_id,
                spec.local_type,
                spec.create_time,
                spec.local_id,
                spec.is_send,
                spec.status,
                spec.des,
                payload,
                type_code,
                spec.packed_info,
            ]

        placeholders = ", ".join("?" for _ in columns)
        quoted_columns = ", ".join('"' + name + '"' for name in columns)
        connection.execute(
            f"INSERT INTO {quoted} ({quoted_columns}) VALUES ({placeholders})", values
        )


# -- canonical fixtures ----------------------------------------------------

ALICE = "wxid_alice"
BOB = "wxid_bob"
TEAM = "12345678@chatroom"

#: Fixed epoch anchors so every expected value in the tests is deterministic.
T_BASE = 1_735_689_600  # 2025-01-01T00:00:00Z
HOUR = 3600


def build_alice_snapshot(root: Path, **kwargs: Any) -> Path:
    """A direct conversation with Alice, spanning a gap and a topic change.

    The gap at ``T_BASE + 2h`` is what the segmentation tests split on.
    """
    builder = SnapshotBuilder(root, **kwargs)
    builder.add_contact(ALICE, nick_name="Alice", remark="Alice", alias="alice_wx")
    builder.add_session(ALICE, T_BASE + 2 * HOUR)
    builder.add_text(ALICE, 1, T_BASE, "最近在学习 AI Agent。")
    builder.add_text(ALICE, 2, T_BASE + 60, "主要在看 LangGraph。", is_send=1)
    builder.add_text(ALICE, 3, T_BASE + 120, "我最近也开始研究 MCP。")
    # Two hours later — beyond the default 60-minute inactivity threshold.
    builder.add_text(ALICE, 4, T_BASE + 2 * HOUR, "下周一起吃饭？")
    builder.add_text(ALICE, 5, T_BASE + 2 * HOUR + 60, "好。", is_send=1)
    return builder.build()


def build_mixed_snapshot(root: Path, **kwargs: Any) -> Path:
    """Three conversations plus a group, exercising most message types."""
    builder = SnapshotBuilder(root, **kwargs)
    builder.add_contact(ALICE, nick_name="Alice", remark="Alice")
    builder.add_contact(BOB, nick_name="Bob")
    builder.add_contact(TEAM, nick_name="项目组", local_type=3)
    builder.add_session(ALICE, T_BASE + 3 * HOUR)
    builder.add_session(BOB, T_BASE + 4 * HOUR)
    builder.add_session(TEAM, T_BASE + 5 * HOUR)

    builder.add_text(ALICE, 1, T_BASE, "最近在学习 AI Agent。")
    builder.add_text(ALICE, 2, T_BASE + 60, "主要在看 LangGraph。", is_send=1)
    builder.add_message(MessageSpec(ALICE, 3, T_BASE + 120, None, local_type=3))  # image
    builder.add_message(
        MessageSpec(
            ALICE,
            4,
            T_BASE + 180,
            "<msg><appmsg><title>MCP 规范</title><type>5</type>"
            "<url>https://example.com/mcp</url></appmsg></msg>",
            local_type=49,
        )
    )
    builder.add_message(
        MessageSpec(
            ALICE,
            5,
            T_BASE + 240,
            "<msg><appmsg><title>设计文档.pdf</title><type>6</type>"
            "<appattach><totallen>204800</totallen><fileext>pdf</fileext></appattach>"
            "</appmsg></msg>",
            local_type=49,
        )
    )

    builder.add_text(BOB, 1, T_BASE + 3600, "Kubernetes 集群升级完了。")
    builder.add_text(BOB, 2, T_BASE + 3660, "收到。", is_send=1)

    builder.add_text(TEAM, 1, T_BASE + 7200, "本周进度同步。")
    builder.add_text(TEAM, 2, T_BASE + 7260, "我这边完成了。", is_send=1)
    builder.add_message(MessageSpec(TEAM, 3, T_BASE + 7320, None, local_type=10000, des=10000))
    return builder.build()
