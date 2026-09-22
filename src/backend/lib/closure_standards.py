"""收口标准配置 —— "故事讲完了没有"的判定依据。

## 设计原则(对应 spec P5:pattern 是耗材,原则是裁判,定义归配置)


1. **确定性 pattern 层**(本模块 `DANGLING_PATTERNS`)—— 纯正则,零 LLM 调用。
   只抓**高把握**的断尾形态(问完就没下文)。拿不准一律放过,交给第二层。
   pattern 是耗材:随时可增删,命中率归零就删掉。

2. **语义裁判层**(标准存在 `ChannelRule.closure_rule`)—— 花一次 LLM 调用。
   裁判依据**跟频道走**,由本模块的 `closure_rule_for(series)` 解析。

## 为什么标准放在 ChannelRule 而不是新建一份配置

项目里已经有 `CHANNEL_RULES`(`channel_intelligence_service.py`),按
`series.industry_tag` 解析,每个频道有自己的 `positioning`。而且
`mystery_explainer` 的 positioning 里**早就写着**「结尾给出最可信的主流解释」——
只是管线从来没有执行它。

再建一份独立配置会带来:①双事实源,改一处忘一处;②粒度从 per-channel 退化成
per-type,频道自己那句具体要求被泛化掉;③新旧两套说法可能相反,实施者不知道信哪个。
所以复用既有载体,只加一个 `closure_rule` 字段。
"""
import re
from typing import Any, Optional


# 没写自己标准的频道用这套(知识科普口径)。
DEFAULT_CLOSURE_RULE = (
    "文案必须给观众完整的落点:开头或标题提出的核心问题,正文必须已经回答;"
    "做出的预告(如'最关键的是X''真正的原因是X')必须已经展开讲透;论证必须有结论。\n"
    "以未回答的问题、未兑现的预告、或半路中断的论证结尾,即为断尾。\n"
    "注意:建立在正文之上的开放式讨论问题是**允许的好结尾**,不要判成断尾。"
)


# ── 确定性 pattern 层 ───────────────────────────────────────────────
# 只收"高把握"形态。判据:提问是**操作性/数量性**的(问做法、用量、时机),
# 承诺了答案却没给。**不要**收录"到底是A还是B""你怎么看"这类讨论性提问
# —— prompt 明确允许用开放问题收尾,误杀会把好结尾也打回去。
DANGLING_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "id": "unanswered_howto",
        # 紧贴句末的操作性提问:「怎么做？」「那加多少、什么时候加？」
        "regex": re.compile(
            r"(怎么做|怎么办|怎么弄|该怎么[^？?]{0,6}|如何做|怎么选|怎么破|"
            r"加多少|放多少|用多少|多少度|多长时间|多久|什么时候[加放做用]?|"
            r"哪一步|第几步)[^。！？!?\n]{0,6}[？?]\s*$"
        ),
        "note": "问完即止式断尾:提出操作性问题却没有下文",
    },
)


# 断头列表:开了序数枚举的头(「第一，」「首先，」)却既没有续上后续
# (「第二/其次/然后…」),也没有收尾(「所以/最后/总之…」)。
# 稿子说「关键在三个条件同时凑齐:第一,…」然后就停了,第二第三从没出现
# 刻意保守:只在**开了头又既不续、又不收**时命中,不误伤「只讲一点就收尾」。
# 序数用法限定为紧跟列表标点/「是/点/条」,以排除「第一次/第一名/第一个/世界第一」。
_ENUM_OPENER = re.compile(
    r"第一[，,、：:是点點条條]|(?:^|[。！？!?，,、\s])首先[，,、：:]"
)
_ENUM_RESOLVED = re.compile(
    r"第二|其次|然后|接下来|接着|再者|最后|最終|最终|"
    r"所以|因此|总之|總之|综上|可见|说到底|归根结底|总而言之"
)


def dangling_pattern_hit(script) -> Optional[str]:
    """命中返回 pattern id,否则 None。零成本快捷通道。"""
    text = str(script or "").strip()
    if not text:
        return None
    for pat in DANGLING_PATTERNS:
        if pat["regex"].search(text):
            return str(pat["id"])
    if _ENUM_OPENER.search(text) and not _ENUM_RESOLVED.search(text):
        return "dangling_enumeration"
    return None


def closure_rule_for(series) -> str:
    """取这个频道的收口判定标准。

    解析链路复用既有的 `_rule_for(series)` → `series.industry_tag` → `CHANNEL_RULES`。
    **任何取不到的情况一律退回 DEFAULT_CLOSURE_RULE**:series 为 None
    (`projects.series_id` 可空)、频道未注册、该频道没写自己的标准、
    或 import 出问题 —— 收口判断是加分项,不能因为拿不到标准就把出片搞挂。
    """
    if series is None:
        return DEFAULT_CLOSURE_RULE
    try:
        from backend.services.channel_intelligence_service import _rule_for

        rule = _rule_for(series)
    except Exception:  # noqa: BLE001 — 取标准失败不许影响出片
        return DEFAULT_CLOSURE_RULE
    text = str(getattr(rule, "closure_rule", "") or "").strip() if rule else ""
    return text or DEFAULT_CLOSURE_RULE
