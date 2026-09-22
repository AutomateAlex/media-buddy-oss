"""Long-video-only visual planning repair.

The shared global director can still be used as a rough planner, but long
videos need a second pass that decides what each sentence should *show* before
stock search starts. This keeps long-video behavior isolated from the short
video orchestrator.
"""
from __future__ import annotations

import copy
import os
import re
from collections import Counter
from typing import Any

from backend.services.global_director import build_global_plan


VISUAL_FOCUS_VALUES = {
    "topic_subject",
    "habitat",
    "consequence",
    "prey_or_livestock",
    "human_impact",
    "abstract_broll",
    "transition",
}

POOL_FIRST_FOCUS = {"abstract_broll", "transition"}

ABSTRACT_MUST_SHOW_TERMS = {
    "truth", "fear", "balance", "ecosystem balance", "human dominance",
    "ripple effects", "civilization", "meaning", "order", "chaos",
    "conflict", "wisdom", "danger", "transformation", "mystery",
    "respect", "biodiversity", "interconnected nature", "family bonds",
    "recovery", "metaphor", "concept", "idea", "emotion", "feeling",
    "真相", "恐惧", "平衡", "生态平衡", "文明", "意义", "秩序", "混乱",
    "冲突", "智慧", "危险", "神秘", "尊重", "多样性", "情绪",
}

GENERIC_ANCHOR_STOPWORDS = {
    "black", "white", "blue", "green", "red", "dark", "light", "close",
    "wide", "slow", "fast", "wild", "animal", "animals", "nature",
    "background", "scene", "shot", "footage", "video", "abstract",
}

QUERY_BANNED_PHRASES = {
    "ecosystem balance",
    "human dominance",
    "ripple effects",
    "truth close up",
    "fear close up",
    "balance close up",
    "conflict close up",
    "pack hunting coordination",
    "hunting coordination",
}

ABSTRACT_QUERY_HEADS = {
    "truth", "fear", "balance", "conflict", "civilization", "meaning",
    "order", "chaos", "wisdom", "danger", "transformation", "mystery",
}

SUBJECT_PHRASES = [
    ("black mamba", "black mamba snake"),
    ("黑曼巴", "black mamba snake"),
    ("termite", "termite"),
    ("white ant", "termite"),
    ("白蚁", "termite"),
    ("wolf", "wolf"),
    ("wolves", "wolf"),
    ("狼", "wolf"),
    ("bamboo", "bamboo"),
    ("竹", "bamboo"),
    ("fractal", "fractal pattern"),
    ("分形", "fractal pattern"),
    ("dimension", "geometric dimension"),
    ("维度", "geometric dimension"),
    ("recipe", "cooking ingredients"),
    ("cooking", "cooking ingredients"),
    ("food", "food preparation"),
    ("食谱", "cooking ingredients"),
    ("美食", "food preparation"),
    ("做菜", "cooking ingredients"),
    ("universe", "cosmos space"),
    ("cosmos", "cosmos space"),
    ("galaxy", "galaxy space"),
    ("宇宙", "cosmos space"),
    ("星系", "galaxy space"),
]

