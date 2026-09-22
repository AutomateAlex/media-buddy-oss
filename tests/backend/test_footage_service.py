"""Tests for multi-source FootageService."""
from unittest.mock import MagicMock
from pathlib import Path
import tempfile


def _candidate(source_id="123", duration=5.0, width=1920, height=1080,
               source_url="https://example.com/v/123"):
    c = MagicMock()
    c.source_id = source_id
    c.duration = duration
    c.width = width
    c.height = height
    c.source_url = source_url
    return c


def _mock_source(name: str, *, available=True, search_results=None,
                 download_path: Path | None = None,
                 download_raises: Exception | None = None):
    src = MagicMock()
    src.name = name
    src.is_available.return_value = available
    src.search.return_value = search_results or []
    if download_raises:
        src.download.side_effect = download_raises
    elif download_path is not None:
        src.download.return_value = download_path
    else:
        src.download.side_effect = lambda c, p: Path(str(p))
    return src


def _mock_library(*clips):
    lib = MagicMock()
    lib.search.return_value = list(clips)
    return lib


def _library_clip(clip_id="lib_1", local_path="C:/tmp/lib.mp4"):
    clip = MagicMock()
    clip.id = clip_id
    clip.source_url = ""
    clip.thumbnail_path = ""
    clip.duration_seconds = 10
    clip.width = 1080
    clip.height = 1920
    clip.tags = ["local", "cached"]
    clip.local_path = local_path
    return clip


def _service_with_sources(
    *sources,
    source_waves: list[list[str]] | None = None,
    max_clip_duration: float = 60.0,
    preferred_clip_duration: float = 30.0,
    library=None,
) -> "FootageService":  # noqa: F821
    from backend.services.footage_service import FootageService
    svc = FootageService.__new__(FootageService)
    svc.sources = list(sources)
    svc._failure_counts = {}
    svc._benched = set()
    svc.library = library
    # Phase 2.11e — wave + duration config (legacy single-wave by default
    # so existing tests behave as before).
    svc._source_waves = source_waves
    svc._max_clip_duration = float(max_clip_duration)
    svc._preferred_clip_duration = float(preferred_clip_duration)
    # Provide the SearchFilters factory used inside fetch
    from backend.lib.stock_sources.base import SearchFilters
    svc.SearchFilters = SearchFilters
    return svc


def test_fetch_uses_top_priority_source_pexels_first():
    pexels = _mock_source("pexels", search_results=[_candidate("PX_1")])
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB_1")])
    coverr = _mock_source("coverr", search_results=[_candidate("CV_1")])
    svc = _service_with_sources(pexels, pixabay, coverr)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["beach sunset"], Path(tmp))

    assert len(results) == 1
    r = results[0]
    assert r.source == "pexels"
    assert r.source_id == "PX_1"
    assert r.error is None
    pexels.download.assert_called_once()
    pixabay.download.assert_not_called()
    coverr.download.assert_not_called()


def test_fetch_falls_back_to_pixabay_when_pexels_empty():
    pexels = _mock_source("pexels", search_results=[])
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB_2")])
    coverr = _mock_source("coverr", search_results=[_candidate("CV_2")])
    svc = _service_with_sources(pexels, pixabay, coverr)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["niche query"], Path(tmp))

    assert results[0].source == "pixabay_video"
    assert results[0].source_id == "PB_2"
    pixabay.download.assert_called_once()
    coverr.download.assert_not_called()


def test_fetch_returns_zero_candidates_error_when_all_sources_empty():
    pexels = _mock_source("pexels", search_results=[])
    pixabay = _mock_source("pixabay_video", search_results=[])
    coverr = _mock_source("coverr", search_results=[])
    svc = _service_with_sources(pexels, pixabay, coverr)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["美甲店招牌特写"], Path(tmp))

    assert results[0].local_path is None
    assert "0 candidates" in results[0].error


def test_fetch_handles_one_source_crashing():
    pexels = _mock_source("pexels")
    pexels.search.side_effect = RuntimeError("rate limited")
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB_3")])
    svc = _service_with_sources(pexels, pixabay)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["test"], Path(tmp))

    # Pexels crash should not poison the run; pixabay still serves the clip
    assert results[0].source == "pixabay_video"
    assert results[0].error is None


def test_fetch_handles_download_failure_gracefully():
    pexels = _mock_source(
        "pexels",
        search_results=[_candidate("PX_5")],
        download_raises=Exception("Network error"),
    )
    svc = _service_with_sources(pexels)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["test"], Path(tmp))

    assert results[0].local_path is None
    assert "Download failed" in results[0].error


