"""Script intelligence library service.

The first version uses deterministic distillation so it is cheap and reliable
while keeping the same public API and database shape.
"""
from __future__ import annotations

import os
import random
import re
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.models.script_intelligence import ScriptManuscript


DEFAULT_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "seed_short_mistake_fix",
        "title": "Shorts mistake-correction hook",
        "content_type": "short_video",
        "template_summary": "Use a common mistake as the hook, then give 2-3 concrete fixes and end with a comment CTA.",
        "distilled_template": {
            "hook_formula": "You think X is the key, but the real result comes from Y.",
            "structure": ["mistake hook", "why it matters", "2-3 fixes", "before/after result", "comment CTA"],
            "pacing": "First 3 seconds creates conflict; every 8-12 seconds gives a new concrete point.",
            "cta_pattern": "Ask viewers to comment their situation or save the checklist.",
        },
        "tags": {
            "industry": ["home", "education", "local_service"],
            "platform": ["YouTube Shorts", "TikTok", "Xiaohongshu"],
            "goal": ["education", "lead_generation", "followers"],
            "style": ["tutorial", "strong_hook"],
            "structure": ["mistake_fix", "three_points"],
            "effect": ["retention", "comments"],
        },
    },
    {
        "id": "seed_short_product_conversion",
        "title": "Short product conversion script",
        "content_type": "short_video",
        "template_summary": "Open with pain or result, show one product/service proof, then push a single action.",
        "distilled_template": {
            "hook_formula": "If you are still doing X, you are probably losing Y.",
            "structure": ["pain hook", "product/service appears", "proof or contrast", "single CTA"],
            "pacing": "No long setup; show the result before explaining features.",
            "cta_pattern": "DM, booking, consultation, or comment keyword. Never promise gifts, discounts, samples, free services, or prizes.",
        },
        "tags": {
            "industry": ["beauty", "restaurant", "ecommerce", "local_service"],
            "platform": ["TikTok", "Instagram Reels", "YouTube Shorts"],
            "goal": ["conversion", "lead_generation"],
            "style": ["direct_response", "product_demo"],
            "structure": ["pain_solution", "before_after"],
            "effect": ["conversion"],
        },
    },
    {
        "id": "seed_youtube_retention_chapters",
        "title": "YouTube long-form retention chapters",
        "content_type": "youtube_long",
        "template_summary": "Start with a thesis and open loop, then build chapters around questions that escalate curiosity.",
        "distilled_template": {
            "hook_formula": "This story looks like X, but the real reason is Y.",
            "structure": ["cold open", "context", "core question", "3-5 escalating chapters", "payoff", "SEO CTA"],
            "pacing": "Each chapter answers one question and opens the next one before the audience relaxes.",
            "retention_checks": ["No background dump before conflict", "Chapter 2 must add a new question", "Title keyword appears in first minute"],
        },
        "tags": {
            "industry": ["history", "education", "finance", "tech"],
            "platform": ["YouTube Long"],
            "goal": ["watch_time", "seo", "subscribers"],
            "style": ["documentary", "explainer"],
            "structure": ["open_loop", "chapters"],
            "effect": ["retention", "seo"],
        },
    },
    {
        # 叙事原型。领域(动物/历史/金融…)的具体名词交给写作模型填,这条原型本身
        # 跨领域复用。5 层结构(叙事弧 / 每拍句式 / 强反差句式 / 完整骨架 / 选题标准)
        # 由 studio_agent._build_formula_block 渲染成 prompt 写作脚手架。
        "id": "seed_youtube_curiosity_reversal",
        "title": "猎奇反转原型（被误解的对象 → 系统 → 冲突）",
        "content_type": "youtube_long",
        "template_summary": (
            "好奇反转原型：熟悉现象→纠正常识→离谱事实→建立系统设定→引入最大敌人→"
            "展示进化武器→回扣主题。把一个被误解的对象讲成有阵营、有冲突的『系统』。"
        ),
        "distilled_template": {
            # 一句话 hook 公式(向后兼容通用消费方)
            "hook_formula": "它看起来像 X，其实是 Y —— 而它最离谱的，是 Z。",
            # = 叙事弧 beats，保留 structure 这个 key 供通用消费方读取
            "structure": [
                "熟悉现象切入", "纠正常识误区", "抛出离谱事实",
                "建立系统设定", "引入最大敌人", "展示进化武器", "回扣主题",
            ],
            # L1 叙事弧
            "narrative_arc": [
                "熟悉现象切入", "纠正常识误区", "抛出离谱事实",
                "建立系统设定", "引入最大敌人", "展示进化武器", "回扣主题",
            ],
            # L2 每拍的填空句式骨架(照着改写，不要照抄)
            "beat_skeletons": [
                {"beat": "熟悉现象切入",
                 "skeleton": "每年一到某个季节/某种场合，很多人都会遇到 ___。大家通常叫它 ___，但它其实并不是。"},
                {"beat": "纠正常识误区",
                 "skeleton": "虽然名字里带着 ___，但它和 ___ 关系并不近。从本质上说，它反而更接近 ___。更离谱的是，它还进化/发展出了 ___。"},
                {"beat": "抛出离谱事实",
                 "skeleton": "单看一个，它似乎没什么威胁。但放到整个尺度上就完全不一样：可以达到 ___，一生/全程可能产生 ___。它不是一个个体，而是一台持续运转的 ___。"},
                {"beat": "建立系统设定",
                 "skeleton": "一个成熟的 ___ 就像一个分工严密的王国：有负责 ___ 的，有负责 ___ 的，还有掌控 ___ 的。每个个体看起来很弱，组合起来却变成一个庞大的系统。"},
                {"beat": "引入最大敌人",
                 "skeleton": "但这么强的 ___，并不是无敌的。它真正害怕的不是 ___，而是 ___。于是双方之间，形成了一场长期的军备竞赛。"},
                {"beat": "展示进化武器",
                 "skeleton": "为了对抗 ___，它们发展出各种奇怪的本领：有的负责 ___，有的可以 ___，甚至有的会用自己 ___ 来完成 ___。"},
                {"beat": "回扣主题",
                 "skeleton": "所以 ___ 真正可怕（迷人）的地方，不是单个有多强，而是它把 ___ 组合成了一个系统。这也是为什么它明明不起眼，却能 ___ 这么久。"},
            ],
            # L3 强反差句式库(至少用 2 条，制造爆点)
            "signature_phrases": [
                "其实并不是 ___",
                "更离谱的是 ___",
                "最可怕的不是 ___，而是 ___",
                "你以为它只是 ___，但实际上 ___",
                "单看 ___ 没什么，但放到 ___ 尺度上就完全不一样",
            ],
            # L4 完整文案骨架(整体参考)
            "full_skeleton": (
                "你有没有见过 ___？很多人以为它是 ___，但其实它真正的身份是 ___。\n"
                "它不但不是 ___，反而更接近 ___。而且在漫长发展中，它发展出了一个非常夸张的能力：___。\n"
                "一个成熟的 ___ 里，有负责 ___ 的，有负责 ___ 的，还有专门负责 ___ 的。"
                "单个看起来很普通，但整个群体组合起来，就像一台精密机器。\n"
                "最夸张的是，___ 可以达到 ___。这意味着它真正可怕的地方，不是单体战斗力，而是 ___。\n"
                "不过，它也有天敌。而它最害怕的不是 ___，而是 ___。"
                "为了应对这个敌人，它们进化出了各种让人难以置信的能力。\n"
                "比如 ___、___、___。有些听起来像科幻设定，但在现实里，这就是它们真实的生存方式。\n"
                "所以，下次你再看到 ___ 时，可能就不会只觉得它普通了。"
                "它其实是一个由 ___、___、___ 组成的复杂系统。这才是它真正恐怖、也真正迷人的地方。"
            ),
            # L5 选题标准(主题不贴合时先点出再尽力套)
            "topic_criteria": [
                "大家见过/听过，但不真正了解",
                "名字或外表容易误导",
                "有夸张但真实的数据",
                "有明确的天敌、对手或冲突",
                "能讲出像游戏设定一样的分工/技能/生存策略",
            ],
            # 事实槽(供 _build_formula_block 的防编造护栏引用)
            "fact_slots": [
                "寿命/时长数字（如『可以活到 X 年』）",
                "数量数字（如『一生产生 X』）",
                "本质分类/亲缘关系（如『更接近 X』）",
                "任何具体百分比、年份、体量、排名",
            ],
            "pacing": (
                "开头 30 秒内必须抛出『纠正常识』的反转；每个 beat 解决一个好奇点的同时"
                "再开一个新好奇点；震撼数字放中段当记忆锚点。"
            ),
            "cta_pattern": "结尾从『恶心/害怕/无聊』拉回『惊叹』，可选一句互动（你还知道哪些这样的 ___？评论告诉我）。",
        },
        "tags": {
            "industry": ["science", "animal", "history", "tech"],
            "platform": ["YouTube Long"],
            "goal": ["watch_time", "seo", "retention"],
            "style": ["story", "strong_hook", "explainer"],
            "structure": ["curiosity_reversal", "system_setup", "conflict"],
            "effect": ["retention", "seo"],
        },
    },
]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _infer_tags(text: str, content_type: str) -> dict[str, list[str]]:
    industries: list[str] = []
    platforms: list[str] = []
    goals: list[str] = []
    styles: list[str] = []
    structures: list[str] = []
    effects: list[str] = []

    if _has(text, "装修", "室内", "家装", "灯光", "客厅", "软装"):
        industries.append("home")
    if _has(text, "美甲", "美妆", "护肤"):
        industries.append("beauty")
    if _has(text, "餐厅", "咖啡", "菜单", "探店"):
        industries.append("restaurant")
    if _has(text, "历史", "战争", "人物", "冷知识"):
        industries.append("history")
    if _has(text, "AI", "软件", "工具", "科技"):
        industries.append("tech")

    if _has(text, "YouTube Shorts?", "Shorts?", "YOUTUBE SHOT"):
        platforms.append("YouTube Shorts")
    if _has(text, "YouTube 长", "长视频", "YouTube Long"):
        platforms.append("YouTube Long")
    if _has(text, "TikTok"):
        platforms.append("TikTok")
    if _has(text, "Reels", "Instagram"):
        platforms.append("Instagram Reels")
    if _has(text, "小红书", "Xiaohongshu", "Rednote"):
        platforms.append("Xiaohongshu")
    if not platforms:
        platforms.append("YouTube Long" if content_type == "youtube_long" else "YouTube Shorts")

    if _has(text, "引流", "获客", "预约", "私信"):
        goals.append("lead_generation")
    if _has(text, "下单", "促销", "转化", "购买"):
        goals.append("conversion")
    if _has(text, "涨粉", "关注", "粉丝"):
        goals.append("followers")
    if _has(text, "科普", "技巧", "教程", "知识", "解释"):
        goals.append("education")
    if content_type == "youtube_long":
        goals.extend(["watch_time", "seo"])

    if _has(text, "你知道", "为什么", "误区", "其实", "很多人"):
        styles.append("strong_hook")
    if _has(text, "教程", "技巧", "步骤"):
        styles.append("tutorial")
    if _has(text, "故事", "后来", "突然", "反转"):
        styles.append("story")
    if _has(text, "高级", "质感", "品牌"):
        styles.append("premium_brand")

    if _has(text, "误区", "不要", "错"):
        structures.append("mistake_fix")
    if _has(text, "第一", "第二", "第三", "三"):
        structures.append("three_points")
    if _has(text, "对比", "前后"):
        structures.append("before_after")
    if content_type == "youtube_long":
        structures.append("chapters")

    if _has(text, "评论", "告诉我"):
        effects.append("comments")
    if _has(text, "收藏", "保存"):
        effects.append("saves")
    if _has(text, "搜索", "关键词", "SEO"):
        effects.append("seo")
    if _has(text, "留存", "看完"):
        effects.append("retention")

    return {
        "industry": _dedupe(industries),
        "platform": _dedupe(platforms),
        "goal": _dedupe(goals),
        "style": _dedupe(styles),
        "structure": _dedupe(structures),
        "effect": _dedupe(effects),
    }