FOCUS_TERMS = {
    "habitat": [
        ("forest", "forest landscape"),
        ("woods", "forest landscape"),
        ("jungle", "jungle landscape"),
        ("mountain", "mountain landscape"),
        ("river", "river landscape"),
        ("lake", "lake landscape"),
        ("ocean", "ocean landscape"),
        ("sea", "ocean landscape"),
        ("grassland", "grassland landscape"),
        ("savanna", "savanna landscape"),
        ("desert", "desert landscape"),
        ("snow", "snowy landscape"),
        ("ice", "icy landscape"),
        ("wilderness", "wilderness landscape"),
        ("space", "space background"),
        ("cosmos", "cosmos space"),
        ("galaxy", "galaxy space"),
        ("森林", "forest landscape"),
        ("山林", "mountain forest"),
        ("山", "mountain landscape"),
        ("河流", "river landscape"),
        ("草原", "grassland landscape"),
        ("荒野", "wilderness landscape"),
        ("沙漠", "desert landscape"),
        ("雪", "snowy landscape"),
        ("冰", "icy landscape"),
        ("宇宙", "cosmos space"),
        ("星空", "starry sky"),
        ("星系", "galaxy space"),
    ],
    "consequence": [
        ("overgrazed", "overgrazed vegetation"),
        ("vegetation", "vegetation close up"),
        ("barren", "barren landscape"),
        ("damaged", "damaged landscape"),
        ("erosion", "soil erosion"),
        ("collapse", "collapsed ecosystem landscape"),
        ("recovery", "nature recovery"),
        ("population", "animal herd"),
        ("flood", "flood damage"),
        ("fire", "wildfire damage"),
        ("泛滥", "animal herd"),
        ("植被", "vegetation close up"),
        ("啃光", "overgrazed vegetation"),
        ("崩塌", "damaged landscape"),
        ("恢复", "nature recovery"),
        ("消失", "empty landscape"),
        ("破坏", "damaged landscape"),
        ("侵蚀", "soil erosion"),
        ("枯萎", "withered plants"),
    ],
    "prey_or_livestock": [
        ("deer", "deer herd"),
        ("cattle", "cattle pasture"),
        ("cow", "cattle pasture"),
        ("sheep", "sheep grazing"),
        ("livestock", "livestock grazing"),
        ("pasture", "pasture landscape"),
        ("farm", "farm animals"),
        ("prey", "wild animals"),
        ("bird", "birds wildlife"),
        ("mammal", "small mammal wildlife"),
        ("ingredient", "cooking ingredients close up"),
        ("food", "food preparation"),
        ("kitchen", "kitchen food preparation"),
        ("recipe", "cooking ingredients close up"),
        ("鹿", "deer herd"),
        ("牛", "cattle pasture"),
        ("羊", "sheep grazing"),
        ("家畜", "livestock grazing"),
        ("猎物", "wild animals"),
        ("牧场", "pasture landscape"),
        ("农场", "farm animals"),
        ("食材", "cooking ingredients close up"),
        ("厨房", "kitchen food preparation"),
        ("食物", "food preparation"),
    ],
    "human_impact": [
        ("human", "human activity"),
        ("people", "people outdoors"),
        ("city", "city landscape"),
        ("road", "road through nature"),
        ("logging", "logging forest"),
        ("deforestation", "deforestation"),
        ("farmland", "farmland expansion"),
        ("pasture", "pasture fence"),
        ("fence", "fence wilderness"),
        ("construction", "construction landscape"),
        ("pollution", "pollution environment"),
        ("urban", "urban expansion"),
        ("人类", "human activity"),
        ("城市", "city landscape"),
        ("道路", "road through nature"),
        ("砍伐", "logging forest"),
        ("伐木", "logging forest"),
        ("牧场", "pasture fence"),
        ("农田", "farmland expansion"),
        ("栅栏", "fence wilderness"),
        ("扩张", "urban expansion"),
        ("建设", "construction landscape"),
        ("污染", "pollution environment"),
        ("侵占", "farmland expansion"),
    ],
}


def build_long_video_global_plan(
    llm: Any,
    sentences: list[str],
    *,
    user_brief: str = "",
) -> dict[str, Any]:
    """Build and repair a global plan for long videos only."""
    # harden_subject_anchor=False:借鉴短视频——绝不把主体词硬塞进每一条 query
    # (开着会把"万神殿穹顶"洗成"rome 万神殿"、把 query 全塌成一个词)。主体一致性
    # 现在由判官 + must_show 地板兜底,导演不需要再暴力强塞。
    base = build_global_plan(
        llm,
        sentences,
        user_brief=user_brief,
        use_segmented=True,
        harden_subject_anchor=False,
    )
    return repair_long_video_plan(base, sentences=sentences, user_brief=user_brief)


