"""Phase 2.7a — Series memory accumulation.

Lightweight bookkeeping that runs after a project's chunks are produced,
folding observations back into Series.learned_patterns so the next run's
LLM prompt is a little smarter about this client.

Schema of `learned_patterns`:
    {
      "good_sources": {"pexels": 14, "pixabay": 6, ...},  # approved-clip count
      "bad_sources":  {"wikimedia": 3, ...},              # blocked/rejected count
      "approved_chunks": int,                             # lifetime
      "blocked_chunks":  int,                             # lifetime
      ...                                                 # future LLM-distilled
    }

Heavier "what kind of hooks does this client prefer?" distillation is left for
hard signals from the orchestrator outcomes.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from backend.models.series import Series

logger = logging.getLogger(__name__)


class MemoryService:
    """Stateless helper — pass a db Session for each call."""

    def record_outcomes(
        self, db: Session, series_id: str, outcomes: Iterable,
    ) -> None:
        """Update Series.learned_patterns based on a project's outcomes.

        `outcomes` is an iterable of objects with attributes:
            .source (str)         — "pexels" | "fal_ai_seedance_lite" | ...
            .safety_severity (str) — "ok" | "warn" | "block"
            .decision (str)        — "use" | "acceptable" | "reject" | "stock_miss"
        """
        if not series_id:
            return
        s = db.query(Series).filter(Series.id == series_id).first()
        if s is None:
            logger.warning("MemoryService.record_outcomes: Series %s missing", series_id)
            return
        patterns = dict(s.learned_patterns or {})
        good = dict(patterns.get("good_sources") or {})
        bad = dict(patterns.get("bad_sources") or {})
        approved_total = int(patterns.get("approved_chunks") or 0)
        blocked_total = int(patterns.get("blocked_chunks") or 0)

        for o in outcomes:
            source = getattr(o, "source", None) or ""
            severity = getattr(o, "safety_severity", "ok")
            decision = getattr(o, "decision", "")
            if not source:
                continue
            if severity == "block" or decision == "reject":
                bad[source] = bad.get(source, 0) + 1
                blocked_total += 1
            elif decision in ("use", "acceptable", "broll_ok"):
                good[source] = good.get(source, 0) + 1
                approved_total += 1

        patterns["good_sources"] = good
        patterns["bad_sources"] = bad
        patterns["approved_chunks"] = approved_total
        patterns["blocked_chunks"] = blocked_total
        s.learned_patterns = patterns
        db.commit()
        logger.info(
            "Series %s memory updated: approved=%d blocked=%d good_sources=%s bad_sources=%s",
            series_id, approved_total, blocked_total, good, bad,
        )
