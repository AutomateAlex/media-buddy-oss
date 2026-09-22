"""Media Buddy v2 global director.

One LLM call produces the full post-script shot plan. The output is deliberately
stock-search shaped: literal queries, metaphor queries, negative terms, and a
small motif pool. Video generation prompts are not produced here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.lib.llm_client import LLMClient

logger = logging.getLogger(__name__)


DEFAULT_MOTIF_POOL = [
    "forest leaf macro veins",
    "river delta aerial top view",
    "crystal growth time lapse",
    "light particles connecting",
    "geometric pattern animation",
    "spiral galaxy nebula",
]

DEFAULT_MOTIF_POOL_NORMALIZED = frozenset(
    " ".join(query.lower().split()) for query in DEFAULT_MOTIF_POOL
)


def is_default_motif_pool(values: Any) -> bool:
    """Return whether *values* contain only the emergency placeholder pool.

    The pool is searchable, but it is not evidence of the video's subject.
    Downstream planners use this signal to quarantine it when the director has
    degraded instead of promoting a placeholder such as "forest leaf".
    """
    if not isinstance(values, (list, tuple, set)):
        return False
    normalized = {
        " ".join(str(value or "").strip().lower().split())
        for value in values
        if str(value or "").strip()
    }
    return bool(normalized) and normalized <= DEFAULT_MOTIF_POOL_NORMALIZED

# -----------------------------------------------------------------------------
#
# Replaces the single 173-chunk big-generation call (which raced the cloud's
# 45s OpenRouter timeout and, on failure, dropped the WHOLE video to a
# degenerate "documentary" fallback) with a layered, locally-recoverable
# pipeline. Off by default; enable with USE_SEGMENTED_GLOBAL_DIRECTOR=1.
# -----------------------------------------------------------------------------
SEGMENT_MAX_CHUNKS = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_SEGMENT_MAX_CHUNKS", "10"))
WINDOW_MAX_TARGET_CHUNKS = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_WINDOW_MAX_CHUNKS", "24"))
CONTEXT_SEGMENTS = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_CONTEXT_SEGMENTS", "1"))
EXPANSION_CONCURRENCY = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_EXPANSION_CONCURRENCY", "2"))
SKELETON_RETRIES = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_SKELETON_RETRIES", "1"))
WINDOW_RETRIES = int(os.environ.get("MEDIA_BUDDY_DIRECTOR_WINDOW_RETRIES", "2"))

# Queries so generic they're useless as the SOLE plan for a chunk. A chunk whose
# every query is one of these is treated as invalid (forces local fallback).
_GENERIC_BAD_QUERIES = {"documentary", "concept", "meaning", "b-roll", "broll", "video"}


def _use_segmented_director() -> bool:
    return os.environ.get("USE_SEGMENTED_GLOBAL_DIRECTOR", "0").strip().lower() in (
        "1", "true", "yes",
    )


@dataclass
class DirectorSegment:
    """A coarse planning unit aggregating several adjacent sentence-chunks.

    chunk_ids are positional sentence indices (chunk_id == chunk_index == idx);
    there is no timing at this stage, so segmentation is by chunk count only.
    """
    id: str
    index: int
    chunk_ids: list[int]
    text: str


@dataclass
class ExpansionWindow:
    """One LLM expansion unit: target segments (whose chunks the model may
    output) plus read-only neighbour context (which it must NOT output)."""
    id: str
    target_segments: list[DirectorSegment]
    context_before: list[DirectorSegment]
    context_after: list[DirectorSegment]
    target_chunk_ids: list[int]


@dataclass
class _MergeResult:
    plan_core: dict[int, dict[str, Any]]
    missing: list[int] = field(default_factory=list)
    invalid: list[int] = field(default_factory=list)
    unexpected: list[Any] = field(default_factory=list)
    duplicate: list[int] = field(default_factory=list)
    failed_windows: list[str] = field(default_factory=list)

GLOBAL_DIRECTOR_SYSTEM = """You are a senior YouTube stock-footage director.

Return JSON only. Produce one complete shot plan for the whole script.

Rules:
1. Do not search abstract words directly. Translate abstract meaning into
   concrete stock-footage visuals.
2. Each non-inherited chunk needs 2-3 literal queries and, for abstract or
   emotional lines, 2-3 metaphor_queries.
3. Every stock-facing field must be English only: queries, metaphor_queries,
   motif_pool, must_show, and must_not_show. Never put Chinese or the original
   sentence in these fields. stock APIs are English-indexed.
4. Every non-inherited chunk needs must_show and must_not_show. must_not_show and
   global_no_show must NEVER include the video's main subject or anything you also
   want to show (must_show / queries). Only list genuinely off-topic / wrong-world /
   wrong-style things (cartoon, logo, watermark, text overlay, unrelated objects).
   NEVER use broad CATEGORY or SUBJECTIVE words (cute, adorable, "cute animals",
   "domesticated pets", animals, wildlife, pets, mammals) — they can't be searched
   out AND they forbid your own subject (a bear cub IS a "cute animal", a cat IS a
   "domesticated pet"). Forbid only CONCRETE wrong things ("kitchen", "city park",
   "cartoon cat", "text overlay"), never a category the subject belongs to.
5. Set visual_anchor=true when the sentence names a concrete visible subject,
   action, place, product, object, person, organism, diagram-worthy concept,
   or specific measurable visual target. Set visual_anchor=false only for
   pure transition, emotion, summary, or CTA lines that can safely reuse the
   previous shot.
6. Transition/CTA chunks should set inherit_prev=true and leave queries empty.
   Never set inherit_prev=true when visual_anchor=true.
7. motif_pool must contain 6 high-availability stock queries that fit the
   whole video's theme.
8. Each chunk's queries come from THAT sentence's OWN concrete visible nouns /
   actions / objects / place (coins, cardboard boxes, coffee cherries, faded
   seal, ledger page, shop sign). Do NOT prepend the video's subject word to
   every chunk, and do NOT replace a concrete sentence with a business cliché
   ("hard work", "small business owner working", "spreadsheet").
9. COVER THE WHOLE SCRIPT: output exactly one chunk object for EVERY chunk_id
   from 0 to the last sentence. Never stop early.
10. Keep each chunk COMPACT — emit ONLY the English fields in the schema. Do NOT
    echo the original sentence and do NOT write any Chinese. Brevity per chunk is
    what lets you fit every sentence of the whole script into one response.