def repair_long_video_plan(
    plan: dict[str, Any],
    *,
    sentences: list[str] | None = None,
    user_brief: str = "",
) -> dict[str, Any]:
    """Undo topic-anchor collapse and add visual_focus for every chunk."""
    repaired = copy.deepcopy(plan or {})
    chunks = list(repaired.get("chunks") or [])
    video_meta = dict(repaired.get("video_meta") or {})
    motif_pool = _safe_queries(video_meta.get("motif_pool")) or [
        "nature landscape",
        "atmospheric b roll",
        "wide landscape",
    ]
    topic_anchor = _derive_topic_anchor(chunks, sentences or [], user_brief, motif_pool)
    collapse = _topic_anchor_collapse_detected(chunks, topic_anchor)
    # 借鉴短视频:信任 LLM(segmented expand)逐句拆出来的具体 query,不再用
    # topic_anchor + 写死的动物词("{term} wildlife/natural habitat")覆盖它。
    # 旧的强塞逻辑对非动物题材(金融/人物)是灾难(帕布莱→每句都"financial wildlife")。
    # 主体一致性交给判官 + must_show 地板。回滚:MEDIA_BUDDY_LONG_DIRECTOR_TRUST_LLM=0。
    trust_llm = os.environ.get(
        "MEDIA_BUDDY_LONG_DIRECTOR_TRUST_LLM", "1",
    ).strip().lower() not in ("0", "false", "no", "off", "")

    for idx, chunk in enumerate(chunks):
        if "chunk_index" not in chunk:
            chunk["chunk_index"] = idx
        sentence = str(
            chunk.get("sentence")
            or chunk.get("text")
            or (sentences[idx] if sentences and idx < len(sentences) else "")
        )
        chunk["sentence"] = sentence
        if trust_llm:
            llm_q = _safe_queries(chunk.get("queries"))
            llm_show = [str(s).strip() for s in _as_list(chunk.get("must_show"))
                        if str(s).strip()][:2]
            if not llm_q:
                # LLM 这句没给具体 query → 用本片 motif_pool 兜底(话题相关,非动物硬编词)
                llm_q = _safe_queries(motif_pool)[:3]
            chunk["visual_focus"] = _infer_visual_focus(chunk, sentence, topic_anchor)
            chunk["visual_focus_source"] = "long_video_repair_trust_llm"
            chunk["use_pool_first"] = not _safe_queries(chunk.get("queries"))
            chunk["must_show"] = llm_show
            chunk["queries"] = llm_q
            chunk["metaphor_queries"] = _safe_queries(chunk.get("metaphor_queries"))
            chunk["visual_anchor"] = bool(llm_q)
            chunk["long_video_visual_policy"] = {
                "topic_anchor": topic_anchor,
                "trust_llm": True,
                "max_must_show": 2,
            }
        else:
            focus = _infer_visual_focus(chunk, sentence, topic_anchor)
            term = _term_for_focus(focus, sentence, chunk, topic_anchor, motif_pool)
            chunk["visual_focus"] = focus
            chunk["visual_focus_source"] = "long_video_repair"
            chunk["use_pool_first"] = focus in POOL_FIRST_FOCUS
            chunk["must_show"] = _must_show_for_focus(focus, term, topic_anchor)
            chunk["queries"] = _queries_for_focus(
                focus, term, topic_anchor, motif_pool,
                original=chunk.get("queries"),
                collapse_detected=collapse,
            )
            chunk["metaphor_queries"] = _metaphor_queries_for_focus(
                focus, term, topic_anchor, motif_pool,
                original=chunk.get("metaphor_queries"),
            )
            chunk["visual_anchor"] = focus not in POOL_FIRST_FOCUS and bool(chunk["queries"])
            chunk["long_video_visual_policy"] = {
                "topic_anchor": topic_anchor,
                "topic_anchor_collapse_repaired": collapse,
                "max_must_show": 1,
            }
        _sanitize_must_not(chunk)

    video_meta["motif_pool"] = _safe_queries(motif_pool)[:8]
    video_meta["long_video_topic_anchor"] = topic_anchor
    video_meta["topic_anchor_collapse_repaired"] = collapse
    repaired["video_meta"] = video_meta
    repaired["chunks"] = chunks
    meta = dict(repaired.get("meta") or {})
    meta["long_video_visual_focus"] = True
    meta["long_video_visual_focus_version"] = "v1"
    repaired["meta"] = meta
    return repaired


def _derive_topic_anchor(
    chunks: list[dict[str, Any]],
    sentences: list[str],
    user_brief: str,
    motif_pool: list[str],
) -> str:
    joined_text = " ".join([user_brief, " ".join(sentences)]).lower()
    # Pick the SUBJECT_PHRASE that DOMINATES the script by frequency — never the
    # first one merely mentioned. A wild-boar script (野猪 ×29) that says "狼"
    # once in passing must not become a wolf video. A real subject is repeated;
    # require >=2 hits so an incidental comparison cannot hijack the anchor.
    # Below threshold, fall through to the must_show/query-term counting, which
    # reads the (boar) search queries and gets the subject right.
    phrase_counts: Counter[str] = Counter()
    for needle, canonical in SUBJECT_PHRASES:
        hits = joined_text.count(needle.lower())
        if hits:
            phrase_counts[canonical] += hits
    if phrase_counts:
        top_phrase, top_hits = phrase_counts.most_common(1)[0]
        if top_hits >= 2:
            return top_phrase

    counts: Counter[str] = Counter()
    for chunk in chunks:
        for raw in _as_list(chunk.get("must_show")):
            term = _clean_term(raw)
            if _term_is_usable_anchor(term):
                counts[term] += 2
        for raw in _as_list(chunk.get("queries"))[:2]:
            for term in _query_terms(raw):
                if _term_is_usable_anchor(term):
                    counts[term] += 1
    if counts:
        return counts.most_common(1)[0][0]

    for query in motif_pool:
        for term in _query_terms(query):
            if _term_is_usable_anchor(term):
                return term
    return ""


