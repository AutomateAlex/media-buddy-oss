"""Deterministic content-safety gate for stock-footage search.

Architecture mirrors ``cultural_filter`` / ``visual_guardrails``: the LLM stays
the *director* of WHAT to search; this is a thin *safety inspector* that

  1. rewrites or drops a search QUERY carrying sexual / violent / gory terms
     **before** it ever reaches a stock provider (``sanitize_query``), and
  2. rejects a returned CANDIDATE whose title/tags/url are sexual / violent /
     gory (``asset_is_unsafe``), so the selector falls through to a clean clip.

This is defense-in-depth on top of each provider's own ``safe_search`` /
``safesearch`` flag, which is imperfect (esp. for borderline/violent imagery and
on topics like colonial exploitation where the topic words themselves are
explicit, e.g. "nude exhibition"). Wired into the single remote-search funnel
``FootageService._safe_search`` so it covers short + long video and every
source. Toggle off with ``MEDIA_BUDDY_CONTENT_SAFETY=0`` (A/B).

Term banks are deliberately HIGH PRECISION — only words that are almost always
genuinely unsafe. Broad terms (war, gun, weapon, knife, fight, blood) are NOT
banned: curated stock libraries carry abundant *safe* b-roll for them and
over-rejection would starve a chunk into a worse fallback. English + Chinese.
"""
from __future__ import annotations

import os
import re
from typing import Optional

# ASCII unsafe terms matched as WHOLE WORDS (\b...\b) so e.g. "naked" does not
# fire inside "snakeskin" and "sex" is not matched bare (false positives).
_ASCII_WORDS = (
    # sexual / pornographic
    "nude", "nudity", "naked", "topless", "erotic", "erotica", "porn",
    "pornographic", "pornography", "sexual", "sexually", "explicit",
    "lingerie", "fetish", "seductive", "sexy", "stripper", "striptease",
    "boudoir", "nsfw", "xxx", "bdsm",
    # graphic violence / gore
    "gore", "gory", "bloody", "bloodshed", "bloodbath", "corpse", "corpses",
    "cadaver", "beheading", "beheaded", "massacre", "torture", "tortured",
    "lynching", "mangled",
)

# Substrings matched anywhere (multi-word phrases, English stems, and all CJK —
# Chinese has no word boundaries so substring is correct).
_SUBSTRINGS = (
    "sex tape", "graphic violence", "graphic content", "self-harm",
    "self harm", "mutilat", "dismember", "decapitat", "disembowel",
    # 中文（色情）
    "裸体", "裸露", "全裸", "色情", "情色", "性爱", "床戏", "三点全露",
    # 中文（暴力/血腥）
    "血腥", "血淋淋", "尸体", "尸首", "残肢", "断肢", "斩首", "砍头",
    "酷刑", "凌迟", "屠杀", "自残",
    # 死亡/腐烂的动物（恶心，即使谈不上"血腥"）—— 高精度专用词
    "carcass", "carrion", "roadkill", "road kill", "maggot", "maggots",
    "putrefying", "putrefaction", "rotting carcass",
    "腐尸", "死尸", "动物尸体", "腐烂的尸",
)

# Morbid / dead-animal imagery — visually disgusting to a general audience even
# when it isn't "gory" (e.g. a desiccated dead gecko on a wall). Bare "dead" is
# deliberately NOT banned (Dead Sea / deadline / dead end / dead leaves / dead
# wood); we only fire when a death/decay word is PAIRED with an animal noun
# nearby. Reverse order ("lizard ... dead") is rare in stock titles; skip it.
_MORBID_ANIMAL_RE = re.compile(
    r"\b(dead|decaying|decomposing|decomposed|rotting|rotten|lifeless|deceased)\b"
    r"(?:\W+\w+){0,3}?\W+"
    r"(lizard|gecko|reptile|snake|serpent|animal|animals|creature|creatures|"
    r"bird|birds|fish|frog|toad|rat|rats|mouse|mice|cat|cats|dog|dogs|deer|"
    r"rabbit|fox|wolf|bat|turtle|tortoise|crab|squirrel|raccoon|possum|opossum|"
    r"hedgehog|insect|insects|bug|bugs|cow|pig|sheep|goat|horse|monkey|"
    r"carcass|corpse|remains|roadkill)\b",
    re.IGNORECASE,
)