Output schema (emit ONE object per sentence, all of them):
{
  "video_meta": {
    "theme": "...",
    "visual_world": "...",
    "global_no_show": ["..."],
    "motif_pool": ["..."]
  },
  "chunks": [
    {
      "chunk_id": 0,
      "sentence_type": "fact|comparison|example|abstract_metaphor|emotion|transition|cta",
      "visual_anchor": true,
      "inherit_prev": false,
      "must_show": ["..."],
      "must_not_show": ["..."],
      "queries": ["english stock query"],
      "metaphor_queries": ["english stock query"]
    }
  ]
}
"""


def build_global_plan(
    llm: LLMClient,
    sentences: list[str],
    *,
    user_brief: str = "",
    use_segmented: Optional[bool] = None,
    harden_subject_anchor: bool = True,
) -> dict[str, Any]:
    """Build a v2 global shot plan.

    Dispatches to the segmented director (explicit use_segmented=True or
    USE_SEGMENTED_GLOBAL_DIRECTOR=1) or the legacy single-call path. Both return
    the same shape:
    {"video_meta": {..., "motif_pool"}, "chunks": [<full per-chunk dict>]}.
    """
    segmented = _use_segmented_director() if use_segmented is None else bool(use_segmented)
    if segmented:
        return _build_global_plan_segmented(
            llm, sentences, user_brief=user_brief,
            harden_subject_anchor=harden_subject_anchor,
        )
    return _build_global_plan_legacy(llm, sentences, user_brief=user_brief)


def _build_global_plan_legacy(
    llm: LLMClient,
    sentences: list[str],
    *,
    user_brief: str = "",
) -> dict[str, Any]:
    """Legacy single big-generation path. Kept as rollback behind the flag;
    on LLM failure or malformed JSON it returns the conservative whole-video
    fallback so the pipeline can still run."""
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
    user = (
        f"User brief:\n{user_brief or '(none)'}\n\n"
        f"Script sentences:\n{numbered}\n\n"
        "Return exactly one JSON object matching the schema."
    )
    # that shapes the whole video's stock queries + motif_pool; a single flaky
    # cloud-LLM upstream error here used to drop the run straight to the
    # degenerate fallback plan (generic "documentary" queries → nothing matches
    # → footage stalls). Retry a few times before falling back so a momentary
    # upstream hiccup doesn't poison the entire render.
    import time as _time
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = llm._call(
                GLOBAL_DIRECTOR_SYSTEM,
                user,
                purpose="global_director",
            )
            data = _extract_json(raw)
            if _has_non_english_stock_fields(data):
                data = _repair_stock_fields_to_english(llm, data)
            return _normalize_plan(data, sentences)
        except Exception as e:
            last_err = e
            logger.warning(
                "global_director attempt %d/3 failed: %s", attempt + 1, e,
            )
            if attempt < 2:
                _time.sleep(1.5 * (attempt + 1))  # 1.5s, then 3s
    logger.warning(
        "global_director failed after 3 attempts, using fallback plan: %s",
        last_err,
    )
    return _fallback_plan(sentences, allow_whole_movie_fallback=True)


def _trim_to_last_brace(s: str) -> str:
    j = s.rfind("}")
    return s[: j + 1] if j > 0 else ""


def _repair_truncated_json(s: str) -> str:
    """Best-effort repair for a TRUNCATED JSON object (the usual cause of
    'Expecting , delimiter' from an LLM that hit its token ceiling): close any
    still-open string, drop a dangling trailing comma, and close the open
    braces/brackets in reverse order. Salvages the parsed-so-far structure so we
    avoid a full re-call (each global_director retry is a ~30-60s LLM round-trip)."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack:
            stack.pop()
    out = s
    if in_str:
        out += '"'
    out = re.sub(r",\s*$", "", out.rstrip())
    out += "".join(reversed(stack))
    return out


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    # Strip markdown code fences (```json ... ```) if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # Fast path.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in global director response")
    span = text[start:]
    # Try, in order: full span; trim-to-last-}; close-open-brackets repair; and
    # finally repair AFTER trimming to the last complete '}' (drops a half-written
    # trailing chunk but keeps every complete one).
    for candidate in (
        span,
        _trim_to_last_brace(span),
        _repair_truncated_json(span),
        _repair_truncated_json(_trim_to_last_brace(span)),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise ValueError("could not parse JSON from global director response")


def _has_non_english_stock_fields(data: dict[str, Any]) -> bool:
    meta = data.get("video_meta") if isinstance(data.get("video_meta"), dict) else {}
    values: list[Any] = []
    values.extend(meta.get("motif_pool") or [])
    values.extend(meta.get("global_no_show") or [])
    chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    for raw in chunks:
        if not isinstance(raw, dict):
            continue
        for key in ("queries", "metaphor_queries", "must_show", "must_not_show"):
            values.extend(raw.get(key) or [])
    return any(str(v).strip() and not str(v).strip().isascii() for v in values)


def _repair_stock_fields_to_english(llm: LLMClient, data: dict[str, Any]) -> dict[str, Any]:
    """Translate only stock-facing fields to English.

    This is a rare safety net. stock search is English-first, so Chinese
    query text must be repaired before literal_probe can see it.
    """
    system = """Return JSON only.
Translate stock-footage search fields to concise English.
Keep structure, chunk_id, text, sentence_type, visual_anchor, inherit_prev, and intent unchanged.
Only rewrite these fields when present: video_meta.motif_pool, video_meta.global_no_show,
chunks[].queries, chunks[].metaphor_queries, chunks[].must_show, chunks[].must_not_show.
Rules:
- Use English only in those stock fields.
- Use concrete searchable stock terms, 2-8 words for queries.
- Do not add Chinese.
"""
    try:
        raw = llm._call(
            system,
            json.dumps(data, ensure_ascii=False),
            purpose="global_director_query_repair",
        )
        repaired = _extract_json(raw)
        if _has_non_english_stock_fields(repaired):
            logger.warning("global_director query repair still contained non-English; dropping invalid fields")
        return repaired
    except Exception as e:
        logger.warning("global_director query repair failed: %s", e)
        return data


def _normalize_plan(data: dict[str, Any], sentences: list[str]) -> dict[str, Any]:
    meta = data.get("video_meta") if isinstance(data.get("video_meta"), dict) else {}
    cleaned_motif_pool = _clean_stock_queries(meta.get("motif_pool"))[:6]
    motif_pool = cleaned_motif_pool or DEFAULT_MOTIF_POOL
    motif_pool_source = "llm" if cleaned_motif_pool else "default_fallback"
    global_no_show = _clean_stock_terms(meta.get("global_no_show"))
    chunks_raw = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    by_id = {}
    for raw in chunks_raw:
        if not isinstance(raw, dict):
            continue
        try:
            cid = int(raw.get("chunk_id"))
        except (TypeError, ValueError):
            continue
        by_id[cid] = raw

    chunks = [
        _normalize_one_chunk(idx, sentence, by_id.get(idx, {}), global_no_show)
        for idx, sentence in enumerate(sentences)
    ]
    return {
        "video_meta": {
            "theme": str(meta.get("theme") or ""),
            "visual_world": str(meta.get("visual_world") or ""),
            "global_no_show": global_no_show,
            "motif_pool": motif_pool,
            "motif_pool_source": motif_pool_source,
        },
        "chunks": chunks,
        "meta": {
            "version": "legacy",
            "whole_movie_fallback": False,
            "director_degraded": motif_pool_source == "default_fallback",
            "degradation_reasons": (
                ["default_motif_pool"] if motif_pool_source == "default_fallback" else []
            ),
        },
    }


def _normalize_one_chunk(
    idx: int,
    sentence: str,
    raw: dict[str, Any],
    global_no_show: list[str],
) -> dict[str, Any]:
    """Fill the full ~25-field per-chunk dict the v2 pipeline consumes from a
    small core (queries/metaphor_queries/must_show/must_not_show/sentence_type/
    visual_anchor/inherit_prev/...). Shared by the legacy normalizer and the
    segmented merge so both emit an identical schema."""
    sentence_type = str(raw.get("sentence_type") or "fact")
    visual_anchor = _coerce_visual_anchor(raw, sentence_type)
    inherit_prev = bool(raw.get("inherit_prev", False)) and not visual_anchor
    queries = _clean_stock_queries(raw.get("queries"))
    metaphor_queries = _clean_stock_queries(raw.get("metaphor_queries"))
    must_not = _clean_stock_terms(raw.get("must_not_show")) + global_no_show
    must_show = _clean_stock_terms(raw.get("must_show"))
    return {
        "chunk_index": idx,
        "chunk_id": idx,
        "sentence": str(raw.get("text") or sentence),
        "text": str(raw.get("text") or sentence),
        "sentence_type": sentence_type,
        "is_abstract": bool(raw.get("is_abstract", False)),
        "visual_anchor": visual_anchor,
        "inherit_prev": inherit_prev,
        "inherit_reason": str(raw.get("inherit_reason") or ""),
        "intent": str(raw.get("intent") or sentence[:80]),
        "must_show": must_show,
        "must_not_show": _dedupe(must_not),
        "queries": [] if inherit_prev else queries,
        "metaphor_queries": metaphor_queries,
        "metaphor_essence": str(raw.get("metaphor_essence") or ""),
        # Compatibility fields used by existing persistence / UI.
        "search_query": " ".join(queries[:1]),
        "search_keywords": [],
        "ai_prompt": "",
        "visual_tags": [],
        "motion_tags": [],
        "shot_type": "atmosphere",
        "is_hero_shot": idx == 0,
        "priority": "high" if idx == 0 else "normal",
        "target_shot_count": 1,
        "atoms": {},
        "cascade_depth": 2,
        "prompts": {},
        # Provenance for observability (llm_expand / deterministic_fallback /
        # legacy). Harmless extra key; downstream ignores unknown fields.
        "plan_source": str(raw.get("plan_source") or "legacy"),
    }


# =============================================================================
# Segmented global director (V1) — layered, locally-recoverable shot planning.
# Any layer may fail, but a failure only degrades its local chunks; the whole
# video is NEVER dropped to a degenerate "documentary" plan.
# =============================================================================

_SKELETON_SYSTEM = """You are planning the visual direction for a long video.
Return compact valid JSON only. No markdown. No explanation.

You receive a brief and a list of director segments (segment_id + transcript text).
Produce video-level meta and exactly ONE section per input segment.
Every stock-facing field MUST be English only (stock APIs are English-first).

Schema:
{
  "video_meta": {
    "theme": "short theme",
    "visual_world": "short visual world",
    "global_no_show": ["english terms to avoid"],
    "motif_pool": ["6 high-availability english stock queries fitting the whole theme"]
  },
  "sections": [
    {
      "segment_id": "seg_000",
      "role": "short narrative role",
      "motif": "max 4 words",
      "visual_direction": "max 16 words",
      "search_bank": ["2-5 english stock queries, each 2-6 words"]
    }
  ]
}
Rules: exactly one section per input segment; no per-chunk output; JSON only."""

_EXPAND_SYSTEM = """You expand a high-level plan into chunk-level stock-search plans.
Return compact valid JSON only. No markdown. No explanation.

You receive: the global style, the target segments' skeleton sections, neighbour
context, and a TARGET list of chunks (id + sentence). Output ONE item per target
chunk id. Only output ids in TARGET_CHUNK_IDS; never output context ids.
All query fields English only. Queries must be concrete searchable stock footage.

STOCK REALITY (HIGHEST PRIORITY — these OVERRIDE every other rule below whenever
they conflict. We ONLY have a real-footage library; we do NOT do AI generation,
animation, data-viz/number/arrow overlays, infrared/thermal imaging, split-
screens, diagrams, or cell/neural/receptor/molecular animation):
- Every query must be something a REAL camera can film: a real subject, action,
  place, or human moment. NEVER query "infrared", "thermal imaging", "TRPV1 /
  receptor protein", "neural pathway", "ion channel", "split screen", or any
  "<X> animation / diagram / overlay / cross-section".
- NICHE-SUBJECT RULE (overrides "use the sentence's literal noun" and "anchor on
  the recurring subject"): if the main subject is a niche species the stock
  library barely has (kangaroo rat, jerboa, fennec, pangolin, axolotl…), the
  FIRST query MUST be the BROADENED generic category ("desert rodent", "small
  desert mammal", "rodent in sand"); the exact species may appear ONLY as a 2nd
  query. Do NOT make every chunk "kangaroo rat X".
- HUMAN / DIFFERENT-SUBJECT RULE (overrides "abstract → main-subject b-roll"): if
  THIS sentence is about a human or a different subject than the main one (你 /
  人类 / 手 / 手腕 / person / hand), query THAT subject's real action — e.g.
  "你摸暴晒车门会先缩手" → "hand touching hot car door" / "man wincing pain". Do
  NOT substitute the main animal here.
- Abstract / scientific / numeric sentences (with no human) → map to a REAL
  filmable substitute: (a) the subject's real behaviour, (b) on-topic environment
  b-roll, or (c) a human-empathy analogy. Examples: "nerve faster than brain" →
  "man wincing" / "hand pulling back from hot surface"; "dense ion channels in
  skin" → "animal skin macro texture"; "receptor fires above 43C" → "desert heat
  haze" / "sun-baked rock".
- A word with a scientific meaning that also has an everyday meaning (switch,
  channel, signal, trigger, gate) → take ONLY the biological/behavioural sense;
  never query a literal light switch, electric wire, or plant root.

CRITICAL rules for stock-footage matchability (most rejects come from breaking these):
- Build each chunk's queries from what THAT sentence literally shows — its OWN
  concrete, visible noun / object / action / place. Read the sentence in front of
  you; do NOT paste one global template onto every chunk.
- "show" = at most 2 concrete VISIBLE nouns taken from THIS sentence (the thing it
  depicts + optionally one place/scene word). NEVER abstract concepts, emotions,
  adjectives, numbers, or compound phrases.
- The video may have a recurring subject. It is the main ACTOR or SETTING the
  script follows (the street vendor at his stall, the wolf in snow) — NEVER an
  ingredient, flavor, material, colour or prop (rose, sugar, copper, plastic).
  Use it ONLY in sentences that are actually about it. If a sentence introduces a
  DIFFERENT concrete thing (a paper cup, a metro stall, four drinks, a foil bag),
  query THAT thing directly — and NEVER prepend an ingredient/material/flavor word
  to a sentence that is not about that ingredient ("rose four drinks" wrongly pulls
  rose wine; "copper paper cup" wrongly pulls copper wire).
  GOOD (sentence about the animal): ["wolf howling snow","wolf howling","wolf snow"].
  GOOD (sentence about a paper cup): ["recycled paper cup elephant print","eco paper cup","illustrated paper cup"].
  GOOD (sentence about a metro stall): ["indian metro station street stall","subway entrance food cart"].
  BAD (forcing a prop word everywhere): ["copper paper cup","copper metro stall"] — copper is NOT what those sentences show.
- For an ABSTRACT / emotional / numeric / conclusion sentence (no filmable thing of
  its own), output "show":[] and queries = plain on-topic B-roll of the main subject
  or setting in action (e.g. the vendor working at his stall, the wolf in snow).
  Do NOT film abstractions ("balance","profit","truth"). VARY these b-roll chunks —
  pull a DIFFERENT facet each time (the hands working, the stall, the crowd, the
  cups, the signage, a wide shot); two abstract chunks must NOT share the same query.
- "q" = 2-4 queries, 2-5 words each, concrete and searchable. Prefer THIS sentence's
  own subject; broaden to the recurring subject/setting only as the fallback rung.
  Never produce "{abstract_word} close up".

Schema:
{"items":[{"id":0,"q":["2-4 english queries, 2-5 words, concrete per-sentence visual"],"show":["<=2 concrete visible nouns from THIS sentence, [] if abstract"],"avoid":["0-3 forbidden"],"m":"motif max 4 words"}]}
Rules: one item per target chunk id; JSON only."""


def build_deterministic_segments(
    sentences: list[str], *, max_chunks: int = SEGMENT_MAX_CHUNKS,
) -> list[DirectorSegment]:
    """Aggregate adjacent sentence-chunks into coarse director segments. No LLM,
    so always available — used both as window-alignment boundaries and as the
    skeleton's deterministic fallback driver. Pure chunk-count split (no timing
    exists at this stage)."""
    max_chunks = max(1, max_chunks)
    segments: list[DirectorSegment] = []
    current: list[int] = []
    for idx in range(len(sentences)):
        current.append(idx)
        if len(current) >= max_chunks:
            segments.append(_make_segment(len(segments), current, sentences))
            current = []
    if current:
        segments.append(_make_segment(len(segments), current, sentences))
    return segments


def _make_segment(index: int, chunk_ids: list[int], sentences: list[str]) -> DirectorSegment:
    text = " ".join((sentences[i] or "") for i in chunk_ids).strip()
    return DirectorSegment(
        id=f"seg_{index:03d}", index=index, chunk_ids=list(chunk_ids), text=text,
    )


def make_segment_aligned_windows(
    segments: list[DirectorSegment],
    *,
    max_target_chunks: int = WINDOW_MAX_TARGET_CHUNKS,
    context_segments: int = CONTEXT_SEGMENTS,
) -> list[ExpansionWindow]:
    """Group adjacent segments into expansion windows of <= max_target_chunks
    chunks (a single oversized segment becomes its own window). Each window
    carries neighbour segments as read-only context."""
    groups: list[list[DirectorSegment]] = []
    current: list[DirectorSegment] = []
    for seg in segments:
        cur_count = sum(len(s.chunk_ids) for s in current)
        if current and cur_count + len(seg.chunk_ids) > max_target_chunks:
            groups.append(current)
            current = [seg]
        else:
            current.append(seg)
    if current:
        groups.append(current)

    windows: list[ExpansionWindow] = []
    for i, group in enumerate(groups):
        first = group[0].index
        last = group[-1].index
        before = segments[max(0, first - context_segments):first]
        after = segments[last + 1:last + 1 + context_segments]
        target_chunk_ids = [cid for s in group for cid in s.chunk_ids]
        windows.append(ExpansionWindow(
            id=f"win_{i:03d}",
            target_segments=group,
            context_before=before,
            context_after=after,
            target_chunk_ids=target_chunk_ids,
        ))
    return windows


def build_skeleton_or_fallback(
    llm: LLMClient, segments: list[DirectorSegment], *, user_brief: str = "",
) -> dict[str, Any]:
    """One small LLM call for the global skeleton; on failure (timeout / bad
    JSON), fall back to a deterministic skeleton. Never raises — skeleton is not
    a single point."""
    for attempt in range(SKELETON_RETRIES + 1):
        try:
            raw = llm._call(
                _SKELETON_SYSTEM, _skeleton_user(segments, user_brief),
                purpose="global_director_skeleton",
            )
            data = _extract_json(raw)
            return _complete_skeleton(data, segments, source="llm")
        except Exception as e:  # noqa: BLE001
            logger.warning("global_director_skeleton attempt %d/%d failed: %s",
                           attempt + 1, SKELETON_RETRIES + 1, e)
            if attempt < SKELETON_RETRIES:
                time.sleep(1.0 * (attempt + 1))
    logger.warning("global_director_skeleton using deterministic fallback")
    return _deterministic_skeleton(segments)


def _skeleton_user(segments: list[DirectorSegment], user_brief: str) -> str:
    lines = [f"[{s.id}] {s.text[:280]}" for s in segments]
    return (
        f"User brief:\n{user_brief or '(none)'}\n\n"
        f"Director segments:\n" + "\n".join(lines) + "\n\n"
        "Return exactly one JSON object matching the schema."
    )


def _complete_skeleton(
    data: dict[str, Any], segments: list[DirectorSegment], *, source: str,
) -> dict[str, Any]:
    """Build a COMPLETE skeleton: keep valid LLM sections, fill gaps
    deterministically per segment, and normalize video_meta. Never raises."""
    if not isinstance(data, dict):
        return _deterministic_skeleton(segments)
    meta = data.get("video_meta") if isinstance(data.get("video_meta"), dict) else {}
    sections_raw = data.get("sections") if isinstance(data.get("sections"), list) else []
    by_seg: dict[str, dict[str, Any]] = {}
    for sec in sections_raw:
        if isinstance(sec, dict) and sec.get("segment_id"):
            by_seg[str(sec["segment_id"])] = sec

    sections: list[dict[str, Any]] = []
    for seg in segments:
        raw = by_seg.get(seg.id, {})
        bank = _clean_stock_queries(raw.get("search_bank"))
        if not bank:
            bank = _deterministic_search_bank(seg)
        sections.append({
            "segment_id": seg.id,
            "chunk_ids": seg.chunk_ids,
            "role": str(raw.get("role") or "segment"),
            "motif": str(raw.get("motif") or "neutral")[:64],
            "visual_direction": str(raw.get("visual_direction") or "")[:160],
            "search_bank": bank,
        })
    cleaned_motif_pool = _clean_stock_queries(meta.get("motif_pool"))[:6]
    motif_pool = cleaned_motif_pool or DEFAULT_MOTIF_POOL
    return {
        "video_meta": {
            "theme": str(meta.get("theme") or ""),
            "visual_world": str(meta.get("visual_world") or ""),
            "global_no_show": _clean_stock_terms(meta.get("global_no_show")),
            "motif_pool": motif_pool,
            "_motif_pool_source": "llm" if cleaned_motif_pool else "default_fallback",
            "_source": source,
        },
        "sections": sections,
    }


def _deterministic_skeleton(segments: list[DirectorSegment]) -> dict[str, Any]:
    sections = [{
        "segment_id": seg.id,
        "chunk_ids": seg.chunk_ids,
        "role": "segment",
        "motif": "neutral",
        "visual_direction": "",
        "search_bank": _deterministic_search_bank(seg),
    } for seg in segments]
    return {
        "video_meta": {
            "theme": "", "visual_world": "",
            "global_no_show": [], "motif_pool": DEFAULT_MOTIF_POOL,
            "_motif_pool_source": "default_fallback",
            "_source": "deterministic",
        },
        "sections": sections,
    }


def _deterministic_search_bank(seg: DirectorSegment) -> list[str]:
    """A rotating slice of the default motif pool — real, searchable English
    queries (never the old 'documentary' constant), varied per segment."""
    pool = DEFAULT_MOTIF_POOL
    n = len(pool)
    start = (seg.index * 2) % n
    return [pool[(start + k) % n] for k in range(3)]


def run_expansion_windows(
    llm: LLMClient,
    windows: list[ExpansionWindow],
    skeleton: dict[str, Any],
    sentences: list[str],
) -> list[dict[str, Any]]:
    """Expand each window with its own small LLM call. A window failure returns
    empty items (local fallback handles it) and never affects sibling windows."""
    if not windows:
        return []
    if EXPANSION_CONCURRENCY <= 1 or len(windows) == 1:
        return [_expand_window_with_retries(llm, w, skeleton, sentences) for w in windows]
    with ThreadPoolExecutor(max_workers=EXPANSION_CONCURRENCY) as ex:
        return list(ex.map(
            lambda w: _expand_window_with_retries(llm, w, skeleton, sentences),
            windows,
        ))


def _expand_window_with_retries(
    llm: LLMClient,
    window: ExpansionWindow,
    skeleton: dict[str, Any],
    sentences: list[str],
) -> dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(WINDOW_RETRIES + 1):
        started = time.monotonic()
        try:
            items = _expand_window(llm, window, skeleton, sentences)
            logger.info(
                "global_director_expand window=%s targets=%d attempt=%d "
                "duration_ms=%d status=ok items=%d",
                window.id, len(window.target_chunk_ids), attempt,
                int((time.monotonic() - started) * 1000), len(items),
            )
            return {
                "window_id": window.id,
                "target_chunk_ids": window.target_chunk_ids,
                "items": items,
                "status": "ok",
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            code = _error_code(e)
            logger.warning(
                "global_director_expand window=%s attempt=%d duration_ms=%d "
                "status=error code=%s: %s",
                window.id, attempt, int((time.monotonic() - started) * 1000), code, e,
            )
            if attempt < WINDOW_RETRIES:
                if code == "upstream_rate_limit":
                    time.sleep(2 + attempt * 3)
                else:
                    time.sleep(1 + attempt * 2)
    return {
        "window_id": window.id,
        "target_chunk_ids": window.target_chunk_ids,
        "items": [],
        "status": "failed",
        "error": repr(last_err),
    }


def _expand_window(
    llm: LLMClient,
    window: ExpansionWindow,
    skeleton: dict[str, Any],
    sentences: list[str],
) -> list[dict[str, Any]]:
    raw = llm._call(
        _EXPAND_SYSTEM, _expand_user(window, skeleton, sentences),
        purpose="global_director_expand",
    )
    data = _extract_json(raw)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("expand response has no items array")
    return [it for it in items if isinstance(it, dict)]


def _expand_user(
    window: ExpansionWindow, skeleton: dict[str, Any], sentences: list[str],
) -> str:
    vm = skeleton.get("video_meta", {})
    sec_by_seg = {s["segment_id"]: s for s in skeleton.get("sections", [])}
    target_lines = []
    for seg in window.target_segments:
        sec = sec_by_seg.get(seg.id, {})
        target_lines.append(
            f"[{seg.id}] dir={sec.get('visual_direction','')} "
            f"bank={', '.join(sec.get('search_bank', [])[:4])}"
        )
    ctx_lines = [
        f"[{s.id}] {s.text[:120]}"
        for s in (window.context_before + window.context_after)
    ]
    chunk_lines = [f"{cid}: {sentences[cid]}" for cid in window.target_chunk_ids]
    return (
        f"Style: theme={vm.get('theme','')} visual_world={vm.get('visual_world','')}\n\n"
        f"Target skeleton sections:\n" + "\n".join(target_lines) + "\n\n"
        f"Context segments (DO NOT output these chunks):\n" + ("\n".join(ctx_lines) or "(none)") + "\n\n"
        f"TARGET_CHUNK_IDS: {window.target_chunk_ids}\n\n"
        f"Target chunks:\n" + "\n".join(chunk_lines) + "\n\n"
        "Return exactly one JSON object with an items array."
    )


def merge_batch_results(
    sentences: list[str], batch_results: list[dict[str, Any]],
) -> _MergeResult:
    """Strict whitelist merge: a window may only write its own target ids;
    context / out-of-range / unknown / duplicate / invalid items are dropped
    (recorded for observability). Missing ids are filled downstream."""
    n = len(sentences)
    plan_core: dict[int, dict[str, Any]] = {}
    invalid: list[int] = []
    unexpected: list[Any] = []
    duplicate: list[int] = []
    failed_windows: list[str] = []

    for result in batch_results:
        if result.get("status") != "ok":
            failed_windows.append(result.get("window_id"))
            continue
        target = set(result.get("target_chunk_ids") or [])
        for item in result.get("items") or []:
            cid = _parse_int_id(item.get("id"))
            if cid is None or cid not in target or cid < 0 or cid >= n:
                unexpected.append(item.get("id"))
                continue
            core = _normalize_expand_item(item)
            if not _is_valid_core(core):
                invalid.append(cid)
                continue
            if cid in plan_core:
                duplicate.append(cid)
                continue
            plan_core[cid] = core

    missing = [i for i in range(n) if i not in plan_core]
    return _MergeResult(plan_core, missing, invalid, unexpected, duplicate, failed_windows)


def _normalize_expand_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map the compact expand item (q/show/avoid/m, or long keys) to a chunk
    core consumed by _normalize_one_chunk."""
    queries = _clean_stock_queries(item.get("q") if item.get("q") is not None else item.get("queries"))
    must_show = _clean_stock_terms(item.get("show") if item.get("show") is not None else item.get("must_show"))
    must_not = _clean_stock_terms(item.get("avoid") if item.get("avoid") is not None else item.get("must_not_show"))
    metaphor = _clean_stock_queries(item.get("metaphor_queries"))
    return {
        "queries": queries,
        "metaphor_queries": metaphor,
        "must_show": must_show,
        "must_not_show": must_not,
        "sentence_type": str(item.get("sentence_type") or "fact"),
        "visual_anchor": bool(item.get("visual_anchor", True)),
        "inherit_prev": bool(item.get("inherit_prev", False)),
        "plan_source": "llm_expand",
    }


def _is_valid_core(core: dict[str, Any]) -> bool:
    queries = core.get("queries") or []
    if not queries:
        return False
    if all(str(q).strip().lower() in _GENERIC_BAD_QUERIES for q in queries):
        return False
    return True


def fill_missing_with_local_fallback(
    plan_core: dict[int, dict[str, Any]],
    sentences: list[str],
    segments: list[DirectorSegment],
    skeleton: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """Fill any chunk the LLM missed/invalidated with a LOCAL deterministic core
    (segment skeleton search_bank — real English, theme-fitted). Never whole
    video, never the 'documentary' constant."""
    seg_by_chunk = {cid: seg for seg in segments for cid in seg.chunk_ids}
    sec_by_seg = {s["segment_id"]: s for s in skeleton.get("sections", [])}
    for idx in range(len(sentences)):
        if idx in plan_core:
            continue
        seg = seg_by_chunk.get(idx)
        section = sec_by_seg.get(seg.id) if seg else None
        plan_core[idx] = _deterministic_chunk_core(section)
    return plan_core


def _deterministic_chunk_core(section: Optional[dict[str, Any]]) -> dict[str, Any]:
    bank = list((section or {}).get("search_bank") or [])
    queries = _dedupe(bank)[:4] or list(DEFAULT_MOTIF_POOL[:2])
    motif = str((section or {}).get("motif") or "neutral")[:64]
    return {
        "queries": queries,
        "metaphor_queries": [],
        "must_show": queries[:2],
        "must_not_show": [],
        "sentence_type": "fact",
        "visual_anchor": True,
        "inherit_prev": False,
        "plan_source": "deterministic_fallback",
        "motif": motif,
    }


def final_validate_or_raise(sentences: list[str], chunks: list[dict[str, Any]]) -> None:
    n = len(sentences)
    idxs = [c["chunk_index"] for c in chunks]
    if set(idxs) != set(range(n)):
        raise RuntimeError(
            "global_director_final_coverage_failed: "
            f"missing={sorted(set(range(n)) - set(idxs))} "
            f"extra={sorted(set(idxs) - set(range(n)))}"
        )
    if len(idxs) != len(set(idxs)):
        raise RuntimeError("global_director_final_duplicate_chunk_index")
    for c in chunks:
        if not c.get("inherit_prev") and not c.get("queries") and not c.get("metaphor_queries"):
            raise RuntimeError(f"global_director_final_empty_queries: {c['chunk_index']}")


def _parse_int_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_code(exc: Exception) -> str:
    msg = str(exc).lower()
    if "upstream_timeout" in msg or "timeout" in msg or "aborted" in msg:
        return "upstream_timeout"
    if "rate_limit" in msg or "429" in msg:
        return "upstream_rate_limit"
    if re.search(r"\b5\d\d\b", msg):
        return "upstream_5xx"
    return "error"


# bottleneck is over-constrained must_show + degraded fallback queries, not the
# observer. These terms must never act as a literal search subject or a hard
# must_show item; the observer would reject perfectly good footage for "not
# showing balance/truth/fear". Kept deliberately small and topic-agnostic.
_ABSTRACT_TERMS = frozenset({
    "balance", "ecosystem", "ecosystem balance", "natural selection", "truth",
    "fear", "conflict", "dominance", "human dominance", "ripple", "ripple effect",
    "civilization", "population decline", "family bonds", "interconnected nature",
    "meaning", "concept", "documentary", "harmony", "power", "survival", "instinct",
    "freedom", "mystery", "chaos", "order", "control", "emotion", "resilience",
    "legacy", "destiny", "sound waves", "soundwave", "sound wave", "frequency",
    "rhythm", "breath", "echo", "tension", "fate", "spirit", "energy", "presence",
    "nature itself", "the unknown", "time", "memory", "silence", "void",
    "browsed", "empty", "modern", "abstract", "various", "concept art",
})


def _strip_abstract(terms: list) -> list[str]:
    out = []
    for t in terms or []:
        s = str(t).strip().lower()
        if s and s not in _ABSTRACT_TERMS:
            out.append(s)
    return out


def _derive_topic_anchor(chunks: list[dict], video_meta: dict) -> str:
    """The single recurring concrete subject of the whole video (e.g. 'wolf').
    Most common 1-word head among non-abstract must_show terms; falls back to the
    motif_pool's first concrete word."""
    counts: dict[str, int] = {}
    for c in chunks:
        for t in _strip_abstract(c.get("must_show")):
            head = t.split()[0] if t.split() else ""
            if head:
                counts[head] = counts.get(head, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    for m in (video_meta.get("motif_pool") or []):
        w = _strip_abstract([str(m)])
        if w:
            return w[0].split()[0]
    return ""


# Umbrella / subjective forbid terms that must NEVER sit in must_not_show:
# they can't be reliably excluded by a stock search AND they semantically cover
# whole subjects (a bear cub IS a "cute animal"; a cat IS a "domesticated pet"),
# so the observer judge rejects the video's OWN subject. We strip any forbid that
# is purely subjective, OR that reduces to a generic animal-category noun once
# generic modifiers are removed. Concrete distractors ("cartoon cat", "text
# overlay", "kitchen cooking", "modern city park") are NOT umbrellas → kept.
_UMBRELLA_MODIFIERS = {
    "cute", "adorable", "baby", "lovely", "pretty", "beautiful", "domestic",
    "domesticated", "very", "super", "wild", "small", "little", "tiny", "young",
}
_UMBRELLA_CATEGORY_NOUNS = {
    "animal", "animals", "pet", "pets", "creature", "creatures", "critter",
    "critters", "wildlife", "mammal", "mammals", "fauna",
}
_PURE_SUBJECTIVE_FORBIDS = {
    "cute", "adorable", "cutesy", "lovely", "pretty", "beautiful", "cuteness",
}


def _is_umbrella_forbid(term: object) -> bool:
    import re as _re
    words = _re.findall(r"[a-z]+", str(term).lower())
    if not words:
        return False
    if all(w in _PURE_SUBJECTIVE_FORBIDS for w in words):
        return True  # "cute", "adorable animals" core handled below
    core = [w for w in words if w not in _UMBRELLA_MODIFIERS]
    # After dropping generic modifiers, nothing left but generic category nouns
    # → it's an umbrella that covers any animal subject (e.g. "cute animals" →
    # {animals}, "domesticated pets" → {pets}). Drop it.
    return bool(core) and all(w in _UMBRELLA_CATEGORY_NOUNS for w in core)


def _scrub_subject_from_must_not(chunks: list[dict], video_meta: dict) -> None:
    """Never forbid the thing the video is ABOUT. Remove from each chunk's
    must_not_show any term whose EVERY word is something we actually search for
    (the topic anchor, any chunk's queries/must_show, the motif_pool), AND any
    umbrella/subjective category term (see _is_umbrella_forbid). Fixes the bug
    where the LLM put the main subject (e.g. 'cat') OR an umbrella that covers it
    ('cute animals', 'domesticated pets') into global_no_show — the orchestrator
    then rejected nearly all on-topic footage (pass_rate collapsed, the whole
    video reused one clip / grabbed random off-subject animals). Mixed forbids
    like 'cartoon cat' / 'text overlay' are kept. Mutates chunks in place."""
    import re as _re
    show_words: set[str] = set()

    def _add(term: object) -> None:
        for w in _re.findall(r"[a-z0-9]+", str(term).lower()):
            if len(w) >= 2:
                show_words.add(w)

    _add(_derive_topic_anchor(chunks, video_meta))
    for c in chunks:
        for field in ("queries", "metaphor_queries", "must_show"):
            for t in (c.get(field) or []):
                _add(t)
    for m in (video_meta.get("motif_pool") or []):
        _add(m)
    # Note: even when show_words is empty we still strip umbrella forbids below.
    for c in chunks:
        kept: list[str] = []
        for term in (c.get("must_not_show") or []):
            if _is_umbrella_forbid(term):
                continue  # umbrella/subjective term — covers the subject, drop it
            words = set(_re.findall(r"[a-z0-9]+", str(term).lower()))
            if show_words and words and words.issubset(show_words):
                continue  # the whole forbid is stuff we WANT to show — drop it
            kept.append(term)
        c["must_not_show"] = kept


def _harden_long_chunks(chunks: list[dict], video_meta: dict) -> None:
    """Apply the four-cuts deterministically (LONG video only). Mutates chunks in
    place: subject-anchored must_show (<=2 concrete), abstract chunks routed to
    subject B-roll, every query forced to contain the subject."""
    anchor = _derive_topic_anchor(chunks, video_meta)
    if not anchor:
        return  # can't derive a subject — leave the LLM plan untouched
    scenes = [s for s in _strip_abstract(video_meta.get("motif_pool"))
              if anchor not in s][:4]

    def _anchor_queries(qs: list, abstract: bool) -> list[str]:
        out: list[str] = []
        for q in qs or []:
            s = str(q).strip().lower()
            if not s:
                continue
            toks = [w for w in s.split() if w not in _ABSTRACT_TERMS]
            if not toks:
                continue
            s2 = " ".join(toks)
            if anchor not in s2:               # subject lost → re-anchor
                s2 = f"{anchor} {s2}".strip()
            out.append(s2)
        if abstract or not out:                # B-roll ladder on the subject
            out = [f"{anchor} {s}" for s in scenes[:3]] + [anchor]
        seen: set[str] = set()
        return [q for q in out if not (q in seen or seen.add(q))][:5]

    for c in chunks:
        ms = _strip_abstract(c.get("must_show"))[:2]
        abstract = len(ms) == 0
        if abstract:
            ms = [anchor]
        elif not any(anchor in t for t in ms):
            # ensure the recurring subject is present, keep at most one extra
            extra = next((t for t in ms if len(t.split()) <= 2), None)
            ms = [anchor] + ([extra] if extra else [])
        c["must_show"] = ms
        c["queries"] = _anchor_queries(c.get("queries"), abstract)
        c["metaphor_queries"] = _anchor_queries(c.get("metaphor_queries"), abstract) if c.get("metaphor_queries") else []


def _build_global_plan_segmented(
    llm: LLMClient, sentences: list[str], *, user_brief: str = "",
    harden_subject_anchor: bool = True,
) -> dict[str, Any]:
    if not sentences:
        return {
            "video_meta": {"theme": "", "visual_world": "",
                           "global_no_show": [], "motif_pool": DEFAULT_MOTIF_POOL,
                           "motif_pool_source": "default_fallback"},
            "chunks": [],
            "meta": {"version": "segmented_v1", "whole_movie_fallback": False,
                     "chunk_count": 0, "director_degraded": True,
                     "degradation_reasons": ["default_motif_pool"]},
        }
    t0 = time.monotonic()
    segments = build_deterministic_segments(sentences)
    skeleton = build_skeleton_or_fallback(llm, segments, user_brief=user_brief)
    windows = make_segment_aligned_windows(segments)
    batch_results = run_expansion_windows(llm, windows, skeleton, sentences)
    merge = merge_batch_results(sentences, batch_results)
    plan_core = fill_missing_with_local_fallback(
        merge.plan_core, sentences, segments, skeleton,
    )

    vm = skeleton.get("video_meta", {})
    global_no_show = _clean_stock_terms(vm.get("global_no_show"))
    chunks = [
        _normalize_one_chunk(idx, sentences[idx], plan_core[idx], global_no_show)
        for idx in range(len(sentences))
    ]
    # chunks to subject B-roll. LONG video only — for SHORT video this force-prepends
    # the topic anchor onto EVERY query ("coffee excel chart") and forces
    # must_show=[anchor] on abstract chunks (a bad anchor like "empty" then poisons
    # the whole video via the orchestrator hard-anchor guardrail). Short video keeps
    # the concrete per-sentence expand output and does its own abstract-chunk B-roll
    # routing downstream in visual_focus.repair_short_video_plan.
    if harden_subject_anchor:
        _harden_long_chunks(chunks, {
            "motif_pool": _clean_stock_queries(vm.get("motif_pool"))[:6] or DEFAULT_MOTIF_POOL,
        })
    # 绝不禁掉本片主体:把"全片要展示的词"从 must_not_show 里减掉(修"猫片禁猫"导致画面只剩一条重复猫片)。
    _scrub_subject_from_must_not(chunks, {
        "theme": str(vm.get("theme") or ""),
        "motif_pool": _clean_stock_queries(vm.get("motif_pool"))[:6] or DEFAULT_MOTIF_POOL,
    })
    final_validate_or_raise(sentences, chunks)

    fallback_count = sum(1 for c in chunks if c.get("plan_source") == "deterministic_fallback")
    motif_pool_source = str(vm.get("_motif_pool_source") or "default_fallback")
    degradation_reasons: list[str] = []
    if vm.get("_source") == "deterministic":
        degradation_reasons.append("deterministic_skeleton")
    if motif_pool_source == "default_fallback":
        degradation_reasons.append("default_motif_pool")
    if merge.failed_windows:
        degradation_reasons.append("failed_expansion_window")
    if fallback_count:
        degradation_reasons.append("deterministic_chunk_fallback")
    logger.warning(
        "global_director_summary version=segmented_v1 chunks=%d segments=%d "
        "windows=%d failed_windows=%d missing_after_llm=%d invalid=%d "
        "fallback_chunks=%d skeleton=%s duration_ms=%d whole_movie_fallback=False",
        len(sentences), len(segments), len(windows), len(merge.failed_windows),
        len(merge.missing), len(merge.invalid), fallback_count,
        vm.get("_source", "?"), int((time.monotonic() - t0) * 1000),
    )
    return {
        "video_meta": {
            "theme": str(vm.get("theme") or ""),
            "visual_world": str(vm.get("visual_world") or ""),
            "global_no_show": global_no_show,
            "motif_pool": _clean_stock_queries(vm.get("motif_pool"))[:6] or DEFAULT_MOTIF_POOL,
            "motif_pool_source": motif_pool_source,
        },
        "chunks": chunks,
        "meta": {
            "version": "segmented_v1",
            "whole_movie_fallback": False,
            "chunk_count": len(sentences),
            "segment_count": len(segments),
            "window_count": len(windows),
            "failed_window_count": len(merge.failed_windows),
            "fallback_chunk_count": fallback_count,
            "skeleton_source": vm.get("_source", "?"),
            "director_degraded": bool(degradation_reasons),
            "degradation_reasons": degradation_reasons,
        },
    }


def _fallback_plan(
    sentences: list[str], *, allow_whole_movie_fallback: bool = False,
) -> dict[str, Any]:
    # Whole-video fallback (every chunk → generic query) is the OLD accident
    # amplifier. The segmented director never calls this; only the legacy path
    # opts in explicitly. Default-raise guards against accidental reuse.
    if not allow_whole_movie_fallback:
        raise RuntimeError(
            "whole-movie _fallback_plan is disabled; use the segmented "
            "director's per-chunk local fallback instead"
        )
    chunks = []
    for idx, sentence in enumerate(sentences):
        chunks.append({
            "chunk_index": idx,
            "chunk_id": idx,
            "sentence": sentence,
            "text": sentence,
            "sentence_type": "fact",
            "is_abstract": False,
            "visual_anchor": _fallback_visual_anchor(sentence),
            "inherit_prev": False,
            "inherit_reason": "",
            "intent": sentence[:80],
            "must_show": [],
            "must_not_show": ["cartoon", "logo", "watermark", "text overlay"],
            "queries": [_fallback_english_query(sentence)],
            "metaphor_queries": [],
            "metaphor_essence": "",
            "search_query": _fallback_english_query(sentence),
            "search_keywords": [],
            "ai_prompt": "",
            "visual_tags": [],
            "motion_tags": [],
            "shot_type": "atmosphere",
            "is_hero_shot": idx == 0,
            "priority": "high" if idx == 0 else "normal",
            "target_shot_count": 1,
            "atoms": {},
            "cascade_depth": 2,
            "prompts": {},
            "plan_source": "whole_movie_fallback",
        })
    return {
        "video_meta": {
            "theme": "",
            "visual_world": "",
            "global_no_show": [],
            "motif_pool": DEFAULT_MOTIF_POOL,
            "motif_pool_source": "default_fallback",
        },
        "chunks": chunks,
        "meta": {
            "version": "legacy_fallback",
            "whole_movie_fallback": True,
            "director_degraded": True,
            "degradation_reasons": ["whole_movie_fallback", "default_motif_pool"],
        },
    }


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


# 抽象·非视觉词:库存里搜不到的概念词。omni-flash/qwen-plus 偶尔会把它们塞进
# query(如 "queue psychology" / "consumer behavior")。策略=从 query 里**剥掉**这些
# 词、保留具体部分;剥完仍是有效 query(≥2 个有意义词)就用剥后的,否则整条丢。
# 这样既留住模型的镜头创意,又堵住搜不到的废词。env MEDIA_BUDDY_DIRECTOR_STRIP_ABSTRACT=0 关。
_ABSTRACT_NONVISUAL_TERMS = frozenset({
    "psychology", "psychological", "perception", "behavior", "behaviour",
    "behavioral", "mindset", "ritual", "abstract", "conceptual", "awareness",
    "anticipation", "motivation", "subconscious", "intangible", "loyalty",
    "nostalgia", "mentality", "perceived",
})


def _strip_abstract_terms(query: str) -> str:
    kept = [w for w in query.split()
            if w.strip(",.;:").lower() not in _ABSTRACT_NONVISUAL_TERMS]
    return " ".join(kept).strip()


def _clean_stock_queries(value: Any) -> list[str]:
    strip_on = os.environ.get(
        "MEDIA_BUDDY_DIRECTOR_STRIP_ABSTRACT", "1"
    ).strip().lower() not in {"0", "false", "no", "off"}
    out: list[str] = []
    for q in _clean_str_list(value):
        if not _is_english_stock_query(q):
            continue
        if strip_on:
            stripped = _strip_abstract_terms(q)
            if stripped != q:
                # contained an abstract term — keep the salvaged concrete part
                # only if it's still a valid searchable query, else drop it.
                if _is_english_stock_query(stripped):
                    out.append(stripped)
                continue
        out.append(q)
    return out


def _clean_stock_terms(value: Any) -> list[str]:
    return [q for q in _clean_str_list(value) if _is_english_stock_query(q, min_words=1)]


def _is_english_stock_query(value: str, *, min_words: int = 2) -> bool:
    text = str(value or "").strip()
    if not text or not text.isascii():
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)
    return len(words) >= min_words


def _fallback_english_query(sentence: str) -> str:
    # layer still has better theme-level English options if this is too broad.
    text = str(sentence or "")
    if re.search(r"\d", text):
        return "abstract data visualization"
    return "documentary concept visual"


def _dedupe(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _coerce_visual_anchor(raw: dict[str, Any], sentence_type: str) -> bool:
    if "visual_anchor" in raw:
        return bool(raw.get("visual_anchor"))
    # Older / malformed plans did not provide visual_anchor. Be conservative:
    # CTA, transitions, and pure emotion can inherit; factual/example/comparison
    # chunks need their own search.
    if sentence_type in {"cta", "transition", "emotion"}:
        return False
    if _clean_str_list(raw.get("must_show")) or _clean_str_list(raw.get("queries")):
        return True
    return True


def _fallback_visual_anchor(sentence: str) -> bool:
    text = str(sentence or "").strip()
    if not text:
        return False
    lower = text.lower()
    cta_markers = ("评论", "留言", "告诉我", "关注", "收藏", "点赞", "订阅")
    cta_markers_en = ("comment", "subscribe", "follow", "like", "save", "share", "tell me")
    if any(marker in text for marker in cta_markers) or any(marker in lower for marker in cta_markers_en):
        return False
    # Short punctuation-heavy reactions are usually emotional tails. Everything
    # else gets its own search in fallback mode because a missed inheritance is
    # less harmful than carrying the wrong shot across topics.
    compact = re.sub(r"[\s，。！？,.!?；;：:、]+", "", text)
    if len(compact) <= 8 and not re.search(r"\d", compact):
        return False
    return True