def _topic_anchor_collapse_detected(chunks: list[dict[str, Any]], topic_anchor: str) -> bool:
    if not topic_anchor or not chunks:
        return False
    head = topic_anchor.split()[0]
    query_total = 0
    query_hits = 0
    must_show_hits = 0
    for chunk in chunks:
        must_show = " ".join(str(v).lower() for v in _as_list(chunk.get("must_show")))
        if head and head in must_show:
            must_show_hits += 1
        for query in _as_list(chunk.get("queries")):
            query_total += 1
            if head and head in str(query).lower():
                query_hits += 1
    chunk_ratio = must_show_hits / max(1, len(chunks))
    query_ratio = query_hits / max(1, query_total)
    return chunk_ratio >= 0.7 or query_ratio >= 0.8


def _infer_visual_focus(chunk: dict[str, Any], sentence: str, topic_anchor: str) -> str:
    st = str(chunk.get("sentence_type") or "").lower().strip()
    compact = re.sub(r"[\s,.!?;:，。！？；：、\"'“”‘’()（）]+", "", sentence)
    lower = _focus_text(sentence, chunk)
    if st in {"cta", "transition"} or len(compact) <= 4:
        return "transition"
    has_topic = _contains_topic(sentence, topic_anchor)
    if has_topic and _ecology_food_chain_context(lower):
        return "prey_or_livestock"
    if has_topic and _topic_subject_context(lower):
        return "topic_subject"
    if _contains_strong_consequence_term(lower):
        return "consequence"
    if _contains_concrete_human_impact(lower):
        return "human_impact"
    if _contains_concrete_prey_or_livestock(lower, has_topic=has_topic):
        return "prey_or_livestock"
    if _contains_focus_term(lower, "habitat"):
        return "habitat"
    if _contains_focus_term(lower, "consequence"):
        return "consequence"
    if _contains_topic(sentence, topic_anchor):
        return "topic_subject"
    if _looks_abstract(lower, chunk):
        return "abstract_broll"
    if _has_concrete_original_anchor(chunk):
        return "topic_subject"
    return "abstract_broll"


def _term_for_focus(
    focus: str,
    sentence: str,
    chunk: dict[str, Any],
    topic_anchor: str,
    motif_pool: list[str],
) -> str:
    lower = _focus_text(sentence, chunk)
    if focus == "topic_subject":
        return topic_anchor or _first_concrete_term(chunk) or "main subject"
    if focus in FOCUS_TERMS:
        found = _first_focus_term(lower, focus)
        if found:
            if focus == "human_impact" and found == "human activity" and topic_anchor:
                return "habitat loss wilderness"
            if focus == "prey_or_livestock" and found == "food preparation" and topic_anchor:
                return "wild prey animals"
            return found
    if focus == "prey_or_livestock" and topic_anchor and _ecology_food_chain_context(lower):
        return "wild prey animals"
    if focus in {"abstract_broll", "transition"}:
        return ""
    return _first_concrete_term(chunk) or _first_motif_term(motif_pool) or ""


def _must_show_for_focus(focus: str, term: str, topic_anchor: str) -> list[str]:
    if focus in POOL_FIRST_FOCUS:
        return []
    cleaned = _clean_term(term)
    if not cleaned:
        return []
    if cleaned in ABSTRACT_MUST_SHOW_TERMS:
        return []
    if focus != "topic_subject" and topic_anchor:
        anchor_head = topic_anchor.split()[0]
        if cleaned == topic_anchor or cleaned == anchor_head:
            return []
    return [cleaned]


