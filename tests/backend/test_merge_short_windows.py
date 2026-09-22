"""Min-shot merge: coalesce sub-threshold on-screen windows into the previous
shot so brief sub-sentences don't flash a <1s clip (gated, default off)."""
from backend.services.pipeline_service import PipelineService

f = PipelineService._merge_short_windows


def test_passthrough_when_disabled():
    w = [(0.0, 2.0), (2.0, 4.0), (4.0, 4.5)]
    assert f(w, 0.0) == [(0, 0.0, 2.0), (1, 2.0, 4.0), (2, 4.0, 4.5)]


def test_merges_short_window_into_previous():
    w = [(0.0, 2.0), (2.0, 4.0), (4.0, 4.5)]  # last window = 0.5s
    assert f(w, 1.0) == [(0, 0.0, 2.0), (1, 2.0, 4.5)]


def test_protected_short_window_keeps_own_scene():
    w = [(0.0, 2.0), (2.0, 4.0), (4.0, 4.5)]
    assert f(w, 1.0, protected_scene_indices={2}) == [
        (0, 0.0, 2.0),
        (1, 2.0, 4.0),
        (2, 4.0, 4.5),
    ]


def test_short_window_after_protected_scene_does_not_extend_protected_visual():
    w = [(0.0, 3.0), (3.0, 4.0), (4.0, 5.0)]
    assert f(w, 2.0, protected_scene_indices={1}) == [
        (0, 0.0, 3.0),
        (1, 3.0, 4.0),
        (2, 4.0, 5.0),
    ]


def test_chain_of_shorts_collapses_into_anchor():
    w = [(0.0, 3.0), (3.0, 3.6), (3.6, 4.0), (4.0, 7.0)]
    assert f(w, 1.0) == [(0, 0.0, 4.0), (3, 4.0, 7.0)]


def test_short_first_window_kept():
    w = [(0.0, 0.5), (0.5, 3.0)]  # no previous to merge into
    assert f(w, 1.0) == [(0, 0.0, 0.5), (1, 0.5, 3.0)]


def test_empty():
    assert f([], 1.0) == []


def test_total_coverage_unchanged():
    w = [(0.0, 2.0), (2.0, 2.4), (2.4, 5.0)]
    merged = f(w, 1.0)
    assert merged[0][1] == 0.0 and merged[-1][2] == 5.0  # spans full timeline


def test_three_second_default_shape_for_one_minute_shorts():
    windows = [(float(i * 2), float((i + 1) * 2)) for i in range(30)]
    merged = f(windows, 0.0, max_shots=20)
    assert len(merged) == 20
    assert merged[0][1] == 0.0
    assert merged[-1][2] == 60.0
    assert max(end - start for _, start, end in merged) <= 4.0


def test_protected_scenes_disable_global_compression():
    windows = [(float(i * 2), float((i + 1) * 2)) for i in range(6)]
    merged = f(windows, 0.0, max_shots=3, protected_scene_indices={2})
    assert merged == [
        (0, 0.0, 2.0),
        (1, 2.0, 4.0),
        (2, 4.0, 6.0),
        (3, 6.0, 8.0),
        (4, 8.0, 10.0),
        (5, 10.0, 12.0),
    ]


def test_hard_visual_plan_is_merge_protected():
    assert PipelineService._fine_cut_scene_merge_protected({
        "must_show": ["kettle", "syrup or honey"],
    })
    assert not PipelineService._fine_cut_scene_merge_protected({
        "must_show": ["vendor"],
        "story_soft_fallback": True,
    })


def test_cash_or_queue_sentence_is_merge_protected_even_without_plan():
    assert PipelineService._fine_cut_scene_merge_protected(
        {}, "成本8卢比", {},
    )
    assert PipelineService._fine_cut_scene_merge_protected(
        {}, "顾客排队变长", {},
    )
