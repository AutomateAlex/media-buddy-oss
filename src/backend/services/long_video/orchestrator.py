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
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.lib.llm_client import LLMClient
from backend.lib.pipeline_mode import PipelineMode, get_mode_config
from backend.models.chunk import Chunk, Shot
from backend.models.project import Project
from backend.services.asset_cache import AssetCache
from backend.services.footage_service import FootageCandidate, FootageService
from backend.services.generated_cache import GeneratedCache
from backend.services.long_video.visual_focus import build_long_video_global_plan
from backend.services.literal_probe import (
    _subject_keys,
    _subject_mismatch,
    probe_broadened_subject_candidates,
    probe_library_candidates,
    probe_literal_candidates,
    probe_metaphor_candidates,
    probe_motif_candidate,
    probe_motif_candidate_lenient,
    probe_similar_category_candidates,
    probe_similar_subject_candidates,
)
from backend.services.observer_judge import HaikuReviewBudget, judge_candidate
from backend.services.reframe_service import ReframeService, _orientation_to_aspect
from backend.services.shot_router import ShotRouter
from backend.services.video_gen_service import VideoGenService

logger = logging.getLogger(__name__)


def build_global_plan(
    llm: LLMClient,
    sentences: list[str],
    *,
    user_brief: str = "",
) -> dict[str, Any]:
    """Long-video local compatibility wrapper.

    Tests and older monkeypatches target this symbol, but production now routes
    through the long-video visual_focus repair instead of using the shared plan
    directly.
    """
    return build_long_video_global_plan(llm, sentences, user_brief=user_brief)


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
# to this long-video module; short video keeps MEDIA_BUDDY_TOP_N=3. Library/tag
# matches already skip observer entirely (see _v2_review_candidates).
DEFAULT_TOP_N = int(os.environ.get("MEDIA_BUDDY_LONG_TOP_N", "2"))

# (similar_subject / similar_category / motif) judge only the single best
# candidate instead of top_n. These levels are reached only when literal +
# candidates are weak anyway, so judging a 2nd one rarely changes the verdict
# but doubles the observer (gemini) cost there. The shallow levels (library /
DEEP_TOP_N = int(os.environ.get("MEDIA_BUDDY_LONG_DEEP_TOP_N", "1"))

# Long-video cost cut: abstract/transition chunks should not burn stock search
# and Gemini calls. They are safest when filled from the current video's
# already-accepted visual pool after concrete chunks have been selected.
POOL_FIRST_COST_CUT_ENABLED = os.environ.get(
    "MEDIA_BUDDY_LONG_POOL_FIRST_COST_CUT", "1"
).lower() in {"1", "true", "yes", "on"}


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


