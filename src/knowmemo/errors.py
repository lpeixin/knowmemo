"""Categorised error taxonomy (design doc §35).

Every failure KnowMemo raises carries a machine-readable :class:`ErrorCode`.
The CLI and the API surface that code; the import pipeline records it per
record so that one malformed message never terminates a whole import.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """The closed set of failure categories from design doc §35."""

    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    DATABASE_INVALID = "DATABASE_INVALID"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    MESSAGE_PARSE_ERROR = "MESSAGE_PARSE_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    DOCUMENT_BUILD_ERROR = "DOCUMENT_BUILD_ERROR"
    WEKNORA_CONNECTION_ERROR = "WEKNORA_CONNECTION_ERROR"
    WEKNORA_UPLOAD_ERROR = "WEKNORA_UPLOAD_ERROR"
    LLM_ERROR = "LLM_ERROR"


class KnowMemoError(Exception):
    """Base class for all KnowMemo failures.

    ``context`` is meant for identifiers only — never message content
    (design doc §28). Callers may log it freely.
    """

    code: ErrorCode = ErrorCode.MESSAGE_PARSE_ERROR

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


class SourceNotFoundError(KnowMemoError):
    code = ErrorCode.SOURCE_NOT_FOUND


class SourceUnreadableError(KnowMemoError):
    """Raised when the OS refuses access — on macOS this is usually TCC.

    See ``docs/plan.md`` finding F8: reading the WeChat container requires
    Full Disk Access.
    """

    code = ErrorCode.SOURCE_UNREADABLE


class DatabaseInvalidError(KnowMemoError):
    code = ErrorCode.DATABASE_INVALID


class SchemaUnsupportedError(KnowMemoError):
    """Raised when a source database has a shape the connector cannot map."""

    code = ErrorCode.SCHEMA_UNSUPPORTED


class MessageParseError(KnowMemoError):
    code = ErrorCode.MESSAGE_PARSE_ERROR


class NormalizationError(KnowMemoError):
    code = ErrorCode.NORMALIZATION_ERROR


class DocumentBuildError(KnowMemoError):
    code = ErrorCode.DOCUMENT_BUILD_ERROR


class WeKnoraConnectionError(KnowMemoError):
    code = ErrorCode.WEKNORA_CONNECTION_ERROR


class WeKnoraUploadError(KnowMemoError):
    code = ErrorCode.WEKNORA_UPLOAD_ERROR


class LLMError(KnowMemoError):
    code = ErrorCode.LLM_ERROR


class NotImplementedYet(KnowMemoError):
    """Placeholder for pipeline stages scheduled for a later milestone.

    Distinct from a bug: this is "not built yet", and the design doc §SOUL
    insists the two are never conflated.
    """

    code = ErrorCode.DOCUMENT_BUILD_ERROR
