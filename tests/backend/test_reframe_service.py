"""Phase 2.11 — Reframe service tests (letterbox MVP)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_video(tmp_path):
    """Make a real 2-second 1280x720 mp4 via ffmpeg so reframe has something
    to chew on. If ffmpeg is unavailable, skip — these tests need it."""
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH — reframe tests require it")
    out = tmp_path / "src.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1280x720:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "2", str(out)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not out.exists():
        pytest.skip(f"ffmpeg failed to make fixture: {r.stderr[-200:]}")
    return out


def test_reframe_landscape_to_portrait_blur_pad_default(tmp_video, tmp_path):
    """Default strategy is blur_pad (TikTok-style polished look)."""
    from backend.services.reframe_service import ReframeService
    out = ReframeService().to_aspect(
        tmp_video, target_aspect="9:16", out_dir=tmp_path / "out",
    )
    assert out is not None
    assert Path(out).exists()
    # Verify output is 1080x1920
    import json, subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(out)],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(r.stdout)
    assert data["streams"][0]["width"] == 1080
    assert data["streams"][0]["height"] == 1920


def test_reframe_letterbox_strategy_still_works(tmp_video, tmp_path):
    """letterbox strategy preserved for users who want flat black bars."""
    from backend.services.reframe_service import ReframeService
    out = ReframeService().to_aspect(
        tmp_video, target_aspect="9:16", out_dir=tmp_path / "out",
        strategy="letterbox",
    )
    assert out is not None
    assert Path(out).exists()
    assert Path(out) != Path(tmp_video)
    assert "letterbox" in Path(out).name


def test_reframe_unknown_strategy_falls_back_to_blur_pad(tmp_video, tmp_path):
    from backend.services.reframe_service import ReframeService
    out = ReframeService().to_aspect(
        tmp_video, target_aspect="9:16", out_dir=tmp_path / "out",
        strategy="not_a_real_strategy",
    )
    assert out is not None
    # Should have used blur_pad (the default fallback)
    assert "blur_pad" in Path(out).name


def test_reframe_already_target_aspect_is_noop(tmp_path):
    """Source already 9:16 → returns the original path, no transcode."""
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    src = tmp_path / "vert.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", str(src)],
        capture_output=True, timeout=20,
    )
    if not src.exists():
        pytest.skip("ffmpeg failed to make 9:16 fixture")
    from backend.services.reframe_service import ReframeService
    out = ReframeService().to_aspect(src, target_aspect="9:16")
    # Returns original path (same file) since already correct aspect
    assert out is not None
    assert Path(out).resolve() == src.resolve()


def test_reframe_missing_source_returns_none(tmp_path):
    from backend.services.reframe_service import ReframeService
    assert ReframeService().to_aspect(tmp_path / "nope.mp4", "9:16") is None


def test_reframe_unknown_aspect_returns_none(tmp_video):
    from backend.services.reframe_service import ReframeService
    assert ReframeService().to_aspect(tmp_video, target_aspect="42:1") is None


def test_aspect_label_helper():
    from backend.services.reframe_service import _aspect_label
    assert _aspect_label(1920, 1080) == "16:9"
    assert _aspect_label(1080, 1920) == "9:16"
    assert _aspect_label(1080, 1080) == "1:1"
    assert _aspect_label(0, 0) == ""


def test_orientation_to_aspect_helper():
    from backend.services.reframe_service import _orientation_to_aspect
    assert _orientation_to_aspect("portrait") == "9:16"
    assert _orientation_to_aspect("landscape") == "16:9"
    assert _orientation_to_aspect("square") == "1:1"
    assert _orientation_to_aspect("anything_else") is None


def test_orchestrator_reframe_invoked_when_portrait():
    """When orientation='portrait', the orchestrator's _reframe_if_needed
    should call ReframeService.to_aspect with target_aspect='9:16'."""
    from backend.services.orchestrator import Orchestrator
    from unittest.mock import MagicMock

    orch = Orchestrator.__new__(Orchestrator)
    orch.reframer = MagicMock()
    orch.reframer.to_aspect.return_value = Path("/tmp/reframed.mp4")

    out = orch._reframe_if_needed(Path("/tmp/in.mp4"), "portrait")
    orch.reframer.to_aspect.assert_called_once()
    args, kwargs = orch.reframer.to_aspect.call_args
    assert kwargs.get("target_aspect") == "9:16"


def test_orchestrator_reframe_skipped_for_landscape():
    """Landscape target → no reframe call (saves CPU)."""
    from backend.services.orchestrator import Orchestrator
    from unittest.mock import MagicMock

    orch = Orchestrator.__new__(Orchestrator)
    orch.reframer = MagicMock()

    out = orch._reframe_if_needed(Path("/tmp/in.mp4"), "landscape")
    orch.reframer.to_aspect.assert_not_called()
    assert out == Path("/tmp/in.mp4")


def test_footage_service_drops_orientation_filter_for_portrait():
    """For portrait/square targets, FootageService no longer hard-filters
    """
    from backend.services.footage_service import FootageService

    # Construct without going through full __init__ (avoids needing OM)
    fs = FootageService.__new__(FootageService)
    fs.library = None
    fs.sources = []
    fs._benched = set()
    fs._failure_counts = {}
    # Phase 2.11e — wave / duration attrs (None waves = legacy single-wave)
    fs._source_waves = None
    fs._max_clip_duration = 60.0
    fs._preferred_clip_duration = 30.0

    # Stub a SearchFilters factory so we can read what was constructed
    captured = []
    class FakeFilters:
        def __init__(self, **kw):
            captured.append(kw)
    fs.SearchFilters = FakeFilters

    # No sources → returns []; we just want to check filters captured
    fs.search_top_n("test", n=5, orientation="portrait")
    # Since sources is empty, filters never built. Add a fake source to trigger.
    fs.sources = [MagicMock(name="fake", is_available=lambda: True)]

    captured.clear()
    with patch.object(fs, "_safe_search", return_value=[]):
        fs.search_top_n("test", n=5, orientation="portrait")
    assert captured, "filters was never constructed"
    assert captured[0]["orientation"] is None  # portrait → relaxed to None

    captured.clear()
    with patch.object(fs, "_safe_search", return_value=[]):
        fs.search_top_n("test", n=5, orientation="landscape")
    assert captured[0]["orientation"] == "landscape"  # landscape → kept
