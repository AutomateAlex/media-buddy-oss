"""Media Buddy v2 literal stock probe.

This layer spends no LLM calls. It searches the stock sources, applies a
negative metadata filter, and returns a small ordered candidate list for the
observer-judge. The first query that produces survivors wins.
"""
from __future__ import annotations

import re
import logging
import os
from typing import Any

from backend.services.footage_service import FootageCandidate, FootageService

logger = logging.getLogger(__name__)


def reset_probe_search_cache(footage: FootageService) -> None:
    """Reset per-run stock search cache.

    The cache stores raw provider search results, not final selections. Candidate
    filtering still runs per chunk with the latest used_source_ids, so this
    speeds up repeated queries without increasing adjacent reuse.
    """
    setattr(footage, "_mb_probe_search_cache", {})
    setattr(footage, "_mb_probe_search_cache_stats", {"hits": 0, "misses": 0})


def probe_search_cache_stats(footage: FootageService) -> dict[str, int]:
    stats = getattr(footage, "_mb_probe_search_cache_stats", None)
    if isinstance(stats, dict):
        return {
            "hits": int(stats.get("hits", 0) or 0),
            "misses": int(stats.get("misses", 0) or 0),
        }
    return {"hits": 0, "misses": 0}


def _cached_secondary_search(
    footage: FootageService,
    query: str,
    orientation: str,
    *,
    kind: str,
    limit: int,
) -> list[FootageCandidate]:
    return _cached_provider_search(
        footage, "secondary", query, orientation, kind=kind, limit=limit,
        fetch=lambda: footage.search_secondary(
            query, orientation, kind=kind, limit=limit,
        ),
    )


def _cached_provider_search(
    footage: FootageService,
    provider: str,
    query: str,
    orientation: str,
    *,
    kind: str,
    limit: int,
    fetch,
) -> list[FootageCandidate]:
    cache = getattr(footage, "_mb_probe_search_cache", None)
    if not isinstance(cache, dict):
        reset_probe_search_cache(footage)
        cache = getattr(footage, "_mb_probe_search_cache", {})
    stats = getattr(footage, "_mb_probe_search_cache_stats", None)
    if not isinstance(stats, dict):
        stats = {"hits": 0, "misses": 0}
        setattr(footage, "_mb_probe_search_cache_stats", stats)
    key = (
        provider,
        _normalize_search_cache_query(query),
        str(orientation or "").lower(),
        str(kind or "").lower(),
        int(limit or 0),
    )
    if key in cache:
        stats["hits"] = int(stats.get("hits", 0) or 0) + 1
        logger.info("v2_search_cache_hit provider=%s query=%r", provider, query)
        return list(cache[key])
    stats["misses"] = int(stats.get("misses", 0) or 0) + 1
    results = list(fetch() or [])
    cache[key] = results
    return list(results)


