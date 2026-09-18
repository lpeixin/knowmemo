"""Where KnowMemo's derived files live on disk (design doc §7, §2.3).

One module owns the data-tree layout, so "which file holds this conversation"
has exactly one answer. Both derived artifacts — the normalised JSONL
transcript and the knowledge Markdown document — are named from *stable
identifiers*, never from a timestamp or a counter. Re-running an import
therefore overwrites the same paths instead of accumulating duplicates (§2.4).

The names are derived from content rather than from user-supplied strings
alone: a conversation title is sanitised for readability and paired with a
digest of the conversation ID, because two contacts can share a display name
and a display name can contain a path separator.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SLUG_FALLBACK = "unnamed"
DEFAULT_SLUG_LENGTH = 48
DEFAULT_DIGEST_LENGTH = 8


def safe_slug(text: str | None, *, max_length: int = DEFAULT_SLUG_LENGTH) -> str:
    """Reduce arbitrary text to something safe in a filename.

    ``str.isalnum`` is Unicode-aware, so CJK display names survive intact
    instead of being stripped to nothing.
    """
    if not text:
        return SLUG_FALLBACK
    cleaned = "".join(char if char.isalnum() else "-" for char in text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-")[:max_length].strip("-")
    return cleaned or SLUG_FALLBACK


def short_digest(value: str, *, length: int = DEFAULT_DIGEST_LENGTH) -> str:
    """A short, stable discriminator for a long identifier."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalized_dir(root: Path, source_type: str | None = None) -> Path:
    """``data/normalized/`` — the §2.3 record of what was actually read."""
    base = root / "normalized"
    return base / source_type if source_type else base


def knowledge_dir(root: Path, source_type: str | None = None) -> Path:
    """``data/knowledge/`` — the §16 documents that feed retrieval."""
    base = root / "knowledge"
    return base / source_type if source_type else base


def normalized_file(
    root: Path,
    *,
    source_type: str,
    conversation_id: str,
    conversation_name: str | None = None,
) -> Path:
    """Path of the JSONL transcript for one conversation.

    Deterministic in ``conversation_id``; the readable part is decoration.
    """
    name = f"{safe_slug(conversation_name)}__{short_digest(conversation_id)}.jsonl"
    return normalized_dir(root, source_type) / name


def knowledge_file(
    root: Path,
    *,
    source_type: str,
    segment_id: str,
    conversation_name: str | None = None,
) -> Path:
    """Path of the Markdown document for one segment.

    ``segment_id`` is already a fixed-length hex digest, so it is filename-safe
    by construction — and it is the only part that has to be unique.
    """
    name = f"{safe_slug(conversation_name)}__{segment_id}.md"
    return knowledge_dir(root, source_type) / name
