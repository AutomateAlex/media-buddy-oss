import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def db_session():
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


def _make_candidate(source_id="S1"):
    from backend.services.footage_service import FootageCandidate

    return FootageCandidate(
        source="pexels",
        source_id=source_id,
        source_url=f"https://stock.example/{source_id}",
        thumbnail_url="https://thumb",
        duration=5.0,
        width=1920,
        height=1080,
        query="leaf macro veins",
        source_tags="leaf macro veins branching pattern",
        _raw=MagicMock(),
    )


def _make_orch(footage=None, critic=None, video_gen=None):
    from backend.lib.pipeline_mode import PipelineMode
    from backend.services.orchestrator import Orchestrator
    from backend.services.shot_router import ShotRouter

    return Orchestrator(
        llm=MagicMock(),
        footage=footage or MagicMock(),
        critic=critic or MagicMock(),
        video_gen=video_gen or MagicMock(),
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(),
        generated_cache=MagicMock(),
        mode=PipelineMode.FAST,
    )


def _plan():
    return {
        "chunk_index": 0,
        "chunk_id": 0,
        "sentence": "Fractals appear in nature.",
        "text": "Fractals appear in nature.",
        "sentence_type": "fact",
        "visual_anchor": True,
        "intent": "show natural branching pattern",
        "must_show": ["branching pattern"],
        "must_not_show": ["cartoon"],
        "queries": ["leaf macro veins"],
        "metaphor_queries": ["river delta aerial"],
        "metaphor_essence": "repeating branching structure",
        "search_query": "leaf macro veins",
        "visual_tags": [],
        "motion_tags": [],
        "shot_type": "atmosphere",
        "is_hero_shot": True,
        "priority": "high",
        "target_shot_count": 1,
        "atoms": {},
        "cascade_depth": 2,
        "prompts": {},
    }


def test_v2_run_accepts_first_candidate_and_skips_old_cost_paths(
    db_session, tmp_path, monkeypatch,
):
    from backend.services.observer_judge import ObserverJudgeResult

    monkeypatch.setenv("MEDIA_BUDDY_PIPELINE_VERSION", "v2")
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "1")

    fake_clip = tmp_path / "clip.mp4"
    fake_clip.write_bytes(b"x" * 2048)

    footage = MagicMock()
    cand = _make_candidate("S1")
    footage.search_secondary.return_value = [cand]
    footage.download_candidate.return_value = fake_clip

    critic = MagicMock()
    video_gen = MagicMock()
    svc = _make_orch(footage=footage, critic=critic, video_gen=video_gen)
    monkeypatch.setattr(svc, "_basic_file_check", lambda path: (True, ""))
    monkeypatch.setattr(
        "backend.services.orchestrator.build_short_video_global_plan",
        lambda llm, sentences, user_brief="": {
            "video_meta": {"motif_pool": ["leaf macro veins"]},
            "chunks": [_plan()],
        },
    )
    calls = []

    def fake_judge(candidate, plan, *, mode, is_last_candidate, budget):
        assert budget.max_per_video == 0
        assert budget.max_per_minute == 0
        calls.append(candidate.source_id)
        return ObserverJudgeResult(verdict="accept", reason="leaf veins show branching")

    monkeypatch.setattr("backend.services.orchestrator.judge_candidate", fake_judge)

    outcomes = svc.run(
        db_session,
        db_session._test_project_id,
        ["Fractals appear in nature."],
        tmp_path,
    )

    assert len(outcomes) == 1
    assert outcomes[0].used_level == "L_literal"
    assert calls == ["S1"]
    assert not footage.search_library.called
    assert not critic.mm_critique.called
    assert not critic.moderate_clip.called
    assert not video_gen.generate_clip.called