def _split_sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[。！？!?])\s*|\n+", text.strip())
        if s.strip()
    ]


_FORBIDDEN_CTA_PROMISE_WORDS = (
    "抽奖", "中奖", "幸运", "随机评论", "随机观众", "随机粉丝", "获赠",
    "赠送", "送出", "送给", "奖品", "礼品", "礼物", "纪念品", "赠品",
    "红包", "现金", "奖金", "优惠券", "代金券", "折扣", "免费名额",
    "福利", "样品", "试用", "一杯", "一份",
    "giveaway", "winner", "random commenter", "free cup", "free gift",
    "prize", "coupon", "voucher", "discount", "sample",
)


def _is_forbidden_cta_candidate(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").lower()
    return any(word.lower().replace(" ", "") in compact for word in _FORBIDDEN_CTA_PROMISE_WORDS)


def distill_script(raw_text: str, content_type: str, explicit_tags: dict[str, list[str]] | None = None) -> dict[str, Any]:
    text = raw_text.strip()
    sentences = _split_sentences(text)
    tags = _infer_tags(text, content_type)
    explicit_tags = explicit_tags or {}
    for key, values in explicit_tags.items():
        if key in tags:
            tags[key] = _dedupe([*tags[key], *values])

    hook = next(
        (s for s in sentences[:5] if _has(s, "你知道", "为什么", "其实", "误区", "有没有")),
        sentences[0] if sentences else "",
    )
    cta = next(
        (
            s for s in reversed(sentences)
            if _has(s, "评论", "私信", "预约", "关注", "下单", "收藏")
            and not _is_forbidden_cta_candidate(s)
        ),
        "",
    )

    if content_type == "youtube_long":
        structure = ["cold open", "context", "core question", "chapter build", "payoff", "SEO pack"]
        pacing = "Use chapters as curiosity steps; each section should answer one question and open the next."
    else:
        structure = ["hook", "problem", "proof/fix", "result", "CTA"]
        pacing = "Open in 3 seconds, keep one idea per beat, and avoid long setup."

    template = {
        "hook_formula": hook or "Open with a clear question, conflict, or mistake.",
        "structure": structure,
        "pacing": pacing,
        "cta_pattern": cta or "End with one concrete action: comment, save, DM, book, or follow.",
        "rewrite_guidance": [
            "Preserve the structure, not the exact original wording.",
            "Replace brand-specific details with the current user's product, audience, and platform.",
            "Keep the strongest hook visible in the first line.",
        ],
    }
    summary = (
        f"{content_type}: {template['hook_formula'][:80]} "
        f"Structure={', '.join(structure)}"
    )
    return {"tags": tags, "template": template, "summary": summary}


# 领域关键词 —— 把视频主题/频道定位映射到模板领域,实现「按领域智能匹配」:
# 动物视频只抽动物模板,财商视频只抽财商模板,避免结构串味。模板的领域存在
# industry_tags(灌库时按来源频道打)。匹配不到任何领域 → 退回通用随机。
DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "finance": ("财商", "理财", "投资", "股票", "股市", "美股", "基金", "经济", "通胀",
                "利率", "降息", "房贷", "房价", "黄金", "比特币", "美元", "汇率", "货币",
                "资产", "赚钱", "财富", "银行", "金融", "IPO", "债券", "巴菲特", "复利",
                "穷", "贫困", "穷忙", "月光", "工资", "收入", "现金流", "打工", "外卖",
                "努力越穷", "阶层", "机会成本"),
    "business": ("创业", "公司", "商业", "生意", "品牌", "营销", "增长", "商业模式",
                 "老板", "创始人", "副业", "电商", "供应链", "护城河", "盈利", "变现"),
    "wisdom": ("名言", "智慧", "哲学", "认知", "思维", "纳瓦尔", "语录", "名人名言",
               "读书", "书评", "经典", "金句", "人生道理", "格言", "巴菲特", "Buffett",
               "Warren Buffett", "Naval", "Ravikant", "芒格", "Munger", "查理芒格",
               "致股东信", "Almanack", "名人语录", "投资语录"),
    "travel": ("旅游", "旅行", "自由行", "攻略", "关西", "京都", "大阪", "奈良", "东京",
               "日本旅游", "票价", "营业时间", "交通", "JR", "新干线", "酒店", "签证",
               "景点", "路线", "行程", "旅居", "生活 vlog"),
    "crime": ("犯罪", "凶案", "悬案", "离奇", "谋杀", "案件", "失踪", "罪犯", "命案",
              "悬疑", "真凶", "悬案", "诡异"),
    "history": ("历史", "国家", "帝国", "王朝", "战争", "地缘", "文明", "古代", "朝代",
                "二战", "冷战", "民族", "日本", "罗马", "苏联", "近代"),
    "science": ("科普", "科学", "宇宙", "物理", "化学", "原理", "知识", "天文", "量子",
                "地球", "人体", "睡前", "冷知识"),
    "tech": ("AI", "人工智能", "科技", "芯片", "算法", "互联网", "机器人", "大模型",
             "技术", "半导体", "自动驾驶", "量子计算", "黄仁勋", "英伟达"),
    "animal": ("动物", "物种", "昆虫", "海洋", "生物", "演化", "入侵物种", "宠物",
               "蛇", "鱼", "鸟", "虫", "兽", "鲨", "蜘蛛", "螃蟹", "青蛙", "鳄", "猫科",
               "深海", "毒液", "捕食", "蜜獾", "平头哥", "獾", "章鱼", "乌贼", "墨鱼",
               "科莫多", "水熊虫", "黑曼巴", "毒蛇", "猛兽", "海鲜",
               # 常见宠物/动物口语词 — 之前缺,导致"狗/猫/萌宠"被'知识'带成 science、
               # 配不到动物专属模板。补全后宠物/萌宠题材才会命中 animal 原型。
               "萌宠", "狗", "犬", "猫", "喵", "汪", "仓鼠", "沙鼠", "松鼠", "兔",
               "乌鸦", "鹦鹉", "麻雀", "鼠", "马", "牛", "羊", "鹿", "狐", "熊", "狼",
               "象", "猴", "鲸", "海豚", "企鹅", "蝙蝠", "刺猬", "蜥蜴", "龟", "蜂",
               "蚁", "蝴蝶", "萤火虫", "蜗牛", "蜻蜓", "猫咪", "狗狗"),
    # 健康/养生/身体 — 全新领域。当前无专属模板,命中后会走通用骨架兜底(见
    # random_template);未来补 health 模板即自动生效。
    "health": ("健康", "养生", "睡眠", "失眠", "营养", "减肥", "瘦身", "血糖", "血压",
               "心率", "心慌", "肠道", "免疫", "维生素", "蛋白质", "咖啡因", "熬夜",
               "代谢", "激素", "疾病", "症状", "护肤", "衰老", "长寿", "锻炼", "健身"),
    "mystery": ("神秘", "未解", "谜", "外星", "阴谋", "灵异", "预言", "灾难", "老高",
                "ufo", "超自然", "末日", "诡异", "都市传说"),
    "self_growth": ("成长", "自我", "注意力", "习惯", "时间管理", "心理", "情绪",
                    "个人成长", "拖延", "自律", "副驾", "焦虑", "内耗", "认知升级",
                    "努力", "穷忙", "越努力越穷", "技能", "打工"),
}


