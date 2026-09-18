"""Content decoding: type mapping, decompression, app-message XML (§8, §13)."""

from __future__ import annotations

import pytest

from knowmemo.connectors.wechat.decoders import (
    WCDB_TYPE_ZSTD,
    AppMessage,
    ContentDecodingError,
    decode_payload,
    extract_text,
    map_message_type,
    parse_app_message,
)
from knowmemo.domain.message import MessageType


class TestMapMessageType:
    @pytest.mark.parametrize(
        ("local_type", "expected"),
        [
            (1, MessageType.TEXT),
            (3, MessageType.IMAGE),
            (34, MessageType.VOICE),
            (42, MessageType.CONTACT),
            (43, MessageType.VIDEO),
            (47, MessageType.STICKER),
            (48, MessageType.LOCATION),
            (10000, MessageType.SYSTEM),
            (10002, MessageType.SYSTEM),
        ],
    )
    def test_maps_known_types(self, local_type: int, expected: MessageType) -> None:
        assert map_message_type(local_type) is expected

    def test_unknown_type_is_not_guessed(self) -> None:
        assert map_message_type(99999) is MessageType.UNKNOWN

    def test_absent_type_is_unknown(self) -> None:
        assert map_message_type(None) is MessageType.UNKNOWN

    def test_app_message_type_is_refined_by_inner_type(self) -> None:
        link = AppMessage(app_type=5)
        assert map_message_type(49, link) is MessageType.LINK
        file = AppMessage(app_type=6)
        assert map_message_type(49, file) is MessageType.FILE
        quote = AppMessage(app_type=57)
        assert map_message_type(49, quote) is MessageType.TEXT

    def test_unmapped_app_subtype_falls_back_to_unknown(self) -> None:
        assert map_message_type(49, AppMessage(app_type=12345)) is MessageType.UNKNOWN

    def test_app_message_without_inner_type_is_unknown(self) -> None:
        assert map_message_type(49, AppMessage()) is MessageType.UNKNOWN


class TestDecodePayload:
    def test_plain_text_passes_through(self) -> None:
        assert decode_payload("你好".encode(), None) == "你好"

    def test_str_payload_passes_through(self) -> None:
        assert decode_payload("你好", None) == "你好"

    def test_none_and_empty_become_none(self) -> None:
        assert decode_payload(None, None) is None
        assert decode_payload(b"", None) is None
        assert decode_payload("", None) is None

    def test_undecodable_bytes_are_replaced_not_raised(self) -> None:
        result = decode_payload(b"\xff\xfe invalid", None)
        assert result is not None
        assert "invalid" in result

    def test_zstd_payload_is_decompressed(self) -> None:
        zstandard = pytest.importorskip("zstandard")
        payload = zstandard.ZstdCompressor().compress("压缩过的消息".encode())
        assert decode_payload(payload, WCDB_TYPE_ZSTD) == "压缩过的消息"

    def test_zstd_payload_without_the_library_is_an_explicit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing optional dependency must name itself, not fail obscurely."""
        import knowmemo.connectors.wechat.decoders as decoders

        def _missing():
            raise ContentDecodingError("install 'knowmemo[wechat]'")

        monkeypatch.setattr(decoders, "_require_zstandard", _missing)
        with pytest.raises(ContentDecodingError, match=r"knowmemo\[wechat\]"):
            decoders.decompress_zstd(b"not really zstd")


class TestParseAppMessage:
    def test_extracts_a_link(self) -> None:
        xml = (
            "<msg><appmsg><title>MCP 规范</title><des>模型上下文协议</des>"
            "<type>5</type><url>https://example.com</url></appmsg></msg>"
        )
        message = parse_app_message(xml)
        assert message.app_type == 5
        assert message.title == "MCP 规范"
        assert message.url == "https://example.com"

    def test_extracts_a_file_attachment(self) -> None:
        xml = (
            "<msg><appmsg><title>设计文档.pdf</title><type>6</type>"
            "<appattach><totallen>204800</totallen><fileext>pdf</fileext></appattach>"
            "</appmsg></msg>"
        )
        message = parse_app_message(xml)
        assert message.app_type == 6
        assert message.file_size == 204800
        assert message.extras["fileext"] == "pdf"

    def test_extracts_a_quote_reference(self) -> None:
        xml = (
            "<msg><appmsg><title>回复内容</title><type>57</type>"
            "<refermsg><svrid>9988</svrid><content>原始消息</content></refermsg>"
            "</appmsg></msg>"
        )
        message = parse_app_message(xml)
        assert message.refer_server_id == "9988"
        assert message.refer_content == "原始消息"

    def test_malformed_xml_degrades_instead_of_raising(self) -> None:
        message = parse_app_message("<msg><appmsg><title>unclosed")
        assert message.app_type is None
        assert message.description is not None

    def test_plain_text_without_xml_envelope(self) -> None:
        message = parse_app_message("just some text")
        assert message.description == "just some text"

    def test_best_text_prefers_title_then_filename(self) -> None:
        assert AppMessage(title="T", filename="F", description="D").best_text == "T"
        assert AppMessage(filename="F", description="D").best_text == "F"
        assert AppMessage(description="  ").best_text is None


class TestExtractText:
    def test_text_message_keeps_its_content(self) -> None:
        assert extract_text(1, "最近在学习 AI Agent。", None) == "最近在学习 AI Agent。"

    @pytest.mark.parametrize("local_type", [3, 34, 43, 47, 42])
    def test_pure_media_yields_no_indexable_text(self, local_type: int) -> None:
        """Placeholders would pollute semantic search with unmatchable tokens."""
        assert extract_text(local_type, "<msg/>", None) is None

    def test_link_uses_its_title(self) -> None:
        app = AppMessage(app_type=5, title="MCP 规范", url="https://example.com")
        assert extract_text(49, "<msg/>", app) == "MCP 规范"

    def test_file_uses_its_name(self) -> None:
        app = AppMessage(app_type=6, filename="设计文档.pdf")
        assert extract_text(49, "<msg/>", app) == "设计文档.pdf"

    def test_quote_uses_the_reply_body(self) -> None:
        app = AppMessage(app_type=57, title="回复内容", refer_content="原始消息")
        assert extract_text(49, "<msg/>", app) == "回复内容"

    def test_missing_content_stays_none(self) -> None:
        assert extract_text(1, None, None) is None
