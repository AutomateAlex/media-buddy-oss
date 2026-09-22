"""Tests for Orchestrator — Stock-first parallel pipeline."""
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def db_session():
    """Use the shared engine; unique project IDs per test."""
    from backend.database import Base, SessionLocal, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    from backend.models.project import Project
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    s = SessionLocal()
    s.add(Project(id=pid, name="t", mode="script", script_text="dummy"))
    s.commit()
    s._test_project_id = pid  # type: ignore[attr-defined]
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _default_progressive_off(monkeypatch):
    """Plan B (progressive waves) is the production default as of

    The legacy _stock_attempt tests in this file cover the non-progressive
    single-pass path, so pin the env OFF for every test by default. The
    progressive-wave tests re-enable it explicitly via _make_progressive_orch.
    """
    monkeypatch.setenv("MEDIA_BUDDY_PROGRESSIVE_WAVES", "0")


@pytest.fixture(autouse=True)
def _default_ai_gen_enabled_for_legacy_tests(monkeypatch):
    """
    orchestrator._ai_attempt now short-circuits unless
    MEDIA_BUDDY_ENABLE_AI_GEN=1 is set. The legacy tests in this file
    were written assuming AI gen is always tried, so re-enable it here
    to preserve their semantics. Production code default = sealed."""
    monkeypatch.setenv("MEDIA_BUDDY_ENABLE_AI_GEN", "1")


@pytest.fixture(autouse=True)
def _default_local_fallback_enabled_for_legacy_tests(monkeypatch):
    """
    acceptable content path by default — the orchestrator raises
    UnmatchedChunkError instead. Legacy tests in this file assert on
    the local_fallback outcome shape, so opt them in via env. Production
    code default = strict (no local_fallback as content).

    Tests that specifically validate the new strict behavior
    (UnmatchedChunkError) set this back to "0" or delete the env."""
    monkeypatch.setenv("MEDIA_BUDDY_LOCAL_FALLBACK_AS_CONTENT", "1")


def _make_orch(llm=None, footage=None, critic=None, video_gen=None,
               shot_router=None, mode=None):
    from backend.services.orchestrator import Orchestrator
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    return Orchestrator(
        llm=llm or MagicMock(),
        footage=footage or MagicMock(),
        critic=critic or MagicMock(),
        video_gen=video_gen or MagicMock(),
        shot_router=shot_router or ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(),
        generated_cache=MagicMock(),
        mode=mode or PipelineMode.FAST,
    )


def _make_candidate(source="pexels", source_id="X", duration=5.0):
    from backend.services.footage_service import FootageCandidate
    return FootageCandidate(
        source=source, source_id=source_id, source_url=f"https://x/{source_id}",
        duration=duration, width=1920, height=1080, query="x",
        _raw=MagicMock(),
    )






class TestPhase5StrictUnmatchedChunkError:
    """
    raise UnmatchedChunkError when chunk has zero real candidates.
    """


    def test_local_fallback_escape_hatch_still_works(
        self, db_session, tmp_path, monkeypatch,
    ):
        """MEDIA_BUDDY_LOCAL_FALLBACK_AS_CONTENT=1 (autouse fixture sets it)
        → orchestrator returns local_fallback outcome instead of raising.
        This is what every other test in this file depends on."""
        # Autouse fixture already set the env var
        from backend.services.orchestrator import Orchestrator
        from backend.services.shot_router import ShotRouter
        from backend.lib.pipeline_mode import PipelineMode

        llm = MagicMock()
        llm.plan_chunks.return_value = [{
            "chunk_index": 0, "sentence": "niche", "search_query": "x",
            "search_keywords": [], "visual_tags": [], "motion_tags": [],
            "shot_type": "atmosphere", "is_hero_shot": False,
            "priority": "normal", "target_shot_count": 1,
        }]
        footage = MagicMock()
        footage.source_names = []
        footage.num_active_waves.return_value = 1
        footage.search_top_n.return_value = []
        footage.search_secondary.return_value = []
        video_gen = MagicMock()
        video_gen.is_available.return_value = False

        svc = Orchestrator(
            llm=llm, footage=footage, critic=MagicMock(),
            video_gen=video_gen,
            shot_router=ShotRouter(mode=PipelineMode.FAST),
            asset_cache=MagicMock(), generated_cache=MagicMock(),
            mode=PipelineMode.FAST,
        )
        svc._basic_file_check = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]
        svc._build_harness_contracts = MagicMock()  # type: ignore[method-assign]

        # Should NOT raise — returns outcomes (legacy escape hatch)
        outcomes = svc.run(
            db_session, db_session._test_project_id,
            ["niche"], tmp_path,
        )
        assert len(outcomes) == 1