def _queries_for_focus(
    focus: str,
    term: str,
    topic_anchor: str,
    motif_pool: list[str],
    *,
    original: Any,
    collapse_detected: bool,
) -> list[str]:
    if focus in POOL_FIRST_FOCUS:
        return []
    term = _query_term(term or topic_anchor or "")
    generated: list[str] = []
    if focus == "topic_subject":
        generated = [
            f"{term} wildlife",
            f"{term} natural habitat",
            f"{term} documentary footage",
        ]
    elif focus == "habitat":
        generated = [
            term,
            f"{term} wide shot",
            f"{term} nature landscape",
        ]
    elif focus == "consequence":
        generated = [
            term,
            f"{term} nature",
            f"{term} landscape",
        ]
    elif focus == "prey_or_livestock":
        generated = [
            term,
            f"{term} close shot",
            f"{term} outdoor footage",
        ]
    elif focus == "human_impact":
        generated = [
            term,
            f"{term} documentary",
            f"{term} landscape",
        ]
    if not collapse_detected:
        generated = _as_list(original)[:2] + generated
    return _safe_queries(generated, topic_anchor=topic_anchor, focus=focus)[:5]


def _metaphor_queries_for_focus(
    focus: str,
    term: str,
    topic_anchor: str,
    motif_pool: list[str],
    *,
    original: Any,
) -> list[str]:
    if focus in POOL_FIRST_FOCUS:
        return []
    safe_original = _safe_queries(_as_list(original), topic_anchor=topic_anchor, focus=focus)
    if safe_original:
        return safe_original[:3]
    if focus in {"habitat", "consequence", "human_impact"}:
        return _safe_queries(motif_pool[:2])[:2]
    if focus == "prey_or_livestock":
        return _safe_queries([term, "natural materials close up"])[:2]
    return []


def _focus_text(sentence: str, chunk: dict[str, Any]) -> str:
    values = [
        sentence,
        str(chunk.get("intent") or ""),
        str(chunk.get("metaphor_essence") or ""),
    ]
    return " ".join(values).lower()


def _contains_focus_term(text: str, focus: str) -> bool:
    return bool(_first_focus_term(text, focus))


def _topic_subject_context(text: str) -> bool:
    """Keep topic-behavior/ecology narration anchored to the topic subject."""
    terms = {
        "pack", "family", "social", "cooperation", "hierarchy", "role",
        "ecosystem", "predator", "stabilize", "stability",
        "myth", "king", "soul", "wildlife",
        "狼群", "家族", "家庭", "社会", "协作", "等级", "照顾", "分享",
        "族群", "生态", "维稳", "王者", "荒野",
        "神话", "灵魂", "修复者",
    }
    return any(term in text for term in terms)


def _ecology_food_chain_context(text: str) -> bool:
    terms = {
        "food chain", "predator prey", "prey relationship", "trophic",
        "食物链", "生态链", "捕食关系", "捕食", "猎物",
    }
    return any(term in text for term in terms)


def _contains_concrete_human_impact(text: str) -> bool:
    concrete_terms = {
        "city", "urban", "road", "logging", "deforestation", "farmland",
        "pasture", "fence", "construction", "pollution", "expansion",
        "habitat loss", "cleared forest", "factory", "mine", "mining",
        "城市", "道路", "公路", "砍伐", "伐木", "森林砍伐", "农田",
        "牧场", "栅栏", "建设", "污染", "扩张", "侵占", "栖息地",
        "工厂", "矿山", "采矿",
    }
    return any(term in text for term in concrete_terms)


def _contains_concrete_prey_or_livestock(text: str, *, has_topic: bool) -> bool:
    if has_topic and _ecology_food_chain_context(text):
        return True
    concrete_terms = {
        "deer", "cattle", "cow", "sheep", "livestock", "pasture", "farm",
        "prey", "bird", "mammal", "ingredient", "kitchen", "recipe",
        "cooking", "food preparation",
        "鹿", "牛", "羊", "家畜", "牧场", "农场", "猎物", "鸟",
        "哺乳动物", "食材", "厨房", "菜谱", "烹饪", "做菜",
    }
    return any(term in text for term in concrete_terms)


def _contains_strong_consequence_term(text: str) -> bool:
    strong_terms = {
        "overgrazed", "barren", "damaged", "erosion", "collapse",
        "flood", "fire", "泛滥", "啃光", "崩塌", "破坏", "侵蚀", "枯萎",
    }
    return any(term in text for term in strong_terms)


def _first_focus_term(text: str, focus: str) -> str:
    for needle, canonical in FOCUS_TERMS.get(focus, []):
        if needle in text:
            return canonical
    return ""


def _contains_topic(sentence: str, topic_anchor: str) -> bool:
    if not topic_anchor:
        return False
    lower = sentence.lower()
    if topic_anchor in lower:
        return True
    for needle, canonical in SUBJECT_PHRASES:
        if canonical == topic_anchor and needle in lower:
            return True
    return False