_FIT_SPLIT = re.compile(r"[、，,/|;；。\s]+")

# 「禁用模板」门控(改造3):专用框架的母模板,题目里没有对应触发词就排除,防止把
# 不搭的结构硬套(如黑曼巴抽到入侵灾难型→写成"泛滥成灾")。(标题关键词, 触发词…)。
_ARCHETYPE_GATES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("入侵", "灾难"), ("入侵", "外来", "泛滥", "物种入侵", "扩散", "生态入侵")),
    (("餐桌", "活物", "食材"), ("吃", "海鲜", "食材", "餐桌", "美食", "料理", "食用")),
    (("宠物",), ("宠物", "养", "饲养", "萌宠")),
    (("繁殖", "孵化"), ("繁殖", "产卵", "交配", "生育", "孵化")),
    (("神兽", "图腾", "神话"), ("神话", "传说", "神兽", "图腾", "信仰", "文化")),
    (("房产", "房贷"), ("房", "楼", "地产", "房贷", "买房", "租房")),
    (("名人", "大佬", "警告"), ("巴菲特", "马斯克", "芒格", "名人", "大佬", "他说", "警告", "喊话")),
]


def _gate_pool(context: str, pool: list) -> list:
    """排除题目不触发的专用母模板。全被排掉则退回原池(不至于无模板可选)。"""
    ctx = context or ""
    kept = []
    for r in pool:
        title = str(getattr(r, "title", "") or "")
        excluded = False
        for title_kws, triggers in _ARCHETYPE_GATES:
            if any(k in title for k in title_kws) and not any(t in ctx for t in triggers):
                excluded = True
                break
        if not excluded:
            kept.append(r)
    return kept or pool