def _normalize_search_cache_query(query: Any) -> str:
    text = " ".join(str(query or "").strip().lower().split())
    text = re.sub(r"\b(stock|footage|video|clip|scene|shot|b[- ]?roll)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or " ".join(str(query or "").strip().lower().split())


def probe_literal_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
    top_n: int,
    include_secondary: bool = True,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    queries = _queries(chunk_plan.get("queries"))
    must_not = _terms(chunk_plan.get("must_not_show"))
    must_show = _terms(chunk_plan.get("must_show"))
    title_pool_size = _title_pool_size()
    for query in queries:
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(candidates, must_not, must_show, used_source_ids, chunk_plan)
        _log_probe_stats("literal", query, candidates, survivors, top_n)
        if survivors:
            return survivors[:top_n]
        if include_secondary:
            secondary = _cached_secondary_search(
                footage,
                query,
                orientation,
                kind="video",
                limit=max(title_pool_size, top_n, 3),
            )
            survivors = filter_candidates(secondary, must_not, must_show, used_source_ids, chunk_plan)
            _log_probe_stats("literal_secondary", query, secondary, survivors, top_n)
            if survivors:
                return survivors[:top_n]
    return []


def probe_library_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
    top_n: int,
    motif_pool: list[str] | None = None,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    """Search local clips before any remote stock API.

    The library is free and already downloadable. We still pass candidates to
    observer_judge, but a local accept avoids stock search/download
    entirely for that sentence.
    """
    must_not = _terms(chunk_plan.get("must_not_show"))
    must_show = _terms(chunk_plan.get("must_show"))
    title_pool_size = _title_pool_size()
    seen: set[str] = set()
    query_groups = [
        ("library_literal", _queries(chunk_plan.get("queries")), must_show),
        ("library_metaphor", _queries(chunk_plan.get("metaphor_queries")), []),
        ("library_motif", _queries(motif_pool or []), []),
    ]
    for mode, queries, required_terms in query_groups:
        for query in queries:
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates = footage.search_library(
                query,
                orientation,
                limit=max(title_pool_size, top_n, 3),
            )
            survivors = filter_candidates(
                candidates, must_not, required_terms, used_source_ids, chunk_plan,
            )
            _log_probe_stats(mode, query, candidates, survivors, top_n)
            if survivors:
                return survivors[:top_n]
    return []


def probe_metaphor_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
    top_n: int,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    for query in _queries(chunk_plan.get("metaphor_queries")):
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(candidates, must_not, [], used_source_ids, chunk_plan)
        _log_probe_stats("metaphor", query, candidates, survivors, top_n)
        if survivors:
            return survivors[:top_n]
    return []


def probe_similar_subject_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
    top_n: int,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    """Find a looser same-subject substitute when exact stock is unavailable.

    This is still online stock only. It intentionally keeps one concrete anchor
    term when possible, e.g. "different types of bamboo" -> "bamboo close up".
    """
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    anchors = _similar_anchor_terms(chunk_plan)
    if not anchors:
        return []
    chunk_plan.setdefault("_similar_rescue_queries", {})["subject"] = []
    for query in _similar_subject_queries(anchors):
        chunk_plan["_similar_rescue_queries"]["subject"].append(query)
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(
            candidates, must_not, anchors[:2], used_source_ids, chunk_plan,
            title_gate_mode="similar",
        )
        _log_probe_stats("similar_subject", query, candidates, survivors, top_n)
        if survivors:
            return survivors[:top_n]
        secondary = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(
            secondary, must_not, anchors[:2], used_source_ids, chunk_plan,
            title_gate_mode="similar",
        )
        _log_probe_stats("similar_subject_secondary", query, secondary, survivors, top_n)
        if survivors:
            return survivors[:top_n]
    return []


def probe_broadened_subject_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    broadened_queries: list[str],
    *,
    orientation: str,
    top_n: int,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    """Search LLM-broadened generic queries when the exact niche subject's stock
    pool collided or was empty (e.g. 'kangaroo rat' -> 'desert rodent').

    Online stock only, no extra LLM here — the broadened query strings are
    produced upstream (orchestrator via LLMClient.broaden_subject_for_stock) and
    passed in. Lenient same-category title gate, since a broadened category is a
    deliberate substitute. The first query that yields survivors wins.
    """
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    rescue = chunk_plan.setdefault("_similar_rescue_queries", {})
    rescue["broadened"] = []
    for query in _dedupe_queries([q for q in (broadened_queries or []) if q]):
        rescue["broadened"].append(query)
        anchors = [w for w in re.findall(r"[A-Za-z]+", query) if len(w) > 2][:2]
        for searcher, label in (
            (_cached_secondary_search, "broadened_subject"),
            (_cached_secondary_search, "broadened_subject_secondary"),
        ):
            candidates = searcher(
                footage, query, orientation, kind="video",
                limit=max(title_pool_size, top_n, 3),
            )
            survivors = filter_candidates(
                candidates, must_not, anchors, used_source_ids, chunk_plan,
                title_gate_mode="similar",
            )
            _log_probe_stats(label, query, candidates, survivors, top_n)
            if survivors:
                return survivors[:top_n]
    return []


def probe_similar_category_candidates(
    footage: FootageService,
    chunk_plan: dict[str, Any],
    motif_pool: list[str] | None = None,
    *,
    orientation: str,
    top_n: int,
    used_source_ids: set[str] | None = None,
) -> list[FootageCandidate]:
    """Find a broad same-category substitute for low-specificity chunks."""
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    chunk_plan.setdefault("_similar_rescue_queries", {})["category"] = []
    for query in _similar_category_queries(chunk_plan, motif_pool or []):
        chunk_plan["_similar_rescue_queries"]["category"].append(query)
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(
            candidates, must_not, [], used_source_ids, chunk_plan,
            title_gate_mode="similar",
        )
        _log_probe_stats("similar_category", query, candidates, survivors, top_n)
        if survivors:
            return survivors[:top_n]
        secondary = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, top_n, 3),
        )
        survivors = filter_candidates(
            secondary, must_not, [], used_source_ids, chunk_plan,
            title_gate_mode="similar",
        )
        _log_probe_stats("similar_category_secondary", query, secondary, survivors, top_n)
        if survivors:
            return survivors[:top_n]
    return []


def probe_motif_candidate(
    footage: FootageService,
    motif_pool: list[str],
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
    used_source_ids: set[str] | None = None,
) -> FootageCandidate | None:
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    for query in _queries(motif_pool):
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, 2),
        )
        survivors = filter_candidates(candidates, must_not, [], used_source_ids, chunk_plan)
        _log_probe_stats("motif", query, candidates, survivors, 1)
        if survivors:
            return survivors[0]
    return None


