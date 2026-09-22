"""Phase 2.6 — Stock-first orchestrator with parallel chunk processing.

Replaces ChunkEngine.process_chunk's serial loop with a parallel pipeline:
  Stage 1: LLM plan_chunks (one call → all metadata)
  Stage 2: per-chunk parallel stock search (top N candidates)
  Stage 3: per-chunk parallel score + pick (mm_critic)
  Stage 4: AI fallback (only chunks without acceptable stock)
            ├── ShotRouter → 1 provider
            ├── Generate
            ├── Quality check
            └── Retry once with fallback_provider on reject

Returns footage_dicts compatible with PipelineService._stage_compose.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.lib.ffmpeg_locator import ensure_ffmpeg_on_path, video_encoder_args
from backend.lib.llm_client import LLMClient
from backend.lib.pipeline_mode import PipelineMode, get_mode_config
from backend.models.chunk import Chunk, Shot
from backend.models.project import Project
from backend.services.asset_cache import AssetCache
from backend.services.footage_service import FootageCandidate, FootageService
from backend.services.generated_cache import GeneratedCache
from backend.services.short_video.visual_focus import build_short_video_global_plan
from backend.services.literal_probe import (
    _subject_keys,
    _subject_mismatch,
    probe_search_cache_stats,
    probe_broadened_subject_candidates,
    probe_literal_candidates,
    probe_metaphor_candidates,
    probe_motif_candidate,
    probe_motif_candidate_lenient,
    reset_probe_search_cache,
    probe_similar_category_candidates,
    probe_similar_subject_candidates,
)
from backend.services.observer_judge import HaikuReviewBudget, judge_candidate
from backend.services.reframe_service import ReframeService, _orientation_to_aspect
from backend.services.shot_router import ShotRouter
from backend.services.video_gen_service import VideoGenService

logger = logging.getLogger(__name__)

SHORT_V2_RULE_VERSION = os.environ.get(
    "MEDIA_BUDDY_SHORT_V2_RULE_VERSION",
    "short_v2_anchor_scope_2026_06_20_phase1",
)


_VISUAL_TERM_STOPWORDS = {
    "the", "and", "with", "for", "from", "into", "onto", "that", "this",
    "shot", "stock", "clip", "video", "image", "photo", "scene", "view",
    "camera", "pan", "zoom", "close", "closeup", "close_up", "focus",
    "focusing", "beautiful", "beautifully", "presented", "abstract",
    "concept", "representation", "idea", "ideas", "background", "blurry",
    "soft", "light", "lighting", "dark", "colorful", "digital", "static",
}


_VISUAL_TERM_ALIASES = {
    "fractals": "fractal",
    "dimensions": "dimension",
    "curves": "curve",
    "lines": "line",
    "branches": "branch",
    "branching": "branch",
    "trees": "tree",
    "veins": "vein",
    "lungs": "lung",
    "mathematicians": "mathematician",
    "drawings": "drawing",
}


def _visual_terms(*values: Any) -> set[str]:
    terms: set[str] = set()

    def add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(item)
            return
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", str(raw).lower()):
            token = _VISUAL_TERM_ALIASES.get(token, token)
            if token in _VISUAL_TERM_STOPWORDS:
                continue
            terms.add(token)
            if token == "fractal":
                terms.update({"curve", "pattern", "geometry", "dimension"})
            elif token == "dimension":
                terms.update({"geometry", "fractal", "line"})
            elif token == "branch":
                terms.update({"tree", "nature", "vein", "structure"})
            elif token == "lung":
                terms.update({"anatomy", "medical", "vein"})
            elif token == "vein":
                terms.update({"anatomy", "medical", "branch", "lung"})
            elif token == "mathematician":
                terms.update({"math", "paper", "drawing", "desk"})

    for value in values:
        add(value)
    return terms


# How many top candidates to download+score per chunk. Higher = better odds
#
#   most winners surface in top 2-3 by source rank. The env var lets us
#   crank back up (TOP_N=5) for a "pro" mode without code change.
# Aligned with the long-video pipeline (DEFAULT_TOP_N=2): most winners surface in
# observer calls with negligible quality loss. Short-video only.
DEFAULT_TOP_N = int(os.environ.get("MEDIA_BUDDY_TOP_N", "2"))


@dataclass
class ChunkOutcome:
    """Per-chunk final selection (stock OR AI)."""
    chunk_index: int
    plan: dict
    local_path: Optional[str]
    duration: float
    source: str            # "pexels" | "fal_ai_seedance_lite" | ...
    source_id: str
    source_url: str
    resolution: str
    score: float
    decision: str          # "use" | "acceptable" | "broll_ok" | "reject" | "stock_miss" | "pending_inherit"
    cost_usd: float
    source_tags: str = ""  # provider title/tags plus stock-search metadata
    # Subject horizontal position (0.0=left .. 1.0=right) from the observer
    # vision judge; threaded to compose so portrait reframing crops toward the
    # subject instead of blindly centering. None = unknown → center crop.
    subject_h_pos: Optional[float] = None
    # Phase 2.7b — content moderation result for the FINAL chosen clip
    safety_severity: str = "ok"     # "ok" | "warn" | "block"
    safety_flags: list = None       # ["nudity", ...] when not safe
    safety_reason: str = ""         # short human-readable
    error: Optional[str] = None
    # _ai_attempt to record WHICH gate approved this clip.
    semantic_pass_source: str = ""  # "contract_verifier" | "harness_generation_prompt" | "legacy_mm_critic" | ""
    mm_quality_pass: bool = False
    mm_quality_score: float = 0.0
    review_skipped: bool = False
    review_skip_reason: str = ""
    # Populated by _stock_attempt_progressive when finalizing a winner,
    # then propagated to shots.used_level / shots.used_wave columns.
    used_level: Optional[str] = None  # "L1" | "L2" | "L3" | "L4" | "L5" | "L5_image"
    used_wave: Optional[int] = None
    asset_type: str = "video"          # "video" | "image"
    motion_type: Optional[str] = None  # "ken_burns" / "push_in" / ... when image
    director_intent: str = ""
    selected_prompt: str = ""
    verify_reason: str = ""
    confidence: float = 0.0
    fallback_reason: str = ""
    # + user-facing audit. Empty dict when no verifier ran (e.g. AI gen,
    verify_scores: dict = None
    # List of {"test_id": str, "passed": bool, "reason": str}.
    verify_test_results: list = None
    # reuses the previous chunk's clip via Pass 2 in run(), these record
    # the donor + reason so the asset-report API can show "this chunk
    # inherited from chunk N (transition)" instead of pretending it was
    # an independent stock hit.
    inherited_from_chunk: Optional[int] = None
    inherit_reason: str = ""   # "transition" | "cta_farewell" | ""
    # commit 3a (audit_cascade_trace) with per-level reviewed/passed counts.
    # Using field(default_factory=dict) is REQUIRED — bare `= {}` would
    # share a single dict instance across all ChunkOutcome instances.
    cascade_trace: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.safety_flags is None:
            self.safety_flags = []
        if self.verify_scores is None:
            self.verify_scores = {}
        if self.verify_test_results is None:
            self.verify_test_results = []


@dataclass
class HarnessSelectResult:
    """Outcome of harness contract verification for one chunk.

    Distinguishes "harness completed and rejected everything" (the only
    true veto signal — caller should NOT let mm_critic override) from
    "harness infrastructure failed" (legacy mm_critic path still allowed).
    """
    status: str   # "selected" | "all_rejected" | "no_contracts" |
                  # "no_observations" | "winner_blocked" | "error"
    outcome: Optional[ChunkOutcome] = None  # populated when status="selected"
    reviewed_count: int = 0
    passed_count: int = 0
    rejected_count: int = 0
    error: str = ""


class Orchestrator:
    """Stock-first parallel orchestrator. One method: run()."""

    def __init__(
        self,
        llm: LLMClient,
        footage: FootageService,
        critic: Any,
        video_gen: VideoGenService,
        shot_router: ShotRouter,
        asset_cache: Optional[AssetCache] = None,
        generated_cache: Optional[GeneratedCache] = None,
        mode: PipelineMode = PipelineMode.FAST,
        # Phase 2.7b — content moderation context (will become Series fields)
        industry: str = "",
        forbidden_topics: Optional[list[str]] = None,
        moderation_enabled: bool = True,
    ):
        self.llm = llm
        self.footage = footage
        self.critic = critic
        self.video_gen = video_gen
        self.shot_router = shot_router
        self.asset_cache = asset_cache or AssetCache()
        self.generated_cache = generated_cache or GeneratedCache()
        self.mode = mode
        self.cfg = get_mode_config(mode)
        # Moderation
        self.industry = industry
        self.forbidden_topics = forbidden_topics
        self.moderation_enabled = moderation_enabled
        self._image_gen_count = 0
        self._image_gen_lock = threading.Lock()
        # Phase 2.11 — letterbox reframer for 16:9 → 9:16 (and similar)
        self.reframer = ReframeService()
        # build fails (LLM auth / cloud quota / etc.), these stay None / {}
        # Runtime niche-broaden cache (subject -> broadened queries). Populated
        # lazily by _v2_broadened_subject_candidates; one cheap LLM call per
        # distinct failed subject per run. See MEDIA_BUDDY_BROADEN_NICHE.
        self._broaden_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Phase 2.11 — letterbox reframe helper
    # ------------------------------------------------------------------
    def _reframe_if_needed(
        self, local: Path, orientation: str, subject_h_pos: object = None,
    ) -> Path:
        """If `orientation` requests a non-landscape aspect, run the source
        through ReframeService. Returns the reframed file (or the original
        if it already matched / reframe failed). Idempotent for matching aspects.

        `subject_h_pos` (0=left..1=right, or None) shifts the portrait crop
        toward the subject instead of centering."""
        target = _orientation_to_aspect(orientation)
        return self._reframe_to_aspect(local, target, subject_h_pos=subject_h_pos)

    def _reframe_to_aspect(
        self, local: Path, target_aspect: Optional[str], subject_h_pos: object = None,
    ) -> Path:
        """Same as `_reframe_if_needed` but accepts an aspect string directly
        (e.g. '9:16'). Used by AI generation paths where aspect is already
        plumbed through."""
        if target_aspect is None or target_aspect == "16:9":
            return local
        try:
            res = self.reframer.to_aspect(
                local, target_aspect=target_aspect, out_dir=Path(local).parent,
                subject_h_pos=subject_h_pos,
            )
            if res is None:
                return local
            return res
        except Exception as e:
            logger.warning("reframe failed for %s: %s", local, e)
            return local

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        db: Session,
        project_id: str,
        sentences: list[str],
        output_dir: Path,
        orientation: str = "landscape",
        top_n: int = DEFAULT_TOP_N,
    ) -> list[ChunkOutcome]:
        """Run the full Stock-first pipeline. Persists Chunk + Shot ORM rows
        for inspection but does not gate on them. Returns ordered outcomes."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # is process-global (single-pipeline-per-process assumption), so
        # we set + detach it around the run() body. summary() is dumped to
        # the log at the end for smoke 15+ baseline collection.
        from backend.services.llm_call_counter import (
            LLMCallCounter, set_active_counter, get_active_counter,
        )
        # Reuse a counter an outer scope already bound (pipeline_service owns one
        # for the WHOLE video so script-gen + TTS cost are captured too); only
        # own + detach it when running standalone (tests/smoke).
        _owns_counter = get_active_counter() is None
        _llm_counter = get_active_counter() or LLMCallCounter()
        if _owns_counter:
            set_active_counter(_llm_counter)

        try:
            return self._run_inner(
                db, project_id, sentences, output_dir, orientation, top_n,
                llm_counter=_llm_counter,
            )
        finally:
            # Dump summary regardless of success/failure — failures are
            # also interesting cost data points.
            #
            # logger.warning (not just .info — many stdout-captured smoke
            # runs filter INFO and we lose the numbers). The JSON file is
            # the source-of-truth artifact; the log line is for quick
            # visibility.
            try:
                summary = _llm_counter.summary()
                # 1. Write canonical artifact
                summary_path = output_dir / "llm_call_counter.json"
                try:
                    import json as _json
                    summary_path.write_text(
                        _json.dumps(summary, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as _e:
                    logger.warning(
                        "llm_call_counter: failed to write %s: %s",
                        summary_path, _e,
                    )
                # 2. Emit at WARN level so stdout-tail / smoke drivers see it
                logger.warning(
                    "llm_call_counter_summary calls=%d est_cost=$%.4f real_cost=$%.4f "
                    "by_purpose_model=%s artifact=%s",
                    summary["grand_total_calls"],
                    summary["grand_total_est_cost_usd"],
                    summary.get("grand_total_real_cost_usd", 0.0),
                    summary["aggregate_by_purpose_and_model"],
                    summary_path,
                )
            except Exception as e:
                logger.warning("llm_call_counter summary dump failed: %s", e)
            if _owns_counter:
                set_active_counter(None)

    def _run_inner(
        self,
        db: Session,
        project_id: str,
        sentences: list[str],
        output_dir: Path,
        orientation: str = "landscape",
        top_n: int = DEFAULT_TOP_N,
        *,
        llm_counter: Optional["LLMCallCounter"] = None,
    ) -> list[ChunkOutcome]:
        """Actual run implementation. Split out so run() can wrap with
        counter activation + summary dump without indenting all the
        existing code."""
        pipeline_version = os.environ.get("MEDIA_BUDDY_PIPELINE_VERSION", "v2").strip().lower()
        logger.info("MEDIA_BUDDY_PIPELINE_VERSION=%r", pipeline_version or "v1")
        if pipeline_version == "v2":
            logger.info("MEDIA_BUDDY v2 stock-only pipeline ACTIVE")
            return self._run_v2_inner(
                db, project_id, sentences, output_dir, orientation, top_n,
                llm_counter=llm_counter,
            )
        raise RuntimeError("only the v2 pipeline ships in this build (MEDIA_BUDDY_PIPELINE_VERSION=v2)")

    # ------------------------------------------------------------------
    # Media Buddy v2 stock-only pipeline
    # ------------------------------------------------------------------
    def _run_v2_inner(
        self,
        db: Session,
        project_id: str,
        sentences: list[str],
        output_dir: Path,
        orientation: str = "landscape",
        top_n: int = DEFAULT_TOP_N,
        *,
        llm_counter: Optional["LLMCallCounter"] = None,
    ) -> list[ChunkOutcome]:
        self._preflight_auth_check()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
        except Exception:
            project = None
        user_brief = str(getattr(project, "prompt", "") or "") if project is not None else ""

        global_plan = build_short_video_global_plan(self.llm, sentences, user_brief=user_brief)
        plans = list(global_plan.get("chunks") or [])
        motif_pool = list((global_plan.get("video_meta") or {}).get("motif_pool") or [])
        # 导演现场落盘(best-effort):完整 global_plan 里带 video_meta.motif_pool_source
        # the render worker),ECI 销毁后仍可查。写失败绝不影响出片。
        # 注意:必须写到流水线根 = output_dir.parent.parent(与 observer_debug/
        # visual_plan 同目录,见 _emit_short_observer_debug_report),否则 worker 的
        # 诊断上传扫不到它。default=str 兜底任何非原生 JSON 值,绝不让 dumps 抛。
        try:
            (output_dir.parent.parent / "director_raw.json").write_text(
                json.dumps(global_plan, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("director_raw.json write failed", exc_info=True)

        for plan in plans:
            self._persist_chunk_plan(db, project_id, plan)

        budget = HaikuReviewBudget(max_per_video=0, max_per_minute=0)
        reset_probe_search_cache(self.footage)
        observer_cache: dict[tuple[Any, ...], Any] = {}
        observer_cache_lock = threading.Lock()
        observer_budget = {"calls": 0}
        observer_budget_lock = threading.Lock()
        outcomes: list[Optional[ChunkOutcome]] = [None] * len(plans)
        used_source_ids: set[str] = set()
        used_source_lock = threading.Lock()
        selection_jobs: list[tuple[int, dict]] = []
        # Attach read-only narration context so the generated-footage director
        # biography's life stages visually distinct. Static, parallel-safe.
        self._v2_attach_narration_context(plans)

        for idx, plan in enumerate(plans):
            if self._v2_has_visual_anchor(plan):
                plan["inherit_prev"] = False
                plan["inherit_forward"] = False
                plan["inherit_reason"] = ""
            if idx > 0 and self._v2_should_inherit_fragment(plan):
                plan["inherit_prev"] = True
                plan["inherit_reason"] = plan.get("inherit_reason") or "sentence_fragment"
            if plan.get("inherit_prev") and idx > 0:
                outcomes[idx] = ChunkOutcome(
                    chunk_index=idx, plan=plan, local_path=None,
                    duration=0, source="", source_id="", source_url="",
                    resolution="", score=0.0, decision="pending_inherit",
                    cost_usd=0.0,
                    inherit_reason=plan.get("inherit_reason", "transition"),
                    used_level="L_inherit_prev",
                )
                continue
            selection_jobs.append((idx, plan))

        concurrency = self._v2_selection_concurrency()
        logger.info(
            "v2 asset selection concurrency=%d jobs=%d",
            concurrency, len(selection_jobs),
        )
        if concurrency <= 1 or len(selection_jobs) <= 1:
            for idx, plan in selection_jobs:
                if llm_counter is not None:
                    with llm_counter.chunk(idx):
                        outcomes[idx] = self._v2_select_chunk(
                            plan, output_dir, orientation, top_n, motif_pool,
                            budget, used_source_ids,
                            used_source_lock=used_source_lock,
                            observer_cache=observer_cache,
                            observer_cache_lock=observer_cache_lock,
                            observer_budget=observer_budget,
                            observer_budget_lock=observer_budget_lock,
                        )
                else:
                    outcomes[idx] = self._v2_select_chunk(
                        plan, output_dir, orientation, top_n, motif_pool,
                        budget, used_source_ids,
                        used_source_lock=used_source_lock,
                        observer_cache=observer_cache,
                        observer_cache_lock=observer_cache_lock,
                        observer_budget=observer_budget,
                        observer_budget_lock=observer_budget_lock,
                    )
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                future_to_idx = {
                    ex.submit(
                        self._v2_select_chunk_with_counter,
                        plan, output_dir, orientation, top_n, motif_pool,
                        budget, used_source_ids, used_source_lock,
                        llm_counter,
                        observer_cache, observer_cache_lock,
                        observer_budget, observer_budget_lock,
                    ): idx
                    for idx, plan in selection_jobs
                }
                for fut in as_completed(future_to_idx):
                    idx = future_to_idx[fut]
                    outcomes[idx] = fut.result()

        max_inherits_per_donor = max(0, int(
            os.environ.get("MEDIA_BUDDY_V2_MAX_INHERIT_PER_SOURCE", "1")
        ))
        inherit_donor_counts: dict[int, int] = {}
        for idx, plan in enumerate(plans):
            cur = outcomes[idx]
            if cur is not None and cur.decision not in ("pending_inherit", "stock_miss"):
                continue
            donor = None
            if cur is not None and cur.decision == "pending_inherit" and plan.get("inherit_prev"):
                donor = self._find_donor_outcome_for_run(outcomes, idx)
            elif max_inherits_per_donor > 0:
                exhausted_donors = {
                    donor_idx
                    for donor_idx, count in inherit_donor_counts.items()
                    if count >= max_inherits_per_donor
                }
                donor = self._find_donor_outcome_for_aggressive(
                    outcomes, idx, exclude=exhausted_donors,
                )
            if donor is None:
                if self._v2_story_fail_closed(plan):
                    outcomes[idx] = self._v2_needs_manual_asset_outcome(
                        plan,
                        idx,
                        "story_fail_closed: blocked ship fallback because no accepted asset satisfied hard anchors",
                    )
                    continue
                # matched no stock used to raise UnmatchedChunkError and waste
                # the whole run (e.g. chunk 146 of a 152-chunk black-mamba long
                # video, after 978 LLM calls). Two guaranteed levels instead,
                # in the user's priority order: (A) a theme-environment empty
                # shot from motif_pool that still fits the whole video's
                # background, (B) reuse the nearest real shot. UnmatchedChunkError
                # is only reached when the WHOLE video produced no footage at all.
                rescued = self._v2_ship_fallback(
                    plan, idx, output_dir, orientation, motif_pool,
                    budget, outcomes, used_source_ids, used_source_lock,
                )
                if rescued is not None:
                    outcomes[idx] = rescued
                    if rescued.source_id:
                        used_source_ids.add(rescued.source_id)
                        if rescued.source:
                            used_source_ids.add(f"{rescued.source}:{rescued.source_id}")
                    continue
                outcomes[idx] = ChunkOutcome(
                    chunk_index=idx, plan=plan, local_path=None,
                    duration=0, source="", source_id="", source_url="",
                    resolution="", score=0.0, decision="needs_manual_asset",
                    cost_usd=0.0,
                    error="v2: no acceptable stock asset after similar rescue; no safe inheritance donor available",
                    used_level="needs_manual_asset",
                )
                continue
            distinct = self._v2_distinct_before_reuse_fallback(
                plan, idx, output_dir, orientation, motif_pool,
                used_source_ids, used_source_lock,
            )
            if distinct is not None:
                outcomes[idx] = distinct
                continue
            if self._v2_story_fail_closed(plan):
                outcomes[idx] = self._v2_needs_manual_asset_outcome(
                    plan,
                    idx,
                    "story_fail_closed: blocked L_inherit_safe because no accepted asset satisfied hard anchors",
                )
                continue
            inherited = self._build_inherited_outcome(plan, donor, idx)
            inherited.inherit_reason = (
                plan.get("inherit_reason")
                or ("inherit_prev" if donor.chunk_index < idx else "inherit_forward")
            )
            inherited.used_level = (
                "L_inherit_safe"
            )
            outcomes[idx] = inherited
            inherit_donor_counts[donor.chunk_index] = (
                inherit_donor_counts.get(donor.chunk_index, 0) + 1
            )
            if inherited.source_id:
                used_source_ids.add(inherited.source_id)
                if inherited.source:
                    used_source_ids.add(f"{inherited.source}:{inherited.source_id}")

        # Final ship-pass: earlier chunks can miss before later chunks have
        # produced a usable donor. Once the first pass is complete, fill every
        # remaining hole with the nearest real clip, and only then use a neutral
        # generated motion fallback. This keeps short videos from failing after
        # money has already been spent on script/TTS/stock probes.
        for idx, oc in enumerate(outcomes):
            if oc is not None and oc.local_path:
                continue
            plan = plans[idx]
            if self._v2_story_fail_closed(plan):
                distinct = self._v2_story_distinct_ship_fallback(
                    plan, idx, output_dir, orientation, motif_pool,
                    used_source_ids, used_source_lock,
                )
                if distinct is not None:
                    outcomes[idx] = distinct
                    logger.info(
                        "chunk %d final ship-pass used fresh distinct story fallback",
                        idx,
                    )
                    continue
                degraded = self._v2_story_degraded_ship_fallback(
                    plan, idx, outcomes,
                )
                if degraded is not None:
                    outcomes[idx] = degraded
                    logger.warning(
                        "chunk %d final ship-pass degraded hard anchors=%s via donor=%s",
                        idx, self._v2_hard_must_show_anchors(plan),
                        degraded.inherited_from_chunk,
                    )
                    continue
                outcomes[idx] = self._v2_needs_manual_asset_outcome(
                    plan,
                    idx,
                    "story_fail_closed: blocked final ship fallback for hard-anchor story chunk",
                )
                logger.warning(
                    "chunk %d final ship-pass blocked by story fail-closed hard_anchors=%s",
                    idx, self._v2_hard_must_show_anchors(plan),
                )
                continue
            distinct = self._v2_distinct_before_reuse_fallback(
                plan, idx, output_dir, orientation, motif_pool,
                used_source_ids, used_source_lock,
            )
            if distinct is not None:
                outcomes[idx] = distinct
                logger.info(
                    "chunk %d final ship-pass used fresh distinct fallback",
                    idx,
                )
                continue
            donor = self._find_any_donor(outcomes, idx)
            if donor is not None:
                inherited = self._build_inherited_outcome(plan, donor, idx)
                inherited.inherit_reason = (
                    "ship_inherit_prev"
                    if donor.chunk_index < idx
                    else "ship_inherit_forward"
                )
                inherited.used_level = "L_ship_inherit"
                inherited.fallback_reason = "v2_final_ship_fallback_single_chunk_unmatched"
                outcomes[idx] = inherited
                logger.info(
                    "chunk %d final ship-pass inherits nearest shot from chunk %d",
                    idx, donor.chunk_index,
                )
                continue
            emergency = self._local_fallback_attempt(
                plan, output_dir, orientation,
                reason=(
                    (oc.error if oc is not None else None)
                    or "v2_final_ship_fallback_no_online_asset_or_donor"
                ),
            )
            if emergency is not None:
                emergency.used_level = "L_emergency_fallback"
                emergency.fallback_reason = "v2_final_ship_fallback_generated_neutral_motion"
                outcomes[idx] = emergency
                logger.warning(
                    "chunk %d final ship-pass used generated neutral motion fallback",
                    idx,
                )

        final = [oc for oc in outcomes if oc is not None]
        # Video-level subject sweep (ported from the long-video pipeline, kept
        # independent here): replace any shot whose tags name a DIFFERENT concept-
        # group member than the video's subject (e.g. a wolf clip in a boar short)
        # with the nearest on-subject shot. Path-independent backstop.
        try:
            self._short_sweep_subject_mismatches(final, self._short_video_subject_keys(plans))
        except Exception:  # noqa: BLE001 — a sweep failure must never kill a render
            logger.exception("short subject sweep failed (non-fatal)")
        search_stats = probe_search_cache_stats(self.footage)
        for plan in plans:
            metrics = plan.setdefault("_v2_detection_metrics", {})
            metrics["search_cache_hits"] = search_stats.get("hits", 0)
            metrics["search_cache_misses"] = search_stats.get("misses", 0)
        try:
            self._emit_short_observer_debug_report(plans, final, output_dir)
        except Exception:  # noqa: BLE001 - debug reports must never kill production
            logger.exception("short observer_debug report failed (non-fatal)")
        missing = [oc for oc in final if not oc.local_path]
        if missing:
            from backend.lib.plan_validation import UnmatchedChunkError
            for oc in final:
                self._persist_shot_outcome(db, project_id, oc)
            db.commit()
            first = missing[0]
            sentence = str((first.plan or {}).get("sentence") or "")
            raise UnmatchedChunkError(
                first.chunk_index,
                first.error or "v2_no_acceptable_stock_asset",
                sentence=sentence,
            )
        for oc in final:
            self._persist_shot_outcome(db, project_id, oc)
        search_stats = probe_search_cache_stats(self.footage)
        logger.info(
            "short_v2_probe_search_cache hits=%d misses=%d",
            search_stats.get("hits", 0), search_stats.get("misses", 0),
        )
        db.commit()
        return final

    @staticmethod
    def _v2_should_inherit_fragment(plan: dict) -> bool:
        sentence = str(plan.get("sentence") or plan.get("text") or "").strip()
        compact = re.sub(r"[\s，。！？,.!?；;：:、]+", "", sentence)
        if not compact:
            return False
        if len(compact) > 8:
            return False
        if Orchestrator._v2_has_visual_anchor(plan):
            return False
        if re.search(r"\d", sentence):
            return False
        return True

    @staticmethod
    def _v2_has_visual_anchor(plan: dict) -> bool:
        """Return True when the chunk deserves its own visual search.

        v2 treats this as a planning contract, not a topic-specific word list:
        global_director decides whether the sentence has a concrete visible
        target. The fallback below only protects older plans that lack the new
        visual_anchor field.
        """
        if "visual_anchor" in plan:
            return bool(plan.get("visual_anchor"))
        sentence = str(plan.get("sentence") or plan.get("text") or "").strip()
        lower = sentence.lower()
        if (
            any(marker in sentence for marker in ("评论", "留言", "告诉我", "关注", "收藏", "点赞", "订阅"))
            or any(marker in lower for marker in ("comment", "subscribe", "follow", "like", "save", "share", "tell me"))
        ):
            return False
        sentence_type = str(plan.get("sentence_type") or "").strip().lower()
        if sentence_type in {"cta", "transition", "emotion"}:
            return False
        if plan.get("must_show") or plan.get("queries") or plan.get("metaphor_queries"):
            return True
        compact = re.sub(r"[\s，。！？,.!?；;：:、]+", "", sentence)
        return len(compact) > 8 or bool(re.search(r"\d", compact))

    @staticmethod
    def _short_v2_version_stamp() -> dict[str, str]:
        root = Path.cwd()
        commit = os.environ.get("MEDIA_BUDDY_COMMIT_SHA", "").strip()
        if not commit:
            try:
                commit = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=root,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2,
                ).strip()
            except Exception:
                commit = "unknown"
        return {
            "commit_sha": commit,
            "rule_version": SHORT_V2_RULE_VERSION,
            "worker_root": str(root),
            "service_root": str(root),
            "python_path": sys.executable,
        }

    @staticmethod
    def _v2_flatten_text(value: Any) -> str:
        parts: list[str] = []
        if isinstance(value, dict):
            for item in value.values():
                text = Orchestrator._v2_flatten_text(item)
                if text:
                    parts.append(text)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                text = Orchestrator._v2_flatten_text(item)
                if text:
                    parts.append(text)
        elif value is not None:
            parts.append(str(value))
        return " ".join(parts).lower()




    @staticmethod
    def _v2_hard_must_show_anchors(plan: dict) -> list[str]:
        weak = {
            "street", "city", "urban", "scene", "footage", "video", "shot",
            "view", "close", "close-up", "closeup", "macro", "wide", "background", "daytime", "local",
            "authentic", "life", "lifestyle", "business",
            "mumbai", "indian", "india", "delhi", "kolkata", "jaipur",
            "chinese", "china", "taiwan", "taipei", "japanese", "japan",
            "korean", "korea", "vietnam", "vietnamese", "hanoi", "thai", "thailand",
            "turkish", "turkey", "istanbul",
            "blue", "red", "green", "yellow", "orange", "purple", "pink",
            "brown", "gray", "grey", "golden", "silver", "rusty", "rusted",
            "shiny", "matte", "bright", "dark", "colorful", "colourful",
            "copper", "brass", "bronze", "steel", "stainless", "iron",
            "aluminum", "aluminium", "tin", "plastic", "acrylic", "ceramic",
            "metal", "paper", "wood", "wooden",
            "thick", "thin", "wall", "long", "short", "sweet", "taste", "mouth",
            "hot", "warm", "cold", "fresh", "small", "neat", "ready",
            "available", "blank", "busy", "cheap", "daily", "empty", "expired",
            "expiration", "expiry", "expensive", "filled", "full", "left",
            "leftover", "new", "old", "open", "closed", "ordinary", "remaining",
            "space", "spare", "temporary", "unused", "vacant",
            "morning", "noon", "evening", "night", "dusk", "sunset", "sunrise",
            "drink", "preparation", "food", "pack", "vendor", "stall", "cart", "stand",
            "tourist", "tourists", "commuter", "commuters", "person", "people",
            "give", "gives", "giving", "receive", "receives", "receiving",
            "scoop", "scoops", "scooping", "stack", "stacked", "stacking",
            "stop", "stops", "pause", "pauses", "quick", "quickly",
        }
        anchors: list[str] = []
        for term in (plan.get("must_show") or []):
            text = str(term or "").strip().lower()
            if not text:
                continue
            parts = re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text)
            if len(parts) > 1:
                for part in parts:
                    part = part.strip("-")
                    if part and part not in weak and part not in anchors:
                        anchors.append(part)
                continue
            if text not in weak and text not in anchors:
                anchors.append(text)
        return anchors

    @staticmethod
    def _v2_chunk_type(plan: dict) -> str:
        sentence_type = str(plan.get("sentence_type") or "").strip().lower()
        focus = str(plan.get("visual_focus") or "").strip().lower()
        if sentence_type in {"cta", "transition"} or focus == "transition":
            return "transition"
        if sentence_type in {"title", "intro"}:
            return sentence_type
        if focus == "metaphor_broll" or sentence_type in {"emotion", "abstract", "summary"}:
            return "abstract"
        return "story"

    @classmethod
    def _v2_story_fail_closed(cls, plan: dict) -> bool:
        if bool(plan.get("story_soft_fallback")):
            return False
        return cls._v2_chunk_type(plan) == "story" and bool(cls._v2_hard_must_show_anchors(plan))

    @staticmethod
    def _v2_mark_fallback_blocked(plan: dict, reason: str) -> None:
        metrics = plan.setdefault("_v2_detection_metrics", {})
        metrics["fallback_blocked"] = True
        metrics["fallback_block_reason"] = reason
        metrics["selection_status"] = "needs_manual_asset"

    @classmethod
    def _v2_needs_manual_asset_outcome(cls, plan: dict, idx: int, reason: str) -> ChunkOutcome:
        cls._v2_mark_fallback_blocked(plan, reason)
        metrics = dict(plan.get("_v2_detection_metrics") or {})
        metrics.setdefault("hard_anchors", cls._v2_hard_must_show_anchors(plan))
        metrics.setdefault("chunk_type", cls._v2_chunk_type(plan))
        metrics.setdefault("acceptance_policy", "fail_closed")
        return ChunkOutcome(
            chunk_index=idx, plan=plan, local_path=None,
            duration=0, source="", source_id="", source_url="",
            resolution="", score=0.0, decision="needs_manual_asset",
            cost_usd=0.0,
            error=reason,
            used_level="needs_manual_asset",
            verify_scores=metrics,
            cascade_trace={"v2_detection": metrics},
        )

    @staticmethod
    def _v2_candidate_meta_without_query(cand: "FootageCandidate") -> str:
        meta = " ".join([
            str(getattr(cand, "source_tags", "") or ""),
            str(getattr(cand, "source_url", "") or ""),
        ]).lower()
        query = str(getattr(cand, "query", "") or "").lower().strip()
        if query:
            meta = meta.replace(query, " ")
        return " ".join(meta.split())

    @staticmethod
    def _v2_candidate_anchor_proof_text(cand: "FootageCandidate") -> str:
        return Orchestrator._v2_candidate_meta_without_query(cand).strip()

    @staticmethod
    def _v2_plan_is_retail(plan: dict) -> bool:
        text = " ".join(
            [str(plan.get("domain") or "")]
            + [str(plan.get("sentence") or "")]
            + [str(plan.get("text") or "")]
            + [str(x) for x in (plan.get("must_show") or [])]
            + [str(x) for x in (plan.get("queries") or [])]
            + [str(x) for x in (plan.get("domain_motif_pool") or [])]
        ).lower()
        return any(term in text for term in (
            "convenience-store", "convenience store", "supermarket", "grocery",
            "retail", "store shelf", "retail shelf", "product shelf",
            "refrigerated display case", "cola", "coke", "yogurt", "milk",
            "dairy", "expiry", "expiration", "discount tag", "inventory shelf",
        ))

    @staticmethod
    def _v2_retail_candidate_mismatch_reason(proof_text: str) -> Optional[str]:
        bad_terms = (
            "bookshelf", "book shelf", "library shelf", "cinema seats",
            "theater seats", "subway train", "metro train", "swing set",
            "broken glass", "cracked glass", "ice crack texture",
            "abstract ink", "liquid paint", "paint wallpaper", "watercolor",
            "parallax", "live wallpaper", "lava", "fire flames", "flame background",
            "turntable", "vinyl", "record", "audio", "music track",
            "clothing store", "fashion", "apparel", "boutique", "dress",
            "shirt", "hanger", "textile", "wardrobe", "handshake",
            "partnership agreement", "business meeting",
        )
        for term in bad_terms:
            if term in proof_text:
                return f"retail_wrong_world:{term}"
        if "abstract" in proof_text and not any(
            term in proof_text for term in ("retail", "store", "supermarket", "grocery")
        ):
            return "retail_wrong_world:abstract"
        return None

    @classmethod
    def _v2_retail_degraded_score(
        cls,
        plan: dict,
        candidate_text: str,
        anchors: set[str],
        exact_hits: int,
    ) -> int:
        """Same-world fallback score for retail scripts.

        Hard retail details such as expiry-date labels and ice packs are often
        sparse in stock. If exact proof is missing, degrade only to a real
        retail/store/product shelf, never to a word-only match like vinyl
        "label", clothing-store "retail", or abstract "space".
        """
        if not cls._v2_plan_is_retail(plan):
            return 0
        if cls._v2_retail_candidate_mismatch_reason(candidate_text):
            return 0

        def has_any(text: str, terms: tuple[str, ...]) -> bool:
            return any(term in text for term in terms)

        retail_terms = (
            "convenience store", "supermarket", "grocery", "retail shelf",
            "store shelf", "product shelf", "products on shelf",
            "packaged goods", "packaged food", "food aisle", "gas station food",
            "refrigerated", "refrigerator", "cooler", "fridge", "dairy shelf",
            "cola", "coca cola", "coke", "soft drink", "yogurt", "yoghurt",
            "milk", "food label", "nutrition facts", "price tag",
            "discount tag", "yellow tag", "checkout counter",
            "inventory", "stocking shelf", "snack bags", "packaged snack",
        )
        if not has_any(candidate_text, retail_terms):
            return 0

        if anchors & {"cola", "coke", "bottles"}:
            return 90 + exact_hits * 5 if has_any(
                candidate_text, ("cola", "coca cola", "coke", "soft drink", "drink bottle")
            ) else 35 + exact_hits * 3
        if anchors & {"yogurt", "yoghurt", "milk", "dairy", "cartons"}:
            return 85 + exact_hits * 5 if has_any(
                candidate_text, ("yogurt", "yoghurt", "milk", "dairy", "refrigerated", "cooler", "fridge")
            ) else 35 + exact_hits * 3
        if anchors & {"packaged", "fried", "snack", "mahua"}:
            return 80 + exact_hits * 5 if has_any(
                candidate_text, ("packaged snack", "snack bags", "chips and snacks", "checkout counter", "gas station food")
            ) else 35 + exact_hits * 3
        if anchors & {"date", "label", "expiry"}:
            return 78 + exact_hits * 5 if has_any(
                candidate_text, ("expiry", "expiration", "date label", "food label", "nutrition facts", "best before", "price tag")
            ) else 35 + exact_hits * 3
        if anchors & {"discount", "tag"}:
            return 76 + exact_hits * 5 if has_any(
                candidate_text, ("discount tag", "yellow tag", "yellow label", "price tag")
            ) else 35 + exact_hits * 3
        if anchors & {"refrigerated", "display", "case", "cooler"}:
            return 78 + exact_hits * 5 if has_any(
                candidate_text, ("refrigerated", "refrigerator", "cooler", "fridge", "dairy shelf")
            ) else 35 + exact_hits * 3
        if anchors & {"inventory"}:
            return 65 + exact_hits * 5 if has_any(
                candidate_text, ("inventory", "stocking shelf", "warehouse storage", "store shelf")
            ) else 35 + exact_hits * 3
        return 55 + exact_hits * 5

    @staticmethod
    def _v2_locale_guardrail_reject(cand: "FootageCandidate", plan: dict) -> Optional[str]:
        meta = Orchestrator._v2_candidate_meta_without_query(cand)
        if not meta:
            return None
        plan_text = " ".join(
            [str(plan.get("domain") or "")]
            + [str(plan.get("sentence") or "")]
            + [str(plan.get("text") or "")]
            + [str(q) for q in (plan.get("queries") or [])]
            + [str(q) for q in (plan.get("must_show") or [])]
            + [str(q) for q in (plan.get("domain_visual_pack") or [])]
            + [str(q) for q in (plan.get("domain_motif_pool") or [])]
        ).lower()
        locale_groups = [
            ({"mumbai", "bombay", "india", "indian"}, (
                "china", "chinese", "taiwan", "taipei", "hong kong",
                "japan", "japanese", "korea", "korean", "thailand",
                "bangkok", "vietnam", "vietnamese", "singapore",
            )),
            ({"hanoi", "vietnam", "vietnamese", "dong", "phin"}, (
                "india", "indian", "mumbai", "japan", "japanese", "tokyo",
                "ueno", "turkey", "turkish", "istanbul", "thailand", "thai",
                "khao yai", "china", "chinese", "georgia", "mtskheta",
            )),
            ({"istanbul", "turkey", "turkish", "lira"}, (
                "india", "indian", "mumbai", "vietnam", "vietnamese", "hanoi",
                "japan", "japanese", "tokyo", "ueno", "thailand", "thai",
                "khao yai", "georgia", "mtskheta", "morocco", "marrakech",
                "tunisia", "tunis",
            )),
        ]
        for positives, negatives in locale_groups:
            if not any(term in plan_text for term in positives):
                continue
            if any(term in meta for term in positives):
                return None
            for bad in negatives:
                if bad in meta:
                    return f"locale_mismatch:{bad}"
        return None

    @staticmethod
    def _v2_anchor_guardrail_reject(cand: "FootageCandidate", plan: dict) -> Optional[str]:
        meta = Orchestrator._v2_candidate_meta_without_query(cand)
        if not meta:
            return None
        anchors = Orchestrator._v2_hard_must_show_anchors(plan)
        if not anchors:
            return None
        proof_text = Orchestrator._v2_candidate_anchor_proof_text(cand)
        anchor_set = set(anchors)
        retail_anchor_group = {
            "retail", "store", "shelf", "grocery", "supermarket",
            "convenience", "product", "products",
        }
        strict_anchor_words = retail_anchor_group | {
            "refrigerated", "display", "case", "cooler", "cola", "coke",
            "bottles", "yogurt", "yoghurt", "milk", "dairy", "cartons",
            "date", "label", "expiry", "discount", "tag", "packaged",
            "fried", "snack", "mahua", "inventory",
        }
        if Orchestrator._v2_plan_is_retail(plan):
            retail_mismatch = Orchestrator._v2_retail_candidate_mismatch_reason(proof_text)
            if retail_mismatch:
                return retail_mismatch
        if not (anchor_set & strict_anchor_words):
            if any(anchor and anchor in proof_text for anchor in anchors):
                return None
        anchor_groups = [
            (
                retail_anchor_group,
                (
                    "convenience store", "supermarket", "grocery", "retail",
                    "store shelf", "product shelf", "products on shelf",
                    "packaged goods", "food aisle", "dairy shelf",
                ),
            ),
            (
                {"refrigerated", "display", "case", "cooler"},
                ("refrigerated display", "cooler", "fridge", "refrigerator", "dairy shelf"),
            ),
            (
                {"cola", "coke", "bottles"},
                ("cola", "coca cola", "coke", "soft drink", "drink bottle"),
            ),
            (
                {"yogurt", "yoghurt", "milk", "dairy", "cartons"},
                ("yogurt", "yoghurt", "milk", "dairy", "carton", "refrigerated"),
            ),
            (
                {"date", "label", "expiry"},
                ("expiry", "expiration", "date label", "food label", "nutrition facts", "best before"),
            ),
            (
                {"discount", "tag"},
                ("discount tag", "yellow tag", "yellow label", "price tag"),
            ),
            (
                {"packaged", "fried", "snack", "mahua"},
                (
                    "packaged snack", "fried snack", "checkout counter",
                    "convenience store", "supermarket", "grocery", "retail shelf",
                ),
            ),
            (
                {"inventory"},
                ("inventory", "stocking shelf", "shop owner checking", "store shelf"),
            ),
            (
                {"customer", "line", "queue"},
                ("customer", "customers", "queue", "line", "waiting", "crowd", "buying", "customer queue", "customer buying"),
            ),
            (
                {"cash", "money", "rupee", "lira", "dong"},
                ("cash", "money", "rupee", "lira", "dong", "currency", "banknote", "banknotes", "paying", "cash handling", "rupee cash"),
            ),
            (
                {"syrup", "honey"},
                ("syrup", "honey", "jaggery", "sweetener", "caramel", "sugar syrup drink", "syrup preparation"),
            ),
            (
                {"kettle", "pot"},
                ("kettle", "pot", "vessel", "cooking pot"),
            ),
            (
                {"cup", "glass"},
                ("cup", "glass", "glass cup", "iced coffee", "condensed milk"),
            ),
            (
                {"coffee", "phin", "milk", "ice"},
                ("coffee", "iced coffee", "phin filter", "condensed milk", "ice", "cup"),
            ),
            (
                {"chestnut", "pan", "bag", "bags"},
                ("chestnut", "roasted chestnut", "roasting food", "pan", "paper bag", "stacked bags", "scooping", "steam"),
            ),
            (
                {"sugarcane", "juice"},
                ("sugarcane", "sugarcane juice", "juice extraction", "glass cup"),
            ),
        ]
        for group, proofs in anchor_groups:
            if anchor_set & group and any(term in proof_text for term in proofs):
                return None
        # HARD-reject on a missing must_show anchor ONLY for retail plans, where the
        # strict convenience-store anchor→proof mapping above is tuned and reliable.
        # For every other topic this literal anchor matching was rejecting on-theme
        # footage whose tags merely lacked the anchor WORD (a "coffee shop interior"
        # tagged "cafe counter pastries" → missing_hard_anchor:coffee), collapsing
        # stays a soft ranking preference, not a hard gate. (LLM-as-director: thin
        # safety guardrails only, no heavy per-topic rule packs.)
        if Orchestrator._v2_plan_is_retail(plan):
            return "missing_hard_anchor:" + ",".join(anchors[:3])
        return None

    @staticmethod
    def _v2_candidate_wrong_world_or_locale_reject(cand: "FootageCandidate", plan: dict) -> Optional[str]:
        try:
            from backend.lib.content_safety_filter import asset_is_unsafe
            unsafe = asset_is_unsafe(
                getattr(cand, "source_tags", "") or getattr(cand, "query", "") or "",
                getattr(cand, "source_url", "") or "",
            )
            if unsafe:
                return unsafe
        except Exception:
            pass
        try:
            from backend.lib.visual_guardrails import asset_wrong_world
        except Exception:
            return Orchestrator._v2_locale_guardrail_reject(cand, plan)
        pol = plan.get("short_video_visual_policy") or {}
        subject_text = " ".join(
            [str(pol.get("topic_anchor") or "")]
            + [str(q) for q in (plan.get("queries") or [])]
            + [str(q) for q in (plan.get("must_show") or [])]
            + [str(q) for q in (plan.get("domain_motif_pool") or [])]
        )
        wrong_world = asset_wrong_world(
            source_tags=getattr(cand, "source_tags", "") or "",
            source_url=getattr(cand, "source_url", "") or "",
            subject_text=subject_text,
            forbidden_pack=plan.get("forbidden_visual_pack") or [],
        )
        if wrong_world:
            return wrong_world
        return Orchestrator._v2_locale_guardrail_reject(cand, plan)

    @staticmethod
    def _v2_guardrail_reject(cand: "FootageCandidate", plan: dict) -> Optional[str]:
        """Quality-inspect a just-accepted candidate: return a short reason if
        its metadata points at the WRONG visual world for this video (so the
        caller falls through to the next candidate), else None. The LLM stays
        the director — this only rejects, never injects."""
        # Content-safety backstop: reject sexual / violent / gory clips that
        # slipped past provider safe-search or came from cache/library.
        try:
            from backend.lib.content_safety_filter import asset_is_unsafe
            unsafe = asset_is_unsafe(
                getattr(cand, "source_tags", "") or getattr(cand, "query", "") or "",
                getattr(cand, "source_url", "") or "",
            )
            if unsafe:
                return unsafe
        except Exception:
            pass
        try:
            from backend.lib.visual_guardrails import asset_wrong_world
        except Exception:
            return None
        pol = plan.get("short_video_visual_policy") or {}
        subject_text = " ".join(
            [str(pol.get("topic_anchor") or "")]
            + [str(q) for q in (plan.get("queries") or [])]
            + [str(q) for q in (plan.get("must_show") or [])]
        )
        wrong_world = asset_wrong_world(
            source_tags=getattr(cand, "source_tags", "") or getattr(cand, "query", "") or "",
            source_url=getattr(cand, "source_url", "") or "",
            subject_text=subject_text,
            forbidden_pack=plan.get("forbidden_visual_pack") or [],
        )
        if wrong_world:
            return wrong_world
        return (
            Orchestrator._v2_locale_guardrail_reject(cand, plan)
            or Orchestrator._v2_anchor_guardrail_reject(cand, plan)
        )

    @staticmethod
    def _v2_domain_terms(plan: dict) -> set[str]:
        stop = {
            "the", "and", "with", "for", "from", "into", "onto", "that", "this",
            "shot", "stock", "clip", "video", "image", "photo", "scene", "view",
            "close", "wide", "broll", "footage", "documentary",
        }
        values: list[Any] = []
        for key in ("domain_visual_pack", "domain_motif_pool", "queries", "must_show"):
            raw = plan.get(key)
            if isinstance(raw, (list, tuple, set)):
                values.extend(raw)
            elif raw:
                values.append(raw)
        terms: set[str] = set()
        for value in values:
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", str(value).lower()):
                if token not in stop:
                    terms.add(token)
        return terms

    @staticmethod
    def _v2_forbidden_terms(plan: dict) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for key in ("forbidden_visual_pack", "must_not_show"):
            raw = plan.get(key) or []
            if not isinstance(raw, (list, tuple, set)):
                raw = [raw]
            for item in raw:
                text = " ".join(str(item or "").strip().lower().split())
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        return out

    @classmethod
    def _v2_same_domain_score(cls, cand: FootageCandidate, plan: dict) -> int:
        """Metadata-only score for emergency same-domain salvage.

        Candidate.query is intentionally excluded from positive scoring because
        every result for a query inherits the same query text. A train returned
        for "mumbai street food vendor" must not pass merely because the query
        contains Mumbai/vendor words. If provider metadata is absent, fall back
        to a weak query score so legacy adapters still have a path.
        """
        meta = cls._v2_candidate_meta_without_query(cand)
        query = str(getattr(cand, "query", "") or "").lower()
        haystack = " ".join([meta, query])
        for forbidden in cls._v2_forbidden_terms(plan):
            if forbidden and forbidden in haystack:
                return 0
        plan_text = " ".join([
            str(plan.get("domain") or ""),
            " ".join(str(q) for q in (plan.get("queries") or [])),
            " ".join(str(q) for q in (plan.get("domain_visual_pack") or [])),
            " ".join(str(q) for q in (plan.get("domain_motif_pool") or [])),
        ]).lower()
        if any(term in plan_text for term in ("mumbai", "india", "indian", "street_food_mumbai")):
            if not any(term in meta for term in ("mumbai", "india", "indian")):
                return 0
        terms = cls._v2_domain_terms(plan)
        if not terms:
            return 0
        score = sum(1 for term in terms if term in meta)
        if score == 0 and not meta.strip():
            score = 1 if any(term in query for term in terms) else 0
        return score

    @staticmethod
    def _v2_candidate_rejected_by_observer(cand: FootageCandidate, plan: dict) -> Optional[str]:
        metrics = plan.get("_v2_detection_metrics") or {}
        for verdict in metrics.get("candidate_verdicts") or []:
            if str(verdict.get("source") or "") != str(cand.source or ""):
                continue
            if str(verdict.get("source_id") or "") != str(cand.source_id or ""):
                continue
            if str(verdict.get("verdict") or "") in {"reject", "reject_wrong_world"}:
                return str(verdict.get("reason") or "observer_reject")
        return None

    @classmethod
    def _v2_pick_same_domain_candidate(
        cls, candidates: list[FootageCandidate], plan: dict,
    ) -> Optional[FootageCandidate]:
        best: Optional[FootageCandidate] = None
        best_score = 0
        for cand in candidates or []:
            if cls._v2_candidate_rejected_by_observer(cand, plan):
                continue
            if cls._v2_guardrail_reject(cand, plan):
                continue
            score = cls._v2_same_domain_score(cand, plan)
            if score > best_score:
                best = cand
                best_score = score
        return best if best_score >= 2 else None

    def _v2_literal_domain_salvage(
        self,
        plan: dict,
        candidates: list[FootageCandidate],
        chunk_dir: Path,
        orientation: str,
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        cand = self._v2_pick_same_domain_candidate(candidates, plan)
        if cand is None:
            return None
        metrics = plan.setdefault("_v2_detection_metrics", {})
        metrics["observer_all_rejected"] = True
        metrics["best_same_domain_candidate"] = {
            "source": cand.source,
            "source_id": cand.source_id,
            "query": getattr(cand, "query", "") or "",
            "score": self._v2_same_domain_score(cand, plan),
        }
        outcome = self._v2_download_outcome(
            plan, cand, chunk_dir, orientation,
            used_level="L_literal_domain_salvage",
            verify_reason=(
                "literal same-domain salvage: observer rejected all literal "
                "candidates; keeping the best same-domain candidate instead "
                "of falling to unrelated motif/ship fallback"
            ),
            score=4.0,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if outcome is None:
            return None
        outcome.semantic_pass_source = "literal_domain_salvage"
        outcome.confidence = 0.45
        outcome.fallback_reason = "observer_all_rejected_literal_same_domain_salvage"
        outcome.verify_scores.update({
            "literal_domain_salvage": 1,
            "observer_judge_accept": 0,
        })
        outcome.verify_test_results.append({
            "test_id": "literal_domain_salvage",
            "passed": True,
            "reason": "Same-domain literal stock beats unrelated fallback.",
        })
        outcome.cascade_trace["v2_detection"] = dict(metrics)
        logger.warning(
            "chunk %d v2 literal same-domain salvage used %s:%s query=%r",
            int(plan.get("chunk_index", -1)), cand.source, cand.source_id,
            getattr(cand, "query", "") or "",
        )
        return outcome

    @staticmethod
    def _v2_debug_candidate_extras(cand: FootageCandidate) -> dict:
        raw = getattr(cand, "_raw", None)
        extra = getattr(raw, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
        preview: dict[str, Any] = {}
        video_pictures = extra.get("video_pictures")
        if isinstance(video_pictures, list) and video_pictures:
            urls = [
                p.get("picture")
                for p in video_pictures
                if isinstance(p, dict) and isinstance(p.get("picture"), str)
            ]
            if len(urls) > 3:
                idxs = sorted({round((len(urls) - 1) * f) for f in (0.2, 0.5, 0.8)})
                urls = [urls[i] for i in idxs]
            if urls:
                preview["video_pictures"] = urls
        preview_video_url = extra.get("preview_video_url")
        if isinstance(preview_video_url, str) and preview_video_url:
            preview["preview_video_url"] = preview_video_url
        return {
            "thumbnail_url": getattr(cand, "thumbnail_url", "") or "",
            "query": getattr(cand, "query", "") or "",
            "source_tags": (getattr(cand, "source_tags", "") or "")[:420],
            "preview_assets": preview,
        }

    @staticmethod
    def _emit_short_observer_debug_report(plans, outcomes, output_dir) -> None:
        import html as _html
        import json as _json
        from collections import Counter as _Counter
        from pathlib import Path as _Path

        out = _Path(output_dir).parent.parent
        out.mkdir(parents=True, exist_ok=True)

        oc_by_idx = {
            int(getattr(oc, "chunk_index", -1)): oc
            for oc in outcomes if oc is not None
        }
        total = accept = reject = ambiguous = haiku_escalated = 0
        est_cost = 0.0
        observer_cache_hits = 0
        observer_chunk_cap_skips = 0
        observer_video_cap_skips = 0
        search_cache_hits = 0
        search_cache_misses = 0
        used_level_counts = _Counter()
        source_counts = _Counter()
        focus_counts = _Counter()
        duplicate_assets = _Counter()
        query_source_counts = _Counter()
        guardrail_rejects_total = 0
        subject_missing_count = 0
        degraded_chunk_idxs = []
        chunks_data = []
        version_stamp = Orchestrator._short_v2_version_stamp()

        for plan in plans:
            idx = int(plan.get("chunk_index", -1))
            metrics = plan.get("_v2_detection_metrics") or {}
            observer_cache_hits += int(metrics.get("observer_cache_hits", 0) or 0)
            observer_chunk_cap_skips += int(metrics.get("observer_chunk_cap_skips", 0) or 0)
            observer_video_cap_skips += int(metrics.get("observer_video_cap_skips", 0) or 0)
            search_cache_hits = max(search_cache_hits, int(metrics.get("search_cache_hits", 0) or 0))
            search_cache_misses = max(search_cache_misses, int(metrics.get("search_cache_misses", 0) or 0))
            verdicts = list(metrics.get("candidate_verdicts") or [])
            oc = oc_by_idx.get(idx)
            used_level = (getattr(oc, "used_level", "") if oc else "") or "(none)"
            source = (getattr(oc, "source", "") if oc else "") or "(none)"
            source_id = (getattr(oc, "source_id", "") if oc else "") or ""
            used_level_counts[used_level] += 1
            source_counts[source] += 1
            focus = str(plan.get("visual_focus") or "(unset)")
            focus_counts[focus] += 1
            if source and source_id:
                duplicate_assets[f"{source}:{source_id}"] += 1
            for v in verdicts:
                total += 1
                verdict = str(v.get("verdict") or "").lower()
                if verdict == "accept":
                    accept += 1
                elif verdict == "reject":
                    reject += 1
                else:
                    ambiguous += 1
                if v.get("escalated"):
                    haiku_escalated += 1
                est_cost += float(v.get("est_cost_usd") or 0.0)
            qsrc = str(plan.get("query_source") or "")
            query_source_counts[qsrc or "(none)"] += 1
            gw_rej = int(metrics.get("guardrail_rejects", 0) or 0)
            guardrail_rejects_total += gw_rej
            if plan.get("query_degradation_level") == "domain_repaired":
                degraded_chunk_idxs.append(idx)
            # subject-missing: a concrete (non pool-first) sentence that ended up
            # with no real query at all → no on-subject footage was searched.
            if not bool(plan.get("use_pool_first")) and not (plan.get("queries") or []):
                subject_missing_count += 1
            chunks_data.append({
                "idx": idx,
                "sentence": plan.get("sentence") or plan.get("text") or "",
                "visual_focus": focus,
                "guardrail_rejects": gw_rej,
                "domain": plan.get("domain") or "",
                "domain_motif_pool": plan.get("domain_motif_pool") or [],
                "forbidden_visual_pack": plan.get("forbidden_visual_pack") or [],
                "query_source": plan.get("query_source") or "",
                "query_degradation_level": plan.get("query_degradation_level") or "",
                "must_show": plan.get("must_show") or [],
                "hard_anchors": Orchestrator._v2_hard_must_show_anchors(plan),
                "chunk_type": Orchestrator._v2_chunk_type(plan),
                "acceptance_policy": (
                    "fail_closed" if Orchestrator._v2_story_fail_closed(plan) else "fail_open"
                ),
                "must_not_show": plan.get("must_not_show") or [],
                "queries": plan.get("queries") or [],
                "metaphor_queries": plan.get("metaphor_queries") or [],
                "used_level": used_level,
                "source": source,
                "source_id": source_id,
                "observer_all_rejected": bool(metrics.get("observer_all_rejected", False)),
                "best_same_domain_candidate": metrics.get("best_same_domain_candidate"),
                "fallback_reason": getattr(oc, "fallback_reason", "") if oc else "",
                "adjacent_repeat_blocked": bool(getattr(oc, "adjacent_repeat_blocked", False)) if oc else False,
                "inherit_donor_distance": getattr(oc, "inherit_donor_distance", None) if oc else None,
                "verdicts": verdicts,
                "detection_est_cost_usd": metrics.get("detection_est_cost_usd", 0.0),
                "observer_cache_hits": metrics.get("observer_cache_hits", 0),
                "observer_chunk_cap_skips": metrics.get("observer_chunk_cap_skips", 0),
                "observer_video_cap_skips": metrics.get("observer_video_cap_skips", 0),
                "duplicate_asset_cap_skips": metrics.get("duplicate_asset_cap_skips", 0),
                "selection_status": metrics.get(
                    "selection_status",
                    "selected_asset" if oc and getattr(oc, "local_path", None) else getattr(oc, "decision", "unknown"),
                ),
                "fallback_blocked": bool(metrics.get("fallback_blocked", False)),
                "fallback_block_reason": metrics.get("fallback_block_reason", ""),
            })

        pass_rate = round((accept / total * 100.0), 1) if total else 0.0
        duplicate_count = sum(max(0, count - 1) for count in duplicate_assets.values())
        max_asset_occurrences = Orchestrator._v2_max_asset_occurrences()
        max_total_repeat_occurrences = Orchestrator._v2_max_total_repeat_occurrences()
        duplicate_over_cap = {
            key: count
            for key, count in duplicate_assets.items()
            if count > max_asset_occurrences
        }
        duplicate_over_cap_count = sum(
            max(0, count - max_asset_occurrences)
            for count in duplicate_assets.values()
        )
        duplicate_total_over_cap_count = max(
            0,
            duplicate_count - max_total_repeat_occurrences,
        )
        adjacent_same_asset_count = 0
        ordered = sorted(chunks_data, key=lambda c: int(c.get("idx", -1)))
        for prev, cur in zip(ordered, ordered[1:]):
            prev_key = f'{prev.get("source")}:{prev.get("source_id")}'
            cur_key = f'{cur.get("source")}:{cur.get("source_id")}'
            if prev.get("source_id") and cur.get("source_id") and prev_key == cur_key:
                adjacent_same_asset_count += 1
        domain_pack_used_count = sum(
            1 for c in chunks_data if c.get("query_source") == "domain_visual_pack"
        )
        generic_query_repaired_count = sum(
            1 for c in chunks_data if c.get("query_degradation_level") == "domain_repaired"
        )
        summary = {
            "total_candidates": total,
            "accept": accept,
            "reject": reject,
            "ambiguous": ambiguous,
            "pass_rate_pct": pass_rate,
            "gemini_calls_est": total - haiku_escalated,
            "haiku_escalated": haiku_escalated,
            "estimated_cost_usd": round(est_cost, 6),
            "real_cost_usd": None,
            "used_level_counts": dict(used_level_counts),
            "source_counts": dict(source_counts),
            "visual_focus_counts": dict(focus_counts),
            "max_asset_occurrences": max_asset_occurrences,
            "max_total_repeat_occurrences": max_total_repeat_occurrences,
            "duplicate_asset_reuse_count": duplicate_count,
            "duplicate_asset_over_cap_count": duplicate_over_cap_count,
            "duplicate_total_over_cap_count": duplicate_total_over_cap_count,
            "duplicate_assets_over_cap": duplicate_over_cap,
            "adjacent_same_asset_count": adjacent_same_asset_count,
            "domain_pack_used_count": domain_pack_used_count,
            "generic_query_repaired_count": generic_query_repaired_count,
            "query_source_counts": dict(query_source_counts),
            "rejected_forbidden_assets": guardrail_rejects_total,
            "subject_missing_count": subject_missing_count,
            "degraded_chunk_idxs": degraded_chunk_idxs,
            "observer_cache_hits": observer_cache_hits,
            "observer_chunk_cap_skips": observer_chunk_cap_skips,
            "observer_video_cap_skips": observer_video_cap_skips,
            "search_cache_hits": search_cache_hits,
            "search_cache_misses": search_cache_misses,
            "version_stamp": version_stamp,
        }
        selection_trace = {"version_stamp": version_stamp, "summary": summary, "chunks": chunks_data}
        (out / "observer_debug.json").write_text(
            _json.dumps(selection_trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "selection_trace.json").write_text(
            _json.dumps(selection_trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "visual_plan.json").write_text(
            _json.dumps({
                "version_stamp": version_stamp,
                "chunks": [
                    {
                        "idx": c["idx"],
                        "chunk_type": c["chunk_type"],
                        "text": c["sentence"],
                        "hard_anchors": c["hard_anchors"],
                        "soft_preferences": c.get("domain_motif_pool") or [],
                        "hard_forbidden": c.get("forbidden_visual_pack") or [],
                        "acceptance_policy": c["acceptance_policy"],
                    }
                    for c in chunks_data
                ],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "version_stamp.txt").write_text(
            "\n".join(f"{k}={v}" for k, v in version_stamp.items()) + "\n",
            encoding="utf-8",
        )

        esc = _html.escape
        rows = []
        for c in chunks_data:
            verdict_rows = []
            for v in c["verdicts"]:
                color = {
                    "accept": "#1a7f37",
                    "reject": "#cf222e",
                    "ambiguous": "#9a6700",
                }.get(str(v.get("verdict") or "").lower(), "#57606a")
                thumb = str(v.get("thumbnail_url") or "")
                img = f'<img src="{esc(thumb)}" loading="lazy">' if thumb else '<div class="noimg">no thumbnail</div>'
                verdict_rows.append(
                    f'<div class="candidate">{img}<div><b style="color:{color}">'
                    f'{esc(str(v.get("verdict") or "?"))}</b>'
                    f'<div class="meta">{esc(str(v.get("source") or ""))} · {esc(str(v.get("mode") or ""))} · '
                    f'{esc(str(v.get("model_id") or ""))}</div>'
                    f'<div class="meta">query: {esc(str(v.get("query") or ""))}</div>'
                    f'<div class="reason">{esc(str(v.get("reason") or ""))}</div></div></div>'
                )
            rows.append(
                f'<section><h3>#{c["idx"]} · {esc(c["used_level"])} · {esc(c["visual_focus"])}</h3>'
                f'<p>{esc(str(c["sentence"]))}</p>'
                f'<div class="meta">source: {esc(c["source"])}:{esc(c["source_id"])} · '
                f'fallback: {esc(str(c["fallback_reason"]))}</div>'
                f'<div class="meta">chunk_type: {esc(str(c["chunk_type"]))} · policy: {esc(str(c["acceptance_policy"]))} · '
                f'status: {esc(str(c["selection_status"]))}</div>'
                f'<div class="meta">hard_anchors: {esc(str(c["hard_anchors"]))} · must_show: {esc(str(c["must_show"]))}</div>'
                f'<div class="meta">fallback_blocked: {esc(str(c["fallback_blocked"]))} · '
                f'{esc(str(c["fallback_block_reason"]))}</div>'
                f'<div class="meta">queries: {esc(str(c["queries"]))}</div>'
                f'{"".join(verdict_rows)}</section>'
            )
        html = (
            '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<title>Short Observer Debug</title><style>'
            'body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f8fa;color:#1f2328;margin:0}'
            '.wrap{max-width:1180px;margin:0 auto;padding:20px}.summary,section{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px;margin-bottom:14px}'
            '.summary b{font-size:22px}.meta{color:#57606a;font-size:12px;margin:4px 0}.candidate{display:flex;gap:10px;border-top:1px solid #d8dee4;padding:8px 0}'
            '.candidate img,.noimg{width:160px;height:90px;object-fit:cover;border-radius:6px;background:#ffebe9;color:#cf222e;display:flex;align-items:center;justify-content:center;font-size:12px}'
            '.reason{font-size:13px;white-space:pre-wrap;margin-top:6px}'
            '</style></head><body><div class="wrap">'
            f'<div class="summary"><b>Short observer debug</b>'
            f'<div class="meta">pass {pass_rate}% · accept {accept}/{total} · reject {reject} · ambiguous {ambiguous}</div>'
            f'<div class="meta">Gemini calls est: {summary["gemini_calls_est"]} · Haiku escalated: {haiku_escalated} · '
            f'est cost ${summary["estimated_cost_usd"]:.6f} · duplicate reuse {duplicate_count}</div>'
            f'<div class="meta">commit_sha: {esc(version_stamp["commit_sha"])} · rule_version: {esc(version_stamp["rule_version"])} · '
            f'worker_root: {esc(version_stamp["worker_root"])}</div>'
            f'<div class="meta">python_path: {esc(version_stamp["python_path"])}</div>'
            f'<div class="meta">used levels: {esc(str(summary["used_level_counts"]))}</div>'
            f'<div class="meta">sources: {esc(str(summary["source_counts"]))}</div></div>'
            + "".join(rows)
            + "</div></body></html>"
        )
        (out / "observer_debug.html").write_text(html, encoding="utf-8")
        logger.warning(
            "short observer_debug written: %s (pass_rate=%.1f%% accept=%d/%d haiku=%d)",
            out / "observer_debug.html", pass_rate, accept, total, haiku_escalated,
        )

    def _v2_select_chunk(
        self,
        plan: dict,
        output_dir: Path,
        orientation: str,
        top_n: int,
        motif_pool: list[str],
        budget: HaikuReviewBudget,
        used_source_ids: set[str],
        *,
        used_source_lock: Optional[threading.Lock] = None,
        observer_cache: Optional[dict[tuple[Any, ...], Any]] = None,
        observer_cache_lock: Optional[threading.Lock] = None,
        observer_budget: Optional[dict[str, int]] = None,
        observer_budget_lock: Optional[threading.Lock] = None,
    ) -> ChunkOutcome:
        idx = int(plan["chunk_index"])
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)


        literal = probe_literal_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates(
            plan, literal, chunk_dir, orientation,
            mode="literal", used_level="L_literal", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
            observer_cache=observer_cache,
            observer_cache_lock=observer_cache_lock,
            observer_budget=observer_budget,
            observer_budget_lock=observer_budget_lock,
        )
        if selected is not None:
            return selected
        literal_salvage = self._v2_literal_domain_salvage(
            plan, literal, chunk_dir, orientation,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if literal_salvage is not None:
            return literal_salvage

        # failed — usually because the subject is a niche term our stock library
        # lacks and it keyword-collides ('kangaroo rat'->real kangaroos, 'dog
        # listening'->boy with headphones). The director cannot know our inventory,
        # so broaden HERE, informed by the failed search, into a generic category
        broadened = self._v2_broadened_subject_candidates(
            plan, orientation, top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates(
            plan, broadened, chunk_dir, orientation,
            mode="similar_subject", used_level="L_broadened_subject", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
            observer_cache=observer_cache,
            observer_cache_lock=observer_cache_lock,
            observer_budget=observer_budget,
            observer_budget_lock=observer_budget_lock,
        )
        if selected is not None:
            return selected

        # only in the final ship-pass, right before a hard-anchor story chunk would
        # otherwise FAIL the whole video — NOT here in mid-cascade (which would

        metaphor = probe_metaphor_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates(
            plan, metaphor, chunk_dir, orientation,
            mode="metaphor", used_level="L_metaphor", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
            observer_cache=observer_cache,
            observer_cache_lock=observer_cache_lock,
            observer_budget=observer_budget,
            observer_budget_lock=observer_budget_lock,
        )
        if selected is not None:
            return selected

        similar_subject = probe_similar_subject_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates(
            plan, similar_subject, chunk_dir, orientation,
            mode="similar_subject", used_level="L_similar_subject", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
            observer_cache=observer_cache,
            observer_cache_lock=observer_cache_lock,
            observer_budget=observer_budget,
            observer_budget_lock=observer_budget_lock,
        )
        if selected is not None:
            return selected

        similar_category = probe_similar_category_candidates(
            self.footage, plan, motif_pool, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates(
            plan, similar_category, chunk_dir, orientation,
            mode="similar_category", used_level="L_similar_category", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
            observer_cache=observer_cache,
            observer_cache_lock=observer_cache_lock,
            observer_budget=observer_budget,
            observer_budget_lock=observer_budget_lock,
        )
        if selected is not None:
            return selected

        motif = probe_motif_candidate(
            self.footage, motif_pool, plan, orientation=orientation,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        if motif is not None:
            gw_reason = self._v2_guardrail_reject(motif, plan)
            if gw_reason:
                metrics = plan.setdefault("_v2_detection_metrics", {})
                metrics["guardrail_rejects"] = int(metrics.get("guardrail_rejects", 0)) + 1
                metrics.setdefault("candidate_verdicts", []).append({
                    "mode": "motif",
                    "source": motif.source,
                    "source_id": motif.source_id,
                    "verdict": "reject_wrong_world",
                    "model_id": "short_v2_guardrail",
                    "elapsed_seconds": 0.0,
                    "est_cost_usd": 0.0,
                    "escalated": False,
                    "cache_hit": False,
                    "reason": ("guardrail " + gw_reason)[:240],
                    **self._v2_debug_candidate_extras(motif),
                })
                motif = None
        # must_show 地板:motif 兜底是"无判官信任"路径,至少要求候选标签命中 must_show
        # (如 rats),否则就是无人把关的跳题镜头(蜜蜂)。命中不了 → 丢弃,落到下面的
        # stock_miss → 老鼠继承/留空,绝不上跳题素材。
        if motif is not None and not self._v2_candidate_matches_must_show(motif, plan):
            metrics = plan.setdefault("_v2_detection_metrics", {})
            metrics.setdefault("candidate_verdicts", []).append({
                "mode": "motif",
                "source": motif.source,
                "source_id": motif.source_id,
                "verdict": "reject_must_show_floor",
                "model_id": "short_v2_must_show_floor",
                "elapsed_seconds": 0.0,
                "est_cost_usd": 0.0,
                "escalated": False,
                "cache_hit": False,
                "reason": "motif 兜底未命中 must_show,丢弃避免跳题",
                **self._v2_debug_candidate_extras(motif),
            })
            motif = None
        if motif is not None:
            selected = self._v2_download_outcome(
                plan, motif, chunk_dir, orientation,
                used_level="L_motif",
                verify_reason="motif_pool rescue: trusted high-availability stock query",
                score=5.0,
                used_source_ids=used_source_ids,
                used_source_lock=used_source_lock,
            )
            if selected is not None:
                return selected

        return ChunkOutcome(
            chunk_index=idx, plan=plan, local_path=None,
            duration=0, source="", source_id="", source_url="",
            resolution="", score=0.0, decision="stock_miss",
            cost_usd=0.0,
            error="v2: literal, metaphor, similar, and motif search failed",
            verify_scores=dict(plan.get("_v2_detection_metrics") or {}),
        )

    @staticmethod
    def _v2_attach_narration_context(plans: list[dict]) -> None:
        """Give each chunk plan a small read-only narration window (previous two
        lines + next line + position) under ``_narration_ctx``. The FLUX
        generated-footage director reads this to place the shot in the right
        scene/era/age/mood — e.g. tell a biography's childhood beat apart from its
        present-day beat. Computed once before parallel selection, so it is safe to
        read from worker threads."""
        sents = [
            str(p.get("sentence") or p.get("text") or "").strip() for p in plans
        ]
        total = len(sents)
        for i, p in enumerate(plans):
            p["_narration_ctx"] = {
                "index": i,
                "total": total,
                "prev": [s for s in sents[max(0, i - 2):i] if s],
                "next": sents[i + 1] if i + 1 < total else "",
            }


    def _v2_broadened_subject_candidates(
        self,
        plan: dict,
        orientation: str,
        top_n: int,
        *,
        used_source_ids: set[str],
    ) -> list[FootageCandidate]:
        """Search LLM-broadened generic queries for a niche subject whose exact
        stock pool failed. Gated by MEDIA_BUDDY_BROADEN_NICHE (default on); only
        runs after runtime_probe + literal levels miss, so the cost (one cheap
        text LLM call per distinct subject, cached) is paid only by hard chunks."""
        if os.environ.get(
            "MEDIA_BUDDY_BROADEN_NICHE", "1",
        ).strip().lower() in ("0", "false", "no", ""):
            return []
        queries = self._v2_broadened_queries_for_plan(plan)
        if not queries:
            return []
        try:
            return probe_broadened_subject_candidates(
                self.footage, plan, queries,
                orientation=orientation, top_n=top_n,
                used_source_ids=used_source_ids,
            )
        except Exception as e:
            logger.warning("broadened-subject probe failed: %s", e)
            return []

    def _v2_broadened_queries_for_plan(self, plan: dict) -> list[str]:
        """Lazily derive (and cache) broadened generic queries for this chunk's
        failed subject via LLMClient.broaden_subject_for_stock."""
        lead_query = ""
        for q in (plan.get("queries") or []):
            s = self._v2_flatten_text(q).strip()
            if s:
                lead_query = s
                break
        if not lead_query:
            lead_query = self._v2_flatten_text(
                (plan.get("must_show") or [""])[0]
            ).strip()
        if not lead_query:
            return []
        key = lead_query.lower()
        if key in self._broaden_cache:
            return self._broaden_cache[key]
        sentence = self._v2_flatten_text(
            plan.get("sentence") or plan.get("text") or ""
        )
        try:
            queries = self.llm.broaden_subject_for_stock(lead_query, sentence)
        except Exception as e:
            logger.warning("broaden_subject_for_stock failed for %r: %s", lead_query, e)
            queries = []
        self._broaden_cache[key] = queries
        if queries:
            logger.info("niche-broaden %r -> %s", lead_query, queries)
        return queries





    def _v2_review_candidates(
        self,
        plan: dict,
        candidates: list[FootageCandidate],
        chunk_dir: Path,
        orientation: str,
        *,
        mode: str,
        used_level: str,
        budget: HaikuReviewBudget,
        used_source_ids: Optional[set[str]] = None,
        used_source_lock: Optional[threading.Lock] = None,
        observer_cache: Optional[dict[tuple[Any, ...], Any]] = None,
        observer_cache_lock: Optional[threading.Lock] = None,
        observer_budget: Optional[dict[str, int]] = None,
        observer_budget_lock: Optional[threading.Lock] = None,
    ) -> Optional[ChunkOutcome]:
        metrics = plan.setdefault("_v2_detection_metrics", {
            "detection_calls": 0,
            "detection_elapsed_seconds": 0.0,
            "detection_est_cost_usd": 0.0,
            "candidate_verdicts": [],
        })
        max_calls_per_chunk = self._short_v2_max_observer_per_chunk()
        max_calls_per_video = self._short_v2_max_observer_per_video()
        adaptive = self._short_v2_observer_adaptive()
        base_calls = self._short_v2_observer_base()
        close_fit = self._short_v2_observer_close_fit()
        use_floor = self._short_v2_observer_use_floor()
        best_reject: Optional[tuple[float, Any, Any]] = None  # (fit, cand, result) closest non-accept
        candidates = self._v2_prioritize_anchor_candidates(plan, candidates)
        for i, cand in enumerate(candidates):
            if cand.source == "library":
                logger.warning(
                    "short v2 ignored local library candidate %s:%s",
                    cand.source, cand.source_id,
                )
                continue
            review_plan = plan
            pre_gw_reason = self._v2_guardrail_reject(cand, plan)
            if pre_gw_reason:
                metrics["guardrail_rejects"] = int(metrics.get("guardrail_rejects", 0)) + 1
                metrics["guardrail_pregate_rejects"] = int(
                    metrics.get("guardrail_pregate_rejects", 0)
                ) + 1
                metrics.setdefault("candidate_verdicts", []).append({
                    "mode": mode,
                    "source": cand.source,
                    "source_id": cand.source_id,
                    "verdict": "reject_wrong_world",
                    "model_id": "short_v2_guardrail_pregate",
                    "elapsed_seconds": 0.0,
                    "est_cost_usd": 0.0,
                    "escalated": False,
                    "cache_hit": False,
                    "reason": ("guardrail " + pre_gw_reason)[:240],
                    **self._v2_debug_candidate_extras(cand),
                })
                continue
            cache_key = self._short_v2_observer_cache_key(cand, review_plan, mode)
            cached_result = None
            if observer_cache is not None:
                if observer_cache_lock is None:
                    cached_result = observer_cache.get(cache_key)
                else:
                    with observer_cache_lock:
                        cached_result = observer_cache.get(cache_key)
            cache_hit = cached_result is not None
            if cache_hit:
                result = cached_result
                metrics["observer_cache_hits"] = int(metrics.get("observer_cache_hits", 0)) + 1
            else:
                _dc = int(metrics.get("detection_calls", 0) or 0)
                _stop = _dc >= max_calls_per_chunk  # hard per-chunk ceiling always wins
                if not _stop and adaptive and _dc >= base_calls:
                    # past the cheap base budget: keep going ONLY if a non-accept so
                    # far was CLOSE (worth one more shot); else it's all far off → stop.
                    _stop = not (best_reject is not None and best_reject[0] >= close_fit)
                if _stop:
                    metrics["observer_chunk_cap_skips"] = int(
                        metrics.get("observer_chunk_cap_skips", 0)
                    ) + max(1, len(candidates) - i)
                    metrics.setdefault("candidate_verdicts", []).append({
                        "mode": mode,
                        "source": cand.source,
                        "source_id": cand.source_id,
                        "verdict": "skipped_observer_chunk_cap",
                        "model_id": "short_v2_budget_gate",
                        "elapsed_seconds": 0.0,
                        "est_cost_usd": 0.0,
                        "escalated": False,
                        "cache_hit": False,
                        "reason": f"per-chunk observer cap reached ({max_calls_per_chunk})",
                        **self._v2_debug_candidate_extras(cand),
                    })
                    break
                if not self._short_v2_try_reserve_observer_call(
                    observer_budget, observer_budget_lock, max_calls_per_video,
                ):
                    metrics["observer_video_cap_skips"] = int(
                        metrics.get("observer_video_cap_skips", 0)
                    ) + max(1, len(candidates) - i)
                    metrics.setdefault("candidate_verdicts", []).append({
                        "mode": mode,
                        "source": cand.source,
                        "source_id": cand.source_id,
                        "verdict": "skipped_observer_video_cap",
                        "model_id": "short_v2_budget_gate",
                        "elapsed_seconds": 0.0,
                        "est_cost_usd": 0.0,
                        "escalated": False,
                        "cache_hit": False,
                        "reason": f"per-video observer cap reached ({max_calls_per_video})",
                        **self._v2_debug_candidate_extras(cand),
                    })
                    break
                result = judge_candidate(
                    cand, review_plan, mode=mode,
                    is_last_candidate=(i == len(candidates) - 1),
                    budget=budget,
                )
                if observer_cache is not None:
                    if observer_cache_lock is None:
                        observer_cache[cache_key] = result
                    else:
                        with observer_cache_lock:
                            observer_cache[cache_key] = result
            image_count = 1 if getattr(cand, "thumbnail_url", "") else 0
            call_count = 0 if cache_hit else max(1, int(result.call_count or 1))
            metrics["detection_calls"] = int(metrics.get("detection_calls", 0)) + call_count
            metrics["detection_elapsed_seconds"] = round(
                float(metrics.get("detection_elapsed_seconds", 0.0))
                + (0.0 if cache_hit else float(result.elapsed_seconds or 0.0)),
                3,
            )
            metrics["detection_est_cost_usd"] = round(
                float(metrics.get("detection_est_cost_usd", 0.0))
                + (0.0 if cache_hit else float(result.est_cost_usd or 0.0)),
                6,
            )
            metrics["total_image_count"] = int(metrics.get("total_image_count", 0)) + image_count * call_count
            metrics["image_count_call_count"] = int(metrics.get("image_count_call_count", 0)) + call_count
            metrics.setdefault("candidate_verdicts", []).append({
                "mode": mode,
                "source": cand.source,
                "source_id": cand.source_id,
                "verdict": result.verdict,
                "model_id": result.model_id,
                "elapsed_seconds": 0.0 if cache_hit else result.elapsed_seconds,
                "est_cost_usd": 0.0 if cache_hit else result.est_cost_usd,
                "escalated": bool(result.escalated),
                "cache_hit": bool(cache_hit),
                "reason": (result.reason or "")[:240],
                **self._v2_debug_candidate_extras(cand),
            })
            if result.verdict != "accept":
                # Remember the CLOSEST non-accept for the best-of-close fallback.
                if best_reject is None or result.fit_score > best_reject[0]:
                    best_reject = (result.fit_score, cand, result)
                continue
            # Thin guardrail: reject a clip whose metadata is the WRONG visual
            # world (e.g. a Mayan/jungle clip in a Roman Pantheon video) even
            # after the LLM accepted it, so we fall through to the next candidate.
            gw_reason = self._v2_guardrail_reject(cand, plan)
            if gw_reason:
                verdicts = metrics.get("candidate_verdicts")
                if verdicts:
                    verdicts[-1]["verdict"] = "reject_wrong_world"
                    verdicts[-1]["reason"] = ("guardrail " + gw_reason)[:240]
                metrics["guardrail_rejects"] = int(metrics.get("guardrail_rejects", 0)) + 1
                continue
            # Subject-aware portrait crop: carry the judge's horizontal subject
            # position (0=left..1=right) onto the candidate so reframing crops
            # toward the subject instead of blindly centering. None when absent.
            try:
                cand.subject_h_pos = result.observation.get("subject_h_pos")
            except Exception:
                cand.subject_h_pos = None
            outcome = self._v2_download_outcome(
                plan, cand, chunk_dir, orientation,
                used_level=used_level,
                verify_reason=result.reason,
                score=8.0,
                used_source_ids=used_source_ids,
                used_source_lock=used_source_lock,
            )
            if outcome is not None:
                outcome.semantic_pass_source = "observer_judge"
                outcome.mm_quality_pass = True
                outcome.mm_quality_score = None
                outcome.review_skipped = True
                outcome.review_skip_reason = "v2_observer_judge_no_old_review"
                outcome.verify_test_results = [{
                    "test_id": "observer_judge",
                    "passed": True,
                    "reason": (result.reason or "")[:200],
                }]
                outcome.verify_scores = {
                    "observer_judge_accept": 1,
                    "haiku_escalated": 0,
                    "detection_calls": metrics.get("detection_calls", 0),
                    "detection_elapsed_seconds": metrics.get("detection_elapsed_seconds", 0.0),
                    "detection_est_cost_usd": metrics.get("detection_est_cost_usd", 0.0),
                    "total_image_count": metrics.get("total_image_count", 0),
                    "image_count_call_count": metrics.get("image_count_call_count", 0),
                }
                if getattr(outcome, "cascade_trace", None) is None:
                    outcome.cascade_trace = {}
                outcome.cascade_trace["v2_detection"] = dict(metrics)
                return outcome
        # Nothing accepted. Before falling to a generic (possibly off-topic)
        # fallback, use the CLOSEST non-accept if it scored >= use_floor — a
        # near-miss real clip beats an off-topic motif. Never use a wrong-world clip.
        if adaptive and best_reject is not None and best_reject[0] >= use_floor:
            fit, cand, result = best_reject
            if not self._v2_guardrail_reject(cand, plan):
                try:
                    cand.subject_h_pos = result.observation.get("subject_h_pos")
                except Exception:
                    cand.subject_h_pos = None
                outcome = self._v2_download_outcome(
                    plan, cand, chunk_dir, orientation,
                    used_level=used_level,
                    verify_reason=f"best_of_close fit={fit:.0f}: {result.reason}",
                    score=6.5,
                    used_source_ids=used_source_ids,
                    used_source_lock=used_source_lock,
                )
                if outcome is not None:
                    outcome.semantic_pass_source = "observer_judge_best_of_close"
                    outcome.mm_quality_pass = True
                    if getattr(outcome, "cascade_trace", None) is None:
                        outcome.cascade_trace = {}
                    outcome.cascade_trace["v2_detection"] = dict(metrics)
                    logger.info(
                        "chunk best_of_close: used closest non-accept (fit=%.1f) instead of fallback",
                        fit,
                    )
                    return outcome
        return None

    @staticmethod
    def _v2_candidate_matches_must_show(cand: object, plan: dict) -> bool:
        """该候选的素材标签是否命中本句的 must_show / hard_anchors。

        用于"无判官兜底"(motif rescue)的地板:兜底不能把一个和 must_show(如 rats)
        完全不沾边的镜头(蜜蜂"Bee on Concrete")硬塞到片子里。宽松原则:must_show 为空
        → 不设地板(返回 True);有 must_show 但候选无标签 → 保守视为不匹配(兜底处宁严勿松)。
        """
        anchors: list[str] = []
        for key in ("must_show", "hard_anchors"):
            for x in (plan.get(key) or []):
                s = str(x).strip().lower()
                if s:
                    anchors.append(s)
        if not anchors:
            return True
        tags = str(getattr(cand, "source_tags", "") or "").lower()
        if not tags:
            return False
        for a in anchors:
            if a in tags:
                return True
            # 英文单复数粗匹配(rat/rats)
            if a.endswith("s") and len(a) > 2 and a[:-1] in tags:
                return True
            if (a + "s") in tags:
                return True
        return False

    @staticmethod
    def _short_v2_max_observer_per_chunk() -> int:
        # 镜头(老鼠),反而轮到无判官的 motif 兜底塞进跑题素材(蜜蜂)。omni-flash 一张
        try:
            return max(0, int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_CHUNK", "6")))
        except ValueError:
            return 6

    # Instead of a fixed cap, judge a cheap BASE number of candidates; keep going
    # past base ONLY when a non-accept so far was CLOSE (fit_score >= close_fit) —
    # worth one more try — up to the hard per-chunk ceiling. If nothing is
    # accepted but the closest reject scored >= use_floor, use THAT near-miss clip
    # instead of a generic off-topic fallback. Toggle off → old fixed-cap behavior.
    @staticmethod
    def _short_v2_observer_adaptive() -> bool:
        return os.environ.get("MEDIA_BUDDY_SHORT_V2_OBSERVER_ADAPTIVE", "1").strip().lower() \
            not in ("0", "false", "no", "off")

    @staticmethod
    def _short_v2_observer_base() -> int:
        try:
            return max(1, int(os.environ.get("MEDIA_BUDDY_SHORT_V2_OBSERVER_BASE", "2")))
        except ValueError:
            return 2

    @staticmethod
    def _short_v2_observer_close_fit() -> float:
        try:
            return float(os.environ.get("MEDIA_BUDDY_SHORT_V2_OBSERVER_CLOSE_FIT", "6"))
        except ValueError:
            return 6.0

    @staticmethod
    def _short_v2_observer_use_floor() -> float:
        try:
            return float(os.environ.get("MEDIA_BUDDY_SHORT_V2_OBSERVER_USE_FLOOR", "5"))
        except ValueError:
            return 5.0

    @staticmethod
    def _short_v2_max_observer_per_video() -> int:
        try:
            return max(0, int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_VIDEO", "120")))
        except ValueError:
            return 120

    @staticmethod
    def _short_v2_try_reserve_observer_call(
        observer_budget: Optional[dict[str, int]],
        observer_budget_lock: Optional[threading.Lock],
        max_calls_per_video: int,
    ) -> bool:
        if max_calls_per_video <= 0:
            return False
        if observer_budget is None:
            return True
        if observer_budget_lock is None:
            current = int(observer_budget.get("calls", 0) or 0)
            if current >= max_calls_per_video:
                return False
            observer_budget["calls"] = current + 1
            return True
        with observer_budget_lock:
            current = int(observer_budget.get("calls", 0) or 0)
            if current >= max_calls_per_video:
                return False
            observer_budget["calls"] = current + 1
            return True

    @staticmethod
    def _short_v2_observer_cache_key(
        cand: FootageCandidate,
        plan: dict,
        mode: str,
    ) -> tuple[Any, ...]:
        def clean_list(value: Any) -> tuple[str, ...]:
            if not isinstance(value, list):
                return ()
            return tuple(sorted(str(item or "").strip().lower() for item in value if str(item or "").strip()))

        return (
            str(cand.source or "").lower(),
            str(cand.source_id or cand.source_url or cand.query or "").strip().lower(),
            str(mode or "").lower(),
            str(plan.get("visual_focus") or "").lower(),
            clean_list(plan.get("must_show")),
            clean_list(plan.get("must_not_show")),
        )

    @staticmethod
    def _v2_candidate_anchor_text(cand: FootageCandidate) -> str:
        raw = getattr(cand, "_raw", None)
        return " ".join([
            str(getattr(cand, "query", "") or ""),
            str(getattr(cand, "source_tags", "") or ""),
            str(getattr(raw, "title", "") or ""),
            str(getattr(raw, "description", "") or ""),
            str(getattr(raw, "keywords", "") or ""),
        ]).lower()

    @classmethod
    def _v2_candidate_anchor_score(cls, plan: dict, cand: FootageCandidate) -> int:
        anchors = set(cls._v2_hard_must_show_anchors(plan))
        if not anchors:
            return 0
        text = " ".join([
            cls._v2_candidate_anchor_text(cand),
            cls._v2_candidate_anchor_proof_text(cand),
        ])
        score = 0
        anchor_groups = [
            ({"customer", "line", "queue"}, ("customer", "customers", "queue", "line", "waiting", "crowd", "buying")),
            ({"cash", "money", "rupee"}, ("cash", "money", "rupee", "rupees", "currency", "banknote", "paying")),
            ({"syrup", "honey"}, ("syrup", "honey", "jaggery", "sweetener", "caramel")),
            ({"kettle", "pot"}, ("kettle", "pot", "vessel", "cooking pot", "stainless steel", "copper")),
            ({"drink", "preparation", "vendor"}, ("drink", "juice", "vendor", "stall", "preparation", "pouring")),
            ({"coffee", "phin", "milk", "ice"}, ("coffee", "iced coffee", "phin filter", "condensed milk", "ice")),
            ({"chestnut", "pan", "bag", "bags"}, ("chestnut", "roasted chestnut", "pan", "paper bag", "stacked bags", "steam")),
            ({"sugarcane", "juice"}, ("sugarcane", "sugarcane juice", "juice extraction")),
        ]
        for group, terms in anchor_groups:
            if anchors & group and any(term in text for term in terms):
                score += 10
        for anchor in anchors:
            if anchor and anchor in text:
                score += 3
        return score

    @classmethod
    def _v2_prioritize_anchor_candidates(
        cls, plan: dict, candidates: list[FootageCandidate],
    ) -> list[FootageCandidate]:
        indexed = list(enumerate(candidates or []))
        indexed.sort(
            key=lambda row: (
                -cls._v2_candidate_anchor_score(plan, row[1]),
                row[0],
            )
        )
        return [cand for _idx, cand in indexed]


    def _v2_download_outcome(
        self,
        plan: dict,
        cand: FootageCandidate,
        chunk_dir: Path,
        orientation: str,
        *,
        used_level: str,
        verify_reason: str,
        score: float,
        used_source_ids: Optional[set[str]] = None,
        used_source_lock: Optional[threading.Lock] = None,
        allow_reuse: bool = False,
    ) -> Optional[ChunkOutcome]:
        # allow_reuse: the "video must ship" fallback may legitimately re-use a
        # motif clip that another chunk already consumed (a repeated theme
        # environment shot beats a dead pipeline), so it skips the dedup reserve.
        if not allow_reuse and not self._v2_reserve_source(
            used_source_ids, cand, used_source_lock,
        ):
            logger.info(
                "chunk %d v2 skipped duplicate reserved source %s:%s",
                plan["chunk_index"], cand.source, cand.source_id,
            )
            return None
        local = self.footage.download_candidate(cand, chunk_dir)
        if not local:
            self._v2_mark_source_unusable(
                used_source_ids, cand, used_source_lock,
            )
            return None
        # Shorts speed path: keep the downloaded source as-is and let the
        # final VideoCompose filtergraph do the aspect adaptation in one pass.
        # The old path pre-rendered every selected clip to 9:16 here, then
        # encoded again during compose; for 20+ shot shorts that dominated
        # runtime. Keep an env escape hatch for troubleshooting.
        defer_reframe = os.environ.get(
            "MEDIA_BUDDY_DEFER_REFRAME_TO_COMPOSE", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        if not defer_reframe:
            local = self._reframe_if_needed(
                local, orientation,
                subject_h_pos=getattr(cand, "subject_h_pos", None),
            )
        ok, err = self._basic_file_check(local)
        if not ok:
            logger.info(
                "chunk %d v2 selected %s but file check failed: %s",
                plan["chunk_index"], cand.source, err,
            )
            self._v2_mark_source_unusable(
                used_source_ids, cand, used_source_lock,
            )
            return None
        post_reason = self._v2_post_download_verify_reject(plan, cand, local, used_level)
        if post_reason:
            logger.warning(
                "chunk %d v2 post-download verification rejected %s:%s: %s",
                plan["chunk_index"], cand.source, cand.source_id, post_reason,
            )
            metrics = plan.setdefault("_v2_detection_metrics", {})
            metrics.setdefault("candidate_verdicts", []).append({
                "mode": "post_download",
                "source": cand.source,
                "source_id": cand.source_id,
                "verdict": "reject_post_download",
                "model_id": "google/gemini-2.5-flash",
                "elapsed_seconds": 0.0,
                "est_cost_usd": 0.0,
                "escalated": False,
                "reason": post_reason[:240],
                **self._v2_debug_candidate_extras(cand),
            })
            self._v2_mark_source_unusable(
                used_source_ids, cand, used_source_lock,
            )
            return None
        return ChunkOutcome(
            chunk_index=int(plan["chunk_index"]),
            plan=plan,
            local_path=str(local),
            duration=cand.duration or 5.0,
            source=cand.source,
            source_id=cand.source_id,
            source_url=cand.source_url,
            resolution=f"{cand.width}x{cand.height}" if cand.width else "",
            score=score,
            decision="use",
            cost_usd=0.0,
            source_tags=cand.source_tags or cand.query or "",
            subject_h_pos=getattr(cand, "subject_h_pos", None),
            safety_severity="ok",
            safety_flags=[],
            safety_reason="v2 old moderation disabled",
            semantic_pass_source="observer_judge" if used_level != "L_motif" else "motif_pool",
            mm_quality_pass=True,
            review_skipped=True,
            review_skip_reason="v2_no_mm_critique_no_moderate_clip",
            used_level=used_level,
            used_wave=1,
            selected_prompt=cand.query or "",
            verify_reason=verify_reason[:480],
            confidence=0.8 if used_level != "L_motif" else 0.5,
            verify_scores={
                "reframe_deferred_to_compose": 1 if defer_reframe else 0,
            },
            verify_test_results=[],
        )

    @staticmethod
    def _v2_post_download_verify_enabled(used_level: str) -> bool:
        if os.environ.get("MEDIA_BUDDY_SHORT_V2_POST_DOWNLOAD_VERIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
            return False
        return str(used_level or "") in {
            "L_literal_domain_salvage",
        }

    def _v2_post_download_verify_reject(
        self,
        plan: dict,
        cand: FootageCandidate,
        local: Path,
        used_level: str,
    ) -> Optional[str]:
        if not self._v2_post_download_verify_enabled(used_level):
            return None
        data_url = self._v2_extract_validation_frame_data_url(Path(local), Path(local).parent)
        if not data_url:
            return "post_download_frame_extract_failed"
        check = FootageCandidate(
            source=cand.source,
            source_id=f"{cand.source_id}:downloaded_frame",
            source_url=cand.source_url,
            query=cand.query,
            thumbnail_url=data_url,
            source_tags=cand.source_tags,
            duration=cand.duration,
            width=cand.width,
            height=cand.height,
        )
        review_plan = self._v2_post_download_review_plan(plan, cand, used_level)
        if self._v2_post_download_should_forbid_cash(plan):
            forbidden = list(review_plan.get("must_not_show") or [])
            for term in ("cash", "money", "rupee", "currency", "banknotes"):
                if term not in forbidden:
                    forbidden.append(term)
            review_plan["must_not_show"] = forbidden
        result = judge_candidate(
            check,
            review_plan,
            mode="post_download",
            is_last_candidate=True,
            budget=HaikuReviewBudget(max_per_video=0),
        )
        metrics = plan.setdefault("_v2_detection_metrics", {})
        metrics["detection_calls"] = int(metrics.get("detection_calls", 0)) + int(result.call_count or 1)
        metrics["detection_elapsed_seconds"] = round(
            float(metrics.get("detection_elapsed_seconds", 0.0)) + float(result.elapsed_seconds or 0.0),
            3,
        )
        metrics["detection_est_cost_usd"] = round(
            float(metrics.get("detection_est_cost_usd", 0.0)) + float(result.est_cost_usd or 0.0),
            6,
        )
        if result.verdict != "accept":
            return f"post_download_{result.verdict}: {result.reason}"
        return None

    @staticmethod
    def _v2_post_download_review_plan(
        plan: dict,
        cand: Optional[FootageCandidate] = None,
        used_level: str = "",
    ) -> dict:
        review_plan = dict(plan or {})
        text = " ".join([
            str(review_plan.get("sentence") or ""),
            str(review_plan.get("text") or ""),
            " ".join(str(x) for x in (review_plan.get("must_show") or [])),
            " ".join(str(x) for x in (review_plan.get("queries") or [])),
        ]).lower()
        has_kettle = any(term in text for term in (
            "kettle", "pot", "copper", "stainless steel",
            "铜壶", "壶", "不锈钢", "厚壁",
        ))
        has_syrup = any(term in text for term in (
            "syrup", "honey", "jaggery", "sweet liquid", "sweetener",
            "糖浆", "浓度", "椰枣蜜", "蜂蜜",
        ))
        if has_kettle and has_syrup:
            review_plan["must_show"] = [
                "kettle or syrup/sweet liquid preparation",
            ]
        return review_plan

    @staticmethod
    def _v2_post_download_should_forbid_cash(plan: dict) -> bool:
        text = " ".join([
            str(plan.get("sentence") or ""),
            str(plan.get("text") or ""),
            " ".join(str(x) for x in (plan.get("must_show") or [])),
            " ".join(str(x) for x in (plan.get("queries") or [])),
        ]).lower()
        return not any(term in text for term in (
            "cash", "money", "rupee", "rupees", "currency", "banknote",
            "counting money", "paying", "payment", "price", "pricing",
            "profit", "cost", "revenue", "margin",
            "收钱", "现金", "数钱", "卢比", "定价", "成本", "利润", "收入", "毛利",
        ))

    @staticmethod
    def _v2_extract_validation_frame_data_url(local: Path, chunk_dir: Path) -> str:
        try:
            frame_dir = chunk_dir / "post_download_validation"
            frame_dir.mkdir(parents=True, exist_ok=True)
            frame = frame_dir / f"{local.stem}_sheet.jpg"
            pattern = frame_dir / f"{local.stem}_%02d.jpg"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(local),
                "-vf", "fps=1,scale=240:-1",
                "-frames:v", "3",
                str(pattern),
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            frames = sorted(frame_dir.glob(f"{local.stem}_*.jpg"))[:3]
            frames = [p for p in frames if p.exists() and p.stat().st_size >= 512]
            if not frames:
                return ""
            try:
                from PIL import Image, ImageDraw
                thumbs = []
                for idx, path in enumerate(frames, start=1):
                    im = Image.open(path).convert("RGB")
                    im.thumbnail((240, 426))
                    canvas = Image.new("RGB", (240, 456), "white")
                    canvas.paste(im, ((240 - im.width) // 2, 0))
                    ImageDraw.Draw(canvas).text((8, 432), f"downloaded frame {idx}", fill=(0, 0, 0))
                    thumbs.append(canvas)
                sheet = Image.new("RGB", (len(thumbs) * 250 + 10, 466), "white")
                for idx, thumb in enumerate(thumbs):
                    sheet.paste(thumb, (10 + idx * 250, 5))
                sheet.save(frame, quality=90)
            except Exception:
                frame = frames[0]
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
        except Exception:
            return ""

    @staticmethod
    def _v2_mark_source_unusable(
        used_source_ids: Optional[set[str]],
        cand: FootageCandidate,
        used_source_lock: Optional[threading.Lock] = None,
    ) -> None:
        """Blacklist a clip for the rest of this v2 run after download/file failure."""
        if used_source_ids is None or not cand.source_id:
            return
        keys = {cand.source_id}
        if cand.source:
            keys.add(f"{cand.source}:{cand.source_id}")
        if used_source_lock is None:
            used_source_ids.update(keys)
            return
        with used_source_lock:
            used_source_ids.update(keys)

    @staticmethod
    def _v2_selection_concurrency() -> int:
        try:
            return max(1, int(os.environ.get("MEDIA_BUDDY_V2_ASSET_CONCURRENCY", "4")))
        except ValueError:
            return 4

    def _v2_select_chunk_with_counter(
        self,
        plan: dict,
        output_dir: Path,
        orientation: str,
        top_n: int,
        motif_pool: list[str],
        budget: HaikuReviewBudget,
        used_source_ids: set[str],
        used_source_lock: threading.Lock,
        llm_counter: Optional["LLMCallCounter"],
        observer_cache: Optional[dict[tuple[Any, ...], Any]] = None,
        observer_cache_lock: Optional[threading.Lock] = None,
        observer_budget: Optional[dict[str, int]] = None,
        observer_budget_lock: Optional[threading.Lock] = None,
    ) -> ChunkOutcome:
        idx = int(plan["chunk_index"])
        if llm_counter is None:
            return self._v2_select_chunk(
                plan, output_dir, orientation, top_n, motif_pool, budget,
                used_source_ids, used_source_lock=used_source_lock,
                observer_cache=observer_cache,
                observer_cache_lock=observer_cache_lock,
                observer_budget=observer_budget,
                observer_budget_lock=observer_budget_lock,
            )
        with llm_counter.chunk(idx):
            return self._v2_select_chunk(
                plan, output_dir, orientation, top_n, motif_pool, budget,
                used_source_ids, used_source_lock=used_source_lock,
                observer_cache=observer_cache,
                observer_cache_lock=observer_cache_lock,
                observer_budget=observer_budget,
                observer_budget_lock=observer_budget_lock,
            )

    @staticmethod
    def _v2_used_source_snapshot(
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> set[str]:
        if used_source_lock is None:
            return set(used_source_ids)
        with used_source_lock:
            return set(used_source_ids)

    @staticmethod
    def _v2_reserve_source(
        used_source_ids: Optional[set[str]],
        cand: FootageCandidate,
        used_source_lock: Optional[threading.Lock],
    ) -> bool:
        if used_source_ids is None or not cand.source_id:
            return True
        keys = {cand.source_id}
        if cand.source:
            keys.add(f"{cand.source}:{cand.source_id}")
        if used_source_lock is None:
            if used_source_ids & keys:
                return False
            used_source_ids.update(keys)
            return True
        with used_source_lock:
            if used_source_ids & keys:
                return False
            used_source_ids.update(keys)
            return True

    @staticmethod
    def _v2_max_asset_occurrences() -> int:
        """Same source asset can appear at most twice by default."""
        try:
            return max(
                1,
                int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_ASSET_OCCURRENCES", "2")),
            )
        except ValueError:
            return 2

    @staticmethod
    def _v2_max_total_repeat_occurrences() -> int:
        """Whole-video repeat budget.

        Default 1 means the entire video can contain only one duplicate shot
        occurrence in total, no matter which asset is repeated.
        """
        try:
            return max(
                0,
                int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_TOTAL_REPEAT_OCCURRENCES", "1")),
            )
        except ValueError:
            return 1

    @staticmethod
    def _candidate_asset_key(cand: FootageCandidate) -> str:
        source = str(getattr(cand, "source", "") or "")
        source_id = str(getattr(cand, "source_id", "") or "")
        source_url = str(getattr(cand, "source_url", "") or "")
        if source and source_id:
            return f"{source}:{source_id}"
        return source_id or source_url

    def _v2_asset_occurrence_count(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        asset_key: str,
    ) -> int:
        if not asset_key:
            return 0
        return sum(
            1
            for oc in stock_outcomes
            if oc is not None
            and oc.decision == "use"
            and bool(oc.local_path)
            and self._outcome_asset_key(oc) == asset_key
        )

    def _v2_total_duplicate_reuse_count(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
    ) -> int:
        counts: dict[str, int] = {}
        for oc in stock_outcomes:
            if oc is None or oc.decision != "use" or not oc.local_path:
                continue
            key = self._outcome_asset_key(oc)
            if key:
                counts[key] = counts.get(key, 0) + 1
        return sum(max(0, count - 1) for count in counts.values())

    def _v2_asset_repeat_available(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        asset_key: str,
        *,
        max_occurrences: Optional[int] = None,
        max_total_repeats: Optional[int] = None,
    ) -> bool:
        if not asset_key:
            return True
        current_count = self._v2_asset_occurrence_count(stock_outcomes, asset_key)
        limit = (
            self._v2_max_asset_occurrences()
            if max_occurrences is None
            else max(1, int(max_occurrences))
        )
        if current_count >= limit:
            return False
        if current_count <= 0:
            return True
        repeat_limit = (
            self._v2_max_total_repeat_occurrences()
            if max_total_repeats is None
            else max(0, int(max_total_repeats))
        )
        return self._v2_total_duplicate_reuse_count(stock_outcomes) < repeat_limit

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _find_donor_outcome_for_run(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        *,
        max_asset_occurrences: Optional[int] = None,
    ) -> Optional[ChunkOutcome]:
        """Find a non-adjacent earlier donor for explicit inheritance."""
        occurrence_limit = (
            self._v2_max_asset_occurrences()
            if max_asset_occurrences is None
            else max(1, int(max_asset_occurrences))
        )
        adjacent_keys = self._adjacent_asset_keys(stock_outcomes, idx)
        for j in range(idx - 2, -1, -1):
            prev = stock_outcomes[j]
            if prev is None:
                continue
            if prev.decision == "use" and prev.local_path:
                key = self._outcome_asset_key(prev)
                if key in adjacent_keys:
                    continue
                if not self._v2_asset_repeat_available(
                    stock_outcomes, key, max_occurrences=occurrence_limit,
                ):
                    continue
                return prev
        return None

    def _find_donor_outcome_for_aggressive(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        *,
        exclude: set[int],
        max_asset_occurrences: Optional[int] = None,
    ) -> Optional[ChunkOutcome]:
        """

        Like `_find_donor_outcome_for_run` but skips any chunk index in
        `exclude` (already used as donor in aggressive mode this run).
        Goal: prevent the "5 chunks reuse same mandelbrot zoom" visual
        regression that pure best-effort inheritance would create.

        Bi-directional search:
          1. First pass: scan backward for fresh donor (not in exclude)
          2. Second pass: if no backward fresh donor, scan forward
          3. If everything used, return None (let chunk fall through —
             the round-robin guarantee is "at most once" not "always
             have one")
        """
        # Backward scan — prefer earlier chunks for continuity
        occurrence_limit = (
            self._v2_max_asset_occurrences()
            if max_asset_occurrences is None
            else max(1, int(max_asset_occurrences))
        )
        adjacent_keys = self._adjacent_asset_keys(stock_outcomes, idx)
        for j in range(idx - 2, -1, -1):
            prev = stock_outcomes[j]
            if prev is None:
                continue
            if prev.decision != "use" or not prev.local_path:
                continue
            key = self._outcome_asset_key(prev)
            if key in adjacent_keys:
                continue
            if not self._v2_asset_repeat_available(
                stock_outcomes, key, max_occurrences=occurrence_limit,
            ):
                continue
            if prev.chunk_index in exclude:
                continue
            # Skip inherited outcomes themselves to avoid chain inheritance
            # from aggressive mode — keep chains only from explicit
            # transition/CTA Pass 2 inheritance.
            if prev.inherited_from_chunk is not None:
                continue
            return prev
        # Forward scan as fallback (idx may be near start; first chunks
        # could be stock_miss themselves but later ones succeeded)
        for j in range(idx + 2, len(stock_outcomes)):
            nxt = stock_outcomes[j]
            if nxt is None:
                continue
            if nxt.decision != "use" or not nxt.local_path:
                continue
            key = self._outcome_asset_key(nxt)
            if key in adjacent_keys:
                continue
            if not self._v2_asset_repeat_available(
                stock_outcomes, key, max_occurrences=occurrence_limit,
            ):
                continue
            if nxt.chunk_index in exclude:
                continue
            if nxt.inherited_from_chunk is not None:
                continue
            return nxt
        return None

    def _find_any_donor(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        *,
        max_asset_occurrences: Optional[int] = None,
    ) -> Optional[ChunkOutcome]:
        """Last-resort donor finder for the 'video must ship' fallback.

        It accepts already-inherited donors, but it must not choose the
        immediately previous/next chunk. Adjacent reuse makes the video look
        frozen, so ship fallback first searches for a non-adjacent donor.
        """
        n = len(stock_outcomes)
        occurrence_limit = (
            self._v2_max_asset_occurrences()
            if max_asset_occurrences is None
            else max(1, int(max_asset_occurrences))
        )
        adjacent_keys = self._adjacent_asset_keys(stock_outcomes, idx)
        for dist in range(1, n):
            if dist <= 1:
                continue
            for j in (idx + dist, idx - dist):
                if j < 0 or j >= n:
                    continue
                cand = stock_outcomes[j]
                if cand is None:
                    continue
                if cand.decision == "use" and cand.local_path:
                    key = self._outcome_asset_key(cand)
                    if key in adjacent_keys:
                        continue
                    if not self._v2_asset_repeat_available(
                        stock_outcomes, key, max_occurrences=occurrence_limit,
                    ):
                        continue
                    return cand
        return None

    @staticmethod
    def _outcome_asset_key(outcome: Optional[ChunkOutcome]) -> str:
        if outcome is None:
            return ""
        source = str(getattr(outcome, "source", "") or "")
        source_id = str(getattr(outcome, "source_id", "") or "")
        local_path = str(getattr(outcome, "local_path", "") or "")
        if source and source_id:
            return f"{source}:{source_id}"
        return local_path

    def _adjacent_asset_keys(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
    ) -> set[str]:
        keys: set[str] = set()
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(stock_outcomes):
                key = self._outcome_asset_key(stock_outcomes[j])
                if key:
                    keys.add(key)
        return keys

    @staticmethod
    def _v2_story_degraded_fallback_enabled() -> bool:
        raw = os.environ.get("MEDIA_BUDDY_SHORT_V2_STORY_DEGRADED_FALLBACK", "1")
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _v2_prefer_fresh_distinct_before_reuse_enabled() -> bool:
        raw = os.environ.get("MEDIA_BUDDY_SHORT_V2_FRESH_DISTINCT_BEFORE_REUSE", "1")
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _v2_outcome_semantic_text(outcome: ChunkOutcome) -> str:
        plan = outcome.plan or {}
        return " ".join([
            str(outcome.source_tags or ""),
            str(outcome.selected_prompt or ""),
            str(outcome.verify_reason or ""),
            str(plan.get("sentence") or ""),
            str(plan.get("text") or ""),
            str(plan.get("domain") or ""),
            " ".join(str(x) for x in (plan.get("must_show") or [])),
            " ".join(str(x) for x in (plan.get("queries") or [])),
            " ".join(str(x) for x in (plan.get("domain_motif_pool") or [])),
        ]).lower()

    @classmethod
    def _v2_story_degraded_donor_score(cls, plan: dict, donor: ChunkOutcome) -> int:
        """Score whether a verified donor is safe for final-pass degradation."""
        if donor is None or donor.decision != "use" or not donor.local_path:
            return 0
        candidate = FootageCandidate(
            source=donor.source or "",
            source_id=donor.source_id or "",
            source_url=donor.source_url or "",
            query=donor.selected_prompt or "",
            source_tags=donor.source_tags or "",
        )
        if cls._v2_locale_guardrail_reject(candidate, plan):
            return 0
        anchors = set(cls._v2_hard_must_show_anchors(plan))
        if not anchors:
            return 0
        donor_text = cls._v2_outcome_semantic_text(donor)
        plan_text = " ".join([
            str(plan.get("sentence") or ""),
            str(plan.get("text") or ""),
            str(plan.get("domain") or ""),
            " ".join(str(x) for x in (plan.get("must_show") or [])),
            " ".join(str(x) for x in (plan.get("queries") or [])),
            " ".join(str(x) for x in (plan.get("domain_motif_pool") or [])),
        ]).lower()

        def has_any(text: str, terms: tuple[str, ...]) -> bool:
            return any(term in text for term in terms)

        exact_hits = sum(1 for anchor in anchors if anchor and anchor in donor_text)
        if cls._v2_plan_is_retail(plan):
            return cls._v2_retail_degraded_score(plan, donor_text, anchors, exact_hits)
        if "chestnut" in anchors:
            return 90 + exact_hits * 5 if "chestnut" in donor_text else 0
        if anchors & {"coffee", "phin", "milk", "ice"} or (
            "coffee" in plan_text and anchors & {"cup", "glass"}
        ):
            return 80 + exact_hits * 5 if has_any(
                donor_text,
                ("coffee", "phin", "iced coffee", "condensed milk", "coffee glass", "cup"),
            ) else 0
        if anchors & {"syrup", "honey", "kettle", "pot"}:
            return 65 + exact_hits * 5 if has_any(
                donor_text,
                ("sugarcane", "juice", "drink", "cup", "glass", "stall", "vendor", "cart"),
            ) else 0
        if anchors & {"cash", "money", "rupee", "lira", "dong"}:
            if has_any(donor_text, ("cash", "rupee", "lira", "dong", "currency", "banknote")):
                if (
                    has_any(plan_text, ("rupee", "dong", "lira"))
                    and has_any(donor_text, ("dollar", "usd", "united states"))
                ):
                    return 0
                return 75 + exact_hits * 5
            return 55 if has_any(
                donor_text,
                ("vendor", "stall", "cart", "market", "customer", "sugarcane", "coffee", "chestnut"),
            ) else 0
        if anchors & {"customer", "line", "queue"}:
            return 70 + exact_hits * 5 if has_any(
                donor_text,
                ("customer", "customers", "queue", "line", "crowd", "vendor", "stall", "cart", "market"),
            ) else 0
        if anchors & {"pan", "steam", "bag", "bags"}:
            return 60 + exact_hits * 5 if has_any(
                donor_text,
                ("pan", "steam", "grill", "food", "vendor", "stall", "cart", "chestnut", "coffee", "sugarcane"),
            ) else 0
        return 30 + exact_hits * 5 if exact_hits else 0

    @classmethod
    def _v2_story_degraded_candidate_score(cls, plan: dict, cand: FootageCandidate) -> int:
        """Score a fresh candidate for non-repeating story degradation.

        This mirrors donor degradation, but it is used before inheritance so a
        distinct same-story clip wins over repeated footage whenever available.
        """
        candidate = FootageCandidate(
            source=cand.source or "",
            source_id=cand.source_id or "",
            source_url=cand.source_url or "",
            query=cand.query or "",
            source_tags=cand.source_tags or "",
        )
        if cls._v2_candidate_wrong_world_or_locale_reject(candidate, plan):
            return 0
        anchors = set(cls._v2_hard_must_show_anchors(plan))
        if not anchors:
            return 0
        candidate_text = cls._v2_candidate_anchor_proof_text(candidate)
        if not candidate_text:
            candidate_text = " ".join([
                str(candidate.source_tags or ""),
                str(candidate.source_url or ""),
            ]).lower()
        plan_text = " ".join([
            str(plan.get("sentence") or ""),
            str(plan.get("text") or ""),
            str(plan.get("domain") or ""),
            " ".join(str(x) for x in (plan.get("must_show") or [])),
            " ".join(str(x) for x in (plan.get("queries") or [])),
            " ".join(str(x) for x in (plan.get("domain_motif_pool") or [])),
        ]).lower()

        def has_any(text: str, terms: tuple[str, ...]) -> bool:
            return any(term in text for term in terms)

        exact_hits = sum(1 for anchor in anchors if anchor and anchor in candidate_text)
        if cls._v2_plan_is_retail(plan):
            return cls._v2_retail_degraded_score(plan, candidate_text, anchors, exact_hits)
        if "chestnut" in anchors:
            return 90 + exact_hits * 5 if has_any(
                candidate_text,
                ("roasted chestnut", "chestnut cart", "chestnut stall", "street chestnut", "chestnut pan"),
            ) else 0
        if anchors & {"coffee", "phin", "milk", "ice"} or (
            "coffee" in plan_text and anchors & {"cup", "glass"}
        ):
            return 80 + exact_hits * 5 if has_any(
                candidate_text,
                ("coffee", "phin", "iced coffee", "condensed milk", "coffee glass", "cup"),
            ) else 0
        if anchors & {"syrup", "honey", "kettle", "pot"}:
            return 65 + exact_hits * 5 if has_any(
                candidate_text,
                ("sugarcane", "juice", "drink", "cup", "glass", "stall", "vendor", "cart"),
            ) else 0
        if anchors & {"cash", "money", "rupee", "lira", "dong"}:
            if has_any(candidate_text, ("cash", "rupee", "lira", "dong", "currency", "banknote")):
                if (
                    has_any(plan_text, ("rupee", "dong", "lira"))
                    and has_any(candidate_text, ("dollar", "usd", "united states"))
                ):
                    return 0
                return 75 + exact_hits * 5
            return 55 if has_any(
                candidate_text,
                ("vendor", "stall", "cart", "market", "customer", "sugarcane", "coffee", "chestnut"),
            ) else 0
        if anchors & {"customer", "line", "queue"}:
            return 70 + exact_hits * 5 if has_any(
                candidate_text,
                ("customer", "customers", "queue", "line", "crowd", "vendor", "stall", "cart", "market"),
            ) else 0
        if anchors & {"pan", "steam", "bag", "bags"}:
            return 60 + exact_hits * 5 if has_any(
                candidate_text,
                ("pan", "steam", "grill", "food", "vendor", "stall", "cart", "chestnut", "coffee", "sugarcane"),
            ) else 0
        return 30 + exact_hits * 5 if exact_hits else 0

    def _find_story_degraded_donor(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        plan: dict,
    ) -> Optional[ChunkOutcome]:
        if not self._v2_story_degraded_fallback_enabled():
            return None
        n = len(stock_outcomes)
        occurrence_limit = self._v2_max_asset_occurrences()
        best_score = 0
        best_dist = 9999
        best: Optional[ChunkOutcome] = None
        for dist in range(2, n + 1):
            for j in (idx - dist, idx + dist):
                if j < 0 or j >= n:
                    continue
                donor = stock_outcomes[j]
                if donor is None or donor.local_path is None:
                    continue
                if not self._v2_asset_repeat_available(
                    stock_outcomes,
                    self._outcome_asset_key(donor),
                    max_occurrences=occurrence_limit,
                ):
                    continue
                score = self._v2_story_degraded_donor_score(plan, donor)
                if score <= 0:
                    continue
                if score > best_score or (score == best_score and dist < best_dist):
                    best_score = score
                    best_dist = dist
                    best = donor
        return best

    def _v2_story_degraded_ship_fallback(
        self,
        plan: dict,
        idx: int,
        outcomes: list[Optional[ChunkOutcome]],
    ) -> Optional[ChunkOutcome]:
        donor = self._find_story_degraded_donor(outcomes, idx, plan)
        if donor is None:
            return None
        inherited = self._build_inherited_outcome(plan, donor, idx)
        inherited.inherit_reason = "story_degraded_same_subject"
        inherited.used_level = "L_story_degraded_inherit"
        inherited.fallback_reason = "hard_anchor_degraded_to_same_story_verified_asset"
        inherited.inherit_donor_distance = abs(int(donor.chunk_index) - int(idx))
        metrics = plan.setdefault("_v2_detection_metrics", {})
        metrics["fallback_blocked"] = False
        metrics["selection_status"] = "degraded_story_inherit"
        metrics["degraded_fallback"] = {
            "policy": "same_story_verified_asset",
            "donor_chunk": donor.chunk_index,
            "donor_source_id": donor.source_id,
            "hard_anchors": self._v2_hard_must_show_anchors(plan),
            "reason": "exact stock missing; reused non-adjacent verified same-story clip",
        }
        inherited.verify_scores = dict(metrics)
        inherited.cascade_trace["v2_detection"] = dict(metrics)
        return inherited

    def _v2_story_distinct_ship_fallback(
        self,
        plan: dict,
        idx: int,
        output_dir: Path,
        orientation: str,
        motif_pool: list[str],
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        """Find a fresh same-story asset before reusing an old shot.

        This is the "素材充足则不重复" layer: strict search has already failed,
        so we allow same-story degradation, but only with a new source asset.
        """
        if not self._v2_prefer_fresh_distinct_before_reuse_enabled():
            return None
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        metrics = plan.setdefault("_v2_detection_metrics", {})
        try:
            limit = max(3, int(os.environ.get("MEDIA_BUDDY_SHORT_V2_DISTINCT_FALLBACK_LIMIT", "8")))
        except ValueError:
            limit = 8
        for query in self._short_ship_queries(plan, motif_pool):
            for source_name, search_fn in (
                ("secondary", self.footage.search_secondary),
            ):
                try:
                    candidates = search_fn(query, orientation, kind="video", limit=limit)
                except TypeError:
                    candidates = search_fn(query, orientation)
                except Exception as e:  # noqa: BLE001 - fallback must not crash
                    logger.warning(
                        "chunk %d story distinct fallback search failed source=%s query=%r: %s",
                        idx, source_name, query, e,
                    )
                    continue
                for cand in self._v2_prioritize_anchor_candidates(plan, list(candidates or [])):
                    if cand.source == "library":
                        continue
                    score = self._v2_story_degraded_candidate_score(plan, cand)
                    if score <= 0:
                        metrics["story_distinct_candidate_rejects"] = int(
                            metrics.get("story_distinct_candidate_rejects", 0) or 0
                        ) + 1
                        continue
                    outcome = self._v2_download_outcome(
                        plan, cand, chunk_dir, orientation,
                        used_level="L_story_distinct_fallback",
                        verify_reason=(
                            f"fresh distinct same-story fallback: {source_name} '{query}' score={score}"
                        ),
                        score=min(7.0, max(4.5, score / 10.0)),
                        used_source_ids=used_source_ids,
                        used_source_lock=used_source_lock,
                        allow_reuse=False,
                    )
                    if outcome is None:
                        continue
                    outcome.inherit_reason = "story_distinct_fallback"
                    outcome.fallback_reason = "fresh_distinct_before_reuse"
                    metrics["selection_status"] = "fresh_distinct_story_fallback"
                    metrics["fresh_distinct_fallback"] = {
                        "source": outcome.source,
                        "source_id": outcome.source_id,
                        "query": query,
                        "score": score,
                    }
                    outcome.verify_scores = {
                        **(outcome.verify_scores or {}),
                        **dict(metrics),
                    }
                    if getattr(outcome, "cascade_trace", None) is None:
                        outcome.cascade_trace = {}
                    outcome.cascade_trace["v2_detection"] = dict(metrics)
                    logger.info(
                        "chunk %d story distinct fallback selected %s:%s q=%r score=%s",
                        idx, outcome.source, outcome.source_id, query, score,
                    )
                    return outcome
        metrics["fresh_distinct_fallback_miss"] = int(
            metrics.get("fresh_distinct_fallback_miss", 0) or 0
        ) + 1
        return None

    def _v2_distinct_before_reuse_fallback(
        self,
        plan: dict,
        idx: int,
        output_dir: Path,
        orientation: str,
        motif_pool: list[str],
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        if not self._v2_prefer_fresh_distinct_before_reuse_enabled():
            return None
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        if self._v2_story_fail_closed(plan):
            return self._v2_story_distinct_ship_fallback(
                plan, idx, output_dir, orientation, motif_pool,
                used_source_ids, used_source_lock,
            )
        return self._short_ship_remote_fallback(
            plan, idx, chunk_dir, orientation, motif_pool,
            used_source_ids, used_source_lock,
        )


    def _v2_ship_fallback(
        self,
        plan: dict,
        idx: int,
        output_dir: Path,
        orientation: str,
        motif_pool: list[str],
        budget: HaikuReviewBudget,
        outcomes: list[Optional[ChunkOutcome]],
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        """Guarantee a clip for an otherwise-unmatched chunk so the video ships.

        Two levels, in the user's stated priority order:
          A. A theme-environment empty shot from `motif_pool` (lenient gates) —
             fresh footage that still fits the whole video's background
             (e.g. african-savanna footage for a black-mamba video).
          B. Reuse the nearest real shot already downloaded this run (zero
             network/cost), via `_find_any_donor`.

        Returns a ready-to-use ChunkOutcome, or None only when the whole video
        produced no footage at all (theoretical — the caller then reports it).
        Honors the line-2607 rule: real stock, never a local silent empty file.
        """
        if self._v2_story_fail_closed(plan):
            self._v2_mark_fallback_blocked(
                plan,
                "story_fail_closed: blocked _v2_ship_fallback for hard-anchor story chunk",
            )
            return None
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        # Level A — theme-environment empty shot (fresh, fits the whole video).
        try:
            motif = probe_motif_candidate_lenient(
                self.footage, motif_pool, plan, orientation=orientation,
            )
        except Exception as e:  # noqa: BLE001 — fallback must never raise
            logger.warning("chunk %d ship-fallback lenient motif failed: %s", idx, e)
            motif = None
        if motif is not None:
            motif_key = self._candidate_asset_key(motif)
            if not self._v2_asset_repeat_available(outcomes, motif_key):
                metrics = plan.setdefault("_v2_detection_metrics", {})
                metrics["duplicate_asset_cap_skips"] = int(
                    metrics.get("duplicate_asset_cap_skips", 0) or 0
                ) + 1
                logger.info(
                    "chunk %d ship-fallback skipped motif %s at duplicate cap",
                    idx, motif_key,
                )
                motif = None
        if motif is not None:
            outcome = self._v2_download_outcome(
                plan, motif, chunk_dir, orientation,
                used_level="L_motif_lenient",
                verify_reason="ship fallback: theme-environment empty shot (lenient motif)",
                score=4.0,
                used_source_ids=used_source_ids,
                used_source_lock=used_source_lock,
                allow_reuse=True,
            )
            if outcome is not None:
                outcome.inherit_reason = "ship_theme_motif"
                logger.info(
                    "chunk %d ship-fallback filled with theme motif %s:%s",
                    idx, outcome.source, outcome.source_id,
                )
                return outcome

        # Level A.5 — DISTINCT broad-theme remote stock. Ported idea from the
        # long-video pipeline (kept independent here): scarce/abstract short
        # topics leave many empty chunks; without this they ALL inherit the one
        # clip that matched → "single shot looping". This fills each empty chunk
        # with a DIFFERENT clip (reserved per chunk) via lenient broad search.
        remote = self._short_ship_remote_fallback(
            plan, idx, chunk_dir, orientation, motif_pool,
            used_source_ids, used_source_lock,
        )
        if remote is not None:
            return remote

        # Level B — reuse the nearest real shot already in hand.
        donor = self._find_any_donor(outcomes, idx)
        if donor is not None:
            inherited = self._build_inherited_outcome(plan, donor, idx)
            inherited.inherit_reason = (
                "ship_inherit_prev"
                if donor.chunk_index < idx
                else "ship_inherit_forward"
            )
            inherited.used_level = "L_ship_inherit"
            inherited.inherit_donor_distance = abs(int(donor.chunk_index) - int(idx))
            logger.info(
                "chunk %d ship-fallback inherits non-adjacent shot from chunk %d",
                idx, donor.chunk_index,
            )
            return inherited

        return None

    @staticmethod
    def _short_ship_queries(plan: dict, motif_pool: list[str]) -> list[str]:
        """Broad-but-on-theme queries for an empty chunk's distinct-clip rescue.
        The chunk's OWN queries come first (each chunk differs → different clips),
        then the video-wide motif/theme pool, then must_show subjects."""
        out: list[str] = []
        seen: set[str] = set()
        forbidden = {
            " ".join(str(q or "").strip().lower().split())
            for q in (plan.get("forbidden_visual_pack") or [])
            if str(q or "").strip()
        }
        domain = str(plan.get("domain") or "")

        def add(raw: object) -> None:
            q = " ".join(str(raw or "").strip().split())
            key = q.lower()
            if not q or key in seen:
                return
            if any(term and term in key for term in forbidden):
                return
            if key in {
                "forest leaf macro veins",
                "river delta aerial",
                "river delta aerial top view",
                "data visualization",
            } and domain not in {"technology_explainer", "space_science"}:
                return
            seen.add(key)
            out.append(q)

        for q in (plan.get("queries") or [])[:3]:
            add(q)
        for q in (plan.get("metaphor_queries") or [])[:2]:
            add(q)
        for q in (plan.get("domain_motif_pool") or [])[:6]:
            add(q)
        for q in (motif_pool or [])[:6]:
            add(q)
        for q in (plan.get("must_show") or [])[:2]:
            add(q)
        return out

    def _short_ship_remote_fallback(
        self, plan: dict, idx: int, chunk_dir: Path, orientation: str,
        motif_pool: list[str], used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        """Fill an empty chunk with a DISTINCT broad-theme clip (reserved, so each
        empty chunk gets a different one — no single-clip loop). Lenient: only the
        must_not filter applies. Returns None if nothing fresh is found."""
        must_not = [
            str(t).strip().lower()
            for t in (plan.get("must_not_show") or [])
            if str(t).strip()
        ]
        for query in self._short_ship_queries(plan, motif_pool):
            try:
                candidates = self.footage.search_secondary(
                    query, orientation, kind="video", limit=6,
                )
            except Exception as e:  # noqa: BLE001 — fallback must not crash
                logger.warning(
                    "chunk %d short ship-remote search failed for %r: %s",
                    idx, query, e,
                )
                continue
            for cand in candidates:
                meta = " ".join([
                    str(getattr(cand, "source_tags", "") or ""),
                    str(getattr(cand, "source_url", "") or ""),
                    str(getattr(cand, "query", "") or ""),
                ]).lower()
                if any(term and term in meta for term in must_not):
                    continue
                outcome = self._v2_download_outcome(
                    plan, cand, chunk_dir, orientation,
                    used_level="L_ship_remote_distinct",
                    verify_reason=f"ship fallback: distinct broad theme '{query}'",
                    score=3.5,
                    used_source_ids=used_source_ids,
                    used_source_lock=used_source_lock,
                    allow_reuse=False,  # reserve → each empty chunk gets a different clip
                )
                if outcome is not None:
                    outcome.inherit_reason = "ship_remote_distinct"
                    logger.info(
                        "chunk %d short ship-fallback DISTINCT remote %s:%s q=%r",
                        idx, outcome.source, outcome.source_id, query,
                    )
                    return outcome
        return None

    def _short_video_subject_keys(self, plans: list[dict]) -> set:
        """The whole short's concrete subject(s): dominant concept-group member
        across all chunk plans. Reuses literal_probe's shared _subject_keys."""
        from collections import Counter
        counter: Counter = Counter()
        for plan in plans or []:
            for key in _subject_keys(plan or {}, []):
                counter[key] += 1
        if not counter:
            return set()
        best: dict = {}
        for (gi, canon), n in counter.items():
            if gi not in best or n > best[gi][1]:
                best[gi] = (canon, n)
        return {(gi, canon) for gi, (canon, _n) in best.items()}

    def _short_sweep_subject_mismatches(self, outcomes: list, video_subject: set) -> None:
        """Replace any shot whose tags name a DIFFERENT member of the video's
        subject group with the nearest on-subject shot. Path-independent backstop
        (mirrors the long-video sweep; short-video only)."""
        if not video_subject:
            return

        def mismatched(oc) -> bool:
            if not getattr(oc, "local_path", None):
                return False
            meta = str(getattr(oc, "source_tags", "") or "").lower()
            return bool(_subject_mismatch(meta, video_subject))

        clean = [i for i, oc in enumerate(outcomes) if oc.local_path and not mismatched(oc)]
        bad = sum(1 for oc in outcomes if mismatched(oc))
        if not bad:
            return
        if not clean:
            logger.warning(
                "short subject sweep: %d off-subject shot(s) but no on-subject donor", bad,
            )
            return
        swept = 0
        for i, oc in enumerate(outcomes):
            if not mismatched(oc):
                continue
            adjacent_keys = self._adjacent_asset_keys(outcomes, i)
            donor_indexes = [
                j for j in clean
                if abs(j - i) > 1
                and self._outcome_asset_key(outcomes[j]) not in adjacent_keys
                and self._v2_asset_repeat_available(
                    outcomes, self._outcome_asset_key(outcomes[j])
                )
            ]
            if not donor_indexes:
                logger.warning(
                    "short subject sweep: chunk %d has no non-adjacent on-subject donor; leaving original shot",
                    i,
                )
                continue
            donor = outcomes[min(donor_indexes, key=lambda j: abs(j - i))]
            new = self._build_inherited_outcome(oc.plan, donor, oc.chunk_index)
            new.inherit_reason = "subject_sweep"
            new.used_level = "L_subject_sweep"
            new.inherit_donor_distance = abs(int(donor.chunk_index) - int(oc.chunk_index))
            outcomes[i] = new
            swept += 1
        logger.warning(
            "short subject sweep replaced %d off-subject shot(s) (video subject=%s)",
            swept, sorted(c for _g, c in video_subject),
        )

    def _build_inherited_outcome(
        self,
        plan: dict,
        donor: ChunkOutcome,
        idx: int,
    ) -> ChunkOutcome:
        """Construct an inherited ChunkOutcome that reuses donor's clip.

        Copies asset fields (local_path, source, source_id, etc.) and stamps
        inheritance metadata (inherited_from_chunk, inherit_reason,
        used_level='INHERIT_FROM_PREV'). cost_usd=0 because no LLM/stock
        work was needed.
        """
        if donor.inherited_from_chunk is not None:
            # Chain inheritance: donor itself inherited. Allowed but worth
            # a warning so visual-continuity issues surface in audit logs.
            logger.warning(
                "chunk %d inherits from chunk %d which itself inherited "
                "from chunk %d — chain length growing, visual continuity "
                "check needed",
                idx, donor.chunk_index, donor.inherited_from_chunk,
            )
        return ChunkOutcome(
            chunk_index=idx,
            plan=plan,
            local_path=donor.local_path,
            duration=donor.duration,
            source=donor.source,
            source_id=donor.source_id,
            source_url=donor.source_url,
            resolution=donor.resolution,
            score=donor.score,
            decision="use",
            cost_usd=0.0,
            source_tags=donor.source_tags,
            # 继承镜头要带上主体水平位置,否则竖屏裁切退回盲居中、主体又偏到边上。
            subject_h_pos=getattr(donor, "subject_h_pos", None),
            safety_severity=donor.safety_severity,
            safety_flags=list(donor.safety_flags or []),
            safety_reason=donor.safety_reason,
            inherited_from_chunk=donor.chunk_index,
            inherit_reason=plan.get("inherit_reason", ""),
            used_level="INHERIT_FROM_PREV",
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Stage 2-3: stock attempt for one chunk
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _preflight_auth_check(self) -> None:
        """Fail fast when the cloud session is invalid or revoked.

        which reads `bool(self.refresh)` — i.e. "is a refresh token present
        in the OS keychain". This **silently accepts revoked tokens**:
        device, password change, server policy) and the local keychain
        never knows. Result: pre-flight passes, render starts, then the
        first LLM call mid-pipeline gets 401 → harness verify / mm_critic
        / moderation cascade through fail-closed defaults → user sees a
        broken video with no clear cause. This was the recurring "token
        revoked mid-session" pain.

        roundtrip via `gw.get_quota()` which:
          1. Reads access JWT
          2. If near expiry → refreshes (handles 401-on-refresh by
             clearing tokens and raising NotAuthenticatedError)
          3. Sends an authenticated GET → 401 if access was revoked
             server-side
        Either branch surfaces the failure HERE, before any pipeline work.

        Bonus: logs current cloud quota usage. If quota is already exhausted,
        warns the user — downstream LLM calls will fail-closed anyway, but
        we surface it explicitly so they can fix it instead of guessing.

        Bypass with MEDIA_BUDDY_ALLOW_OFFLINE=1 for local-only smoke
        scenarios where cloud LLMs aren't expected.
        """
        if os.environ.get("MEDIA_BUDDY_ALLOW_OFFLINE", "0") == "1":
            logger.warning(
                "MEDIA_BUDDY_ALLOW_OFFLINE=1 — skipping auth pre-flight; "
                "harness + mm_critic + moderation may silently no-op",
            )
            return

        from backend.lib.cloud_auth import (
            CloudTemporaryUnavailableError,
            NotAuthenticatedError,
            get_default_auth,
        )
        from backend.lib.cloud_gateway import get_default_gateway

        try:
            # Pre-flight uses the same singleton the rest of the pipeline will,
            # so a successful refresh here primes shared state for chunk-level
            # concurrency that follows.
            auth = get_default_auth()
            if not auth.is_authenticated:
                raise NotAuthenticatedError("no LLM key configured")
            gw = get_default_gateway()
            quota = asyncio.run(gw.get_quota())
        except NotAuthenticatedError as e:
            logger.warning("provider pre-flight: no usable LLM key (%s)", e)
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Add it in Settings → API Keys, then re-render."
            ) from e
        except CloudTemporaryUnavailableError:
            logger.warning("auth pre-flight: cloud temporarily unavailable")
            raise
        except Exception as e:
            logger.warning(
                "auth pre-flight crashed (%s) — refusing to render to avoid "
                "silent degradation", e,
            )
            raise RuntimeError(
                f"Provider pre-flight failed unexpectedly ({e}). Check your keys in Settings and try again."
            ) from e

        try:
            used = float(quota.get("used_eur", 0))
            limit = float(quota.get("limit_eur", 0))
        except (TypeError, ValueError):
            used = limit = 0.0
        resets_at = quota.get("resets_at", "?")
        if limit > 0 and used >= limit:
            logger.warning(
                "⚠️  cloud quota EXHAUSTED (%.2f/%.2f EUR, resets %s) — "
                "downstream LLM/AI calls will fail until quota refreshes. "
                "Render will likely fall back to local_fallback clips.",
                used, limit, resets_at,
            )
        else:
            logger.info(
                "auth pre-flight OK — token validated against cloud "
                "(quota %.2f/%.2f EUR)", used, limit,
            )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------











    # ------------------------------------------------------------------
    # Stage 4: AI fallback for one chunk
    # ------------------------------------------------------------------

    @staticmethod
    def _basic_file_check(path) -> tuple[bool, str]:
        """Local-only integrity check for trusted AI-generated clips.

        Returns (ok, error_reason). Performed in the trusted path INSTEAD
        of omni moderation + mm_critic — the trust is in the harness prompt
        that produced the clip, so we only verify the file is renderable.
        """
        from backend.lib.video_compose import _probe_duration
        try:
            p = Path(path)
            if not p.exists():
                return False, "file does not exist"
            if p.stat().st_size < 1024:
                return False, f"file too small ({p.stat().st_size} bytes)"
            duration = _probe_duration(p)
            if duration <= 0:
                return False, "ffprobe could not read duration (corrupt?)"
            return True, ""
        except Exception as e:
            return False, f"basic_file_check exception: {type(e).__name__}: {e}"


    # ------------------------------------------------------------------
    # Last-resort local fallback
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_fallback_duration(plan: dict) -> float:
        """Estimate the fallback clip duration from the chunk sentence so
        the placeholder roughly matches the TTS span. Was hard-coded 20s
        which made concat previews look like nothing-but-placeholder. Now
        approximates 4 Chinese chars/sec (typical narration pacing),
        clamped to 3-12s. The compose stage still trims to actual TTS
        duration in the final render; this just affects raw-file preview."""
        sentence = str(plan.get("sentence") or "")
        if not sentence:
            return 6.0
        char_count = len(sentence)
        # ~3.5 chars/sec for Chinese narration, ~4 for English short syllables
        est = char_count / 3.5
        return max(3.0, min(12.0, est))

    def _local_fallback_attempt(
        self, plan: dict, output_dir: Path, orientation: str,
        reason: Optional[str] = None,
    ) -> Optional[ChunkOutcome]:
        """Create a neutral local motion clip when both stock and AI fail."""
        idx = plan["chunk_index"]
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        duration = self._estimate_fallback_duration(plan)
        local = self._create_local_fallback_clip(
            chunk_dir, idx, orientation, duration_seconds=duration,
        )
        if not local:
            return None

        width, height = self._fallback_dimensions(orientation)
        logger.warning(
            "chunk %d: using local fallback clip (%.1fs) after asset miss (%s)",
            idx, duration, reason or "unknown reason",
        )
        return ChunkOutcome(
            chunk_index=idx,
            plan=plan,
            local_path=str(local),
            duration=duration,
            source="local_fallback",
            source_id=f"local-fallback-{idx}",
            source_url="",
            resolution=f"{width}x{height}",
            score=0.0,
            decision="fallback",
            cost_usd=0.0,
            error=reason,
        )

    @staticmethod
    def _fallback_dimensions(orientation: str) -> tuple[int, int]:
        if orientation == "portrait":
            return 720, 1280
        if orientation == "square":
            return 1080, 1080
        return 1280, 720

    def _create_local_fallback_clip(
        self, chunk_dir: Path, idx: int, orientation: str,
        duration_seconds: float = 20.0,
    ) -> Optional[Path]:
        chunk_dir.mkdir(parents=True, exist_ok=True)
        out_path = chunk_dir / "local_fallback.mp4"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

        ffmpeg = ensure_ffmpeg_on_path()
        if not ffmpeg:
            logger.error("local fallback clip unavailable: ffmpeg not found")
            return None

        width, height = self._fallback_dimensions(orientation)
        hue = (idx * 17) % 42
        vf = (
            "geq="
            f"r='18+{hue}+30*X/W+10*sin((X+N*4)/80)':"
            f"g='30+{hue // 2}+20*Y/H+8*sin((Y+N*3)/70)':"
            f"b='44+{hue}+55*Y/H+18*sin((X+Y+N*4)/100)',"
            "format=yuv420p"
        )
        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi",
            "-i", f"nullsrc=s={width}x{height}:r=30:d={duration_seconds:.3f}",
            "-vf", vf,
            *video_encoder_args(
                ffmpeg, crf="30", preset="ultrafast", bitrate="2200k",
                mpeg4_quality="7",
            ),
            "-an",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=240,
            )
        except Exception as e:
            logger.error("local fallback clip generation crashed: %s", e)
            return None
        if result.returncode != 0:
            logger.error(
                "local fallback ffmpeg failed (%s): %s",
                result.returncode, (result.stderr or "")[-800:],
            )
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        if not out_path.exists() or out_path.stat().st_size <= 0:
            logger.error("local fallback ffmpeg produced no output")
            return None
        return out_path


    # ------------------------------------------------------------------
    # AI decision normalization
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # ORM persistence (light-touch — for inspection only)
    # ------------------------------------------------------------------
    def _persist_chunk_plan(
        self, db: Session, project_id: str, plan: dict,
    ) -> Chunk:
        """Upsert a Chunk row from a plan dict."""
        chunk = (
            db.query(Chunk)
            .filter_by(project_id=project_id, chunk_index=plan["chunk_index"])
            .first()
        )
        # the per-shot persistence step (_persist_shot_outcome) can read them
        # without needing new ORM columns on Chunk. Final per-shot values
        v212_extra = {
            "atoms": plan.get("atoms", {}),
            "cascade_depth": plan.get("cascade_depth", 3),
            "prompts": plan.get("prompts", {}),
            # the asset-report API + diagnostics can see which chunks
            # forked to cinematic_ideation vs followed atom cascade.
            # cinematic_shots is set later by _stock_attempt_progressive
            # and merged in via _persist_shot_outcome.
            "is_abstract": bool(plan.get("is_abstract", False)),
        }
        if chunk is None:
            chunk = Chunk(
                id=str(uuid.uuid4()),
                project_id=project_id,
                chunk_index=plan["chunk_index"],
                sentence=plan["sentence"],
                target_shot_count=plan.get("target_shot_count", 1),
                shot_type=plan.get("shot_type", "atmosphere"),
                is_hero_shot=int(bool(plan.get("is_hero_shot", False))),
                motion_tags=plan.get("motion_tags", []),
                visual_tags=plan.get("visual_tags", []),
                priority=plan.get("priority", "normal"),
                extra=v212_extra,
                status="planning",
            )
            db.add(chunk)
        else:
            chunk.shot_type = plan.get("shot_type", chunk.shot_type)
            chunk.is_hero_shot = int(bool(plan.get("is_hero_shot", False)))
            chunk.motion_tags = plan.get("motion_tags", [])
            chunk.visual_tags = plan.get("visual_tags", [])
            chunk.priority = plan.get("priority", "normal")
            existing_extra = dict(chunk.extra) if chunk.extra else {}
            existing_extra.update(v212_extra)
            chunk.extra = existing_extra
        db.commit()
        return chunk

    def _persist_shot_outcome(
        self, db: Session, project_id: str, outcome: ChunkOutcome,
    ) -> None:
        """Persist a single Shot row for the chunk's primary selection."""
        chunk = (
            db.query(Chunk)
            .filter_by(project_id=project_id, chunk_index=outcome.chunk_index)
            .first()
        )
        if chunk is None:
            return
        # One representative shot row per chunk for now
        existing = db.query(Shot).filter_by(chunk_id=chunk.id, shot_index=0).first()
        if existing is None:
            existing = Shot(
                id=str(uuid.uuid4()),
                chunk_id=chunk.id, shot_index=0,
            )
            db.add(existing)
        existing.query = outcome.plan.get("search_query", "")
        existing.selected_local_path = outcome.local_path
        existing.selected_source = outcome.source
        existing.selected_source_id = outcome.source_id
        existing.selected_source_url = outcome.source_url
        existing.selected_clip_duration = outcome.duration
        existing.selected_resolution = outcome.resolution
        existing.mm_critic_score = outcome.score
        # onto the shot row. used_level + used_wave are observed-at-runtime
        # values set by Model A's _stock_attempt_progressive when a winner
        # is selected at a specific (wave, level) pair.
        plan = outcome.plan or {}
        try:
            existing.cascade_depth = int(plan.get("cascade_depth", 3))
        except (TypeError, ValueError):
            existing.cascade_depth = 3
        existing.prompts = plan.get("prompts") or {}
        existing.used_level = outcome.used_level   # "L1".."L5" / "L5_image" / None
        existing.used_wave = outcome.used_wave
        existing.asset_type = outcome.asset_type or "video"
        existing.motion_type = outcome.motion_type
        existing.selected_prompt = outcome.selected_prompt or ""
        existing.verify_reason = outcome.verify_reason or ""
        existing.confidence = outcome.confidence or 0.0
        existing.fallback_reason = outcome.fallback_reason or ""
        _cc = None
        if _cc is not None:
            intent = (
                getattr(_cc, "what_it_communicates", "") or
                getattr(_cc, "visual_intent", "")
            )
            existing.director_intent = (intent or "")[:500]
        existing.mm_critic_notes = {
            "decision": outcome.decision,
            "cost_usd": outcome.cost_usd,
            "error": outcome.error,
            "source_tags": outcome.source_tags,
            "verify_scores": outcome.verify_scores or {},
            "verify_test_results": outcome.verify_test_results or [],
            "cascade_trace": outcome.cascade_trace or {},
        }
        existing.status = "approved" if outcome.local_path and outcome.decision != "reject" else "failed"
        chunk.status = (
            "approved" if existing.status == "approved" else "failed"
        )
        chunk.mm_critic_avg = outcome.score
        # stamped on outcome.plan by _stock_attempt_progressive during
        # search; _persist_chunk_plan ran BEFORE search so it didn't see
        # this. The asset-report API and diagnostics read chunk.extra.
        cine = (outcome.plan or {}).get("cinematic_shots")
        if cine:
            existing_extra = dict(chunk.extra) if chunk.extra else {}
            existing_extra["cinematic_shots"] = cine
            chunk.extra = existing_extra
        # this function was relying on the caller's commit, but Shot rows
        # only landed in the DB by luck of autoflush. Mirror
        # _persist_chunk_plan and commit explicitly.
        db.commit()


def _dict_to_candidate(d: dict, query: str) -> FootageCandidate:
    """Reconstruct FootageCandidate from cached dict (no _raw, so download
    won't work — caller should re-search if download needed)."""
    return FootageCandidate(
        source=d.get("source", ""),
        source_id=d.get("source_id", ""),
        source_url=d.get("source_url", ""),
        thumbnail_url=d.get("thumbnail_url", ""),
        duration=float(d.get("duration", 0)),
        width=int(d.get("width", 0)),
        height=int(d.get("height", 0)),
        license=d.get("license", ""),
        query=query,
        _raw=None,
    )