# Safe phrases that CONTAIN an unsafe word but are themselves fine. Neutralized
# before scanning so they never trip the gate (classic: "naked eye").
_SAFE_PHRASES = (
    "naked eye",
)

_ASCII_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _ASCII_WORDS) + r")\b",
    re.IGNORECASE,
)
_MIN_MEANINGFUL_CHARS = 3
_MEANINGFUL_RE = re.compile(r"[a-z一-鿿]")

# ---------------------------------------------------------------------------
# DRUGS / CANNABIS — compliance-critical. Checked INDEPENDENTLY of the general
# A/B switch above (see ``drug_safety_enabled``): even with
# ``MEDIA_BUDDY_CONTENT_SAFETY=0`` for experiments, drug imagery stays blocked.
# Our audience is primarily in China, where ANY illegal-drug / cannabis / poppy
# imagery is a hard legal line — one such frame shipping is catastrophic. So this
# bank is deliberately MORE aggressive than the banks above and ALSO bans
# borderline botanicals (cannabis / hemp leaves & plants, opium AND decorative
# poppy) — the user's explicit call ("擦边的肯定不生产"). Everyday-ambiguous words
# (weed / pot / grass / joint / high / smoke / hemp-rope) are kept OUT of the
# bare-word list so we don't nuke gardening / cooking / BBQ / textile b-roll;
# they are banned only inside an unmistakable drug/plant PHRASE. The
# observer-judge visual pass is the real net for a clean-named clip that actually
# shows drugs. English + Chinese. Toggle (do NOT, for China): MEDIA_BUDDY_DRUG_SAFETY=0.
_DRUG_ASCII_WORDS = (
    "cannabis", "marijuana", "marihuana", "ganja", "hashish",
    "thc", "lsd", "mdma", "cannabidiol", "tetrahydrocannabinol",
    "cocaine", "heroin", "methamphetamine", "meth", "amphetamine",
    "opioid", "opioids", "opium", "fentanyl", "oxycodone", "oxycontin",
    "ketamine", "psilocybin", "narcotic", "narcotics",
    "bong", "bongs", "spliff", "doobie", "stoner",
)
_DRUG_SUBSTRINGS = (
    # English multi-word / borderline-botanical phrases (bare ambiguous words
    # like weed/pot/hemp/poppy intentionally excluded — only their drug forms).
    "magic mushroom", "shrooms", "drug abuse", "drug addict", "drug overdose",
    "drug paraphernalia", "drug dealer", "drug trafficking", "illegal drug",
    "recreational drug", "rolling paper", "crack cocaine", "crystal meth",
    "meth lab", "cbd oil", "cbd gummies",
    "weed plant", "weed leaf", "cannabis leaf", "cannabis plant",
    "marijuana leaf", "marijuana plant", "pot leaf",
    "hemp leaf", "hemp plant", "hemp field", "hemp bud", "hemp flower",
    "poppy field", "poppy flower", "poppies", "opium poppy", "red poppy",
    "coca leaf", "coca plant",
    "smoking weed", "smoking pot", "bong hit", "snorting cocaine",
    "injecting drug", "drug injection",
    # 中文（毒品 / 大麻 / 罂粟 / 器具）
    "大麻", "毒品", "冰毒", "海洛因", "可卡因", "鸦片", "罂粟", "罂粟花",
    "摇头丸", "吸毒", "贩毒", "毒贩", "注射毒品", "麻醉品", "毒瘾",
    "大麻叶", "大麻植株", "大麻田", "大麻二酚", "芬太尼", "甲基苯丙胺",
    "氯胺酮", "致幻剂", "迷幻蘑菇", "K粉", "麻古",
)
_DRUG_ASCII_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _DRUG_ASCII_WORDS) + r")\b",
    re.IGNORECASE,
)


