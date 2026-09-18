"""Normalised record output (design doc §2.3).

Every conversation the pipeline reads is written back out as JSONL, one
message per line, before anything else happens to it. That file is the
*normalised record*: source-agnostic, complete, and re-readable without the
original connector. It exists so that a later change to segmentation,
document building or retrieval can be replayed against what was actually
imported, instead of requiring the source to still be reachable.

Serialisation is deterministic — ``sort_keys`` on, ``ensure_ascii`` off, and
the caller passes messages in a stable order — so the same input always
produces byte-identical output. That is what lets the pipeline compare a
content hash instead of re-uploading.

The file is written in place rather than through a temp-file-and-rename dance.
It is a derived artifact: fully reproducible from the source, and accompanied
by a recorded row count and content hash, so a truncated write is detectable
rather than silently authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from knowmemo.domain.ids import content_hash
from knowmemo.domain.message import Message


@dataclass(frozen=True)
class NormalizedWrite:
    """What was written, for the manifest (§26)."""

    path: Path
    message_count: int
    content_hash: str


def serialize_messages(messages: Sequence[Message]) -> str:
    """Render messages as JSONL text without touching the filesystem."""
    lines = [
        json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for message in messages
    ]
    return "\n".join(lines) + "\n" if lines else ""


def write_messages(path: Path, messages: Sequence[Message]) -> NormalizedWrite:
    """Write one conversation's messages to ``path`` as JSONL."""
    payload = serialize_messages(messages)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return NormalizedWrite(
        path=path,
        message_count=len(messages),
        content_hash=content_hash(payload),
    )


def read_messages(path: Path) -> Iterator[Message]:
    """Read a JSONL file back into domain objects.

    Raises ``pydantic.ValidationError`` on a malformed line rather than
    skipping it: a normalised record KnowMemo wrote and cannot read back is a
    defect worth surfacing, not data to paper over.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield Message.model_validate_json(line)


def count_records(path: Path) -> int:
    """Count non-empty lines without parsing them."""
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
