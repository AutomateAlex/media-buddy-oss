"""

When stock can't deliver, route ONE provider per chunk. No fanout. Decision
is based on the chunk's shot_type + is_hero_shot + the active pipeline mode.

Provider mapping:
  static / atmosphere / product / interior / establishing
      → seedance_lite (cheap, realistic)
  human_action / performance / interaction
      → pika_v2_turbo (handles hand motion, human movement)
  complex_motion / vfx / hero shot
      → kling_v16_std (only when mode allows kling)
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.lib.pipeline_mode import PipelineMode, get_mode_config


@dataclass
class RoutingDecision:
    provider: str           # short key: "seedance_lite" | "pika_v2_turbo" | "kling_v16_std"
    fallback_provider: str  # used if primary fails or mm_critic rejects
    max_retries: int        # 0 (Fast) or 1 (Balanced/Premium)
    reason: str             # human-readable for logs


_HUMAN_ACTION_TYPES = {"human_action", "performance", "interaction"}
_COMPLEX_TYPES = {"complex_motion", "vfx"}
_STATIC_TYPES = {
    "static", "atmosphere", "product", "interior", "establishing",
}


class ShotRouter:
    """One provider per chunk. Returns RoutingDecision."""

    def __init__(self, mode: PipelineMode = PipelineMode.FAST):
        self.mode = mode
        self.cfg = get_mode_config(mode)

    def route(self, shot_type: str, is_hero_shot: bool = False) -> RoutingDecision:
        retries = 1 if self.cfg["allow_retry"] else 0
        st = (shot_type or "atmosphere").lower()

        # Hero / complex / vfx — try Kling if mode allows; else Pika
        if is_hero_shot or st in _COMPLEX_TYPES:
            if self.cfg["allow_kling"]:
                return RoutingDecision(
                    provider="kling_v16_std",
                    fallback_provider="pika_v2_turbo",
                    max_retries=1,  # always allow 1 retry for hero
                    reason=f"hero/complex shot ({st}); kling allowed in {self.mode.value}",
                )
            # Fast mode: no kling, fall back to pika
            return RoutingDecision(
                provider="pika_v2_turbo",
                fallback_provider="seedance_lite",
                max_retries=retries,
                reason=f"hero/complex shot ({st}); kling disabled in {self.mode.value}",
            )

        # Human action — pika is best for hand/body motion
        if st in _HUMAN_ACTION_TYPES:
            return RoutingDecision(
                provider="pika_v2_turbo",
                fallback_provider="seedance_lite",
                max_retries=retries,
                reason=f"human action ({st})",
            )

        # Static / atmosphere / product / interior — seedance is cheapest + realistic
        if st in _STATIC_TYPES:
            return RoutingDecision(
                provider="seedance_lite",
                fallback_provider="pika_v2_turbo",
                max_retries=retries,
                reason=f"static/atmosphere ({st})",
            )

        # Unknown shot type — default to seedance
        return RoutingDecision(
            provider="seedance_lite",
            fallback_provider="pika_v2_turbo",
            max_retries=retries,
            reason=f"unknown shot_type {st!r}; default seedance",
        )