def _make_harness_contracts(chunk_index=0, with_gen_prompt=True):
    """Build minimal-but-valid VideoContract + ChunkContract for tests
    that exercise the Contract-Gate paths in Orchestrator._stock_attempt
    and _ai_attempt. We never call the real harness LLMs in unit tests;
    we just populate Orchestrator._harness_* state directly.
    """
    from backend.lib.harness.schemas import (
        ChunkContract, SearchIntent, VerificationTest, VideoContract,
    )
    intents = [
        SearchIntent(type="primary", query="primary stock query"),
    ]
    if with_gen_prompt:
        intents.append(SearchIntent(
            type="generation_prompt",
            query="minimal 3x3 grid geometric construction animation, "
                  "abstract math diagram style",
        ))
    cc = ChunkContract(
        chunk_id=chunk_index,
        sentence="test sentence",
        what_it_communicates="mathematical 3x3 grid subdivision",
        visual_must_convey=["3x3 grid"],
        visual_must_not_convey=["food", "kitchen"],
        likely_wrong_matches=[],
        search_intents=intents,
        verification_tests=[
            VerificationTest(
                test_id="T1",
                question="does this clip show 3x3 grid?",
                severity="required",
            ),
        ],
        pass_threshold=8.0,
        importance="normal",
    )
    vc = VideoContract(
        video_topic="fractals",
        content_type="science_explainer",
        narrative_goal="explain fractal subdivision",
        target_audience="general",
        tone=["curious", "educational"],
        visual_world=["abstract math diagrams"],
        wrong_visual_worlds_in_this_video=["food", "lifestyle"],
        style_anchor="3Blue1Brown minimal math animation",
        continuity_rules=[],
    )
    return vc, cc


def test_orchestrator_no_fanout(db_session, tmp_path):
    """Verify orchestrator never calls generate_clip_parallel — single
    provider per chunk is the contract."""
    llm = MagicMock()
    llm.plan_chunks.return_value = [{
        "chunk_index": 0, "sentence": "x", "search_query": "x",
        "visual_tags": [], "motion_tags": [],
        "shot_type": "atmosphere", "is_hero_shot": False,
        "priority": "normal", "target_shot_count": 1,
    }]
    footage = MagicMock()
    footage.source_names = []
    footage.search_top_n.return_value = []  # empty stock → AI
    critic = MagicMock()
    critic.mm_critique.return_value = {
        "score": 8.0, "decision": "use", "pass": True,
        "retry_needed": False, "issues": [],
    }
    video_gen = MagicMock()
    video_gen.is_available.return_value = True
    ai_out = tmp_path / "ai.mp4"
    ai_out.write_bytes(b"\x00")
    video_gen.generate_clip.return_value = ai_out

    gcache = MagicMock()
    gcache.get.return_value = None
    from backend.services.orchestrator import Orchestrator
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    svc = Orchestrator(
        llm=llm, footage=footage, critic=critic, video_gen=video_gen,
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(), generated_cache=gcache,
        mode=PipelineMode.FAST,
    )
    svc.run(db_session, db_session._test_project_id, ["x"], tmp_path)

    video_gen.generate_clip_parallel.assert_not_called()


def test_orchestrator_total_asset_miss_uses_local_fallback(
    db_session, tmp_path, monkeypatch,
):
    """If stock and cloud AI both miss, create a local fallback clip."""
    llm = MagicMock()
    llm.plan_chunks.return_value = [{
        "chunk_index": 0, "sentence": "x", "search_query": "x",
        "visual_tags": [], "motion_tags": [],
        "shot_type": "atmosphere", "is_hero_shot": False,
        "priority": "normal", "target_shot_count": 1,
    }]
    footage = MagicMock()
    footage.source_names = []
    footage.search_top_n.return_value = []
    critic = MagicMock()
    video_gen = MagicMock()
    video_gen.is_available.return_value = False

    from backend.services.orchestrator import Orchestrator
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode

    def fake_fallback(self, chunk_dir, idx, orientation, duration_seconds=20.0):
        path = Path(chunk_dir) / "local_fallback.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fallback")
        return path

    monkeypatch.setattr(
        Orchestrator, "_create_local_fallback_clip", fake_fallback,
    )
    svc = Orchestrator(
        llm=llm, footage=footage, critic=critic, video_gen=video_gen,
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(), generated_cache=MagicMock(),
        mode=PipelineMode.FAST,
    )
    outcomes = svc.run(db_session, db_session._test_project_id, ["x"], tmp_path)

    assert len(outcomes) == 1
    assert outcomes[0].source == "local_fallback"
    assert outcomes[0].decision == "fallback"
    assert Path(outcomes[0].local_path).exists()




# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Tests verify that _preflight_auth_check actually contacts the cloud
# (via get_quota) instead of just checking local refresh-token presence.
# conftest.py sets MEDIA_BUDDY_ALLOW_OFFLINE=1 by default, so each test
# below explicitly unsets it via monkeypatch to exercise the new logic.


def test_preflight_skipped_when_allow_offline(monkeypatch):
    """The existing escape hatch still works — no cloud roundtrip attempted."""
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "1")
    svc = _make_orch()
    # Should return without raising and without touching CloudAuth/CloudGateway.
    svc._preflight_auth_check()


def test_preflight_passes_with_valid_token(monkeypatch):
    """Valid cloud session → quota returned, pre-flight succeeds."""
    from unittest.mock import patch
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "0")
    svc = _make_orch()

    fake_auth = MagicMock()
    fake_auth.is_authenticated = True
    fake_gw = MagicMock()

    async def _quota():
        return {"used_eur": 10.5, "limit_eur": 25.0,
                "resets_at": "2026-06-14T21:05:39+00:00"}
    fake_gw.get_quota = _quota

    # _preflight_auth_check now goes through the process-wide singletons
    # introduced to fix the refresh-token rotation race. Patch the getters,
    # not the classes — constructing a fresh CloudAuth no longer happens.
    with patch("backend.lib.cloud_auth.get_default_auth", return_value=fake_auth), \
         patch("backend.lib.cloud_gateway.get_default_gateway", return_value=fake_gw):
        svc._preflight_auth_check()  # no raise


def test_preflight_fails_on_revoked_token(monkeypatch):
    """Cloud returned 401 (token revoked server-side) → RuntimeError with
    user-actionable 'please re-login' message. This is the recurring pain
    point #151 fixed: previously is_authenticated returned True for revoked
    refresh tokens, letting the user proceed through a doomed render."""
    from unittest.mock import patch
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "0")
    svc = _make_orch()

    fake_auth = MagicMock()
    fake_auth.is_authenticated = True
    fake_gw = MagicMock()

    from backend.lib.cloud_auth import NotAuthenticatedError

    async def _revoked():
        raise NotAuthenticatedError("provider rejected the key")
    fake_gw.get_quota = _revoked

    with patch("backend.lib.cloud_auth.get_default_auth", return_value=fake_auth), \
         patch("backend.lib.cloud_gateway.get_default_gateway", return_value=fake_gw):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
            svc._preflight_auth_check()


def test_preflight_fails_when_no_refresh_token(monkeypatch):
    """No refresh token in keychain → fail fast with the same login prompt."""
    from unittest.mock import patch
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "0")
    svc = _make_orch()

    fake_auth = MagicMock()
    fake_auth.is_authenticated = False  # no key at all

    with patch("backend.lib.cloud_auth.get_default_auth", return_value=fake_auth):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
            svc._preflight_auth_check()


def test_preflight_warns_on_quota_exhausted_but_proceeds(monkeypatch, caplog):
    """Quota at limit → log warning but don't block. Downstream LLM calls
    will fail-closed naturally; pre-flight only gates on auth state."""
    from unittest.mock import patch
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "0")
    svc = _make_orch()

    fake_auth = MagicMock()
    fake_auth.is_authenticated = True
    fake_gw = MagicMock()

    async def _exhausted():
        return {"used_eur": 25.47, "limit_eur": 25.0,
                "resets_at": "2026-06-14T21:05:39+00:00"}
    fake_gw.get_quota = _exhausted

    import logging
    with patch("backend.lib.cloud_auth.get_default_auth", return_value=fake_auth), \
         patch("backend.lib.cloud_gateway.get_default_gateway", return_value=fake_gw), \
         caplog.at_level(logging.WARNING, logger="backend.services.orchestrator"):
        svc._preflight_auth_check()  # no raise

    assert any("quota EXHAUSTED" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Tests for the env-gated progressive search: wave-by-wave, quality-aware
# stopping. Default is OFF, so these explicitly set the env. The legacy
# _stock_attempt tests above stay green because they never set this flag.


def _make_progressive_orch(monkeypatch, min_score: float = 5.0):
    """Helper: orchestrator with MEDIA_BUDDY_PROGRESSIVE_WAVES=1 and a
    given MIN harness score threshold."""
    monkeypatch.setenv("MEDIA_BUDDY_PROGRESSIVE_WAVES", "1")
    monkeypatch.setenv("MEDIA_BUDDY_MIN_HARNESS_SCORE", str(min_score))
    return _make_orch()


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Old bug: line 1790 had `or culture_match <= 2` unconditional, which fired
# for Universal-culture scripts where culture_match has no semantic meaning.
# Symptom: when omni quota_exceeded fell back to score=0, every AI clip
# got mislabelled `culture_hard_reject` regardless of script culture.





