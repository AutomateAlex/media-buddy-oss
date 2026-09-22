"""短视频口播语速标定 —— 字数预算的唯一出处。

## 为什么有这个模块

字数预算原来是一个写死的 `3.5 字/秒 × 语速`,**散在 3 个地方各写一遍**
(prompt 目标字数 / 硬截断阈值 / 太短重生成阈值)。两个问题:

1. **标错了引擎。** 3.5 是照 ElevenLabs 中文标的,但生产实际跑千问音色
   目标的短片,成片只有 36.7-50.3 秒,**无一达标**。结算按 `max(1.0, 秒/60)`
   分钟算,客户按 1 分钟付费却只拿到 47 秒的片子。
2. **模型被挤没了结尾。** 预算偏小 + prompt 里"字数超范围就重写"是最硬的一条,
   长生不老"→ 举一个例子 → 结束)。

三处常量各改各的会互相打架 —— 尤其是**prompt 允许的字数上限一旦越过硬截断阈值,
模型写到上沿就会被反手砍掉最后一句**,又是一个"结尾没了"。所以统一收到这里。

## 数字怎么来的


标定基准是**不含空格的字符数**(含中文标点),与三处调用方的计数口径一致。
"""
import os
import re
from typing import Tuple


def _env_float(name: str, default: float) -> float:
    """读一个可调系数。配错(空/非数字)一律退回默认值,绝不因配置把出片搞挂。"""
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default

# ── 两套档位 ────────────────────────────────────────────────────────
#             —— 没量过就不改,这是底线。只做一件事:把 prompt 允许区间收窄,
#             让它不再和硬截断阈值倒挂(那个倒挂本身就会砍掉结尾,见下)。
#
# 英文按「词/秒」算,和这里的「字/秒」不是一个量纲。要标定它得单独测,不在本次范围。

_LEGACY_RATE_1X = 3.5

_MEASURED_RATE_1X = {
    "qwen_ethan": 5.03,   # 波动 4.70-5.47
    "qwen_neil": 4.70,    # 波动 4.41-5.22
    "qwen_kai": 4.62,     # 波动 4.38-4.97
    "qwen_cherry": 4.57,  # 波动 3.93-4.83
    "qwen_moon": 4.25,    # 波动 3.73-4.72
    "qwen_elias": 3.83,
    "azure_yunyang": 4.78,      # 波动 4.57-4.95
    "azure_yunxi": 4.58,        # 波动 4.34-4.71
}
# 没单独测过的千问音色取已测音色的中位附近。
# qwen_eldric_sage / qwen_vincent。它们暂走这个兜底值,
# 由 M6 的漂移告警在样本攒够后把偏差报出来 —— 这正是那套闭环的用途。
QWEN_DEFAULT_RATE_1X = 4.5

# 瞄准放行带的中点。放行带是 [0.85, 1.00] → 中点 0.925,取 0.92。
# 为什么不瞄 1.00:结算是 `billable_minutes = max(1.0, 秒/60)`,连续按比例,
# 为什么不瞄更低:短太多客户会觉得"没给够"。
# env `MB_TARGET_FACTOR` 可调(回滚只需改配置,不动代码)。
_TARGET_FACTOR = _env_float("MB_TARGET_FACTOR", 0.92)
_MAX_FACTOR = 1.05

# ── 长度三带模型 ────────────────────────────────────────────────────
# 波动就有这么大)。所以判定带宽必须比误差宽,否则就是在为噪声改稿:
#   放行带   —— 判过,静默放行
#   灰区     —— 判过但打标记,**不触发修复**(误差量级,动刀是净风险)
#   触发带   —— 判不过,进分诊修复
_PASS_LOW = _env_float("MB_LEN_PASS_LOW", 0.85)
_PASS_HIGH = _env_float("MB_LEN_PASS_HIGH", 1.00)
_TRIGGER_LOW = _env_float("MB_LEN_TRIGGER_LOW", 0.75)
_TRIGGER_HIGH = _env_float("MB_LEN_TRIGGER_HIGH", 1.10)
# 第四条线:**灾难线**。>_TRIGGER_HIGH 只是"该修";超过这条才是"病得不轻",
# 用来决定 fail-closed 之类的终态策略,与"要不要修"是两件事。
# 而灰区上界 66 秒(1.10)属于正常波动 —— 灾难线要落在两者之间。
HARD_HIGH = _env_float("MB_LEN_HARD_HIGH", 1.25)