def probe_motif_candidate_lenient(
    footage: FootageService,
    motif_pool: list[str],
    chunk_plan: dict[str, Any],
    *,
    orientation: str,
) -> FootageCandidate | None:
    """

    Same theme-fitted motif_pool queries as ``probe_motif_candidate`` but with
    the three gates that starved chunk 146 RELAXED, so a long video can ALWAYS
    fill an otherwise-unmatched chunk with a clip that still fits the whole
    video's background (e.g. african-savanna footage for a black-mamba video):
      - ignore ``used_source_ids`` dedup (reusing a motif clip is fine — better
        than a dead pipeline; the orchestrator only reaches here when nothing
        else matched)
      - ``title_gate_mode="similar"`` (the loosest existing gate — passes
        unless the title is clearly off-topic)
      - still honor ``must_not_show`` (never show forbidden content)

    If the gated pass still finds nothing, a final ungated pass returns the raw
    first search hit (only must_not_show enforced), because shipping a
    theme-fitted environment clip ALWAYS beats a dead pipeline.

    Used only by the orchestrator's Pass-2 "video must ship" fallback, never on
    the normal path, so the strict ``probe_motif_candidate`` behavior is intact.
    """
    must_not = _terms(chunk_plan.get("must_not_show"))
    title_pool_size = _title_pool_size()
    for query in _queries(motif_pool):
        candidates = _cached_secondary_search(
            footage,
            query,
            orientation,
            kind="video",
            limit=max(title_pool_size, 2),
        )
        survivors = filter_candidates(
            candidates, must_not, [],
            used_source_ids=None,        # ignore dedup — reuse is acceptable here
            chunk_plan=chunk_plan,
            title_gate_mode="similar",   # loosest existing gate
        )
        _log_probe_stats("motif_lenient", query, candidates, survivors, 1)
        if survivors:
            return survivors[0]
    # Final ungated pass: theme clip beats a dead pipeline. Only must_not_show.
    for query in _queries(motif_pool):
        candidates = _cached_secondary_search(
            footage, query, orientation, kind="video", limit=max(title_pool_size, 2),
        )
        for cand in candidates:
            meta = " ".join([cand.source_tags or "", cand.source_url or ""]).lower()
            if any(term and term in meta for term in must_not):
                continue
            _log_probe_stats("motif_ungated", query, candidates, [cand], 1)
            return cand
    return None


def filter_candidates(
    candidates: list[FootageCandidate],
    must_not: list[str],
    must_show: list[str],
    used_source_ids: set[str] | None = None,
    chunk_plan: dict[str, Any] | None = None,
    *,
    title_gate_mode: str = "strict",
) -> list[FootageCandidate]:
    out = []
    used = set(used_source_ids or set())
    # If this video is about a concrete subject (animal/dish/vehicle/...), reject
    # clips whose tags name a different member of that group (computed once).
    subject_keys = _subject_keys(chunk_plan or {}, must_show)
    for cand in candidates:
        source_key = f"{cand.source}:{cand.source_id}"
        if cand.source_id and (cand.source_id in used or source_key in used):
            continue
        meta = " ".join([
            cand.source_tags or "",
            cand.source_url or "",
        ]).lower()
        if _is_off_topic_decorative_stock(meta, chunk_plan or {}):
            continue
        if not _title_gate_allows(cand, chunk_plan or {}, mode=title_gate_mode):
            continue
        if any(term and term in meta for term in must_not):
            continue
        # Subject-mismatch gate (general rule): a video about one concrete subject
        # must not use clips whose tags name a different member of the same group.
        # Pexels/Pixabay return off-subject results for niche queries (a "wild
        # boar" search yields "gray wolf, predator" clips); nothing else on the
        # long-video v2 path catches it.
        if subject_keys:
            reason = _subject_mismatch(meta, subject_keys)
            if reason:
                logger.info(
                    "literal_probe subject gate reject %s:%s — %s",
                    cand.source, cand.source_id, reason,
                )
                continue
        overlap = sum(1 for term in must_show if term and term in meta)
        title_score = _title_gate_score(cand, chunk_plan or {})
        out.append((overlap + title_score, cand))
    out.sort(key=lambda pair: (-pair[0], _source_rank(pair[1])))
    return [cand for _, cand in out]


def _source_rank(candidate: FootageCandidate) -> int:
    if candidate.source == "library":
        return 0
    return 1


def _similar_anchor_terms(chunk_plan: dict[str, Any]) -> list[str]:
    raw_terms: list[str] = []
    for value in (
        chunk_plan.get("must_show"),
        chunk_plan.get("queries"),
        chunk_plan.get("metaphor_queries"),
    ):
        values = value if isinstance(value, list) else [value]
        for item in values:
            raw_terms.extend(re.findall(r"[A-Za-z][A-Za-z-]{2,}", str(item or "").lower()))
    stop = _TITLE_STOPWORDS | {
        "different", "various", "several", "multiple", "types", "type",
        "kinds", "kind", "varieties", "variety", "select", "selecting",
        "choose", "choosing", "chosen", "show", "showing", "using", "used",
        "make", "making", "made", "process", "concept", "visual",
    }
    anchors: list[str] = []
    for term in raw_terms:
        term = term.strip("-")
        if term in stop or len(term) < 3:
            continue
        if term.endswith("ies") and len(term) > 4:
            term = f"{term[:-3]}y"
        elif term.endswith("s") and len(term) > 4:
            term = term[:-1]
        if term not in anchors:
            anchors.append(term)
    return anchors[:3]


