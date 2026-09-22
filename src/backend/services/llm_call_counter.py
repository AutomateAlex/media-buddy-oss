"""LLMCallCounter — per-chunk × per-purpose × per-model call tally.

proliferation of LLM calls in failure paths — we had no per-chunk visibility
into what was burning, so optimization decisions were guesses.

Design:
  - Thread-local current-chunk context (matches ThreadPoolExecutor in
    Orchestrator._stock_attempt_progressive).
  - 3-dimensional aggregate: chunk_index × purpose × model → count.
  - summary() shape lets us answer:
        "what would flipping BLIND_OBSERVE_MODEL save?"
    by inspecting aggregate_by_purpose_and_model with current vs target cost.
  - Two thresholds: WARN_THRESHOLD (80) logs but allows; ERROR_THRESHOLD
    (150) logs an error stripe but does not raise (let video finish).
  - Kill switch MEDIA_BUDDY_DISABLE_COST_GUARDRAIL=1 silences the logging
    entirely (counter still runs — needed for summary even when guard off).

Concurrency: Orchestrator uses ThreadPoolExecutor; threading.local is the
right primitive. If the codebase moves to asyncio, replace with contextvars
(API would not change).
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Per-call thresholds. The ERROR threshold
# does NOT raise — pipelines must still finish so the user gets their video
# even when guardrails trip. Use the audit log to find the bad chunk after.
WARN_THRESHOLD = 80
ERROR_THRESHOLD = 150

# Cost estimates are informational only in this build. All tables start empty;
# fill them via MEDIA_BUDDY_AI_PRICE_JSON if you want per-video cost estimates.
_MODEL_COST_PER_CALL_USD = {"_unknown_": 0.0}


def _est_cost(model: str, n_calls: int) -> float:
    per = _MODEL_COST_PER_CALL_USD.get(model, _MODEL_COST_PER_CALL_USD["_unknown_"])
    return round(per * n_calls, 4)


# ---------------------------------------------------------------------------
# from the exact `usage` tokens Alibaba returns × a DELIBERATELY-HIGH price
# table, so "estimate < charge" guarantees we never lose money — and it over-
# estimates whether the dedicated deployment is per-token OR flat monthly.
#
# ⚠️ PLACEHOLDER PRICES — public list prices rounded UP, USD per 1M tokens.
# Fill real (conservative) rates after confirming the dedicated deployment's
# billing mode. Everything here is env-overridable via MEDIA_BUDDY_AI_PRICE_JSON.
# A flat +BUFFER on top keeps us on the safe side of price drift / tiers.
# ---------------------------------------------------------------------------
_BUFFER_MULT = float(os.environ.get("MEDIA_BUDDY_AI_COST_BUFFER", "1.2") or "1.2")
PRICE_TABLE_VERSION = "unpriced"

# per 1M tokens, USD: (input, output, cached_input). Empty in this build.
_ALIBABA_PRICE_PER_1M: dict[str, tuple[float, float, float]] = {"_unknown_": (0.0, 0.0, 0.0)}
_SEARCH_SURCHARGE_USD = float(os.environ.get("MEDIA_BUDDY_AI_SEARCH_SURCHARGE", "0") or "0")
# TTS: USD per 1000 characters. Empty in this build.
_TTS_PRICE_PER_1K_CHARS: dict[str, float] = {"_default_": 0.0}
_FLUX_PER_IMAGE_USD = float(os.environ.get("MEDIA_BUDDY_AI_FLUX_PER_IMAGE", "0") or "0")
_COVER_PER_IMAGE_USD = float(os.environ.get("MEDIA_BUDDY_COVER_PER_IMAGE_USD", "0") or "0")


def _price_override() -> dict:
    """Optional full-table override via env (JSON). Lets us patch prices in
    """
    raw = os.environ.get("MEDIA_BUDDY_AI_PRICE_JSON", "").strip()
    if not raw:
        return {}
    try:
        import json
        return json.loads(raw)
    except Exception:
        logger.warning("MEDIA_BUDDY_AI_PRICE_JSON is not valid JSON; ignoring")
        return {}


# 已经喊过「没有价格」的模型，避免每次调用都刷一条日志。
_UNPRICED_SEEN: set[str] = set()


def _alibaba_rates(model: str) -> tuple[float, float, float]:
    """(input, output, cached_input) USD per 1M tokens for a qwen model.
    Exact key first, then longest family-prefix match, else the high default."""
    m = (model or "").strip().lower()
    table = dict(_ALIBABA_PRICE_PER_1M)
    table.update({k.lower(): tuple(v) for k, v in _price_override().get("alibaba", {}).items()})
    if m in table:
        return table[m]  # type: ignore[return-value]
    # family match: pick the longest known key that the model starts with
    fam = [k for k in table if k != "_unknown_" and m.startswith(k)]
    if fam:
        return table[max(fam, key=len)]  # type: ignore[return-value]
    return table["_unknown_"]  # type: ignore[return-value]


def _token_cost_usd(model: str, prompt_tokens: int, completion_tokens: int,
                    cached_tokens: int = 0, search: bool = False) -> float:
    """Conservative USD for one call. Cached input tokens are billed at the
    (cheaper) cache rate; the rest of the input at the full input rate. A flat
    BUFFER and the optional search surcharge are added on top."""
    rin, rout, rcache = _alibaba_rates(model)
    cached = max(0, int(cached_tokens or 0))
    fresh_in = max(0, int(prompt_tokens or 0) - cached)
    out = max(0, int(completion_tokens or 0))
    usd = (fresh_in * rin + cached * rcache + out * rout) / 1_000_000.0
    usd *= _BUFFER_MULT
    if search:
        usd += _SEARCH_SURCHARGE_USD
    return usd


def _tts_cost_usd(chars: int, provider: str) -> float:
    p = (provider or "_default_").strip().lower()
    # Provider strings carry a voice suffix (qwen_cherry, azure_yunyang,
    # elevenlabs_amy); collapse each family to its price-table key. Qwen is the
    # production default now, so an unrecognised qwen_* must NOT fall to _default_.
    if p.startswith("qwen"):
        p = "qwen"
    elif p.startswith("azure"):
        p = "azure_yunyang"
    elif p.startswith("elevenlabs"):
        p = "elevenlabs"
    over = _price_override().get("tts", {})
    # Unknown providers fall to the expensive _default_ (over-estimate = safe).
    rate = float(over.get(p, _TTS_PRICE_PER_1K_CHARS.get(p, _TTS_PRICE_PER_1K_CHARS["_default_"])))
    return (max(0, int(chars or 0)) / 1000.0) * rate * _BUFFER_MULT


class LLMCallCounter:
    """Thread-safe per-chunk × purpose × model call aggregator.

    Lifecycle:
        counter = LLMCallCounter()
        with counter.chunk(chunk_index=3):
            # any code that calls LLMClient._call() with purpose="X" and
            # model="Y" → counter.bump("X", "Y") inside the call site.
        summary = counter.summary()   # dump after pipeline done
    """

    def __init__(self) -> None:
        self._local = threading.local()
        # counts keyed by chunk_index → (purpose, model) → int
        self._counts: dict[int, dict[tuple[str, str], int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # Calls that bump() while no chunk context is active (e.g. shared
        # parallel) land here under "shared".
        self._shared: dict[tuple[str, str], int] = defaultdict(int)
        # actual OpenRouter `cost_eur` (usage.total_cost). The estimate-based
        # _est_cost undercounted ~4-8x; this is the exact billed amount.
        self._real_cost_usd: dict[tuple[str, str], float] = defaultdict(float)
        # per-video AI cost now that the worker calls Alibaba direct. Accumulated
        # per source so a video's cost breaks down into llm / flux / tts.
        self._cost_by_source: dict[str, float] = defaultdict(float)
        self._tokens = {"prompt": 0, "completion": 0, "cached": 0}
        # Per-model token totals — lets us price each model with its own real
        # rate when reconciling against the actual Alibaba bill.
        self._tokens_by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt": 0, "completion": 0, "cached": 0}
        )
        self._tts_chars = 0
        self._flux_images = 0
        self._cover_images = 0
        # Identity of the video this counter is measuring (set at bind time).
        self.project_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.output_format: Optional[str] = None
        self.final_seconds: Optional[float] = None
        self._started_at = time.monotonic()
        self._lock = threading.Lock()
        self._guardrail_silent = (
            os.environ.get("MEDIA_BUDDY_DISABLE_COST_GUARDRAIL", "0").lower()
            in ("1", "true", "yes")
        )

    # ---- context management ----
    @contextlib.contextmanager
    def chunk(self, chunk_index: int) -> Iterator[None]:
        """Bind current thread's chunk context. Required for per-chunk attribution.

        Nesting: if the thread is already inside a chunk context, we
        preserve the outer index and restore on exit (defensive — the
        Orchestrator does not nest today but a future refactor might).
        """
        prev = getattr(self._local, "current", None)
        self._local.current = int(chunk_index)
        try:
            yield
        finally:
            self._local.current = prev

    # ---- bump ----
    def bump(self, purpose: str, model: str) -> None:
        """Record one LLM call. Safe to call without an active chunk context
        (lands under 'shared'). Both purpose and model are normalized to
        strings for dict-key safety."""
        purpose = purpose or "unknown"
        model = model or "unknown"
        idx = getattr(self._local, "current", None)
        with self._lock:
            if idx is None:
                self._shared[(purpose, model)] += 1
                running = self._shared[(purpose, model)]
                scope = "shared"
            else:
                self._counts[idx][(purpose, model)] += 1
                running = sum(self._counts[idx].values())
                scope = f"chunk {idx}"
        # Threshold emission outside the lock (logger.warn is slow + IO).
        if not self._guardrail_silent and idx is not None:
            if running == WARN_THRESHOLD + 1:
                logger.warning(
                    "cost_guardrail %s reached %d calls (warn threshold %d)",
                    scope, running, WARN_THRESHOLD,
                )
            elif running == ERROR_THRESHOLD + 1:
                logger.error(
                    "cost_guardrail %s reached %d calls (error threshold %d) — "
                    "investigate cascade after pipeline completes",
                    scope, running, ERROR_THRESHOLD,
                )

    def add_real_cost(self, purpose: str, model: str, cost_usd: float) -> None:
        """
        actual OpenRouter usage. Keyed by (purpose, model) like bump()."""
        if not cost_usd:
            return
        purpose = purpose or "unknown"
        model = model or "unknown"
        with self._lock:
            self._real_cost_usd[(purpose, model)] += float(cost_usd)

    # ---- conservative token/char cost capture (per-video source of truth) ----
    def set_identity(self, *, project_id=None, user_id=None,
                     output_format=None, final_seconds=None) -> None:
        """Tag this counter with the video it measures. Called at bind time.
        Only overwrites fields that are provided (idempotent-ish)."""
        with self._lock:
            if project_id is not None:
                self.project_id = str(project_id)
            if user_id is not None:
                self.user_id = str(user_id)
            if output_format is not None:
                self.output_format = str(output_format)
            if final_seconds is not None:
                try:
                    self.final_seconds = float(final_seconds)
                except (TypeError, ValueError):
                    pass

    def add_token_usage(self, purpose: str, model: str, prompt_tokens: int,
                        completion_tokens: int, cached_tokens: int = 0,
                        search: bool = False) -> None:
        """Record one LLM call's token usage → conservative USD into 'llm'."""
        usd = _token_cost_usd(model, prompt_tokens, completion_tokens,
                              cached_tokens, search)
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
        cch = max(0, int(cached_tokens or 0))
        mkey = model or "unknown"
        with self._lock:
            self._cost_by_source["llm"] += usd
            self._tokens["prompt"] += pt
            self._tokens["completion"] += ct
            self._tokens["cached"] += cch
            m = self._tokens_by_model[mkey]
            m["prompt"] += pt
            m["completion"] += ct
            m["cached"] += cch

    def add_tts_chars(self, chars: int, provider: str) -> None:
        usd = _tts_cost_usd(chars, provider)
        with self._lock:
            self._cost_by_source["tts"] += usd
            self._tts_chars += max(0, int(chars or 0))

    def add_flux_images(self, n: int = 1) -> None:
        n = max(0, int(n or 0))
        with self._lock:
            self._cost_by_source["flux"] += n * _FLUX_PER_IMAGE_USD * _BUFFER_MULT
            self._flux_images += n

    def add_cover_images(self, n: int = 1) -> None:
        """SEO cover thumbnail(s). Priced separately from flux so the per-video cost
        breakdown shows what the cover actually cost — the customer is billed for it."""
        n = max(0, int(n or 0))
        with self._lock:
            self._cost_by_source["cover"] += n * _COVER_PER_IMAGE_USD * _BUFFER_MULT
            self._cover_images += n

    def add_openrouter_cost(self, purpose: str, model: str, cost_usd: float) -> None:
        """
        这是 OpenRouter 响应里回传的 usage.cost,**真实值,不乘 _BUFFER_MULT**(区别于 llm/tts
        /flux/cover 的保守估算)。进 cost_by_source['openrouter'] → 自动含进 cost_total_usd。
        """
        c = float(cost_usd or 0.0)
        if c <= 0:
            return
        purpose = purpose or "openrouter"
        model = model or "unknown"
        with self._lock:
            self._cost_by_source["openrouter"] += c
            self._real_cost_usd[(purpose, model)] += c

    # ---- inspection ----
    def summary(self) -> dict:
        """Return the canonical 3-dimensional aggregate.

        Shape:
            {
              "per_chunk": {
                0: {
                  "plan_chunks": {"calls": N, "model": "...", "est_cost": "$X"},
                  ...
                  "total": K,
                },
                ...
              },
              "shared": {
                "plan_chunks": {"calls": N, "model": "...", "est_cost": "$X"},
                ...
              },
              "aggregate_by_purpose_and_model": {
                "blind_observe:google/gemini-2.5-flash": {"calls": K, "est_cost": "$X"},
                ...
              },
              "grand_total_calls": int,
              "grand_total_est_cost_usd": float,
            }
        """
        with self._lock:
            counts_snapshot = {
                idx: dict(per_pm) for idx, per_pm in self._counts.items()
            }
            shared_snapshot = dict(self._shared)
            real_cost_snapshot = dict(self._real_cost_usd)
            cost_by_source = dict(self._cost_by_source)
            tokens_snapshot = dict(self._tokens)
            tokens_by_model = {k: dict(v) for k, v in self._tokens_by_model.items()}
            tts_chars = self._tts_chars
            flux_images = self._flux_images
            cover_images = self._cover_images
            # 按 ECI 实例规格的按秒价填。真实估算,不乘 buffer。summary 时算(非累加),多次调用不重复。
            try:
                _rate = float(os.environ.get("MB_COMPUTE_USD_PER_SEC", "0") or "0")
            except (TypeError, ValueError):
                _rate = 0.0
            if _rate > 0:
                _render_sec = max(0.0, time.monotonic() - self._started_at)
                cost_by_source["compute"] = round(_render_sec * _rate, 6)
            identity = {
                "project_id": self.project_id, "user_id": self.user_id,
                "output_format": self.output_format, "final_seconds": self.final_seconds,
            }

        per_chunk: dict[int, dict] = {}
        for idx, pm_counts in counts_snapshot.items():
            entry: dict = {}
            total = 0
            for (purpose, model), n in pm_counts.items():
                # When the same purpose has multiple models (e.g. retry
                # routed differently), keep the dominant model + sum the
                # calls. This matters for env-flip experiments where the
                # FIRST model used wins display priority.
                if purpose not in entry:
                    entry[purpose] = {
                        "calls": n, "model": model,
                        "est_cost": _est_cost(model, n),
                    }
                else:
                    entry[purpose]["calls"] += n
                    # Per-purpose cost only reflects first-model price.
                    # Acceptable for summary; precise per-(model) audit
                    # available via aggregate_by_purpose_and_model below.
                    entry[purpose]["est_cost"] = _est_cost(
                        entry[purpose]["model"], entry[purpose]["calls"]
                    )
                total += n
            entry["total"] = total
            per_chunk[idx] = entry

        shared_out: dict[str, dict] = {}
        for (purpose, model), n in shared_snapshot.items():
            if purpose in shared_out:
                shared_out[purpose]["calls"] += n
                shared_out[purpose]["est_cost"] = _est_cost(
                    shared_out[purpose]["model"], shared_out[purpose]["calls"]
                )
            else:
                shared_out[purpose] = {
                    "calls": n, "model": model,
                    "est_cost": _est_cost(model, n),
                }

        # Aggregate by (purpose, model) — the key analysis view for
        # env-var flip experiments. "would flipping BLIND_OBSERVE to Haiku
        # save $X" reads directly off this row.
        agg: dict[str, dict] = {}
        all_pm: dict[tuple[str, str], int] = defaultdict(int)
        for per_pm in counts_snapshot.values():
            for k, v in per_pm.items():
                all_pm[k] += v
        for k, v in shared_snapshot.items():
            all_pm[k] += v
        grand = 0
        grand_cost = 0.0
        for (purpose, model), n in all_pm.items():
            key = f"{purpose}:{model}"
            cost = _est_cost(model, n)
            real = round(real_cost_snapshot.get((purpose, model), 0.0), 6)
            agg[key] = {"calls": n, "est_cost": cost, "real_cost_usd": real}
            grand += n
            grand_cost += cost

        grand_real = round(sum(real_cost_snapshot.values()), 4)
        return {
            "per_chunk": per_chunk,
            "shared": shared_out,
            "aggregate_by_purpose_and_model": agg,
            "grand_total_calls": grand,
            "grand_total_est_cost_usd": round(grand_cost, 4),
            # cost. 0.0 if no real cost was reported (e.g. offline/mocked runs).
            "grand_total_real_cost_usd": grand_real,
            # ---- conservative per-video cost (the calibration source of truth) ----
            "identity": identity,
            "cost_by_source_usd": {k: round(v, 5) for k, v in cost_by_source.items()},
            "cost_total_usd": round(sum(cost_by_source.values()), 5),
            "tokens": tokens_snapshot,
            "tokens_by_model": tokens_by_model,
            "tts_chars": tts_chars,
            "flux_images": flux_images,
            "cover_images": cover_images,
            "price_table_version": PRICE_TABLE_VERSION,
        }