def validate_bands(trigger_low, pass_low, pass_high, trigger_high, hard_high) -> None:
    """阈值必须单调:TRIGGER_LOW ≤ PASS_LOW ≤ PASS_HIGH ≤ TRIGGER_HIGH < HARD_HIGH。

    配错(比如 PASS_HIGH 配得比 TRIGGER_HIGH 大)不会报错,只会让闸的行为变得诡异
    —— 那种问题排查代价极高。**宁可拒绝启动**,让人在第一时间看见。
    """
    if not (trigger_low <= pass_low <= pass_high <= trigger_high < hard_high):
        raise ValueError(
            "MB_LEN_* 阈值必须单调递增:"
            f"TRIGGER_LOW({trigger_low}) ≤ PASS_LOW({pass_low}) ≤ "
            f"PASS_HIGH({pass_high}) ≤ TRIGGER_HIGH({trigger_high}) < "
            f"HARD_HIGH({hard_high})"
        )


# 导入即校验:配错就别启动,别带着诡异的闸跑起来。
validate_bands(_TRIGGER_LOW, _PASS_LOW, _PASS_HIGH, _TRIGGER_HIGH, HARD_HIGH)
# 沿用档:目标字数**一个字都不变**(=老的 duration×3.5×speed),阈值也沿用老的 1.1。
_LEGACY_TARGET_FACTOR = 1.00
_LEGACY_MAX_FACTOR = 1.10

# 这一条对两个档位都生效,修的是一个**原有的隐性 bug**:老区间上沿是 target×1.15,
# 而硬截断阈值只有 target×1.1 —— 模型写到区间上沿,反手就被截断砍掉最后一句。
_RANGE_LO, _RANGE_HI = 0.93, 1.07
# 太短重生成阈值(沿用原来的 0.7 语义)。
_MIN_FACTOR = 0.70


def _clamp_speed(speed) -> float:
    try:
        return max(0.7, min(2.0, float(speed or 1.0)))
    except (TypeError, ValueError):
        return 1.0


def is_measured(tts_provider: str) -> bool:
    """这个音色有没有实测数据(决定走实测档还是沿用档)。"""
    name = str(tts_provider or "").strip().lower()
    return name in _MEASURED_RATE_1X or name.startswith("qwen")


def chars_per_second_1x(tts_provider: str) -> float:
    """1.0x 语速下,这个音色每秒念多少个中文字符(不含空格,含标点)。

    配合 _LEGACY_TARGET_FACTOR=1.0 正好复现老行为。
    """
    name = str(tts_provider or "").strip().lower()
    if name in _MEASURED_RATE_1X:
        return _MEASURED_RATE_1X[name]
    if name.startswith("qwen"):
        return QWEN_DEFAULT_RATE_1X
    return _LEGACY_RATE_1X


def _chars_for(duration_seconds, tts_provider: str, speed, factor: float) -> int:
    try:
        sec = max(1.0, float(duration_seconds or 60))
    except (TypeError, ValueError):
        sec = 60.0
    sp = _clamp_speed(speed)
    return int(sec * chars_per_second_1x(tts_provider) * sp * factor)


def short_script_target_chars(duration_seconds, tts_provider: str, speed) -> int:
    """写给模型的目标字数。沿用档返回的值与改动前逐字节一致。"""
    factor = _TARGET_FACTOR if is_measured(tts_provider) else _LEGACY_TARGET_FACTOR
    return _chars_for(duration_seconds, tts_provider, speed, factor)


def short_script_char_range(
    duration_seconds, tts_provider: str, speed,
) -> Tuple[int, int]:
    """写给模型的允许区间 (下限, 上限)。上限保证低于硬截断阈值。"""
    target = short_script_target_chars(duration_seconds, tts_provider, speed)
    return int(target * _RANGE_LO), int(target * _RANGE_HI)


def short_script_max_chars(duration_seconds, tts_provider: str, speed) -> int:
    """硬截断阈值:超过就按完整句截断(只兜模型彻底跑飞的情况)。

    必须始终高于 short_script_char_range 的上限,否则模型写到允许区间上沿就会被
    这里砍掉最后一句 —— 这是"结尾没了"的成因之一,由
    test_prompt_range_stays_inside_the_truncation_cap 守住。
    """
    factor = _MAX_FACTOR if is_measured(tts_provider) else _LEGACY_MAX_FACTOR
    return _chars_for(duration_seconds, tts_provider, speed, factor)


def short_script_min_chars(duration_seconds, tts_provider: str, speed) -> int:
    """低于这个字数就重生成一次、取较长的一版。"""
    return _chars_for(duration_seconds, tts_provider, speed, _MIN_FACTOR)


