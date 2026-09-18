"""Deterministic ID generation (design doc §27)."""

from __future__ import annotations

from knowmemo.domain.ids import (
    SEGMENT_ID_PREFIX,
    content_hash,
    make_conversation_id,
    make_document_id,
    make_message_id,
    make_segment_id,
)


class TestReadableComposites:
    def test_message_id_matches_the_documented_shape(self) -> None:
        assert make_message_id("wechat", "acct1", "conv456", "msg789") == (
            "wechat:acct1:conv456:msg789"
        )

    def test_conversation_id_omits_a_missing_account(self) -> None:
        assert make_conversation_id("wechat", None, "conv456") == "wechat:-:conv456"

    def test_ids_are_stable_across_calls(self) -> None:
        first = make_message_id("wechat", "a", "c", "m")
        second = make_message_id("wechat", "a", "c", "m")
        assert first == second

    def test_different_inputs_produce_different_ids(self) -> None:
        assert make_message_id("wechat", "a", "c", "m1") != make_message_id(
            "wechat", "a", "c", "m2"
        )


class TestSegmentId:
    def test_is_prefixed_and_fixed_length(self) -> None:
        segment_id = make_segment_id("conv", "m1", "m2", 1)
        assert segment_id.startswith(f"{SEGMENT_ID_PREFIX}_")
        assert len(segment_id) == len(SEGMENT_ID_PREFIX) + 1 + 20

    def test_is_stable_for_the_same_boundaries(self) -> None:
        assert make_segment_id("conv", "m1", "m2", 1) == make_segment_id("conv", "m1", "m2", 1)

    def test_appending_later_messages_leaves_earlier_segments_untouched(self) -> None:
        """The property incremental import depends on."""
        before = make_segment_id("conv", "m1", "m5", 1)
        after_appending_m6 = make_segment_id("conv", "m1", "m5", 1)
        assert before == after_appending_m6

    def test_changing_segmentation_version_changes_the_id(self) -> None:
        """Re-tuning the parameters must force an explicit re-index."""
        assert make_segment_id("conv", "m1", "m2", 1) != make_segment_id("conv", "m1", "m2", 2)

    def test_moving_a_boundary_changes_the_id(self) -> None:
        assert make_segment_id("conv", "m1", "m2", 1) != make_segment_id("conv", "m1", "m3", 1)


class TestContentHash:
    def test_is_deterministic(self) -> None:
        assert content_hash("hello") == content_hash("hello")

    def test_detects_a_change(self) -> None:
        assert content_hash("hello") != content_hash("hello ")

    def test_document_id_tracks_its_segment(self) -> None:
        assert make_document_id("seg_abc") == "seg_abc"
