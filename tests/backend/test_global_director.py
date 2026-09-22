"""

The invariant under test everywhere: any layer may fail, but a failure only
never triggers a whole-movie fallback, and every chunk has non-empty queries.
"""
import json
import re

import pytest

from backend.services import global_director as gd
from backend.services.global_director import (
    build_deterministic_segments,
    make_segment_aligned_windows,
    merge_batch_results,
    _build_global_plan_segmented,
)


# --------------------------------------------------------------------------- #
# FakeLLM — programmable per-purpose / per-window
# --------------------------------------------------------------------------- #
_VALID_Q = ["wildlife nature scene", "savanna landscape wide"]


def _default_skeleton(user: str) -> str:
    seg_ids = re.findall(r"\[(seg_\d+)\]", user)
    return json.dumps({
        "video_meta": {
            "theme": "nature", "visual_world": "savanna",
            "global_no_show": ["cartoon"],
            "motif_pool": ["african savanna", "wildlife nature", "dry grass wind",
                           "rocky terrain", "sky clouds time", "river water flow"],
        },
        "sections": [
            {"segment_id": sid, "role": "segment", "motif": "wild nature",
             "visual_direction": "wide nature shots",
             "search_bank": ["savanna wildlife", "nature landscape", "animal habitat"]}
            for sid in seg_ids
        ],
    })


def _parse_target_ids(user: str) -> list[int]:
    m = re.search(r"TARGET_CHUNK_IDS:\s*\[([^\]]*)\]", user)
    if not m or not m.group(1).strip():
        return []
    return [int(x) for x in re.findall(r"\d+", m.group(1))]


def _default_expand(ids: list[int]) -> str:
    return json.dumps({"items": [
        {"id": i, "q": list(_VALID_Q), "show": ["animal"], "avoid": [], "m": "wild"}
        for i in ids
    ]})


class FakeLLM:
    def __init__(self, *, skeleton_fn=None, expand_fn=None):
        self.skeleton_fn = skeleton_fn or _default_skeleton
        self.expand_fn = expand_fn or (lambda ids, user: _default_expand(ids))
        self.calls: list[str] = []

    def _call(self, system, user, purpose=None):
        self.calls.append(purpose)
        if purpose == "global_director_skeleton":
            return self.skeleton_fn(user)
        if purpose == "global_director_expand":
            return self.expand_fn(_parse_target_ids(user), user)
        raise AssertionError(f"unexpected purpose: {purpose}")


def _sentences(n: int) -> list[str]:
    return [f"第{i}句中文内容。" for i in range(n)]


def _assert_healthy(plan, n):
    idxs = [c["chunk_index"] for c in plan["chunks"]]
    assert set(idxs) == set(range(n))
    assert len(idxs) == len(set(idxs))          # no duplicates
    assert plan["meta"]["whole_movie_fallback"] is False
    assert all(c["queries"] for c in plan["chunks"])  # never empty
    assert [c["chunk_index"] for c in plan["chunks"]] == list(range(n))  # order


# --------------------------------------------------------------------------- #
# segments
# --------------------------------------------------------------------------- #
def test_segments_cover_all_and_preserve_order():
    segs = build_deterministic_segments(_sentences(25), max_chunks=10)
    flat = [cid for s in segs for cid in s.chunk_ids]
    assert flat == list(range(25))


def test_segments_respect_max_chunks():
    segs = build_deterministic_segments(_sentences(25), max_chunks=10)
    assert all(len(s.chunk_ids) <= 10 for s in segs)
    assert len(segs) == 3  # 10 + 10 + 5


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #
def test_windows_cover_all_no_overlap_respect_max():
    segs = build_deterministic_segments(_sentences(173), max_chunks=10)
    windows = make_segment_aligned_windows(segs, max_target_chunks=24, context_segments=1)
    all_targets = [cid for w in windows for cid in w.target_chunk_ids]
    assert all_targets == list(range(173))             # cover + order
    assert len(all_targets) == len(set(all_targets))   # no overlap
    assert all(len(w.target_chunk_ids) <= 24 for w in windows)


def test_single_oversized_segment_becomes_its_own_window():
    # one giant segment of 30 chunks (> max 24) must still form a window
    segs = build_deterministic_segments(_sentences(30), max_chunks=30)
    assert len(segs) == 1
    windows = make_segment_aligned_windows(segs, max_target_chunks=24)
    assert len(windows) == 1
    assert windows[0].target_chunk_ids == list(range(30))


# --------------------------------------------------------------------------- #
# full segmented run — happy path
# --------------------------------------------------------------------------- #
def test_all_windows_ok_full_coverage_llm_source():
    n = 25
    plan = _build_global_plan_segmented(FakeLLM(), _sentences(n))
    _assert_healthy(plan, n)
    assert all(c["plan_source"] == "llm_expand" for c in plan["chunks"])
    assert plan["meta"]["fallback_chunk_count"] == 0
    assert plan["video_meta"]["motif_pool"]
    assert plan["video_meta"]["motif_pool_source"] == "llm"
    assert plan["meta"]["director_degraded"] is False
    assert plan["meta"]["degradation_reasons"] == []


# --------------------------------------------------------------------------- #
# merge matrix
# --------------------------------------------------------------------------- #
def test_merge_ignores_context_and_unknown_ids():
    n = 5
    batch = [{"window_id": "win_000", "target_chunk_ids": [0, 1], "status": "ok",
              "items": [
                  {"id": 0, "q": _VALID_Q},
                  {"id": 3, "q": _VALID_Q},   # context id (not in target) → drop
                  {"id": 99, "q": _VALID_Q},  # unknown id (>= n) → drop
              ]}]
    merge = merge_batch_results(_sentences(n), batch)
    assert set(merge.plan_core) == {0}
    assert 3 in [x for x in merge.unexpected] and 99 in [x for x in merge.unexpected]