# ----------------------------------------------------------------------
# Global pointer — set by Orchestrator at run() start, read by LLMClient
# _call hook. We use a module-level binding instead of injecting through
# every constructor because:
#   1. LLMClient is constructed at app start, way before any pipeline run
#   2. Threading the counter through every method signature would touch
#      ~30 files for an observation-only feature
# Trade-off: only one counter active at a time. Acceptable — concurrent
# pipelines in one process is not a supported case (Stage 2 already
# assumes single-pipeline-per-process).
# ----------------------------------------------------------------------
_active_counter: Optional[LLMCallCounter] = None
_active_counter_lock = threading.Lock()


def set_active_counter(counter: Optional[LLMCallCounter]) -> None:
    """Bind a counter as the active singleton. Pass None to detach."""
    global _active_counter
    with _active_counter_lock:
        _active_counter = counter


def get_active_counter() -> Optional[LLMCallCounter]:
    with _active_counter_lock:
        return _active_counter


def bump_active(purpose: str, model: str) -> None:
    """Convenience: bump the active counter if any, otherwise no-op.
    Call sites use this so they don't have to null-check."""
    c = get_active_counter()
    if c is not None:
        c.bump(purpose, model)


def bump_active_real_cost(purpose: str, model: str, cost_usd: float) -> None:
    """Convenience: record real billed cost on the active counter (no-op if
    """
    c = get_active_counter()
    if c is not None:
        c.add_real_cost(purpose, model, cost_usd)


