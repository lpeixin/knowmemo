"""End-to-end WeChat connector behaviour (§11, §12, §46 steps 5–7).

Everything here runs against generated synthetic snapshots. That is not a
convenience: reading a real store needs Full Disk Access, and producing a
plaintext snapshot is an out-of-band step (``docs/plan.md`` F8, D4). Fixtures
carry the entire verification burden for this connector, so they are varied
deliberately — layouts, sharding and column naming all get exercised.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from knowmemo.connectors.wechat.extractor import WeChatConnector
from knowmemo.connectors.wechat.parser import SENDER_PARTNER, SENDER_SELF, SENDER_UNRESOLVED
from knowmemo.domain.message import MessageType
from knowmemo.domain.source import SourceStatus
from tests.fixtures.wechat.builder import (
    ALICE,
    BOB,
    T_BASE,
    TEAM,
    MessageSpec,
    SnapshotBuilder,
    build_alice_snapshot,
    build_mixed_snapshot,
    message_table_name,
)


def connector_for(snapshot: Path, **kwargs: object) -> WeChatConnector:
    return WeChatConnector(snapshot_path=snapshot, account_id="wxid_me", **kwargs)


class TestDiscover:
    def test_unconfigured_source_is_reported_not_raised(self) -> None:
        source = WeChatConnector().discover()
        assert source.status is SourceStatus.NOT_CONFIGURED
        assert source.path is None

    def test_missing_snapshot_is_reported(self, tmp_path: Path) -> None:
        source = WeChatConnector(snapshot_path=tmp_path / "absent").discover()
        assert source.status is SourceStatus.NOT_FOUND

    def test_unreadable_snapshot_is_reported(self, tmp_path: Path) -> None:
        """A path that is not a snapshot must not raise out of discover()."""
        target = tmp_path / "not-a-snapshot"
        target.mkdir()
        source = WeChatConnector(snapshot_path=target).discover()
        assert source.status is SourceStatus.UNSUPPORTED
        assert source.status_detail

    def test_available_snapshot_reports_counts(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        source = WeChatConnector(snapshot_path=tmp_path / "snap").discover()
        assert source.status is SourceStatus.AVAILABLE
        assert "3 conversation tables" in (source.status_detail or "")
        assert source.metadata["databases"] == 3

    def test_the_live_store_is_recorded_but_never_read(self, tmp_path: Path) -> None:
        snapshot = build_alice_snapshot(tmp_path / "snap")
        live = tmp_path / "live-wechat"
        live.mkdir()
        source = connector_for(snapshot, data_path=live).discover()
        assert source.status is SourceStatus.AVAILABLE
        assert str(snapshot) in (source.path or "")


class TestHealthCheck:
    def test_reports_the_live_store_as_deliberately_unread(self, tmp_path: Path) -> None:
        snapshot = build_alice_snapshot(tmp_path / "snap")
        live = tmp_path / "live"
        live.mkdir()
        checks = connector_for(snapshot, data_path=live).health_check()
        live_check = next(c for c in checks if "live store" in c.name)
        assert live_check.ok
        assert "never reads" in (live_check.remedy or "")

    def test_unconfigured_snapshot_has_an_actionable_remedy(self) -> None:
        checks = WeChatConnector().health_check()
        assert not checks[0].ok
        assert "snapshot_path" in (checks[0].remedy or "")

    def test_healthy_snapshot_passes(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        checks = connector_for(tmp_path / "snap").health_check()
        assert all(check.ok for check in checks), [c.detail for c in checks]

    def test_unresolved_tables_are_flagged(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap", with_contact_db=False)
        checks = connector_for(tmp_path / "snap").health_check()
        resolution = next(c for c in checks if c.name == "Contact resolution")
        assert not resolution.ok
        assert resolution.remedy

    def test_a_missing_account_id_is_flagged(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        checks = WeChatConnector(snapshot_path=tmp_path / "snap").health_check()
        account = next(c for c in checks if c.name == "Account identity")
        assert not account.ok


class TestScan:
    def test_reports_conversation_and_message_counts(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        result = connector_for(tmp_path / "snap").scan()
        assert result.conversations == 3
        assert result.messages == 10
        assert result.details["contacts"] == 3

    def test_reports_unmapped_message_types_instead_of_hiding_them(self, tmp_path: Path) -> None:
        """An unmapped localType is a connector gap and must be visible."""
        builder = SnapshotBuilder(tmp_path / "snap")
        builder.add_contact(ALICE, nick_name="Alice")
        builder.add_text(ALICE, 1, T_BASE, "hello")
        builder.add_message(MessageSpec(ALICE, 2, T_BASE + 60, "mystery", local_type=99999))
        builder.build()
        result = connector_for(tmp_path / "snap").scan()
        assert result.details["unsupported_types"] == {99999: 1}

    def test_lists_message_databases(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", shards=2)
        result = connector_for(tmp_path / "snap").scan()
        assert sorted(result.details["message_databases"]) == ["message_0.db", "message_1.db"]

    def test_no_compression_column_variant_scans(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", schema_variant="no_compression_column")
        assert connector_for(tmp_path / "snap").scan().messages == 10

    def test_snake_case_variant_scans(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", schema_variant="snake_case")
        assert connector_for(tmp_path / "snap").scan().messages == 10

    def test_flat_layout_scans(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", layout="flat")
        assert connector_for(tmp_path / "snap").scan().messages == 10


class TestExtract:
    def test_yields_one_conversation_per_table(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        extracted = list(connector_for(tmp_path / "snap").extract())
        assert len(extracted) == 3
        assert {e.conversation.source_id for e in extracted} == {ALICE, BOB, TEAM}

    def test_messages_are_normalised_into_the_unified_model(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        assert extracted.conversation.message_count == 5
        assert len(extracted.messages) == 5
        first = extracted.messages[0]
        assert first.source_type == "wechat"
        assert first.content == "最近在学习 AI Agent。"
        assert first.sender_id == ALICE
        assert first.message_type is MessageType.TEXT

    def test_message_ids_are_deterministic_composites(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        first_run = [m.id for m in next(iter(connector_for(tmp_path / "snap").extract())).messages]
        second_run = [m.id for m in next(iter(connector_for(tmp_path / "snap").extract())).messages]
        assert first_run == second_run
        assert first_run[0] == f"wechat:wxid_me:wechat:wxid_me:{ALICE}:1"

    def test_timestamps_come_from_create_time(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        assert extracted.conversation.start_time == datetime.fromtimestamp(T_BASE)
        assert extracted.conversation.end_time == datetime.fromtimestamp(T_BASE + 7200 + 60)

    def test_outgoing_messages_are_attributed_to_the_account(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        roles = {p.id: p.role for p in extracted.conversation.participants}
        assert str(roles["wxid_me"]) == "self"
        assert str(roles[ALICE]) == "other"

    def test_incoming_message_sender_is_the_partner(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        incoming = [m for m in extracted.messages if m.sender_id == ALICE]
        outgoing = [m for m in extracted.messages if m.sender_id == "wxid_me"]
        assert len(incoming) == 3
        assert len(outgoing) == 2
        assert incoming[0].metadata["sender_resolution"] == SENDER_PARTNER
        assert outgoing[0].metadata["sender_resolution"] == SENDER_SELF

    def test_direct_conversation_type(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        extracted = {
            e.conversation.source_id: e.conversation
            for e in connector_for(tmp_path / "snap").extract()
        }
        assert str(extracted[ALICE].conversation_type) == "direct"
        assert str(extracted[TEAM].conversation_type) == "group"

    def test_group_messages_from_others_are_not_attributed(self, tmp_path: Path) -> None:
        """packed_info_data is not decoded yet — say so rather than guess."""
        build_mixed_snapshot(tmp_path / "snap")
        extracted = {
            e.conversation.source_id: e for e in connector_for(tmp_path / "snap").extract()
        }
        group = extracted[TEAM]
        assert group.conversation.metadata["unresolved_senders"] >= 1
        unresolved = [
            m for m in group.messages if m.metadata["sender_resolution"] == SENDER_UNRESOLVED
        ]
        assert unresolved
        # The conversation is kept as context; no plausible-looking name is invented.
        assert unresolved[0].sender_name is None

    def test_media_messages_carry_no_indexable_text(self, tmp_path: Path) -> None:
        """Placeholders would pollute semantic search with unmatchable tokens."""
        build_mixed_snapshot(tmp_path / "snap")
        extracted = {
            e.conversation.source_id: e for e in connector_for(tmp_path / "snap").extract()
        }
        images = [m for m in extracted[ALICE].messages if m.message_type is MessageType.IMAGE]
        assert images
        assert images[0].content is None
        assert images[0].is_empty

    def test_links_and_files_use_their_title_or_name(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        extracted = {
            e.conversation.source_id: e for e in connector_for(tmp_path / "snap").extract()
        }
        by_type = {m.message_type: m for m in extracted[ALICE].messages}
        assert by_type[MessageType.LINK].content == "MCP 规范"
        assert by_type[MessageType.LINK].metadata["url"] == "https://example.com/mcp"
        assert by_type[MessageType.FILE].content == "设计文档.pdf"
        assert by_type[MessageType.FILE].metadata["file_size"] == 204800

    def test_system_messages_are_typed_as_system(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        extracted = {
            e.conversation.source_id: e for e in connector_for(tmp_path / "snap").extract()
        }
        system = [m for m in extracted[TEAM].messages if m.message_type is MessageType.SYSTEM]
        assert system

    def test_a_quote_records_its_reply_target(self, tmp_path: Path) -> None:
        builder = SnapshotBuilder(tmp_path / "snap")
        builder.add_contact(ALICE, nick_name="Alice")
        builder.add_text(ALICE, 1, T_BASE, "原始消息")
        builder.add_message(
            MessageSpec(
                ALICE,
                2,
                T_BASE + 60,
                "<msg><appmsg><title>回复内容</title><type>57</type>"
                "<refermsg><svrid>1</svrid><content>原始消息</content></refermsg>"
                "</appmsg></msg>",
                local_type=49,
                is_send=1,
            )
        )
        builder.build()
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        reply = [m for m in extracted.messages if m.reply_to_id][0]
        assert reply.reply_to_id == extracted.messages[0].id

    def test_zstd_messages_are_decompressed(self, tmp_path: Path) -> None:
        pytest.importorskip("zstandard")
        builder = SnapshotBuilder(tmp_path / "snap")
        builder.add_contact(ALICE, nick_name="Alice")
        builder.add_text(ALICE, 1, T_BASE, "这条是压缩存储的。", encoding="zstd")
        builder.build()
        connector = connector_for(tmp_path / "snap")
        extracted = next(iter(connector.extract()))
        assert extracted.messages[0].content == "这条是压缩存储的。"
        assert connector.failures == []

    def test_unresolved_tables_are_skipped_not_guessed(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap", with_contact_db=False)
        assert list(connector_for(tmp_path / "snap").extract()) == []

    def test_a_conversation_with_no_usable_messages_is_not_emitted(self, tmp_path: Path) -> None:
        """An empty conversation carries no knowledge and would only add noise."""
        import sqlite3

        builder = SnapshotBuilder(tmp_path / "snap")
        builder.add_contact(ALICE, nick_name="Alice")
        builder.add_text(ALICE, 1, T_BASE, "placeholder")
        snapshot = builder.build()
        connection = sqlite3.connect(snapshot / "message" / "message_0.db")
        connection.execute(f'DELETE FROM "{message_table_name(ALICE)}"')
        connection.commit()
        connection.close()

        assert list(connector_for(snapshot).extract()) == []

    def test_a_broken_row_is_recorded_and_the_run_continues(self, tmp_path: Path) -> None:
        """§35: one malformed message must not terminate an import."""
        import sqlite3

        snapshot = build_alice_snapshot(tmp_path / "snap")
        connection = sqlite3.connect(snapshot / "message" / "message_0.db")
        connection.execute(
            f'INSERT INTO "{message_table_name(ALICE)}" '
            "(localId, localType, createTime, isSend, message_content) "
            "VALUES (99, 1, NULL, 0, ?)",
            (b"no timestamp",),
        )
        connection.commit()
        connection.close()

        connector = connector_for(snapshot)
        extracted = list(connector.extract())
        assert len(extracted) == 1
        assert extracted[0].conversation.message_count == 5
        assert len(connector.failures) == 1
        assert connector.failures[0].local_id == 99
        assert connector.failures[0].code == "MESSAGE_PARSE_ERROR"

    def test_sharded_snapshots_are_merged_across_databases(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", shards=3)
        extracted = list(connector_for(tmp_path / "snap").extract())
        assert len(extracted) == 3
        assert sum(e.conversation.message_count for e in extracted) == 10

    def test_every_schema_variant_extracts_identically(self, tmp_path: Path) -> None:
        contents: dict[str, list[str | None]] = {}
        for variant in ("standard", "no_compression_column", "snake_case"):
            snapshot = tmp_path / variant
            build_alice_snapshot(snapshot, schema_variant=variant)
            extracted = next(iter(connector_for(snapshot).extract()))
            contents[variant] = [m.content for m in extracted.messages]
        assert contents["standard"] == contents["no_compression_column"] == contents["snake_case"]

    def test_messages_are_ordered_by_timestamp(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        extracted = next(iter(connector_for(tmp_path / "snap").extract()))
        stamps = [m.timestamp for m in extracted.messages]
        assert stamps == sorted(stamps)