def test_long_video_orchestrator_runs_v2_path(db_session, tmp_path, monkeypatch):
    """The pruned, isolated long-video orchestrator runs the v2 stock path
    end-to-end (regression after Phase-1 separation + dead-code prune)."""
    from backend.lib.pipeline_mode import PipelineMode
    from backend.services.long_video.orchestrator import LongVideoOrchestrator
    from backend.services.shot_router import ShotRouter
    from backend.services.observer_judge import ObserverJudgeResult

    monkeypatch.setenv("MEDIA_BUDDY_PIPELINE_VERSION", "v2")
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "1")

    fake_clip = tmp_path / "clip.mp4"
    fake_clip.write_bytes(b"x" * 2048)
    footage = MagicMock()
    footage.search_secondary.return_value = [_make_candidate("S1")]
    footage.download_candidate.return_value = fake_clip

    svc = LongVideoOrchestrator(
        llm=MagicMock(), footage=footage, critic=MagicMock(), video_gen=MagicMock(),
        shot_router=ShotRouter(mode=PipelineMode.FAST),
        asset_cache=MagicMock(), generated_cache=MagicMock(), mode=PipelineMode.FAST,
    )
    monkeypatch.setattr(svc, "_basic_file_check", lambda path: (True, ""))
    monkeypatch.setattr(
        "backend.services.long_video.orchestrator.build_global_plan",
        lambda llm, sentences, user_brief="": {
            "video_meta": {"motif_pool": ["leaf macro veins"]},
            "chunks": [_plan()],
        },
    )
    monkeypatch.setattr(
        "backend.services.long_video.orchestrator.judge_candidate",
        lambda candidate, plan, *, mode, is_last_candidate, budget: ObserverJudgeResult(
            verdict="accept", reason="leaf veins branching",
        ),
    )

    outcomes = svc.run(
        db_session, db_session._test_project_id,
        ["Fractals appear in nature."], tmp_path,
    )
    assert len(outcomes) == 1
    assert outcomes[0].decision == "use"


def test_v2_early_stop_does_not_review_second_candidate(tmp_path, monkeypatch):
    from backend.services.observer_judge import HaikuReviewBudget, ObserverJudgeResult

    svc = _make_orch()
    monkeypatch.setattr(svc, "_v2_download_outcome", lambda *args, **kwargs: SimpleNamespace())
    calls = []

    def fake_judge(candidate, plan, *, mode, is_last_candidate, budget):
        calls.append(candidate.source_id)
        return ObserverJudgeResult(
            verdict="accept",
            reason="good",
            model_id="google/gemini-2.5-flash",
            elapsed_seconds=1.25,
            est_cost_usd=0.00015,
        )

    monkeypatch.setattr("backend.services.orchestrator.judge_candidate", fake_judge)

    result = svc._v2_review_candidates(
        _plan(),
        [_make_candidate("S1"), _make_candidate("S2")],
        tmp_path,
        "landscape",
        mode="literal",
        used_level="L_literal",
        budget=HaikuReviewBudget(),
    )

    assert result is not None
    assert calls == ["S1"]
    assert result.verify_scores["detection_calls"] == 1
    assert result.verify_scores["detection_elapsed_seconds"] == 1.25
    assert result.verify_scores["detection_est_cost_usd"] == 0.00015