def _template_fit_score(context: str, row: Any) -> int:
    """领域内选母模板:模板 topic_criteria(「适合 蜂鸟、水熊虫…」)里的对象词,有几个
    """
    ctx = (context or "")
    tpl = (row.distilled_template or {}) if row is not None else {}
    crit = tpl.get("topic_criteria") or []
    score = 0
    for line in crit:
        for tok in _FIT_SPLIT.split(str(line)):
            tok = tok.strip()
            # 2-6 字的对象词;过滤掉「适合/主题/等」这类停用词
            if 2 <= len(tok) <= 6 and tok not in ("适合", "主题", "选题", "这个", "核心", "模板"):
                if tok in ctx:
                    score += 1
    return score


def detect_domains(text: str) -> set[str]:
    """从视频主题/频道定位文本里检测命中的领域(可命中多个)。"""
    t = (text or "").lower()
    if not t.strip():
        return set()
    hits: set[str] = set()
    for dom, kws in DOMAIN_KEYWORDS.items():
        for k in kws:
            if k.lower() in t:
                hits.add(dom)
                break
    return hits


# 通用解说骨架兜底:题材没匹配到专属模板时注入(科普/健康/冷知识等)。题材中立、
# 只给结构与口吻纪律,不串味。MEDIA_BUDDY_UNIVERSAL_FALLBACK=0 可关(回退到旧的裸奔)。
_UNIVERSAL_SHORT_TEMPLATE: dict[str, Any] = {
    "title": "通用解说叙事骨架 · 短视频",
    "distilled_template": {
        "hook_formula": "用一个反直觉现象 / 一个具体数字 / 「你以为X其实是Y」开场,3秒制造好奇,绝不用平铺定义开头。",
        "structure": [
            "反直觉钩子(前3秒)",
            "一句话点破核心机制/真相,不绕弯不堆术语",
            "逐拍给点:每句只给一个新信息点(机制/数字/对比)",
            "一个日常生活类比,把抽象翻译成观众能身体感受的画面",
            "留回味的点题金句收尾",
        ],
        "pacing": "3秒进入,一拍一个点,不堆砌过度具体的废数字,总时长45-60秒。",
        "cta_pattern": "用一句有余味的点题或轻反问收尾;绝不喊点赞/关注/收藏,绝不用'你怎么看'这类空洞互动。",
        "rewrite_guidance": [
            "内容写你自己的主题,结构和口吻照这个骨架。",
            "所有数字按事实防火墙模糊化,拿不准就不写精确值。",
            "口吻像跟好朋友分享一个你刚发现、觉得超有意思的冷知识;不像百科词条,不像AI播报。",
        ],
    },
    "raw_text": "",
}


