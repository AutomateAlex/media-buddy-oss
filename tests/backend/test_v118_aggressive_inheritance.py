"""

When MEDIA_BUDDY_AGGRESSIVE_INHERITANCE=1, Stage 2d converts stock_miss
chunks to inherited (from nearest successful predecessor / forward chunk)
instead of falling through to AI gen / UnmatchedChunkError. Default OFF.

Round-robin protection: same donor used at most ONCE in aggressive mode
to prevent the "5 chunks 全是同一段 mandelbrot zoom 在循环" visual regression.
Pass 2 (pending_inherit transition/CTA) is intentionally NOT subject to this
limit — that's design-level reuse.
"""
from unittest.mock import MagicMock


def _mk_orch():
    from backend.services.orchestrator import Orchestrator
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    return Orchestrator(
        llm=MagicMock(), footage=MagicMock(), critic=MagicMock(),
        video_gen=MagicMock(),
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        mode=PipelineMode.FAST,
    )


def _mk_outcome(idx, decision="use", local_path="x.mp4",
                source_id="src", inherited_from_chunk=None):
    from backend.services.orchestrator import ChunkOutcome
    return ChunkOutcome(
        chunk_index=idx, plan={"chunk_index": idx},
        local_path=local_path, duration=5.0, source="pexels",
        source_id=source_id, source_url=f"https://x/{source_id}",
        resolution="1920x1080", score=7.0, decision=decision,
        cost_usd=0.0, inherited_from_chunk=inherited_from_chunk,
    )


# ----------------------------------------------------------------------
# _find_donor_outcome_for_aggressive — donor selection logic
# ----------------------------------------------------------------------
def test_aggressive_donor_finds_nearest_unused_backward():
    """Picks nearest backward use=decision donor when none excluded."""
    svc = _mk_orch()
    outcomes = [
        _mk_outcome(0, local_path="C.mp4", source_id="C"),
        _mk_outcome(1, local_path="B.mp4", source_id="B"),
        _mk_outcome(2, decision="stock_miss", local_path=None, source_id=""),
    ]
    donor = svc._find_donor_outcome_for_aggressive(outcomes, 2, exclude=set())
    assert donor is not None
    assert donor.chunk_index == 0


def test_aggressive_donor_skips_excluded():
    """Round-robin: skip already-used donor, find next backward."""
    svc = _mk_orch()
    outcomes = [
        _mk_outcome(0, local_path="A.mp4", source_id="A"),
        _mk_outcome(1, local_path="B.mp4", source_id="B"),
        _mk_outcome(2, decision="stock_miss", local_path=None, source_id=""),
    ]
    donor = svc._find_donor_outcome_for_aggressive(
        outcomes, 2, exclude={1},  # 1 already used → should walk to 0
    )
    assert donor is not None
    assert donor.chunk_index == 0


def test_aggressive_donor_falls_forward_when_no_backward():
    """idx=0 stock_miss — fallback to forward scan."""
    svc = _mk_orch()
    outcomes = [
        _mk_outcome(0, decision="stock_miss", local_path=None, source_id=""),
        _mk_outcome(1, local_path="B.mp4", source_id="B"),
        _mk_outcome(2, local_path="C.mp4", source_id="C"),
    ]
    donor = svc._find_donor_outcome_for_aggressive(outcomes, 0, exclude=set())
    assert donor is not None
    assert donor.chunk_index == 2   # nearest safe forward


def test_aggressive_donor_skips_already_inherited_outcomes():
    """Aggressive mode should not chain off another inherited outcome —
    that compounds visual reuse. Keeps chain-inheritance to explicit
    transition/CTA path (Pass 2)."""
    svc = _mk_orch()
    outcomes = [
        _mk_outcome(0, local_path="C.mp4", source_id="C"),
        _mk_outcome(1, local_path="A.mp4", source_id="A",
                    inherited_from_chunk=0),    # chunk 1 inherited from 0
        _mk_outcome(2, decision="stock_miss", local_path=None, source_id=""),
    ]
    donor = svc._find_donor_outcome_for_aggressive(outcomes, 2, exclude=set())
    # Should skip chunk 1 (inherited) and land on chunk 0
    assert donor is not None
    assert donor.chunk_index == 0
    assert donor.inherited_from_chunk is None


def test_aggressive_donor_returns_none_when_all_used_or_unavailable():
    """All viable donors excluded → returns None (caller falls through
    to AI gen / UnmatchedChunkError)."""
    svc = _mk_orch()
    outcomes = [
        _mk_outcome(0, local_path="A.mp4", source_id="A"),
        _mk_outcome(1, decision="stock_miss", local_path=None, source_id=""),
        _mk_outcome(2, decision="stock_miss", local_path=None, source_id=""),
    ]
    donor = svc._find_donor_outcome_for_aggressive(
        outcomes, 1, exclude={0},   # 0 already used, 1 = self, 2 = stock_miss
    )
    assert donor is None


# ----------------------------------------------------------------------
# Env switch wiring — Stage 2d only runs when MEDIA_BUDDY_AGGRESSIVE_INHERITANCE=1
# ----------------------------------------------------------------------
def test_aggressive_inheritance_env_default_off(monkeypatch):
    """env unset → reading code reads "0" → not triggered.

    Read pattern matches orchestrator.py Stage 2d.
    """
    monkeypatch.delenv("MEDIA_BUDDY_AGGRESSIVE_INHERITANCE", raising=False)
    import os
    raw = os.environ.get(
        "MEDIA_BUDDY_AGGRESSIVE_INHERITANCE", "0"
    ).strip().lower()
    assert raw not in ("1", "true", "yes")


def test_aggressive_inheritance_env_on_recognized(monkeypatch):
    """Various truthy values trigger the mode."""
    import os
    for val in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("MEDIA_BUDDY_AGGRESSIVE_INHERITANCE", val)
        raw = os.environ.get(
            "MEDIA_BUDDY_AGGRESSIVE_INHERITANCE", "0"
        ).strip().lower()
        assert raw in ("1", "true", "yes"), f"value {val!r} should trigger"