def add_active_usage(purpose: str, model: str, prompt_tokens: int,
                     completion_tokens: int, cached_tokens: int = 0,
                     search: bool = False) -> None:
    """Feed one call's token usage into the active counter (no-op if none).
    Call sites pass the exact `usage` block they used to discard."""
    c = get_active_counter()
    if c is not None:
        try:
            c.add_token_usage(purpose, model, prompt_tokens, completion_tokens,
                              cached_tokens, search)
        except Exception:  # never let cost-accounting break a render
            logger.debug("add_active_usage failed", exc_info=True)


def add_active_tts_chars(chars: int, provider: str) -> None:
    c = get_active_counter()
    if c is not None:
        try:
            c.add_tts_chars(chars, provider)
        except Exception:
            logger.debug("add_active_tts_chars failed", exc_info=True)


def add_active_flux_images(n: int = 1) -> None:
    c = get_active_counter()
    if c is not None:
        try:
            c.add_flux_images(n)
        except Exception:
            logger.debug("add_active_flux_images failed", exc_info=True)


def add_active_cover_images(n: int = 1) -> None:
    c = get_active_counter()
    if c is not None:
        try:
            c.add_cover_images(n)
        except Exception:
            logger.debug("add_active_cover_images failed", exc_info=True)


def add_active_openrouter_cost(purpose: str, model: str, cost_usd: float) -> None:
    """把 OpenRouter 直连的真实成本(DeepSeek 每单)记进 active counter(无 active 则 no-op)。"""
    c = get_active_counter()
    if c is not None:
        try:
            c.add_openrouter_cost(purpose, model, cost_usd)
        except Exception:
            logger.debug("add_active_openrouter_cost failed", exc_info=True)


def set_active_identity(**kwargs) -> None:
    """Tag the active counter with the video's project_id/user_id/format/seconds."""
    c = get_active_counter()
    if c is not None:
        try:
            c.set_identity(**kwargs)
        except Exception:
            logger.debug("set_active_identity failed", exc_info=True)
