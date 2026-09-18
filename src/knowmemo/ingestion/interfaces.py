"""The connector contract (design doc §10, §11).

Every data source implements :class:`SourceConnector`. This interface is the
architectural seam that keeps WeChat out of the core: the pipeline downstream
of a connector only ever sees :class:`~knowmemo.domain.message.Message` and
:class:`~knowmemo.domain.conversation.Conversation`.

Hard prohibitions (§11) — a connector must **never**:

* call an LLM
* compute embeddings
* perform RAG or retrieval
* construct prompts
* contain WeKnora-specific logic

Connectors are pure readers. They observe a source and emit normalised domain
objects. Everything else is somebody else's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from knowmemo.domain.conversation import Conversation
from knowmemo.domain.message import Message
from knowmemo.domain.source import Source


class ExtractedConversation(BaseModel):
    """A normalised conversation together with its normalised messages.

    The specification's §10 sketch has ``extract()`` return ``Conversation``
    objects, but :class:`Conversation` deliberately carries counts rather than
    payloads. Segmentation (§15) needs the messages themselves, so the
    connector contract returns both. Refining a conceptual signature is
    cheaper than making ``Conversation`` mean two different things.
    """

    conversation: Conversation
    messages: list[Message]


class ScanResult(BaseModel):
    """What ``knowmemo source scan`` reports (§12).

    Counts are what the connector could *see*, not what it parsed — a scan is
    allowed to be cheap and may skip message-level detail.
    """

    source_type: str
    source_id: str | None = None
    path: str | None = None

    conversations: int = 0
    messages: int = 0

    earliest: datetime | None = None
    latest: datetime | None = None

    details: dict[str, Any] = Field(default_factory=dict)


class HealthCheck(BaseModel):
    """A single connector health probe result."""

    name: str
    ok: bool
    detail: str | None = None
    #: What the user should do about it, when there is something to do.
    remedy: str | None = None


class SourceConnector(ABC):
    """Base class for every data-source connector.

    Subclasses declare :attr:`source_type` and implement the four methods
    below. Nothing else in KnowMemo should need to know their names.
    """

    #: Matches :class:`~knowmemo.domain.source.SourceType`.
    source_type: str = ""

    def __init__(self, **options: Any) -> None:
        self.options = options

    @abstractmethod
    def discover(self) -> Source:
        """Locate the source and report whether it is usable.

        Must not raise for a missing or unreadable source — return a
        :class:`~knowmemo.domain.source.Source` carrying the appropriate
        :class:`~knowmemo.domain.source.SourceStatus` instead. Discovery runs
        before the user has necessarily configured anything.
        """

    @abstractmethod
    def health_check(self) -> list[HealthCheck]:
        """Return diagnostics suitable for ``knowmemo doctor``.

        Each check should carry a ``remedy`` when the fix is actionable by the
        user (a missing permission, a path that does not exist).
        """

    @abstractmethod
    def scan(self) -> ScanResult:
        """Count what is present, cheaply, without full parsing."""

    @abstractmethod
    def extract(self) -> Iterator[ExtractedConversation]:
        """Yield fully normalised conversations with their messages.

        Streams rather than returns a list: a mailbox or a chat history can be
        large, and the caller should be able to checkpoint progress.

        Implementations must be resumable and must not mutate the source.
        """


class ConnectorRegistry:
    """Maps a source type to its connector class.

    Kept as an explicit registry rather than a dynamic plugin scan so that the
    set of connectors is visible in one place and testable in isolation.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, type[SourceConnector]] = {}

    def register(self, connector: type[SourceConnector]) -> type[SourceConnector]:
        if not connector.source_type:
            raise ValueError(f"{connector.__name__} does not declare source_type")
        self._connectors[connector.source_type] = connector
        return connector

    def get(self, source_type: str) -> type[SourceConnector] | None:
        return self._connectors.get(source_type)

    def available(self) -> list[str]:
        return sorted(self._connectors)

    def create(self, source_type: str, **options: Any) -> SourceConnector:
        connector = self.get(source_type)
        if connector is None:
            raise KeyError(
                f"no connector registered for {source_type!r}; "
                f"available: {', '.join(self.available()) or '<none>'}"
            )
        return connector(**options)


registry = ConnectorRegistry()
