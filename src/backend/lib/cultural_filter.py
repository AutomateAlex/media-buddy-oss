"""Cultural relevance filter for stock footage candidates.

Background: the orchestrator used to rely entirely on Pexels search + a
single mm_critic score. When the script was clearly about France but the
LLM-generated query was generic ("dining table", "food close-up"), Pexels
returned Asian / Chinese street food clips, the critic gave them 8.5
("main subject is food, subject matches"), and the user got a French-
themed video full of Nanjing street footage.

This module enforces the rule from media-buddy-rules.md §3 (Cultural
Gate): before any candidate enters scoring, reject it if its
title/description contains keywords from the opposing culture's blacklist.

Two-tier model:
- BLACKLIST per culture: hard reject any candidate hitting these in
  title/description. Includes opposing-culture markers AND wrong-language
  scripts (Chinese characters in a French video → reject).
- WHITELIST per culture: candidates hitting these get a boost (return
  is_preferred=True). The orchestrator can use this to re-rank.

Cultures recognized:
  France, China, Japan, Korea, Italy, Spain, USA, UK,
  Generic-Western, Universal.

"Universal" (sunset, abstract textures, generic nature) means no filter
applies — all candidates pass cultural gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Opposing-culture keywords per script culture. Hit any of these in
# title or description (case-insensitive substring) → hard reject.
# Keep lists tight: each entry is a HARD signal of a different culture,
# not a vague association.
_BLACKLISTS: dict[str, list[str]] = {
    "France": [
        # Chinese culture
        "china", "chinese", "中国", "中式", "中華", "華人",
        "nanjing", "shanghai", "beijing", "guangzhou", "chengdu",
        "hangzhou", "chongqing", "shenzhen", "xi'an", "tianjin",
        "南京", "上海", "北京", "广州", "成都", "杭州", "重庆",
        "yangtze", "扬子江", "长江", "黄浦", "外滩",
        "chinese new year", "spring festival", "春节", "中秋", "端午",
        "mandarin", "cantonese", "putonghua",
        "dim sum", "kung pao", "mapo", "hotpot", "wok",
        "rickshaw", "panda",
        # Japanese / Korean (often confused as "Asian")
        "japan", "japanese", "tokyo", "kyoto", "osaka", "sakura",
        "kimono", "zen garden", "geisha", "samurai", "ninja",
        "ramen", "sushi", "sashimi",
        "korea", "korean", "seoul", "hanbok", "k-pop", "kimchi",
        # Other clear non-French markers
        "thailand", "thai", "vietnam", "vietnamese", "phở", "pho",
        "india", "indian", "bollywood", "curry house",
        "mexican", "tacos", "mariachi",
        # Asian generic
        "asian street food", "asian market", "asian temple",
        "buddhist temple", "shrine",
    ],
    "China": [
        "french", "france", "paris", "eiffel", "champs-élysées",
        "baguette", "croissant", "boeuf bourguignon", "escargot",
        "italian", "italy", "rome", "venice", "pasta", "pizza napoli",
        "japanese", "tokyo", "kyoto", "sushi", "sashimi",
        "indian", "bollywood",
    ],
    "Japan": [
        "chinese", "china", "中国", "nanjing", "beijing",
        "french", "france", "paris",
        "italian", "italy", "rome",
        "korean", "seoul", "hanbok",
    ],
    "Korea": [
        "japanese", "tokyo", "kyoto", "sushi", "kimono",
        "chinese", "china", "beijing",
        "french", "france",
    ],
    "Italy": [
        "french", "france", "paris", "baguette", "croissant",
        "chinese", "china", "wok",
        "japanese", "sushi",
    ],
    "Spain": [
        "french", "france", "paris",
        "chinese", "china",
        "italian", "italy", "pasta",
    ],
    "USA": [
        "chinese", "china", "中国",
        "japanese", "tokyo",
        # USA-themed videos shouldn't be filtered against European unless
        # explicit. Conservative list.
    ],
    "UK": [
        "chinese", "china", "中国",
        "japanese", "tokyo",
    ],
    "Generic-Western": [
        # Catches "European/American" themed videos where Asian content
        # would jar. Tighter than per-country lists.
        "chinese", "china", "中国", "中式", "南京", "北京", "上海",
        "japanese", "tokyo", "kyoto", "sushi", "kimono",
        "korean", "seoul", "hanbok",
        "thai", "thailand", "indian curry",
    ],
}


# Preferred keywords per culture. A candidate hitting any of these is
# marked is_preferred=True so orchestrator can prioritize.
_WHITELISTS: dict[str, list[str]] = {
    "France": [
        "french", "france", "paris", "parisian", "provence", "lyon",
        "bordeaux", "marseille", "nice", "champagne", "normandy",
        "café", "cafe", "brasserie", "bistro", "boulangerie",
        "patisserie", "fromagerie",
        "baguette", "croissant", "pain au chocolat", "macaron",
        "escargot", "boeuf bourguignon", "ratatouille", "cassoulet",
        "crème brûlée", "tarte tatin",
        "eiffel tower", "louvre", "montmartre", "champs-élysées",
        "seine", "notre-dame", "versailles",
        "french cuisine", "french restaurant", "french wine",
        "bordeaux wine", "vineyard",
    ],
    "China": [
        "chinese", "china", "中国", "中式",
        "beijing", "shanghai", "guangzhou", "chengdu",
        "great wall", "forbidden city", "terracotta",
        "chinese cuisine", "dim sum", "noodles", "dumplings",
        "hotpot", "kung pao", "mapo tofu",
    ],
    "Japan": [
        "japanese", "japan", "tokyo", "kyoto", "osaka", "hokkaido",
        "sakura", "cherry blossom", "kimono", "zen garden",
        "sushi", "sashimi", "ramen", "tempura", "japanese cuisine",
    ],
    "Korea": [
        "korean", "korea", "seoul", "busan",
        "hanbok", "k-drama", "k-pop",
        "kimchi", "bibimbap", "korean bbq", "korean cuisine",
    ],
    "Italy": [
        "italian", "italy", "rome", "florence", "venice", "milan",
        "tuscany", "naples", "amalfi", "sicily",
        "pasta", "pizza", "risotto", "tiramisu", "espresso",
        "italian cuisine", "italian restaurant",
    ],
    "Spain": [
        "spanish", "spain", "madrid", "barcelona", "seville",
        "tapas", "paella", "sangria", "flamenco",
    ],
    "USA": [
        "american", "usa", "new york", "los angeles", "chicago",
        "california", "texas", "manhattan", "brooklyn",
    ],
    "UK": [
        "british", "uk", "england", "london", "scotland",
        "edinburgh", "british cuisine", "fish and chips",
    ],
    "Generic-Western": [
        "european", "western", "mediterranean",
        "french", "italian", "spanish", "british", "american",
    ],
}


_VALID_CULTURES = frozenset({
    "France", "China", "Japan", "Korea", "Italy", "Spain",
    "USA", "UK", "Generic-Western", "Universal",
})


def normalize_culture(value: str | None) -> str:
    """Snap LLM-emitted culture string to one of _VALID_CULTURES.

    Empty / unknown → 'Universal' so we never accidentally apply the
    wrong country's blacklist."""
    if not value:
        return "Universal"
    v = value.strip()
    # Direct match
    if v in _VALID_CULTURES:
        return v
    # Common aliases
    lowered = v.lower()
    aliases = {
        "french": "France", "francais": "France", "français": "France",
        "fr": "France", "paris": "France",
        "chinese": "China", "cn": "China", "prc": "China",
        "japanese": "Japan", "jp": "Japan",
        "korean": "Korea", "kr": "Korea",
        "italian": "Italy", "it": "Italy",
        "spanish": "Spain", "es": "Spain",
        "american": "USA", "us": "USA", "united states": "USA",
        "british": "UK", "england": "UK", "united kingdom": "UK",
        "western": "Generic-Western", "europe": "Generic-Western",
        "european": "Generic-Western",
        "any": "Universal", "none": "Universal", "global": "Universal",
        "neutral": "Universal", "generic": "Universal",
    }
    if lowered in aliases:
        return aliases[lowered]
    return "Universal"