# ── 结构预算(M2)────────────────────────────────────────────────────
# 只给一个总字数,模型会把预算花在铺陈上,到结尾没额度了 —— 这正是断尾的机制
# 所以把总额**拆成节拍**,并给落点划出**不许挪用**的专属额度。
_AVG_SENTENCE_CHARS = 35          # 中文口播句均长度(经验值,只用来估句数)
_MIN_SENTENCES = 5                # 再短的片子也要留得下 钩子+正文+落点
_HOOK_SHARE = 0.12
_LANDING_SHARE = 0.22
_LANDING_SENTENCES = 2


def beat_budget(duration_seconds, tts_provider: str, speed) -> dict:
    """把目标字数拆成 钩子 / 正文 / 落点 三段额度。

    它省钱的方式是让稿子第一次就写对,从而不进 script_gate 的修复轮
    (每轮 2 次调用)。
    """
    total = short_script_target_chars(duration_seconds, tts_provider, speed)
    n = max(_MIN_SENTENCES, round(total / _AVG_SENTENCE_CHARS))

    hook_chars = max(1, int(total * _HOOK_SHARE))
    landing_chars = max(1, int(total * _LANDING_SHARE))
    body_chars = total - hook_chars - landing_chars
    body_sentences = max(1, n - 1 - _LANDING_SENTENCES)
    # 兜底:极端参数下正文可能被挤没,把额度从落点匀一点回来,保证三段都 > 0。
    if body_chars <= 0:
        body_chars = max(1, total - hook_chars - 1)
        landing_chars = max(1, total - hook_chars - body_chars)
    return {
        "total_chars": total,
        "sentences": 1 + body_sentences + _LANDING_SENTENCES,
        "hook": {"sentences": 1, "chars": hook_chars},
        "body": {"sentences": body_sentences, "chars": body_chars},
        "landing": {"sentences": _LANDING_SENTENCES, "chars": landing_chars},
    }


def is_disaster_length(char_count, tts_provider: str, speed, duration_seconds) -> bool:
    """超过灾难线了吗?——决定终态策略(fail-closed)用,不决定"要不要修"。

    拿它判"灾难"没有依据(和 length_verdict 同一条纪律)。
    """
    if not is_measured(tts_provider):
        return False
    try:
        target = float(duration_seconds or 60)
    except (TypeError, ValueError):
        target = 60.0
    if target <= 0:
        return False
    return estimate_seconds(char_count, tts_provider, speed) / target > HARD_HIGH


def short_script_hard_waste_chars(duration_seconds, tts_provider: str, speed) -> int:
    """**硬废稿线**:低于这个字数就不是"偏短",是根本没写出东西。

    设在放行带下限的一半(0.85/2 = 0.425 × 时长)。这条线以上的偏短稿一律归
    script_gate 的长度分诊去修,**不许退通用模板稿** —— 断尾/偏短但有真材实料的
    稿子,永远好过一篇什么都没有的模板稿。
    """
    return _chars_for(duration_seconds, tts_provider, speed, _PASS_LOW / 2)


def disaster_floor_seconds(planned_seconds) -> float:
    """**post-TTS 灾难线**:配音短于它 = TTS 彻底出错/静音,该让这单失败。

    注意它兜的是灾难,**不是"片子短了点"** —— 后者归三带模型管,由文字阶段解决。
    历史上这里是散落在 pipeline 里的 0.35 魔数,现在收到这里统一(env 可调)。
    """
    try:
        planned = float(planned_seconds or 45.0)
    except (TypeError, ValueError):
        planned = 45.0
    ratio = _env_float("MB_DISASTER_FLOOR_RATIO", 0.35)
    return max(8.0, min(20.0, planned * ratio))


# ── 英文路径(§12)——量纲不同,必须另建一张表 ─────────────────────
# 中文按字/秒,英文按**词/秒**。直接套中文的数会错得离谱。
#
# 不是 ElevenLabs —— 后者已停用。一度按 ElevenLabs 建表是错的。
#
_EN_DEFAULT_WPS = _env_float("MB_EN_WORDS_PER_SEC", 2.2)
_EN_MEASURED_WPS: dict[str, float] = {
    "qwen_cherry": 2.20,
}


def count_words(text) -> int:
    """英文口径的唯一数词方法(P9 的英文版)。"""
    return len(str(text or "").split())


def english_words_per_second(voice: str) -> tuple[float, bool]:
    """→ (词/秒, 是否兜底值)。**兜底要明说**,不许假装已标定。"""
    name = str(voice or "").strip().lower()
    if name in _EN_MEASURED_WPS:
        return _EN_MEASURED_WPS[name], False
    return _EN_DEFAULT_WPS, True