def test_persistently_failing_source_gets_benched():
    """A source that throws on every search should be benched after the budget,
    and not be searched again in subsequent queries within the same run."""
    pexels = _mock_source("pexels")
    pexels.search.side_effect = RuntimeError("401 Unauthorized")
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB")])
    svc = _service_with_sources(pexels, pixabay)

    with tempfile.TemporaryDirectory() as tmp:
        # 3 queries; pexels should crash on first 2 then be benched.
        results = svc.fetch_for_scenes(["q1", "q2", "q3"], Path(tmp))

    assert all(r.error is None for r in results)
    assert all(r.source == "pixabay_video" for r in results)
    # 2 strikes, then benched — not called on the 3rd query
    assert pexels.search.call_count == 2
    assert "pexels" in svc._benched


def test_exclude_sources_skips_named_sources():
    """exclude_sources={'pexels'} should not call pexels.search at all."""
    pexels = _mock_source("pexels", search_results=[_candidate("PX_x")])
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB_x")])
    svc = _service_with_sources(pexels, pixabay)

    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(
            ["q"], Path(tmp), exclude_sources={"pexels"},
        )

    assert results[0].source == "pixabay_video"
    pexels.search.assert_not_called()
    pixabay.search.assert_called_once()


def test_exclude_all_sources_returns_empty_with_clear_error():
    pexels = _mock_source("pexels")
    svc = _service_with_sources(pexels)
    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["q"], Path(tmp), exclude_sources={"pexels"})
    assert results[0].local_path is None
    assert "excluded" in results[0].error


def test_is_available_reflects_source_count():
    svc = _service_with_sources()
    assert svc.is_available() is False

    svc = _service_with_sources(_mock_source("pexels"))
    assert svc.is_available() is True


# ---------------------------------------------------------------------------
# Phase 2.11e — wave-based search + duration filter
# ---------------------------------------------------------------------------