@dataclass
class CulturalVerdict:
    """Result of cultural_check for one candidate."""
    blocked: bool
    is_preferred: bool
    reason: str
    matched_blacklist: list[str]
    matched_whitelist: list[str]


def cultural_check(
    *,
    culture: str,
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> CulturalVerdict:
    """Decide whether a candidate is culturally appropriate.

    Args:
      culture: Script culture (one of _VALID_CULTURES). 'Universal' →
        no filter, always passes.
      title: Pexels (or other source) video title.
      description: Free-text description if available.
      tags: Source-supplied tag list.

    Returns:
      CulturalVerdict. ``blocked=True`` means hard reject (any blacklist
      hit). ``is_preferred=True`` means at least one whitelist hit; the
      orchestrator can use this to re-rank candidates within a query.
    """
    culture = normalize_culture(culture)
    if culture == "Universal":
        return CulturalVerdict(
            blocked=False, is_preferred=False,
            reason="universal — no culture filter applied",
            matched_blacklist=[], matched_whitelist=[],
        )

    haystack = " ".join(
        s.lower() for s in (title, description, *(tags or [])) if s
    )
    if not haystack.strip():
        # No text to judge → don't block, but also don't prefer.
        return CulturalVerdict(
            blocked=False, is_preferred=False,
            reason="no text metadata — neutral pass",
            matched_blacklist=[], matched_whitelist=[],
        )

    blacklist = _BLACKLISTS.get(culture, [])
    whitelist = _WHITELISTS.get(culture, [])

    matched_b = [kw for kw in blacklist if kw.lower() in haystack]
    matched_w = [kw for kw in whitelist if kw.lower() in haystack]

    if matched_b:
        return CulturalVerdict(
            blocked=True, is_preferred=False,
            reason=f"blacklist hit for {culture}: {matched_b[:3]}",
            matched_blacklist=matched_b, matched_whitelist=matched_w,
        )

    return CulturalVerdict(
        blocked=False,
        is_preferred=bool(matched_w),
        reason=(
            f"whitelist hit for {culture}: {matched_w[:3]}"
            if matched_w else "neutral — no blacklist or whitelist hits"
        ),
        matched_blacklist=[], matched_whitelist=matched_w,
    )


def cultural_anchor_words(culture: str) -> list[str]:
    """Return 1-3 canonical anchor words to prepend to a Pexels query
    so the search itself is culturally biased.

    Used by llm_client.plan_chunks to enforce that EVERY search_query
    starts with a cultural anchor when the script has a definite culture.
    """
    culture = normalize_culture(culture)
    anchors = {
        "France": ["French"],
        "China": ["Chinese"],
        "Japan": ["Japanese"],
        "Korea": ["Korean"],
        "Italy": ["Italian"],
        "Spain": ["Spanish"],
        "USA": ["American"],
        "UK": ["British"],
        "Generic-Western": ["European"],
        "Universal": [],
    }
    return anchors.get(culture, [])


def has_cultural_anchor(query: str, culture: str) -> bool:
    """True if `query` already contains a cultural anchor word for the
    given `culture`. Used as a defensive check in plan_chunks parsing —
    if the LLM didn't include one we inject it."""
    if not query:
        return False
    culture = normalize_culture(culture)
    if culture == "Universal":
        return True  # nothing to enforce
    qlower = query.lower()
    anchors = _WHITELISTS.get(culture, [])
    return any(a.lower() in qlower for a in anchors[:20])


def inject_cultural_anchor(query: str, culture: str) -> str:
    """If `query` lacks a cultural anchor, prepend the canonical one.

    Idempotent: queries already containing an anchor are returned
    unchanged. Universal culture → no-op.
    """
    if not query or not query.strip():
        return query
    if has_cultural_anchor(query, culture):
        return query
    anchors = cultural_anchor_words(culture)
    if not anchors:
        return query
    return f"{anchors[0]} {query.strip()}"


# Public API surface used by tests + diagnostics
__all__ = [
    "cultural_check",
    "CulturalVerdict",
    "normalize_culture",
    "cultural_anchor_words",
    "has_cultural_anchor",
    "inject_cultural_anchor",
]
