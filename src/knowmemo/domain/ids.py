"""Deterministic identifier generation (design doc §27).

Imported records must be identified deterministically so that re-running an
import cannot create duplicates. Random UUIDs are therefore never the sole
identity of an imported record.

Two shapes are produced:

* :func:`make_message_id` and :func:`make_conversation_id` return the readable
  composite form shown in §14 — ``wechat:account:conversation:message``. It is
  deterministic, debuggable, and stable across runs.
* :func:`make_segment_id` returns a fixed-length digest, because segment IDs
  end up in filenames and in WeKnora metadata where length matters.

The segment hash deliberately includes ``segmentation_version``. Without it,
re-tuning ``inactivity_minutes`` or ``max_messages`` would silently change
every boundary, hence every segment ID, hence trigger a full re-ingest with no
explanation. With it, changing the parameters is an explicit re-index.
"""

from __future__ import annotations

import hashlib

SEGMENT_ID_PREFIX = "seg"
SEGMENT_DIGEST_LENGTH = 20

#: Separator for the readable composite form. Chosen because source identifiers
#: (wxid, chatroom ids) never contain a colon.
SEPARATOR = ":"


def _digest(*parts: str, length: int) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:length]


def make_conversation_id(
    source_type: str,
    source_account: str | None,
    source_conversation_id: str,
) -> str:
    """Build a stable conversation identifier.

    >>> make_conversation_id("wechat", "acct1", "conv456")
    'wechat:acct1:conv456'
    """
    account = source_account or "-"
    return SEPARATOR.join((source_type, account, source_conversation_id))


def make_message_id(
    source_type: str,
    source_account: str | None,
    conversation_id: str,
    source_message_id: str,
) -> str:
    """Build a stable message identifier in the §14 composite form.

    >>> make_message_id("wechat", "acct1", "conv456", "msg789")
    'wechat:acct1:conv456:msg789'
    """
    account = source_account or "-"
    return SEPARATOR.join((source_type, account, conversation_id, source_message_id))


def make_segment_id(
    conversation_id: str,
    first_message_id: str,
    last_message_id: str,
    segmentation_version: int,
) -> str:
    """Hash a segment's identity (§27, with the segmentation_version fix).

    Stable under append-only imports: appending later messages leaves existing
    segments' boundaries — and therefore their IDs — untouched.
    """
    digest = _digest(
        conversation_id,
        first_message_id,
        last_message_id,
        str(segmentation_version),
        length=SEGMENT_DIGEST_LENGTH,
    )
    return f"{SEGMENT_ID_PREFIX}_{digest}"


def make_document_id(segment_id: str) -> str:
    """Derive the knowledge-document ID from its segment.

    One segment yields exactly one document, so the mapping is 1:1 and the
    document needs no identity of its own.
    """
    return segment_id


def content_hash(text: str) -> str:
    """Hash arbitrary content — used for dedup and for upload change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