def estimate_seconds_en(word_count, voice: str, speed) -> float:
    rate, _fb = english_words_per_second(voice)
    per_sec = rate * _clamp_speed(speed)
    try:
        n = max(0.0, float(word_count or 0))
    except (TypeError, ValueError):
        return 0.0
    return n / per_sec if per_sec > 0 else 0.0


def english_target_words(duration_seconds, voice: str, speed) -> int:
    rate, _fb = english_words_per_second(voice)
    try:
        sec = max(1.0, float(duration_seconds or 60))
    except (TypeError, ValueError):
        sec = 60.0
    return int(sec * rate * _clamp_speed(speed) * _TARGET_FACTOR)


def english_min_words(duration_seconds, voice: str, speed) -> int:
    """低于这个词数就重生成一次、取较长的一版(英文口径)。

    和中文的 `short_script_min_chars` 同构:`_MIN_FACTOR` 乘在**原始速率**上,
    """
    rate, _fb = english_words_per_second(voice)
    try:
        sec = max(1.0, float(duration_seconds or 60))
    except (TypeError, ValueError):
        sec = 60.0
    return int(sec * rate * _clamp_speed(speed) * _MIN_FACTOR)


def english_hard_waste_words(duration_seconds, voice: str, speed) -> int:
    """英文硬废稿线。和中文 `short_script_hard_waste_chars` 同一条定义
    (放行带下限的一半),只是换成英文的词/秒表 —— 判据一致,量纲各自。
    """
    rate, _fb = english_words_per_second(voice)
    try:
        sec = max(1.0, float(duration_seconds or 60))
    except (TypeError, ValueError):
        sec = 60.0
    return int(sec * rate * _clamp_speed(speed) * (_PASS_LOW / 2))


def english_length_verdict(word_count, voice: str, speed, duration_seconds) -> str:
    """

      因为这时预估是可信的;
      就是在拿噪声改稿(P8)。

    """
    try:
        target = float(duration_seconds or 60)
    except (TypeError, ValueError):
        target = 60.0
    if target <= 0:
        return "pass"
    _rate, is_fallback = english_words_per_second(voice)
    ratio = estimate_seconds_en(word_count, voice, speed) / target
    if is_fallback:
        return "repair" if ratio > HARD_HIGH else "pass"
    # 曾经这里只判超长(遗留自「翻译后凝练」那条老路,那时只怕译文变长),
    # 结果一条 130 词 / 42.2 秒(目标 60)的稿子判了 pass ——
    # 客户设 60 秒、付 1 分钟、拿 42 秒。过短和超长一样要抓。
    if _PASS_LOW <= ratio <= _PASS_HIGH:
        return "pass"
    if _TRIGGER_LOW <= ratio <= _TRIGGER_HIGH:
        return "gray"
    return "repair"


def count_chars(text) -> int:
    """**全系统唯一的数字数方法**:非空白字符数(含中文标点)。

    任何模块要引用字数阈值,都必须用这个函数。项目里原本还有一套
    `_script_content_units`(CJK + 拉丁词×2,**不含标点**),两者对同一篇稿子
    """
    return len(re.sub(r"\s", "", str(text or "")))


def estimate_seconds(char_count, tts_provider: str, speed) -> float:
    """由字数反算预计成片秒数。**全系统所有"预计时长"必须经此函数。**

    —— 所以 length_verdict 对这类音色一律放行,不拿它去触发改稿。
    """
    try:
        n = max(0.0, float(char_count or 0))
    except (TypeError, ValueError):
        return 0.0
    per_second = chars_per_second_1x(tts_provider) * _clamp_speed(speed)
    return n / per_second if per_second > 0 else 0.0


def length_verdict(char_count, tts_provider: str, speed, duration_seconds) -> str:
    """

    - pass   :落在放行带,静默放行
    - gray   :落在灰区 —— 判过但打标记,**不触发修复**。灰区宽度 ≈ 预估误差量级,
               为这种偏差改稿是"为噪声改稿",动一篇已合格的稿子是净风险。
    - repair :明显偏离,进分诊修复

    拿一个编造的数去触发 LLM 改稿没有依据,还会把 ElevenLabs(月产 236 条)
    """
    if not is_measured(tts_provider):
        return "pass"
    try:
        target = float(duration_seconds or 60)
    except (TypeError, ValueError):
        target = 60.0
    if target <= 0:
        return "pass"
    ratio = estimate_seconds(char_count, tts_provider, speed) / target
    if _PASS_LOW <= ratio <= _PASS_HIGH:
        return "pass"
    if _TRIGGER_LOW <= ratio <= _TRIGGER_HIGH:
        return "gray"
    return "repair"
