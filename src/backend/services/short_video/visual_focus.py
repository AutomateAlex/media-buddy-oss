"""Short-video visual intent repair for the v2 stock pipeline.

This module mirrors the *principle* of the long-video visual-focus repair but
keeps short-video rules isolated. Short videos need fast, low-cost planning:
decide what each sentence should show, keep search queries concrete and
English-only, and avoid turning every sentence into the same subject search.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from collections import Counter
from typing import Any

from backend.services.global_director import is_default_motif_pool


logger = logging.getLogger(__name__)


POOL_FIRST_FOCUS = {"metaphor_broll", "transition"}


def _content_domain_packs_enabled() -> bool:
    """Whether the hardcoded content-domain rule packs (Mayan / Mesopotamia /
    maritime / weather …) are allowed to act as the *director* of footage
    queries.

    OFF by default for short-video v2 domain repair: Step 0 proved the LLM
    (global_director) already emits perfect concrete English queries per sentence
    (e.g. a Roman Pantheon script → "Pantheon Rome interior dome sunlight", a
    coffee-couple script → "hand picking coffee beans"). The old rule packs were
    *overriding* that correct output — forcing wrong-civilization searches
    (Roman → "Mayan ruins / jungle temple") and regenerating concrete queries into
    motif b-roll ("hand picking coffee beans" → "coffee cooking"). With this OFF
    the LLM queries are kept verbatim and the rule packs survive only as an
    emergency fallback for chunks where the LLM gave nothing. Set
    ``ENABLE_CONTENT_DOMAIN_PACKS=1`` to restore the legacy rule-pack director.

    NOTE: this default was briefly flipped to True, which silently routed every
    chunk through rule-pack regeneration and destroyed the director's concrete
    queries (only topics with a hardcoded ``_retail_queries_for_sentence`` mapper
    survived). Restored to False = LLM-as-director, per the architecture mandate.
    """
    raw = os.environ.get("ENABLE_CONTENT_DOMAIN_PACKS")
    if raw is None:
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _anchor_from_director_enabled() -> bool:
    """topic_anchor 是否优先取"导演(LLM)已产出的英文 queries/must_show"。

    根因修复(浣熊被当成熊):旧逻辑先用静态中文表 SUBJECT_PHRASES 对 user_brief
    做纯子串匹配,"北美浣熊"命中单字"熊"→锚点=bear,判官/motif 兜底/FLUX 全被污染,
    哪怕导演 22/22 镜头的搜索词都是正确的 raccoon。新逻辑:锚点从 LLM 已经理解
    对了的英文产出里提取,静态中文表降级为最后兜底(且最长词优先)。

    默认开;设 MB_ANCHOR_FROM_DIRECTOR=0 回退旧逻辑。
    """
    raw = os.environ.get("MB_ANCHOR_FROM_DIRECTOR")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}

ABSTRACT_TERMS = {
    "truth", "meaning", "fear", "memory", "emotion", "feeling", "idea",
    "concept", "balance", "mystery", "secret", "question", "answer",
    "future", "past", "change", "choice", "risk", "value", "success",
    "failure", "knowledge", "wisdom", "culture", "history",
}

GENERIC_BAD_ANCHORS = {
    "background", "b roll", "b-roll", "broll", "clip", "clips", "close shot",
    "close up", "concept", "concept visual", "documentary",
    "documentary concept visual", "documentary footage", "footage", "meaning",
    "natural scene", "scene", "shot", "stock", "stock footage", "video",
    "visual", "wildlife", "wildlife nature", "animal", "animals", "nature",
    "habitat", "natural habitat", "landscape", "wide landscape", "atmosphere",
    "atmospheric b roll", "abstract background", "ancient history",
    "ancient historical scene", "data visualization", "modern city park",
    "empty footage", "empty close shot", "empty natural scene",
    "empty environment", "empty natural background",
}

STATE_NON_ANCHOR_WORDS = {
    # Visible states/modifiers are useful inside a concrete query ("empty shelf
    # space"), but they are not the story subject. If promoted to hard anchors
    # they poison the whole video by rejecting every non-empty/non-cheap/etc.
    # candidate.
    "available", "blank", "busy", "cheap", "daily", "empty", "expired",
    "expiration", "expiry", "expensive", "filled", "full", "left", "leftover",
    "long", "new", "old", "open", "closed", "ordinary", "remaining", "short",
    "space", "spare", "temporary", "unused", "vacant",
}

GENERIC_BAD_WORDS = {
    "abstract", "animal", "animals", "atmosphere", "atmospheric",
    "background", "broll", "clip", "clips", "close", "concept",
    "documentary", "footage", "natural", "scene", "shot", "stock", "video",
    "visual", "wildlife", "nature", "habitat", "landscape", "wide",
} | STATE_NON_ANCHOR_WORDS

QUERY_STOPWORDS = {
    "about", "after", "again", "also", "because", "before", "being",
    "between", "could", "every", "first", "from", "have", "into",
    "just", "like", "make", "many", "more", "most", "only", "other",
    "show", "some", "that", "their", "there", "these", "this", "those",
    "through", "video", "what", "when", "where", "which", "while",
    "with", "would", "your",
}

SUBJECT_PHRASES = [
    # Chinese-to-English stock-search anchors. These are a generic fallback
    # for degenerate plans, not per-video special cases.
    ("\u4fbf\u5229\u5e97", "convenience store shelf"),
    ("\u8d85\u5e02", "supermarket shelf"),
    ("\u8d27\u67b6", "convenience store shelf"),
    ("\u51b0\u67dc", "refrigerated display case"),
    ("\u53ef\u4e50", "cola bottles"),
    ("\u9178\u5976", "yogurt cups"),
    ("\u9c9c\u5976", "milk bottles"),
    ("\u4e34\u671f\u98df\u54c1", "near expiry food shelf"),
    ("\u4e34\u671f", "near expiry food shelf"),
    ("\u4fdd\u8d28\u671f", "expiry date label"),
    ("\u9ec4\u6807", "yellow discount tag"),
    ("\u756a\u8304\u7092\u86cb", "tomato scrambled eggs"),
    ("\u897f\u7ea2\u67ff\u7092\u9e21\u86cb", "tomato scrambled eggs"),
    ("\u4eba\u5de5\u667a\u80fd", "artificial intelligence"),
    ("\u79e6\u59cb\u7687", "Qin dynasty emperor"),
    ("\u53e4\u7f57\u9a6c", "ancient Rome"),
    ("\u53e4\u57c3\u53ca", "ancient Egypt"),
    ("\u9ed1\u6d1e", "black hole"),
    ("\u94f6\u6cb3", "galaxy"),
    ("\u661f\u7cfb", "galaxy"),
    ("\u706b\u661f", "Mars planet"),
    ("\u6708\u7403", "moon"),
    ("\u884c\u661f", "planet"),
    ("\u6052\u661f", "star"),
    ("\u5b87\u5b99", "outer space"),
    ("\u706b\u5c71", "volcano"),
    ("\u5730\u9707", "earthquake"),
    ("\u6d77\u5578", "tsunami"),
    ("\u98d3\u98ce", "hurricane"),
    ("\u9f99\u5377\u98ce", "tornado"),
    ("\u5bd2\u6f6e", "cold wave weather"),
    ("\u6781\u5bd2", "extreme cold weather"),
    ("\u51b7\u7a7a\u6c14", "cold air outbreak"),
    ("\u5317\u6781\u53d8\u6696", "Arctic warming climate change"),
    ("\u5317\u6781", "Arctic ice landscape"),
    ("\u6c14\u6e29\u9aa4\u964d", "temperature drop cold wave"),
    ("\u964d\u96ea", "snowfall"),
    ("\u66b4\u96ea", "blizzard"),
    ("\u98ce\u66b4", "winter storm"),
    ("\u6781\u7aef\u5929\u6c14", "extreme weather"),
    ("\u6c14\u8c61", "weather system"),
    ("\u6c14\u5019", "climate weather"),
    ("\u5929\u6c14", "weather"),
    ("\u6d77\u6d0b\u6e29\u5ea6", "ocean temperature climate"),
    ("\u6c99\u6f20", "desert"),
    ("\u51b0\u5ddd", "glacier"),
    ("\u82af\u7247", "computer chip"),
    ("\u673a\u5668\u4eba", "robot"),
    ("\u4e92\u8054\u7f51", "internet network"),
    ("\u6570\u636e", "data visualization"),
    ("\u54c1\u724c", "brand"),
    ("\u4ea7\u54c1", "product"),
    ("\u7f8e\u98df", "cooking"),
    ("\u98df\u8c31", "cooking"),
    ("\u505a\u83dc", "cooking"),
    ("\u70f9\u996a", "cooking"),
    ("\u725b\u6392", "steak"),
    ("\u5496\u5561", "coffee"),
    ("\u9762\u5305", "bread"),
    ("\u5386\u53f2", "ancient history"),
    ("\u53e4\u4ee3", "ancient history"),
    ("\u90d1\u548c", "ancient Chinese maritime voyage"),
    ("\u4e0b\u897f\u6d0b", "ancient Chinese maritime voyage"),
    ("\u8239\u961f", "sailing fleet"),
    ("\u5e06\u8239", "wooden sailing ship"),
    ("\u822a\u6d77", "ocean voyage"),
    ("\u6e2f\u53e3", "historic harbor"),
    ("\u6c34\u624b", "sailors on ship"),
    ("\u5546\u4eba", "historic trade port"),
    ("\u6d77\u7981", "ocean blockade"),
    ("\u8fdc\u822a", "ocean voyage"),
    ("\u739b\u96c5", "Mayan ruins"),
    ("\u9057\u8ff9", "ancient ruins"),
    ("\u795e\u5e99", "ancient temple"),
    ("\u8003\u53e4", "archaeological site"),
    ("\u77f3\u523b", "stone carving"),
    ("\u53e4\u6587\u660e", "ancient civilization ruins"),
    ("\u82cf\u7f8e\u5c14", "ancient Mesopotamian civilization"),
    ("\u7f8e\u7d22\u4e0d\u8fbe\u7c73\u4e9a", "ancient Mesopotamian civilization"),
    ("\u4e24\u6cb3\u6d41\u57df", "ancient Mesopotamian civilization"),
    ("\u6954\u5f62\u6587\u5b57", "cuneiform clay tablet"),
    ("\u6ce5\u677f", "cuneiform clay tablet"),
    ("\u4e4c\u9c81\u514b", "ancient Mesopotamian city ruins"),
    ("\u4e4c\u5c14", "ancient Mesopotamian city ruins"),
    ("sumer", "ancient Mesopotamian civilization"),
    ("sumerian", "ancient Mesopotamian civilization"),
    ("mesopotamia", "ancient Mesopotamian civilization"),
    ("mesopotamian", "ancient Mesopotamian civilization"),
    ("cuneiform", "cuneiform clay tablet"),
    ("clay tablet", "cuneiform clay tablet"),
    ("ziggurat", "ancient ziggurat"),
    ("cold wave", "cold wave weather"),
    ("cold snap", "cold wave weather"),
    ("extreme cold", "extreme cold weather"),
    ("cold air outbreak", "cold air outbreak"),
    ("arctic warming", "Arctic warming climate change"),
    ("winter storm", "winter storm"),
    ("snowstorm", "snowstorm"),
    ("blizzard", "blizzard"),
    ("snowfall", "snowfall"),
    ("climate change", "climate weather"),
    ("ocean temperature", "ocean temperature climate"),
    ("\u57ce\u5821", "castle"),
    ("\u9ed1\u66fc\u5df4", "black mamba snake"),
    ("\u91ce\u732a", "wild boar"),
    ("\u8d64\u72d0", "fox"),
    ("\u72d0\u72f8", "fox"),
    ("\u4e4c\u9e26", "raven"),
    ("\u767d\u8681", "termite"),
    ("\u7af9\u5b50", "bamboo"),
    ("\u72fc", "wolf"),
    ("\u86c7", "snake"),
    ("\u718a", "bear"),
    ("\u9e7f", "deer"),
    ("\u72ee\u5b50", "lion"),
    ("\u8001\u864e", "tiger"),
    ("\u730e\u8c79", "cheetah"),
    ("\u8c79\u5b50", "leopard"),
    ("\u5927\u8c61", "elephant"),
    ("\u9ca8\u9c7c", "shark"),
    ("\u9cb8\u9c7c", "whale"),
    ("\u6d77\u8c5a", "dolphin"),
    ("\u7ae0\u9c7c", "octopus"),
    ("\u9cc4\u9c7c", "crocodile"),
    ("\u4f01\u9e45", "penguin"),
    ("\u732b\u5934\u9e70", "owl"),
    ("\u8001\u9e70", "eagle"),
    ("\u871c\u8702", "bee"),
    ("\u8682\u8681", "ant"),
    ("\u8718\u86db", "spider"),
    ("\u8759\u8760", "bat"),
    # \u9ad8\u5371\u590d\u5408\u8bcd\u515c\u5e95(\u540d\u5b57\u91cc\u542b"\u718a/\u9a6c"\u7b49\u5355\u5b57\u3001\u66fe\u88ab\u77ed\u8bcd\u52ab\u6301\u7684\u7269\u79cd)\u3002
    # \u6ce8\u610f:\u5fc5\u987b\u653e\u5728\u4e0a\u9762\u5355\u5b57\u6761\u76ee\u4e4b\u540e \u2014\u2014 \u65e7\u903b\u8f91(MB_ANCHOR_FROM_DIRECTOR=0)\u6309\u8868\u5e8f
    # \u8ba1\u6570\u3001\u5e73\u7968\u53d6\u5148\u63d2\u5165\u8005,\u65b0\u6761\u76ee\u653e\u540e\u9762\u624d\u4e0d\u6539\u53d8\u65e7\u884c\u4e3a;\u65b0\u903b\u8f91\u6309\u8bcd\u957f\u964d\u5e8f\u5339\u914d,
    # \u4e0e\u8868\u5185\u4f4d\u7f6e\u65e0\u5173\u3002
    ("\u6d63\u718a", "raccoon"),  # \u6d63\u718a
    ("\u5c0f\u718a\u732b", "red panda"),  # \u5c0f\u718a\u732b
    ("\u718a\u732b", "giant panda"),  # \u718a\u732b(\u9632\u5355\u5b57"\u718a"\u52ab\u6301,\u540c\u7c7b\u95ee\u9898\u4e00\u5e76\u5835\u4e0a)
    ("\u718a\u8702", "bumblebee"),  # \u718a\u8702
    ("\u6d77\u9a6c", "seahorse"),  # \u6d77\u9a6c
    ("raccoon", "raccoon"),
    ("red panda", "red panda"),
    ("giant panda", "giant panda"),
    ("bumblebee", "bumblebee"),
    ("seahorse", "seahorse"),
    ("wild boar", "wild boar"),
    ("boar", "wild boar"),
    ("foxes", "fox"),
    ("fox", "fox"),
    ("raven", "raven"),
    ("ravens", "raven"),
    ("crow", "raven"),
    ("crows", "raven"),
    ("wolf", "wolf"),
    ("wolves", "wolf"),
    ("black mamba", "black mamba snake"),
    ("snake", "snake"),
    ("termite", "termite"),
    ("bamboo", "bamboo"),
    ("fractal", "fractal pattern"),
    ("dimension", "geometric dimension"),
    ("recipe", "cooking"),
    ("food", "food"),
    ("dish", "food"),
    ("brand", "brand"),
    ("product", "product"),
    ("ai", "artificial intelligence"),
]

ANIMAL_ANCHORS = {
    "ant", "bat", "bear", "bee", "black mamba snake", "bumblebee",
    "cheetah", "crocodile", "deer", "dolphin", "eagle", "elephant",
    "fox", "giant panda", "leopard", "lion", "octopus", "owl",
    "penguin", "raccoon", "raven", "red panda", "seahorse", "shark",
    "snake", "spider", "termite", "tiger", "whale", "wild boar", "wolf",
}

FINANCE_DOMAIN_MARKERS = {
    "money", "finance", "financial", "investment", "investing", "inflation",
    "bank", "banking", "debt", "income", "salary", "budget", "saving",
    "stock market", "fund", "consumer", "business", "company", "economy",
    "\u91d1\u94b1", "\u8d22\u5546", "\u91d1\u878d", "\u6295\u8d44", "\u901a\u8d27\u81a8\u80c0", "\u901a\u80c0",
    "\u94f6\u884c", "\u503a\u52a1", "\u8d1f\u503a", "\u6536\u5165", "\u5de5\u8d44", "\u9884\u7b97", "\u50a8\u84c4",
    "\u80a1\u5e02", "\u80a1\u7968", "\u57fa\u91d1", "\u6d88\u8d39", "\u5546\u4e1a", "\u516c\u53f8", "\u7ecf\u6d4e",
}

BIOGRAPHY_PRIORITY_MARKERS = {
    "biography", "life story", "life journey", "personal story", "career story",
    "childhood", "\u4eba\u7269", "\u4f20\u8bb0", "\u4e00\u751f", "\u4eba\u751f", "\u751f\u5e73", "\u6210\u957f\u7ecf\u5386",
    "\u804c\u4e1a\u751f\u6daf", "\u4eba\u7269\u6545\u4e8b",
}

BIOGRAPHY_DOMAIN_MARKERS = BIOGRAPHY_PRIORITY_MARKERS | {
    "founder", "entrepreneur", "scientist", "inventor", "artist", "actor",
    "leader", "career", "\u521b\u59cb\u4eba", "\u4f01\u4e1a\u5bb6", "\u79d1\u5b66\u5bb6", "\u53d1\u660e\u5bb6",
    "\u827a\u672f\u5bb6", "\u6f14\u5458", "\u9886\u5bfc\u8005",
}

FOCUS_KEYWORDS = {
    "action_or_process": {
        "cook": "cooking process",
        "cooking": "cooking process",
        "boil": "boiling food",
        "fry": "frying food",
        "cut": "cutting ingredients",
        "slice": "slicing ingredients",
        "mix": "mixing ingredients",
        "make": "making process",
        "build": "building process",
        "grow": "growing process",
        "hunt": "wild animal hunting",
        "run": "running movement",
        "move": "movement",
        "\u70f9\u996a": "cooking process",
        "\u505a\u83dc": "cooking process",
        "\u7092": "cooking process",
        "\u714e": "frying food",
        "\u716e": "boiling food",
        "\u5207": "cutting ingredients",
        "\u6df7\u5408": "mixing ingredients",
        "\u5236\u4f5c": "making process",
        "\u751f\u957f": "growing process",
        "\u72e9\u730e": "wild animal hunting",
        "\u6355\u730e": "wild animal hunting",
        "\u6355\u98df": "predator hunting prey",
        "\u63a5\u8fd1\u730e\u7269": "predator stalking prey",
        "\u9003\u751f": "escape route",
        "\u5954\u8dd1": "running movement",
        "\u79fb\u52a8": "movement",
        "\u5b66\u4e60": "learning behavior",
        "\u6a21\u4eff": "animal learning behavior",
    },
    "context_scene": {
        "forest": "forest landscape",
        "jungle": "jungle landscape",
        "mountain": "mountain landscape",
        "river": "river landscape",
        "ocean": "ocean landscape",
        "city": "city street",
        "street": "city street",
        "kitchen": "kitchen",
        "restaurant": "restaurant kitchen",
        "farm": "farm landscape",
        "space": "outer space",
        "universe": "space universe",
        "\u68ee\u6797": "forest landscape",
        "\u4e1b\u6797": "jungle landscape",
        "\u5c71": "mountain landscape",
        "\u6cb3": "river landscape",
        "\u6d77\u6d0b": "ocean landscape",
        "\u57ce\u5e02": "city street",
        "\u8857\u9053": "city street",
        "\u53a8\u623f": "kitchen",
        "\u9910\u5385": "restaurant kitchen",
        "\u519c\u573a": "farm landscape",
        "\u8349\u539f": "grassland landscape",
        "\u6c99\u6f20": "desert landscape",
        "\u96ea\u5730": "snow landscape",
        "\u6816\u606f\u5730": "natural habitat",
        "\u751f\u6001\u7cfb\u7edf": "ecosystem habitat",
        "\u5b87\u5b99": "outer space",
        "\u592a\u7a7a": "outer space",
    },
    "object_or_detail": {
        "ingredient": "fresh ingredients",
        "ingredients": "fresh ingredients",
        "knife": "kitchen knife",
        "leaf": "leaf close up",
        "plant": "plant close up",
        "tree": "tree close up",
        "fur": "animal fur close up",
        "eye": "animal eye close up",
        "pattern": "pattern close up",
        "line": "lines abstract",
        "data": "data visualization",
        "network": "network connections",
        "\u98df\u6750": "fresh ingredients",
        "\u539f\u6599": "fresh ingredients",
        "\u5200": "kitchen knife",
        "\u53f6\u5b50": "leaf close up",
        "\u690d\u7269": "plant close up",
        "\u6811": "tree close up",
        "\u6811\u679d": "tree branch",
        "\u679c\u5b9e": "fruit on tree",
        "\u6bdb\u53d1": "animal fur close up",
        "\u773c\u775b": "animal eye close up",
        "\u56fe\u6848": "pattern close up",
        "\u7ebf\u6761": "lines abstract",
        "\u7f51\u7edc": "network connections",
    },
}

FOOD_ANCHORS = {
    "bread", "coffee", "cooking", "food", "steak", "tomato scrambled eggs",
}

SPACE_ANCHORS = {
    "black hole", "galaxy", "mars planet", "moon", "outer space", "planet", "star",
}

TECH_ANCHORS = {
    "artificial intelligence", "computer chip", "data visualization",
    "digital network", "internet network", "robot",
}

HISTORY_ANCHORS = {
    "ancient Egypt", "ancient history", "ancient Rome", "castle",
    "Qin dynasty emperor", "ancient Chinese maritime voyage",
    "ancient civilization ruins", "ancient ruins", "ancient temple",
    "ancient Mesopotamian civilization", "ancient Mesopotamian city ruins",
    "ancient ziggurat", "cuneiform clay tablet",
    "archaeological site", "historic harbor", "historic trade port",
    "Mayan ruins", "ocean blockade", "ocean voyage", "sailing fleet",
    "sailors on ship", "stone carving", "wooden sailing ship",
}

NATURAL_DISASTER_ANCHORS = {
    "desert", "earthquake", "glacier", "hurricane", "tornado", "tsunami",
    "volcano",
}

BROAD_TOPIC_ANCHORS = {
    "ancient history", "artificial intelligence", "brand", "cooking", "food",
    "outer space", "product",
}

UNIVERSAL_BAD_FALLBACK_QUERIES = {
    "data visualization",
    "forest leaf macro veins",
    "river delta aerial",
    "river delta aerial top view",
    "crystal growth time lapse",
    "light particles connecting",
    "geometric pattern animation",
    "spiral galaxy nebula",
}

DEGRADED_DIRECTOR_SOURCES = {
    "deterministic_fallback",
    "whole_movie_fallback",
}

DOMAIN_RULE_PACKS = [
    {
        "domain": "history_maritime",
        "priority": 20,
        "needles": {
            "\u90d1\u548c", "\u4e0b\u897f\u6d0b", "\u8239\u961f", "\u5e06\u8239",
            "\u822a\u6d77", "\u6e2f\u53e3", "\u6c34\u624b", "\u5546\u4eba",
            "\u6d77\u7981", "\u8fdc\u822a", "zheng he", "maritime",
            "sailing", "fleet", "harbor", "port", "ocean voyage",
        },
        "topic_anchor": "ancient Chinese maritime voyage",
        "visual_pack": [
            "ancient Chinese sailing ship",
            "wooden sailing fleet",
            "historic harbor",
            "ancient port",
            "ocean voyage",
            "old maritime trade route",
            "spice trade port",
            "old nautical map",
        ],
        "motif_pool": [
            "wooden sailing fleet",
            "ancient Chinese sailing ship",
            "historic harbor",
            "ocean voyage",
            "old maritime trade route",
            "ancient port",
        ],
        "forbidden": [
            "data visualization",
            "forest leaf macro veins",
            "river delta aerial",
            "kitchen cooking",
            "modern city park",
            "generic ancient architecture",
            "wild footage",
            "wild hunting",
            "wild animal hunting",
            "deer",
        ],
    },
    {
        "domain": "ancient_civilization",
        "priority": 10,
        "needles": {
            "\u739b\u96c5", "\u9057\u8ff9", "\u795e\u5e99", "\u8003\u53e4",
            "\u91d1\u5b57\u5854", "\u77f3\u523b", "\u53e4\u6587\u660e",
            "\u4e1b\u6797", "maya", "mayan", "ruins", "temple",
            "archaeology", "archaeological", "stone carving",
        },
        "topic_anchor": "Mayan ruins",
        "visual_pack": [
            "Mayan ruins",
            "jungle temple",
            "ancient stone pyramid",
            "archaeological site",
            "stone carving",
            "Mesoamerican temple",
            "ancient civilization ruins",
            "jungle ruins",
        ],
        "motif_pool": [
            "Mayan ruins",
            "jungle temple",
            "archaeological site",
            "stone carving",
            "ancient civilization ruins",
            "jungle ruins",
        ],
        "forbidden": [
            "sailing ship",
            "ocean voyage",
            "data visualization",
            "modern city",
            "forest leaf macro veins",
            "wild footage",
            "wild hunting",
            "wild animal hunting",
            "deer",
        ],
    },
    {
        "domain": "ancient_mesopotamia",
        "priority": 30,
        "needles": {
            "\u82cf\u7f8e\u5c14", "\u7f8e\u7d22\u4e0d\u8fbe\u7c73\u4e9a",
            "\u4e24\u6cb3\u6d41\u57df", "\u6954\u5f62\u6587\u5b57", "\u6ce5\u677f",
            "\u4e4c\u9c81\u514b", "\u4e4c\u5c14", "\u5409\u5c14\u4f3d\u7f8e\u4ec0",
            "sumer", "sumerian", "mesopotamia", "mesopotamian",
            "cuneiform", "clay tablet", "ziggurat", "uruk", "ur",
            "gilgamesh",
        },
        "topic_anchor": "ancient Mesopotamian civilization",
        "visual_pack": [
            "Mesopotamian ruins",
            "ancient ziggurat",
            "cuneiform clay tablet",
            "clay tablet writing",
            "archaeological excavation",
            "desert archaeological site",
            "ancient city ruins",
            "ancient stone relief",
            "museum ancient artifact",
            "ancient Mesopotamian city",
        ],
        "motif_pool": [
            "Mesopotamian ruins",
            "ancient ziggurat",
            "cuneiform clay tablet",
            "archaeological excavation",
            "desert archaeological site",
            "ancient city ruins",
            "ancient stone relief",
            "museum ancient artifact",
        ],
        "forbidden": [
            "kitchen cooking",
            "wild animal",
            "wild footage",
            "wild hunting",
            "wild animal hunting",
            "deer",
            "animal eating",
            "modern city park",
            "data visualization",
            "forest leaf macro veins",
            "river delta aerial",
            "sailing ship",
            "jungle temple",
        ],
    },
    {
        "domain": "weather_climate",
        "priority": 25,
        "needles": {
            "\u5bd2\u6f6e", "\u6781\u5bd2", "\u51b7\u7a7a\u6c14",
            "\u5317\u6781\u53d8\u6696", "\u5317\u6781", "\u6c14\u6e29\u9aa4\u964d",
            "\u964d\u96ea", "\u66b4\u96ea", "\u98ce\u66b4", "\u6781\u7aef\u5929\u6c14",
            "\u6c14\u8c61", "\u6c14\u5019", "\u5929\u6c14", "\u6d77\u6d0b\u6e29\u5ea6",
            "cold wave", "cold snap", "extreme cold", "cold air",
            "arctic warming", "arctic", "temperature drop", "snowfall",
            "snowstorm", "blizzard", "winter storm", "extreme weather",
            "weather", "climate", "ocean temperature",
        },
        "topic_anchor": "cold wave weather",
        "visual_pack": [
            "cold wave weather",
            "winter storm",
            "snowstorm",
            "blizzard",
            "snowfall",
            "Arctic ice landscape",
            "icy city street",
            "storm clouds",
            "ocean temperature climate",
            "weather satellite cloud system",
        ],
        "motif_pool": [
            "cold wave weather",
            "winter storm",
            "snowstorm",
            "snowfall",
            "Arctic ice landscape",
            "icy city street",
            "storm clouds",
            "stormy ocean waves",
            "weather satellite cloud system",
        ],
        "forbidden": [
            "laboratory",
            "scientific laboratory",
            "microscope",
            "chemistry",
            "biotechnology",
            "pharmacy",
            "scientific research",
            "volcano",
            "lava",
            "polar bear",
            "wild animal",
            "kitchen cooking",
            "ancient ruins",
            "data visualization",
            "forest leaf macro veins",
            "river delta aerial",
        ],
    },
]

SHORT_DOMAIN_VISUAL_PACK_SYSTEM = """You plan stock footage search domains for short videos.
Return JSON only with keys: domain, topic_anchor, domain_visual_pack, domain_motif_pool, forbidden_visual_pack.
Use one domain value from: wildlife_science, finance_economy, history_maritime,
ancient_civilization, ancient_mesopotamia, food_cooking, space_science,
technology_explainer, weather_climate, biography_people, health_wellness,
society_lifestyle, travel_geography, business_work, general_topic.
All search phrases must be English, concrete, visible stock-footage nouns.
Keep the pack inside the video's subject. Do not use universal fallback like data visualization, forest leaf macro veins, or river delta aerial unless the script truly asks for it.
Do not include text for narration, subtitles, CTA, gifts, money, or promises."""


def build_short_video_global_plan(
    llm: Any,
    sentences: list[str],
    *,
    user_brief: str = "",
) -> dict[str, Any]:
    """Build and repair a short-video global plan.

    The shared global director still provides the high-level script plan. This
    repair layer makes it safe for the short-video stock matcher.
    """
    from backend.services.global_director import build_global_plan

    # harden_subject_anchor=False: short video must NOT force the topic anchor onto
    # every query (it produced "coffee excel chart" and forced must_show=['empty']
    # that poisoned whole videos). Keep the concrete per-sentence director output;
    # abstract-chunk B-roll routing is handled by repair_short_video_plan below.
    plan = build_global_plan(
        llm, sentences, user_brief=user_brief, harden_subject_anchor=False,
    )
    domain_pack = build_short_domain_visual_pack(llm, sentences, user_brief=user_brief)
    return repair_short_video_plan(
        plan,
        sentences,
        user_brief=user_brief,
        domain_pack=domain_pack,
    )


def build_short_domain_visual_pack(
    llm: Any,
    sentences: list[str],
    *,
    user_brief: str = "",
) -> dict[str, Any]:
    """Create a per-video visual domain pack for short-video stock fallback.

    The LLM output is only a stock-search contract. It is never merged into the
    script, TTS text, subtitles, or SEO. If the LLM is unavailable, deterministic
    rules still keep the fallback inside the current topic.
    """
    # When content-domain packs are OFF (default), the hardcoded rule packs are
    # NOT a director: never prefer them over the LLM pack. Keep them only as the
    # last-resort fallback if the LLM is entirely unavailable.
    packs_enabled = _content_domain_packs_enabled()
    rule_pack = _rule_domain_visual_pack(user_brief, sentences, "") if packs_enabled else {}
    if llm is None or not hasattr(llm, "_call"):
        return rule_pack
    script = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences[:24]))
    prompt = (
        "User brief:\n"
        f"{user_brief or '(none)'}\n\n"
        "Short-video script sentences:\n"
        f"{script}\n\n"
        "Build one domain visual pack. Use 6-10 domain_visual_pack entries, "
        "5-8 domain_motif_pool entries, and 3-8 forbidden_visual_pack entries."
    )
    try:
        raw = llm._call(SHORT_DOMAIN_VISUAL_PACK_SYSTEM, prompt, purpose="short_domain_visual_pack")
        parsed = _extract_json_object(raw)
        normalized = _normalize_domain_pack(parsed)
        if rule_pack and _should_prefer_rule_domain(rule_pack, normalized):
            return rule_pack
        return normalized or rule_pack
    except Exception:
        return rule_pack


# Words that hijack a stock query toward the literal material/flower instead of
# the real scene (e.g. "copper indian vendor" → copper-wire factory; "rose indian
# vendor" → rose bouquets). Stripped deterministically so the LLM anchoring a
# query on a prop's material or a drink's flavor/flower can't poison the video.
_MISLEADING_MATERIAL_WORDS = frozenset({
    # materials → pull industrial / manufacturing stock
    "copper", "brass", "bronze", "steel", "iron", "aluminum", "aluminium",
    "tin", "plastic", "acrylic", "ceramic", "stainless",
    # flowers / florals used as flavors → pull literal flower / wine footage
    "rose", "lavender", "jasmine", "hibiscus", "lotus", "lily", "tulip",
    "orchid", "chamomile", "violet",
})
_SOFT_VISUAL_MODIFIER_WORDS = frozenset({
    # Color / condition words are useful search hints, but they are not the
    # subject. If they become hard anchors, one adjective can poison every
    # chunk's fallback policy ("blue cart" -> all chunks require blue).
    "blue", "red", "green", "yellow", "orange", "purple", "pink", "brown",
    "gray", "grey", "golden", "silver", "rusty", "rusted", "shiny", "matte",
    "bright", "dark", "colorful", "colourful",
})
# Shot-facet / scene words to vary the SETTING b-roll so abstract or duplicate
# chunks each get a DIFFERENT clip of the SAME correct scene (not the same shot,
_SETTING_FACETS = (
    "wide shot", "close up", "busy scene", "morning light", "customers",
    "hands working", "overhead view", "detail", "evening", "street crowd",
)
# Words that only modify a shot (not the subject) — ignored when deriving the
# recurring SETTING so "indian street vendor close up" still counts as the scene.
_FACET_TOKENS = {
    "close", "up", "wide", "shot", "slow", "motion", "view", "detail", "busy",
    "scene", "overhead", "side", "angle", "establishing", "macro", "closeup",
    "morning", "evening", "light", "background",
} | _SOFT_VISUAL_MODIFIER_WORDS


def _strip_material_words(q: str) -> str:
    words = str(q or "").split()
    kept = [w for w in words if w.lower().strip(",.;:") not in _MISLEADING_MATERIAL_WORDS]
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else str(q or "")


def _derive_setting(chunks: list[dict]) -> str:
    """Deterministically derive the video's recurring SCENE/SUBJECT from its own
    concrete queries (the most frequent short concrete phrase). This is the
    actor+place the script follows (e.g. 'indian street vendor', 'gray wolf') —
    NOT a prop/material/flavor the LLM may have latched onto."""
    phrase_counts: Counter = Counter()
    bigrams: Counter = Counter()
    for chunk in chunks:
        for q in _safe_queries(chunk.get("queries")) + _safe_queries(chunk.get("metaphor_queries")):
            toks = [
                t for t in _strip_material_words(q).lower().split()
                if t not in _FACET_TOKENS and len(t) >= 3 and not t.isdigit()
            ]
            if 2 <= len(toks) <= 4:
                phrase_counts[" ".join(toks)] += 1
            for i in range(len(toks) - 1):
                bigrams[(toks[i], toks[i + 1])] += 1
    # Prefer a recurring phrase naming the human SCENE/ACTOR (vendor at his stall)
    # over the product/ingredient (sugarcane) — abstract chunks read better as the
    # scene than the raw material.
    scene_words = {
        "vendor", "market", "stall", "street", "shop", "store", "cart", "stand",
        "kitchen", "restaurant", "workshop", "factory", "seller", "merchant",
    }
    ranked = phrase_counts.most_common(8)
    for phrase, n in ranked:
        if n >= 2 and not _is_generic_query(phrase) and any(w in phrase.split() for w in scene_words):
            return phrase
    for phrase, n in ranked:
        if n >= 2 and not _is_generic_query(phrase):
            return phrase
    if bigrams:
        (a, b), n = bigrams.most_common(1)[0]
        if n >= 2:
            return f"{a} {b}"
    return ""


_WEAK_SENTENCE_MARKERS = (
    "你猜", "评论区", "下条", "下期", "百分", "翻倍", "毛利", "利润", "成本",
    "感知", "其实", "关键在", "更关键", "关键", "为什么", "结果", "数据", "%", "倍",
    # financial / unit-economics markers that name no filmable object
    "卢比", "净利", "复购", "回头客", "省下", "节省", "降低", "降了", "重复用",
    "清洗费", "容器", "成本从", "利润从", "总利润", "每杯", "每只", "占比", "增长",
)


def _is_weak_sentence(sentence: str) -> bool:
    """A sentence with no concrete filmable thing of its own (numbers, percentages,
    conclusions, rhetorical CTA, unit-economics) — should show a CONCEPT SYMBOL or
    SETTING b-roll, not a literal search ('mumbai profit per unit focus')."""
    s = str(sentence or "")
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    digits = sum(1 for c in s if c.isdigit())
    if cjk and digits / max(cjk, 1) > 0.18:
        return True
    return any(m in s for m in _WEAK_SENTENCE_MARKERS)


# Locale / nationality words. When the video is set in a specific place/culture,
# EVERY stock query must carry it or the search drifts to other regions' footage
# (an Indian Mumbai story getting Hanoi / Greek / NYC street vendors).
_LOCALE_WORDS = frozenset({
    "indian", "india", "mumbai", "delhi", "kolkata", "jaipur", "rajasthan",
    "chinese", "china", "japanese", "japan", "tokyo", "korean", "korea",
    "thai", "thailand", "vietnamese", "vietnam", "hanoi", "italian", "italy", "rome",
    "roman", "mexican", "mexico", "french", "france", "paris", "spanish",
    "spain", "greek", "greece", "egyptian", "egypt", "nepalese", "nepal",
    "himalayan", "british", "turkish", "turkey", "istanbul", "moroccan", "morocco",
    "brazilian", "brazil", "indonesian", "filipino", "persian", "iranian",
    "african", "ethiopian", "peruvian", "icelandic", "iceland", "russian",
    "american", "australian", "australia",
})


def _derive_locale(video_meta: dict, chunks: list[dict]) -> str:
    """The video's place/culture word (e.g. 'indian'), taken from the stable
    skeleton fields (visual_world / theme / motif_pool). '' when the video is not
    place-specific (e.g. a wildlife video) so nothing is injected."""
    parts = [
        str(video_meta.get("visual_world") or ""),
        str(video_meta.get("theme") or ""),
        " ".join(_safe_queries(video_meta.get("motif_pool"))),
    ]
    counts: Counter = Counter()
    for tok in " ".join(parts).lower().replace(",", " ").split():
        if tok in _LOCALE_WORDS:
            counts[tok] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return ""


def _ensure_locale(q: str, locale: str) -> str:
    """Prepend the locale to a query that names no place/culture, so the stock
    search stays in the right country."""
    if not locale:
        return q
    toks = str(q or "").lower().split()
    if any(t.strip(",.;:") in _LOCALE_WORDS for t in toks):
        return q
    return f"{locale} {q}".strip()


# Scene / actor words: a query naming one of these establishes WHERE the story
# happens, so it must carry the locale. Reused by _derive_setting's scene check.
_SCENE_WORDS = frozenset({
    "vendor", "market", "stall", "street", "shop", "store", "cart", "stand",
    "kitchen", "restaurant", "workshop", "factory", "seller", "merchant",
    "roadside", "bazaar", "shopfront", "counter",
})
# People words: a query showing PEOPLE who are part of the local culture should
# carry the locale (young indians, indian children); abstract symbols must not.
_PEOPLE_WORDS = frozenset({
    "people", "person", "man", "woman", "men", "women", "child", "children",
    "kid", "kids", "young", "youth", "boy", "girl", "friends", "family",
    "customer", "customers", "crowd", "faces", "portrait",
})

_WEAK_MUST_SHOW_TERMS = frozenset({
    "street", "city", "urban", "scene", "footage", "video", "shot", "view",
    "close", "wide", "background", "daytime", "district", "local", "authentic",
}) | _SOFT_VISUAL_MODIFIER_WORDS

_HARD_ANCHOR_STOPWORDS = _WEAK_MUST_SHOW_TERMS | _LOCALE_WORDS | frozenset({
    "with", "from", "into", "near", "like", "tiny", "fresh", "south", "asian",
    "manual", "crowded", "busy", "flow", "forming", "daytime", "comparison",
    "side", "condensation", "reflecting", "sunlight", "seller", "serving",
    "vendor", "stall", "street", "shop", "store", "cart", "stand", "market",
    "person", "people", "man", "woman", "customer", "customers", "hands",
    "cut", "cuts", "cutting", "slice", "slices", "slicing", "slit", "slits",
    "pour", "pours", "pouring", "roast", "roasted", "roasting", "make",
    "makes", "making", "prepare", "prepares", "preparing", "sell", "selling",
    "drink", "preparation", "food", "tourist", "tourists", "commuter",
    "commuters", "receiving", "receive", "receives", "giving", "give", "gives",
    "stack", "stacked", "stacking", "golden", "hot", "warm", "dusk", "metal",
    "close-up", "closeup",
}) | _SOFT_VISUAL_MODIFIER_WORDS | STATE_NON_ANCHOR_WORDS


def _query_hard_anchors(queries: Any) -> list[str]:
    anchors: list[str] = []
    for query in _safe_queries(queries):
        for token in re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", query.lower()):
            token = token.strip("-")
            if token in _HARD_ANCHOR_STOPWORDS or len(token) < 3:
                continue
            if token.endswith("ies") and len(token) > 4:
                token = f"{token[:-3]}y"
            elif (
                token.endswith("s")
                and len(token) > 4
                and not token.endswith(("ss", "us", "is"))
                and token not in _NON_PLURAL_S
            ):
                token = token[:-1]
            if token not in anchors:
                anchors.append(token)
    return anchors


def _dedupe_must_show_terms(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = _clean_term(value)
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _strengthen_must_show(must_show: Any, queries: Any) -> list[str]:
    terms = [
        _clean_term(term)
        for term in _as_list(must_show)
        if _clean_term(term)
    ]
    strong = [term for term in terms if term not in _WEAK_MUST_SHOW_TERMS]
    anchors = _query_hard_anchors(queries)
    if strong:
        return _dedupe_must_show_terms(strong)[:3]
    strong = anchors[:3]
    return _dedupe_must_show_terms(strong)[:3]


def _sentence_local_must_show(sentence: str, queries: Any, current: Any) -> list[str]:
    """Keep hard must_show scoped to this sentence's visual subject.

    The whole-video motif pool is allowed to provide soft story context, but it
    must not turn one sentence's visual (queue/cash/etc.) into every sentence's
    hard anchor.
    """
    text = str(sentence or "").lower()
    terms: list[str] = []

    def add(term: str) -> None:
        if term and term not in terms:
            terms.append(term)

    has_queue = any(x in text for x in ("queue", "line", "customer", "buy", "顾客", "排队", "队伍", "复购", "购买"))
    has_cash_action = any(x in text for x in (
        "cash", "money", "counting money", "banknote", "banknotes",
        "notes", "change", "payment", "rupee", "rupees", "dong", "lira",
        "收钱", "现金", "数钱", "印度卢比",
    ))
    has_business_money = any(x in text for x in ("profit", "cost", "revenue", "margin", "利润", "成本", "收入", "毛利"))
    has_price = any(x in text for x in ("price", "pricing", "rupee", "rupees", "卢比", "定价"))
    has_retail_shelf = any(x in text for x in (
        "shelf", "shelves", "convenience store", "supermarket", "retail",
        "货架", "便利店", "超市", "专区", "左区", "中区", "右区", "两格",
    ))
    has_expiry = any(x in text for x in (
        "expiry", "expiration", "best before", "sell-by", "near expiry",
        "临期", "保质期", "剩27天", "剩 27 天", "剩余", "到期",
    ))
    has_scene_only_subject = (
        any(x in text for x in (
            "motorbike", "commuter", "commuters", "curb", "road", "traffic",
            "pedestrian", "pedestrians", "tourist", "tourists", "people wait",
        ))
        and not any(x in text for x in (
            "coffee", "phin", "condensed milk", "iced coffee", "sugarcane",
            "chestnut", "syrup", "honey", "cup", "glass", "cash", "money",
        ))
    )

    if any(x in text for x in ("kettle", "pot", "壶")):
        add("kettle")
    if any(x in text for x in ("freezer", "refrigerator", "refrigerated", "cooler", "冰柜", "冷柜")):
        add("refrigerated display case")
    if any(x in text for x in ("cola", "coke", "soft drink", "可乐")):
        add("cola bottles")
    if any(x in text for x in ("yogurt", "yoghurt", "酸奶")):
        add("yogurt cups")
    if any(x in text for x in ("milk", "fresh milk", "鲜奶", "牛奶")):
        add("milk bottles")
    if any(x in text for x in ("mahua", "fried snack", "麻花")):
        add("packaged fried snack")
    if has_expiry:
        add("expiry date label")
    if any(x in text for x in ("discount tag", "yellow tag", "yellow label", "黄标", "折扣贴", "标签")):
        add("yellow discount tag")
    if any(x in text for x in ("ice pack", "ice bag", "冰袋")):
        if any(term in terms for term in ("milk bottles", "yogurt cups")):
            add("refrigerated dairy shelf")
        else:
            add("ice pack")
    if has_retail_shelf:
        if any(x in text for x in ("empty", "vacant", "空着", "空了", "空位", "两格")):
            add("empty retail shelf")
        else:
            add("store shelf")
    if any(x in text for x in ("ledger", "notebook", "inventory book", "账本", "笔记本", "进货")):
        add("inventory notebook")
    if any(x in text for x in ("inventory", "loss rate", "损耗率", "损耗")):
        add("store inventory shelf")
    if any(x in text for x in ("cup", "glass", "杯子")):
        add("cup")
    if any(x in text for x in ("coffee", "phin", "condensed milk", "iced coffee")):
        add("coffee")
    if any(x in text for x in ("chestnut", "chestnuts")):
        add("chestnut")
    if any(x in text for x in ("pan", "skillet")) and "chestnut" not in terms:
        add("pan")
    if any(x in text for x in ("syrup", "honey", "jaggery", "sweetener", "糖浆", "浓度", "椰枣蜜", "蜂蜜", "熬糖浆")):
        add("syrup or honey")
    if any(x in text for x in ("pour", "pouring", "drink", "juice", "chai", "tea", "倒", "饮品", "糖水", "甘蔗汁", "茶")):
        add("drink preparation")
    if any(x in text for x in ("\u7518\u8517", "\u7518\u8517\u6c41", "sugarcane")):
        add("sugarcane")
    if any(x in text for x in ("vendor", "stall", "street", "小贩", "摊主", "摊", "街头")):
        add("vendor")
    if has_queue:
        add("customer line")
    if has_cash_action:
        add("cash")

    if "vendor" in terms and len(terms) > 1:
        terms = [term for term in terms if term != "vendor"]
    elif terms == ["vendor"]:
        terms = []

    if terms:
        return _dedupe_must_show_terms(terms)[:3]
    if has_scene_only_subject:
        return []
    if _is_volume_metric_sentence(text):
        return []

    current_terms = []
    for term in _as_list(current):
        cleaned = _clean_term(term)
        if not cleaned or cleaned in _WEAK_MUST_SHOW_TERMS or cleaned in _HARD_ANCHOR_STOPWORDS:
            continue
        if cleaned == "cup" and re.search(r"\d+\s*杯|每杯", text):
            continue
        current_terms.append(cleaned)
    current_terms = _dedupe_must_show_terms(current_terms)
    if current_terms:
        return current_terms[:3]
    anchors = [
        anchor for anchor in _query_hard_anchors(queries)
        if not (anchor == "cup" and re.search(r"\d+\s*杯|每杯", text))
    ]
    return _dedupe_must_show_terms(anchors)[:3]


def _cash_visual_allowed_for_sentence(sentence: str) -> bool:
    text = str(sentence or "").lower()
    return any(x in text for x in (
        "cash", "money", "counting money", "banknote", "banknotes",
        "payment", "paying cash", "rupee notes", "currency bills",
        "\u6536\u94b1", "\u73b0\u91d1", "\u6570\u94b1", "\u949e\u7968", "\u7eb8\u5e01",
    ))


def _is_cash_query(query: Any) -> bool:
    text = str(query or "").lower()
    return any(x in text for x in (
        "cash", "money", "banknote", "banknotes", "currency",
        "counting rupees", "counting cash", "gold coins",
    ))


def _strip_disallowed_cash_queries(queries: Any, sentence: str) -> list[str]:
    safe = _safe_queries(queries)
    if _cash_visual_allowed_for_sentence(sentence):
        return safe
    return [q for q in safe if not _is_cash_query(q)]


def _is_volume_metric_sentence(text: str) -> bool:
    return bool(re.search(r"\d+\s*杯", text)) and any(
        term in text for term in ("日均", "每天", "每日", "daily", "per day")
    )


def _retail_queries_for_sentence(sentence: str) -> list[str]:
    text = str(sentence or "").lower()
    out: list[str] = []

    def has_any(*terms: str) -> bool:
        return any(term in text for term in terms)

    def add(query: str) -> None:
        if query not in out:
            out.append(query)

    if has_any("冰柜", "冷柜", "freezer", "refrigerated", "cooler") and has_any("可乐", "cola", "coke"):
        add("refrigerated display case stocked with cola bottles")
        add("convenience store cooler full of soft drink bottles")
    if has_any("酸奶", "yogurt", "yoghurt"):
        add("yogurt cups in convenience store refrigerator")
        add("packaged dairy shelf with expiry date labels")
    if has_any("麻花", "mahua", "fried snack"):
        add("packaged fried snack on convenience store shelf")
        add("local packaged fried snack near checkout counter")
    if has_any("鲜奶", "milk", "fresh milk") and has_any("冰袋", "ice pack", "ice bag"):
        add("fresh milk bottles in refrigerated dairy shelf")
        add("refrigerated dairy shelf with milk cartons")
    if has_any("保质期", "临期", "expiry", "expiration", "near expiry"):
        add("expiry date label on packaged food")
        add("discount label on convenience store food shelf")
    if has_any(
        "货架", "shelf", "shelves", "便利店", "convenience store",
        "专区", "左区", "中区", "右区", "两格",
    ):
        if has_any("空着", "空了", "空位", "两格", "empty", "vacant"):
            add("partly empty retail shelf with packaged goods")
            add("empty retail shelf in convenience store")
        else:
            add("convenience store shelf with packaged goods")
    if has_any("黄标", "yellow tag", "yellow label", "discount tag"):
        add("convenience store shelf with yellow discount tags")
        add("yellow discount tag on convenience store shelf")
    if has_any("冰袋", "ice pack", "ice bag"):
        add("milk bottles chilled with ice pack")
        add("ice pack on refrigerated dairy shelf")
    if has_any("账本", "笔记本", "ledger", "notebook"):
        add("modern inventory notebook on shop counter")
    if has_any("损耗率", "损耗", "loss rate", "inventory"):
        add("convenience store inventory shelf")
        add("shop owner checking packaged food inventory")
    return out


def _is_retail_video(text: str) -> bool:
    """True only for genuine convenience-store / retail topics. The per-sentence
    retail query mapper must be gated on this: words like 账本/损耗率/inventory
    appear in non-retail stories too (a coffee couple's ledger / spoilage), and
    without this gate they injected "modern inventory notebook" / "convenience
    store inventory shelf" into a coffee video → wrong footage."""
    t = str(text or "").lower()
    return any(x in t for x in (
        "便利店", "超市", "convenience store", "supermarket", "grocery store",
        "冰柜", "冷柜", "货架", "临期", "保质期", "酸奶", "鲜奶", "黄标", "可乐",
    ))


def _retail_forbidden_for_sentence(sentence: str) -> list[str]:
    text = str(sentence or "").lower()
    is_retail = any(x in text for x in (
        "convenience store", "supermarket", "grocery", "retail", "shelf",
        "便利店", "超市", "货架", "冰柜", "临期", "保质期", "黄标", "鲜奶", "酸奶", "麻花",
        "专区", "左区", "中区", "右区", "两格",
    ))
    if not is_retail:
        return []
    forbidden = []
    if any(x in text for x in ("empty", "vacant", "空着", "空了", "空位", "两格")):
        forbidden.extend([
            "fully stocked shelf",
            "full shelf",
            "packed shelf",
            "overstocked shelf",
        ])
    if any(x in text for x in ("mahua", "fried snack", "麻花")):
        forbidden.extend([
            "potatoes",
            "raw potato",
            "vegetable kitchen",
            "restaurant kitchen",
            "frying oil closeup",
            "potato chips factory",
        ])
    forbidden.extend([
        "bookshelf",
        "book shelf",
        "library shelf",
        "cinema seats",
        "theater seats",
        "subway train",
        "metro train",
        "swing set",
        "broken glass",
        "cracked glass",
        "ice crack texture",
        "abstract ink",
        "liquid paint abstract",
        "paint wallpaper",
        "fire flames",
        "lava",
    ])
    return forbidden


def _is_retail_metric_sentence(text: str) -> bool:
    if any(term in text for term in ("loss rate", "损耗率", "损耗")):
        return True
    if any(term in text for term in (
        "cola", "coke", "yogurt", "yoghurt", "milk", "mahua",
        "fried snack", "可乐", "酸奶", "鲜奶", "牛奶", "麻花",
    )):
        return False
    return any(
        term in text
        for term in ("loss rate", "损耗率", "损耗", "毛利", "margin", "profit")
    ) and any(
        term in text
        for term in ("shelf", "store", "inventory", "货架", "便利店", "进货", "临期", "保质期")
    )


def _story_soft_fallback_sentence(sentence: str) -> bool:
    text = str(sentence or "").lower()
    if _is_volume_metric_sentence(text):
        return True
    if _is_retail_metric_sentence(text):
        return True
    if any(term in text for term in ("又加", "再加", "加了一勺", "add a spoon", "adds a spoon")):
        return True
    if any(term in text for term in ("队伍继续", "继续往前", "line keeps moving", "queue keeps moving")):
        return True
    if (
        any(term in text for term in ("倒饮品", "倒糖水", "pour drink", "pouring drink"))
        and any(term in text for term in ("收钱", "卢比", "cash", "money", "rupee"))
    ):
        return True
    if any(term in text for term in ("关键不是", "关键是", "the point is", "not about")):
        return True
    if any(term in text for term in (
        "sells more", "serves more", "sell more", "serve more",
        "regular customers", "more quickly", "faster", "before noon",
    )):
        return True
    return False


def _is_scene_query(q: str) -> bool:
    toks = {t.strip(",.;:") for t in str(q or "").lower().split()}
    return bool(toks & _SCENE_WORDS)


def _is_people_query(q: str) -> bool:
    toks = {t.strip(",.;:") for t in str(q or "").lower().split()}
    return bool(toks & _PEOPLE_WORDS)


# CONCEPT → VISUAL SYMBOL. Abstract / financial / emotional sentences name no
# wrong literal ("mumbai profit per unit focus" → garbage) the user wants the
# CONCEPT mapped to a concrete visual SYMBOL: money→counting cash, growth→rising
# chart, queue→line of customers, young crowd→smiling youth, kids→children.
# Each concept yields SEVERAL distinct symbols so a long sentence can be split
# into multiple ~3s shots (workstream C) without repeating one clip. People
# symbols get the locale (young indians); universal symbols (graph, coins) don't.
# Order matters: the FIRST matching concept leads the shot. People / scene /
# action concepts (queue, youth, children, saving) are the central, concrete
# visual and outrank the abstract money/growth backbone — so "顾客排队变长" (which
# also mentions 卢比) leads on the QUEUE, not on cash.
_CONCEPT_SYMBOL_MAP: tuple[tuple[frozenset, tuple[str, ...]], ...] = (
    # queue / customers / popularity — the marketing pull (people, very visual)
    (frozenset({
        "排队", "顾客", "客人", "人气", "爆满", "回头客", "复购", "排长队", "客流",
        "queue", "customers", "crowd", "popular", "loyal",
    }),
        ("customers waiting in line", "busy crowd at food stall", "happy customer buying drink")),
    # young people / sharing / check-in — who marketing attracts (emotion)
    (frozenset({
        "年轻人", "打卡", "传播", "分享", "拍照", "网红", "社交", "口碑", "刷屏",
        "young", "youth", "sharing", "viral", "social",
    }),
        ("young people smiling with phones", "youth taking photos together", "young friends laughing")),
    # children / kids — warm emotional note
    (frozenset({
        "小朋友", "孩子", "儿童", "children", "kids", "child",
    }),
        ("smiling children portrait", "happy kids laughing", "children joyful faces")),
    # saving / reusing — efficiency (reusable cups, washing). Note: 减少/降低 are
    # deliberately excluded — "减少销量" (reduce sales) is about the growth lever,
    # not waste, and should fall through to growth/money below.
    (frozenset({
        "省下", "节省", "废", "垃圾", "环保", "重复用", "清洗", "塑料杯", "打包",
        "save", "reusable", "recycle", "reuse", "washing",
    }),
        ("stacking reusable cups", "washing glass cups", "reducing plastic waste")),
    # growth / increase / double — the success outcome (chart). Ahead of money so
    (frozenset({
        "增长", "翻倍", "上升", "升到", "提升", "涨", "增加", "暴涨", "增收",
        "扩大", "跳到", "growth", "increase", "double", "rising", "surge", "boost",
    }),
        ("business graph arrow going up", "rising profit chart animation", "upward growth arrow")),
    # money / profit / cost / price — the financial backbone (static cash)
    (frozenset({
        "钱", "利润", "毛利", "净利", "成本", "收入", "赚", "卢比", "定价",
        "售价", "块钱", "营收", "盈利", "现金", "美元", "金币",
        "profit", "margin", "cost", "revenue", "income", "money", "rupee", "cash",
    }),
        ("hands counting cash money", "stack of gold coins", "money banknotes close up")),
)


def _concept_symbol_queries(sentence: str, locale: str = "") -> list[str]:
    """For an abstract/financial/emotional sentence, return concrete visual SYMBOL
    queries for the concepts it mentions (money→counting cash, growth→rising chart).
    People symbols get the locale; universal symbols (graph, coins) stay neutral.
    Empty when the sentence names no mapped concept (caller falls back to scene
    b-roll)."""
    s = str(sentence or "").lower()
    out: list[str] = []
    for needles, symbols in _CONCEPT_SYMBOL_MAP:
        if any(n in s for n in needles):
            for sym in symbols:
                out.append(_ensure_locale(sym, locale) if _is_people_query(sym) else sym)
    return _dedupe_queries(out)


def _establishing_query(locale: str, setting: str, subject: str, pool: list[str]) -> str:
    """The opening (定场) shot must establish WHERE the story is — the recurring
    scene/subject + locale (e.g. 'indian street vendor'), never a generic clip
    (倒水空镜). Topic-agnostic: a wildlife video opens on its subject ('red fox'),
    a street-business video on its scene ('indian street vendor')."""
    cand = ""
    # Prefer a real SCENE phrase from the stable pool (e.g. 'sugarcane juice
    # stall', 'indian street vendor') — that is the strongest establishing shot.
    for p in (pool or []):
        if _is_scene_query(p) and not _is_generic_query(p):
            cand = p
            break
    if not cand:
        cand = setting if (setting and not _is_generic_query(setting)) else subject
    if not cand and pool:
        cand = pool[0]
    cand = _strip_material_words(str(cand or "").strip())
    return _ensure_locale(cand, locale) if cand else ""


def _dedupe_diversify_chunk_queries(
    chunks: list[dict], setting_pool: list[str], locale: str = "",
    *, subject: str = "",
) -> None:
    """Deterministic footage-query hygiene (LLM-independent), in place:
      1. strip misleading material/flower words (they pull industrial / floral stock);
      2. for ABSTRACT / numeric / generic / duplicate chunks, replace the query by
         ROTATING through ``setting_pool`` — the skeleton's STABLE, brief-derived
         on-topic b-roll (video_meta.motif_pool, e.g. ['Indian street vendor',
         'sugarcane juice stall', 'hand-pressed juice']) — plus a rotating
         shot-facet. So weak chunks show varied b-roll of the CORRECT scene instead
         of a wrong literal match, the same repeated clip, or a setting that drifts
         with the LLM's per-run queries.
    Concrete, distinct sentences keep their own per-sentence query."""
    pool = [
        _ensure_locale(q, locale)
        for q in _dedupe_queries([_strip_material_words(q) for q in _safe_queries(setting_pool)])
        if q and not _is_generic_query(q)
    ]
    if not pool:
        fallback = _ensure_locale(_derive_setting(chunks), locale)
        pool = [fallback] if fallback else []
    setting_for_open = _derive_setting(chunks) or subject
    seen: set[str] = set()
    pi = 0
    fi = 0
    for idx, chunk in enumerate(chunks):
        for field in ("queries", "metaphor_queries"):
            arr = _safe_queries(chunk.get(field))
            if arr:
                chunk[field] = _dedupe_queries([_strip_material_words(q) for q in arr])

        lead_field = "queries" if _safe_queries(chunk.get("queries")) else "metaphor_queries"
        arr = list(chunk.get(lead_field) or [])
        sentence = str(chunk.get("sentence") or "")
        lead = arr[0] if arr else ""

        # Rule 6: the OPENING shot must establish WHERE the story is (locale +
        # scene), never a generic clip. Force chunk 0's lead to an establishing
        # query so the video opens on the Indian street stall, not a stray
        # "pouring glass of water". Only for a REAL opening: a multi-chunk video
        # whose first chunk is a concrete (non-pool-first) shot — never a lone
        # CTA / transition chunk (those are intentionally subject-safe b-roll).
        if (
            idx == 0
            and len(chunks) > 1
            and str(chunk.get("visual_focus") or "") not in POOL_FIRST_FOCUS
        ):
            est = _establishing_query(locale, setting_for_open, subject, pool)
            if est:
                arr = [est] + [a for a in arr if _normalize_query(a) != _normalize_query(est)]
                chunk[lead_field] = arr[:6]
                seen.add(_normalize_query(est))
                continue

        # Only override the director when the LEAD QUERY itself is bad (empty,
        # generic, or a duplicate of an earlier shot). Do NOT override just because
        # the sentence contains numbers (`_is_weak_sentence`): a concrete query like
        # "hand picking coffee beans" for "云南海拔1800米手摘豆" is good and must be
        # kept — the director (harden off) now produces concrete per-sentence
        # queries, so the repair's job is hygiene, not regeneration. A genuinely
        # abstract/financial sentence shows up here as a GENERIC lead and still gets
        # the concept-symbol / pool treatment below.
        weak = (
            not lead
            or _is_generic_query(lead)
            or (_normalize_query(lead) in seen)
        )
        if weak:
            # Prefer CONCEPT→SYMBOL for an abstract/financial sentence (money→cash,
            # growth→chart) — relevant to THIS sentence. Fall back to rotating the
            # stable scene b-roll pool for generic-weak sentences with no concept.
            symbols = [
                s for s in _concept_symbol_queries(sentence, locale)
                if _normalize_query(s) not in seen
            ] or _concept_symbol_queries(sentence, locale)
            if symbols:
                arr = _dedupe_queries(symbols + [a for a in arr])
                chunk[lead_field] = arr[:6]
                lead = arr[0]
            elif pool:
                base = pool[pi % len(pool)]
                pi += 1
                new_lead = base
                if _normalize_query(new_lead) in seen:
                    new_lead = f"{base} {_SETTING_FACETS[fi % len(_SETTING_FACETS)]}"
                    fi += 1
                arr = [new_lead] + [a for a in arr if _normalize_query(a) != _normalize_query(new_lead)]
                chunk[lead_field] = arr[:6]
                lead = new_lead
        elif arr:
            chunk[lead_field] = arr
        if lead:
            seen.add(_normalize_query(lead))

    # Selective locale (rule 4): only SCENE/SETTING and PEOPLE queries carry the
    # place word (they establish the "where" / the local culture). Generic
    # actions, objects and concept symbols (waiting in line, washing cups,
    # business graph, gold coins) stay locale-free — forcing "indian" onto every
    # query was over-anchoring. chunk 0 (establishing) is always localized.
    if locale:
        for idx, chunk in enumerate(chunks):
            for field in ("queries", "metaphor_queries"):
                arr = _safe_queries(chunk.get(field))
                if not arr:
                    continue
                chunk[field] = _dedupe_queries([
                    _ensure_locale(q, locale)
                    if (idx == 0 or _is_scene_query(q) or _is_people_query(q)) else q
                    for q in arr
                ])


def _chunk_director_degraded(chunk: dict[str, Any]) -> bool:
    return str(chunk.get("plan_source") or "").strip().lower() in DEGRADED_DIRECTOR_SOURCES


def _default_pool_is_quarantined(
    plan: dict[str, Any],
    chunks: list[dict[str, Any]],
    motif_pool: list[str],
) -> bool:
    video_meta = dict((plan or {}).get("video_meta") or {})
    if str(video_meta.get("motif_pool_source") or "") == "default_fallback":
        return True
    if not is_default_motif_pool(motif_pool):
        return False
    # Backward-compatible inference for plans created before motif_pool_source
    # existed. A real LLM may legitimately ask for a river delta or leaf macro;
    # only quarantine the pool when every chunk also carries fallback provenance.
    return bool(chunks) and all(_chunk_director_degraded(chunk) for chunk in chunks)


def _director_degradation_reasons(
    plan: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    default_pool_quarantined: bool,
) -> list[str]:
    meta = dict((plan or {}).get("meta") or {})
    reasons = [str(value) for value in _as_list(meta.get("degradation_reasons")) if str(value)]
    if meta.get("whole_movie_fallback"):
        reasons.append("whole_movie_fallback")
    if any(_chunk_director_degraded(chunk) for chunk in chunks):
        reasons.append("deterministic_chunk_fallback")
    if default_pool_quarantined:
        reasons.append("default_motif_pool")
    return list(dict.fromkeys(reasons))


def repair_short_video_plan(
    plan: dict[str, Any],
    sentences: list[str],
    *,
    user_brief: str = "",
    domain_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repaired = copy.deepcopy(plan or {})
    chunks = list(repaired.get("chunks") or [])
    video_meta = dict(repaired.get("video_meta") or {})
    domain_pack = _normalize_domain_pack(domain_pack)
    original_motif_pool = _safe_queries(video_meta.get("motif_pool"))
    default_pool_quarantined = _default_pool_is_quarantined(
        repaired, chunks, original_motif_pool,
    )
    initial_pool = (
        [] if default_pool_quarantined
        else (original_motif_pool or _default_motif_pool(user_brief, sentences))
    )
    subject, topic_anchor_source = _derive_topic_anchor_with_source(
        chunks, sentences, user_brief, initial_pool,
    )
    packs_enabled = _content_domain_packs_enabled()
    rule_domain_pack = _rule_domain_visual_pack(user_brief, sentences, subject)
    use_rule_domain_pack = False
    if rule_domain_pack:
        if packs_enabled:
            use_rule_domain_pack = (
                _should_prefer_rule_domain(rule_domain_pack, domain_pack)
                or not domain_pack
            )
        else:
            rule_domain = str(rule_domain_pack.get("domain") or "")
            candidate_domain = str(domain_pack.get("domain") or "")
            candidate_matches_rule = bool(
                candidate_domain
                and candidate_domain == rule_domain
                and (
                    domain_pack.get("domain_visual_pack")
                    or domain_pack.get("domain_motif_pool")
                )
            )
            # Packs are OFF by default, so a rule pack may only step in when the
            # director produced no usable concrete search terms for this video's
            # detected domain, or when the provided domain is malformed/unknown.
            # "Concrete but wrong-world" queries (river delta for Mayan ruins,
            # volcano/polar-bear for cold waves) are treated as unusable here.
            if candidate_matches_rule:
                use_rule_domain_pack = False
            elif _should_prefer_rule_domain(rule_domain_pack, domain_pack):
                use_rule_domain_pack = True
            elif rule_domain != "general_topic":
                if not _plan_has_director_queries_allowed_for_domain(chunks, rule_domain_pack):
                    use_rule_domain_pack = True
                elif (
                    not domain_pack
                    and _plan_queries_need_domain_repair(chunks, rule_domain_pack)
                ):
                    use_rule_domain_pack = True
            elif not _plan_has_director_queries_allowed_for_domain(chunks, domain_pack):
                use_rule_domain_pack = True
    if use_rule_domain_pack:
        domain_pack = rule_domain_pack
    domain_pack = _prune_contradictory_forbidden(domain_pack, sentences, chunks, user_brief)
    domain_anchor = _clean_term(domain_pack.get("topic_anchor"))
    if domain_anchor and (
        not subject
        or subject in BROAD_TOPIC_ANCHORS
        or topic_anchor_source in {"none", "motif_pool"}
    ):
        subject = domain_anchor
        topic_anchor_source = "domain_pack"
    motif_pool = _domain_motif_pool(domain_pack, initial_pool)
    trusted_llm_pool = False
    if not packs_enabled:
        # Build the video's shared visual vocabulary from the LLM's OWN concrete
        # per-sentence queries. Sentences the LLM left empty (or that fell back to
        # a generic "<subject> footage") then draw distinct, on-subject B-roll
        # (e.g. "Pantheon dome interior", "Roman concrete macro") from this pool
        # instead of a bland "ancient rome footage" — fixes the over-split
        # fragments that otherwise got generic stock. The LLM stays the director;
        # this only reuses what it already produced.
        forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
        llm_pool = _dedupe_queries([
            q
            for ch in chunks
            if not _chunk_director_degraded(ch)
            for q in _safe_queries(ch.get("queries"))
            if _query_allowed_for_domain(
                q, domain_pack, forbidden, allow_universal=not bool(domain_pack),
            )
        ])
        if llm_pool:
            trusted_llm_pool = True
            motif_pool = _dedupe_queries(llm_pool + _safe_queries(motif_pool))

    for idx, chunk in enumerate(chunks):
        if "chunk_index" not in chunk:
            chunk["chunk_index"] = idx
        sentence = str(
            chunk.get("sentence")
            or chunk.get("text")
            or (sentences[idx] if idx < len(sentences) else "")
        )
        chunk["sentence"] = sentence
        focus = _infer_visual_focus(chunk, sentence, subject)
        chunk_degraded = _chunk_director_degraded(chunk)
        llm_native = [] if chunk_degraded else _safe_queries(chunk.get("queries"))
        # LLM-as-director: when content-domain packs are OFF and the LLM gave
        # concrete queries for this sentence, keep them verbatim (the LLM already
        # nailed the subject, e.g. "Pantheon Rome dome interior") instead of
        # letting the rule packs rebuild/override them. A chunk the focus
        # classifier called pool-first but which DID get concrete LLM queries is
        # promoted to a real visual anchor so those queries actually get searched.
        if not packs_enabled and llm_native:
            forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
            llm_allowed = [
                q for q in llm_native
                if _query_allowed_for_domain(
                    q, domain_pack, forbidden, allow_universal=not bool(domain_pack),
                )
            ]
        else:
            llm_allowed = []
        if not packs_enabled and llm_allowed:
            if focus in POOL_FIRST_FOCUS:
                focus = "object_or_detail"
            term = _term_for_focus(focus, sentence, chunk, subject, motif_pool)
            queries = llm_allowed[:8]
            query_source, degradation = "llm_native", "none"
        elif (
            not packs_enabled
            and not domain_pack
            and focus not in POOL_FIRST_FOCUS
            and motif_pool
            and (not default_pool_quarantined or trusted_llm_pool)
        ):
            # Concrete sentence the LLM left without its own query (e.g. an
            # over-split fragment). Draw distinct, on-subject shots from the
            # video's LLM vocabulary instead of a generic "<subject> footage".
            term = _term_for_focus(focus, sentence, chunk, subject, motif_pool)
            queries = _safe_queries(motif_pool)[:6]
            query_source, degradation = "llm_motif_pool", "none"
        else:
            term = _term_for_focus(focus, sentence, chunk, subject, motif_pool)
            queries = _queries_for_focus(focus, term, subject, motif_pool, chunk.get("queries"))
            if chunk_degraded and domain_pack:
                # A degraded director query is only a placeholder. Force the
                # independent per-video domain contract to supply concrete
                # visuals instead of accepting invented "<topic> natural scene".
                queries = []
            queries, query_source, degradation = _repair_queries_with_domain(
                queries,
                focus,
                sentence,
                term,
                subject,
                motif_pool,
                domain_pack,
            )
            if chunk_degraded and query_source == "short_video_repair":
                query_source = "subject_fallback"
                degradation = "director_degraded_repaired"
        chunk["visual_focus"] = focus
        chunk["visual_focus_source"] = "short_video_repair"
        chunk["use_pool_first"] = focus in POOL_FIRST_FOCUS
        chunk["must_show"] = _must_show_for_focus(focus, term, subject)
        chunk["queries"] = queries
        chunk["must_show"] = _repair_must_show_with_domain(
            chunk.get("must_show"),
            domain_pack,
        )
        chunk["must_show"] = _strengthen_must_show(chunk.get("must_show"), chunk.get("queries"))
        chunk["metaphor_queries"] = _metaphor_queries_for_focus(
            focus,
            term,
            subject,
            motif_pool,
            chunk.get("metaphor_queries"),
            domain_pack=domain_pack,
        )
        chunk["query_source"] = query_source
        chunk["query_degradation_level"] = degradation
        chunk["domain"] = domain_pack.get("domain") or ""
        chunk["domain_motif_pool"] = list(domain_pack.get("domain_motif_pool") or [])
        chunk["forbidden_visual_pack"] = list(domain_pack.get("forbidden_visual_pack") or [])
        chunk["visual_anchor"] = focus not in POOL_FIRST_FOCUS and bool(chunk["queries"])
        chunk["short_video_visual_policy"] = {
            "topic_anchor": subject,
            "max_must_show": 1,
            "haiku_escalation": False,
        }
        _sanitize_must_not(chunk, domain_pack)

    # Deterministic hygiene pass: strip misleading material anchors + reanchor
    # abstract/duplicate chunks to the skeleton's STABLE on-topic motif_pool
    # (brief-derived, not the LLM's per-run queries) so the back half isn't the
    # same repeated / off / drifting clip. Use the original (skeleton) motif_pool
    # here — video_meta["motif_pool"] is overwritten with the working pool below.
    if len(chunks) > 1:
        _dedupe_diversify_chunk_queries(
            chunks, original_motif_pool, _derive_locale(video_meta, chunks),
            subject=subject,
        )
    # The hardcoded retail query/forbidden mapper only applies to genuine
    # convenience-store / retail videos. Gated on the WHOLE video's topic so words
    # like 账本/损耗率 in a coffee story don't pull "modern inventory notebook" /
    # "convenience store inventory shelf".
    retail_video = _is_retail_video(" ".join([str(user_brief or "")] + [str(s) for s in sentences]))
    for chunk in chunks:
        sentence = str(chunk.get("sentence") or "")
        chunk["queries"] = _strip_disallowed_cash_queries(chunk.get("queries"), sentence)
        chunk["metaphor_queries"] = _strip_disallowed_cash_queries(chunk.get("metaphor_queries"), sentence)
        retail_forbidden = _retail_forbidden_for_sentence(sentence) if retail_video else []
        if retail_forbidden:
            chunk["forbidden_visual_pack"] = _safe_forbidden_queries(
                _as_list(chunk.get("forbidden_visual_pack")) + retail_forbidden
            )[:24]
            _sanitize_must_not(chunk, {"forbidden_visual_pack": chunk["forbidden_visual_pack"]})
        retail_queries = _retail_queries_for_sentence(sentence) if retail_video else []
        if retail_queries:
            chunk["queries"] = _dedupe_queries(retail_queries + _safe_queries(chunk.get("queries")))[:8]
        chunk["must_show"] = _sentence_local_must_show(
            sentence,
            chunk.get("queries"),
            chunk.get("must_show"),
        )
        chunk["story_soft_fallback"] = _story_soft_fallback_sentence(sentence)

    video_meta["domain"] = domain_pack.get("domain") or ""
    video_meta["domain_visual_pack"] = list(domain_pack.get("domain_visual_pack") or [])
    video_meta["domain_motif_pool"] = list(domain_pack.get("domain_motif_pool") or [])
    video_meta["forbidden_visual_pack"] = list(domain_pack.get("forbidden_visual_pack") or [])
    video_meta["motif_pool"] = _safe_queries(motif_pool)[:8]
    if domain_pack.get("domain_motif_pool"):
        video_meta["motif_pool_source"] = "domain_pack"
    elif trusted_llm_pool:
        video_meta["motif_pool_source"] = "director_queries"
    elif default_pool_quarantined:
        video_meta["motif_pool_source"] = "subject_fallback"
    else:
        video_meta["motif_pool_source"] = str(
            video_meta.get("motif_pool_source") or "director"
        )
    video_meta["short_video_topic_anchor"] = subject
    degradation_reasons = _director_degradation_reasons(
        repaired,
        chunks,
        default_pool_quarantined=default_pool_quarantined,
    )
    video_meta["topic_anchor_source"] = topic_anchor_source
    video_meta["director_degraded"] = bool(degradation_reasons)
    video_meta["director_degradation_reasons"] = degradation_reasons
    video_meta["default_motif_pool_quarantined"] = default_pool_quarantined
    repaired["video_meta"] = video_meta
    repaired["chunks"] = chunks
    meta = dict(repaired.get("meta") or {})
    meta["short_video_visual_focus"] = True
    meta["short_video_visual_focus_version"] = "v2_domain_pack"
    meta["director_degraded"] = bool(degradation_reasons)
    meta["degradation_reasons"] = degradation_reasons
    repaired["meta"] = meta
    logger.info(
        "short_video_visual_focus topic_anchor=%s source=%s domain=%s "
        "director_degraded=%s default_pool_quarantined=%s",
        subject,
        topic_anchor_source,
        domain_pack.get("domain") or "",
        bool(degradation_reasons),
        default_pool_quarantined,
    )
    return repaired


def _extract_json_object(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _normalize_domain_pack(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    domain = _clean_domain(raw.get("domain"))
    topic_anchor = _clean_term(raw.get("topic_anchor")) or _clean_term(raw.get("topic"))
    visual_pack = _safe_domain_queries(raw.get("domain_visual_pack"))
    motif_pool = _safe_domain_queries(raw.get("domain_motif_pool")) or visual_pack[:6]
    forbidden = _safe_forbidden_queries(raw.get("forbidden_visual_pack"))
    if not visual_pack and not motif_pool:
        return {}
    return {
        "domain": domain or "custom",
        "topic_anchor": topic_anchor,
        "domain_visual_pack": visual_pack[:10],
        "domain_motif_pool": motif_pool[:8],
        "forbidden_visual_pack": forbidden[:10],
    }


def _prune_contradictory_forbidden(
    domain_pack: dict[str, Any],
    sentences: list[str],
    chunks: list[dict],
    user_brief: str,
) -> dict[str, Any]:
    if not domain_pack:
        return domain_pack
    story_text = " ".join(
        [user_brief or ""]
        + [str(s or "") for s in sentences or []]
        + [str(q) for ch in chunks for q in _as_list(ch.get("queries"))]
    ).lower()
    positive_money = {
        "money", "cash", "rupee", "rupees", "profit", "margin", "cost",
        "revenue", "income", "transaction", "transactions",
        "\u5362\u6bd4", "\u73b0\u91d1", "\u5229\u6da6", "\u6bdb\u5229", "\u6210\u672c",
    }
    positive_graph = {
        "chart", "charts", "graph", "graphs", "growth", "increase",
        "data visualization", "\u589e\u957f", "\u56fe\u8868", "\u589e\u957f\u56fe",
    }
    remove_needles: set[str] = set()
    if any(term in story_text for term in positive_money):
        remove_needles.update({"money", "cash", "transaction", "transactions", "rupee", "profit"})
    if any(term in story_text for term in positive_graph):
        remove_needles.update({"chart", "charts", "graph", "graphs", "data visualization"})
    if not remove_needles:
        return domain_pack
    kept = []
    for forbidden in _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack")):
        lower = forbidden.lower()
        if any(term in lower for term in remove_needles):
            continue
        kept.append(forbidden)
    out = dict(domain_pack)
    out["forbidden_visual_pack"] = kept
    return out


KNOWN_DOMAIN_NAMES = {
    "ancient_civilization",
    "ancient_mesopotamia",
    "food_cooking",
    "finance_economy",
    "general_topic",
    "biography_people",
    "business_work",
    "health_wellness",
    "history_maritime",
    "space_science",
    "technology_explainer",
    "society_lifestyle",
    "travel_geography",
    "weather_climate",
    "wildlife_science",
}


def _should_prefer_rule_domain(rule_pack: dict[str, Any], candidate_pack: dict[str, Any]) -> bool:
    rule_domain = str((rule_pack or {}).get("domain") or "")
    if not rule_domain or rule_domain == "general_topic":
        return False
    candidate_domain = str((candidate_pack or {}).get("domain") or "")
    if not candidate_pack:
        return True
    return _domain_is_malformed(candidate_domain) or candidate_domain not in KNOWN_DOMAIN_NAMES


def _plan_has_director_queries_allowed_for_domain(
    chunks: list[dict[str, Any]],
    domain_pack: dict[str, Any],
) -> bool:
    forbidden = _safe_forbidden_queries((domain_pack or {}).get("forbidden_visual_pack"))
    return any(
        _query_allowed_for_domain(
            q,
            domain_pack or {},
            forbidden,
            allow_universal=True,
        )
        for chunk in chunks
        if not _chunk_director_degraded(chunk)
        for q in _safe_queries(chunk.get("queries"))
    )


def _plan_queries_need_domain_repair(
    chunks: list[dict[str, Any]],
    domain_pack: dict[str, Any],
) -> bool:
    forbidden = _safe_forbidden_queries((domain_pack or {}).get("forbidden_visual_pack"))
    queries = [
        q
        for chunk in chunks
        for q in _safe_queries(chunk.get("queries"))
    ]
    return bool(queries) and any(
        not _query_allowed_for_domain(q, domain_pack or {}, forbidden)
        or _query_needs_domain_repair(q, domain_pack or {}, forbidden)
        for q in queries
    )


def _domain_is_malformed(domain: str) -> bool:
    domain = str(domain or "").strip().lower()
    if not domain:
        return True
    bad_fragments = ("com", "www", "http", "footage", "stock", "video", "clip", "broll")
    return any(fragment in domain for fragment in bad_fragments)


def _rule_domain_visual_pack(
    user_brief: str,
    sentences: list[str],
    subject: str,
) -> dict[str, Any]:
    text = " ".join([user_brief or "", " ".join(sentences or []), subject or ""]).lower()
    best_rule: dict[str, Any] | None = None
    best_score = 0
    for rule in DOMAIN_RULE_PACKS:
        hit_count = sum(1 for needle in rule["needles"] if _needle_in_text(text, str(needle)))
        if not hit_count:
            continue
        score = hit_count * 10 + int(rule.get("priority") or 0)
        if score > best_score:
            best_score = score
            best_rule = rule
    if best_rule is not None:
        return _normalize_domain_pack({
            "domain": best_rule["domain"],
            "topic_anchor": best_rule["topic_anchor"],
            "domain_visual_pack": best_rule["visual_pack"],
            "domain_motif_pool": best_rule["motif_pool"],
            "forbidden_visual_pack": best_rule["forbidden"],
        })
    if _has_any(text, {"cook", "recipe", "dish", "kitchen", "ingredient", "food", "\u7f8e\u98df", "\u98df\u8c31"}):
        return _normalize_domain_pack({
            "domain": "food_cooking",
            "topic_anchor": subject if subject not in BROAD_TOPIC_ANCHORS else "cooking",
            "domain_visual_pack": [
                "cooking ingredients",
                "kitchen food preparation",
                "fresh ingredients close up",
                "chef cooking",
                "food plating",
                "stir fry cooking",
            ],
            "domain_motif_pool": [
                "cooking ingredients",
                "kitchen food preparation",
                "fresh ingredients close up",
                "food plating",
            ],
            "forbidden_visual_pack": [
                "wild animal hunting",
                "data visualization",
                "ancient ruins",
                "space background",
            ],
        })
    if _has_any(text, {"space", "universe", "planet", "cosmic", "star", "\u5b87\u5b99", "\u592a\u7a7a"}):
        return _normalize_domain_pack({
            "domain": "space_science",
            "topic_anchor": subject if subject not in BROAD_TOPIC_ANCHORS else "outer space",
            "domain_visual_pack": [
                "outer space background",
                "galaxy stars",
                "planet orbit",
                "space nebula",
                "astronomy animation",
                "cosmic visualization",
            ],
            "domain_motif_pool": [
                "outer space background",
                "galaxy stars",
                "planet orbit",
                "space nebula",
            ],
            "forbidden_visual_pack": [
                "kitchen cooking",
                "modern city park",
                "forest leaf macro veins",
                "historic harbor",
            ],
        })
    if _has_any(text, {"data", "network", "ai", "technology", "digital", "\u6570\u636e", "\u7f51\u7edc", "\u79d1\u6280"}):
        return _normalize_domain_pack({
            "domain": "technology_explainer",
            "topic_anchor": subject if subject not in BROAD_TOPIC_ANCHORS else "technology",
            "domain_visual_pack": [
                "technology background",
                "digital network",
                "computer chip",
                "artificial intelligence interface",
                "data visualization",
                "robotics lab",
            ],
            "domain_motif_pool": [
                "technology background",
                "digital network",
                "computer chip",
                "data visualization",
            ],
            "forbidden_visual_pack": [
                "kitchen cooking",
                "ancient ruins",
                "wild animal close up",
                "historic harbor",
            ],
        })
    if (
        _has_any(text, FINANCE_DOMAIN_MARKERS)
        and not _has_any(text, BIOGRAPHY_PRIORITY_MARKERS)
    ):
        anchor = subject if subject and subject not in BROAD_TOPIC_ANCHORS else "personal finance"
        return _normalize_domain_pack({
            "domain": "finance_economy",
            "topic_anchor": anchor,
            "domain_visual_pack": [
                "household budget planning",
                "person reviewing monthly bills",
                "banking app close up",
                "financial chart analysis",
                "shopping receipt close up",
                "office worker calculating expenses",
                "stock market screen",
                "people making purchase decision",
            ],
            "domain_motif_pool": [
                "household budget planning",
                "person reviewing monthly bills",
                "banking app close up",
                "financial chart analysis",
                "shopping receipt close up",
            ],
            "forbidden_visual_pack": [
                "wild animal close up",
                "forest leaf macro veins",
                "ancient ruins",
                "space nebula",
            ],
        })
    if _has_any(text, BIOGRAPHY_DOMAIN_MARKERS):
        anchor = subject if subject and subject not in BROAD_TOPIC_ANCHORS else "biography story"
        return _normalize_domain_pack({
            "domain": "biography_people",
            "topic_anchor": anchor,
            "domain_visual_pack": [
                "archival portrait photograph",
                "person working at desk",
                "childhood family photographs",
                "professional career workplace",
                "public speech audience",
                "newspaper archive close up",
            ],
            "domain_motif_pool": [
                "archival portrait photograph",
                "person working at desk",
                "professional career workplace",
                "newspaper archive close up",
            ],
            "forbidden_visual_pack": [
                "wild animal close up",
                "forest leaf macro veins",
                "space nebula",
                "cooking ingredients",
            ],
        })
    if (
        _normalize_query(subject) in ANIMAL_ANCHORS
        or _has_any(text, {
            "animal", "wild", "wolf", "boar", "snake", "fox", "raven",
            "nature", "\u52a8\u7269", "\u91ce\u751f",
        })
    ):
        animal = subject if subject and subject not in BROAD_TOPIC_ANCHORS else "wildlife"
        return _normalize_domain_pack({
            "domain": "wildlife_science",
            "topic_anchor": animal,
            "domain_visual_pack": [
                f"{animal} footage",
                f"{animal} natural habitat",
                f"{animal} close shot",
                "forest landscape",
                "grassland wildlife",
                "wild prey animals",
            ],
            "domain_motif_pool": [
                f"{animal} footage",
                f"{animal} natural habitat",
                f"{animal} close shot",
                "forest landscape",
                "grassland wildlife",
            ],
            "forbidden_visual_pack": [
                "kitchen cooking",
                "food preparation",
                "data visualization",
                "modern city park",
            ],
        })
    return _normalize_domain_pack({
        "domain": "general_topic",
        "topic_anchor": subject,
        "domain_visual_pack": _default_motif_pool(user_brief, sentences),
        "domain_motif_pool": _default_motif_pool(user_brief, sentences),
        "forbidden_visual_pack": list(UNIVERSAL_BAD_FALLBACK_QUERIES),
    })


def _domain_motif_pool(domain_pack: dict[str, Any], fallback: list[str]) -> list[str]:
    domain_pool = _safe_domain_queries(domain_pack.get("domain_motif_pool"))
    forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
    domain = str(domain_pack.get("domain") or "")
    domain_first = domain in {"history_maritime", "ancient_civilization", "ancient_mesopotamia", "weather_climate"}
    if domain_first:
        return _dedupe_queries(domain_pool)[:8]
    filtered_fallback = [
        q for q in _safe_queries(fallback)
        if _query_allowed_for_domain(q, domain_pack, forbidden)
    ]
    return _dedupe_queries(domain_pool + filtered_fallback)[:8]


def _repair_queries_with_domain(
    queries: list[str],
    focus: str,
    sentence: str,
    term: str,
    subject: str,
    motif_pool: list[str],
    domain_pack: dict[str, Any],
) -> tuple[list[str], str, str]:
    if focus in POOL_FIRST_FOCUS:
        return [], "pool_first", "not_applicable"
    forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
    allowed = [
        q for q in _safe_queries(queries)
        if _query_allowed_for_domain(q, domain_pack, forbidden)
    ]
    domain = str(domain_pack.get("domain") or "")
    domain_first = domain in {"history_maritime", "ancient_civilization", "ancient_mesopotamia", "weather_climate"}
    needs_repair = domain_first or len(allowed) < 2 or any(
        _query_needs_domain_repair(q, domain_pack, forbidden) for q in _safe_queries(queries)[:3]
    )
    if not needs_repair:
        return allowed[:6], "short_video_repair", "ok"
    domain_queries = _domain_queries_for_sentence(sentence, term, subject, motif_pool, domain_pack)
    repaired = [
        q for q in _dedupe_queries(domain_queries + allowed)
        if _query_allowed_for_domain(q, domain_pack, forbidden)
    ]
    if len(repaired) < 2:
        repaired = _dedupe_queries(repaired + _safe_domain_queries(domain_pack.get("domain_visual_pack")))
    return repaired[:6], "domain_visual_pack", "domain_repaired"


def _repair_must_show_with_domain(
    must_show: Any,
    domain_pack: dict[str, Any],
) -> list[str]:
    """Keep hard visual requirements inside the current video's domain.

    Query repair can correctly rebuild the search terms, but later similar
    fallback also reads must_show. If a degenerate upstream plan leaves an
    off-domain hard subject here, the fallback can re-create bad generic
    searches such as wild footage for an archaeology video.
    """
    forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
    repaired: list[str] = []
    domain = str(domain_pack.get("domain") or "")
    domain_first = domain in {"history_maritime", "ancient_civilization", "ancient_mesopotamia", "weather_climate"}
    for raw in _as_list(must_show):
        term = _clean_term(raw)
        if not term:
            continue
        if not _query_allowed_for_domain(term, domain_pack, forbidden):
            continue
        if domain_first and not _term_matches_domain_pack(term, domain_pack):
            continue
        if term == _clean_term(domain_pack.get("topic_anchor")) and domain_first:
            continue
        if term:
            repaired.append(term)
    out: list[str] = []
    seen: set[str] = set()
    for term in repaired:
        key = _normalize_query(term)
        if key and key not in seen:
            seen.add(key)
            out.append(term)
    return out[:1]


def _term_matches_domain_pack(term: str, domain_pack: dict[str, Any]) -> bool:
    normalized = _normalize_query(term)
    if not normalized:
        return False
    pack = _safe_domain_queries(domain_pack.get("domain_visual_pack")) + _safe_domain_queries(
        domain_pack.get("domain_motif_pool")
    )
    for item in pack:
        item_normalized = _normalize_query(item)
        if normalized == item_normalized:
            return True
        if normalized in item_normalized or item_normalized in normalized:
            return True
    return False


def _domain_queries_for_sentence(
    sentence: str,
    term: str,
    subject: str,
    motif_pool: list[str],
    domain_pack: dict[str, Any],
) -> list[str]:
    text = str(sentence or "").lower()
    domain = str(domain_pack.get("domain") or "")
    if domain == "history_maritime":
        if any(x in text for x in ("\u8239\u961f", "\u5e06\u8239", "fleet", "ship")):
            return ["wooden sailing fleet", "ancient Chinese sailing ship", "large wooden ships at sea"]
        if any(x in text for x in ("\u6e2f\u53e3", "\u505c\u9760", "harbor", "port")):
            return ["historic harbor", "ancient port", "sailing ship in harbor"]
        if any(x in text for x in ("\u6c34\u624b", "\u5546\u4eba", "sailor", "merchant", "trade")):
            return ["sailors on wooden ship", "historic trade port", "maritime merchants"]
        if any(x in text for x in ("\u822a\u7ebf", "\u8fdc\u822a", "\u6d77", "route", "ocean")):
            return ["old maritime trade route", "ocean voyage", "old nautical map"]
    if domain == "ancient_civilization":
        if any(x in text for x in ("\u795e\u5e99", "\u91d1\u5b57\u5854", "temple", "pyramid")):
            return ["jungle temple", "ancient stone pyramid", "Mesoamerican temple"]
        if any(x in text for x in ("\u77f3\u523b", "\u96d5\u523b", "carving", "stone")):
            return ["stone carving", "ancient stone relief", "archaeological artifact"]
        if any(x in text for x in ("\u4e1b\u6797", "jungle", "forest")):
            return ["jungle ruins", "Mayan ruins in jungle", "ancient ruins in forest"]
    if domain == "ancient_mesopotamia":
        if any(x in text for x in ("\u6954\u5f62\u6587\u5b57", "\u4e66\u5199", "\u6ce5\u677f", "cuneiform", "clay tablet", "writing")):
            return ["cuneiform clay tablet", "clay tablet writing", "museum ancient artifact"]
        if any(x in text for x in ("\u795e\u8bdd", "\u795e\u660e", "\u53f2\u8bd7", "gilgamesh", "myth")):
            return ["ancient stone relief", "museum ancient artifact", "Mesopotamian ruins"]
        if any(x in text for x in ("\u72e9\u730e", "\u91c7\u96c6", "\u65e7\u77f3\u5668", "hunter", "gatherer", "stone tool")):
            return ["prehistoric hunter gatherer camp", "ancient stone tools", "archaeological excavation"]
        if any(x in text for x in ("\u4e24\u6cb3", "\u7f8e\u7d22\u4e0d\u8fbe\u7c73\u4e9a", "\u571f\u5730", "\u8d77\u6e90", "\u57ce\u5e02", "mesopotamia", "origin", "city")):
            return ["Mesopotamian ruins", "desert archaeological site", "ancient city ruins"]
        if any(x in text for x in ("\u79d1\u5b66", "\u6280\u672f", "\u6210\u5c31", "\u4ea4\u6613", "\u8d38\u6613", "science", "trade")):
            return ["cuneiform clay tablet", "ancient city ruins", "museum ancient artifact"]
        return ["Mesopotamian ruins", "ancient ziggurat", "cuneiform clay tablet", "desert archaeological site"]
    if domain == "weather_climate":
        if any(x in text for x in ("\u5317\u6781\u53d8\u6696", "\u5317\u6781", "arctic")):
            return ["Arctic warming climate change", "Arctic ice landscape", "weather satellite cloud system"]
        if any(x in text for x in ("\u6d77\u6d0b\u6e29\u5ea6", "\u6d77\u6d0b", "\u6d77\u6c34", "ocean")):
            return ["ocean temperature climate", "stormy ocean waves", "weather satellite cloud system"]
        if any(x in text for x in ("\u964d\u96ea", "\u66b4\u96ea", "\u98ce\u66b4", "snow", "storm", "blizzard")):
            return ["winter storm", "snowstorm", "blizzard", "snowfall"]
        if any(x in text for x in ("\u79d1\u5b66\u5bb6", "\u53d1\u73b0", "scientist", "research")):
            return ["weather satellite cloud system", "Arctic warming climate change", "climate change weather"]
        if any(x in text for x in ("\u5bd2\u6f6e", "\u6781\u5bd2", "\u51b7\u7a7a\u6c14", "\u6c14\u6e29", "cold")):
            return ["cold wave weather", "cold air outbreak", "icy city street", "snowfall"]
        return ["cold wave weather", "winter storm", "Arctic ice landscape", "storm clouds"]
    domain_pool = _safe_domain_queries(domain_pack.get("domain_motif_pool"))
    if domain_pool:
        return domain_pool[:4]
    if term and not _is_generic_query(term):
        return [f"{_query_term(term)} footage", f"{_query_term(term)} close shot"]
    if subject and not _is_generic_query(subject):
        return _subject_safe_broll_queries(subject)
    return _safe_queries(motif_pool)[:4]


def _infer_visual_focus(chunk: dict[str, Any], sentence: str, subject: str) -> str:
    st = str(chunk.get("sentence_type") or "").strip().lower()
    compact = re.sub(r"[\s,.!?;:，。！？；：、]+", "", sentence)
    lower = _focus_text(sentence, chunk)
    if st in {"cta", "transition"} or len(compact) <= 4:
        return "transition"
    if subject and _contains_subject(lower, subject):
        return "topic_subject"
    if _contains_action_or_process(lower):
        return "action_or_process"
    if _contains_object_or_detail(lower):
        return "object_or_detail"
    if _contains_context_scene(lower):
        return "context_scene"
    if _looks_abstract(lower, chunk):
        return "metaphor_broll"
    if _first_concrete_term(chunk):
        return "topic_subject"
    return "metaphor_broll"


def _term_for_focus(
    focus: str,
    sentence: str,
    chunk: dict[str, Any],
    subject: str,
    motif_pool: list[str],
) -> str:
    lower = _focus_text(sentence, chunk)
    if focus == "topic_subject":
        return subject or _first_concrete_term(chunk) or _first_motif_term(motif_pool)
    if focus in FOCUS_KEYWORDS:
        found = _first_focus_term(lower, focus)
        if found:
            if found == "food" and subject and _ecology_food_chain_context(lower):
                return "wild prey animals"
            return found
    if focus == "metaphor_broll":
        return ""
    if focus == "transition":
        return ""
    return _first_concrete_term(chunk) or _first_motif_term(motif_pool)


def _must_show_for_focus(focus: str, term: str, subject: str) -> list[str]:
    if focus in POOL_FIRST_FOCUS:
        return []
    cleaned = _clean_term(term)
    if not cleaned or cleaned in ABSTRACT_TERMS:
        return []
    if focus != "topic_subject" and subject:
        head = subject.split()[0]
        if cleaned == subject or cleaned == head:
            return []
    return [cleaned]


def _queries_for_focus(
    focus: str,
    term: str,
    subject: str,
    motif_pool: list[str],
    original: Any,
) -> list[str]:
    if focus in POOL_FIRST_FOCUS:
        return []
    term = _query_term(term or subject or "")
    generated: list[str]
    if focus == "topic_subject":
        generated = _topic_subject_queries(term)
    elif focus == "action_or_process":
        generated = _subject_contextual_queries(subject, term, ["process", "close up"])
    elif focus == "context_scene":
        generated = _subject_contextual_queries(subject, term, ["wide shot", "background"])
    elif focus == "object_or_detail":
        generated = _subject_contextual_queries(subject, term, ["close up", "detail"])
    else:
        generated = []
    return _dedupe_queries(generated + _safe_queries(original)[:2] + _safe_queries(motif_pool)[:2])[:6]


def _topic_subject_queries(term: str) -> list[str]:
    normalized = _normalize_query(term)
    if _anchor_in(normalized, FOOD_ANCHORS):
        return [f"{term} cooking", f"{term} close up", f"{term} food preparation"]
    if _anchor_in(normalized, SPACE_ANCHORS):
        return [f"{term} animation", f"{term} space background", f"{term} visualization"]
    if _anchor_in(normalized, TECH_ANCHORS):
        return [f"{term} technology", f"{term} animation", f"{term} digital background"]
    if _anchor_in(normalized, HISTORY_ANCHORS):
        return [f"{term} historical scene", f"{term} ancient architecture", f"{term} museum artifact"]
    if _anchor_in(normalized, NATURAL_DISASTER_ANCHORS):
        return [f"{term} footage", f"{term} landscape", f"{term} natural disaster"]
    return [f"{term} footage", f"{term} close shot", f"{term} natural scene"]


def _metaphor_queries_for_focus(
    focus: str,
    term: str,
    subject: str,
    motif_pool: list[str],
    original: Any,
    *,
    domain_pack: dict[str, Any] | None = None,
) -> list[str]:
    base = _safe_queries(original)
    domain_pack = domain_pack or {}
    forbidden = _safe_forbidden_queries(domain_pack.get("forbidden_visual_pack"))
    if focus in POOL_FIRST_FOCUS:
        candidates: list[str]
        if not _content_domain_packs_enabled() and _safe_queries(motif_pool):
            # LLM director: lead with the video's OWN concrete visual vocabulary
            # (built from the LLM's per-sentence queries) so abstract / over-split
            # sentences still get distinct, on-subject B-roll (e.g. "Pantheon dome
            # interior") instead of a generic "<subject> footage" clip leading.
            candidates = _safe_queries(motif_pool) + (
                _subject_safe_broll_queries(subject) if subject else []
            )
        elif subject:
            candidates = _subject_safe_broll_queries(subject) + _safe_queries(motif_pool)[:2]
        else:
            candidates = _safe_queries(motif_pool)[:4]
        allowed = [
            q for q in _dedupe_queries(candidates)
            if _query_allowed_for_domain(q, domain_pack, forbidden)
        ]
        if allowed:
            return allowed[:5]
        return _safe_domain_queries(domain_pack.get("domain_motif_pool"))[:5]
    if focus == "topic_subject" and subject:
        base.extend([f"{subject} environment", f"{subject} natural background"])
    elif term:
        base.extend([f"{term} background", f"{term} atmosphere"])
    base.extend(_safe_queries(motif_pool)[:2])
    allowed = [
        q for q in _dedupe_queries(base)
        if _query_allowed_for_domain(q, domain_pack, forbidden)
    ]
    if allowed:
        return allowed[:5]
    return _safe_domain_queries(domain_pack.get("domain_motif_pool"))[:5]


def _subject_contextual_queries(subject: str, term: str, suffixes: list[str]) -> list[str]:
    """Prefer the local visual target, but keep the video's subject attached.

    This prevents a wildlife short from turning "food chain" or "habitat" lines
    into kitchen/anonymous-landscape footage while still allowing cooking,
    product, space, and history topics to use their own subject anchors.
    """
    term = _query_term(term)
    subject = _query_term(subject) if subject else ""
    out: list[str] = []
    if subject and not _is_generic_query(subject):
        out.extend([f"{subject} {term}", f"{subject} natural habitat"])
    out.append(term)
    out.extend(f"{term} {suffix}" for suffix in suffixes)
    return _dedupe_queries(out)


def _subject_safe_broll_queries(subject: str) -> list[str]:
    subject = _query_term(subject)
    if _is_generic_query(subject):
        return []
    if _anchor_in(subject, FOOD_ANCHORS):
        return [f"{subject} cooking", f"{subject} close up", "kitchen food preparation"]
    if _anchor_in(subject, SPACE_ANCHORS):
        return [f"{subject} animation", f"{subject} space background", "outer space background"]
    if _anchor_in(subject, TECH_ANCHORS):
        return [f"{subject} technology", f"{subject} digital background", "technology background"]
    if _anchor_in(subject, HISTORY_ANCHORS):
        return [f"{subject} historical scene", f"{subject} museum artifact", "ancient architecture"]
    if _anchor_in(subject, NATURAL_DISASTER_ANCHORS):
        return [f"{subject} footage", f"{subject} landscape", "natural disaster footage"]
    return [f"{subject} footage", f"{subject} natural habitat", f"{subject} close shot"]


def _anchor_from_director_output(chunks: list[dict[str, Any]]) -> str:
    """从导演(LLM)已产出的英文 must_show/queries 里提取最高频主体名词短语。

    这是锚点的**第一优先来源**:导演逐句写搜索词时已经理解对了主体
    (如"北美浣熊"→ 22/22 镜头都是 raccoon),从它的产出计数比静态中文表
    可靠得多。逻辑与旧 _derive_topic_anchor 的 chunk 计数段逐字一致,
    仅抽成独立函数供新旧两条路径共用。拿不到返回 ""。
    """
    counts: Counter[str] = Counter()
    for chunk in chunks:
        if _chunk_director_degraded(chunk):
            continue
        for raw in _as_list(chunk.get("must_show")):
            term = _clean_term(raw)
            if _term_is_anchor(term):
                counts[term] += 2
        for raw in _as_list(chunk.get("queries"))[:2]:
            for term in _query_terms(raw):
                if _term_is_anchor(term):
                    counts[term] += 1
    if not counts:
        return ""
    top = counts.most_common(1)[0][0]
    # 精确物种优先:最高频锚点若是泛单词(如 "frog"),但它其实是某个更具体的物种短语
    # ("horned frog")的中心词 → 返回**具体短语**,不要塌成泛词。否则 subject="frog"
    # 会污染所有兜底/motif/broll 搜索 → 搜出一堆普通青蛙,主角角蛙反而不出现。
    if " " not in top:
        phrase_hits: Counter[str] = Counter()
        for chunk in chunks:
            if _chunk_director_degraded(chunk):
                continue
            raws = list(_as_list(chunk.get("must_show"))) + list(_as_list(chunk.get("queries"))[:2])
            for raw in raws:
                term = _clean_term(raw)
                words = term.split()
                if 2 <= len(words) <= 3 and words[-1] == top and _term_is_anchor(term):
                    phrase_hits[term] += 1
        if phrase_hits:
            return phrase_hits.most_common(1)[0][0]
    return top


def _subject_phrase_counts_longest(text: str) -> Counter[str]:
    """SUBJECT_PHRASES 兜底匹配 —— CJK 最长词优先 + 命中即消费。

    旧 bug 的机制:CJK 走纯子串匹配,"北美浣熊"命中单字条目"熊"→bear。
    规则级修法:按词条长度降序匹配,CJK 命中后把该片段从文本中消费掉,
    短词就再也吃不到长词的一部分("浣熊"消费后单字"熊"无从命中);
    "棕熊"这类表里没有更长条目的词仍正常落到"熊"→bear,不误伤。
    ASCII 词条保持原有词边界匹配(本就没有子串劫持问题)。
    每个词条按"出现与否"计 1(与 _subject_phrase_counts 口径一致)。
    """
    counts: Counter[str] = Counter()
    remaining = str(text or "").lower()
    ordered = sorted(SUBJECT_PHRASES, key=lambda item: len(item[0]), reverse=True)
    for needle, canonical in ordered:
        n = needle.lower()
        if not n:
            continue
        if not n.isascii():
            if n in remaining:
                counts[canonical] += 1
                remaining = remaining.replace(n, " ")
        elif _needle_in_text(remaining, n):
            counts[canonical] += 1
    return counts


def _derive_topic_anchor(
    chunks: list[dict[str, Any]],
    sentences: list[str],
    user_brief: str,
    motif_pool: list[str],
) -> str:
    return _derive_topic_anchor_with_source(
        chunks, sentences, user_brief, motif_pool,
    )[0]


def _derive_topic_anchor_with_source(
    chunks: list[dict[str, Any]],
    sentences: list[str],
    user_brief: str,
    motif_pool: list[str],
) -> tuple[str, str]:
    """推导整条视频的 topic_anchor(判官 main_subject / motif 兜底池 / FLUX prompt 同源)。

    新优先级(MB_ANCHOR_FROM_DIRECTOR=1,默认):
      ① 导演(LLM)英文产出(chunks 的 must_show/queries)——调用时机在
         build_global_plan 之后,导演产出已在手,无需延后/回填;
      ② 静态中文表 SUBJECT_PHRASES 兜底,最长词优先+命中消费;
      ③ 都拿不到 → motif_pool 提词(保持现状行为)。
    设 MB_ANCHOR_FROM_DIRECTOR=0 回退旧逻辑(中文表先行的老路径)。
    """
    if not _anchor_from_director_enabled():
        return _derive_topic_anchor_legacy(chunks, sentences, user_brief, motif_pool), "legacy"
    # 零售特例保持旧优先级:brief 明确便利店/超市时锚点固定,下游硬编码依赖它。
    brief_counts = _subject_phrase_counts_longest(user_brief)
    for preferred in ("convenience store shelf", "supermarket shelf"):
        if preferred in brief_counts:
            return preferred, "brief_dictionary"
    # ① 导演英文产出优先。
    director_anchor = _anchor_from_director_output(chunks)
    if director_anchor:
        return director_anchor, "director"
    # ② 静态中文表兜底(最长词优先):先看 user_brief,再看全文。
    if brief_counts:
        brief_top = brief_counts.most_common(1)[0][0]
        if brief_top not in BROAD_TOPIC_ANCHORS:
            return brief_top, "brief_dictionary"
    joined = " ".join([user_brief, " ".join(sentences)]).lower()
    phrase_counts = _subject_phrase_counts_longest(joined)
    if phrase_counts:
        for preferred in ("convenience store shelf", "supermarket shelf"):
            if preferred in phrase_counts:
                return preferred, "script_dictionary"
        return phrase_counts.most_common(1)[0][0], "script_dictionary"
    # ③ 保持现状:motif_pool 里提第一个可用词。
    for query in motif_pool:
        for term in _query_terms(query):
            if _term_is_anchor(term):
                return term, "motif_pool"
    return "", "none"


def _derive_topic_anchor_legacy(
    chunks: list[dict[str, Any]],
    sentences: list[str],
    user_brief: str,
    motif_pool: list[str],
) -> str:
    """旧版锚点推导(MB_ANCHOR_FROM_DIRECTOR=0 时的回退路径,行为逐字保留)。

    已知缺陷(也是保留它当回退的原因——行为可预期):中文表纯子串匹配,
    "北美浣熊"会命中单字"熊"→bear。默认路径见 _derive_topic_anchor。
    """
    brief_counts = _subject_phrase_counts(user_brief)
    if brief_counts:
        for preferred in ("convenience store shelf", "supermarket shelf"):
            if preferred in brief_counts:
                return preferred
        brief_top = brief_counts.most_common(1)[0][0]
        if brief_top not in BROAD_TOPIC_ANCHORS:
            return brief_top
    joined = " ".join([user_brief, " ".join(sentences)]).lower()
    phrase_counts: Counter[str] = Counter()
    for needle, canonical in SUBJECT_PHRASES:
        hits = joined.count(needle.lower())
        if hits:
            phrase_counts[canonical] += hits
    if phrase_counts:
        for preferred in ("convenience store shelf", "supermarket shelf"):
            if preferred in phrase_counts:
                return preferred
        top, hits = phrase_counts.most_common(1)[0]
        if hits >= 2:
            return top
    director_anchor = _anchor_from_director_output(chunks)
    if phrase_counts and not director_anchor:
        return phrase_counts.most_common(1)[0][0]
    if director_anchor:
        return director_anchor
    if phrase_counts:
        return phrase_counts.most_common(1)[0][0]
    for query in motif_pool:
        for term in _query_terms(query):
            if _term_is_anchor(term):
                return term
    return ""


def _default_motif_pool(user_brief: str, sentences: list[str]) -> list[str]:
    text = " ".join([user_brief, " ".join(sentences)]).lower()
    if _has_any(text, {"cook", "recipe", "dish", "kitchen", "ingredient", "food"}):
        return ["cooking ingredients", "kitchen food preparation", "fresh ingredients"]
    if _has_any(text, {"animal", "wild", "wolf", "boar", "snake", "nature"}) or _subject_phrase_counts(text):
        return ["wildlife nature", "natural habitat", "forest landscape"]
    if _has_any(text, {"space", "universe", "planet", "cosmic", "star"}):
        return ["outer space", "galaxy stars", "planet orbit"]
    if _has_any(text, {"data", "network", "ai", "technology", "digital"}):
        return ["technology background", "data visualization", "digital network"]
    return ["atmospheric b roll", "wide landscape", "abstract background"]


def _contains_subject(text: str, subject: str) -> bool:
    if not subject:
        return False
    head = subject.split()[0]
    if subject in text or (len(head) >= 3 and head in text):
        return True
    return any(alias and alias in text for alias in _subject_aliases(subject))


def _contains_action_or_process(text: str) -> bool:
    return _first_focus_term(text, "action_or_process") != ""


def _contains_context_scene(text: str) -> bool:
    return _first_focus_term(text, "context_scene") != ""


def _contains_object_or_detail(text: str) -> bool:
    return _first_focus_term(text, "object_or_detail") != ""


def _needle_in_text(text: str, needle: str) -> bool:
    text = str(text or "").lower()
    needle = str(needle or "").lower()
    if not text or not needle:
        return False
    if not needle.isascii():
        return needle in text
    if len(needle) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}[a-z0-9-]*", text) is not None


def _first_focus_term(text: str, focus: str) -> str:
    for needle, term in FOCUS_KEYWORDS.get(focus, {}).items():
        if _needle_in_text(text, needle):
            return term
    return ""


def _ecology_food_chain_context(text: str) -> bool:
    ecology_terms = {
        "food chain",
        "prey",
        "predator",
        "predation",
        "\u98df\u7269\u94fe",
        "\u6355\u98df",
        "\u730e\u7269",
    }
    return any(term in text for term in ecology_terms)


def _looks_abstract(text: str, chunk: dict[str, Any]) -> bool:
    if str(chunk.get("sentence_type") or "").lower() in {"emotion", "abstract", "summary"}:
        return True
    if any(term in text for term in ABSTRACT_TERMS):
        return True
    concrete = _first_concrete_term(chunk)
    return not concrete


def _focus_text(sentence: str, chunk: dict[str, Any]) -> str:
    return " ".join([
        str(sentence or ""),
        " ".join(str(v) for v in _as_list(chunk.get("must_show"))),
        " ".join(str(v) for v in _as_list(chunk.get("queries"))[:3]),
    ]).lower()


def _first_concrete_term(chunk: dict[str, Any]) -> str:
    for raw in _as_list(chunk.get("must_show")) + _as_list(chunk.get("queries")):
        for term in _query_terms(raw):
            if _term_is_anchor(term):
                return term
    return ""


def _first_motif_term(motif_pool: list[str]) -> str:
    for raw in motif_pool:
        for term in _query_terms(raw):
            if _term_is_anchor(term):
                return term
    return ""


# Words ending in "s" that are NOT plurals — naive de-pluralization used to
# mangle them (glass→glas, lens→len, bus→bu), corrupting subject/anchor terms.
_NON_PLURAL_S = frozenset({
    "glass", "grass", "class", "brass", "press", "dress", "lens", "bus",
    "gas", "focus", "virus", "status", "bonus", "campus", "series", "species",
    "news", "physics", "ethics", "lays", "plus", "boss", "loss", "cross",
})


def _query_terms(raw: Any) -> list[str]:
    text = str(raw or "").lower()
    terms = []
    for token in re.findall(r"[a-z][a-z-]{2,}", text):
        token = token.strip("-")
        if token in QUERY_STOPWORDS or token in ABSTRACT_TERMS or token in GENERIC_BAD_WORDS:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = f"{token[:-3]}y"
        elif (
            token.endswith("s")
            and len(token) > 4
            and not token.endswith(("ss", "us", "is"))  # glass/grass, focus/virus, axis
            and token not in _NON_PLURAL_S
        ):
            token = token[:-1]
        if token not in terms:
            terms.append(token)
    return terms[:4]


def _term_is_anchor(term: str) -> bool:
    normalized = _normalize_query(term)
    return bool(
        normalized
        and normalized not in QUERY_STOPWORDS
        and normalized not in ABSTRACT_TERMS
        and not _is_generic_query(normalized)
        and len(normalized) >= 3
    )


def _clean_term(raw: Any) -> str:
    text = " ".join(str(raw or "").strip().lower().split())
    text = re.sub(r"[^a-z0-9 -]", "", text)
    if _is_generic_query(text):
        return ""
    words = [
        w for w in text.split()
        if w not in QUERY_STOPWORDS and w not in ABSTRACT_TERMS and w not in GENERIC_BAD_WORDS
    ]
    cleaned = " ".join(words[:3])
    return "" if _is_generic_query(cleaned) else cleaned


def _query_term(raw: Any) -> str:
    cleaned = _clean_term(raw)
    return cleaned or "stock footage"


def _safe_queries(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for raw in values:
        text = " ".join(str(raw or "").strip().split())
        if not text or not text.isascii():
            continue
        if len(re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)) < 2:
            continue
        if _is_generic_query(text):
            continue
        out.append(text)
    return _dedupe_queries(out)


def _safe_domain_queries(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = " ".join(str(raw or "").strip().split())
        if not text or not text.isascii():
            continue
        if len(re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)) < 2:
            continue
        normalized = _normalize_query(text)
        if normalized in GENERIC_BAD_ANCHORS and normalized != "data visualization":
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(text)
    return out


def _safe_forbidden_queries(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = " ".join(str(raw or "").strip().lower().split())
        if text and text.isascii() and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _clean_domain(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:48]


def _query_needs_domain_repair(
    query: Any,
    domain_pack: dict[str, Any],
    forbidden: list[str],
    *,
    allow_universal: bool = False,
) -> bool:
    q = _normalize_query(query)
    if not q:
        return True
    domain = str(domain_pack.get("domain") or "")
    if q == "data visualization" and domain in {"technology_explainer", "space_science"}:
        return False
    if q in UNIVERSAL_BAD_FALLBACK_QUERIES and not allow_universal:
        return True
    if q in GENERIC_BAD_ANCHORS:
        return True
    if any(term and term in q for term in forbidden):
        return True
    if q == "data visualization" and domain not in {"technology_explainer", "space_science"}:
        return True
    if q in {"ancient historical scene", "ancient ancient architecture"}:
        return True
    if domain in {"history_maritime", "ancient_civilization", "ancient_mesopotamia"}:
        # Historical "hunter-gatherer" narration is valid, but it must resolve
        # to human archaeology visuals, not wildlife/predator hunting footage.
        if "hunter gatherer" in q or "stone tools" in q or "archaeological excavation" in q:
            return False
        if "wild hunting" in q or "natural habitat" in q:
            return True
    if domain == "weather_climate":
        weather_markers = (
            "cold wave", "cold snap", "extreme cold", "cold air",
            "temperature drop", "winter storm", "snowstorm", "blizzard",
            "snowfall", "arctic", "ice landscape", "icy city",
            "storm cloud", "stormy ocean", "ocean temperature",
            "weather satellite", "cloud system", "climate change weather",
            "extreme weather",
        )
        if any(marker in q for marker in weather_markers):
            return False
        off_domain_markers = (
            "scientific research", "laboratory", "microscope", "chemistry",
            "biotechnology", "pharmacy", "volcano", "lava", "polar bear",
            "wild animal", "ancient ruins", "kitchen cooking",
        )
        if any(marker in q for marker in off_domain_markers):
            return True
    return False


def _query_allowed_for_domain(
    query: Any,
    domain_pack: dict[str, Any],
    forbidden: list[str] | None = None,
    *,
    allow_universal: bool = False,
) -> bool:
    q = _normalize_query(query)
    if not q:
        return False
    forbidden = forbidden if forbidden is not None else _safe_forbidden_queries(
        domain_pack.get("forbidden_visual_pack")
    )
    if any(term and term in q for term in forbidden):
        return False
    if _query_needs_domain_repair(
        q, domain_pack, forbidden, allow_universal=allow_universal,
    ):
        return False
    return True


def _dedupe_queries(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        q = " ".join(str(value or "").strip().split())
        key = q.lower()
        if not q or key in seen or not q.isascii():
            continue
        if len(re.findall(r"[A-Za-z][A-Za-z0-9-]*", q)) < 2:
            continue
        if _is_generic_query(q):
            continue
        seen.add(key)
        out.append(q)
    return out


def _subject_phrase_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    lower = str(text or "").lower()
    for needle, canonical in SUBJECT_PHRASES:
        hits = 1 if _needle_in_text(lower, needle.lower()) else 0
        if hits:
            counts[canonical] += hits
    return counts


def _subject_aliases(subject: str) -> list[str]:
    return [needle.lower() for needle, canonical in SUBJECT_PHRASES if canonical == subject]


def _normalize_query(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().replace("_", " ").split())


def _is_generic_query(text: Any) -> bool:
    normalized = _normalize_query(text)
    if not normalized:
        return True
    if normalized in GENERIC_BAD_ANCHORS:
        return True
    tokens = re.findall(r"[a-z][a-z-]{1,}", normalized)
    return bool(tokens) and all(
        token in GENERIC_BAD_WORDS or token in QUERY_STOPWORDS for token in tokens
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _has_any(text: str, terms: set[str]) -> bool:
    return any(_needle_in_text(text, term) for term in terms)


def _anchor_in(term: str, anchors: set[str]) -> bool:
    return any(anchor in term or term in anchor for anchor in anchors)


def _sanitize_must_not(chunk: dict[str, Any], domain_pack: dict[str, Any] | None = None) -> None:
    must_not = []
    raw_values: list[Any] = []
    if domain_pack:
        raw_values.extend(_as_list(domain_pack.get("forbidden_visual_pack")))
    raw_values.extend(_as_list(chunk.get("must_not_show")))
    seen: set[str] = set()
    for raw in raw_values:
        text = " ".join(str(raw or "").strip().split())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            must_not.append(text)
    chunk["must_not_show"] = must_not[:12]
