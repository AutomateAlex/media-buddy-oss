"""

A single chunk that matches no stock used to raise UnmatchedChunkError and waste
the whole long-video run (e.g. chunk 146 of a 152-chunk black-mamba video, after
978 LLM calls). The fallback guarantees a clip via two levels:
  A. theme-environment empty shot from motif_pool (lenient motif), then
  B. reuse the nearest real shot already downloaded this run.
UnmatchedChunkError is only reachable when the WHOLE video produced no footage.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import backend.services.orchestrator as orch_mod
from backend.services.orchestrator import ChunkOutcome, Orchestrator


def _make_orch():
    from backend.lib.pipeline_mode import PipelineMode
    from backend.services.shot_router import ShotRouter

    return Orchestrator(
        llm=MagicMock(),
        footage=MagicMock(),
        critic=MagicMock(),
        video_gen=MagicMock(),
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(),
        generated_cache=MagicMock(),
        mode=PipelineMode.FAST,
    )


def _used(idx: int) -> ChunkOutcome:
    return ChunkOutcome(
        chunk_index=idx, plan={"chunk_index": idx}, local_path=f"/tmp/c{idx}.mp4",
        duration=5.0, source="pexels", source_id=f"S{idx}",
        source_url="", resolution="1920x1080", score=8.0, decision="use",
        cost_usd=0.0,
    )


def _miss(idx: int) -> ChunkOutcome:
    return ChunkOutcome(
        chunk_index=idx, plan={"chunk_index": idx}, local_path=None,
        duration=0, source="", source_id="", source_url="", resolution="",
        score=0.0, decision="stock_miss", cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# _find_any_donor
# ---------------------------------------------------------------------------

def test_find_any_donor_skips_adjacent_and_prefers_nearest_safe_donor():
    orch = _make_orch()
    outcomes = [_used(0), _used(1), _miss(2), _miss(3), _used(4)]
    # idx=2: chunk 1 is adjacent and must be skipped; chunk 4 is safe.
    donor = orch._find_any_donor(outcomes, 2)
    assert donor is not None and donor.chunk_index == 4


def test_find_any_donor_scans_forward_when_only_later_succeeded():
    orch = _make_orch()
    outcomes = [_miss(0), _miss(1), _used(2), _used(3)]
    donor = orch._find_any_donor(outcomes, 0)
    assert donor is not None and donor.chunk_index == 2


def test_find_any_donor_accepts_inherited_donor():
    orch = _make_orch()
    inherited = _used(0)
    inherited.inherited_from_chunk = 5  # aggressive finder would skip this
    outcomes = [inherited, _miss(1), _miss(2), _used(3)]
    donor = orch._find_any_donor(outcomes, 2)
    assert donor is not None and donor.chunk_index == 0


def test_find_any_donor_returns_none_when_only_adjacent_footage():
    orch = _make_orch()
    outcomes = [_used(0), _miss(1), _used(2)]
    assert orch._find_any_donor(outcomes, 1) is None


def test_find_any_donor_allows_non_adjacent_reuse():
    orch = _make_orch()
    outcomes = [_used(0), _miss(1), _miss(2), _used(3)]
    donor = orch._find_any_donor(outcomes, 2)
    assert donor is not None and donor.chunk_index == 0


def test_find_any_donor_respects_global_repeat_budget():
    orch = _make_orch()
    dup_a = _used(0)
    dup_a.source_id = "DUP"
    dup_b = _used(4)
    dup_b.source_id = "DUP"
    fresh = _used(5)
    outcomes = [dup_a, _miss(1), _miss(2), _miss(3), dup_b, fresh]

    donor = orch._find_any_donor(outcomes, 2)

    assert donor is None


def test_find_any_donor_returns_none_when_no_footage():
    orch = _make_orch()
    outcomes = [_miss(0), _miss(1), _miss(2)]
    assert orch._find_any_donor(outcomes, 1) is None


# ---------------------------------------------------------------------------
# _v2_ship_fallback
# ---------------------------------------------------------------------------

def test_ship_fallback_level_a_theme_motif(monkeypatch, tmp_path):
    """Level A: lenient motif yields a theme clip → materialized via download."""
    orch = _make_orch()
    cand = SimpleNamespace(source="pexels", source_id="MOTIF1")
    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: cand)

    materialized = _used(99)
    materialized.source_id = "MOTIF1"
    captured = {}

    def fake_download(plan, c, chunk_dir, orientation, **kwargs):
        captured.update(kwargs)
        return materialized

    monkeypatch.setattr(orch, "_v2_download_outcome", fake_download)

    plan = {"chunk_index": 5}
    outcomes = [_used(0), _miss(5)]
    result = orch._v2_ship_fallback(
        plan, 5, tmp_path, "landscape", ["african savanna"],
        budget=MagicMock(), outcomes=outcomes,
        used_source_ids=set(), used_source_lock=None,
    )
    assert result is materialized
    assert result.inherit_reason == "ship_theme_motif"
    # must bypass dedup so a repeated theme clip can still ship
    assert captured.get("allow_reuse") is True


def test_ship_fallback_blocks_story_chunk_with_hard_anchors(monkeypatch, tmp_path):
    """Phase 1: story chunks with hard anchors fail closed instead of shipping wrong fallback."""
    orch = _make_orch()
    called = {"motif": False}

    def fake_motif(*args, **kwargs):
        called["motif"] = True

    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", fake_motif)
    plan = {
        "chunk_index": 5,
        "sentence_type": "fact",
        "visual_focus": "topic_subject",
        "visual_anchor": True,
        "must_show": ["glass", "cup"],
    }
    outcomes = [_used(0), _miss(5)]

    result = orch._v2_ship_fallback(
        plan, 5, tmp_path, "landscape", ["street vendor"],
        budget=MagicMock(), outcomes=outcomes,
        used_source_ids=set(), used_source_lock=None,
    )

    assert result is None
    assert called["motif"] is False
    metrics = plan["_v2_detection_metrics"]
    assert metrics["fallback_blocked"] is True
    assert metrics["selection_status"] == "needs_manual_asset"


def test_story_degraded_fallback_reuses_same_domain_drink_for_missing_syrup():
    orch = _make_orch()
    donor = _used(3)
    donor.source_tags = "Mumbai sugarcane juice vendor serving cups at street cart"
    donor.plan = {
        "chunk_index": 3,
        "sentence": "Fresh sugarcane juice pours into a cup.",
        "must_show": ["sugarcane", "cup"],
        "domain": "sugarcane_juice_vendor_in_mumbai",
    }
    plan = {
        "chunk_index": 1,
        "sentence": "A small pot of syrup sits beside the cart.",
        "must_show": ["kettle", "syrup or honey"],
        "domain": "sugarcane_juice_vendor_in_mumbai",
    }

    result = orch._v2_story_degraded_ship_fallback(plan, 1, [_miss(0), _miss(1), _miss(2), donor])

    assert result is not None
    assert result.used_level == "L_story_degraded_inherit"
    assert result.inherited_from_chunk == 3
    assert result.verify_scores["selection_status"] == "degraded_story_inherit"


def test_story_distinct_fallback_uses_fresh_asset_before_reuse(monkeypatch, tmp_path):
    from backend.services.footage_service import FootageCandidate

    orch = _make_orch()
    fresh = FootageCandidate(
        source="pexels",
        source_id="FRESH_JUICE",
        source_url="",
        query="sugarcane juice vendor",
        source_tags="Mumbai sugarcane juice vendor serving cups at street cart",
    )
    orch.footage.search_secondary.return_value = [fresh]

    def fake_download(plan, cand, chunk_dir, orientation, **kwargs):
        out = _used(9)
        out.source = cand.source
        out.source_id = cand.source_id
        out.source_tags = cand.source_tags
        out.used_level = kwargs["used_level"]
        return out

    monkeypatch.setattr(orch, "_v2_download_outcome", fake_download)
    plan = {
        "chunk_index": 1,
        "sentence": "A small pot of syrup sits beside the cart.",
        "must_show": ["kettle", "syrup or honey"],
        "domain": "sugarcane_juice_vendor_in_mumbai",
        "queries": ["syrup pot sugarcane juice cart"],
    }

    result = orch._v2_story_distinct_ship_fallback(
        plan, 1, tmp_path, "landscape", ["mumbai sugarcane juice vendor"],
        used_source_ids=set(), used_source_lock=None,
    )

    assert result is not None
    assert result.source_id == "FRESH_JUICE"
    assert result.used_level == "L_story_distinct_fallback"
    assert result.fallback_reason == "fresh_distinct_before_reuse"


def test_story_distinct_fallback_rejects_chestnut_tree_for_roasted_chestnut(monkeypatch, tmp_path):
    from backend.services.footage_service import FootageCandidate

    orch = _make_orch()
    tree = FootageCandidate(
        source="pexels",
        source_id="TREE",
        source_url="",
        query="turkish roasted chestnut cart",
        source_tags="Chestnut tree leaves and green forest foliage",
    )
    roasted = FootageCandidate(
        source="pexels",
        source_id="ROASTED",
        source_url="",
        query="turkish roasted chestnut cart",
        source_tags="Istanbul roasted chestnut street cart with hot pan and paper bags",
    )
    orch.footage.search_secondary.return_value = [tree, roasted]

    def fake_download(plan, cand, chunk_dir, orientation, **kwargs):
        out = _used(9)
        out.source = cand.source
        out.source_id = cand.source_id
        out.source_tags = cand.source_tags
        out.used_level = kwargs["used_level"]
        return out

    monkeypatch.setattr(orch, "_v2_download_outcome", fake_download)
    plan = {
        "chunk_index": 0,
        "sentence": "At dusk in Istanbul, a Turkish roasted chestnut cart serves the market street.",
        "must_show": ["chestnut"],
        "queries": ["turkish roasted chestnut cart"],
    }

    result = orch._v2_story_distinct_ship_fallback(
        plan, 0, tmp_path, "landscape", ["istanbul roasted chestnut stall"],
        used_source_ids=set(), used_source_lock=None,
    )

    assert result is not None
    assert result.source_id == "ROASTED"
    assert plan["_v2_detection_metrics"]["story_distinct_candidate_rejects"] == 1


def test_story_degraded_fallback_respects_global_repeat_budget():
    orch = _make_orch()
    donor_a = _used(0)
    donor_a.source_id = "JUICE"
    donor_a.source_tags = "Mumbai sugarcane juice vendor serving cups at street cart"
    donor_b = _used(3)
    donor_b.source_id = "JUICE"
    donor_b.source_tags = "Mumbai sugarcane juice vendor serving cups at street cart"
    donor_fresh = _used(4)
    donor_fresh.source_id = "COFFEE"
    donor_fresh.source_tags = "Mumbai sugarcane juice vendor serving cups at street cart"
    plan = {
        "chunk_index": 1,
        "sentence": "A small pot of syrup sits beside the cart.",
        "must_show": ["kettle", "syrup or honey"],
        "domain": "sugarcane_juice_vendor_in_mumbai",
    }

    result = orch._v2_story_degraded_ship_fallback(
        plan, 1, [donor_a, _miss(1), _miss(2), donor_b, donor_fresh]
    )

    assert result is None


def test_story_degraded_fallback_requires_core_subject_for_chestnut():
    orch = _make_orch()
    cash = _used(5)
    cash.source_tags = "Red 10 Turkish lira paper money banknote"
    cash.plan = {"chunk_index": 5, "sentence": "The vendor accepts Turkish lira cash.", "must_show": ["cash"]}
    chestnut = _used(4)
    chestnut.source_tags = "ISTANBUL TURKEY roasted chestnut cart with customers"
    chestnut.plan = {"chunk_index": 4, "sentence": "Tourists pause beside the warm chestnut stall.", "must_show": ["chestnut"]}
    plan = {
        "chunk_index": 0,
        "sentence": "At dusk in Istanbul, a Turkish roasted chestnut cart serves the market street.",
        "must_show": ["chestnut"],
    }

    result = orch._v2_story_degraded_ship_fallback(plan, 0, [_miss(0), _miss(1), cash, _miss(3), chestnut])

    assert result is not None
    assert result.inherited_from_chunk == 4
    assert result.source_id == "S4"


def test_story_degraded_fallback_rejects_wrong_currency_for_cash():
    from backend.services.orchestrator import Orchestrator

    dollar = _used(2)
    dollar.source_tags = "Hands counting United States dollar bills cash"

    assert Orchestrator._v2_story_degraded_donor_score(
        {"sentence": "The owner accepts cash in Vietnamese dong.", "must_show": ["cash"]},
        dollar,
    ) == 0


def test_ship_fallback_level_b_inherit_when_motif_empty(monkeypatch, tmp_path):
    """Level B: lenient motif finds nothing → reuse nearest real shot."""
    orch = _make_orch()
    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: None)

    plan = {"chunk_index": 2}
    outcomes = [_used(0), _used(1), _miss(2)]
    result = orch._v2_ship_fallback(
        plan, 2, tmp_path, "landscape", ["savanna"],
        budget=MagicMock(), outcomes=outcomes,
        used_source_ids=set(), used_source_lock=None,
    )
    assert result is not None
    assert result.decision == "use"
    assert result.local_path == "/tmp/c0.mp4"  # skip adjacent chunk 1
    assert result.inherited_from_chunk == 0
    assert result.used_level == "L_ship_inherit"
    assert result.inherit_reason == "ship_inherit_prev"
    assert result.inherit_donor_distance == 2


def test_ship_fallback_returns_none_when_no_footage_at_all(monkeypatch, tmp_path):
    """Theoretical floor: no motif clip AND no successful chunk anywhere."""
    orch = _make_orch()
    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: None)

    plan = {"chunk_index": 0}
    outcomes = [_miss(0), _miss(1)]
    result = orch._v2_ship_fallback(
        plan, 0, tmp_path, "landscape", [],
        budget=MagicMock(), outcomes=outcomes,
        used_source_ids=set(), used_source_lock=None,
    )
    assert result is None


def test_ship_fallback_motif_download_failure_falls_through_to_inherit(monkeypatch, tmp_path):
    """If the lenient motif clip fails to download, level B still ships."""
    orch = _make_orch()
    cand = SimpleNamespace(source="pexels", source_id="MOTIF1")
    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: cand)
    monkeypatch.setattr(orch, "_v2_download_outcome", lambda *a, **k: None)

    plan = {"chunk_index": 3}
    outcomes = [_used(0), _used(1), _used(2), _miss(3)]
    result = orch._v2_ship_fallback(
        plan, 3, tmp_path, "landscape", ["savanna"],
        budget=MagicMock(), outcomes=outcomes,
        used_source_ids=set(), used_source_lock=None,
    )
    assert result is not None
    assert result.inherited_from_chunk == 1  # skip adjacent chunk 2
    assert result.used_level == "L_ship_inherit"


def test_ship_fallback_motif_respects_duplicate_asset_cap(monkeypatch, tmp_path):
    orch = _make_orch()
    cand = SimpleNamespace(source="pexels", source_id="MOTIF1")
    donor_a = _used(0)
    donor_a.source_id = "MOTIF1"
    donor_b = _used(3)
    donor_b.source_id = "MOTIF1"
    download = MagicMock(return_value=_used(99))

    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: cand)
    monkeypatch.setattr(orch, "_v2_download_outcome", download)
    monkeypatch.setattr(orch, "_short_ship_remote_fallback", lambda *a, **k: None)

    plan = {"chunk_index": 1}
    result = orch._v2_ship_fallback(
        plan, 1, tmp_path, "landscape", ["street market"],
        budget=MagicMock(), outcomes=[donor_a, _miss(1), _miss(2), donor_b],
        used_source_ids={"MOTIF1", "pexels:MOTIF1"}, used_source_lock=None,
    )

    assert result is None
    download.assert_not_called()
    assert plan["_v2_detection_metrics"]["duplicate_asset_cap_skips"] == 1


def test_ship_fallback_motif_respects_global_repeat_budget(monkeypatch, tmp_path):
    orch = _make_orch()
    cand = SimpleNamespace(source="pexels", source_id="MOTIF1")
    dup_a = _used(0)
    dup_a.source_id = "DUP"
    dup_b = _used(2)
    dup_b.source_id = "DUP"
    motif_donor = _used(3)
    motif_donor.source_id = "MOTIF1"
    download = MagicMock(return_value=_used(99))

    monkeypatch.setattr(orch_mod, "probe_motif_candidate_lenient", lambda *a, **k: cand)
    monkeypatch.setattr(orch, "_v2_download_outcome", download)
    monkeypatch.setattr(orch, "_short_ship_remote_fallback", lambda *a, **k: None)

    plan = {"chunk_index": 1}
    result = orch._v2_ship_fallback(
        plan, 1, tmp_path, "landscape", ["street market"],
        budget=MagicMock(), outcomes=[dup_a, _miss(1), dup_b, motif_donor],
        used_source_ids={"MOTIF1", "pexels:MOTIF1"}, used_source_lock=None,
    )

    assert result is None
    download.assert_not_called()
    assert plan["_v2_detection_metrics"]["duplicate_asset_cap_skips"] == 1
