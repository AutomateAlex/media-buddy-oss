"""Unit tests for the post-render video health gate (verdict + colour flags)."""
import json

from backend.lib.video_health import evaluate_video_health, RED, ORANGE, YELLOW


def _obs(tmp_path, summary):
    (tmp_path / "observer_debug.json").write_text(
        json.dumps({"summary": summary}), encoding="utf-8"
    )


def _eval(tmp_path, **kw):
    base = dict(
        project_dir=tmp_path, output_duration=45.0, planned_seconds=45,
        script_issue=None, output_format="youtube_shorts",
    )
    base.update(kw)
    return evaluate_video_health(**base)


# ---------- 🔴 RED → hard_fail (auto-fail, no charge) ----------

def test_duration_below_abs_floor_is_hard(tmp_path):
    _obs(tmp_path, {})
    r = _eval(tmp_path, output_duration=3.8)  # < 5s absolute (the 4-second bug)
    assert r.verdict == "hard_fail" and r.is_hard
    assert any(f["type"] == "duration_too_short" and f["level"] == RED for f in r.flags)


def test_duration_below_half_planned_is_hard(tmp_path):
    _obs(tmp_path, {})
    r = _eval(tmp_path, output_duration=20.0, planned_seconds=45)  # 0.44 < 0.5
    assert r.verdict == "hard_fail"


def test_empty_script_is_hard(tmp_path):
    _obs(tmp_path, {})
    r = _eval(tmp_path, script_issue="empty_script")
    assert r.verdict == "hard_fail"
    assert any(f["type"] == "empty_script" and f["level"] == RED for f in r.flags)


def test_cta_only_script_is_hard(tmp_path):
    _obs(tmp_path, {})
    r = _eval(tmp_path, script_issue="cta_only_script")
    assert r.verdict == "hard_fail"


# ---------- 🟠 ORANGE / 🟡 YELLOW → soft_flag (deliver, but flag) ----------

def test_all_local_fallback_is_orange_soft(tmp_path):
    _obs(tmp_path, {"source_counts": {"local_fallback": 6}})
    r = _eval(tmp_path)
    assert r.verdict == "soft_flag" and not r.is_hard
    assert any(f["type"] == "all_local_fallback" and f["level"] == ORANGE for f in r.flags)


def test_adjacent_repeat_is_orange(tmp_path):
    _obs(tmp_path, {"source_counts": {"pexels": 5}, "adjacent_same_asset_count": 2})
    r = _eval(tmp_path)
    assert any(f["type"] == "adjacent_repeat" and f["level"] == ORANGE for f in r.flags)


def test_partial_fallback_is_yellow(tmp_path):
    _obs(tmp_path, {"source_counts": {"pexels": 6, "local_fallback": 4}})
    r = _eval(tmp_path)
    assert r.verdict == "soft_flag"
    assert any(f["type"] == "some_local_fallback" and f["level"] == YELLOW for f in r.flags)


def test_low_pass_rate_is_yellow(tmp_path):
    _obs(tmp_path, {"source_counts": {"pexels": 5}, "pass_rate_pct": 32.0})
    r = _eval(tmp_path)
    assert any(f["type"] == "low_pass_rate" and f["level"] == YELLOW for f in r.flags)


# ---------- 🟢 healthy ----------

def test_healthy_video_has_no_flags(tmp_path):
    _obs(tmp_path, {"source_counts": {"pexels": 4, "pexels": 2},
                    "pass_rate_pct": 80.0, "adjacent_same_asset_count": 0})
    r = _eval(tmp_path, output_duration=46.0)
    assert r.verdict == "healthy"
    assert r.flags == []


def test_long_video_uses_duration_not_short_script_check(tmp_path):
    # Long video → _short_script_quality_issue returns None upstream; rely on duration.
    _obs(tmp_path, {"source_counts": {"pexels": 30}, "pass_rate_pct": 75.0})
    r = _eval(tmp_path, output_duration=1500.0, planned_seconds=1800,
              output_format="youtube_landscape")  # 0.83 > 0.5
    assert r.verdict == "healthy"


def test_missing_observer_debug_is_tolerated(tmp_path):
    # No observer_debug.json on disk → footage signals skipped, no crash.
    r = _eval(tmp_path, output_duration=46.0)
    assert r.verdict == "healthy"
