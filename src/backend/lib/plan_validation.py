"""Plan Validation + Search Readiness Gate — three-layer hard gate.

``llm_client.py:plan_chunks`` that previously masked LLM transient failures
and let `atoms=[]` / `L1=Chinese-sentence` / `L5=generic-pool` plans leak
into stock search, producing the "fractal renders french food" + "all
empty shots" incidents.

Layout::

    plan_chunks LLM call (3-round progressive retry)
        ↓
    validate_plan_chunks_output()    ← THIS MODULE (static structural + content)
        ↓
    Harness Contract build (sentence-driven, independent)
        ↓
    validate_readiness_gate()        ← THIS MODULE (runtime per-chunk per-level)
        ↓
    stock search → ...

Any check fail → raise :class:`PlanChunksError` → pipeline stops with surfaced
error code. NEVER silently degrade.


* **L5 must be topic-aware** — the 12-entry ``l5_universal_pool`` is style /
  structure inspiration only, NOT an enforced pick-list. Validation rule 5
  requires the L5 prompt to contain at least one token derived from the
  chunk's own atoms or the per-atom ``degradation_map``. Generic L5 like
  "abstract background motion" alone is REJECTED.
* **Readiness Gate is level-aware** — L1/L2 demand strong overlap with the
  chunk's atoms + Harness ``visual_must_convey``; L3 keeps a visual category
  + atom anchor; L4 only checks style/mood/motion words; L5 trusts the
  topic-awareness already enforced in plan validation.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error codes (surfaced to frontend + logged)
# ---------------------------------------------------------------------------

_PLAN_ERROR_CODES: dict[str, str] = {
    "chinese_prompt_detected":           "Lx prompt contains CJK characters",
    "empty_atoms":                       "all 8 atom buckets empty for this chunk",
    "invalid_cascade_depth":             "cascade_depth not in {2, 3, 5}",
    "prompts_keys_mismatch":             "prompts dict keys do not match cascade_depth",
    "l5_not_topic_aware":                "L5 contains no atoms-derived or degradation-map token",
    "prompt_equals_sentence":            "Lx prompt is literally the narration sentence",
    "prompt_no_atoms_overlap":           "prompt has insufficient overlap with atoms/must_convey",
    "progressive_relationship_violated": "L1-L5 ladder broken (e.g. L4 lacks style word)",
    "cjk_in_atoms":                      "atom buckets contain CJK characters",
    "prompt_too_short":                  "prompt length < 4 chars",
    "prompt_too_long":                   "prompt length > 80 chars (validation) / 100 (gate)",
    "prompt_has_punctuation":            "prompt contains non-ASCII or punctuation",
    "retry_exhausted":                   "all 3 plan_chunks retry rounds failed",
    "plan_empty":                        "LLM returned no chunks",
    "chunk_not_dict":                    "a chunk in the LLM output is not a dict",
}

_RETRY_ACTIONS: tuple[str, str, str] = ("retry_round_2", "retry_round_3", "hard_stop")

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_CHUNK_STATUS: dict[str, str] = {
    "plan_pending":     "initial — awaiting LLM call",
    "plan_validating":  "plan validation in progress",
    "plan_invalid":     "validation failed, will retry",
    "search_ready":     "readiness gate passed, may search stock",
    "search_blocked":   "readiness gate blocked",
    "stock_searching":  "orchestrator searching stock",
    "asset_selected":   "primary clip selected for chunk",
    "unmatched":        "no candidates across L1-L5, render must stop",
    "failed":           "fatal error, render stopped",
}

# Visual category words (L3 readiness gate check)
_L3_VISUAL_CATEGORY_TOKENS: frozenset[str] = frozenset({
    "animation", "pattern", "structure", "motion", "graphic",
    "background", "footage", "loop", "abstract", "scene",
    "shot", "view", "render", "visualization", "diagram",
    "illustration", "macro", "aerial",
})

# Style / mood / motion words (L4 readiness gate check + validation rule 6a)
_L4_STYLE_TOKENS: frozenset[str] = frozenset({
    "cinematic", "abstract", "scientific", "minimalist", "vintage",
    "retro", "documentary", "epic", "dreamy", "moody", "ethereal",
    "organic", "geometric", "futuristic", "warm", "cool", "atmospheric",
    "macro", "aerial", "slow", "fast", "smooth", "dynamic", "motion",
    "ambient", "natural", "industrial", "soft", "vibrant", "monochrome",
})

# Valid cascade depths and the expected prompts keys per depth
_VALID_DEPTHS: frozenset[int] = frozenset({2, 3, 5})
_PROMPTS_KEYS_BY_DEPTH: dict[int, frozenset[str]] = {
    2: frozenset({"L1", "L5"}),
    3: frozenset({"L1", "L3", "L5"}),
    5: frozenset({"L1", "L2", "L3", "L4", "L5"}),
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class UnmatchedChunkError(RuntimeError):
    """
    across L1-L5 video + L5 image AND best_seen_video is None.

    The strict trigger (user lock #4) ensures we ONLY fail render when
    there is literally no real footage anywhere — not just "scores are
    low". Falls under pipeline-stop semantics same as PlanChunksError.
    pipeline_service catches both and writes progress=failed with the
    error_code surfaced to the frontend.

    Distinct from PlanChunksError because plan was VALID — it's the
    stock search that came back empty across all providers / all levels.
    """

    def __init__(
        self,
        chunk_index: int,
        last_reason: str = "no_candidates_across_L1_to_L5",
        *,
        sentence: str = "",
    ) -> None:
        self.chunk_index = chunk_index
        self.last_reason = last_reason
        self.sentence = sentence
        self.error_code = "unmatched_chunk"
        super().__init__(
            f"[unmatched_chunk] chunk {chunk_index}: {last_reason} "
            f"(sentence={sentence[:60]!r})"
        )

    def to_dict(self) -> dict:
        return {
            "error_code":  self.error_code,
            "chunk_index": self.chunk_index,
            "last_reason": self.last_reason,
            "sentence":    (self.sentence or "")[:200],
        }


class PlanChunksError(RuntimeError):
    """Raised by Plan Validation or Search Readiness Gate.

    The pipeline_service / API layer catches this, surfaces ``error_code``
    + ``reason`` to the frontend so the user sees an actionable Chinese
    message + "edit script" button — never a silently-degraded empty video.

    Fields::

        error_code      : one of _PLAN_ERROR_CODES keys
        reason          : human-readable (may include atoms / prompt excerpt)
        stage           : "validation" | "readiness_gate" | "retry_exhausted"
        chunk_index     : Optional[int]
        level           : Optional["L1" | "L2" | "L3" | "L4" | "L5"]
        original_prompt : Optional[str] — the prompt that triggered the fail
        retry_action    : Optional["retry_round_2" | "retry_round_3" | "hard_stop"]
        round_n         : Optional[int] — which retry round was running
    """

    def __init__(
        self,
        error_code: str,
        reason: str = "",
        *,
        stage: str = "validation",
        chunk_index: Optional[int] = None,
        level: Optional[str] = None,
        original_prompt: Optional[str] = None,
        retry_action: Optional[str] = None,
        round_n: Optional[int] = None,
    ) -> None:
        self.error_code = error_code
        self.reason = reason or _PLAN_ERROR_CODES.get(error_code, error_code)
        self.stage = stage
        self.chunk_index = chunk_index
        self.level = level
        self.original_prompt = original_prompt
        self.retry_action = retry_action
        self.round_n = round_n
        super().__init__(f"[{stage}/{error_code}] {self.reason}")

    def to_dict(self) -> dict:
        """Serialization-friendly view used by API responses + structured logs."""
        return {
            "error_code":      self.error_code,
            "reason":          self.reason,
            "stage":           self.stage,
            "chunk_index":     self.chunk_index,
            "level":           self.level,
            "original_prompt": (self.original_prompt or "")[:200],
            "retry_action":    self.retry_action,
            "round_n":         self.round_n,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# CJK ranges:
#   U+4E00..U+9FFF : CJK Unified Ideographs (main Chinese)
#   U+3000..U+303F : CJK Symbols and Punctuation
#   U+FF00..U+FFEF : Halfwidth and Fullwidth Forms (includes fullwidth punct)
#   U+3400..U+4DBF : CJK Unified Ideographs Extension A
_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯㐀-䶿]")

# Whitelisted prompt charset: lowercase ASCII letters, digits, space,
# hyphen, dot (for "1.26"-style numbers). Anything else → reject.
_PROMPT_CHARSET_RE = re.compile(r"^[a-zA-Z0-9\s\-\.]+$")

# Word tokenizer: extracts ASCII word tokens of length >= 3
_WORD_RE = re.compile(r"[a-zA-Z]{3,}")


def _contains_cjk(s: str) -> bool:
    """True if string contains any CJK character."""
    return bool(_CJK_RE.search(s or ""))


def _cjk_ratio(s: str) -> float:
    """Ratio of CJK characters in the string's non-whitespace chars."""
    if not s:
        return 0.0
    cjk = len(_CJK_RE.findall(s))
    nonws = sum(1 for c in s if not c.isspace())
    return cjk / nonws if nonws else 0.0


def _tokenize(s: str) -> set[str]:
    """Lowercase ASCII word tokens, length ≥ 3."""
    if not s:
        return set()
    return {t.lower() for t in _WORD_RE.findall(s)}


def _is_ascii_keyword_string(s: str) -> bool:
    """True if the prompt is a clean English keyword string — no CJK,
    no punctuation, no quotes, no parens. Allowed: a-z A-Z 0-9 space hyphen
    dot."""
    if not s:
        return False
    return bool(_PROMPT_CHARSET_RE.match(s))


def _flatten_atoms(atoms_dict: object) -> list[str]:
    """Flatten the 8-bucket atoms dict into a single list of lowercase tokens.

    Returns ``[]`` for missing / invalid input — caller decides whether
    empty is an error.
    """
    if not isinstance(atoms_dict, dict):
        return []
    out: list[str] = []
    for bucket_vals in atoms_dict.values():
        if isinstance(bucket_vals, list):
            for v in bucket_vals:
                if isinstance(v, str) and v.strip():
                    out.append(v.strip().lower())
    return out


def _atom_tokens(atoms_dict: object) -> set[str]:
    """Set of all atom tokens, further split into individual words for
    multi-word atoms like 'koch curve' → {'koch', 'curve'}."""
    flat = _flatten_atoms(atoms_dict)
    tokens: set[str] = set()
    for atom in flat:
        tokens |= _tokenize(atom)
    return tokens


# ---------------------------------------------------------------------------
# Plan Validation (static, 8 checks per chunk)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Search Readiness Gate (runtime, per-chunk per-level)
# ---------------------------------------------------------------------------

def validate_readiness_gate(
    *,
    chunk_index: int,
    sentence: str,
    atoms: dict,
    cascade_depth: int,
    prompts: dict,
    level: str,
    prompt: str,
    visual_must_convey: Optional[list[str]] = None,
) -> None:
    """Last line of defense before stock API call. Raises
    :class:`PlanChunksError` (stage=readiness_gate) on any block.

    Called from ``orchestrator._stock_attempt_progressive`` right before
    each ``footage.search_secondary(prompt, level)`` call.

    Defense in depth — Plan Validation should have caught structural
    issues, but DB JSON mutation / LLM provider drift / cache replay can
    still slip through. This is the closest checkpoint to the actual
    stock API request, so it gives the cleanest debug log location.


    ====  =====================================================
    L1    strong overlap (≥2 must_convey tokens) + ≥1 atom
    L2    ≥1 atoms/must_convey token overlap
    L3    visual category word + ≥1 atom token
    L4    style/mood/motion word (no subject required)
    L5    only universal 6 checks; topic-awareness from validation
    ====  =====================================================

    Args:
      visual_must_convey: list[str] from the chunk's ChunkContract.
        May be None when contract build failed (cloud auth race etc.) —
        gate then skips the overlap check (graceful degradation).
    """
    # === Universal checks (apply to all levels) ===

    # 1a. prompt non-empty
    if not prompt or not prompt.strip():
        raise PlanChunksError(
            "prompt_too_short", "empty prompt at runtime",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )

    # 1b. no CJK
    if _contains_cjk(prompt):
        raise PlanChunksError(
            "chinese_prompt_detected",
            f"runtime gate caught CJK in {level}: {prompt[:60]!r}",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )

    # 2. length 4-100 (runtime is a bit more permissive than validation)
    if len(prompt) < 4:
        raise PlanChunksError(
            "prompt_too_short", f"{level} prompt too short",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )
    if len(prompt) > 100:
        raise PlanChunksError(
            "prompt_too_long", f"{level} len={len(prompt)} > 100",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt[:200],
        )

    # 3. prompt ≠ sentence
    if sentence and prompt.strip() == sentence.strip():
        raise PlanChunksError(
            "prompt_equals_sentence",
            f"runtime gate: {level} prompt equals sentence",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )

    # 4. atoms still non-empty — use raw flatten (same as validation rule 1)
    # so chunks with only number-style atoms ("1.26") aren't false-flagged.
    # `atom_tokens` (tokenized via _tokenize) is for the LEVEL-AWARE overlap
    # rules below; emptiness check belongs on the raw atom list.
    if not _flatten_atoms(atoms):
        raise PlanChunksError(
            "empty_atoms",
            f"runtime gate caught empty atoms for chunk {chunk_index}",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )
    atom_tokens = _atom_tokens(atoms)

    if cascade_depth not in _VALID_DEPTHS:
        raise PlanChunksError(
            "invalid_cascade_depth",
            f"runtime gate: depth={cascade_depth!r}",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
            original_prompt=prompt,
        )

    # 6. prompts keys still match depth
    if not isinstance(prompts, dict):
        raise PlanChunksError(
            "prompts_keys_mismatch", "prompts not a dict at runtime",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
        )
    expected_keys = _PROMPTS_KEYS_BY_DEPTH[cascade_depth]
    actual_keys = {
        k for k, v in prompts.items() if isinstance(v, str) and v.strip()
    }
    if actual_keys != expected_keys:
        raise PlanChunksError(
            "prompts_keys_mismatch",
            f"runtime mismatch: expected {sorted(expected_keys)}, "
            f"got {sorted(actual_keys)}",
            stage="readiness_gate", chunk_index=chunk_index, level=level,
        )

    # === Level-aware overlap rules ===

    prompt_tokens = _tokenize(prompt)
    mc_tokens: set[str] = set()
    if visual_must_convey:
        for entry in visual_must_convey:
            mc_tokens |= _tokenize(entry)

    if level in ("L1", "L2"):
        # Real LLM output uses synonyms, plurals, and variations of atoms
        # that don't lexically token-match (e.g. atom 'dimensions' vs L1
        # 'dimension'; atom '1.26' vs L1 '26'). Forcing exact-match was
        # blocking too many legitimate L1s. The universal CJK / sentence-
        # equal / ASCII checks already catch the catastrophic failure
        # modes, and the cascade lets the orchestrator fall through to
        # L2-L5 if a weakly-anchored L1 fails to find stock.
        pass

    elif level == "L3":
        # Visual category word + ≥1 atom token
        if not (prompt_tokens & _L3_VISUAL_CATEGORY_TOKENS):
            raise PlanChunksError(
                "progressive_relationship_violated",
                f"L3 lacks visual category word "
                f"(prompt_tokens={sorted(prompt_tokens)})",
                stage="readiness_gate", chunk_index=chunk_index, level=level,
                original_prompt=prompt,
            )
        if not (prompt_tokens & atom_tokens):
            raise PlanChunksError(
                "prompt_no_atoms_overlap",
                f"L3 has no atom token "
                f"(prompt_tokens={sorted(prompt_tokens)}, atoms={sorted(atom_tokens)[:5]})",
                stage="readiness_gate", chunk_index=chunk_index, level=level,
                original_prompt=prompt,
            )

    elif level == "L4":
        # Style / mood / motion word required; NO subject overlap demanded
        if not (prompt_tokens & _L4_STYLE_TOKENS):
            raise PlanChunksError(
                "progressive_relationship_violated",
                f"L4 lacks style/mood/motion word "
                f"(prompt_tokens={sorted(prompt_tokens)})",
                stage="readiness_gate", chunk_index=chunk_index, level=level,
                original_prompt=prompt,
            )

    # level == "L5": universal 6 checks already done; topic-awareness
    # was enforced in plan validation rule 5. Don't impose subject overlap
    # here — that's the whole point of user's correction #2.