def _similar_subject_queries(anchors: list[str]) -> list[str]:
    subject = anchors[0]
    queries = [
        _expand_single_word_query(subject),
        f"{subject} close up",
        f"{subject} detail",
        f"{subject} natural background",
    ]
    if subject in {"bamboo", "tree", "plant", "leaf", "flower", "wood"}:
        queries.extend([
            f"{subject} forest",
            f"{subject} plants",
            f"green {subject}",
        ])
    if len(anchors) > 1:
        queries.append(" ".join(anchors[:2]))
    return _dedupe_queries(queries)


def _similar_category_queries(
    chunk_plan: dict[str, Any],
    motif_pool: list[str],
) -> list[str]:
    anchors = _similar_anchor_terms(chunk_plan)
    joined = " ".join(anchors)
    queries: list[str] = []
    if any(t in joined for t in ("bamboo", "plant", "tree", "leaf", "wood", "forest")):
        queries.extend([
            "plants close up",
            "green plants nature",
            "natural materials close up",
        ])
    if any(t in joined for t in ("cook", "food", "kitchen", "onion", "soup", "recipe")):
        queries.extend([
            "cooking ingredients close up",
            "kitchen food preparation",
            "fresh ingredients close up",
        ])
    if any(t in joined for t in ("data", "network", "technology", "computer", "internet")):
        queries.extend([
            "technology network animation",
            "data connections abstract",
            "digital network background",
        ])
    if any(t in joined for t in ("math", "geometry", "fractal", "dimension", "line", "curve")):
        queries.extend([
            "geometric pattern animation",
            "abstract math visualization",
            "lines and shapes animation",
        ])
    queries.extend(_queries(motif_pool)[:3])
    return _dedupe_queries(queries)


def _dedupe_queries(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        q = _normalize_stock_query(value)
        key = q.lower()
        if key in seen or not _is_english_stock_query(q):
            continue
        seen.add(key)
        out.append(q)
    return out[:8]


def _queries(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for raw in value:
            query = _normalize_stock_query(raw)
            if _is_english_stock_query(query):
                out.append(query)
        return out
    if isinstance(value, str) and value.strip():
        query = _normalize_stock_query(value)
        return [query] if _is_english_stock_query(query) else []
    return []


def _normalize_stock_query(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text or not text.isascii():
        return text
    if len(re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)) < 2:
        return _expand_single_word_query(text)
    return text


def _expand_single_word_query(value: Any) -> str:
    word = " ".join(str(value or "").strip().lower().split())
    if not word or not word.isascii():
        return str(value or "").strip()
    specific = {
        "wolf": "wolf wildlife",
        "wolves": "wolf wildlife",
        "human": "human activity",
        "people": "people outdoors",
        "animal": "wild animal",
        "animals": "wild animals",
        "food": "food preparation",
        "cooking": "cooking ingredients",
        "forest": "forest landscape",
        "snowy": "snowy landscape",
        "snow": "snowy landscape",
        "river": "river landscape",
        "mountain": "mountain landscape",
        "nature": "nature landscape",
        "plant": "plant close up",
        "plants": "plants close up",
    }
    return specific.get(word, f"{word} footage")


def _is_english_stock_query(value: str) -> bool:
    text = str(value or "").strip()
    if not text or not text.isascii():
        logger.warning("v2_stock_query_rejected_non_english query=%r", text[:120])
        return False
    if len(re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)) < 2:
        logger.warning("v2_stock_query_rejected_too_short query=%r", text[:120])
        return False
    return True


def _title_pool_size() -> int:
    try:
        return max(3, int(os.environ.get("MEDIA_BUDDY_V2_TITLE_POOL_SIZE", "10")))
    except ValueError:
        return 10


def _log_probe_stats(
    mode: str,
    query: str,
    candidates: list[FootageCandidate],
    survivors: list[FootageCandidate],
    top_n: int,
) -> None:
    logger.info(
        "v2_title_probe mode=%s query=%r returned=%d title_survivors=%d to_gemini=%d",
        mode, query, len(candidates), len(survivors), min(len(survivors), top_n),
    )


def _terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]


_DECORATIVE_FLOWER_TERMS = {
    "floral", "flower", "flowers", "petal", "petals", "blossom",
    "bloom", "blooming", "daisy", "daisies",
}

