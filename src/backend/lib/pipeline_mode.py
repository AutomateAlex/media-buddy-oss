"""

Three modes control the orchestrator's quality / cost / speed tradeoff:
  - Fast (default): stock-first, no Kling, no retry, accept ≥7.0 stock
  - Balanced: stock-first, Kling for hero shots only, 1 retry, ≥7.2 stock
  - Premium: stock-first, Kling allowed by router, ≥7.5 stock, hero ≥8.5

The mode is chosen by the user (env var PIPELINE_MODE or per-project setting).
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Any


class PipelineMode(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    PREMIUM = "premium"


# Phase 2.11e — Source waves replace the old "all sources at once" fan-out.
# Each chunk searches Wave 1 first; only if Wave 1 returns < n usable
# candidates does it advance to Wave 2. This caps simultaneous
# SSL handshakes at (chunk_concurrency × wave_size) instead of (chunk × all_sources).
# Production stock is intentionally limited to keyed mainstream sources.
# Public-domain archive sources were too slow and too noisy for video matching.
_SOURCE_WAVES_DEFAULT = [
    ["pexels", "pixabay_video", "coverr"],  # keyed, broad B-roll, fast
]

# Hard duration ceiling: clips longer than this are dropped entirely (we only
# use one shot per chunk, and 60+ s clips just bloat disk and OOM ffprobe).
_MAX_CLIP_DURATION_DEFAULT = 60.0
# Soft preference: clips ≤ this are preferred when ranking (closer to ideal
# shot duration), but longer clips up to the hard ceiling are still allowed.
_PREFERRED_CLIP_DURATION_DEFAULT = 30.0


_CONFIGS: dict[PipelineMode, dict[str, Any]] = {
    PipelineMode.FAST: {
        # Asset scoring thresholds
        "score_use": 8.0,            # ≥ this → use stock without question
        "score_acceptable": 7.0,     # 7.0..7.9 → acceptable (non-hero)
        "hero_use": 8.5,             # hero shot must clear this
        # AI fallback rules
        "allow_kling": False,        # router never returns kling
        "allow_retry": False,        # 0 retries on AI failure (keep cost low)
        # concurrent LLM calls with zero 429s and flat latency. Combined
        # ~8×8=64 — still inside the verified-safe range.
        "stock_search_concurrency": 8,
        "asset_scoring_concurrency": 6,
        "ai_concurrency": 4,
        "mm_critic_concurrency": 4,
        # AI quality thresholds
        "ai_score_use": 8.0,
        "ai_score_acceptable": 7.0,
        "source_waves": _SOURCE_WAVES_DEFAULT,
        "max_clip_duration_seconds": _MAX_CLIP_DURATION_DEFAULT,
        "preferred_clip_duration_seconds": _PREFERRED_CLIP_DURATION_DEFAULT,
    },
    PipelineMode.BALANCED: {
        "score_use": 8.0,
        "score_acceptable": 7.2,
        "hero_use": 8.5,
        "allow_kling": True,         # router may pick kling for hero / vfx
        "allow_retry": True,
        "stock_search_concurrency": 4,
        "asset_scoring_concurrency": 5,
        "ai_concurrency": 3,
        "mm_critic_concurrency": 4,
        "ai_score_use": 8.0,
        "ai_score_acceptable": 7.2,
        "source_waves": _SOURCE_WAVES_DEFAULT,
        "max_clip_duration_seconds": _MAX_CLIP_DURATION_DEFAULT,
        "preferred_clip_duration_seconds": _PREFERRED_CLIP_DURATION_DEFAULT,
    },
    PipelineMode.PREMIUM: {
        "score_use": 8.5,
        "score_acceptable": 7.5,
        "hero_use": 9.0,
        "allow_kling": True,
        "allow_retry": True,
        "stock_search_concurrency": 3,
        "asset_scoring_concurrency": 4,
        "ai_concurrency": 3,
        "mm_critic_concurrency": 4,
        "ai_score_use": 8.5,
        "ai_score_acceptable": 7.5,
        "source_waves": _SOURCE_WAVES_DEFAULT,
        "max_clip_duration_seconds": _MAX_CLIP_DURATION_DEFAULT,
        "preferred_clip_duration_seconds": _PREFERRED_CLIP_DURATION_DEFAULT,
    },
}


def get_mode_config(mode: PipelineMode | str) -> dict[str, Any]:
    """Return immutable-ish config dict for the given mode. Strings accepted
    so callers can pass env values directly."""
    if isinstance(mode, str):
        try:
            mode = PipelineMode(mode.lower())
        except ValueError:
            mode = PipelineMode.FAST
    return dict(_CONFIGS[mode])  # copy


def resolve_mode(explicit: str | PipelineMode | None = None) -> PipelineMode:
    """Pick mode. Order: explicit arg → PIPELINE_MODE env → Fast default."""
    if explicit is not None:
        if isinstance(explicit, PipelineMode):
            return explicit
        try:
            return PipelineMode(str(explicit).lower())
        except ValueError:
            pass
    env = os.environ.get("PIPELINE_MODE", "").lower().strip()
    if env:
        try:
            return PipelineMode(env)
        except ValueError:
            pass
    return PipelineMode.FAST