def test_wave_search_stops_at_wave_1_when_results_found():
    """Wave 1 (pexels+pixabay) gets a hit → wave 2 (wikimedia+archive) never runs."""
    pexels = _mock_source("pexels", search_results=[_candidate("PX_a", duration=10)])
    pixabay = _mock_source("pixabay_video", search_results=[])
    wikimedia = _mock_source("wikimedia", search_results=[_candidate("WK_a", duration=12)])
    archive = _mock_source("archive_org", search_results=[_candidate("AR_a", duration=14)])
    svc = _service_with_sources(
        pexels, pixabay, wikimedia, archive,
        source_waves=[
            ["pexels", "pixabay_video"],
            ["wikimedia", "archive_org"],
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["q"], Path(tmp))

    assert results[0].source == "pexels"
    pexels.search.assert_called_once()
    pixabay.search.assert_called_once()
    # Wave 2 never fires because wave 1 had a hit
    wikimedia.search.assert_not_called()
    archive.search.assert_not_called()


def test_wave_search_advances_to_wave_2_when_wave_1_empty():
    """Wave 1 empty → wave 2 runs; pick best from wave 2."""
    pexels = _mock_source("pexels", search_results=[])
    pixabay = _mock_source("pixabay_video", search_results=[])
    wikimedia = _mock_source("wikimedia", search_results=[_candidate("WK_b", duration=15)])
    archive = _mock_source("archive_org", search_results=[_candidate("AR_b", duration=20)])
    svc = _service_with_sources(
        pexels, pixabay, wikimedia, archive,
        source_waves=[
            ["pexels", "pixabay_video"],
            ["wikimedia", "archive_org"],
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["niche"], Path(tmp))

    # Wikimedia outranks Archive in SOURCE_PRIORITY → it wins
    assert results[0].source == "wikimedia"
    pexels.search.assert_called_once()
    pixabay.search.assert_called_once()
    wikimedia.search.assert_called_once()
    archive.search.assert_called_once()


def test_duration_filter_drops_clips_longer_than_max():
    """A 90s clip is dropped; a 25s clip is kept."""
    pexels = _mock_source(
        "pexels",
        search_results=[
            _candidate("LONG", duration=90.0),     # > 60s, drop
            _candidate("SHORT", duration=25.0),    # ≤ 60s, keep
        ],
    )
    svc = _service_with_sources(pexels, max_clip_duration=60.0)
    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["q"], Path(tmp))

    assert results[0].source == "pexels"
    assert results[0].source_id == "SHORT"


def test_duration_filter_keeps_unknown_duration():
    """Candidates with duration=0 / None pass through (we can't filter unknowns)."""
    pexels = _mock_source(
        "pexels",
        search_results=[_candidate("UNKNOWN", duration=0.0)],
    )
    svc = _service_with_sources(pexels, max_clip_duration=60.0)
    with tempfile.TemporaryDirectory() as tmp:
        results = svc.fetch_for_scenes(["q"], Path(tmp))

    assert results[0].source_id == "UNKNOWN"


def test_search_top_n_runs_waves_until_n_satisfied():
    """search_top_n stops advancing waves once total candidates ≥ n."""
    from backend.services.footage_service import FootageService
    pexels = _mock_source(
        "pexels",
        search_results=[
            _candidate(f"PX_{i}", duration=10.0) for i in range(5)
        ],
    )
    pixabay = _mock_source(
        "pixabay_video",
        search_results=[
            _candidate(f"PB_{i}", duration=12.0) for i in range(5)
        ],
    )
    wikimedia = _mock_source(
        "wikimedia",
        search_results=[_candidate("WK", duration=20.0)],
    )
    svc = _service_with_sources(
        pexels, pixabay, wikimedia,
        source_waves=[
            ["pexels", "pixabay_video"],
            ["wikimedia", "archive_org"],
        ],
    )
    cands = svc.search_top_n("q", n=8, orientation="landscape")

    # Wave 1 returns 10 → ≥ 8 → wave 2 skipped
    assert len(cands) == 8
    pexels.search.assert_called_once()
    pixabay.search.assert_called_once()
    wikimedia.search.assert_not_called()


def test_compute_waves_appends_leftover_unconfigured_sources():
    """A source not in any wave gets appended as a final wave (never shut out)."""
    a = _mock_source("a")
    b = _mock_source("b")
    z = _mock_source("z_unconfigured")
    svc = _service_with_sources(
        a, b, z,
        source_waves=[["a"], ["b"]],
    )
    waves = svc._compute_waves(svc.sources)
    names_per_wave = [[s.name for s in w] for w in waves]
    assert names_per_wave == [["a"], ["b"], ["z_unconfigured"]]


def test_compute_waves_falls_back_to_single_wave_when_no_config():
    """No source_waves config → all sources lumped into one wave (legacy)."""
    a = _mock_source("a")
    b = _mock_source("b")
    svc = _service_with_sources(a, b, source_waves=None)
    waves = svc._compute_waves(svc.sources)
    assert len(waves) == 1
    assert [s.name for s in waves[0]] == ["a", "b"]






def test_default_pipeline_waves_do_not_include_public_archive_sources():
    """Production source waves are limited to Pexels/Pixabay/Coverr."""
    from backend.lib.pipeline_mode import PipelineMode, get_mode_config

    waves = get_mode_config(PipelineMode.FAST)["source_waves"]
    flattened = {name for wave in waves for name in wave}

    assert waves == [["pexels", "pixabay_video", "coverr"]]
    assert flattened == {"pexels", "pixabay_video", "coverr"}
    assert not (
        flattened
        & {"wikimedia", "archive_org", "nasa", "loc", "nara", "pond5_pd"}
    )


def test_search_secondary_only_uses_allowed_production_secondary_sources():
    pexels = _mock_source("pexels", search_results=[_candidate("PX")])
    pixabay = _mock_source("pixabay_video", search_results=[_candidate("PB")])
    coverr = _mock_source("coverr", search_results=[_candidate("CV")])
    wikimedia = _mock_source("wikimedia", search_results=[_candidate("WK")])
    archive = _mock_source("archive_org", search_results=[_candidate("AR")])
    nasa = _mock_source("nasa", search_results=[_candidate("NASA")])
    loc = _mock_source("loc", search_results=[_candidate("LOC")])
    svc = _service_with_sources(
        pexels, pixabay, coverr, wikimedia, archive, nasa, loc,
    )

    cands = svc.search_secondary("wolf forest", "landscape", limit=3)

    # 片源策略:二级 = pexels / pixabay / coverr(都免费商用·免署名)。
    # wikimedia / archive_org / nasa / loc 不在允许的二级源里。
    assert {c.source for c in cands} <= {"pexels", "pixabay_video", "coverr"}
    pexels.search.assert_called_once()
    pixabay.search.assert_called_once()
    coverr.search.assert_called_once()
    wikimedia.search.assert_not_called()
    archive.search.assert_not_called()
    nasa.search.assert_not_called()
    loc.search.assert_not_called()


def test_footage_service_default_active_sources_are_production_allowlist(monkeypatch):
    from backend.services.footage_service import FootageService

    monkeypatch.setattr(
        FootageService,
        "_safe_is_available",
        classmethod(lambda cls, source: True),
    )
    monkeypatch.delenv("MEDIA_BUDDY_SKIP_SOURCES", raising=False)

    svc = FootageService(library=None, source_waves=[["pexels"]])

    # no primary (licensed) stock adapter ships in this build → keyed free sources only
    assert set(svc.source_names) == {"pexels", "pixabay_video", "coverr"}
    assert not (
        set(svc.source_names)
        & {"wikimedia", "archive_org", "nasa", "loc", "nara", "pond5_pd"}
    )