_TITLE_STOPWORDS = {
    "the", "and", "with", "from", "into", "onto", "over", "under",
    "video", "animation", "animated", "background", "loop", "seamless",
    "abstract", "motion", "graphic", "effect", "transition", "channel",
    "alpha", "stock", "footage", "view", "shot", "scene", "slow", "fast",
}

_CLEARLY_OFF_TOPIC_TITLE_TERMS = {
    "wedding", "bride", "groom", "party", "birthday", "christmas",
    "coffee", "latte", "cappuccino", "mug", "cup", "drink", "beverage",
    "restaurant", "pizza", "burger", "food", "cooking",
    "dog", "cat", "puppy", "kitten", "car", "traffic", "office",
    "business", "meeting", "handshake", "shopping", "fashion", "makeup",
    "beauty", "beach", "vacation", "hotel", "sports", "football",
    "basketball", "concert", "dance", "rocket", "moon", "lunar",
    "rover", "robot", "exploration", "instruments",
    "software", "timeline", "cursor", "editing", "editor", "adjustment",
    "playhead", "interface", "dashboard",
} | _DECORATIVE_FLOWER_TERMS

_FOOD_PREPARATION_TITLE_TERMS = {
    "food", "foods", "cooking", "cook", "cooked", "kitchen", "recipe",
    "restaurant", "pizza", "burger", "ingredient", "ingredients",
    "preparation", "meal", "dish", "chef", "pan", "sauce",
}

_GENERIC_HUMAN_ACTIVITY_TITLE_TERMS = {
    "city", "urban", "street", "park", "building", "buildings",
    "office", "business", "meeting", "people", "person", "man", "woman",
    "walking", "tourist", "tourists", "travel", "vacation", "beach",
    "fitness", "workout", "exercise", "sports",
}

_WILDLIFE_TITLE_TERMS = {
    "wildlife", "wild", "animal", "animals", "deer", "wolf", "wolves",
    "snake", "bird", "forest", "savanna", "jungle", "nature", "predator",
    "prey", "herd", "habitat",
}

