"""Deterministic file naming for derived artifacts (§2.4, §7)."""

from __future__ import annotations

from pathlib import Path

from knowmemo.storage.layout import (
    SLUG_FALLBACK,
    knowledge_file,
    normalized_file,
    safe_slug,
    short_digest,
)

ROOT = Path("/data")


class TestSafeSlug:
    def test_keeps_cjk(self) -> None:
        assert safe_slug("产品讨论组") == "产品讨论组"

    def test_removes_path_separators(self) -> None:
        assert "/" not in safe_slug("a/b")
        assert safe_slug("a/b") == "a-b"

    def test_collapses_runs_of_punctuation(self) -> None:
        assert safe_slug("a /// b") == "a-b"

    def test_strips_leading_and_trailing_punctuation(self) -> None:
        assert safe_slug("  ...hello...  ") == "hello"

    def test_empty_input_falls_back(self) -> None:
        assert safe_slug("") == SLUG_FALLBACK
        assert safe_slug(None) == SLUG_FALLBACK

    def test_punctuation_only_falls_back(self) -> None:
        assert safe_slug("///") == SLUG_FALLBACK

    def test_truncates_without_a_trailing_separator(self) -> None:
        slug = safe_slug("a" * 40 + " " + "b" * 40, max_length=20)
        assert len(slug) <= 20
        assert not slug.endswith("-")


class TestPaths:
    def test_normalized_path_is_stable_for_a_conversation(self) -> None:
        first = normalized_file(
            ROOT, source_type="wechat", conversation_id="c1", conversation_name="Alice"
        )
        second = normalized_file(
            ROOT, source_type="wechat", conversation_id="c1", conversation_name="Alice"
        )
        assert first == second

    def test_two_conversations_sharing_a_name_do_not_collide(self) -> None:
        """Display names collide; IDs do not. The digest is the identity."""
        first = normalized_file(
            ROOT, source_type="wechat", conversation_id="c1", conversation_name="Alice"
        )
        second = normalized_file(
            ROOT, source_type="wechat", conversation_id="c2", conversation_name="Alice"
        )
        assert first != second

    def test_paths_live_under_the_source_type(self) -> None:
        assert (
            normalized_file(ROOT, source_type="wechat", conversation_id="c1").parent
            == ROOT / "normalized" / "wechat"
        )
        assert (
            knowledge_file(ROOT, source_type="wechat", segment_id="seg_abc").parent
            == ROOT / "knowledge" / "wechat"
        )

    def test_knowledge_filename_ends_with_the_segment_id(self) -> None:
        path = knowledge_file(
            ROOT, source_type="wechat", segment_id="seg_abc", conversation_name="Alice"
        )
        assert path.name.endswith("__seg_abc.md")

    def test_jsonl_suffix(self) -> None:
        assert normalized_file(ROOT, source_type="wechat", conversation_id="c1").suffix == ".jsonl"

    def test_digest_is_stable_and_short(self) -> None:
        assert short_digest("c1") == short_digest("c1")
        assert len(short_digest("c1")) == 8
        assert short_digest("c1") != short_digest("c2")
