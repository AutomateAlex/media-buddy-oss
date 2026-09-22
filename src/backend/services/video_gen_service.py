"""AI video generation — not shipped in this build.

The orchestrators keep a sealed "generate a clip when no stock matches" tier;
this service tells them it is unavailable so they fall through to stock-only.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

CURATED_MODELS: dict[str, tuple[str, str]] = {}
DEFAULT_MODEL = ""
DEFAULT_FANOUT_KEYS: list[str] = []


class VideoGenService:
    """Always unavailable: no AI video provider is configured in this build."""

    def __init__(self, model: Optional[str] = None):
        self.model = model or DEFAULT_MODEL

    def is_available(self) -> bool:
        return False

    def generate_clip(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return None

    def generate_clip_parallel(self, *args: Any, **kwargs: Any) -> list:
        return []