# GENERAL subject-mismatch rule (not animal-specific): a video about one concrete
# subject must not use footage whose tags name a DIFFERENT member of the same
# "mutually-exclusive concept group". Each group below maps aliases -> a canonical
# (so "wolves"/"gray wolf" -> wolf, "fried rice"/"炒饭" stay distinct from "pizza").
# The gate logic is one generic engine; supporting a new domain = add a group here,
# no code change. Adding a group is how this becomes a rule rather than a hack.
_SUBJECT_GROUPS: list[dict[str, str]] = [
    # --- animals ---
    {
        "boar": "boar", "boars": "boar", "wild boar": "boar", "pig": "boar",
        "pigs": "boar", "swine": "boar", "hog": "boar", "hogs": "boar", "warthog": "boar",
        "wolf": "wolf", "wolves": "wolf", "gray wolf": "wolf", "grey wolf": "wolf",
        "european wolf": "wolf", "eurasian wolf": "wolf", "coyote": "wolf",
        "lion": "lion", "lions": "lion", "lioness": "lion",
        "tiger": "tiger", "tigers": "tiger",
        "leopard": "leopard", "cheetah": "cheetah", "jaguar": "jaguar", "panther": "leopard",
        "bear": "bear", "bears": "bear", "grizzly": "bear", "panda": "panda",
        "deer": "deer", "elk": "deer", "moose": "deer", "stag": "deer", "reindeer": "deer",
        "fox": "fox", "foxes": "fox",
        "rabbit": "rabbit", "rabbits": "rabbit", "hare": "rabbit",
        "elephant": "elephant", "elephants": "elephant",
        "rhino": "rhino", "rhinoceros": "rhino", "hippo": "hippo", "hippopotamus": "hippo",
        "giraffe": "giraffe", "zebra": "zebra",
        "horse": "horse", "horses": "horse",
        "cow": "cow", "cattle": "cow", "bull": "cow", "buffalo": "buffalo", "bison": "bison",
        "sheep": "sheep", "goat": "goat", "goats": "goat",
        "dog": "dog", "dogs": "dog", "puppy": "dog",
        "cat": "cat", "cats": "cat", "kitten": "cat",
        "monkey": "monkey", "ape": "ape", "gorilla": "gorilla",
        "chimp": "ape", "chimpanzee": "ape", "orangutan": "ape",
        "kangaroo": "kangaroo", "koala": "koala",
        "shark": "shark", "sharks": "shark", "whale": "whale", "dolphin": "dolphin",
        "seal": "seal", "otter": "otter", "walrus": "walrus",
        "snake": "snake", "snakes": "snake", "cobra": "snake", "python": "snake",
        "viper": "snake", "mamba": "snake", "serpent": "snake",
        "crocodile": "crocodile", "alligator": "crocodile",
        "lizard": "lizard", "gecko": "lizard", "iguana": "lizard", "chameleon": "chameleon",
        "eagle": "eagle", "hawk": "eagle", "falcon": "eagle", "owl": "owl",
        "penguin": "penguin", "ostrich": "ostrich", "flamingo": "flamingo",
        "crow": "crow", "raven": "crow", "hummingbird": "hummingbird", "albatross": "albatross",
        "bat": "bat", "bats": "bat",
        "octopus": "octopus", "squid": "squid", "cuttlefish": "squid",
        "jellyfish": "jellyfish", "crab": "crab",
        "bee": "bee", "bees": "bee", "ant": "ant", "ants": "ant",
        "spider": "spider", "spiders": "spider", "mantis": "mantis",
        "butterfly": "butterfly", "scorpion": "scorpion",
        "sloth": "sloth", "hyena": "hyena", "hyaena": "hyena", "wolverine": "wolverine",
        "pangolin": "pangolin", "hedgehog": "hedgehog", "raccoon": "raccoon",
        "skunk": "skunk", "badger": "badger",
        "frog": "frog", "toad": "frog", "turtle": "turtle", "tortoise": "turtle",
        "firefly": "firefly", "centipede": "centipede",
        "cockroach": "cockroach", "roach": "cockroach",
    },
    # --- prepared dishes / foods ---
    {
        "pizza": "pizza", "burger": "burger", "hamburger": "burger",
        "sushi": "sushi", "sashimi": "sushi", "ramen": "ramen", "noodle": "noodle",
        "noodles": "noodle", "pasta": "pasta", "spaghetti": "pasta",
        "fried rice": "fried rice", "rice": "rice", "dumpling": "dumpling",
        "dumplings": "dumpling", "steak": "steak", "soup": "soup", "salad": "salad",
        "sandwich": "sandwich", "taco": "taco", "curry": "curry", "cake": "cake",
        "bread": "bread", "pancake": "pancake", "egg": "egg", "eggs": "egg",
    },
    # --- vehicles ---
    {
        "car": "car", "cars": "car", "truck": "truck", "bus": "bus", "train": "train",
        "airplane": "airplane", "aeroplane": "airplane", "plane": "airplane",
        "jet": "airplane", "helicopter": "helicopter", "motorcycle": "motorcycle",
        "motorbike": "motorcycle", "bicycle": "bicycle", "bike": "bicycle",
        "ship": "ship", "boat": "boat", "submarine": "submarine", "tank": "tank",
    },
    # --- musical instruments ---
    {
        "piano": "piano", "guitar": "guitar", "violin": "violin", "cello": "cello",
        "drum": "drum", "drums": "drum", "flute": "flute", "saxophone": "saxophone",
        "trumpet": "trumpet", "harp": "harp", "accordion": "accordion",
    },
    # --- sports ---
    {
        "football": "football", "soccer": "soccer", "basketball": "basketball",
        "tennis": "tennis", "golf": "golf", "baseball": "baseball",
        "hockey": "hockey", "rugby": "rugby", "boxing": "boxing", "cricket": "cricket",
    },
]
# Per group, split multi-word phrases (substring match) from single words (token match).
_GROUP_LOOKUP: list[tuple[dict[str, str], dict[str, str]]] = [
    ({k: v for k, v in g.items() if " " in k}, {k: v for k, v in g.items() if " " not in k})
    for g in _SUBJECT_GROUPS
]


def _subjects_in(text: str) -> set[tuple[int, str]]:
    """Concrete subjects named in text, as {(group_index, canonical)}. Single-word
    aliases match on token boundaries so "ant" does not fire inside "plant"."""
    if not text:
        return set()
    low = text.lower()
    tokens = set(re.findall(r"[a-z]+", low))
    found: set[tuple[int, str]] = set()
    for gi, (multi, single) in enumerate(_GROUP_LOOKUP):
        for phrase, canon in multi.items():
            if phrase in low:
                found.add((gi, canon))
        for tok in tokens:
            canon = single.get(tok)
            if canon:
                found.add((gi, canon))
    return found


def _subject_keys(chunk_plan: dict[str, Any], must_show: list[str]) -> set[tuple[int, str]]:
    """What concrete subject(s) this video is about: the video-level topic_anchor,
    the chunk's must_show, AND its literal queries. The queries matter because
    topic_anchor/must_show can be empty on abstract/transition chunks while the
    query still names the subject ("wild boar abandoned site") — without them the
    gate goes inert and off-subject clips (wolf) slip through on those chunks.
    Empty only when the subject is genuinely abstract (then the gate stays off)."""
    policy = chunk_plan.get("long_video_visual_policy") or {}
    parts = [str(policy.get("topic_anchor") or "")]
    parts.extend(str(t) for t in (must_show or []))
    parts.extend(str(t) for t in (chunk_plan.get("must_show") or []))
    parts.extend(str(t) for t in (chunk_plan.get("queries") or []))
    return _subjects_in(" ".join(parts))


