"""Structured logging with mandatory redaction (design doc §28).

The rule is absolute: raw message content must never reach a log sink. Logs
carry identifiers and status only —

    message_id, conversation_id, processing_status, error, timestamp

A :class:`RedactionFilter` enforces this defensively. It scrubs both
``extra`` keys and format arguments, so a careless
``log.info("parsed %s", msg.content)`` degrades to a placeholder rather than
leaking a private conversation to disk.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "[redacted]"

#: Keys that must never be logged verbatim. Matched case-insensitively
#: against both mapping keys and record attribute names.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "content",
        "message_content",
        "text",
        "body",
        "raw",
        "raw_content",
        "payload",
        "api_key",
        "apikey",
        "token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "session_key",
    }
)

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


def _is_sensitive(key: str) -> bool:
    return key.lower() in SENSITIVE_KEYS


def _scrub_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive(str(key)) else _scrub_mapping(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_mapping(item) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Strip sensitive keys from a record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED or key.startswith("_"):
                continue
            if _is_sensitive(key):
                setattr(record, key, REDACTED)
            else:
                setattr(record, key, _scrub_mapping(value))

        if record.args:
            if isinstance(record.args, dict):
                record.args = _scrub_mapping(record.args)
            else:
                record.args = tuple(
                    REDACTED if isinstance(arg, str) and len(arg) > 200 else arg
                    for arg in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable and machine-parseable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable fallback for interactive CLI use."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        tail = ""
        if extras:
            tail = " " + " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        base = f"{stamp} {record.levelname:<7} {record.getMessage()}{tail}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: Any = None,
) -> None:
    """Install KnowMemo's logging configuration on the root logger.

    Idempotent: calling it twice replaces the previous handler rather than
    stacking duplicates.
    """

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger. ``name`` is normally ``__name__``."""
    return logging.getLogger(name)