def _universal_short_template(content_type: str) -> dict[str, Any] | None:
    """没匹配到领域模板时的兜底:短视频给通用解说骨架,长视频保持旧的保守(None)。"""
    if str(content_type) != "short_video":
        return None
    if (os.environ.get("MEDIA_BUDDY_UNIVERSAL_FALLBACK", "1") or "").strip().lower() in (
        "0", "false", "no", "",
    ):
        return None
    return dict(_UNIVERSAL_SHORT_TEMPLATE)


class ScriptIntelligenceService:
    def random_template(
        self,
        db: Session,
        content_type: str,
        user_id: str | None = None,
        context: str = "",
    ) -> dict[str, Any] | None:
        """按领域智能匹配 + 随机抽一个模板,用于脚本随机化。

        - 池子:该用户自己 + 全局共享 public_seed(status=indexed)。
        - 领域匹配:从 ``context``(视频主题 + 频道定位)检测领域,只在**同领域**模板
          里随机抽;没有领域信号或同领域没模板 → 返回 None(不注入结构模板)。
        - **整池随机**(非 top-N),所以同领域几十个模板都能轮到,真正分散结构。
        失败/空一律返回 None,调用方优雅降级。
        """
        try:
            mq = (
                db.query(ScriptManuscript)
                .filter(ScriptManuscript.content_type == content_type)
                .filter(ScriptManuscript.status == "indexed")
            )
            if user_id:
                # 该用户自己的 + 全局共享公式;案例池(public_case)永不进随机结构。
                mq = mq.filter(
                    or_(
                        and_(
                            ScriptManuscript.user_id == user_id,
                            ScriptManuscript.scope != "public_case",
                        ),
                        ScriptManuscript.scope == "public_seed",
                    )
                )
            else:
                # 无 user_id(批量/无主)→ 只用全局共享公式,绝不漏别人私有或案例卡。
                mq = mq.filter(ScriptManuscript.scope == "public_seed")
            rows = mq.all()
            domains = detect_domains(context)
            if domains:
                # 检测到领域 → 只在同领域里抽;绝不跨领域硬套(防动物视频套到商业结构)。
                pool = [r for r in rows if set(r.industry_tags or []) & domains]
                if not pool:
                    # 同领域没模板(如 health 暂无专属模板)→ 退到通用解说骨架,
                    # 而不是裸奔。通用骨架是题材中立的安全结构,不会串味。
                    return _universal_short_template(content_type)
            else:
                # 没领域信号 → 同样退到通用骨架(短视频);长视频保持保守不注入。
                return _universal_short_template(content_type)
            # 「禁用模板」门控:先排除题目不触发的专用框架(入侵/餐桌/宠物/神兽/房产/名人型)。
            pool = _gate_pool(context, pool)
            # 领域内「选母模板」:用模板自带的 topic_criteria(适合 xx)跟题目打分,
            # 命中具体对象的模板优先(如「水熊虫」命中怪物能力型);命中并列时随机,
            # 全不命中 → 在门控后的池里随机(已排掉硬套框架)。零额外 LLM。
            scored = [(r, _template_fit_score(context, r)) for r in pool]
            best = max(s for _, s in scored)
            candidates = [r for r, s in scored if s == best] if best > 0 else pool
            row = random.choice(candidates)
            try:
                row.usage_count = int(row.usage_count or 0) + 1
                db.commit()
            except Exception:
                db.rollback()
            return {
                "title": row.title,
                "distilled_template": row.distilled_template or {},
                "raw_text": row.raw_text or "",
            }
        except Exception:
            return None

    def create(self, db: Session, data: dict[str, Any]) -> ScriptManuscript:
        explicit = {
            "industry": data.get("industry_tags") or [],
            "platform": data.get("platform_tags") or [],
            "goal": data.get("goal_tags") or [],
            "style": data.get("style_tags") or [],
            "structure": data.get("structure_tags") or [],
            "effect": data.get("effect_tags") or [],
        }
        distilled = distill_script(data["raw_text"], data["content_type"], explicit)
        tags = distilled["tags"]
        row = ScriptManuscript(
            user_id=data.get("user_id"),
            title=data["title"],
            raw_text=data["raw_text"],
            content_type=data["content_type"],
            language=data.get("language") or "zh",
            scope=data.get("scope") or "private_raw",
            source=data.get("source") or "manual",
            template_summary=distilled["summary"],
            distilled_template=distilled["template"],
            industry_tags=tags["industry"],
            platform_tags=tags["platform"],
            goal_tags=tags["goal"],
            style_tags=tags["style"],
            structure_tags=tags["structure"],
            effect_tags=tags["effect"],
            quality_score=float(data.get("quality_score") or 0.0),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list(self, db: Session, page: int = 1, page_size: int = 50, content_type: str | None = None, user_id: str | None = None):
        q = db.query(ScriptManuscript)
        if user_id:
            q = q.filter(ScriptManuscript.user_id == user_id)
        if content_type:
            q = q.filter(ScriptManuscript.content_type == content_type)
        total = q.count()
        rows = (
            q.order_by(ScriptManuscript.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return rows, total

    def recommendations(
        self,
        db: Session,
        content_type: str,
        industry: str = "",
        platform: str = "",
        goal: str = "",
        limit: int = 5,
        user_id: str | None = None,
        include_public: bool = False,
    ) -> list[dict[str, Any]]:
        needles = {
            "industry": {v.strip().lower() for v in re.split(r"[,/| ]+", industry or "") if v.strip()},
            "platform": {platform.strip().lower()} if platform else set(),
            "goal": {goal.strip().lower()} if goal else set(),
        }

        def score_item(tags: dict[str, list[str]], quality: float, usage: int) -> float:
            score = quality + min(usage, 20) * 0.03
            for key, values in tags.items():
                hay = {str(v).lower() for v in values}
                if needles.get(key) and hay.intersection(needles[key]):
                    score += 2.0
            return score

        # 🔒 安全:public_seed = 核心公式库(大杀招),绝不出服务端给客户端刮。
        # 仅 admin(include_public=True)能在推荐里看到全局公式;普通用户只看自己的稿。
        # 无身份且非 admin → 不返回任何 DB 稿(堵死「不传 user_id 拉全部」的旧隐患)。
        # 服务端出片仍走 random_template(同样只在流水线内注入,从不下发客户端)。
        rows: list[Any] = []
        if include_public or user_id:
            mq = (
                db.query(ScriptManuscript)
                .filter(ScriptManuscript.content_type == content_type)
                .filter(ScriptManuscript.status == "indexed")
            )
            conds = []
            if user_id:
                conds.append(ScriptManuscript.user_id == user_id)
            if include_public:
                conds.append(ScriptManuscript.scope == "public_seed")
            mq = mq.filter(or_(*conds)) if len(conds) > 1 else mq.filter(conds[0])
            rows = mq.all()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            tags = {
                "industry": row.industry_tags or [],
                "platform": row.platform_tags or [],
                "goal": row.goal_tags or [],
                "style": row.style_tags or [],
                "structure": row.structure_tags or [],
                "effect": row.effect_tags or [],
            }
            candidates.append({
                "id": row.id,
                "title": row.title,
                "content_type": row.content_type,
                "score": score_item(tags, float(row.quality_score or 0), int(row.usage_count or 0)),
                "template_summary": row.template_summary or "",
                "distilled_template": row.distilled_template or {},
                "tags": tags,
            })

        for seed in DEFAULT_PATTERNS:
            if seed["content_type"] != content_type:
                continue
            tags = seed["tags"]
            candidates.append({
                "id": seed["id"],
                "title": seed["title"],
                "content_type": seed["content_type"],
                "score": score_item(tags, 1.0, 0),
                "template_summary": seed["template_summary"],
                "distilled_template": seed["distilled_template"],
                "tags": tags,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[: max(1, min(limit, 20))]