class LongVideoOrchestrator:
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
        # niche→通用 退化查询缓存(同一失败主体只算一次 LLM)。
        self._broaden_cache: dict[str, list[str]] = {}
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
        # Phase 2.11 — letterbox reframer for 16:9 → 9:16 (and similar)
        self.reframer = ReframeService()
        # build fails (LLM auth / cloud quota / etc.), these stay None / {}
        # Long-video-only runtime query health. This keeps repeated bad title
        # queries from burning Gemini calls after the first few rejects.
        self._long_query_failure_cache: dict[str, dict[str, int]] = {}
        self._long_query_failure_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Phase 2.11 — letterbox reframe helper
    # ------------------------------------------------------------------
    def _reframe_if_needed(self, local: Path, orientation: str) -> Path:
        """If `orientation` requests a non-landscape aspect, run the source
        through ReframeService. Returns the reframed file (or the original
        if it already matched / reframe failed). Idempotent for matching aspects."""
        target = _orientation_to_aspect(orientation)
        return self._reframe_to_aspect(local, target)

    def _reframe_to_aspect(self, local: Path, target_aspect: Optional[str]) -> Path:
        """Same as `_reframe_if_needed` but accepts an aspect string directly
        (e.g. '9:16'). Used by AI generation paths where aspect is already
        plumbed through."""
        if target_aspect is None or target_aspect == "16:9":
            return local
        try:
            res = self.reframer.to_aspect(
                local, target_aspect=target_aspect, out_dir=Path(local).parent,
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
        """Long video is stock-only -> always the v2 pipeline. The legacy v1
        (harness / AI-gen / progressive search) path was pruned from this
        isolated long-video copy."""
        logger.info("long video: v2 stock-only pipeline")
        return self._run_v2_inner(
            db, project_id, sentences, output_dir, orientation, top_n,
            llm_counter=llm_counter,
        )

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

        self._long_query_failure_cache = {}
        global_plan = build_global_plan(self.llm, sentences, user_brief=user_brief)
        plans = list(global_plan.get("chunks") or [])
        motif_pool = list((global_plan.get("video_meta") or {}).get("motif_pool") or [])
        # 导演现场落盘(best-effort):长视频此前完全不落选材诊断(observer/visual/
        # "为什么选了这段画面"。这里补上 director_raw.json —— 完整 global_plan 带
        # video_meta.motif_pool_source + meta.director_degraded/degradation_reasons +
        # 每 chunk queries,是"导演空转→退化"的根因证据。与短视频同源同格式。
        # 写流水线根 output_dir.parent.parent(=assets/footage 的上两级),渲完由 worker
        try:
            import json as _json_dr
            (output_dir.parent.parent / "director_raw.json").write_text(
                _json_dr.dumps(global_plan, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("long director_raw.json write failed", exc_info=True)

        for plan in plans:
            self._persist_chunk_plan(db, project_id, plan)

        budget = HaikuReviewBudget(max_per_video=int(
            os.environ.get("MEDIA_BUDDY_HAIKU_REVIEW_BUDGET", "3")
        ))
        outcomes: list[Optional[ChunkOutcome]] = [None] * len(plans)
        used_source_ids: set[str] = set()
        used_source_lock = threading.Lock()
        selection_jobs: list[tuple[int, dict]] = []

        for idx, plan in enumerate(plans):
            if self._v2_has_visual_anchor(plan):
                plan["inherit_prev"] = False
                plan["inherit_forward"] = False
                plan["inherit_reason"] = ""
            if idx > 0 and self._v2_should_inherit_fragment(plan):
                plan["inherit_prev"] = True
                plan["inherit_reason"] = plan.get("inherit_reason") or "sentence_fragment"
            if self._v2_should_defer_to_visual_pool(plan):
                outcomes[idx] = ChunkOutcome(
                    chunk_index=idx, plan=plan, local_path=None,
                    duration=0, source="", source_id="", source_url="",
                    resolution="", score=0.0, decision="pending_inherit",
                    cost_usd=0.0,
                    inherit_reason=plan.get("inherit_reason") or "pool_first_visual_focus",
                    used_level="L_atmosphere_pool",
                )
                continue
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
                        )
                else:
                    outcomes[idx] = self._v2_select_chunk(
                        plan, output_dir, orientation, top_n, motif_pool,
                        budget, used_source_ids,
                        used_source_lock=used_source_lock,
                    )
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                future_to_idx = {
                    ex.submit(
                        self._v2_select_chunk_with_counter,
                        plan, output_dir, orientation, top_n, motif_pool,
                        budget, used_source_ids, used_source_lock,
                        llm_counter,
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
                # Adjacent-source dedup (quality A): prefer a donor whose clip
                # differs from both neighbours so the same shot never lands
                # back-to-back. Relax if that leaves no donor (must ship).
                neighbor_sids: set[str] = set()
                for nb in (idx - 1, idx + 1):
                    if 0 <= nb < len(outcomes) and outcomes[nb] is not None:
                        if outcomes[nb].source_id:
                            neighbor_sids.add(outcomes[nb].source_id)
                donor = self._find_visual_focus_pool_donor(
                    outcomes, idx, plan, exclude=exhausted_donors,
                    exclude_source_ids=neighbor_sids,
                )
                if donor is not None:
                    pooled = self._build_inherited_outcome(plan, donor, idx)
                    pooled.inherit_reason = "visual_focus_pool"
                    pooled.used_level = (
                        "L_atmosphere_pool"
                        if str(plan.get("visual_focus") or "") in {"abstract_broll", "transition"}
                        else "L_focus_pool"
                    )
                    outcomes[idx] = pooled
                    inherit_donor_counts[donor.chunk_index] = (
                        inherit_donor_counts.get(donor.chunk_index, 0) + 1
                    )
                    if pooled.source_id:
                        used_source_ids.add(pooled.source_id)
                        if pooled.source:
                            used_source_ids.add(f"{pooled.source}:{pooled.source_id}")
                    continue
                donor = self._find_donor_outcome_for_aggressive(
                    outcomes, idx, exclude=exhausted_donors,
                    exclude_source_ids=neighbor_sids,
                )
                if donor is None and neighbor_sids:
                    donor = self._find_donor_outcome_for_aggressive(
                        outcomes, idx, exclude=exhausted_donors,
                    )
            if donor is None:
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

        final = [oc for oc in outcomes if oc is not None]
        # Video-level subject sweep (last line, path-independent): some shots can
        # still be off-subject (e.g. wolf clips in a wild-boar video) if they were
        # chosen on a chunk whose own plan lacked the subject signal. Compute the
        # whole video's subject from ALL chunk plans, then replace any shot whose
        # tags name a different member of that concept group with the nearest
        # on-subject shot already in hand.
        try:
            self._v2_sweep_subject_mismatches(final, self._video_subject_keys(plans))
        except Exception:  # noqa: BLE001 — a sweep failure must never kill a render
            logger.exception("v2 subject sweep failed (non-fatal)")
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
        db.commit()
        # 选材诊断 observer_debug.{json,html}:原本门控在 MEDIA_BUDDY_OBSERVER_DEBUG
        # 现改为【总是产出】,与短视频一致(见 _emit_short_observer_debug_report)——
        try:
            self._emit_observer_debug_report(plans, outcomes, output_dir)
        except Exception:
            logger.exception("observer_debug report failed (non-fatal)")
        return final

    @staticmethod
    def _v2_debug_candidate_extras(cand) -> dict:
        """
        source's free preview assets, so observer_debug.html can reveal
        "thumbnail misleading but video_pictures/preview good". Lazy & inert —
        the fields are only consumed when the debug report is emitted."""
        # Strict isinstance guards — these fields land in cascade_trace which is
        # JSON-serialized into the shots table, so anything non-primitive (e.g.
        # a test MagicMock candidate) must coerce to "" rather than leak through.
        def _s(x):
            return x if isinstance(x, str) else ""

        raw = getattr(cand, "_raw", None)
        extra = getattr(raw, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
        preview: dict = {}
        vps = extra.get("video_pictures")
        if isinstance(vps, list) and vps:
            pics = sorted(
                (p for p in vps if isinstance(p, dict)),
                key=lambda p: p.get("nr", 0),
            )
            urls = [p["picture"] for p in pics if isinstance(p.get("picture"), str)]
            if len(urls) > 3:
                idxs = sorted({round((len(urls) - 1) * f) for f in (0.2, 0.5, 0.8)})
                urls = [urls[i] for i in idxs]
            if urls:
                preview["video_pictures"] = urls
        pvu = extra.get("preview_video_url")
        if isinstance(pvu, str) and pvu:
            preview["preview_video_url"] = pvu
        return {
            "thumbnail_url": _s(getattr(cand, "thumbnail_url", "")),
            "query": _s(getattr(cand, "query", "")),
            "source_tags": _s(getattr(cand, "source_tags", ""))[:300],
            "preview_assets": preview,
        }

    @staticmethod
    def _emit_observer_debug_report(plans, outcomes, output_dir) -> None:
        """
        Gemini saw + verdict + reason + sub-scores + the source's FREE preview
        assets (Pexels video_pictures / stock preview mp4). Written to the
        project dir (survives the post-export asset cleanup)."""
        import json as _json
        import html as _html
        from collections import Counter as _Counter
        from pathlib import Path as _Path

        # output_dir is .../<project>/assets/footage → write to project root.
        out = _Path(output_dir).parent.parent
        out.mkdir(parents=True, exist_ok=True)

        oc_by_idx = {
            int(getattr(oc, "chunk_index", -1)): oc
            for oc in outcomes if oc is not None
        }
        total = accept = reject = ambiguous = no_thumb_rejects = 0
        total_image_count = image_call_count = multi_frame_count = 0
        query_disabled_count = accepted_but_inherited_count = 0
        used_level_counts: dict = {}
        source_counts: dict = {}
        visual_focus_counts: dict = {}
        failed_queries = _Counter()
        chunks_data = []
        for plan in plans:
            idx = int(plan.get("chunk_index", -1))
            metrics = plan.get("_v2_detection_metrics") or {}
            verdicts = metrics.get("candidate_verdicts") or []
            oc = oc_by_idx.get(idx)
            used_level = (getattr(oc, "used_level", "") if oc else "") or "(none)"
            used_level_counts[used_level] = used_level_counts.get(used_level, 0) + 1
            visual_focus = str(plan.get("visual_focus") or "(unset)")
            visual_focus_counts[visual_focus] = visual_focus_counts.get(visual_focus, 0) + 1
            total_image_count += int(metrics.get("total_image_count", 0) or 0)
            image_call_count += int(metrics.get("image_count_call_count", 0) or 0)
            multi_frame_count += int(metrics.get("multi_frame_count", 0) or 0)
            query_disabled_count += int(metrics.get("query_disabled_count", 0) or 0)
            has_accept = any((v.get("verdict") or "").lower() == "accept" for v in verdicts)
            if has_accept and "inherit" in used_level.lower():
                accepted_but_inherited_count += 1
            for v in verdicts:
                total += 1
                vd = (v.get("verdict") or "").lower()
                if vd == "accept":
                    accept += 1
                elif vd == "reject":
                    reject += 1
                    if not v.get("thumbnail_url"):
                        no_thumb_rejects += 1
                    if v.get("query"):
                        failed_queries[str(v.get("query"))] += 1
                else:
                    ambiguous += 1
                src = v.get("source") or "?"
                source_counts[src] = source_counts.get(src, 0) + 1
            chunks_data.append({
                "idx": idx,
                "sentence": plan.get("sentence") or plan.get("text") or "",
                "visual_focus": visual_focus,
                "must_show": plan.get("must_show") or [],
                "must_not_show": plan.get("must_not_show") or [],
                "queries": plan.get("queries") or [],
                "intent": plan.get("intent") or "",
                "is_abstract": plan.get("is_abstract"),
                "used_level": used_level,
                "accept_unused": metrics.get("accept_unused") or [],
                "verdicts": verdicts,
            })
        pass_rate = (accept / total * 100.0) if total else 0.0
        avg_image_count = (
            round(total_image_count / image_call_count, 3)
            if image_call_count else 0.0
        )
        top_failed_queries = failed_queries.most_common(12)

        (out / "observer_debug.json").write_text(_json.dumps({
            "summary": {
                "total_candidates": total, "accept": accept, "reject": reject,
                "ambiguous": ambiguous, "pass_rate_pct": round(pass_rate, 1),
                "no_thumbnail_rejects": no_thumb_rejects,
                "used_level_counts": used_level_counts, "source_counts": source_counts,
                "visual_focus_counts": visual_focus_counts,
                "accepted_but_inherited_count": accepted_but_inherited_count,
                "total_image_count": total_image_count,
                "image_call_count": image_call_count,
                "avg_image_count_per_call": avg_image_count,
                "multi_frame_count": multi_frame_count,
                "query_disabled_count": query_disabled_count,
                "top_failed_queries": top_failed_queries,
            },
            "chunks": chunks_data,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        esc = _html.escape

        def _badge(vd: str) -> str:
            color = {"accept": "#1a7f37", "reject": "#cf222e"}.get(vd, "#9a6700")
            return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-weight:600">{esc(vd or "?")}</span>')

        p = ['<!doctype html><html lang="zh"><head><meta charset="utf-8">'
             '<title>Observer Debug</title><style>'
             'body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}'
             '.wrap{max-width:1240px;margin:0 auto;padding:20px}'
             '.sum{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:20px}'
             '.sum b{font-size:22px}.chunk{background:#fff;border:1px solid #d0d7de;border-radius:8px;margin-bottom:16px;padding:14px}'
             '.chunk h3{margin:0 0 6px}.meta{font-size:13px;color:#57606a;margin-bottom:8px}'
             '.tag{display:inline-block;background:#ddf4ff;color:#0969da;padding:1px 6px;border-radius:4px;margin:1px;font-size:12px}'
             '.no{background:#ffebe9;color:#cf222e}.inh{background:#fff8c5;color:#9a6700;font-weight:700;padding:2px 8px;border-radius:4px}'
             '.cands{display:flex;flex-wrap:wrap;gap:10px}'
             '.card{border:1px solid #d0d7de;border-radius:6px;padding:8px;width:350px;background:#fafbfc}'
             '.card img{width:150px;border-radius:4px;vertical-align:top;border:1px solid #d0d7de}'
             '.frames img,.frames video{width:96px}.reason{font-size:12px;margin-top:6px;white-space:pre-wrap}'
             '.sub{font-size:11px;color:#57606a;margin-top:4px}'
             '.noimg{display:inline-block;width:150px;height:84px;background:#ffebe9;color:#cf222e;text-align:center;line-height:84px;border-radius:4px;font-size:12px}'
             '</style></head><body><div class="wrap">']
        sl = " · ".join(f"{esc(str(k))}:{v}" for k, v in sorted(used_level_counts.items(), key=lambda x: -x[1]))
        sc = " · ".join(f"{esc(str(k))}:{v}" for k, v in sorted(source_counts.items(), key=lambda x: -x[1]))
        p.append(f'<div class="sum"><b>通过率 {pass_rate:.1f}%</b> &nbsp;(accept {accept} / 候选 {total})'
                 f' &nbsp;|&nbsp; reject {reject} · ambiguous {ambiguous} · 无图reject {no_thumb_rejects}'
                 f'<div class="meta">used_level: {sl}<br>source: {sc}</div>'
                 f'人工标每个 reject：A 有效(候选垃圾)/B 缩略图骗人/C 无图证据不足/D 太严。看右侧免费预览能否救回。</div>')
        for c in chunks_data:
            inh = ' <span class="inh">INHERIT</span>' if "inherit" in c["used_level"].lower() else ""
            ms = "".join(f'<span class="tag">{esc(str(x))}</span>' for x in c["must_show"]) or "—"
            mns = "".join(f'<span class="tag no">{esc(str(x))}</span>' for x in c["must_not_show"]) or "—"
            p.append(f'<div class="chunk"><h3>#{c["idx"]} · {esc(c["used_level"])}{inh}</h3>'
                     f'<div class="meta">{esc(str(c["sentence"]))}</div>'
                     f'<div class="meta">must_show: {ms} &nbsp; must_not_show: {mns} &nbsp;'
                     f' intent: {esc(str(c["intent"]))} &nbsp; abstract: {esc(str(c["is_abstract"]))}</div><div class="cands">')
            for v in c["verdicts"]:
                thumb = v.get("thumbnail_url") or ""
                img = (f'<img src="{esc(thumb)}" loading="lazy">' if thumb
                       else '<span class="noimg">无图</span>')
                j = v.get("judgment") or {}
                jj = j.get("judgment") if isinstance(j, dict) and isinstance(j.get("judgment"), dict) else j
                subs = ""
                if isinstance(jj, dict):
                    subs = (f'role_fit:{jj.get("scene_role_fit")} · violated:'
                            f'{esc(str(jj.get("must_not_show_violated")))} · style:'
                            f'{jj.get("style_consistency")} · show_cov:{jj.get("must_show_coverage")}')
                pa = v.get("preview_assets") or {}
                frames = ""
                if pa.get("video_pictures"):
                    frames = ('<div class="frames">免费多帧: '
                              + "".join(f'<img src="{esc(u)}" loading="lazy">' for u in pa["video_pictures"])
                              + '</div>')
                elif pa.get("preview_video_url"):
                    frames = (f'<div class="frames">预览mp4: <video src="{esc(pa["preview_video_url"])}"'
                              f' width="150" controls preload="none"></video></div>')
                p.append(f'<div class="card">{img} {_badge((v.get("verdict") or "").lower())} '
                         f'<span class="sub">{esc(v.get("source") or "")}·{esc(v.get("mode") or "")}</span>'
                         f'<div class="reason">{esc(str(v.get("reason") or ""))}</div>'
                         f'<div class="sub">{subs}</div>{frames}</div>')
            p.append('</div></div>')
        p.append('</div></body></html>')
        (out / "observer_debug.html").write_text("".join(p), encoding="utf-8")
        logger.warning(
            "observer_debug written: %s (pass_rate=%.1f%% accept=%d/%d reject=%d)",
            out / "observer_debug.html", pass_rate, accept, total, reject,
        )

    @staticmethod
    def _v2_should_inherit_fragment(plan: dict) -> bool:
        sentence = str(plan.get("sentence") or plan.get("text") or "").strip()
        compact = re.sub(r"[\s，。！？,.!?；;：:、]+", "", sentence)
        if not compact:
            return False
        # ("于是" "但是" "更离谱的是") continue the previous shot; 5-8 char
        # sentences now earn their own footage search instead of repeating the
        # neighbour clip. Cuts the visible "same shot twice in a row" the user
        if len(compact) > 4:
            return False
        if LongVideoOrchestrator._v2_has_visual_anchor(plan):
            return False
        if re.search(r"\d", sentence):
            return False
        return True

    @staticmethod
    def _v2_should_defer_to_visual_pool(plan: dict) -> bool:
        if not POOL_FIRST_COST_CUT_ENABLED:
            return False
        focus = str(plan.get("visual_focus") or "").strip()
        if focus not in {"abstract_broll", "transition"}:
            return False
        if plan.get("visual_anchor"):
            return False
        if plan.get("inherit_prev") or plan.get("inherit_forward"):
            return False
        if plan.get("must_show"):
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

    # ── 从短视频 orchestrator 移植的选片精准化(长视频独立一份,短视频不受影响)──
    @staticmethod
    def _v2_flatten_text(value: Any) -> str:
        parts: list[str] = []
        if isinstance(value, dict):
            for item in value.values():
                text = LongVideoOrchestrator._v2_flatten_text(item)
                if text:
                    parts.append(text)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                text = LongVideoOrchestrator._v2_flatten_text(item)
                if text:
                    parts.append(text)
        elif value is not None:
            parts.append(str(value))
        return " ".join(parts).lower()

    def _v2_broadened_queries_for_plan(self, plan: dict) -> list[str]:
        """失败的 niche 主体 → LLMClient.broaden_subject_for_stock 退成通用查询(同主体缓存,只算一次)。"""
        lead_query = ""
        for q in (plan.get("queries") or []):
            s = self._v2_flatten_text(q).strip()
            if s:
                lead_query = s
                break
        if not lead_query:
            lead_query = self._v2_flatten_text((plan.get("must_show") or [""])[0]).strip()
        if not lead_query:
            return []
        key = lead_query.lower()
        if key in self._broaden_cache:
            return self._broaden_cache[key]
        sentence = self._v2_flatten_text(plan.get("sentence") or plan.get("text") or "")
        try:
            queries = self.llm.broaden_subject_for_stock(lead_query, sentence)
        except Exception as e:
            logger.warning("broaden_subject_for_stock failed for %r: %s", lead_query, e)
            queries = []
        self._broaden_cache[key] = queries
        if queries:
            logger.info("long niche-broaden %r -> %s", lead_query, queries)
        return queries

    def _v2_broadened_subject_candidates(
        self, plan: dict, orientation: str, top_n: int, *, used_source_ids: set[str],
    ) -> list[FootageCandidate]:
        """niche 主体的精确池全失 → 搜 LLM 退化后的通用查询(如 'kangaroo rat' → 'desert rodent')。
        长视频独立开关 MEDIA_BUDDY_LONG_BROADEN_NICHE(默认开);只在 literal 没命中之后才跑,
        """
        if os.environ.get("MEDIA_BUDDY_LONG_BROADEN_NICHE", "1").strip().lower() in ("0", "false", "no", ""):
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
            logger.warning("long broadened-subject probe failed: %s", e)
            return []

    @staticmethod
    def _v2_candidate_matches_must_show(cand: object, plan: dict) -> bool:
        """候选素材标签是否命中本句 must_show / hard_anchors。给"无判官兜底"(motif rescue)当地板:
        兜底不能把一个和 must_show(如 rats)完全不沾边的镜头硬塞进片子。must_show 为空 → 不设地板
        (True);有 must_show 但候选无标签 → 保守视为不匹配(兜底处宁严勿松)。"""
        anchors: list[str] = []
        for keyname in ("must_show", "hard_anchors"):
            for x in (plan.get(keyname) or []):
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
            if a.endswith("s") and len(a) > 2 and a[:-1] in tags:
                return True
            if (a + "s") in tags:
                return True
        return False

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
    ) -> ChunkOutcome:
        idx = int(plan["chunk_index"])
        chunk_dir = output_dir / f"chunk_{idx:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        library = probe_library_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            motif_pool=motif_pool,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, library, chunk_dir, orientation,
            mode="metaphor", used_level="L_library", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        literal = probe_literal_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, literal, chunk_dir, orientation,
            mode="literal", used_level="L_literal", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        # L_broadened_subject:niche 主体精确池全失 → 退成通用查询(desert rodent…)再搜一轮,仍过判官。
        broadened = self._v2_broadened_subject_candidates(
            plan, orientation, top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, broadened, chunk_dir, orientation,
            mode="similar_subject", used_level="L_broadened_subject", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        metaphor = probe_metaphor_candidates(
            self.footage, plan, orientation=orientation, top_n=top_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, metaphor, chunk_dir, orientation,
            mode="metaphor", used_level="L_metaphor", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        deep_n = max(1, min(top_n, DEEP_TOP_N))
        similar_subject = probe_similar_subject_candidates(
            self.footage, plan, orientation=orientation, top_n=deep_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, similar_subject, chunk_dir, orientation,
            mode="similar_subject", used_level="L_similar_subject", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        similar_category = probe_similar_category_candidates(
            self.footage, plan, motif_pool, orientation=orientation, top_n=deep_n,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        selected = self._v2_review_candidates_guarded(
            plan, similar_category, chunk_dir, orientation,
            mode="similar_category", used_level="L_similar_category", budget=budget,
            used_source_ids=used_source_ids,
            used_source_lock=used_source_lock,
        )
        if selected is not None:
            return selected

        motif = probe_motif_candidate(
            self.footage, motif_pool, plan, orientation=orientation,
            used_source_ids=self._v2_used_source_snapshot(
                used_source_ids, used_source_lock,
            ),
        )
        # must_show 地板:motif 是"无判官兜底",绝不能把和 must_show 完全不沾边的镜头硬塞进来
        # (短视频"老鼠那句配蜜蜂"的同款漏洞)。不命中就当没兜到,落到下面 stock_miss 让填充逻辑处理。
        if motif is not None and not self._v2_candidate_matches_must_show(motif, plan):
            logger.info(
                "long motif rescue rejected (must_show floor): chunk=%s tags=%r",
                plan.get("chunk_index"), (getattr(motif, "source_tags", "") or "")[:80],
            )
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
    def _long_v2_max_observer_per_chunk() -> int:
        """每句判官看缩略图的张数上限(0=不限)。导演修好后头几张通常就对,封顶砍掉
        '一路跌到底级、狂看十几张'的慢尾。env MEDIA_BUDDY_LONG_V2_MAX_OBSERVER_PER_CHUNK。"""
        try:
            return max(0, int(os.environ.get("MEDIA_BUDDY_LONG_V2_MAX_OBSERVER_PER_CHUNK", "4")))
        except ValueError:
            return 4

    def _v2_review_candidates_guarded(
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
    ) -> Optional[ChunkOutcome]:
        metrics = self._v2_detection_metrics(plan)
        for i, cand in enumerate(candidates):
            _unsafe = self._v2_asset_content_unsafe(cand)
            if _unsafe:
                self._v2_append_candidate_verdict(
                    metrics, cand, mode=mode, verdict="reject",
                    model_id="content_safety", reason=_unsafe,
                    elapsed_seconds=0.0, est_cost_usd=0.0, judgment=None,
                    image_count=0, accepted_used=None,
                )
                continue
            if cand.source == "library" and used_level == "L_library":
                self._v2_append_candidate_verdict(
                    metrics, cand, mode=mode, verdict="accept",
                    model_id="local_library_tags", reason="local library tags accepted",
                    elapsed_seconds=0.0, est_cost_usd=0.0, judgment=None,
                    image_count=0, accepted_used=None,
                )
                outcome = self._v2_download_outcome(
                    plan, cand, chunk_dir, orientation,
                    used_level=used_level,
                    verify_reason=(
                        "local library tag match: "
                        f"{(cand.source_tags or cand.query or '')[:420]}"
                    ),
                    score=8.0,
                    used_source_ids=used_source_ids,
                    used_source_lock=used_source_lock,
                )
                if outcome is not None:
                    outcome.semantic_pass_source = "local_library_tags"
                    outcome.mm_quality_pass = True
                    outcome.review_skipped = True
                    outcome.review_skip_reason = "v2_local_library_tags_no_cloud_review"
                    outcome.verify_test_results = [{
                        "test_id": "local_library_tags",
                        "passed": True,
                        "reason": "Local library metadata survived title/tag filtering.",
                    }]
                    outcome.verify_scores = {
                        "local_library_tag_accept": 1,
                        "detection_calls": metrics.get("detection_calls", 0),
                        "detection_elapsed_seconds": metrics.get("detection_elapsed_seconds", 0.0),
                        "detection_est_cost_usd": metrics.get("detection_est_cost_usd", 0.0),
                    }
                    outcome.cascade_trace["v2_detection"] = dict(metrics)
                    return outcome
                self._v2_record_accept_unused(plan, cand, metrics)
                continue

            query = str(getattr(cand, "query", "") or "")
            if self._long_query_is_disabled(query):
                metrics["query_disabled_count"] = int(metrics.get("query_disabled_count", 0)) + 1
                self._v2_append_candidate_verdict(
                    metrics, cand, mode=mode, verdict="reject",
                    model_id="query_failure_cache",
                    reason="query disabled after repeated low-value results in this long-video run",
                    elapsed_seconds=0.0, est_cost_usd=0.0, judgment=None,
                    image_count=0, accepted_used=None,
                )
                continue

            # 判官每句缩略图上限:到顶就停判,落到下面 motif/兜底(计数存 plan,跨级别累计)。
            _obs_cap = self._long_v2_max_observer_per_chunk()
            if _obs_cap and int(plan.get("_v2_observer_calls", 0)) >= _obs_cap:
                metrics["observer_capped"] = int(metrics.get("observer_capped", 0)) + 1
                break
            plan["_v2_observer_calls"] = int(plan.get("_v2_observer_calls", 0)) + 1
            result = judge_candidate(
                cand, plan, mode=mode,
                is_last_candidate=(i == len(candidates) - 1),
                budget=budget,
            )
            image_count = 1 if getattr(cand, "thumbnail_url", "") else 0
            call_count = max(1, int(result.call_count or 1))
            metrics["detection_calls"] = int(metrics.get("detection_calls", 0)) + call_count
            metrics["detection_elapsed_seconds"] = round(
                float(metrics.get("detection_elapsed_seconds", 0.0))
                + float(result.elapsed_seconds or 0.0),
                3,
            )
            metrics["detection_est_cost_usd"] = round(
                float(metrics.get("detection_est_cost_usd", 0.0))
                + float(result.est_cost_usd or 0.0),
                6,
            )
            metrics["total_image_count"] = int(metrics.get("total_image_count", 0)) + image_count * call_count
            metrics["image_count_call_count"] = int(metrics.get("image_count_call_count", 0)) + call_count
            self._v2_append_candidate_verdict(
                metrics, cand, mode=mode, verdict=result.verdict,
                model_id=result.model_id,
                reason=result.reason if isinstance(result.reason, str) else "",
                elapsed_seconds=result.elapsed_seconds,
                est_cost_usd=result.est_cost_usd,
                judgment=result.raw if isinstance(getattr(result, "raw", None), dict) else None,
                image_count=image_count * call_count,
                accepted_used=None,
            )
            if result.verdict != "accept":
                self._long_record_query_verdict(query, result.verdict)
                continue

            outcome = self._v2_download_outcome(
                plan, cand, chunk_dir, orientation,
                used_level=used_level,
                verify_reason=result.reason,
                score=8.0 if not result.escalated else 8.5,
                used_source_ids=used_source_ids,
                used_source_lock=used_source_lock,
            )
            if outcome is not None:
                outcome.semantic_pass_source = (
                    "observer_judge_haiku" if result.escalated else "observer_judge"
                )
                outcome.mm_quality_pass = True
                outcome.mm_quality_score = None
                outcome.review_skipped = True
                outcome.review_skip_reason = "v2_observer_judge_no_old_review"
                outcome.verify_test_results = [{
                    "test_id": "observer_judge",
                    "passed": True,
                    "reason": result.reason[:200],
                }]
                outcome.verify_scores = {
                    "observer_judge_accept": 1,
                    "haiku_escalated": 1 if result.escalated else 0,
                    "detection_calls": metrics.get("detection_calls", 0),
                    "detection_elapsed_seconds": metrics.get("detection_elapsed_seconds", 0.0),
                    "detection_est_cost_usd": metrics.get("detection_est_cost_usd", 0.0),
                }
                outcome.cascade_trace["v2_detection"] = dict(metrics)
                metrics["candidate_verdicts"][-1]["accepted_used"] = True
                self._long_record_query_verdict(query, result.verdict)
                return outcome

            metrics["candidate_verdicts"][-1]["accepted_used"] = False
            self._v2_record_accept_unused(plan, cand, metrics)
            self._long_record_query_verdict(query, "reject")
        return None

    @staticmethod
    def _v2_asset_content_unsafe(cand: "FootageCandidate") -> Optional[str]:
        """Compliance tag backstop for the long path: return a reason
        (``unsafe:drug:...`` / ``unsafe:...``) if a candidate's tags/title/url
        carry drug/cannabis (or sexual/violent/morbid) markers, else None.
        Critical because library clips are accepted on tags alone and SKIP the
        visual judge — this is the only name-check they get. Mirrors the short
        orchestrator's ``asset_is_unsafe`` gate; the drug bank is always-on."""
        try:
            from backend.lib.content_safety_filter import asset_is_unsafe
            return asset_is_unsafe(
                getattr(cand, "source_tags", "") or getattr(cand, "query", "") or "",
                getattr(cand, "source_url", "") or "",
            )
        except Exception:
            return None


    @staticmethod
    def _v2_detection_metrics(plan: dict) -> dict:
        metrics = plan.setdefault("_v2_detection_metrics", {})
        metrics.setdefault("detection_calls", 0)
        metrics.setdefault("detection_elapsed_seconds", 0.0)
        metrics.setdefault("detection_est_cost_usd", 0.0)
        metrics.setdefault("candidate_verdicts", [])
        metrics.setdefault("total_image_count", 0)
        metrics.setdefault("image_count_call_count", 0)
        metrics.setdefault("multi_frame_count", 0)
        metrics.setdefault("query_disabled_count", 0)
        metrics.setdefault("accept_unused", [])
        return metrics

    def _v2_append_candidate_verdict(
        self,
        metrics: dict,
        cand: FootageCandidate,
        *,
        mode: str,
        verdict: str,
        model_id: str,
        reason: str,
        elapsed_seconds: float,
        est_cost_usd: float,
        judgment: Optional[dict],
        image_count: int,
        accepted_used: Optional[bool],
    ) -> None:
        metrics.setdefault("candidate_verdicts", []).append({
            "mode": mode,
            "source": cand.source,
            "source_id": cand.source_id,
            "verdict": verdict,
            "model_id": model_id,
            "elapsed_seconds": elapsed_seconds,
            "est_cost_usd": est_cost_usd,
            "reason": reason or "",
            "judgment": judgment,
            "image_count_sent_to_gemini": image_count,
            "accepted_used": accepted_used,
            **self._v2_debug_candidate_extras(cand),
        })

    @staticmethod
    def _v2_record_accept_unused(plan: dict, cand: FootageCandidate, metrics: dict) -> None:
        reason = str(plan.pop("_v2_last_download_failure_reason", "") or "download_failed")
        item = {
            "source": cand.source,
            "source_id": cand.source_id,
            "query": cand.query,
            "reason": reason,
        }
        metrics.setdefault("accept_unused", []).append(item)
        plan["_v2_accept_unused_reason"] = reason

    def _long_query_is_disabled(self, query: str) -> bool:
        q = self._long_query_key(query)
        if not q:
            return False
        if self._long_query_is_static_bad(q):
            return True
        cache = self._long_query_cache()
        lock = self._long_query_lock()
        with lock:
            stats = cache.get(q) or {}
            return int(stats.get("reject", 0)) >= 5 and int(stats.get("accept", 0)) == 0

    def _long_record_query_verdict(self, query: str, verdict: str) -> None:
        q = self._long_query_key(query)
        if not q:
            return
        cache = self._long_query_cache()
        lock = self._long_query_lock()
        with lock:
            stats = cache.setdefault(q, {"reviewed": 0, "accept": 0, "reject": 0})
            stats["reviewed"] = int(stats.get("reviewed", 0)) + 1
            if verdict == "accept":
                stats["accept"] = int(stats.get("accept", 0)) + 1
            else:
                stats["reject"] = int(stats.get("reject", 0)) + 1

    def _long_query_cache(self) -> dict[str, dict[str, int]]:
        cache = getattr(self, "_long_query_failure_cache", None)
        if cache is None:
            cache = {}
            self._long_query_failure_cache = cache
        return cache

    def _long_query_lock(self) -> threading.Lock:
        lock = getattr(self, "_long_query_failure_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._long_query_failure_lock = lock
        return lock

    @staticmethod
    def _long_query_key(query: str) -> str:
        return " ".join(str(query or "").strip().lower().split())

    @staticmethod
    def _long_query_is_static_bad(query: str) -> bool:
        q = query.lower()
        if "coordination" in q:
            return True
        if "ecosystem balance" in q or "human dominance" in q:
            return True
        if " close up" in q:
            head = q.split(" close up", 1)[0].strip()
            if head in {"truth", "fear", "balance", "conflict", "meaning"}:
                return True
        return False

    def _video_subject_keys(self, plans: list[dict]) -> set:
        """The whole video's concrete subject(s): the dominant concept-group
        member across ALL chunk plans (e.g. boar). Robust to individual chunks
        whose own plan lacks the subject signal — that gap is exactly how
        off-subject clips slip past the per-chunk gate."""
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

    def _v2_sweep_subject_mismatches(self, outcomes: list, video_subject: set) -> None:
        """Replace any shot whose tags name a DIFFERENT member of the video's
        subject group with the nearest on-subject shot already in hand.
        Path-independent backstop: catches off-subject footage no matter which
        probe / fallback / inheritance produced it (wolf in a wild-boar video)."""
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
                "v2 subject sweep: %d off-subject shot(s) but no on-subject donor; leaving as-is",
                bad,
            )
            return
        swept = 0
        for i, oc in enumerate(outcomes):
            if not mismatched(oc):
                continue
            donor = outcomes[min(clean, key=lambda j: abs(j - i))]
            new = self._build_inherited_outcome(oc.plan, donor, oc.chunk_index)
            new.inherit_reason = "subject_sweep"
            new.used_level = "L_subject_sweep"
            outcomes[i] = new
            swept += 1
        logger.warning(
            "v2 subject sweep replaced %d off-subject shot(s) with on-subject donors "
            "(video subject=%s)", swept, sorted(c for _g, c in video_subject),
        )

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
        # Subject-mismatch guard (last line of defense): never let a clip whose
        # tags name a DIFFERENT concrete subject than the video become a used
        # shot, no matter which probe or ship-fallback produced it. The ship
        # remote fallback filters on must_not only, so a wild-boar video would
        # otherwise still ship wolf clips here. Returning None makes the caller
        # try the next candidate/fallback (ultimately inheriting a safe shot).
        subject_keys = _subject_keys(plan, [])
        if subject_keys:
            meta = " ".join([
                str(cand.source_tags or ""),
                str(cand.source_url or ""),
            ]).lower()
            reason = _subject_mismatch(meta, subject_keys)
            if reason:
                plan["_v2_last_download_failure_reason"] = "subject_mismatch"
                logger.info(
                    "chunk %d v2 subject gate reject %s:%s — %s",
                    plan["chunk_index"], cand.source, cand.source_id, reason,
                )
                return None
        # allow_reuse: the "video must ship" fallback may legitimately re-use a
        # motif clip that another chunk already consumed (a repeated theme
        # environment shot beats a dead pipeline), so it skips the dedup reserve.
        if not allow_reuse and not self._v2_reserve_source(
            used_source_ids, cand, used_source_lock,
        ):
            plan["_v2_last_download_failure_reason"] = "diversity_conflict"
            logger.info(
                "chunk %d v2 skipped duplicate reserved source %s:%s",
                plan["chunk_index"], cand.source, cand.source_id,
            )
            return None
        local = self.footage.download_candidate(cand, chunk_dir)
        if not local:
            plan["_v2_last_download_failure_reason"] = "download_failed"
            self._v2_mark_source_unusable(
                used_source_ids, cand, used_source_lock,
            )
            return None
        local = self._reframe_if_needed(local, orientation)
        ok, err = self._basic_file_check(local)
        if not ok:
            logger.info(
                "chunk %d v2 selected %s but file check failed: %s",
                plan["chunk_index"], cand.source, err,
            )
            self._v2_mark_source_unusable(
                used_source_ids, cand, used_source_lock,
            )
            plan["_v2_last_download_failure_reason"] = "duration_invalid"
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
            verify_scores={},
            verify_test_results=[],
        )

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
    ) -> ChunkOutcome:
        idx = int(plan["chunk_index"])
        if llm_counter is None:
            return self._v2_select_chunk(
                plan, output_dir, orientation, top_n, motif_pool, budget,
                used_source_ids, used_source_lock=used_source_lock,
            )
        with llm_counter.chunk(idx):
            return self._v2_select_chunk(
                plan, output_dir, orientation, top_n, motif_pool, budget,
                used_source_ids, used_source_lock=used_source_lock,
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

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _find_donor_outcome_for_run(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
    ) -> Optional[ChunkOutcome]:
        """Scan backwards from idx-1 for the nearest successful outcome.

        "Successful" = decision == "use" AND local_path present. Inherited
        outcomes themselves qualify (chain inheritance is allowed) but the
        caller will log a warning when chains form.
        """
        for j in range(idx - 1, -1, -1):
            prev = stock_outcomes[j]
            if prev is None:
                continue
            if prev.decision == "use" and prev.local_path:
                return prev
        return None

    def _find_visual_focus_pool_donor(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        plan: dict,
        *,
        exclude: set[int],
        exclude_source_ids: Optional[set[str]] = None,
    ) -> Optional[ChunkOutcome]:
        """Reuse a non-adjacent accepted asset with the same visual role.

        This is still a per-video temporary pool: it only looks at outcomes
        downloaded during this run, never at a persistent local library.
        """
        focus = str(plan.get("visual_focus") or "").strip()
        if not focus:
            return None
        allowed_focus = {focus}
        if focus in {"abstract_broll", "transition"}:
            allowed_focus.update({"habitat", "topic_subject", "human_impact"})
        elif focus == "consequence":
            allowed_focus.update({"habitat", "prey_or_livestock"})

        best: Optional[ChunkOutcome] = None
        best_distance = 10**9
        for candidate in stock_outcomes:
            if candidate is None:
                continue
            if candidate.decision != "use" or not candidate.local_path:
                continue
            if candidate.chunk_index in exclude:
                continue
            if abs(candidate.chunk_index - idx) <= 1:
                continue
            if candidate.inherited_from_chunk is not None:
                continue
            if candidate.source_id and candidate.source_id in (exclude_source_ids or set()):
                continue
            cand_focus = str((candidate.plan or {}).get("visual_focus") or "")
            if cand_focus not in allowed_focus:
                continue
            distance = abs(candidate.chunk_index - idx)
            if distance < best_distance:
                best = candidate
                best_distance = distance
        return best

    def _find_donor_outcome_for_aggressive(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
        *,
        exclude: set[int],
        exclude_source_ids: Optional[set[str]] = None,
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
        for j in range(idx - 1, -1, -1):
            prev = stock_outcomes[j]
            if prev is None:
                continue
            if prev.decision != "use" or not prev.local_path:
                continue
            if prev.chunk_index in exclude:
                continue
            # chunk — that's exactly the "same shot twice in a row" complaint.
            if prev.source_id and prev.source_id in (exclude_source_ids or set()):
                continue
            # Skip inherited outcomes themselves to avoid chain inheritance
            # from aggressive mode — keep chains only from explicit
            # transition/CTA Pass 2 inheritance.
            if prev.inherited_from_chunk is not None:
                continue
            return prev
        # Forward scan as fallback (idx may be near start; first chunks
        # could be stock_miss themselves but later ones succeeded)
        for j in range(idx + 1, len(stock_outcomes)):
            nxt = stock_outcomes[j]
            if nxt is None:
                continue
            if nxt.decision != "use" or not nxt.local_path:
                continue
            if nxt.chunk_index in exclude:
                continue
            if nxt.source_id and nxt.source_id in (exclude_source_ids or set()):
                continue
            if nxt.inherited_from_chunk is not None:
                continue
            return nxt
        return None

    def _find_any_donor(
        self,
        stock_outcomes: list[Optional[ChunkOutcome]],
        idx: int,
    ) -> Optional[ChunkOutcome]:
        """Last-resort donor finder for the 'video must ship' fallback.

        Unlike `_find_donor_outcome_for_aggressive`, this drops EVERY anti-repeat
        guard (no `exclude` set, accepts already-inherited donors) and returns
        the nearest chunk holding a real downloaded clip, expanding outward from
        `idx` so visual continuity is best-effort preserved. Returns None only
        when the whole video produced zero usable clips.
        """
        n = len(stock_outcomes)
        for dist in range(1, n):
            for j in (idx - dist, idx + dist):
                if j < 0 or j >= n:
                    continue
                cand = stock_outcomes[j]
                if cand is None:
                    continue
                if cand.decision == "use" and cand.local_path:
                    return cand
        return None

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

        # Level B — reuse the nearest real shot already in hand.
        remote = self._v2_ship_remote_fallback(
            plan, idx, chunk_dir, orientation, motif_pool,
            used_source_ids, used_source_lock,
        )
        if remote is not None:
            return remote

        donor = self._find_any_donor(outcomes, idx)
        if donor is not None:
            inherited = self._build_inherited_outcome(plan, donor, idx)
            inherited.inherit_reason = (
                "ship_inherit_prev"
                if donor.chunk_index < idx
                else "ship_inherit_forward"
            )
            inherited.used_level = "L_ship_inherit"
            logger.info(
                "chunk %d ship-fallback inherits nearest shot from chunk %d",
                idx, donor.chunk_index,
            )
            return inherited

        return None

    def _v2_ship_remote_fallback(
        self,
        plan: dict,
        idx: int,
        chunk_dir: Path,
        orientation: str,
        motif_pool: list[str],
        used_source_ids: set[str],
        used_source_lock: Optional[threading.Lock],
    ) -> Optional[ChunkOutcome]:
        """Fill a no-donor chunk with broad remote stock from allowed sources."""
        must_not = self._fallback_terms(plan.get("must_not_show"))
        for query in self._v2_ship_fallback_queries(plan, motif_pool):
            try:
                candidates = self.footage.search_secondary(
                    query, orientation, kind="video", limit=6,
                )
            except Exception as e:  # noqa: BLE001 - fallback must not crash
                logger.warning(
                    "chunk %d ship-fallback secondary search failed for %r: %s",
                    idx, query, e,
                )
                candidates = []
            for cand in candidates:
                if self._fallback_candidate_blocked(cand, must_not):
                    continue
                outcome = self._v2_download_outcome(
                    plan, cand, chunk_dir, orientation,
                    used_level="L_ship_remote",
                    verify_reason=f"ship fallback: broad remote stock query '{query}'",
                    score=3.5,
                    used_source_ids=used_source_ids,
                    used_source_lock=used_source_lock,
                    allow_reuse=True,
                )
                if outcome is not None:
                    outcome.inherit_reason = "ship_remote_stock"
                    logger.info(
                        "chunk %d ship-fallback filled with remote stock %s:%s query=%r",
                        idx, outcome.source, outcome.source_id, query,
                    )
                    return outcome
        return None

    @staticmethod
    def _fallback_terms(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = [value]
        else:
            try:
                items = list(value)
            except TypeError:
                items = [str(value)]
        return [str(item).strip().lower() for item in items if str(item).strip()]

    @staticmethod
    def _fallback_candidate_blocked(
        cand: FootageCandidate, must_not: list[str],
    ) -> bool:
        if not must_not:
            return False
        meta = " ".join([
            str(cand.source_tags or ""),
            str(cand.source_url or ""),
            str(cand.query or ""),
        ]).lower()
        return any(term and term in meta for term in must_not)

    @staticmethod
    def _v2_ship_fallback_queries(
        plan: dict, motif_pool: list[str],
    ) -> list[str]:
        queries: list[str] = []
        seen: set[str] = set()

        def add(raw: Any) -> None:
            q = " ".join(str(raw or "").strip().split())
            if not q:
                return
            key = q.lower()
            if key in seen:
                return
            seen.add(key)
            queries.append(q)

        for q in list(motif_pool or [])[:4]:
            add(q)
        for q in list(plan.get("queries") or [])[:3]:
            add(q)
        for q in list(plan.get("metaphor_queries") or [])[:2]:
            add(q)

        policy = plan.get("long_video_visual_policy") or {}
        topic = str(policy.get("topic_anchor") or "").strip()
        if not topic:
            must_show = plan.get("must_show") or []
            if isinstance(must_show, str):
                topic = must_show.strip()
            elif must_show:
                topic = str(must_show[0] or "").strip()
        if topic:
            add(f"{topic} wilderness")
            add(f"{topic} in nature")
            add(f"{topic} habitat")

        focus = str(plan.get("visual_focus") or "").strip()
        if focus in {"habitat", "abstract_broll", "transition", ""}:
            add("wild nature landscape")
            add("forest wilderness")
            add("mountain landscape")
        return queries[:12]

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
