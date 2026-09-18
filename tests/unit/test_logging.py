"""Log redaction (design doc §28).

These tests are the enforcement mechanism for "never log raw message content".
"""

from __future__ import annotations

import json
import logging

import pytest

from knowmemo.logging_setup import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    TextFormatter,
    configure_logging,
)


@pytest.fixture
def captured() -> tuple[logging.Logger, list[str]]:
    logger = logging.getLogger("knowmemo.test")
    logger.handlers.clear()
    logger.propagate = False

    records: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(JsonFormatter().format(record))

    handler = Collector()
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger, records


class TestExtraKeyRedaction:
    def test_content_is_redacted(self, captured) -> None:
        logger, records = captured
        logger.info(
            "parsed message",
            extra={"message_id": "m1", "content": "最近在学习 AI Agent。"},
        )
        payload = json.loads(records[0])
        assert payload["message_id"] == "m1"
        assert payload["content"] == REDACTED
        assert "AI Agent" not in records[0]

    def test_secrets_are_redacted(self, captured) -> None:
        logger, records = captured
        logger.info("auth", extra={"api_key": "sk-live-123", "token": "abc"})
        payload = json.loads(records[0])
        assert payload["api_key"] == REDACTED
        assert payload["token"] == REDACTED
        assert "sk-live-123" not in records[0]

    def test_nested_mappings_are_scrubbed(self, captured) -> None:
        logger, records = captured
        logger.info("ctx", extra={"metadata": {"content": "secret text", "id": "m1"}})
        payload = json.loads(records[0])
        assert payload["metadata"]["content"] == REDACTED
        assert payload["metadata"]["id"] == "m1"

    def test_safe_identifiers_survive(self, captured) -> None:
        """§28 asks for message_id, conversation_id, status, error, timestamp."""
        logger, records = captured
        logger.info(
            "stage done",
            extra={
                "message_id": "m1",
                "conversation_id": "c1",
                "processing_status": "NORMALIZED",
                "error": None,
            },
        )
        payload = json.loads(records[0])
        assert payload["message_id"] == "m1"
        assert payload["conversation_id"] == "c1"
        assert payload["processing_status"] == "NORMALIZED"


class TestArgumentRedaction:
    def test_long_string_arguments_are_dropped(self, captured) -> None:
        """Defends against ``log.info("parsed %s", msg.content)``."""
        logger, records = captured
        logger.info("parsed %s", "x" * 500)
        assert "x" * 500 not in records[0]
        assert REDACTED in records[0]

    def test_short_arguments_are_kept(self, captured) -> None:
        logger, records = captured
        logger.info("stage=%s", "normalize")
        assert "normalize" in records[0]

    def test_mapping_arguments_are_scrubbed(self, captured) -> None:
        logger, records = captured
        logger.info("record %(message_id)s", {"message_id": "m1", "content": "private"})
        assert "private" not in records[0]


class TestFormatters:
    def test_json_formatter_emits_one_object_per_line(self, captured) -> None:
        logger, records = captured
        logger.info("a")
        logger.info("b")
        assert len(records) == 2
        assert all(json.loads(record)["message"] in {"a", "b"} for record in records)

    def test_text_formatter_includes_extras(self) -> None:
        record = logging.LogRecord("n", logging.INFO, "p", 1, "hello", None, None)
        record.message_id = "m1"
        assert "message_id=m1" in TextFormatter().format(record)


class TestConfigureLogging:
    def test_is_idempotent(self) -> None:
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_installs_the_redaction_filter(self) -> None:
        configure_logging("INFO")
        handler = logging.getLogger().handlers[0]
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