def test_merge_missing_target_goes_to_fallback():
    n = 12

    def expand_fn(ids, user):
        # omit chunk id 5 from whatever window owns it
        return json.dumps({"items": [
            {"id": i, "q": _VALID_Q} for i in ids if i != 5
        ]})

    plan = _build_global_plan_segmented(FakeLLM(expand_fn=expand_fn), _sentences(n))
    _assert_healthy(plan, n)
    by_idx = {c["chunk_index"]: c for c in plan["chunks"]}
    assert by_idx[5]["plan_source"] == "deterministic_fallback"
    assert by_idx[4]["plan_source"] == "llm_expand"


def test_merge_invalid_item_goes_to_fallback():
    n = 6

    def expand_fn(ids, user):
        return json.dumps({"items": [
            {"id": i, "q": ([] if i == 2 else _VALID_Q)} for i in ids
        ]})

    plan = _build_global_plan_segmented(FakeLLM(expand_fn=expand_fn), _sentences(n))
    _assert_healthy(plan, n)
    by_idx = {c["chunk_index"]: c for c in plan["chunks"]}
    assert by_idx[2]["plan_source"] == "deterministic_fallback"
    assert by_idx[2]["queries"]  # fallback still gives real english queries


def test_merge_duplicate_id_does_not_override():
    n = 4
    batch = [{"window_id": "win_000", "target_chunk_ids": [0, 1], "status": "ok",
              "items": [
                  {"id": 0, "q": ["first valid query"]},
                  {"id": 0, "q": ["second should be dropped"]},
              ]}]
    merge = merge_batch_results(_sentences(n), batch)
    assert merge.plan_core[0]["queries"] == ["first valid query"]
    assert 0 in merge.duplicate


def test_failed_window_only_local_fallback():
    n = 40  # → multiple windows

    def expand_fn(ids, user):
        if 0 in ids:                         # fail the first window entirely
            raise RuntimeError("upstream provider error: upstream_timeout")
        return _default_expand(ids)

    plan = _build_global_plan_segmented(FakeLLM(expand_fn=expand_fn), _sentences(n))
    _assert_healthy(plan, n)
    by_idx = {c["chunk_index"]: c for c in plan["chunks"]}
    assert by_idx[0]["plan_source"] == "deterministic_fallback"   # failed window
    assert by_idx[n - 1]["plan_source"] == "llm_expand"           # other windows fine
    assert plan["meta"]["failed_window_count"] >= 1


# --------------------------------------------------------------------------- #
# skeleton failure still drives expansion
# --------------------------------------------------------------------------- #
def test_skeleton_failure_uses_deterministic_skeleton():
    n = 15

    def skeleton_fn(user):
        raise RuntimeError("upstream provider error: upstream_timeout")

    plan = _build_global_plan_segmented(FakeLLM(skeleton_fn=skeleton_fn), _sentences(n))
    _assert_healthy(plan, n)
    # expansion still ran on top of the deterministic skeleton
    assert plan["meta"]["skeleton_source"] == "deterministic"
    assert all(c["plan_source"] == "llm_expand" for c in plan["chunks"])
    assert plan["video_meta"]["motif_pool_source"] == "default_fallback"
    assert plan["meta"]["director_degraded"] is True
    assert "default_motif_pool" in plan["meta"]["degradation_reasons"]


def test_total_collapse_skeleton_and_all_windows_still_ships():
    n = 30

    def skeleton_fn(user):
        raise RuntimeError("upstream_timeout")

    def expand_fn(ids, user):
        raise RuntimeError("upstream_timeout")

    plan = _build_global_plan_segmented(
        FakeLLM(skeleton_fn=skeleton_fn, expand_fn=expand_fn), _sentences(n),
    )
    # even with EVERY LLM call dead, the video still gets a complete plan
    _assert_healthy(plan, n)
    assert all(c["plan_source"] == "deterministic_fallback" for c in plan["chunks"])
    assert plan["meta"]["whole_movie_fallback"] is False
    assert plan["meta"]["director_degraded"] is True
    assert "deterministic_chunk_fallback" in plan["meta"]["degradation_reasons"]


def test_default_motif_pool_detection_is_provenance_safe():
    assert gd.is_default_motif_pool(["forest leaf macro veins"])
    assert gd.is_default_motif_pool(gd.DEFAULT_MOTIF_POOL)
    assert not gd.is_default_motif_pool(["raven using stick tool"])
    assert not gd.is_default_motif_pool([
        "forest leaf macro veins",
        "raven using stick tool",
    ])


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #
def test_empty_input():
    plan = _build_global_plan_segmented(FakeLLM(), [])
    assert plan["chunks"] == []
    assert plan["meta"]["whole_movie_fallback"] is False


def test_short_text_fallback_non_empty_queries():
    n = 3

    def expand_fn(ids, user):
        raise RuntimeError("upstream_timeout")

    plan = _build_global_plan_segmented(FakeLLM(expand_fn=expand_fn), _sentences(n))
    _assert_healthy(plan, n)
    assert all(c["queries"] for c in plan["chunks"])


# --------------------------------------------------------------------------- #
# dispatcher: flag off → legacy path (preserves existing behavior)
# --------------------------------------------------------------------------- #
def test_dispatcher_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("USE_SEGMENTED_GLOBAL_DIRECTOR", raising=False)
    assert gd._use_segmented_director() is False
    monkeypatch.setenv("USE_SEGMENTED_GLOBAL_DIRECTOR", "1")
    assert gd._use_segmented_director() is True