def _looks_abstract(text: str, chunk: dict[str, Any]) -> bool:
    if str(chunk.get("sentence_type") or "").lower() in {"emotion", "summary", "abstract_metaphor"}:
        return True
    if any(term in text for term in ABSTRACT_MUST_SHOW_TERMS):
        return True
    compact = re.sub(r"\s+", "", text)
    return len(compact) < 18


def _has_concrete_original_anchor(chunk: dict[str, Any]) -> bool:
    return bool(_first_concrete_term(chunk))


def _first_concrete_term(chunk: dict[str, Any]) -> str:
    for key in ("must_show", "queries", "metaphor_queries"):
        for raw in _as_list(chunk.get(key)):
            term = _clean_term(raw)
            if _term_is_usable_anchor(term):
                return term
            for qterm in _query_terms(raw):
                if _term_is_usable_anchor(qterm):
                    return qterm
    return ""


def _first_motif_term(motif_pool: list[str]) -> str:
    for query in motif_pool:
        for term in _query_terms(query):
            if _term_is_usable_anchor(term):
                return term
    return ""


def _sanitize_must_not(chunk: dict[str, Any]) -> None:
    cleaned = [_clean_term(v) for v in _as_list(chunk.get("must_not_show"))]
    chunk["must_not_show"] = [v for v in cleaned if v and v not in ABSTRACT_MUST_SHOW_TERMS][:5]


def _safe_queries(
    values: Any,
    *,
    topic_anchor: str = "",
    focus: str = "",
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _as_list(values):
        q = " ".join(str(raw or "").strip().lower().split())
        if not q or not q.isascii():
            continue
        if _is_unsafe_generic_query(q, topic_anchor=topic_anchor, focus=focus):
            continue
        if _is_bad_query(q):
            continue
        if focus and focus != "topic_subject" and topic_anchor:
            anchor_head = topic_anchor.split()[0]
            if q == topic_anchor or q.startswith(f"{anchor_head} close"):
                continue
        if len(re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", q)) < 2:
            q = f"{q} footage"
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _is_unsafe_generic_query(query: str, *, topic_anchor: str, focus: str) -> bool:
    if not topic_anchor:
        return False
    q = query.lower().strip()
    food_topics = {"food preparation", "cooking ingredients"}
    if q in {"food preparation", "kitchen food preparation"}:
        return topic_anchor not in food_topics
    if q in {"human activity", "people outdoors"}:
        return focus != "human_impact"
    return False


def _is_bad_query(query: str) -> bool:
    q = query.lower()
    if any(phrase in q for phrase in QUERY_BANNED_PHRASES):
        return True
    if " close up" in q:
        head = q.split(" close up", 1)[0].strip()
        if head in ABSTRACT_QUERY_HEADS:
            return True
    if any(term in q for term in ABSTRACT_MUST_SHOW_TERMS if term.isascii()):
        if not any(concrete in q for concrete in ("forest", "animal", "people", "city", "food", "nature", "space")):
            return True
    return False


def _query_term(term: str) -> str:
    cleaned = " ".join(str(term or "").strip().lower().split())
    if not cleaned:
        return "documentary b roll"
    return cleaned


def _clean_term(value: Any) -> str:
    term = " ".join(str(value or "").strip().lower().split())
    if not term or not term.isascii():
        return ""
    if _is_bad_query(term):
        return ""
    if term in ABSTRACT_MUST_SHOW_TERMS:
        return ""
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", term)]
    if not words:
        return ""
    if len(words) > 3:
        words = words[:3]
    return " ".join(words)


def _query_terms(value: Any) -> list[str]:
    text = str(value or "").lower()
    words = [
        w.strip("-")
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text)
        if w.strip("-") not in GENERIC_ANCHOR_STOPWORDS
    ]
    terms: list[str] = []
    if len(words) >= 2:
        bigram = " ".join(words[:2])
        if _term_is_usable_anchor(bigram):
            terms.append(bigram)
    terms.extend(w for w in words if _term_is_usable_anchor(w))
    deduped: list[str] = []
    for term in terms:
        if term not in deduped:
            deduped.append(term)
    return deduped[:3]


def _term_is_usable_anchor(term: str) -> bool:
    term = (term or "").strip().lower()
    if not term or term in GENERIC_ANCHOR_STOPWORDS:
        return False
    if term in ABSTRACT_MUST_SHOW_TERMS:
        return False
    if not term.isascii():
        return False
    if _is_bad_query(term):
        return False
    return bool(re.search(r"[a-z]", term))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []
