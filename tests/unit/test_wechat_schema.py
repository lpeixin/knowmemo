"""Snapshot discovery and schema introspection (design doc §13, rewritten).

The specification proposed version adapters. These tests pin the behaviour that
replaced them: discovery over sharding, dynamic ``Msg_<md5>`` tables, and
column-name variation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knowmemo.connectors.wechat.database import ReadOnlyDatabase
from knowmemo.connectors.wechat.schema import (
    MessageColumns,
    introspect,
    locate_snapshot,
)
from knowmemo.errors import (
    DatabaseInvalidError,
    SchemaUnsupportedError,
    SourceNotFoundError,
    SourceUnreadableError,
)
from tests.fixtures.wechat.builder import (
    ALICE,
    BOB,
    T_BASE,
    build_alice_snapshot,
    build_mixed_snapshot,
    md5_of,
    message_table_name,
)


class TestLocateSnapshot:
    def test_finds_a_nested_layout(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        layout = locate_snapshot(tmp_path / "snap")
        assert layout.contact_db is not None
        assert layout.contact_db.name == "contact.db"
        assert layout.session_db is not None
        assert len(layout.message_dbs) == 1

    def test_finds_a_flat_layout(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap", layout="flat")
        layout = locate_snapshot(tmp_path / "snap")
        assert layout.contact_db is not None
        assert layout.session_db is not None
        assert len(layout.message_dbs) == 1

    def test_finds_every_shard(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap", shards=3)
        layout = locate_snapshot(tmp_path / "snap")
        assert len(layout.message_dbs) == 3

    def test_missing_directory_is_a_source_error(self, tmp_path: Path) -> None:
        with pytest.raises(SourceNotFoundError):
            locate_snapshot(tmp_path / "absent")

    def test_a_file_is_not_a_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "file.db"
        target.write_bytes(b"")
        with pytest.raises(SourceNotFoundError, match="not a directory"):
            locate_snapshot(target)

    def test_unrelated_databases_are_kept_apart(self, tmp_path: Path) -> None:
        snapshot = build_alice_snapshot(tmp_path / "snap")
        (snapshot / "emoticon").mkdir()
        sqlite3.connect(snapshot / "emoticon" / "emoticon.db").close()
        layout = locate_snapshot(snapshot)
        assert [path.name for path in layout.other_dbs] == ["emoticon.db"]


class TestMessageColumns:
    def test_detects_the_standard_wcdb_layout(self) -> None:
        columns = MessageColumns.detect(
            [
                "localId",
                "localType",
                "createTime",
                "isSend",
                "message_content",
                "WCDB_CT_message_content",
            ]
        )
        assert columns.local_id == "localId"
        assert columns.local_type == "localType"
        assert columns.create_time == "createTime"
        assert columns.is_send == "isSend"
        assert columns.content == "message_content"
        assert columns.content_type_code == "WCDB_CT_message_content"
        assert columns.missing_required == []

    def test_detects_snake_case(self) -> None:
        columns = MessageColumns.detect(["local_id", "local_type", "create_time", "is_send"])
        assert columns.local_id == "local_id"
        assert columns.create_time == "create_time"
        assert columns.missing_required == []

    def test_reports_which_required_columns_are_missing(self) -> None:
        columns = MessageColumns.detect(["localType", "message_content"])
        assert set(columns.missing_required) == {"local_id", "create_time"}

    def test_absent_optional_columns_stay_none(self) -> None:
        columns = MessageColumns.detect(["localId", "createTime"])
        assert columns.content_type_code is None
        assert columns.is_send is None


class TestIntrospect:
    def test_reads_contacts(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        schema = introspect(tmp_path / "snap")
        assert ALICE in schema.contacts
        assert schema.contacts[ALICE].display_name == "Alice"

    def test_resolves_table_md5_to_a_username(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        schema = introspect(tmp_path / "snap")
        assert len(schema.message_tables) == 1
        table = schema.message_tables[0]
        assert table.table_name == message_table_name(ALICE)
        assert table.username == ALICE
        assert table.is_resolved

    def test_counts_messages(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        schema = introspect(tmp_path / "snap")
        assert schema.total_messages == 10

    def test_handles_an_unresolvable_table(self, tmp_path: Path) -> None:
        """A table whose digest is not in the contact list is reported, not fatal."""
        build_alice_snapshot(tmp_path / "snap")
        connection = sqlite3.connect(tmp_path / "snap" / "message" / "message_0.db")
        connection.execute(
            f'CREATE TABLE "Msg_{md5_of("wxid_ghost")}" (localId INTEGER, createTime INTEGER)'
        )
        connection.commit()
        connection.close()

        schema = introspect(tmp_path / "snap")
        assert len(schema.message_tables) == 2
        assert len(schema.unresolved_tables) == 1
        assert any("could not be matched" in warning for warning in schema.warnings)

    def test_missing_contact_database_warns_but_still_introspects(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap", with_contact_db=False)
        schema = introspect(tmp_path / "snap")
        assert schema.contacts == {}
        assert schema.unresolved_tables
        assert any("no contact database" in warning for warning in schema.warnings)

    def test_reads_sessions(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        schema = introspect(tmp_path / "snap")
        assert schema.sessions[ALICE].last_timestamp == T_BASE + 7200

    @pytest.mark.parametrize("variant", ["standard", "no_compression_column", "snake_case"])
    def test_every_schema_variant_introspects(self, tmp_path: Path, variant: str) -> None:
        build_mixed_snapshot(tmp_path / "snap", schema_variant=variant)
        schema = introspect(tmp_path / "snap")
        assert len(schema.message_tables) == 3
        assert schema.total_messages == 10

    def test_snake_case_columns_are_mapped(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap", schema_variant="snake_case")
        schema = introspect(tmp_path / "snap")
        columns = schema.columns_for(message_table_name(ALICE))
        assert columns.local_id == "local_id"
        assert columns.create_time == "create_time"

    def test_a_table_without_required_columns_is_skipped_with_a_warning(
        self, tmp_path: Path
    ) -> None:
        snapshot = build_alice_snapshot(tmp_path / "snap")
        connection = sqlite3.connect(snapshot / "message" / "message_0.db")
        connection.execute(f'CREATE TABLE "Msg_{md5_of(BOB)}" (something TEXT)')
        connection.commit()
        connection.close()

        schema = introspect(snapshot)
        assert len(schema.message_tables) == 1
        assert any("missing required column" in warning for warning in schema.warnings)

    def test_no_message_tables_is_unsupported_not_empty(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snap"
        snapshot.mkdir()
        sqlite3.connect(snapshot / "contact.db").close()
        with pytest.raises(SchemaUnsupportedError):
            introspect(snapshot)

    def test_no_databases_at_all_is_a_source_error(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snap"
        snapshot.mkdir()
        with pytest.raises(SourceNotFoundError, match="no SQLite databases"):
            introspect(snapshot)

    def test_unrelated_databases_are_reported(self, tmp_path: Path) -> None:
        snapshot = build_alice_snapshot(tmp_path / "snap")
        (snapshot / "sns").mkdir()
        sqlite3.connect(snapshot / "sns" / "sns.db").close()
        schema = introspect(snapshot)
        assert any("unrelated database" in warning for warning in schema.warnings)


class TestReadOnlyDatabase:
    def test_refuses_writes(self, tmp_path: Path) -> None:
        """§2.3: never modify or destroy the original imported data."""
        build_alice_snapshot(tmp_path / "snap")
        with (
            ReadOnlyDatabase(tmp_path / "snap" / "contact" / "contact.db") as database,
            pytest.raises(sqlite3.OperationalError, match="readonly"),
        ):
            database.connect().execute("DELETE FROM contact")

    def test_rejects_an_unknown_table(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        with (
            ReadOnlyDatabase(tmp_path / "snap" / "contact" / "contact.db") as database,
            pytest.raises(DatabaseInvalidError, match="no such table"),
        ):
            list(database.iter_rows("nope"))

    def test_rejects_an_unknown_order_by_column(self, tmp_path: Path) -> None:
        build_alice_snapshot(tmp_path / "snap")
        with (
            ReadOnlyDatabase(tmp_path / "snap" / "contact" / "contact.db") as database,
            pytest.raises(DatabaseInvalidError, match="missing column"),
        ):
            list(database.iter_rows("contact", order_by=["nope"]))

    def test_quoting_survives_an_embedded_quote(self) -> None:
        assert ReadOnlyDatabase._quote_identifier('we"ird') == '"we""ird"'

    def test_count_by_column_groups_values(self, tmp_path: Path) -> None:
        build_mixed_snapshot(tmp_path / "snap")
        with ReadOnlyDatabase(tmp_path / "snap" / "message" / "message_0.db") as database:
            grouped = database.count_by_column(message_table_name(ALICE), "localType")
        assert grouped[1] == 2
        assert grouped[3] == 1
        assert grouped[49] == 2

    def test_missing_file_is_a_source_error(self, tmp_path: Path) -> None:
        with pytest.raises(SourceUnreadableError):
            ReadOnlyDatabase(tmp_path / "absent.db").connect()

    def test_a_non_sqlite_file_is_a_database_error(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.db"
        bogus.write_bytes(b"this is definitely not a database")
        with pytest.raises(DatabaseInvalidError):
            ReadOnlyDatabase(bogus).connect()