def _subject_mismatch(meta: str, subject_keys: set[tuple[int, str]]) -> str:
    """Return a reason string if the candidate's tags name a DIFFERENT member of a
    concept group the video is about (e.g. wolf clip in a boar video), else ""."""
    cand_keys = _subjects_in(meta)
    if not cand_keys:
        return ""
    for gi in {g for g, _ in subject_keys}:
        subj_members = {c for g, c in subject_keys if g == gi}
        cand_members = {c for g, c in cand_keys if g == gi}
        if cand_members and not (cand_members & subj_members):
            return f"clip {sorted(cand_members)} != subject {sorted(subj_members)}"
    return ""

_BRIDGE_GROUPS = [
    {"fractal", "recursive", "recursion", "self", "similarity", "pattern", "branch", "branches", "tree", "leaf", "vein", "network", "spiral", "river", "lightning", "crack", "snowflake", "koch", "peano"},
    {"dimension", "dimensional", "layer", "layers", "space", "geometry", "geometric", "grid", "line", "curve", "fold", "folding", "origami"},
    {"universe", "cosmos", "cosmic", "galaxy", "nebula", "stars", "space"},
    {"earth", "planet", "globe", "global", "world", "international", "connectivity", "connection", "connections", "network", "networks", "data", "exchange", "communication", "communications", "digital", "futuristic", "surrounding", "orbit", "orbital", "line", "lines"},
    {"lung", "lungs", "bronchi", "bronchial", "medical", "vascular", "blood", "vessel", "vessels", "branch", "branches", "network"},
]

_CONTEXTUAL_OFF_TOPIC_GROUPS = [
    (
        {"lung", "lungs", "alveoli", "bronchi", "bronchial", "medical"},
        {"tree", "trees", "treetop", "forest", "leaves", "leaf", "sky", "sun"},
        {"tree", "trees", "branch", "branches", "leaf", "leaves", "forest"},
    ),
    (
        {"earth", "planet", "globe", "global", "world", "orbit", "circumference"},
        {"blood", "vessel", "vessels", "vascular", "neuron", "brain", "cell", "anatomy", "medical"},
        {"blood", "vessel", "vessels", "vascular", "neuron", "brain", "cell", "anatomy", "medical"},
    ),
]


def _is_off_topic_decorative_stock(meta: str, chunk_plan: dict[str, Any]) -> bool:
    """Reject common decorative overlay clips before spending judge calls.

    Stock sources often return "floral petal wipe" motion graphics for broad
    abstract queries. These can look repetitive, but they are not a valid
    mathematical/science metaphor unless the sentence explicitly asks for
    flowers/plants.
    """
    if not meta:
        return False
    if not any(term in meta for term in _DECORATIVE_FLOWER_TERMS):
        return False
    text = " ".join([
        str(chunk_plan.get("sentence") or chunk_plan.get("text") or ""),
        str(chunk_plan.get("intent") or ""),
        " ".join(str(v) for v in (chunk_plan.get("must_show") or [])),
        " ".join(str(v) for v in (chunk_plan.get("queries") or [])),
        " ".join(str(v) for v in (chunk_plan.get("metaphor_queries") or [])),
    ]).lower()
    flower_allowed = any(term in text for term in {
        "flower", "flowers", "floral", "petal", "blossom", "daisy",
        "plant", "plants",
        "花", "花瓣", "开花", "植物",
    })
    return not flower_allowed


def _title_gate_allows(
    candidate: FootageCandidate,
    chunk_plan: dict[str, Any],
    *,
    mode: str = "strict",
) -> bool:
    """Cheap semantic gate based on provider title/tags only.

    This is intentionally looser than the visual judge: it only rejects titles
    that are clearly unrelated. If the title is plausible or is a known visual
    metaphor, Gemini can still inspect the image.
    """
    title_tokens = _candidate_title_tokens(candidate)
    if not title_tokens:
        return True
    desired_tokens = _desired_tokens(chunk_plan)
    if not desired_tokens:
        return True

    visual_focus = str((chunk_plan or {}).get("visual_focus") or "").strip()
    if visual_focus != "human_impact" and _looks_like_wildlife_or_ecology(chunk_plan):
        if title_tokens & _GENERIC_HUMAN_ACTIVITY_TITLE_TERMS:
            return False
    food_or_cooking_topic = _looks_like_food_or_cooking_topic(chunk_plan)
    if not food_or_cooking_topic:
        if title_tokens & _FOOD_PREPARATION_TITLE_TERMS:
            return False
    elif title_tokens & _WILDLIFE_TITLE_TERMS and not (title_tokens & desired_tokens):
        return False

    off_topic_hits = title_tokens & _CLEARLY_OFF_TOPIC_TITLE_TERMS
    if off_topic_hits and not (off_topic_hits & desired_tokens):
        return False

    for desired_group, bad_group, allowed_group in _CONTEXTUAL_OFF_TOPIC_GROUPS:
        if (
            desired_tokens & desired_group
            and title_tokens & bad_group
            and not (desired_tokens & allowed_group)
        ):
            return False

    if title_tokens & desired_tokens:
        return True
    if _shares_bridge_group(title_tokens, desired_tokens):
        return True

    if mode == "similar":
        anchors = set(_similar_anchor_terms(chunk_plan))
        if anchors and title_tokens & anchors:
            return True
        if off_topic_hits:
            return False
        return True

    if off_topic_hits:
        return False

    # For science/math chunks, a generic decorative title is usually bad even
    # when it does not hit the explicit off-topic list.
    scienceish = {
        "fractal", "dimension", "dimensional", "geometry", "curve", "line",
        "universe", "cosmos", "math", "mathematical", "peano", "koch",
        "lung", "bronchi", "branch", "tree", "earth", "planet", "global",
        "network", "connection", "connectivity", "data",
    }
    generic_decor = {
        "overlay", "wipe", "intro", "logo", "banner", "celebration",
        "confetti", "sparkle", "glitter", "bokeh", "particle", "particles",
    }
    if desired_tokens & scienceish and title_tokens & generic_decor:
        return False
    return True


