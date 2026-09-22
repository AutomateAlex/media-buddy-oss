"""语言档案 —— 把验收闸参数化,而不是给每种语言复制一套闸。

## 为什么有这个模块

英文原来走「中文稿 → 翻译 → 事后凝练」。那**正是我们花整轮工夫从中文路径里拆掉的
东西**:先生成、再发现不对、再事后压。凝练就是截断的温和版本。

而且翻译**天然摧毁长度控制** —— 中文字数→英文词数的比例随内容浮动(专有名词、
句式、信息密度),上游控得再准,过一次翻译就散。这不是调参能解决的,
是量纲转换本身的问题。

(不管要 170 还是 139 词,都停在 195-203)。所以改成**原生英文生成**:
长度回到写稿时决定,和中文同一条原则。

## 设计

一个 profile 提供该语言的:计数 / 预估 / 判定 / 目标 / 切句 / 断尾 pattern。
**闸只认 profile,不认语言** —— 以后加第三种语言不用再动闸。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


# ── 断尾 pattern ────────────────────────────────────────────────────
# 中文那套正则对英文一点用没有,必须各有各的。判据一致:
# 抓「操作性/数量性提问收尾」(承诺了答案却没给),
# **不抓**建立在正文之上的讨论式提问 —— 那是允许的好结尾,误杀代价很大。

# 中文断尾**不在本模块定义**:统一交给 closure_standards.dangling_pattern_hit
# (单一事实源 —— 操作性提问 unanswered_howto + 断头列表 dangling_enumeration)。
# 曾经这里放过一份 _ZH_DANGLING 拷贝,和 closure_standards 各改一处 →
# 教训:同一判据只能有一处实现。zh profile 的 _dangling 因此为 None,见 dangling_hit。
_EN_DANGLING = re.compile(
    r"\b(how (do|does|did|can|should|would) (you|we|i|they)\b[^.!?\n]{0,40}"
    r"|how much\b[^.!?\n]{0,30}|how many\b[^.!?\n]{0,30}"
    r"|how long\b[^.!?\n]{0,30}|what now|what next|so now what)\s*\?\s*$",
    re.IGNORECASE,
)

# 英文切句:只认「句末标点 + 空白/结尾」作为断点。
# 注意这里**找的是断点,不是句子** —— 上一版用「整句」正则去 findall,
# 遇到 "3.5" 那种匹配不上的前缀会被 finditer 直接跳过,
# 结果 "He was worth 3.5 million francs." 切完只剩 "5 million francs."。
# 按断点切、用下标取原文,才能保证**一个字都不会丢**(由测试守住)。
_EN_BREAK = re.compile(r"[.!?]+(?=\s|$)")

# 缩写**必须按名单**,不能按"首字母大写 + 几个字母"的形状判 ——
# 那个形状同样命中 Hook. / Rome. / Cats. / Then.,会把正常句子和下一句粘死,
# 切句一歪,钩子/正文/落点全切错,隔离式修复就改到了错的段落。
_EN_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc",
    "inc", "ltd", "fig", "approx", "dept",
    # 故意**不收** "no"/"co"/"est":它们更常见的身份是普通词
    # ("The answer is no." 会被粘到下一句),收进来弊大于利。
}
# 句末是「单个大写字母 + 点」= 人名缩写(J. K. Rowling),同样不该断。
_EN_INITIAL = re.compile(r"(?:^|[^A-Za-z])[A-Z]\.$")
_EN_DECIMAL = re.compile(r"\d+\.$")


def _en_is_midsentence(prev: str) -> bool:
    """上一段其实没结束(小数 / 缩写 / 人名首字母)→ 该和下一段粘回去。"""
    tail = prev.rstrip()
    if _EN_DECIMAL.search(tail) or _EN_INITIAL.search(tail):
        return True
    m = re.search(r"([A-Za-z]+)\.$", tail)
    return bool(m and m.group(1).lower() in _EN_ABBREV)


def _zh_sentences(text: str) -> list[str]:
    s = re.sub(r"\s+", "", str(text or ""))
    parts = re.findall(r"[^。！？!?…]*[。！？!?…]+|[^。！？!?…]+$", s)
    return [p for p in (p.strip() for p in parts) if p]


def _en_sentences(text: str) -> list[str]:
    """按断点切,保证不丢字:所有片段拼回去 == 归一空白后的原文。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return []
    out: list[str] = []
    start = 0
    for m in _EN_BREAK.finditer(s):
        piece = s[start:m.end()].strip()
        if not piece:
            continue
        if _en_is_midsentence(piece):
            # 小数/缩写/人名首字母 —— 这个点不是句号,别断,继续往后并。
            continue
        out.append(piece)
        start = m.end()
    tail = s[start:].strip()
    if tail:
        out.append(tail)
    return out


@dataclass(frozen=True)
class LangProfile:
    lang: str
    count: Callable[[object], int]
    estimate: Callable[..., float]
    verdict: Callable[..., str]
    target: Callable[..., int]
    min_units: Callable[..., int]      # 低于此值判「过短」,重生成一次
    waste_units: Callable[..., int]    # 低于此值判「硬废稿」,根本没写出东西
    split_sentences: Callable[[str], list]
    _dangling: object
    join: str                    # 拼句子时用什么连接(中文无空格,英文有)
    unit: str                    # 提示词里的量纲词:中文数「字」,英文数「词」
    lang_pin: str                # 钉死输出语言的提示语(中文为空 = 不改原提示词)

    def dangling_hit(self, text) -> Optional[str]:
        """

        中文:委托给 closure_standards.dangling_pattern_hit(唯一事实源,含操作性
        提问 + 断头列表)。英文:走本模块的 _EN_DANGLING。这样加/改中文断尾判据
        只需动一处,不会再出现"闸走旧拷贝、新检测没生效"的漂移(见 _ZH_DANGLING 注释)。
        """
        s = str(text or "").strip()
        if not s:
            return None
        if self.lang == "zh":
            from backend.lib.closure_standards import dangling_pattern_hit
            return dangling_pattern_hit(s)
        if self._dangling is not None and self._dangling.search(s):
            return "unanswered_howto"
        return None


def _zh_profile() -> LangProfile:
    from backend.lib import tts_pacing as tp

    return LangProfile(
        lang="zh", count=tp.count_chars, estimate=tp.estimate_seconds,
        verdict=tp.length_verdict, target=tp.short_script_target_chars,
        min_units=tp.short_script_min_chars, waste_units=tp.short_script_hard_waste_chars, split_sentences=_zh_sentences, _dangling=None, join="",
        unit="字", lang_pin="",
    )


def _en_profile() -> LangProfile:
    from backend.lib import tts_pacing as tp

    return LangProfile(
        lang="en", count=tp.count_words, estimate=tp.estimate_seconds_en,
        verdict=tp.english_length_verdict, target=tp.english_target_words,
        min_units=tp.english_min_words, waste_units=tp.english_hard_waste_words, split_sentences=_en_sentences, _dangling=_EN_DANGLING, join=" ",
        unit="词",
        lang_pin="Write the output in English only. Do not use Chinese.",
    )


def profile_for(output_language) -> LangProfile:
    """默认中文 —— 它占 95% 的产量,未知输入不该被丢进陌生分支。"""
    return _en_profile() if str(output_language or "").lower() == "en" else _zh_profile()