def content_safety_enabled() -> bool:
    """Master switch. ``MEDIA_BUDDY_CONTENT_SAFETY=0`` disables the gate."""
    return os.environ.get("MEDIA_BUDDY_CONTENT_SAFETY", "1") != "0"


def drug_safety_enabled() -> bool:
    """Drug/cannabis gate. INDEPENDENT of ``content_safety_enabled`` — drugs are
    blocked even when the general A/B switch is off. ``MEDIA_BUDDY_DRUG_SAFETY=0``
    disables it, which must NEVER be done for the China market."""
    return os.environ.get("MEDIA_BUDDY_DRUG_SAFETY", "1") != "0"


def _neutralize_safe_phrases(text: str) -> str:
    out = text
    for sp in _SAFE_PHRASES:
        out = out.replace(sp, " ")
    return out


def _first_unsafe(text: str) -> Optional[str]:
    """Return the first unsafe term found in ``text`` (lowercased), else None."""
    if not text:
        return None
    scan = _neutralize_safe_phrases(text.lower())
    for tok in _SUBSTRINGS:
        if tok in scan:
            return tok
    m = _ASCII_RE.search(scan)
    if m:
        return m.group(1).lower()
    m2 = _MORBID_ANIMAL_RE.search(scan)
    if m2:
        return f"dead-animal:{m2.group(1).lower()}-{m2.group(2).lower()}"
    return None


def _first_drug(text: str) -> Optional[str]:
    """Return the first drug/cannabis term found in ``text`` (lowercased), else
    None. Kept separate from ``_first_unsafe`` so drug detection can run under its
    own always-on gate regardless of the general content-safety switch."""
    if not text:
        return None
    scan = text.lower()
    for tok in _DRUG_SUBSTRINGS:
        if tok in scan:
            return tok
    m = _DRUG_ASCII_RE.search(scan)
    if m:
        return m.group(1).lower()
    return None


def sanitize_query(query: str) -> tuple[str, bool]:
    """Strip unsafe terms from a stock-search ``query`` before it goes out.

    Returns ``(safe_query, changed)``. If removing the unsafe terms leaves too
    little to search on, ``safe_query`` is ``""`` (caller should skip this
    query — other queries / metaphor fallbacks still cover the chunk).
    """
    if not query:
        return query, False
    drug_on = drug_safety_enabled()
    general_on = content_safety_enabled()
    drug_hit = _first_drug(query) if drug_on else None
    general_hit = _first_unsafe(query) if general_on else None
    if drug_hit is None and general_hit is None:
        return query, False
    cleaned = _neutralize_safe_phrases(query)
    # Drugs first (own gate), then substring/CJK, ASCII whole-word, dead-animal.
    if drug_on:
        for tok in _DRUG_SUBSTRINGS:
            cleaned = re.sub(re.escape(tok), " ", cleaned, flags=re.IGNORECASE)
        cleaned = _DRUG_ASCII_RE.sub(" ", cleaned)
    if general_on:
        for tok in _SUBSTRINGS:
            cleaned = re.sub(re.escape(tok), " ", cleaned, flags=re.IGNORECASE)
        cleaned = _ASCII_RE.sub(" ", cleaned)
        cleaned = _MORBID_ANIMAL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-_")
    meaningful = "".join(_MEANINGFUL_RE.findall(cleaned.lower()))
    if len(meaningful) < _MIN_MEANINGFUL_CHARS:
        return "", True
    return cleaned, True


def asset_is_unsafe(source_tags: str, source_url: str = "") -> Optional[str]:
    """Return a short reason (``unsafe:<term>``) if a candidate's metadata is
    sexual / violent / gory, else ``None``. Used as a selection-time backstop
    for clips that slipped past provider safe-search or came from cache/library.
    """
    meta = f"{source_tags or ''} {source_url or ''}"
    if drug_safety_enabled():
        drug_hit = _first_drug(meta)
        if drug_hit:
            return f"unsafe:drug:{drug_hit}"
    if content_safety_enabled():
        hit = _first_unsafe(meta)
        if hit:
            return f"unsafe:{hit}"
    return None