def _title_gate_score(candidate: FootageCandidate, chunk_plan: dict[str, Any]) -> int:
    title_tokens = _candidate_title_tokens(candidate)
    desired_tokens = _desired_tokens(chunk_plan)
    if title_tokens & desired_tokens:
        return 2
    if _shares_bridge_group(title_tokens, desired_tokens):
        return 1
    return 0


def _candidate_title_tokens(candidate: FootageCandidate) -> set[str]:
    raw = str(candidate.source_tags or "").lower()
    query = str(candidate.query or "").lower().strip()
    if query:
        raw = raw.replace(query, " ")
    return _tokenize(raw) - _TITLE_STOPWORDS


def _desired_tokens(chunk_plan: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for key in ("sentence", "text", "intent", "metaphor_essence"):
        values.append(str(chunk_plan.get(key) or ""))
    for key in ("must_show", "queries", "metaphor_queries"):
        raw = chunk_plan.get(key)
        if isinstance(raw, list):
            values.extend(str(v) for v in raw)
        elif raw:
            values.append(str(raw))
    joined = " ".join(values)
    tokens = _tokenize(joined) - _TITLE_STOPWORDS
    if _looks_like_ecology_food_chain_text(joined):
        tokens -= {
            "food", "foods", "cooking", "cook", "kitchen", "recipe",
            "ingredient", "ingredients", "preparation",
        }
        tokens.update({"prey", "predator", "wildlife", "ecosystem"})
    return tokens


def _shares_bridge_group(title_tokens: set[str], desired_tokens: set[str]) -> bool:
    for group in _BRIDGE_GROUPS:
        if title_tokens & group and desired_tokens & group:
            return True
    return False


def _looks_like_ecology_food_chain_text(text: str) -> bool:
    lower = str(text or "").lower()
    return any(term in lower for term in {
        "food chain", "food web", "predator prey", "predator-prey",
        "prey relationship", "trophic",
        "ecosystem", "ecology", "wildlife", "食物链", "生态链", "捕食",
    })


def _chunk_context_text(chunk_plan: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("sentence", "text", "intent", "metaphor_essence", "visual_focus"):
        values.append(str((chunk_plan or {}).get(key) or ""))
    for key in ("must_show", "queries", "metaphor_queries"):
        raw = (chunk_plan or {}).get(key)
        if isinstance(raw, list):
            values.extend(str(v) for v in raw)
        elif raw:
            values.append(str(raw))
    return " ".join(values).lower()


def _looks_like_food_or_cooking_topic(chunk_plan: dict[str, Any]) -> bool:
    text = _chunk_context_text(chunk_plan)
    if _looks_like_ecology_food_chain_text(text):
        return False
    return any(term in text for term in {
        "recipe", "cooking", "cook", "kitchen", "chef", "ingredient",
        "ingredients", "dish", "meal", "restaurant", "food preparation",
        "食谱", "做菜", "烹饪", "厨房", "食材", "菜谱",
    })


def _looks_like_wildlife_or_ecology(chunk_plan: dict[str, Any]) -> bool:
    text = _chunk_context_text(chunk_plan)
    if str((chunk_plan or {}).get("visual_focus") or "") in {
        "topic_subject", "habitat", "consequence", "prey_or_livestock", "abstract_broll",
    }:
        return True
    return any(term in text for term in {
        "wildlife", "animal", "animals", "wolf", "wolves", "snake", "forest",
        "nature", "ecosystem", "ecology", "prey", "predator", "habitat",
        "野生", "动物", "狼", "蛇", "森林", "自然", "生态", "栖息地",
    })


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
        if token not in _TITLE_STOPWORDS
    }