def test_v2_reject_and_ambiguous_continue_without_accepting(tmp_path, monkeypatch):
    from backend.services.observer_judge import HaikuReviewBudget, ObserverJudgeResult

    svc = _make_orch()
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_CHUNK", "3")
    monkeypatch.setattr(svc, "_v2_download_outcome", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(svc, "_v2_guardrail_reject", lambda *args, **kwargs: None)
    verdicts = iter([
        ObserverJudgeResult(verdict="reject", reason="wrong subject"),
        ObserverJudgeResult(verdict="ambiguous", reason="could be metaphor"),
        ObserverJudgeResult(verdict="accept", reason="visible match"),
    ])
    calls = []

    def fake_judge(candidate, plan, *, mode, is_last_candidate, budget):
        calls.append(candidate.source_id)
        return next(verdicts)

    monkeypatch.setattr("backend.services.orchestrator.judge_candidate", fake_judge)

    result = svc._v2_review_candidates(
        _plan(),
        [_make_candidate("S1"), _make_candidate("S2"), _make_candidate("S3")],
        tmp_path,
        "landscape",
        mode="literal",
        used_level="L_literal",
        budget=HaikuReviewBudget(),
    )

    assert result is not None
    assert calls == ["S1", "S2", "S3"]




def test_v2_literal_domain_salvage_does_not_revive_rejected_candidate(tmp_path, monkeypatch):
    svc = _make_orch()
    cand = _make_candidate("PLASTIC_CONTAINER")
    cand.query = "stacking reusable cups"
    cand.source_tags = "reusable plastic containers with lids"
    plan = {
        **_plan(),
        "must_show": ["glass", "cup"],
        "queries": ["stacking reusable cups", "washing glass cups"],
        "domain_visual_pack": ["glass cup", "street vendor"],
        "domain_motif_pool": ["glass cup", "street vendor"],
        "_v2_detection_metrics": {
            "candidate_verdicts": [{
                "source": cand.source,
                "source_id": cand.source_id,
                "verdict": "reject",
                "reason": "plastic containers, no glass cups",
            }],
        },
    }

    called = {"download": False}

    def fake_download(*args, **kwargs):
        called["download"] = True

    monkeypatch.setattr(svc, "_v2_download_outcome", fake_download)

    result = svc._v2_literal_domain_salvage(
        plan, [cand], tmp_path, "portrait", set(), None,
    )

    assert result is None
    assert called["download"] is False


def test_v2_guardrail_rejects_conflicting_locale_even_when_query_matches():
    from backend.services.orchestrator import Orchestrator

    cand = _make_candidate("TAIPEI_METRO")
    cand.query = "Mumbai metro station exit crowd flow"
    cand.source_tags = (
        "TAIPEI, TAIWAN 14 OCTOBER 2015: Passengers walk through exit gate "
        "of metro station. Mumbai metro station exit crowd flow"
    )
    plan = {
        **_plan(),
        "domain": "street-vendors",
        "queries": ["Mumbai metro station exit crowd flow"],
        "domain_visual_pack": ["mumbai street food", "indian street vendor"],
        "domain_motif_pool": ["street markets"],
        "must_show": ["metro", "station", "crowd"],
        "forbidden_visual_pack": [],
    }

    reason = Orchestrator._v2_guardrail_reject(cand, plan)

    assert reason and reason.startswith("locale_mismatch:")


def test_short_v2_version_stamp_contains_deployment_identity():
    from backend.services.orchestrator import Orchestrator, SHORT_V2_RULE_VERSION

    stamp = Orchestrator._short_v2_version_stamp()

    assert stamp["rule_version"] == SHORT_V2_RULE_VERSION
    assert stamp["worker_root"]
    assert stamp["service_root"]
    assert stamp["python_path"]
    assert "commit_sha" in stamp


def test_short_v2_hard_anchors_ignore_locale_and_color_modifiers():
    from backend.services.orchestrator import Orchestrator

    anchors = Orchestrator._v2_hard_must_show_anchors({
        "must_show": ["blue", "mumbai street drink", "rusty cart"],
    })

    assert "blue" not in anchors


def test_short_v2_hard_anchors_ignore_material_and_generic_actor_modifiers():
    from backend.services.orchestrator import Orchestrator

    anchors = Orchestrator._v2_hard_must_show_anchors({
        "must_show": ["stainless steel", "thick wall", "vendor"],
    })

    assert anchors == []


def test_short_v2_hard_anchors_ignore_state_modifiers():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    anchors = Orchestrator._v2_hard_must_show_anchors({
        "must_show": ["empty", "full shelf", "cheap", "old ledger", "fresh milk"],
    })

    assert "empty" not in anchors
    assert "full" not in anchors
    assert "cheap" not in anchors
    assert "old" not in anchors
    assert "fresh" not in anchors
    assert "shelf" in anchors
    assert "ledger" in anchors
    assert "milk" in anchors
    assert Orchestrator._v2_anchor_guardrail_reject(
        FootageCandidate(
            source="pexels",
            source_id="shelf-1",
            source_url="",
            query="convenience store shelf",
            source_tags="convenience store shelf with packaged drinks",
        ),
        {"must_show": ["empty"]},
    ) is None


def test_short_v2_retail_empty_shelf_rejects_bookshelf_and_abstract_space():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    plan = {
        "domain": "convenience-store",
        "sentence": "右区永远空着两格，等突发需求和当天补货。",
        "must_show": ["empty retail shelf"],
        "queries": ["partly empty retail shelf with packaged goods"],
    }
    anchors = Orchestrator._v2_hard_must_show_anchors(plan)
    assert "space" not in anchors
    assert "retail" in anchors
    assert "shelf" in anchors

    valid = FootageCandidate(
        source="pexels",
        source_id="retail-shelf",
        source_url="",
        query="partly empty retail shelf with packaged goods",
        source_tags="Convenience store product shelf with packaged goods and some open shelf area",
    )
    bookshelf = FootageCandidate(
        source="pexels",
        source_id="bookshelf",
        source_url="",
        query="convenience store empty shelf",
        source_tags="Close-up of hand selecting mysterious book on dark bookshelf",
    )
    abstract = FootageCandidate(
        source="pixabay_video",
        source_id="abstract-space",
        source_url="",
        query="empty shelf space in convenience store",
        source_tags="yellow beautiful wallpaper liquid paint space motion abstract ink in water",
    )

    assert Orchestrator._v2_anchor_guardrail_reject(valid, plan) is None
    assert Orchestrator._v2_anchor_guardrail_reject(bookshelf, plan).startswith("retail_wrong_world")
    assert Orchestrator._v2_anchor_guardrail_reject(abstract, plan).startswith("retail_wrong_world")


def test_short_v2_packaged_fried_snack_rejects_generic_potato_frying():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    plan = {
        "domain": "convenience-store",
        "sentence": "本地麻花保质期短，但毛利高18%",
        "must_show": ["packaged fried snack", "date label"],
        "queries": ["packaged fried snack on convenience store shelf"],
    }
    potato = FootageCandidate(
        source="pixabay_video",
        source_id="potato",
        source_url="",
        query="local twisted bread short shelf life",
        source_tags="potatoes package frying oil calories snack fastfood vegetable restaurant",
    )
    packaged = FootageCandidate(
        source="pexels",
        source_id="snack",
        source_url="",
        query="packaged fried snack on convenience store shelf",
        source_tags="convenience store retail shelf with packaged snack bags",
    )

    assert Orchestrator._v2_anchor_guardrail_reject(packaged, plan) is None
    assert Orchestrator._v2_anchor_guardrail_reject(potato, plan).startswith("missing_hard_anchor")


def test_short_v2_retail_degraded_score_blocks_word_only_label_and_retail():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    plan = {
        "domain": "convenience-store",
        "sentence": "去年十月起，他专挑临期90天的酸奶进货。",
        "must_show": ["yogurt cups", "date label", "inventory notebook"],
        "queries": ["yogurt cups in convenience store refrigerator"],
    }
    vinyl = FootageCandidate(
        source="pixabay_video",
        source_id="vinyl",
        source_url="",
        query="yogurt cartons near expiry date",
        source_tags="turntable old vinyl music retro record audio label track",
    )
    clothing = FootageCandidate(
        source="pixabay_video",
        source_id="clothing",
        source_url="",
        query="snack beside cash register",
        source_tags="clothing clothes fashion shop store dress retail apparel boutique",
    )
    retail = FootageCandidate(
        source="pexels",
        source_id="dairy",
        source_url="",
        query="yogurt cups in convenience store refrigerator",
        source_tags="supermarket refrigerated dairy shelf with yogurt cups and food labels",
    )

    assert Orchestrator._v2_story_degraded_candidate_score(plan, vinyl) == 0
    assert Orchestrator._v2_story_degraded_candidate_score(plan, clothing) == 0
    assert Orchestrator._v2_story_degraded_candidate_score(plan, retail) > 0


def test_short_v2_hard_anchors_keep_objects_not_scene_or_action_words():
    from backend.services.orchestrator import Orchestrator

    assert Orchestrator._v2_hard_must_show_anchors({
        "must_show": ["chestnut", "istanbul", "dusk"],
    }) == ["chestnut"]
    assert Orchestrator._v2_hard_must_show_anchors({
        "must_show": ["drink preparation"],
    }) == []


def test_anchor_guardrail_accepts_runtime_probe_usable_for_core_object():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    cand = FootageCandidate(
        source="pexels",
        source_id="probe-chestnut",
        source_url="",
        query="turkish vendor golden paper bags stack chestnut cart",
        source_tags="local street cart",
    )
    cand.runtime_probe_usable_for = ["chestnut", "roasted_chestnut", "paper_bag"]

    assert Orchestrator._v2_anchor_guardrail_reject(
        cand,
        {"must_show": ["chestnut", "paper"]},
    ) is None


def test_review_candidates_non_retail_anchor_goes_to_observer(tmp_path, monkeypatch):
    from backend.services.footage_service import FootageCandidate
    from backend.services.observer_judge import HaikuReviewBudget, ObserverJudgeResult

    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_CHUNK", "1")
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_POST_DOWNLOAD_VERIFY", "0")
    fake_clip = tmp_path / "clip.mp4"
    fake_clip.write_bytes(b"x" * 2048)
    svc = _make_orch()
    monkeypatch.setattr(svc, "_basic_file_check", lambda path: (True, ""))
    svc.footage.download_candidate.return_value = fake_clip
    calls = []

    def fake_judge(candidate, plan, **kwargs):
        calls.append(candidate.source_id)
        return ObserverJudgeResult(verdict="accept", reason="visible cash")

    monkeypatch.setattr("backend.services.orchestrator.judge_candidate", fake_judge)
    wrong = FootageCandidate(
        source="pexels",
        source_id="juice",
        source_url="",
        query="sugarcane juice india vendor",
        source_tags="Street Vendor Making Fresh Juice",
    )
    wrong.runtime_probe_usable_for = ["sugarcane_juice"]
    cash = FootageCandidate(
        source="pexels",
        source_id="cash",
        source_url="",
        query="hands counting cash money",
        source_tags="Cash flow video stock footage hands counting cash money",
    )

    plan = {"chunk_index": 0, "must_show": ["cash"], "sentence": "The vendor takes cash."}
    first = svc._v2_review_candidates(
        plan,
        [wrong],
        tmp_path,
        "portrait",
        mode="runtime_probe",
        used_level="L_runtime_probe",
        budget=HaikuReviewBudget(max_per_video=0),
        used_source_ids=set(),
    )

    assert first is not None
    assert first.source_id == "juice"
    assert calls == ["juice"]


def test_locale_guardrail_rejects_dynamic_probe_wrong_country_matches():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    tokyo = FootageCandidate(
        source="pexels",
        source_id="tokyo",
        source_url="",
        query="turkish roasted chestnut street vendor istanbul",
        source_tags="Ueno Park, Tokyo, Japan: traditional street food market",
    )
    assert Orchestrator._v2_locale_guardrail_reject(
        tokyo,
        {"sentence": "At dusk in Istanbul, a Turkish roasted chestnut cart glows."},
    ) == "locale_mismatch:japan"

    thai = FootageCandidate(
        source="pexels",
        source_id="thai-coffee",
        source_url="",
        query="thick condensed milk vietnamese coffee",
        source_tags="Iced Coffee Enjoyment in Khao Yai",
    )
    reason = Orchestrator._v2_locale_guardrail_reject(
        thai,
        {"sentence": "In Hanoi, a Vietnamese phin coffee stall serves iced coffee."},
    )
    assert reason in {"locale_mismatch:thailand", "locale_mismatch:khao yai"}
















def test_story_soft_fallback_does_not_fail_closed():
    from backend.services.orchestrator import Orchestrator

    assert Orchestrator._v2_story_fail_closed({
        "sentence": "日均卖200杯。",
        "must_show": ["queue"],
        "story_soft_fallback": True,
    }) is False
    assert Orchestrator._v2_story_fail_closed({
        "sentence": "顾客排队变长，新定价18卢比，复购率升到67%。",
        "must_show": ["customer line"],
    }) is True






def test_post_download_review_plan_allows_syrup_fallback_for_kettle_sentence():
    from backend.services.orchestrator import Orchestrator

    review_plan = Orchestrator._v2_post_download_review_plan({
        "sentence": "他把铜壶换成厚壁不锈钢壶，调整糖浆浓度",
        "must_show": ["kettle", "syrup or honey"],
        "queries": ["stainless steel pot replacing copper"],
    })

    assert review_plan["must_show"] == [
        "kettle or syrup/sweet liquid preparation",
    ]




def test_anchor_candidate_priority_moves_queue_clip_before_generic_vendor():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    generic = FootageCandidate(
        source="pexels",
        source_id="generic-juice",
        source_url="",
        query="sugarcane juice india vendor",
        source_tags="vendor operating sugarcane press",
    )
    queue = FootageCandidate(
        source="pexels",
        source_id="queue",
        source_url="",
        query="mumbai customers waiting in line",
        source_tags="group of adults waiting in line",
    )

    ordered = Orchestrator._v2_prioritize_anchor_candidates(
        {"must_show": ["customer line"]},
        [generic, queue],
    )

    assert [cand.source_id for cand in ordered] == ["queue", "generic-juice"]


def test_same_domain_salvage_ignores_query_echo_in_source_tags():
    from backend.services.footage_service import FootageCandidate
    from backend.services.orchestrator import Orchestrator

    candidate = FootageCandidate(
        source="pexels",
        source_id="generic-white-queue",
        source_url="",
        query="indian customers waiting in line",
        source_tags="Group of Adults waiting in line indian customers waiting in line",
    )

    assert Orchestrator._v2_same_domain_score(
        candidate,
        {
            "domain": "street_food_mumbai",
            "queries": ["indian customers waiting in line"],
            "must_show": ["customer line"],
        },
    ) == 0




def test_short_v2_debug_report_writes_versioned_trace_files(tmp_path):
    import json

    from backend.services.orchestrator import ChunkOutcome, Orchestrator, SHORT_V2_RULE_VERSION

    plan = {
        **_plan(),
        "chunk_index": 0,
        "must_show": ["glass", "cup"],
        "domain_motif_pool": ["street vendor"],
        "forbidden_visual_pack": ["laboratory"],
        "_v2_detection_metrics": {
            "fallback_blocked": True,
            "fallback_block_reason": "story_fail_closed",
            "selection_status": "needs_manual_asset",
        },
    }
    outcome = ChunkOutcome(
        chunk_index=0,
        plan=plan,
        local_path=None,
        duration=0,
        source="",
        source_id="",
        source_url="",
        resolution="",
        score=0.0,
        decision="needs_manual_asset",
        cost_usd=0.0,
        used_level="needs_manual_asset",
    )
    footage_dir = tmp_path / "project" / "assets" / "footage"

    Orchestrator._emit_short_observer_debug_report([plan], [outcome], footage_dir)

    project_dir = tmp_path / "project"
    selection_trace = json.loads((project_dir / "selection_trace.json").read_text(encoding="utf-8"))
    visual_plan = json.loads((project_dir / "visual_plan.json").read_text(encoding="utf-8"))
    version_txt = (project_dir / "version_stamp.txt").read_text(encoding="utf-8")

    assert selection_trace["version_stamp"]["rule_version"] == SHORT_V2_RULE_VERSION
    assert selection_trace["chunks"][0]["acceptance_policy"] == "fail_closed"
    assert selection_trace["chunks"][0]["selection_status"] == "needs_manual_asset"
    assert selection_trace["chunks"][0]["fallback_blocked"] is True
    assert visual_plan["chunks"][0]["hard_anchors"] == ["glass", "cup"]
    assert "rule_version=" in version_txt
    assert (project_dir / "observer_debug.html").exists()


def test_observer_judge_haiku_escalation_conditions():
    from backend.services.observer_judge import (
        HaikuReviewBudget,
        ObserverJudgeResult,
        _should_escalate,
    )

    budget = HaikuReviewBudget(max_per_video=3)
    ambiguous = ObserverJudgeResult(
        verdict="ambiguous",
        reason="leaf veins show a repeating branching pattern",
    )
    normal_plan = {**_plan(), "chunk_index": 4, "priority": "normal", "sentence_type": "fact"}
    assert _should_escalate(ambiguous, normal_plan, "literal", False, budget) is False
    assert _should_escalate(ambiguous, normal_plan, "literal", True, budget) is True
    assert _should_escalate(ambiguous, normal_plan, "metaphor", False, budget) is True

    important_plan = {**normal_plan, "priority": "high"}
    assert _should_escalate(ambiguous, important_plan, "literal", False, budget) is True

    budget.mark_used(int(normal_plan["chunk_index"]))
    assert _should_escalate(ambiguous, normal_plan, "literal", True, budget) is False


def test_haiku_budget_limits_total_and_per_minute():
    from backend.services.observer_judge import HaikuReviewBudget

    budget = HaikuReviewBudget(max_per_video=10, max_per_minute=3)
    for idx in range(3):
        assert budget.can_use(idx)
        budget.mark_used(idx)

    assert budget.can_use(3) is False

    per_video = HaikuReviewBudget(max_per_video=1, max_per_minute=3)
    assert per_video.can_use(0)
    per_video.mark_used(0)
    assert per_video.can_use(1) is False


def test_haiku_budget_try_mark_used_is_thread_safe():
    from backend.services.observer_judge import HaikuReviewBudget

    budget = HaikuReviewBudget(max_per_video=1, max_per_minute=3)
    results = []
    lock = threading.Lock()

    def worker():
        ok = budget.try_mark_used(0)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert budget.used_total == 1


def test_v2_reserve_source_prevents_parallel_duplicate_downloads():
    from backend.services.orchestrator import Orchestrator

    used = set()
    lock = threading.Lock()
    cand = _make_candidate("SAME")

    assert Orchestrator._v2_reserve_source(used, cand, lock) is True
    assert Orchestrator._v2_reserve_source(used, cand, lock) is False
    assert "SAME" in used
    assert "pexels:SAME" in used


def test_global_director_visual_anchor_blocks_inherit_for_visible_target():
    from backend.services.global_director import _normalize_plan

    plan = _normalize_plan(
        {
            "video_meta": {"motif_pool": ["city street"]},
            "chunks": [{
                "chunk_id": 0,
                "text": "A car exits the tunnel.",
                "sentence_type": "fact",
                "visual_anchor": True,
                "inherit_prev": True,
                "queries": ["car exits tunnel"],
                "must_show": ["car", "tunnel"],
            }],
        },
        ["A car exits the tunnel."],
    )

    chunk = plan["chunks"][0]
    assert chunk["visual_anchor"] is True
    assert chunk["inherit_prev"] is False
    assert chunk["queries"] == ["car exits tunnel"]


def test_v2_visual_anchor_is_generic_contract_not_topic_words():
    from backend.services.orchestrator import Orchestrator

    assert Orchestrator._v2_has_visual_anchor({
        "sentence": "A chef lifts a copper pan.",
        "sentence_type": "fact",
        "visual_anchor": True,
        "inherit_prev": True,
    }) is True
    assert Orchestrator._v2_has_visual_anchor({
        "sentence": "What would you try first?",
        "sentence_type": "cta",
        "visual_anchor": False,
        "inherit_prev": True,
    }) is False


def test_global_director_repairs_chinese_stock_queries_to_english():
    import json

    from backend.services.global_director import build_global_plan

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def _call(self, system, user, purpose=None):
            self.calls.append(purpose)
            if purpose == "global_director":
                return json.dumps({
                    "video_meta": {"motif_pool": ["发光线条环绕地球"]},
                    "chunks": [{
                        "chunk_id": 0,
                        "text": "发光线条环绕地球。",
                        "sentence_type": "fact",
                        "visual_anchor": True,
                        "inherit_prev": False,
                        "queries": ["发光线条环绕地球"],
                        "metaphor_queries": [],
                        "must_show": ["地球", "线条"],
                        "must_not_show": ["咖啡"],
                    }],
                }, ensure_ascii=False)
            return json.dumps({
                "video_meta": {"motif_pool": ["glowing lines around earth"]},
                "chunks": [{
                    "chunk_id": 0,
                    "text": "发光线条环绕地球。",
                    "sentence_type": "fact",
                    "visual_anchor": True,
                    "inherit_prev": False,
                    "queries": ["glowing lines around earth"],
                    "metaphor_queries": [],
                    "must_show": ["earth", "glowing lines"],
                    "must_not_show": ["coffee"],
                }],
            }, ensure_ascii=False)

    plan = build_global_plan(FakeLLM(), ["发光线条环绕地球。"])
    chunk = plan["chunks"][0]

    assert chunk["queries"] == ["glowing lines around earth"]
    assert chunk["must_show"] == ["earth", "glowing lines"]
    assert plan["video_meta"]["motif_pool"] == ["glowing lines around earth"]


def test_similar_rescue_generates_bamboo_substitute_queries(monkeypatch):
    from backend.services.literal_probe import probe_similar_subject_candidates

    seen_queries = []

    class FakeFootage:
        def search_secondary(self, query, orientation, kind="video", limit=3):
            seen_queries.append(query)
            if query == "bamboo forest":
                return [_make_candidate("BAMBOO")]
            return []

    plan = {
        **_plan(),
        "sentence": "And it chooses different types of bamboo.",
        "queries": ["different types of bamboo"],
        "must_show": ["bamboo"],
        "must_not_show": ["city", "finance"],
    }

    result = probe_similar_subject_candidates(
        FakeFootage(), plan, orientation="landscape", top_n=3,
    )

    assert result
    assert "bamboo close up" in seen_queries
    assert "bamboo forest" in seen_queries
    assert plan["_similar_rescue_queries"]["subject"]


def test_v2_uses_similar_subject_before_motif(db_session, tmp_path, monkeypatch):
    from backend.services.observer_judge import ObserverJudgeResult

    monkeypatch.setenv("MEDIA_BUDDY_PIPELINE_VERSION", "v2")
    monkeypatch.setenv("MEDIA_BUDDY_ALLOW_OFFLINE", "1")

    fake_clip = tmp_path / "bamboo.mp4"
    fake_clip.write_bytes(b"x" * 2048)

    cand = _make_candidate("BAMBOO")
    cand.query = "bamboo close up"
    cand.source_tags = "bamboo close up green plants nature"

    footage = MagicMock()
    footage.search_library.return_value = []
    footage.search_secondary.return_value = []
    footage.search_secondary.side_effect = lambda query, *args, **kwargs: (
        [cand] if query == "bamboo close up" else []
    )
    footage.download_candidate.return_value = fake_clip

    svc = _make_orch(footage=footage)
    monkeypatch.setattr(svc, "_basic_file_check", lambda path: (True, ""))
    bamboo_plan = {
        **_plan(),
        "sentence": "And it chooses different types of bamboo.",
        "queries": ["different types of bamboo"],
        "metaphor_queries": [],
        "must_show": ["bamboo"],
        "must_not_show": ["finance", "city"],
    }
    monkeypatch.setattr(
        "backend.services.orchestrator.build_short_video_global_plan",
        lambda llm, sentences, user_brief="": {
            "video_meta": {"motif_pool": ["green nature background"]},
            "chunks": [bamboo_plan],
        },
    )

    def fake_judge(candidate, plan, *, mode, is_last_candidate, budget):
        assert budget.max_per_video == 0
        assert budget.max_per_minute == 0
        assert mode == "similar_subject"
        return ObserverJudgeResult(verdict="accept", reason="visible bamboo close-up")

    monkeypatch.setattr("backend.services.orchestrator.judge_candidate", fake_judge)

    outcomes = svc.run(
        db_session,
        db_session._test_project_id,
        ["And it chooses different types of bamboo."],
        tmp_path,
    )

    assert outcomes[0].used_level == "L_similar_subject"
    assert outcomes[0].local_path == str(fake_clip)
    assert not footage.search_library.called
