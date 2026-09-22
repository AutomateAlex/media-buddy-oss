"""Core pipeline orchestrator: script → assets → music → compose."""
import json
import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)


_OUTPUT_FORMAT_SEO_LABELS = {
    "youtube_shorts": "youtube-shorts",
    "youtube_landscape": "youtube-video",
    "tiktok": "tiktok",
    "instagram_reels": "instagram-reels",
    "instagram_feed": "instagram-feed",
}

_OUTPUT_FORMAT_ALIASES = {
    "youtube_short": "youtube_shorts",
    "youtube-short": "youtube_shorts",
    "youtube-shorts": "youtube_shorts",
    "short": "youtube_shorts",
    "shorts": "youtube_shorts",
    "yt_short": "youtube_shorts",
    "yt_shorts": "youtube_shorts",
    "reels": "instagram_reels",
    "instagram-reels": "instagram_reels",
    "ig_reels": "instagram_reels",
    "douyin": "tiktok",
    "vertical": "youtube_shorts",
    "vertical_9_16": "youtube_shorts",
    "portrait": "youtube_shorts",
}

_SHORT_OUTPUT_FORMATS = {"youtube_shorts", "tiktok", "instagram_reels"}
_SHORT_CTA_MARKERS = (
    "评论区",
    "留言",
    "告诉我",
    "你怎么看",
    "你最想",
    "点赞",
    "关注",
    "订阅",
    "收藏",
    "转发",
    "下条",
    "下一条",
    "继续拆",
)


# ── 中文词边界(字幕断句用 jieba,不把词从中间切开)──────────────────────
# 懒加载 + 优雅降级:没装 jieba 就返回 None,调用方退回老的字符切分逻辑。
_JIEBA = None
_JIEBA_TRIED = False


def _cjk_word_bounds(text: str) -> list[int] | None:
    """返回可安全断句的【词边界】下标列表(每个词结束后的位置)。jieba 不可用 → None。"""
    global _JIEBA, _JIEBA_TRIED
    if not _JIEBA_TRIED:
        _JIEBA_TRIED = True
        try:
            import jieba  # type: ignore
            jieba.initialize()
            _JIEBA = jieba
        except Exception:  # noqa: BLE001 — 没装/加载失败一律降级,绝不因此挂出片
            _JIEBA = None
    if _JIEBA is None:
        return None
    try:
        bounds: list[int] = []
        pos = 0
        for w in _JIEBA.cut(text, HMM=False):
            pos += len(w)
            bounds.append(pos)
        return bounds
    except Exception:  # noqa: BLE001
        return None


def _normalize_output_format(output_format: str | None) -> str:
    key = str(output_format or "youtube_landscape").strip().lower()
    return _OUTPUT_FORMAT_ALIASES.get(key, key)


def _is_short_output_format(output_format: str | None) -> bool:
    return _normalize_output_format(output_format) in _SHORT_OUTPUT_FORMATS


def _is_english_voice(provider: str) -> bool:
    """True if the provider maps to an Azure en-* voice. Derived from AZURE_VOICES
    so it never drifts. Output-language guard uses it to avoid a Chinese voice
    reading English narration."""
    from backend.services.tts_service import AZURE_VOICES
    return AZURE_VOICES.get(str(provider or ""), "").lower().startswith("en-")


from backend.services.voice_registry import (  # noqa: E402
    is_premium_english as _reg_is_premium_english,
)


def _registry_en_eleven() -> frozenset[str]:
    """高级英语配音里的 **ElevenLabs** 音色集合。**从注册表派生。**

       注释写着「必须与 api/tts.py 的 _ELEVENLABS_EN_CATALOG、
       上面长视频例外的内联集合三处一致」——
       三处人肉同步就是三处漂移风险,而漏了不会报错、只会静默换音色。
    """

    from backend.services import voice_registry as _reg

    return frozenset(v.key for v in _reg.VOICES if v.catalog == "elevenlabs_en")


def _is_premium_en_voice(provider: str) -> bool:
    """True = **高级英语配音**的音色(`hdv_*`,走 Google Cloud TTS)。


    客户选音色时试听了 Google 音色、听着对,**但出片出来是另一个人的声音**。

    根因在下面那道「输出语言守卫」:它写在 Google TTS 上线**之前**,
    只认识 Azure / ElevenLabs / 千问三家。`hdv_us_charon` 三个豁免条件
    一个都不满足 → 被判成「不会说英文的音色」→ **静默换成 `azure_aria`**。
    日志里只有一行 info,客户和我们都不会注意到。

    ⚠️ **从 `GOOGLE_TTS_VOICES` 派生,不写死名单** —— 和 `_is_english_voice`
       从 `AZURE_VOICES` 派生同一个道理:以后加音色不用回来改这里,永不漂移。
       (本文件已经有三处写死的 ElevenLabs 名单要求「必须保持一致」,
        那正是漂移的温床 —— 别再加第四处。)
    """

    from backend.services.voice_registry import is_premium_english

    return is_premium_english(provider)


# 字体版权 — burned subtitles ship inside the user's distributed video, so the
# font must be commercially licensable. "Microsoft YaHei" (Founder FZLanTingHei)
# is a commercial-use liability in China. Default to Noto Sans CJK (SIL OFL 1.1,
# free for commercial use + embedding). Overridable via env for any licensed font.
_SUBTITLE_FONT = (os.environ.get("MEDIA_BUDDY_SUBTITLE_FONT") or "Noto Sans CJK SC").strip() or "Noto Sans CJK SC"


def _seo_slug(text: str, *, fallback: str = "media-buddy-video", max_len: int = 96) -> str:
    """Create a Windows-safe, upload-friendly filename slug.

    Keep CJK keywords intact for Chinese SEO, normalize accents for Latin
    titles, and use hyphens instead of spaces/underscores.
    """
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower().replace("&", " and ")
    raw = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", raw)
    raw = re.sub(r"[\s_.,;:!?()[\]{}'`~+=，。！？、；：（）【】《》“”‘’]+", "-", raw)
    raw = re.sub(r"[^0-9a-z\u3400-\u9fff-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("- .")
    raw = raw[:max_len].strip("- .") or fallback
    # Linux/ext4 caps each path COMPONENT at 255 BYTES (not chars). CJK is 3
    # bytes/char, so a 96-char slug can be ~288 bytes → ffmpeg "File name too
    # it). Cap the slug to a byte budget that leaves room for suffixes the
    # pipeline appends (.video_only.mp4, _captioned.mp4, -youtube-video, …).
    _MAX_SLUG_BYTES = 150
    if len(raw.encode("utf-8")) > _MAX_SLUG_BYTES:
        raw = raw.encode("utf-8")[:_MAX_SLUG_BYTES].decode("utf-8", "ignore").strip("- .") or fallback
    if raw.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "LPT1", "LPT2"}:
        raw = f"{raw}-video"
    return raw


def _seo_video_filename(title: str, output_format: str, *, script: str = "") -> str:
    source_title = title or (script.splitlines()[0] if script else "")
    base = _seo_slug(source_title)
    fmt = _seo_slug(
        _OUTPUT_FORMAT_SEO_LABELS.get(output_format, output_format or "video"),
        fallback="video",
        max_len=32,
    )
    stem = base if base.endswith(f"-{fmt}") or base == fmt else f"{base}-{fmt}"
    return f"{stem}.mp4"


def _strip_citations(text: str) -> str:
    """去掉联网模型(:online)漏进正文的网址/引用标记 —— 口播稿给 TTS 直接朗读,
    绝不能念出链接。CJK 感知:URL 在遇到中文/标点处截断,绝不吞掉后面的中文句子。"""
    s = str(text or "")
    # markdown 链接 [label](url) → 整体删除(label 多是来源名/域名,口播不需要)
    s = re.sub(r"\[[^\]\n]{0,100}\]\([^)\n]*\)", "", s)
    # 裸 URL / www. —— 停在空白/中文/全角标点,避免吃掉后续中文
    _url_stop = r"[^\s　-鿿＀-￯]"
    s = re.sub(r"https?://" + _url_stop + r"+", "", s)
    s = re.sub(r"\bwww\." + _url_stop + r"+", "", s)
    # 收尾:残留的空括号 / 空方括号 / 多余空格
    s = re.sub(r"[（(]\s*[)）]", "", s)
    s = re.sub(r"\[\s*\]", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return _repair_citation_remnants(s).strip()


def _repair_citation_remnants(text: str) -> str:
    """Clean grammar scars left after stripping online-search citations.

    Online models often write source-led clauses such as
    ``[腾讯新闻](...)指出，...``. After deterministic link removal the line
    becomes ``指出，...`` or ``。指出，...`` which is awkward in TTS and looks
    like a broken citation. Keep the factual clause, replace the source stub
    with neutral phrasing, and remove visuals the script never produced.
    """
    s = str(text or "")
    # Sentence-initial/source-less reporting verbs after link stripping.
    s = re.sub(
        r"(^|[。！？!?；;\n])\s*(提到|指出|显示|称|报道|表示|认为|揭示|介绍)[，,：:]\s*",
        r"\1有资料\2，",
        s,
    )
    s = re.sub(r"有资料(称|认为|表示)，", r"有资料\1，", s)
    # Remove references to non-existent prepared visuals/maps/checklists.
    s = re.sub(
        r"这张[^。！？!?]{0,24}(地图|图表|清单|表格|资料图|路线图)[^。！？!?]{0,60}[。！？!?]",
        "",
        s,
    )
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


# A narration label the model sometimes prints before the actual script when the
# brief asks it to "de-dup / switch angle" — it narrates its reasoning, then a
# "旁白：" header, then the real narration. We keep only what follows the label.
_NARRATION_LABEL_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:旁白|解说|文案|配音|正文|口播|narration|voice\s*over|script)"
    r"[ \t]*(?:[（(][^）)\n]{0,40}[）)])?[ \t]*[:：][ \t]*",
    re.IGNORECASE,
)
# Keywords that mark a LEADING line as the model's reasoning / notes (de-dup,
# fact-check, angle-switching) rather than spoken narration. The brief sometimes
# asks the model to "自动查重 / 换角度 / 核实" and it prints its whole thought
# process (真实机制：…/核实：…/新角度锁定：…/高风险点已拦截) before the real script.
_SCRIPT_META_KEYWORDS = (
    "查重", "去重", "重复度", "真实机制", "核实", "事实核查", "辟谣",
    "高风险", "伪科学", "风险点", "已拦截", "已排除", "已确认", "已锁定",
    "转向", "新角度", "角度锁定", "选题", "创作思路", "结论先行",
    "参考来源", "依据", "免责", "字数",
)
# A short "标签：…" header at the start of a line (≤8-char label before the colon)
# is a note header, not a spoken sentence.
_SCRIPT_META_HEADER_RE = re.compile(
    r"^[\s*>#＃＞\-—•·✅❌✔☑✓►▶◆■◇]*([一-鿿A-Za-z]{1,8})[:：]"
)


def _line_is_script_meta(line: str) -> bool:
    ln = line.strip().lstrip("*>#＃＞-—•·✅❌✔☑✓►▶◆■◇ ")
    if not ln:
        return False
    if any(kw in ln for kw in _SCRIPT_META_KEYWORDS):
        return True
    m = _SCRIPT_META_HEADER_RE.match(line.strip())
    # "标签：…" header (short label, no sentence punctuation before the colon)
    if m and len(m.group(1)) <= 8 and not any(p in m.group(1) for p in "，。！？,.!?"):
        return True
    return False


def _strip_script_meta_preamble(text: str) -> str:
    """Strip the model's meta-commentary so TTS reads ONLY the narration.

    Briefs that ask the model to self-check for duplicates / switch angle /
    fact-check make it print its reasoning (真实机制：…/核实：…/新角度锁定：…/
    高风险点已拦截) — sometimes with, sometimes without a "旁白：" header — before
    the real script. Without this the voiceover reads the reasoning aloud. We drop
    the LEADING block of meta/blank lines and start at the first real narration
    line (stopping there, so a mid-narration false positive can't truncate it).
    """
    s = str(text or "")
    matches = list(_NARRATION_LABEL_RE.finditer(s))
    if matches:
        # explicit narration label present → keep only what follows the LAST one
        s = s[matches[-1].end():]
    lines = s.split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or _line_is_script_meta(lines[i])):
        i += 1
    cleaned = "\n".join(lines[i:]).strip()
    return cleaned or s.strip()  # never return empty (over-strip safety)


def _strip_tts_unsafe_symbols(text: str) -> str:
    """Remove markup / symbols the voice would read aloud or choke on.

    The model sometimes emits markdown (``**bold**``), headers, list bullets and
    stray symbols; Azure reads these literally (the user heard punctuation read
    out). Normal sentence punctuation (，。！？：；…) is KEPT — TTS uses it for
    natural prosody, it is not spoken.
    """
    s = str(text or "")
    s = re.sub(r"(?m)^[ \t]*[#＃>＞]+[ \t]*", "", s)        # markdown headers / quote markers
    s = re.sub(r"(?m)^[ \t]*[*•·▪◦‣–][ \t]+", "", s)        # list bullets at line start
    s = s.replace("`", "").replace("*", "").replace("＊", "")  # bold/italic/code marks
    s = re.sub(r"[~^|\\_=<>＜＞]+", "", s)                   # stray markup symbols
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s


# P2/6-C:粘贴文案末尾偶尔混进 UI/系统标记(如「原文提交生成」),verbatim 会把它念出来 +
# 烧进字幕。这类词只会出现在【结尾】,用白名单精确匹配、只剥末尾,绝不误伤正文。
_TRAILING_JUNK_MARKERS = ("原文提交生成", "原文提交", "提交生成")


def _strip_trailing_system_markers(text: str) -> str:
    s = str(text or "").rstrip()
    changed = True
    while changed:
        changed = False
        for m in _TRAILING_JUNK_MARKERS:
            if s.endswith(m):
                s = s[: -len(m)].rstrip()
                changed = True
    return s


def _sanitize_script_for_production(text: str, skip_cta_filter: bool = False) -> str:
    """Final safety gate before TTS/subtitles receive script text.

    skip_cta_filter=True(「执行文案」verbatim 模式):跳过送礼/抽奖承诺的 CTA 合规删句
    —— 客户甩定稿要求「一字不改」,风险自担。仍保留去引用标注/去元信息前言/去 TTS
    不安全符号这三步(它们**不改字**,只把不能念的符号清掉,否则配音会把星号/emoji 念出来)。
    另:剥掉末尾偶发的「原文提交生成」类系统标记(6-C)。
    """
    raw = _strip_trailing_system_markers(
        _strip_tts_unsafe_symbols(
            _strip_script_meta_preamble(_strip_citations(str(text or ""))),
        )
    )
    if skip_cta_filter:
        return raw
    try:
        from backend.services.studio_agent import _sanitize_script_cta_promises
        return _sanitize_script_cta_promises(raw)
    except Exception:
        logger.warning("production script CTA sanitizer failed; using original script", exc_info=True)
        return raw


def _script_sentences_for_quality(text: str) -> list[str]:
    cleaned = re.sub(r"\[[^\]]{1,80}\]", " ", str(text or ""))
    cleaned = re.sub(r"^#+\s*.+$", " ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+", cleaned)
    return [p.strip(" \t\r\n-—，,。.!！？?；;：:") for p in parts if p.strip()]


def _script_content_units(text: str) -> int:
    raw = str(text or "")
    cjk = re.findall(r"[\u3400-\u9fff]", raw)
    latin_words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", raw)
    return len(cjk) + len(latin_words) * 2


def _is_short_cta_sentence(sentence: str) -> bool:
    s = str(sentence or "").strip()
    if not s:
        return False
    marker_hits = sum(1 for marker in _SHORT_CTA_MARKERS if marker in s)
    if marker_hits <= 0:
        return False
    # A short sentence dominated by engagement language is an outro/CTA, not
    # useful body narration. Longer sentences with concrete facts may still
    # contain "评论" or "告诉" naturally, so keep those as body.
    return marker_hits >= 2 or _script_content_units(s) <= 36


def _short_script_hard_waste_chars(
    duration_seconds, tts_provider: str = "", speed=1.0,
) -> int:
    """硬废稿字数线。口径统一到 tts_pacing(P9),不再自带第二套数法。"""
    from backend.lib.tts_pacing import short_script_hard_waste_chars

    return short_script_hard_waste_chars(duration_seconds, tts_provider, speed)


def _short_script_quality_issue(
    script: str,
    output_format: str | None,
    duration_hint_seconds: int | float | None = None,
    tts_provider: str = "",
    tts_speed: float = 1.0,
    output_language: str = "zh",
) -> Optional[str]:
    """**硬废稿**判定 —— 只拦"根本不是一篇稿子"的东西。

    分工(M5.2 对账):这里只管空稿 / 纯 CTA / 正文句数过少 / 短到不成篇;
    **"偏短""偏长""没收口"一律归 script_gate 管**,因为本函数的失败路径会退到
    通用模板稿,而模板稿比一篇偏短但有真材实料的稿子更差(spec P4)。

    字数线因此设在放行带下限的一半(≈0.425×时长),只兜真正的残稿;
    而且一律经 tts_pacing 的统一计数(P9)—— 旧口径 `_script_content_units`
    """
    if not _is_short_output_format(output_format):
        return None
    sentences = _script_sentences_for_quality(script)
    if not sentences:
        return "empty_script"
    from backend.lib.lang_profile import profile_for

    prof = profile_for(output_language)
    body_sentences = [s for s in sentences if not _is_short_cta_sentence(s)]
    if not body_sentences:
        return "cta_only_script"
    if len(body_sentences) < 2:
        return "too_few_body_sentences"
    # 按出片语言自己的量纲判 —— 一篇 20 词的英文残稿有 110 个字符,
    # 拿中文字数线(163)去量会判它"够长",硬废稿闸就等于没装。
    waste_line = prof.waste_units(duration_hint_seconds or 45, tts_provider, tts_speed)
    if prof.count(prof.join.join(body_sentences)) < waste_line:
        return "body_too_short"
    return None


# 故事收口(closure)校验已迁出到 backend.lib.script_gate + backend.lib.closure_standards:
#   - 判定标准跟频道走(ChannelRule.closure_rule),不再写死在管线里(spec P5);
#   - pattern 层与语义层分离,修复走"定点补结尾"(把完整原稿交给模型)。
# 上一版把标准和逻辑都堆在本文件里,重写时又没把原稿传给模型 —— 抓得到、修不好。


def _truncate_short_script(
    script: str, seconds, speed: float = 1.0, tts_provider: str = "",
) -> str:
    """⚠️ **已从主链路摘除(M5.1),保留一个版本周期仅供回滚**,勿在新代码中调用。

    它从**尾部**砍字 —— 等于"长度超了就优先牺牲故事完整性",方向反了;
    而且砍完不复检。现在长度归 script_gate 的三带模型:超了压缩正文(结尾逐字保留),
    压不动就放长出片 + 告警,**永远不砍尾巴**。

    原文档:短视频防超长硬兜底:超过阈值就截断到阈值内最后一个完整句(句末标点)。

    阈值出自 backend.lib.tts_pacing —— 由它保证阈值高于 prompt 告诉模型的字数上限。
    两者一旦倒挂,模型写到允许范围的上沿就会被这里反手砍掉最后一句,
    那正是"结尾没了"的成因之一(见该模块头部注释)。"""
    from backend.lib.tts_pacing import short_script_max_chars

    try:
        max_chars = short_script_max_chars(seconds, tts_provider, speed)
    except Exception:
        return script
    text = str(script or "")
    if max_chars <= 0 or len(re.sub(r"\s", "", text)) <= max_chars:
        return script
    kept: list[str] = []
    count = 0
    last_end = 0
    for ch in text:
        kept.append(ch)
        if not ch.isspace():
            count += 1
        if ch in "。！？!?…":
            last_end = len(kept)
        if count >= max_chars:
            break
    out = ("".join(kept[:last_end]) if last_end else "".join(kept)).strip()
    return out or script


def _extract_topic_from_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    patterns = [
        r"主题[：:]\s*([^\n。！？!?]+)",
        r"题材[：:]\s*([^\n。！？!?]+)",
        r"关于\s*([^\n，。！？!?]{2,40})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            topic = re.sub(r"\s+", " ", m.group(1)).strip(" ：:，,。.!！？?")
            if topic:
                return topic[:60]
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:60].strip(" ：:，,。.!！？?") or "这个主题"


def _build_short_script_retry_prompt(
    prompt: str,
    issue: str,
    duration_hint_seconds: int | float | None,
) -> str:
    duration = int(duration_hint_seconds or 45)
    return (
        f"{prompt}\n\n"
        f"上一次短视频脚本无效，原因：{issue}。\n"
        f"请重新生成一条完整的短视频口播正文，目标约 {duration} 秒。\n"
        "要求：\n"
        "1. 必须先讲主题本身的事实、原因、过程或反转，不能只写结尾互动。\n"
        "2. 输出 4-7 句连续口播正文，不要标题、不要列表、不要分镜说明。\n"
        "3. 最多最后一句轻微互动，例如让观众评论想看哪一部分。\n"
        "4. 严禁抽奖、送礼、送钱、送物、赠品、幸运观众、中奖等任何承诺。"
    )


def _build_emergency_short_script(prompt: str) -> str:
    topic = _extract_topic_from_prompt(prompt)
    return (
        f"{topic}，表面看像一个简单问题，其实背后往往藏着一条清晰的因果链。"
        "先看第一点：它通常不是突然出现的，而是由环境、时间和外部条件一起推动。"
        "第二点更关键，人们常常只看到最后的结果，却忽略了前面连续发生的小变化。"
        "如果把这些线索放在一起，你会发现真正重要的不是单个瞬间，而是它怎样一步步形成。"
        "这也是这个主题最值得看的地方：它把一个看似熟悉的现象，变成了可以被重新理解的故事。"
        "评论区告诉我你还想继续看这个主题的哪一部分。"
    )


def _should_translate(output_language: str, *, native_en: bool) -> bool:
    """要不要翻译。

    原生英文模式下稿子**本来就是英文**,再走一次"中译英"是把英文翻英文 ——
    既浪费一次调用,又会把已经写好的英文改坏(翻译器会按"忠实原文"重写)。
    """
    lang = str(output_language or "zh").lower()
    if lang == "zh":
        return False
    return not (lang == "en" and native_en)


def _needs_language_rescue(script, output_language) -> bool:
    """成稿的语言跟订单要的**对不上** —— 需要救一把。

    这是最后一道防线,不管上游怎么错都拦得住:确定性判断,不靠 LLM 自觉。
    对称地两边都管,因为两边都真出过事:

    - 要中文却出了英文:频道定位是英文时 generate_script 会被带跑;
    - 要英文却出了中文:原生英文只接了一半线(跳过了翻译、却没让写稿用英文),

    判据:少数派语言的字符数超过了多数派 —— 夹几个外文词不算,整篇写错才算。
    """
    lang = str(output_language or "zh").lower()
    if lang not in ("zh", "en"):
        return False
    text = str(script or "")
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return (lat > 0 and cjk < lat) if lang == "zh" else (cjk > 0 and lat < cjk)


def _minimum_short_narration_seconds(
    output_format: str | None,
    planned_seconds: int | float | None = None,
) -> float:
    """post-TTS **灾难线**:配音短于它 = TTS 彻底出错/静音,该让这单失败。

    ⚠️ 它兜的是灾难,**不是"片子短了点"** —— 后者归 script_gate 的三带模型,
    在文字阶段解决。阈值已收到 tts_pacing 统一(原来是散在这里的 0.35 魔数)。
    """
    if not _is_short_output_format(output_format):
        return 0.0
    from backend.lib.tts_pacing import disaster_floor_seconds

    return disaster_floor_seconds(planned_seconds)


def _ensure_ffmpeg_on_path() -> None:
    """Prepend ffmpeg's directory to PATH so OpenMontage subprocess.run(['ffmpeg', ...])
    works even when the parent shell's PATH doesn't include it. Idempotent."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    candidates = [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        r"C:\Program Files (x86)\ffmpeg\bin",
    ]
    for d in candidates:
        if (Path(d) / "ffmpeg.exe").exists() and (Path(d) / "ffprobe.exe").exists():
            cur = os.environ.get("PATH", "")
            if d not in cur:
                os.environ["PATH"] = d + os.pathsep + cur
            return


_ensure_ffmpeg_on_path()

from backend.lib.llm_client import LLMClient
from backend.lib.progress import write_progress, read_progress
from backend.database import SessionLocal
from backend.models.chunk import Chunk
from backend.models.project import Project
from backend.services.tts_service import TTSService
from backend.services.footage_service import FootageService
from backend.services.library_service import LibraryService
from backend.services.video_gen_service import VideoGenService
from backend.services.music_service import MusicService, map_segments_to_seconds
from backend.services.audio_mix_service import AudioMixService
from backend.services.audio_library_service import AudioLibraryService
from backend.services.orchestrator import Orchestrator
from backend.services.long_video.orchestrator import LongVideoOrchestrator
from backend.services.shot_router import ShotRouter
from backend.services.asset_cache import AssetCache
from backend.services.generated_cache import GeneratedCache
from backend.lib.pipeline_mode import (
    PipelineMode, get_mode_config, resolve_mode,
)
from backend.lib.cloud_auth import CloudTemporaryUnavailableError, NotAuthenticatedError

# Phase 2.11f — in-house video composer (replaces OpenMontage AGPL import)
from backend.lib.video_compose import VideoCompose, _compose_timeout


class PipelineCancelled(RuntimeError):
    """Raised when the user stops a project while the local pipeline is running."""


class PipelineAuthRequired(RuntimeError):
    """Raised when cloud auth is genuinely invalid and the run must pause."""


_AUTH_MESSAGE_HINTS = (
    "cloud not authenticated",
    "not authenticated",
    "refresh rejected",
    "session has expired",
    "refresh temporarily unavailable",
    "subscription inactive",
)


def _find_auth_cause(exc: BaseException) -> Optional[BaseException]:
    """Walk the exception chain (__cause__ / __context__) for an auth/cloud
    root cause that should make the run resumable instead of hard-failing.

    Returns the resumable exception to re-raise (a NotAuthenticatedError or
    CloudTemporaryUnavailableError), or None if no auth cause is found. Used by
    _stage_assets so a token hiccup that surfaced as UnmatchedChunkError gets
    classified as retry/auth-paused, not as a permanent "unfilmable script"
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (NotAuthenticatedError, CloudTemporaryUnavailableError)):
            return cur
        msg = str(cur).lower()
        if any(h in msg for h in _AUTH_MESSAGE_HINTS):
            # Surface as temporary so the worker retries from checkpoint rather
            # than forcing the user to re-login (which a true NotAuthenticated
            # would). A genuinely revoked token will keep failing and eventually
            # the refresh path raises NotAuthenticatedError explicitly.
            return CloudTemporaryUnavailableError(str(cur))
        cur = cur.__cause__ or cur.__context__
    return None


#
#    「两万一千元」被劈成两条,观众看到「两万」以为就是两万。这类错比断句难看严重得多。
#    一条以「由」结尾,读起来是断的。
#
# 两条都只是**否决候选点**,不改优先级:软停顿标点 → 中文词边界 → 空格 → 兜底硬切。
# 全被否决时退回兜底硬切(宁可难看,也不能不切 —— 不切就冲出画面)。
_NUMBERISH = set("0123456789０１２３４５６７８９"
                 "零一二三四五六七八九十百千万亿两点."
                 "%％点")
# 以这些字结尾的一条读起来是断的(虚词/介词/连词/量词头)
_DANGLING_TAIL = set("的地得和与及由在把被对为从向比让使给就而但或之等於于跟同并且是有个只每这那")


def _cuts_a_number(text: str, cut: int) -> bool:
    """切点两边都是数字/量词的一部分 → 会把一个数劈成两半。"""
    if cut <= 0 or cut >= len(text):
        return False
    return text[cut - 1] in _NUMBERISH and text[cut] in _NUMBERISH


def _dangling_tail(text: str, cut: int) -> bool:
    """切完之后前一条以虚词结尾 → 读起来是断的。"""
    if cut <= 0 or cut > len(text):
        return False
    return text[cut - 1] in _DANGLING_TAIL


class PipelineService:
    PIPELINE_DIR = Path.home() / ".media-buddy-oss" / "pipelines"

    def __init__(
        self,
        llm_provider: str = "auto",
        llm_model: Optional[str] = None,
        tts_provider: str = "azure_yunyang",
        mode: Optional[PipelineMode] = None,
        series: Optional[object] = None,  # backend.models.series.Series
    ):
        # Phase 2.7a — Series enables L1 hard isolation (LLM prompt injection).
        # The cache layer (L3/L4) stays globally shared; Series-specific style
        # is enforced via system-prompt prepend, NOT via cache key.
        self.series = series

        # wave config + duration filter come from the active mode.
        # Series.pipeline_mode wins over the constructor arg.
        if series is not None and getattr(series, "pipeline_mode", None):
            try:
                self.mode = PipelineMode(getattr(series, "pipeline_mode"))
            except ValueError:
                self.mode = resolve_mode(mode)
        else:
            self.mode = resolve_mode(mode)
        mode_cfg = get_mode_config(self.mode)

        self.llm = LLMClient(provider=llm_provider, model=llm_model, series=series)
        self.tts = TTSService()
        # Local visual library is kept as a manual UI feature only. It no longer
        # participates in production by default: stale cached clips can pollute
        # new topics, and stock assets should not become a reusable local pool.
        _enable_library_pipeline = (
            os.environ.get("MEDIA_BUDDY_ENABLE_LOCAL_LIBRARY_IN_PIPELINE", "0") == "1"
        )
        self.library = LibraryService() if _enable_library_pipeline else None
        if not _enable_library_pipeline:
            logger.info(
                "local visual library pipeline integration OFF; "
                "all production searches go to remote stock providers",
            )
        # Pass mode-driven source waves + duration cap to the footage service.
        self.footage = FootageService(
            library=self.library,
            source_waves=mode_cfg.get("source_waves"),
            max_clip_duration=mode_cfg.get("max_clip_duration_seconds", 60.0),
            preferred_clip_duration=mode_cfg.get(
                "preferred_clip_duration_seconds", 30.0,
            ),
        )
        self.video_gen = VideoGenService()
        self.compose_tool = VideoCompose()
        self.critic = None  # no separate critic model in this build
        self.audio_mix = AudioMixService()
        # Phase 2.9b — wire audio library as L0 cache + auto-ingest target
        _enable_audio_library_pipeline = (
            os.environ.get("MEDIA_BUDDY_ENABLE_LOCAL_AUDIO_LIBRARY_IN_PIPELINE", "0") == "1"
        )
        self.audio_library = (
            AudioLibraryService() if _enable_audio_library_pipeline else None
        )
        if not _enable_audio_library_pipeline:
            logger.info(
                "local audio library pipeline integration OFF; "
                "production music searches go to remote music providers",
            )
        self.music = MusicService(
            audio_mix=self.audio_mix,
            audio_library=self.audio_library,
        )

        # USE_LEGACY_CHUNK_ENGINE=1 escape hatch.
        self.shot_router = ShotRouter(mode=self.mode)
        self.asset_cache = AssetCache()
        self.generated_cache = GeneratedCache()

        # Series → orchestrator moderation context
        industry = getattr(series, "industry_tag", "") or "" if series else ""
        forbidden = list(getattr(series, "forbidden_topics", None) or []) if series else None

        self.orchestrator = Orchestrator(
            self.llm, self.footage, self.critic, self.video_gen,
            self.shot_router,
            asset_cache=self.asset_cache,
            generated_cache=self.generated_cache,
            mode=self.mode,
            industry=industry,
            forbidden_topics=forbidden,
        )
        # Long video runs on its OWN isolated orchestrator (same deps, separate
        # class) so long-video iteration can never touch the short-video path.
        # Routed by output_format in _stage_assets_orchestrator.
        self.long_orchestrator = LongVideoOrchestrator(
            self.llm, self.footage, self.critic, self.video_gen,
            self.shot_router,
            asset_cache=self.asset_cache,
            generated_cache=self.generated_cache,
            mode=self.mode,
            industry=industry,
            forbidden_topics=forbidden,
        )

        self._active_stage: Optional[str] = None

    @staticmethod
    def _script_hash(script: str) -> str:
        return hashlib.sha256((script or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _artifact_path_exists(path_value: object) -> bool:
        if not path_value:
            return False
        try:
            return Path(str(path_value)).exists()
        except Exception:
            return False

    @classmethod
    def _footage_checkpoint_files_exist(cls, footage_results: list) -> bool:
        if not footage_results:
            return False
        for item in footage_results:
            local = (
                item.get("local_path")
                if isinstance(item, dict)
                else getattr(item, "local_path", None)
            )
            if not cls._artifact_path_exists(local):
                return False
        return True

    @classmethod
    def _assets_checkpoint_matches(
        cls,
        artifacts: dict,
        *,
        script: str,
        tts_provider: str,
        tts_speed: float,
        align_words: bool = True,
    ) -> bool:
        manifest = artifacts.get("asset_manifest") or {}
        # ⚠️ 字幕开关变了就不能复用配音缓存。关字幕那次跳过了 whisper 强制对齐,
        #    sidecar(.words.json)里是【估算】时间轴;之后再开字幕若直接复用,字幕会对不准。
        #    反向(真毫秒 → 关字幕)复用是安全的,所以只拦「估算 → 要真毫秒」这一向。
        if align_words and manifest.get("align_words") is False:
            return False
        requested_tts = str(tts_provider or "")
        checkpoint_tts = str(manifest.get("tts_provider") or "")
        requested_tts_speed = float(tts_speed or 1.0)
        checkpoint_tts_speed = float(manifest.get("tts_speed") or 1.0)
        requested_script_hash = cls._script_hash(script)
        checkpoint_script_hash = str(manifest.get("script_hash") or "")
        tts_path = manifest.get("tts_path")
        footage_results = manifest.get("footage_results") or []

        tts_matches = (
            requested_tts in ("", "none", "skip")
            or (
                checkpoint_tts == requested_tts
                and abs(checkpoint_tts_speed - requested_tts_speed) < 0.001
                and checkpoint_script_hash == requested_script_hash
                and cls._artifact_path_exists(tts_path)
            )
        )
        return tts_matches and cls._footage_checkpoint_files_exist(footage_results)

    @classmethod
    def _voice_checkpoint_matches(
        cls,
        artifacts: dict,
        *,
        script: str,
        tts_provider: str,
        tts_speed: float,
        align_words: bool = True,
    ) -> bool:
        requested_tts = str(tts_provider or "")
        if requested_tts in ("", "none", "skip"):
            return True
        # ⚠️ 字幕开关变了就不能复用配音缓存。关字幕那次跳过了 whisper 强制对齐,
        #    sidecar(.words.json)里是【估算】时间轴;之后再开字幕若直接复用,字幕会对不准。
        #    反向(真毫秒 → 关字幕)复用是安全的,所以只拦「估算 → 要真毫秒」这一向。
        if align_words and artifacts.get("align_words") is False:
            return False
        return (
            str(artifacts.get("tts_provider") or "") == requested_tts
            and abs(float(artifacts.get("tts_speed") or 1.0) - float(tts_speed or 1.0)) < 0.001
            and str(artifacts.get("script_hash") or "") == cls._script_hash(script)
            and cls._artifact_path_exists(artifacts.get("tts_path"))
        )

    def _raise_if_stopped(self, project_id: str) -> None:
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project is not None and project.status == "stopped":
                raise PipelineCancelled("Project stopped by user")
        finally:
            db.close()

    def _write_stage_progress(
        self,
        project_id: str,
        stage: str,
        status: str,
        artifacts: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        """Write user-facing fine-grained pipeline progress."""
        write_progress(
            self.PIPELINE_DIR,
            project_id,
            stage,
            status,
            artifacts or {},
            error=error,
        )
        if status == "running":
            self._active_stage = stage

        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project is None:
                return
            if status in ("running", "failed", "waiting_cloud", "paused_auth_required"):
                project.current_stage = stage
            elif status in ("completed", "skipped") and project.current_stage == stage:
                project.current_stage = None
            db.commit()
        except Exception as exc:
            logger.warning("failed to update current_stage for %s: %s", project_id, exc)
        finally:
            db.close()

    def run(self, project_id: str, project_data: dict) -> dict:
        self.PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
        project_dir = self.PIPELINE_DIR / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        self.llm.set_project_id(project_id)
        self.tts.set_project_id(project_id)

        mode = project_data.get("mode", "script")
        output_format = _normalize_output_format(
            project_data.get("output_format", "youtube_landscape")
        )
        tts_provider = project_data.get("tts_provider", "azure_yunyang")

        # Bind a cost counter for the WHOLE render (script + footage + TTS +
        # compose) so per-video AI cost is captured and attributed. The
        # orchestrators reuse this one instead of binding their own. Detached in
        # the finally below; the summary is returned for the worker to persist.
        from backend.services.llm_call_counter import (
            LLMCallCounter, set_active_counter, get_active_counter, set_active_identity,
        )
        _owns_cost_counter = get_active_counter() is None
        if _owns_cost_counter:
            set_active_counter(LLMCallCounter())
        set_active_identity(
            project_id=project_id, user_id=project_data.get("user_id"),
            output_format=output_format,
        )
        _cost_summary = None
        tts_speed = float(project_data.get("tts_speed") or 1.0)
        # 字幕字号缩放系数(客户拖拉杆自选):默认 1.0,夹在 [0.5,2.0]。乘到默认字号上。
        try:
            subtitle_font_scale = max(0.5, min(2.0, float(project_data.get("subtitle_font_scale") or 1.0)))
        except (TypeError, ValueError):
            subtitle_font_scale = 1.0
        include_subtitles = bool(project_data.get("include_subtitles", True))
        # 背景音乐开关:默认 False(不加 BGM,躲 YouTube 版权投诉)。客户在出片设置里打开才加。
        include_music = bool(project_data.get("include_music", False))
        framing_style = str(project_data.get("framing_style") or "fill").strip().lower()

        # ElevenLabs 更稳,长视频也走它。应急回退:设 MEDIA_BUDDY_LONG_VIDEO_AZURE_ONLY=1
        if os.environ.get("MEDIA_BUDDY_LONG_VIDEO_AZURE_ONLY", "0").strip().lower() in ("1", "true", "yes", "on") \
                and not _is_short_output_format(output_format) and str(tts_provider).startswith("elevenlabs"):
            # 例外:高级英语配音 —— 客户在英文输出里显式选了美音 ElevenLabs(Rachel/Adam/…)
            # 就尊重,长视频也用 ElevenLabs、不回退 Azure(需求:所有英语流程都可用高级配音)。
            _en_out = str(project_data.get("output_language") or "zh").lower() == "en"
            _adv_en = _en_out and str(tts_provider) in _registry_en_eleven()
            if not _adv_en:
                logger.info("MEDIA_BUDDY_LONG_VIDEO_AZURE_ONLY=1; long video tts_provider=%s -> azure_yunyang",
                            tts_provider)
                tts_provider = "azure_yunyang"

        # 输出语言守卫:出英文视频但选的不是"会说英文的音色"→ 强制换英文默认。
        _is_en_output = str(project_data.get("output_language") or "zh").lower() == "en"
        # 说英文带英音/口音,所以英文短视频一律换成【美音】ElevenLabs 默认(Rachel)。
        # 用户显式选了美音音色(Rachel/Adam/Antoni/Bella/Domi)则保留不动。可用
        # MEDIA_BUDDY_DEFAULT_SHORT_VOICE_EN 改默认。长视频已在上面回退 Azure,不受影响。
        # 英文 ElevenLabs 白名单(高级英语配音,美音+英音)。⚠️必须与 api/tts.py 的
        # _ELEVENLABS_EN_CATALOG、上面长视频例外的内联集合三处一致。
        _US_EN_ELEVEN = _registry_en_eleven()
        if _is_en_output and str(tts_provider).startswith("elevenlabs") \
                and str(tts_provider) not in _US_EN_ELEVEN:
            en_voice = os.environ.get(
                "MEDIA_BUDDY_DEFAULT_SHORT_VOICE_EN", "elevenlabs_rachel",
            ).strip() or "elevenlabs_rachel"
            logger.info("output_language=en: %s isn't US-English; default to %s",
                        tts_provider, en_voice)
            tts_provider = en_voice
        # 反向守卫(高级英语配音):英文 ElevenLabs 音色(Rachel/Adam/…)只服务英文输出。
        # 若输出非英文却带着英文音色(前端切语言漏重置的边角),换回中文默认千问,
        # 避免"美音念中文"。后端二次校验,不只靠前端(需求硬要求)。
        # ⚠️ 高级英语音色(`hdv_*`)同样只服务英文输出 —— 让美音去念中文更难听。
        if not _is_en_output and (str(tts_provider) in _US_EN_ELEVEN
                                  or _is_premium_en_voice(tts_provider)):
            logger.info("non-en output with English ElevenLabs voice %s; coercing to qwen_cherry",
                        tts_provider)
            tts_provider = "qwen_cherry"
        # ElevenLabs 多语种(现已确保是美音)视作英文音色;只有既非英文 Azure、又非
        # ElevenLabs 的中文 Azure 音色才换 azure_aria(长视频 en 落这里→美音英文 Azure)。
        # 千问音色多语种(language_type 随文本自动判定),英文内容也能用 → 豁免,别把用户
        #    没有它,客户选的 Google 高级英语音色会在这里被**静默换成 azure_aria**
        #    —— 试听是对的、出片是另一个人。见该函数的说明。
        if _is_en_output \
                and not _is_english_voice(tts_provider) \
                and not _is_premium_en_voice(tts_provider) \
                and not str(tts_provider).startswith("elevenlabs") \
                and not str(tts_provider).startswith("qwen_"):
            logger.info("output_language=en but tts_provider=%s not English; override azure_aria",
                        tts_provider)
            tts_provider = "azure_aria"

        orientation = {
            "youtube_shorts": "portrait",
            "tiktok": "portrait",
            "instagram_reels": "portrait",
        }.get(output_format, "landscape")

        # (connector-only sentence split — skips the LLM multi-shot split that
        # inflates a 10-min script to ~155 chunks → ~80, and skips that split's
        # ~50 LLM calls). Short video stays fine-grained (flag False → identical
        # behavior). Read by _expand_sentences_for_pipeline.
        self._coarse_shots = output_format == "youtube_landscape"

        billing_result: Optional[dict] = None
        try:
            self._raise_if_stopped(project_id)
            self._emit_job_progress("script", 12)
            script = self._stage_script(
                project_id, project_dir, project_data, mode, output_format,
                tts_provider=tts_provider,
            )
            self._raise_if_stopped(project_id)
            # Footage selection is the LONG stage — surface it so a slow render
            self._emit_job_progress("assets", 30)
            tts_path, footage_results = self._stage_assets(
                project_id, project_dir, script, tts_provider, orientation, tts_speed,
                output_format=output_format,
                planned_seconds=project_data.get("duration_seconds"),
                align_words=include_subtitles,
                # 出片语言的【设定】—— 一路传到强制对齐,告诉 whisper 听哪种话。
                output_language=str(project_data.get("output_language") or "zh").lower(),
            )
            self._raise_if_stopped(project_id)
            self._emit_job_progress("music", 75)
            bgm_path = self._stage_music(
                project_id, project_dir, script, tts_path, include_music,
                project_title=project_data.get("name")
                or project_data.get("title") or "",
            )
            self._raise_if_stopped(project_id)
            self._emit_job_progress("compose", 85)
            output_path = self._stage_compose(
                project_id, project_dir, script, tts_path, bgm_path, footage_results,
                output_format, include_subtitles,
                project_data.get("name") or project_data.get("title") or "",
                framing_style,
                output_language=str(project_data.get("output_language") or "zh").lower(),
                subtitle_language=project_data.get("subtitle_language"),
                subtitle_font_scale=subtitle_font_scale,
            )
            # Post-render health gate. A HARD defect (a few-second clip / empty /
            # CTA-only script) raises VideoHealthError HERE — BEFORE settlement —
            # so the job fails and the customer is NOT charged. SOFT anomalies are
            # only recorded (suspect-videos board). Evaluation errors never raise.
            self._run_health_gate(
                project_id, project_dir, Path(output_path), script,
                project_data, output_format,
            )

            # tts_provider 是经上文音色守卫后的最终值;_US_EN_ELEVEN / _is_en_output 均在 run() 内已定义。
            #
            # 🚨 **口径从注册表来,和音色守卫是同一个定义。**
            #    而守卫认的是 `hdv_*` → **客户用 Google 高级配音,守卫正确放行,
            #    却一分钱没被收**。两处各写各的,必然分叉。
            #
            #    本仓只发布尔标志,不决定金额。
            _premium_en = bool(_is_en_output and _reg_is_premium_english(tts_provider))
            billing_result = self._settle_video_billing(
                project_id,
                Path(output_path),
                planned_seconds=project_data.get("duration_seconds"),
                premium_english_dub=_premium_en,
            )
        except PipelineCancelled as exc:
            if self._active_stage:
                existing = read_progress(self.PIPELINE_DIR, project_id, self._active_stage)
                self._write_stage_progress(
                    project_id,
                    self._active_stage,
                    "stopped",
                    existing.get("artifacts", {}) if existing else {},
                    error=str(exc),
                )
            raise
        except CloudTemporaryUnavailableError as exc:
            if self._active_stage:
                existing = read_progress(self.PIPELINE_DIR, project_id, self._active_stage)
                self._write_stage_progress(
                    project_id,
                    self._active_stage,
                    "waiting_cloud",
                    existing.get("artifacts", {}) if existing else {},
                    error=str(exc),
                )
            raise
        except NotAuthenticatedError as exc:
            if self._active_stage:
                existing = read_progress(self.PIPELINE_DIR, project_id, self._active_stage)
                self._write_stage_progress(
                    project_id,
                    self._active_stage,
                    "paused_auth_required",
                    existing.get("artifacts", {}) if existing else {},
                    error=str(exc),
                )
            raise PipelineAuthRequired(str(exc)) from exc
        except Exception as exc:
            message = str(exc).lower()
            if "cloud not authenticated" in message or "session has expired" in message:
                if self._active_stage:
                    existing = read_progress(self.PIPELINE_DIR, project_id, self._active_stage)
                    self._write_stage_progress(
                        project_id,
                        self._active_stage,
                        "paused_auth_required",
                        existing.get("artifacts", {}) if existing else {},
                        error=str(exc),
                    )
                raise PipelineAuthRequired(str(exc)) from exc
            if self._active_stage:
                existing = read_progress(self.PIPELINE_DIR, project_id, self._active_stage)
                if not existing or existing.get("status") != "failed":
                    self._write_stage_progress(
                        project_id,
                        self._active_stage,
                        "failed",
                        existing.get("artifacts", {}) if existing else {},
                        error=str(exc),
                    )
            raise
        finally:
            self.llm.set_project_id(None)
            self.tts.set_project_id(None)
            if _owns_cost_counter:
                try:
                    c = get_active_counter()
                    if c is not None:
                        _cost_summary = c.summary()
                except Exception:
                    logger.debug("cost summary capture failed", exc_info=True)
                set_active_counter(None)
        return {
            "success": True,
            "output_path": str(output_path),
            "pipeline_dir": str(project_dir),
            "billing": billing_result,
            # Conservative per-video AI cost (llm/flux/tts breakdown + total).
            # the worker persists it; None if this run didn't own the counter.
            "ai_cost": _cost_summary,
        }

    def _settle_video_billing(
        self,
        project_id: str,
        output_path: Path,
        *,
        planned_seconds: Optional[float] = None,
        premium_english_dub: bool = False,
    ) -> Optional[dict]:
        try:
            from backend.lib.cloud_gateway import get_default_gateway

            duration = self._probe_audio_duration(output_path)
            gw = get_default_gateway()
            if not gw.is_authenticated():
                logger.info("skip video billing settlement; cloud not authenticated")
                return None
            import asyncio

            import uuid as _uuid
            return asyncio.run(gw.settle_video_cost(
                project_id=project_id,
                # Cloud settle schema wants INT seconds (z.number().int()).
                final_video_seconds=int(max(1.0, float(duration or 0.0))),
                planned_video_seconds=int(planned_seconds) if planned_seconds else None,
                # ...and a UUID idempotency_key (z.string().uuid()). Derive a
                # STABLE uuid5 from the project so re-settlement is idempotent.
                idempotency_key=str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{project_id}:final-settlement")),
                premium_english_dub=premium_english_dub,
            ))
        except Exception as exc:
            # LOUD, not silent: a swallowed settle failure = a delivered-but-uncharged
            # video (revenue leak). The cloud creates a `cost_pending` session on a
            # partial settle, which the worker's sweep re-settles with the SAME uuid5
            # key. A HARD failure before any session is created has no net — hence
            logger.error(
                "video billing settlement FAILED for %s: %s", project_id, exc, exc_info=True
            )
            return None

    def _emit_job_progress(self, stage: str, pct: int) -> None:
        """Report mid-render stage progress to the queue row (if the caller wired
        a ``_job_progress_cb``). Without this, video_jobs.progress sits at the
        worker's initial pct=5 for the whole render — making the long footage
        stage look frozen/hung. Best-effort; never breaks a render."""
        cb = getattr(self, "_job_progress_cb", None)
        if cb is None:
            return
        try:
            cb(stage, pct)
        except Exception:
            logger.debug("job progress callback failed (non-fatal)", exc_info=True)

    def _run_health_gate(
        self, project_id, project_dir, output_path: Path, script: str,
        project_data: dict, output_format: str,
    ) -> None:
        """Classify the freshly-composed video + persist a video_health row. A
        HARD verdict raises VideoHealthError (→ job fails BEFORE settlement → no
        charge). An evaluation error is swallowed: a monitoring bug must NEVER
        break an otherwise-good video — only the explicit hard verdict raises."""
        from backend.lib.video_health import (
            evaluate_video_health, persist_video_health, VideoHealthError,
        )
        try:
            duration = self._probe_audio_duration(Path(output_path))
            planned = project_data.get("duration_seconds")
            script_issue = _short_script_quality_issue(
                script, output_format, planned,
                tts_provider=str(project_data.get("tts_provider") or ""),
                tts_speed=float(project_data.get("tts_speed") or 1.0),
            )
            result = evaluate_video_health(
                project_dir=project_dir,
                output_duration=duration,
                planned_seconds=float(planned) if planned else None,
                script_issue=script_issue,
                output_format=output_format,
            )
            # M6:把「预估 vs 实际」一起记下,让语速标定的漂移可被发现。
            # 预估必须取**出厂那一版**(_last_gate_result),不是某个被否的中间版本。
            from backend.lib.pacing_feedback import pacing_row_from_gate

            persist_video_health(
                result, project_id=project_id, user_id=project_data.get("user_id"),
                pacing=pacing_row_from_gate(
                    getattr(self, "_last_gate_result", None),
                    voice=str(project_data.get("tts_provider") or ""),
                    speed=float(project_data.get("tts_speed") or 1.0),
                ),
            )
        except Exception:
            logger.exception(
                "health gate evaluation errored for %s (skipping gate)", project_id
            )
            return
        if result.is_hard:
            logger.warning("video %s HARD health fail: %s", project_id, result.flags)
            top = result.top_red() or {}
            raise VideoHealthError(
                "output_health_failed",
                f"这条视频没生成好（{top.get('detail', '质量异常')}），"
                "已自动判为失败，请重试。",
            )

    def _fmt_context(self, prompt: str) -> str:
        """领域匹配用的上下文 = 本次主题 + 频道(Series)定位。"""
        parts = [str(prompt or "")]
        s = getattr(self, "series", None)
        if s is not None:
            parts.append(str(getattr(s, "name", "") or ""))
            parts.append(str(getattr(s, "description", "") or ""))
        return " ".join(p for p in parts if p).strip()

    def _stage_script(
        self, project_id, project_dir, project_data, mode, output_format,
        tts_provider: str = "",
    ) -> str:
        """`tts_provider` 必须由 run() 显式传入**生效后**的音色(字数预算按音色算)。
        run() 对音色有一串覆盖规则(英文出片改写等)且不写回 project_data,
        所以这里不能自己去读 project_data.tts_provider —— 会拿到覆盖前的旧值。"""
        # 「执行文案」verbatim:客户甩定稿要求一字不改 → 跳过 CTA 合规删句(送礼/抽奖照上)。
        # 主路径(1379)与恢复路径(下面这条,重渲/续跑读已存脚本)都要一致跳,否则重试时
        # 会把客户原文又给合规删了 —— 恰恰在渲染失败重跑时破坏「一字不改」承诺。
        verbatim = bool(project_data.get("verbatim"))
        existing = read_progress(self.PIPELINE_DIR, project_id, "script")
        if existing and existing.get("status") == "completed":
            return _sanitize_script_for_production(existing["artifacts"]["script"]["text"], skip_cta_filter=verbatim)

        self._raise_if_stopped(project_id)
        self._write_stage_progress(project_id, "script", "running")
        duration_hint: Optional[int] = None
        # 在分支之前定下来:两条分支(自带稿 / 生成稿)后面都要用它。
        # 只在 else 里赋值 → mode=="script" 时后续引用直接 NameError
        output_language = str(project_data.get("output_language") or "zh").lower()
        if mode == "script":
            script = project_data["script_text"]
        else:
            # Phase 2.11b — pass duration hint so generate_script's word-count
            # target reflects the user's actual intent. Falls back to Series
            # target or 60s default.
            duration_hint = int(
                project_data.get("duration_seconds")
                or (getattr(self.series, "duration_target_seconds", None) if self.series else None)
                or 60
            )
            # 语速:传进脚本生成,让"目标字数=时长×语速"—— 语速越快写越多字,成片才等于设定时长。
            try:
                tts_speed = max(0.7, min(2.0, float(project_data.get("tts_speed") or 1.0)))
            except Exception:
                tts_speed = 1.0
            prompt = project_data.get("prompt", "")
            if output_format == "youtube_landscape":
                # 长视频脚本 v2(与短视频完全隔离):DeepSeek `:online` 联网抓真数据 +
                # 长视频专用"通俗易懂/每个硬事实配比喻/反差脊"策略,直接写一条连贯的
                # ~10 分钟口播。本路径**不碰**短视频的 generate_script/_build_script_system_prompt。
                # 关掉 MEDIA_BUDDY_LONG_SCRIPT_V2、或 v2 失败/过短 → 回退旧的 outline→expand。
                from backend.services.long_video.script_gen import (
                    long_script_v2_enabled,
                    generate_long_script,
                )
                script = None
                if long_script_v2_enabled():
                    try:
                        script = generate_long_script(
                            self.llm, prompt, duration_seconds=duration_hint, speed=tts_speed,
                        )
                        if not script or len(script.strip()) < 800:
                            raise ValueError(
                                f"long-script v2 too short: {len(script or '')} chars"
                            )
                    except Exception:
                        logger.warning(
                            "long-script v2 (DeepSeek grounded) failed; "
                            "falling back to outline→expand", exc_info=True,
                        )
                        script = None
                if not script:
                    # 旧路径(outline → per-section expand)—— v2 关闭或失败时的兜底,保持原样。
                    # 多样性:注入一个随机抽取的叙事骨架(该用户自己 + 全局共享 public_seed 池)。
                    from backend.services.studio_agent import (
                        _generate_long_script_via_outline,
                        build_random_formula_block,
                    )
                    formula_block = ""
                    try:
                        _fdb = SessionLocal()
                        try:
                            formula_block = build_random_formula_block(
                                _fdb, "youtube_long",
                                project_data.get("user_id"), context=self._fmt_context(prompt),
                            )
                        finally:
                            _fdb.close()
                    except Exception:
                        formula_block = ""
                    target_chars = max(2000, int(duration_hint * 4.3 * tts_speed))
                    try:
                        script = _generate_long_script_via_outline(
                            self.llm, prompt, formula_block, target_chars,
                            duration_seconds=duration_hint,
                        )
                    except Exception:
                        logger.warning(
                            "long-video outline→expand failed in pipeline; "
                            "falling back to single-call generate_script", exc_info=True,
                        )
                        script = self.llm.generate_script(
                            prompt, output_format, duration_hint_seconds=duration_hint,
                            system_context=formula_block, speed=tts_speed,
                        )
            else:
                # 短视频:注入一个**随机抽取**的「讲法套路」软建议(该用户自己 +
                # 全局共享池),让同题多条走不同钩子/节奏 → 降低内容农场判定风险。
                # 走 system_context(被硬规则前后夹住,不会覆盖钩子五型/禁词/安全CTA)。
                short_formula = ""
                try:
                    from backend.services.studio_agent import build_random_formula_block
                    _fdb = SessionLocal()
                    try:
                        short_formula = build_random_formula_block(
                            _fdb, "short_video",
                            project_data.get("user_id"), context=self._fmt_context(prompt),
                        )
                    finally:
                        _fdb.close()
                except Exception:
                    short_formula = ""
                script = self.llm.generate_script(
                    prompt, output_format, duration_hint_seconds=duration_hint,
                    system_context=short_formula, speed=tts_speed,
                    tts_provider=tts_provider, output_language=output_language,
                )
                # 重生成一次、取较长的一版,避免出 30 多秒的半截视频。
                # 按出片语言的口径量 —— 中文数字数、英文数词数。拿中文字数口径去量
                # 英文稿会差一个数量级(170 词 ≈ 950 字符),这道兜底就等于形同虚设。
                from backend.lib.lang_profile import profile_for

                _prof = profile_for(output_language)
                _min_units = _prof.min_units(duration_hint, tts_provider, tts_speed)
                if _prof.count(script or "") < _min_units:
                    alt = self.llm.generate_script(
                        prompt, output_format, duration_hint_seconds=duration_hint,
                        system_context=short_formula, speed=tts_speed,
                        tts_provider=tts_provider, output_language=output_language,
                    )
                    if _prof.count(alt or "") > _prof.count(script or ""):
                        script = alt
                # 【已摘除事后截断】原来这里调 _truncate_short_script 从尾部砍字。
                # 那等于"长度超了就优先牺牲故事完整性",方向反了,而且砍完不复检 ——
                # 它会把**刚修好的结尾**又砍掉。
                # 现在长度归 script_gate 的三带模型管:超了就压缩正文(结尾逐字保留),
                # 压不动就放长出片 + 告警,永远不砍尾巴。

        sanitized_script = _sanitize_script_for_production(script, skip_cta_filter=verbatim)
        if mode != "script" and _is_short_output_format(output_format):
            sanitized_script = self._ensure_short_generated_script(
                project_id=project_id,
                script=sanitized_script,
                prompt=project_data.get("prompt", ""),
                output_format=output_format,
                duration_hint_seconds=duration_hint,
                tts_provider=tts_provider,
                tts_speed=tts_speed,
                output_language=output_language,
            )
        if sanitized_script != script:
            logger.info("production script CTA promise sanitized for project=%s", project_id)
            script = sanitized_script

        # 顺序本来就对(闸在制作之前),锁要防的是**以后新增的路径绕过闸** ——
        # 这种事已经发生过一次(裸 return 绕过长度保险丝)。顺序靠约定,不变式靠守卫。
        try:
            from backend.lib.script_lock import lock_for_script

            self._script_lock = lock_for_script(
                gate_result=getattr(self, "_last_gate_result", None),
                mode=mode,
                predicted_seconds=0.0,
            )
            logger.info("script lock: project=%s source=%s", project_id,
                        self._script_lock.lock_source)
        except Exception:  # noqa: BLE001 — 上锁失败不该挡住出片
            logger.warning("script lock failed for project=%s", project_id, exc_info=True)

        # 输出语言:中文草稿 → 英文视频。在定稿净化(+短视频超短校验,按中文长度算)之后、
        # 写 script.txt 之前翻译,这样下游拆句/TTS/字幕/SEO 全吃译文;失败保留原文,不硬挂。
        #
        # ⚠️ TODO(英文路径时长无控制)——**已知缺口,别以为这里管了**:
        # 上面所有长度控制(三带模型 / 结构预算 / script_gate)都作用在**中文稿**上,
        # 翻译发生在它们**之后**,译文长度没有任何环节再量一次。
        # 中译英长度变化很大,而英文按「词/秒」计,与中文的「字/秒」不是一个量纲,
        # 所以不能直接套 tts_pacing 的表。
        # 影响面:elevenlabs_amy 月产约 236 条(第二大音色),这些片子的成片时长
        # 目前完全不受控。修法需按词/秒单独建一张 pacing 表 + 翻译后复检,
        _native_en = os.environ.get("MB_NATIVE_EN", "1") != "0"
        if _should_translate(output_language, native_en=_native_en):
            try:
                translated = self.llm.translate_script(script, output_language)
                if translated and translated.strip():
                    logger.info("translated script to %s for project=%s",
                                output_language, project_id)
                    # §12 英文长度复检:所有中文长度控制都作用在中文稿上,
                    # 翻译在它们之后 —— 译文长度此前从没人量过(约 236 条/月)。
                    try:
                        from backend.lib.english_gate import check_translated_script

                        translated, _acted = check_translated_script(
                            translated, duration_seconds=duration_hint,
                            voice=tts_provider, speed=tts_speed, llm=self.llm,
                        )
                    except Exception:  # noqa: BLE001 — 复检失败照发原译文
                        logger.warning("english length check failed; keeping translation",
                                       exc_info=True)
                    script = translated
            except Exception:
                logger.warning(
                    "script translation to %s failed; keeping source language",
                    output_language, exc_info=True,
                )
        # 语言守卫(对称,两个方向都管):成稿语言和订单要的对不上就翻回去。
        # 走到这里 script 可能来自原生生成、翻译、或应急模板稿(模板稿是中文的),
        # 所以这道检查放在**所有分支之后**,不管上游怎么错都能兜住。
        if _needs_language_rescue(script, output_language):
            try:
                fixed = self.llm.translate_script(script, output_language)
                if fixed and fixed.strip():
                    logger.warning(
                        "language rescue: draft language != output_language=%s; "
                        "force-translated for project=%s", output_language, project_id,
                    )
                    script = fixed
            except Exception:
                logger.warning(
                    "language rescue to %s failed; keeping draft for project=%s",
                    output_language, project_id, exc_info=True,
                )

        script_file = project_dir / "script.txt"
        script_file.write_text(script, encoding="utf-8")

        write_progress(
            self.PIPELINE_DIR,
            project_id,
            "script",
            "completed",
            {"script": {"text": script, "path": str(script_file)}},
        )
        self._write_stage_progress(
            project_id,
            "script",
            "completed",
            {"script": {"text": script, "path": str(script_file)}},
        )
        return script


    def _ensure_short_generated_script(
        self,
        *,
        project_id: str,
        script: str,
        prompt: str,
        output_format: str,
        duration_hint_seconds: int | float | None,
        tts_provider: str = "",
        tts_speed: float = 1.0,
        output_language: str = "zh",
    ) -> str:
        """Reject CTA-only / ultra-short generated short scripts before TTS.

        Batch production is expensive after this stage. If a low-cost writer
        returns only an outro like "评论区告诉我...", repair once with a stricter
        prompt. If it still fails, use a conservative topic-based fallback so
        the pipeline never spends TTS/footage money on a 4-second non-video.

        硬废稿闸(空稿/纯CTA/句数过少)之后,再过 script_gate 的长度+收口验收闸。
        两者分工:这里只兜"根本不是一篇稿子",长度与收口一律归 script_gate 管
        —— 后者绝不退模板稿(spec P4)。
        """
        issue = _short_script_quality_issue(
            script, output_format, duration_hint_seconds,
            tts_provider=tts_provider, tts_speed=tts_speed,
            output_language=output_language,
        )
        if not issue:
            return self._run_script_gate(
                project_id=project_id,
                script=script,
                duration_hint_seconds=duration_hint_seconds,
                tts_provider=tts_provider,
                tts_speed=tts_speed,
                output_language=output_language,
            )

        logger.warning(
            "short generated script failed quality gate for project=%s: %s",
            project_id,
            issue,
        )
        try:
            retry_prompt = _build_short_script_retry_prompt(
                prompt, issue, duration_hint_seconds,
            )
            retry = self.llm.generate_script(
                retry_prompt,
                output_format,
                duration_hint_seconds=int(duration_hint_seconds or 45),
                tts_provider=tts_provider, output_language=output_language,
            )
            retry = _sanitize_script_for_production(retry)
            retry_issue = _short_script_quality_issue(
                retry, output_format, duration_hint_seconds,
                tts_provider=tts_provider, tts_speed=tts_speed,
            )
            if not retry_issue:
                logger.info(
                    "short generated script repaired by retry for project=%s",
                    project_id,
                )
                # ⚠️ 修复稿**必须和正常稿走同一道闸**。
                # 早先这里直接 return,结果"本来就有问题"的稿子反而躲开了长度与收口
                return self._run_script_gate(
                    project_id=project_id,
                    script=retry,
                    duration_hint_seconds=duration_hint_seconds,
                    tts_provider=tts_provider,
                    tts_speed=tts_speed,
                    output_language=output_language,
                )
            logger.warning(
                "short generated script retry still invalid for project=%s: %s",
                project_id,
                retry_issue,
            )
        except Exception:
            logger.warning(
                "short generated script repair retry failed for project=%s",
                project_id,
                exc_info=True,
            )

        fallback = _sanitize_script_for_production(_build_emergency_short_script(prompt))
        fallback_issue = _short_script_quality_issue(
            fallback, output_format, duration_hint_seconds,
            tts_provider=tts_provider, tts_speed=tts_speed,
            output_language=output_language,
        )
        if fallback_issue:
            raise RuntimeError(
                f"short_script_quality_failed:{issue}; fallback={fallback_issue}"
            )
        logger.warning(
            "short generated script replaced with emergency fallback for project=%s",
            project_id,
        )
        return self._run_script_gate(
            project_id=project_id, script=fallback,
            duration_hint_seconds=duration_hint_seconds,
            tts_provider=tts_provider, tts_speed=tts_speed,
            output_language=output_language,
        )

    def _run_script_gate(
        self,
        *,
        project_id: str,
        script: str,
        duration_hint_seconds: int | float | None,
        tts_provider: str,
        tts_speed: float,
        output_language: str = "zh",
    ) -> str:
        """文字验收闸(长度 + 收口)。**任何异常都必须放行原稿。**

        闸内已经是 fail-open 的,这里再包一层是防"闸自己 import 挂了/签名对不上"
        这种低级故障也把出片带崩 —— 这道闸是加分项,不是必经之路。
        """
        try:
            from backend.lib.script_gate import run_script_gate

            res = run_script_gate(
                script_v1=script,
                duration_seconds=duration_hint_seconds,
                voice=tts_provider,
                speed=tts_speed,
                output_language=output_language,
                # getattr:series 在某些构造路径下不存在(桌面单机 / 测试直接
                # __new__),取不到就走 default 收口标准,不许因此崩掉。
                series=getattr(self, "series", None),
                llm=self.llm,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "script gate errored for project=%s; shipping script as-is",
                project_id, exc_info=True,
            )
            return script

        logger.info(
            "script gate project=%s version=%s rounds=%d calls=%d degraded=%s "
            "predicted=%.1fs checks=%s",
            project_id, res.chosen_version, res.rounds_used, res.llm_calls,
            res.degraded, res.chosen_predicted_seconds,
            [(c.round, c.version, c.length, c.closure_ok, c.source, c.closure_reason)
             for c in res.checks],
        )
        # 供 M6 回写校准用:预估必须对应**最终出厂的那一版**,否则校准数据被污染。
        self._last_gate_result = res
        return res.final_script

    def _stage_assets(
        self, project_id, project_dir, script, tts_provider, orientation,
        tts_speed=1.0, output_format="youtube_landscape",
        planned_seconds: int | float | None = None,
        align_words: bool = True,
        output_language: str | None = None,
    ):
        """

        1. Synthesize TTS for the whole script (still single audio file for now;
           per-chunk TTS will come in a later iteration)
        2. Build Chunks via ChunkEngine
        3. Run ChunkEngine.process_chunk() per chunk (text-critic + search + mm-stub)
        4. Return aggregated tts_path + flat shot list ordered by scene_index
        """
        # 花钱入口守卫(§10):没锁的脚本不许进 TTS / 素材 / 分镜。
        try:
            from backend.lib.script_lock import ensure_locked

            ensure_locked(getattr(self, "_script_lock", None),
                          stage="tts", project_id=project_id)
        except ImportError:
            pass

        existing = read_progress(self.PIPELINE_DIR, project_id, "assets")
        if existing and existing.get("status") == "completed":
            artifacts = existing.get("artifacts", {})
            if self._assets_checkpoint_matches(
                artifacts,
                script=script,
                tts_provider=str(tts_provider or ""),
                tts_speed=float(tts_speed or 1.0),
                align_words=align_words,
            ):
                arts = artifacts["asset_manifest"]
                tts_p = arts.get("tts_path")
                checkpoint_tts = str(arts.get("tts_provider") or "")
                checkpoint_tts_speed = float(arts.get("tts_speed") or 1.0)
                logger.info(
                    "resuming from completed assets checkpoint for project %s",
                    project_id,
                )
                self._write_stage_progress(
                    project_id,
                    "voice",
                    "completed" if tts_p else "skipped",
                    {
                        "tts_path": tts_p,
                        "tts_provider": checkpoint_tts,
                        "tts_speed": checkpoint_tts_speed,
                        "script_hash": arts.get("script_hash"),
                    },
                )
                self._write_stage_progress(project_id, "assets", "completed", artifacts)
                return (Path(tts_p) if tts_p else None), arts["footage_results"]
            logger.info(
                "assets checkpoint cannot be reused; regenerating assets "
                "(missing files or script/TTS mismatch)"
            )

        assets_dir = project_dir / "assets"
        assets_dir.mkdir(exist_ok=True)

        self._raise_if_stopped(project_id)

        # ── TTS 与取材并行（默认关，MEDIA_BUDDY_PARALLEL_TTS_ASSETS=1 打开）──
        #
        # 查过确实可行 —— 取材只吃 `script`（脚本文本），**不吃 tts_path**：
        #
        #     _stage_assets_orchestrator(db, project_id, script, assets_dir, ...)
        #
        # 它们串行纯粹是代码顺序写死的，不是技术依赖。
        #
        # ⚠️ 默认关，是因为收益在「TTS 内部并行」落地后大幅缩水：
        #
        #     并行前   TTS 13.6 分 + 取材 6.7 分 = 20.3 分
        #     TTS并行后 TTS 约 3 分 + 取材 6.7 分 =  9.7 分   ← 光这一步就省 10.6 分
        #     再叠本项  max(3, 6.7)             =  6.7 分   ← 只再省 3 分
        #
        #
        # ⚠️ 打开后有一个已知代价：`_guard_short_narration_duration`（配音短于
        #    灾难线就判失败）原本挡在取材**之前**，能省下取材的钱。并行后取材
        #    已经花出去了，守卫只剩「判失败」的作用，不再省钱。概率低，可接受。
        #
        # ⚠️ 线程安全：取材在自己的线程里开自己的 `SessionLocal()`（本来就是），
        #    不与 TTS 共享 DB session。两个阶段属于**同一个用户**，所以
        #    `set_worker_token` 那条进程级全局的铁律在这里不构成串台风险。
        _parallel_assets = os.environ.get(
            "MEDIA_BUDDY_PARALLEL_TTS_ASSETS", "0",
        ).strip().lower() in ("1", "true", "yes", "on")
        _assets_future = None
        _assets_pool = None
        if _parallel_assets:
            from concurrent.futures import ThreadPoolExecutor as _TAPool
            _assets_pool = _TAPool(max_workers=1, thread_name_prefix="mb-assets")
            _assets_future = _assets_pool.submit(
                self._run_assets_selection, project_id, script, assets_dir,
                orientation, output_format,
            )
            logger.info("assets stage started in parallel with TTS (project=%s)", project_id)

        def _close_assets_pool():
            """关掉并行取材的线程池。

            🚨 线程泄漏在 worker 进程里是**累积**的 —— 每渲一条片子漏一个。
               用 AST 扫过 `_stage_assets`：线程池启动后、取回结果前
               只有**一条**提前退出路径（TTS 硬失败时 `raise RuntimeError`），
               那里和正常取回结果处各调一次，就堵全了。
            """
            if _assets_pool is not None:
                _assets_pool.shutdown(wait=False)

        # Stage A: TTS for full script (single file, played across all chunks).
        # Voice has its own checkpoint so an assets-stage failure can resume
        # without paying for TTS again.
        tts_path: Optional[Path] = None
        voice_existing = read_progress(self.PIPELINE_DIR, project_id, "voice")
        if (
            voice_existing
            and voice_existing.get("status") == "completed"
            and self._voice_checkpoint_matches(
                voice_existing.get("artifacts", {}),
                script=script,
                tts_provider=str(tts_provider or ""),
                tts_speed=float(tts_speed or 1.0),
                align_words=align_words,
            )
        ):
            tts_p = voice_existing.get("artifacts", {}).get("tts_path")
            if tts_p:
                tts_path = Path(tts_p)
                logger.info(
                    "resuming from completed voice checkpoint for project %s",
                    project_id,
                )
                self._write_stage_progress(
                    project_id,
                    "voice",
                    "completed",
                    voice_existing.get("artifacts", {}),
                )
        elif tts_provider and tts_provider not in ("", "none", "skip"):
            self._write_stage_progress(
                project_id,
                "voice",
                "running",
                {
                    "tts_provider": str(tts_provider or ""),
                    "tts_speed": float(tts_speed or 1.0),
                    "script_hash": self._script_hash(script),
                    "align_words": bool(align_words),
                },
            )
            candidate = assets_dir / "narration.mp3"
            # align_words=False(字幕关了)→ TTS 内部跳过 faster-whisper 强制对齐,
            # 短片省 13~18s、长片省 3.5~6.5 分钟。见 tts_service.synthesize 的说明。
            tts_result = self.tts.synthesize(
                script, str(candidate), tts_provider, speed=tts_speed,
                align_words=align_words,
                # 把【出片语言的设定】传下去 —— 强制对齐按它告诉 whisper 听哪种话。
                # 不传的话那边只能按文本猜,混合中英文的稿子会猜反。
                output_language=output_language,
            )
            if tts_result.success and tts_result.output_path:
                tts_path = Path(tts_result.output_path)
                self._write_stage_progress(
                    project_id,
                    "voice",
                    "completed",
                    {
                        "tts_path": str(tts_path),
                        "tts_provider": str(tts_provider or ""),
                        "tts_speed": float(tts_speed or 1.0),
                        "script_hash": self._script_hash(script),
                        "align_words": bool(align_words),
                    },
                )
            else:
                if (
                    str(tts_provider).startswith("elevenlabs_")
                    or str(tts_provider).startswith("azure_")
                    or tts_provider in ("elevenlabs_tts", "azure_tts")
                ):
                    provider_label = "Azure Speech" if str(tts_provider).startswith("azure_") else "ElevenLabs"
                    error_msg = (
                        f"{provider_label} TTS failed for selected voice {tts_provider}: "
                        f"{tts_result.error or 'unknown error'}"
                    )
                    logger.error(error_msg)
                    self._write_stage_progress(
                        project_id,
                        "voice",
                        "failed",
                        {
                            "tts_provider": str(tts_provider or ""),
                            "tts_speed": float(tts_speed or 1.0),
                        },
                        error=error_msg,
                    )
                    write_progress(
                        self.PIPELINE_DIR,
                        project_id,
                        "assets",
                        "failed",
                        {
                            "asset_manifest": {
                                "tts_path": None,
                                "tts_provider": str(tts_provider or ""),
                                "tts_speed": float(tts_speed or 1.0),
                                "script_hash": self._script_hash(script),
                                "footage_results": [],
                            },
                            "error": error_msg,
                        },
                    )
                    _close_assets_pool()   # ← 唯一的提前退出路径，别漏
                    raise RuntimeError(error_msg)
                logger.warning(
                    "TTS unavailable (%s); proceeding without narration audio",
                    tts_result.error,
                )
                self._write_stage_progress(
                    project_id,
                    "voice",
                    "skipped",
                    {
                        "tts_provider": str(tts_provider or ""),
                        "tts_speed": float(tts_speed or 1.0),
                        "script_hash": self._script_hash(script),
                    },
                    error=tts_result.error,
                )
        else:
            self._write_stage_progress(
                project_id,
                "voice",
                "skipped",
                {
                    "tts_provider": str(tts_provider or ""),
                    "tts_speed": float(tts_speed or 1.0),
                    "script_hash": self._script_hash(script),
                },
            )

        self._guard_short_narration_duration(
            project_id=project_id,
            tts_path=tts_path,
            output_format=output_format,
            planned_seconds=planned_seconds,
        )

        #
        # ⚠️ 并行模式下这一段**早在 TTS 之前就已经在后台跑了**（见上面
        #    `_assets_future`），这里只是把结果取回来。异常也会在 `.result()`
        #    上原样重新抛出，所以下面那一整套错误处理照常生效。
        db = SessionLocal()
        try:
            if _assets_future is not None:
                try:
                    footage_dicts = _assets_future.result()
                finally:
                    _close_assets_pool()
            else:
                footage_dicts = self._run_assets_selection(
                    project_id, script, assets_dir, orientation, output_format,
                    tts_path=tts_path, db=db,
                )
        # to the user with structured error codes. PipelineService raises
        # PipelineHardStopError with .to_dict(); the API layer translates
        # to HTTP 422 + frontend-readable JSON; the Studio render UI maps
        # error_code to a Chinese message + "edit script" button.
        except Exception as e:
            from backend.lib.plan_validation import (
                PlanChunksError, UnmatchedChunkError,
            )
            # orchestrator (refresh rejected / cloud not authenticated) can get
            # wrapped as an UnmatchedChunkError ("no acceptable stock asset")
            # because every LLM-scored candidate failed for lack of auth. If we
            # treated that as a hard "your script is unfilmable" failure, the
            # whole 10-min video would be discarded instead of resuming once the
            # token recovers. So FIRST walk the exception chain for an auth/cloud
            # root cause and re-raise it as the proper resumable error; the
            # background worker then marks the project retry/auth-paused and the
            # next run continues from the assets checkpoint (already-downloaded
            # footage is reused).
            auth_cause = _find_auth_cause(e)
            if auth_cause is not None:
                logger.warning(
                    "assets stage failed due to auth/cloud root cause "
                    "(surfaced as %s) — treating as resumable: %s",
                    type(e).__name__, auth_cause,
                )
                raise auth_cause
            if isinstance(e, (PlanChunksError, UnmatchedChunkError)):
                err_dict = e.to_dict()
                logger.warning(
                    "pipeline_hard_stop stage=assets error_code=%s detail=%r",
                    err_dict.get("error_code"), err_dict,
                )
                write_progress(
                    self.PIPELINE_DIR, project_id, "assets", "failed",
                    {
                        "asset_manifest": {
                            "tts_path": str(tts_path) if tts_path else None,
                            "tts_provider": str(tts_provider or ""),
                            "tts_speed": float(tts_speed or 1.0),
                            "script_hash": self._script_hash(script),
                            "footage_results": [],
                        },
                        "error": str(e),
                        "error_code": err_dict.get("error_code"),
                        "error_detail": err_dict,
                    },
                )
                self._write_stage_progress(
                    project_id,
                    "assets",
                    "failed",
                    {
                        "tts_path": str(tts_path) if tts_path else None,
                        "error_code": err_dict.get("error_code"),
                        "error_detail": err_dict,
                    },
                    error=str(e),
                )
            raise
        finally:
            db.close()

        # is empty. Previously the stage was marked "completed" with empty
        # results, then compose blew up with no clips and the user saw the
        # cryptic "流水线失败" message. Mark the stage failed loudly so the UI
        # can surface a useful error instead of letting compose throw.
        if not footage_dicts:
            error_msg = (
                "All chunks failed: 0 stock matches and 0 AI generations. "
                "Likely causes: cloud rate limit (aiGateway tier), no Pexels "
                "gateway yet, or the video provider key was revoked. "
                "Check the video provider response for 429/4xx."
            )
            logger.error(error_msg)
            self._write_stage_progress(
                project_id,
                "assets",
                "failed",
                {
                    "tts_path": str(tts_path) if tts_path else None,
                    "footage_results": [],
                },
                error=error_msg,
            )
            write_progress(
                self.PIPELINE_DIR,
                project_id,
                "assets",
                "failed",
                {
                    "asset_manifest": {
                        "tts_path": str(tts_path) if tts_path else None,
                        "tts_provider": str(tts_provider or ""),
                        "tts_speed": float(tts_speed or 1.0),
                        "script_hash": self._script_hash(script),
                        "footage_results": [],
                    },
                    "error": error_msg,
                },
            )
            raise RuntimeError(error_msg)

        write_progress(
            self.PIPELINE_DIR,
            project_id,
            "assets",
            "completed",
            {
                "asset_manifest": {
                        "tts_path": str(tts_path) if tts_path else None,
                        "tts_provider": str(tts_provider or ""),
                        "tts_speed": float(tts_speed or 1.0),
                        "script_hash": self._script_hash(script),
                        "align_words": bool(align_words),
                        "footage_results": footage_dicts,
                    }
                },
        )
        self._write_stage_progress(
            project_id,
            "assets",
            "completed",
            {
                "asset_manifest": {
                        "tts_path": str(tts_path) if tts_path else None,
                        "tts_provider": str(tts_provider or ""),
                        "tts_speed": float(tts_speed or 1.0),
                        "script_hash": self._script_hash(script),
                        "align_words": bool(align_words),
                        "footage_results": footage_dicts,
                    }
                },
        )
        return tts_path, footage_dicts

    def _run_assets_selection(
        self,
        project_id: str,
        script: str,
        assets_dir: Path,
        orientation: str,
        output_format: str,
        *,
        tts_path: Optional[Path] = None,
        db=None,
    ):
        """取材 + AI 打分（原 `_stage_assets` 里 Stage B 那一段，原样搬出来）。

        📌 抽成独立函数只为一件事：**能被丢进线程里，和 TTS 同时跑**。
           逻辑一行没改 —— 判断改动是否安全时，对照 git diff 看这一点。

        ⚠️ `db` 参数：串行调用时复用外面开好的 session；并行时传 None，
           在**本线程内**自己开一个。SQLAlchemy 的 session 不是线程安全的，
           跨线程共用一个必炸。
        """
        _own_db = db is None
        if _own_db:
            db = SessionLocal()
        try:
            self._write_stage_progress(
                project_id,
                "assets",
                "running",
                {"tts_path": str(tts_path) if tts_path else None},
            )
            self._raise_if_stopped(project_id)
            logger.info("Using Phase 2.6 Orchestrator (mode=%s)", self.mode.value)
            return self._stage_assets_orchestrator(
                db, project_id, script, assets_dir, orientation,
                output_format=output_format,
            )
        finally:
            if _own_db:
                try:
                    db.close()
                except Exception:  # noqa: BLE001 — 关不掉不该盖住真正的异常
                    pass

    def _guard_short_narration_duration(
        self,
        *,
        project_id: str,
        tts_path: Optional[Path],
        output_format: str,
        planned_seconds: int | float | None,
    ) -> None:
        min_seconds = _minimum_short_narration_seconds(output_format, planned_seconds)
        if min_seconds <= 0 or not tts_path:
            return
        duration = self._probe_audio_duration(tts_path)
        if duration >= min_seconds:
            return
        error_msg = (
            "short_narration_too_short: "
            f"duration={duration:.2f}s min={min_seconds:.2f}s "
            f"format={_normalize_output_format(output_format)}"
        )
        logger.error("project=%s %s", project_id, error_msg)
        self._write_stage_progress(
            project_id,
            "voice",
            "failed",
            {
                "tts_path": str(tts_path),
                "duration_seconds": round(duration, 2),
                "minimum_duration_seconds": round(min_seconds, 2),
            },
            error=error_msg,
        )
        raise RuntimeError(error_msg)

    def _stage_assets_orchestrator(
        self, db, project_id, script, assets_dir, orientation,
        output_format="youtube_landscape",
    ) -> list[dict]:
        """Phase 2.6 path. Returns flat footage_dicts."""
        # before they enter every chunk-indexed stage. Cached on self so
        # all stages of this run share the same list.
        sentences = self._expand_sentences_for_visual(script)
        # Long video (youtube_landscape) runs on the ISOLATED long-video
        # orchestrator; everything else (shorts/tiktok/reels/feed) stays on the
        # untouched short-video Orchestrator. Same run() signature + outcome shape.
        is_long = output_format == "youtube_landscape"
        orch = self.long_orchestrator if is_long else self.orchestrator
        logger.info(
            "asset orchestrator routed to %s (output_format=%s)",
            type(orch).__name__, output_format,
        )
        outcomes = orch.run(
            db=db, project_id=project_id, sentences=sentences,
            output_dir=assets_dir / "footage", orientation=orientation,
        )

        # Phase 2.7a — fold this run's outcomes into Series.learned_patterns
        # so the next pipeline run on the same Series gets smarter prompts.
        if self.series is not None and getattr(self.series, "id", None):
            try:
                from backend.services.memory_service import MemoryService
                MemoryService().record_outcomes(db, self.series.id, outcomes)
            except Exception as e:
                logger.warning("MemoryService.record_outcomes failed: %s", e)

        # Do not auto-ingest production stock into the local visual library.
        # Clips should remain project-scoped assets unless we explicitly turn
        # the old library pipeline back on for internal debugging.
        if self.library is not None:
            try:
                new_count = self.library.ingest_outcomes(outcomes)
                if new_count:
                    logger.info("library: ingested %d new clip(s) from this run", new_count)
                    from backend.workers.background_worker import get_background_worker
                    get_background_worker().notify_new_work()
            except Exception as e:
                logger.warning("library auto-ingest failed: %s", e)

        # Phase 2.7c — persist project.cost_usd so BatchRun cost cap accounting
        # has real numbers to sum.
        try:
            from backend.models.project import Project
            total_cost = float(sum((oc.cost_usd or 0.0) for oc in outcomes))
            project = db.query(Project).filter(Project.id == project_id).first()
            if project is not None:
                project.cost_usd = (project.cost_usd or 0.0) + total_cost
                db.commit()
        except Exception as e:
            logger.warning("project.cost_usd update failed: %s", e)

        footage_dicts = []
        for oc in outcomes:
            if not oc.local_path:
                logger.warning(
                    "Chunk %d: no clip selected (decision=%s, error=%s)",
                    oc.chunk_index, oc.decision, oc.error,
                )
                continue
            footage_dicts.append({
                "scene_index": oc.chunk_index,
                "shot_index": 0,
                "query": oc.plan.get("search_query", ""),
                "local_path": oc.local_path,
                "duration": oc.duration,
                "source": oc.source,
                "source_id": oc.source_id,
                "source_url": oc.source_url,
                "resolution": oc.resolution,
                "mm_critic_score": oc.score,
                "decision": oc.decision,
                "cost_usd": oc.cost_usd,
                "source_tags": oc.source_tags,
                "subject_h_pos": getattr(oc, "subject_h_pos", None),
            })
        return footage_dicts


    @staticmethod
    def _ai_bgm_director_enabled() -> bool:
        """P2-14 — is the AI BGM director opt-in feature turned on?

        Env-gated, default OFF. When off, the music stage behaves exactly as
        before (segment selection sees only sentences+durations; mixing uses the
        proven static-weight graph). Only when this is on do we (a) feed content
        tonality to the composer and (b) use voice-first sidechain ducking.
        """
        return os.environ.get(
            "MEDIA_BUDDY_AI_BGM_DIRECTOR", "0"
        ).strip().lower() in ("1", "true", "yes", "on")

    def _build_music_content_context(self, project_title: str = "") -> Optional[dict]:
        """P2-14 — assemble the content tonality signals for the AI composer
        from the active Series + project title. Returns None when nothing useful
        is available (composer then falls back to script-only reasoning)."""
        ctx: dict = {}
        if project_title:
            ctx["title"] = project_title
        s = self.series
        if s is not None:
            for key, attr in (
                ("series_name", "name"),
                ("description", "description"),
                ("industry", "industry_tag"),
                ("mood_lock", "bgm_mood_lock"),
            ):
                val = getattr(s, attr, None)
                if val:
                    ctx[key] = str(val)
        return ctx or None

    def _stage_music(
        self, project_id, project_dir, script, tts_path, include_music=False,
        project_title: str = "",
    ) -> Optional[Path]:
        """Stage 2.5: pick a background music track aligned to the script's
        narrative arc. Returns the bgm_track path or None when disabled / no
        music could be found.

        Skipped when the per-project ``include_music`` toggle is off (default —
        avoids YouTube copyright/Content-ID claims) or BGM_ENABLED=false (env).
        Otherwise:
          1. LLM identifies 1-N narrative segments
          2. Map chunk_index ranges → seconds (proportional to char count of TTS)
          3. Per segment: search Pixabay (then Freesound) for a fitting clip
          4. Concat segment clips with crossfade → bgm_track.mp3
        """
        # Per-project switch (checked BEFORE any checkpoint resume, so toggling
        # music off on a re-render never reuses an earlier run's BGM track).
        if not include_music:
            logger.info("background music disabled for project %s (include_music=False); skipping music stage", project_id)
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "music_disabled"})
            return None

        existing = read_progress(self.PIPELINE_DIR, project_id, "music")
        if existing and existing.get("status") == "completed":
            p = existing.get("artifacts", {}).get("bgm_track", {}).get("path")
            if not p or self._artifact_path_exists(p):
                logger.info("resuming from completed music checkpoint for project %s", project_id)
                self._write_stage_progress(project_id, "music", "completed", existing.get("artifacts", {}))
                return Path(p) if p else None
            logger.info("music checkpoint ignored; bgm file missing: %s", p)
        if existing and existing.get("status") == "skipped":
            logger.info("resuming from skipped music checkpoint for project %s", project_id)
            self._write_stage_progress(project_id, "music", "skipped", existing.get("artifacts", {}), error=existing.get("error"))
            return None

        if os.environ.get("BGM_ENABLED", "true").lower() in ("0", "false", "no"):
            logger.info("BGM disabled (BGM_ENABLED=false); skipping music stage")
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "bgm_disabled"})
            return None

        self._raise_if_stopped(project_id)
        self._write_stage_progress(project_id, "music", "running")
        # Music arc analyses full sentence text + matches chunk durations
        # against TTS audio — must use the display list to stay in sync
        # with what TTS actually speaks (visual list has clause truncations).
        sentences = self._expand_sentences_for_display(script)
        if not sentences:
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "no_sentences"})
            return None

        # Per-chunk durations: prefer ffprobe of the TTS file pro-rated by char
        # count; fall back to a Chinese-friendly speaking rate of ~3 chars/sec.
        durations = self._estimate_chunk_durations(sentences, tts_path)
        total_dur = sum(durations)
        if total_dur < 5.0:
            logger.info("Total narration < 5s; skipping music stage")
            self._write_stage_progress(
                project_id,
                "music",
                "skipped",
                {"reason": "short_narration", "duration_seconds": round(total_dur, 2)},
            )
            return None

        # P2-14 — when the AI BGM director is enabled, feed content tonality
        # (series theme / industry / mood-lock / title) so the composer matches
        # music to the content instead of collapsing every video to calm ambient.
        # Default OFF → content_context=None → identical prompt & behaviour.
        content_context = None
        if self._ai_bgm_director_enabled():
            content_context = self._build_music_content_context(project_title)
            logger.info(
                "AI BGM director ON — music context: %s",
                content_context or "(none available)",
            )
        try:
            raw_segments = self.llm.analyze_music_arc(
                sentences, durations, content_context=content_context,
            )
        except Exception as e:
            logger.warning("analyze_music_arc failed: %s — skipping music", e)
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "music_arc_failed"}, error=str(e))
            return None

        segments = map_segments_to_seconds(raw_segments, durations)
        if not segments:
            logger.info("No music segments produced; skipping")
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "no_music_segments"})
            return None
        logger.info(
            "Music arc: %d segment(s) for %.1fs total: %s",
            len(segments), total_dur,
            [(s.narrative_role, s.mood, round(s.duration, 1)) for s in segments],
        )

        music_dir = project_dir / "assets" / "music"
        music_dir.mkdir(parents=True, exist_ok=True)
        results: list[Optional["MusicResult"]] = []
        for i, seg in enumerate(segments):
            self._raise_if_stopped(project_id)
            r = self.music.find_for_segment(seg, music_dir, i)
            if r:
                logger.info(
                    "Segment %d (%s/%s) → %s [%s] %.1fs",
                    i, seg.mood, seg.energy, r.title, r.source, r.duration,
                )
            else:
                logger.warning("Segment %d returned no music — will fill via fallback", i)
            results.append(r)

        # media-buddy-rules.md §13 (Music Completeness): NEVER skip a
        # segment silently. The old behavior was "0 of 3 found → return
        # None → no BGM at all" OR "1 of 3 found → BGM ends at 22s while
        # video runs 67.8s". Fill any missing segment by looping/trimming
        # a successful neighbour to the missing segment's duration.
        results = self._fill_music_gaps(results, segments, music_dir)

        # results may still be all-None if literally nothing was found
        # (Pixabay key + Freesound key both missing AND no library) —
        # in that case skip BGM entirely; the rule 13 "兜底默认曲" is
        # not yet bundled, that's a separate todo.
        non_null = [r for r in results if r is not None]
        if not non_null:
            logger.info("All segments failed music lookup AND no fallback source; skipping bgm")
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "no_music_found"})
            return None
        results = non_null  # build_bgm_track filters None too but be explicit

        bgm_path = self.music.build_bgm_track(
            results, music_dir / "bgm_track.mp3", crossfade_seconds=1.5,
        )
        if not bgm_path:
            logger.warning("bgm concat failed; skipping bgm")
            self._write_stage_progress(project_id, "music", "skipped", {"reason": "bgm_concat_failed"})
            return None

        write_progress(
            self.PIPELINE_DIR, project_id, "music", "completed",
            {
                "bgm_track": {
                    "path": str(bgm_path),
                    "segments": [
                        {
                            "start_chunk_index": s.start_chunk_index,
                            "end_chunk_index": s.end_chunk_index,
                            "start_seconds": round(s.start_seconds, 2),
                            "end_seconds": round(s.end_seconds, 2),
                            "narrative_role": s.narrative_role,
                            "mood": s.mood,
                            "energy": s.energy,
                            "search_keywords": s.search_keywords,
                            "music": {
                                "title": r.title,
                                "source": r.source,
                                "license": r.license,
                                "source_url": r.source_url,
                            } if r else None,
                        }
                        for s, r in zip(segments, results + [None] * (len(segments) - len(results)))
                    ],
                }
            },
        )
        self._write_stage_progress(
            project_id,
            "music",
            "completed",
            {
                "bgm_track": {
                    "path": str(bgm_path),
                    "segment_count": len(segments),
                }
            },
        )
        return bgm_path

    def _fill_music_gaps(
        self,
        results: list,
        segments: list,
        music_dir: Path,
    ) -> list:
        """For each None entry in `results`, copy + loop/trim a successful
        neighbour's audio file to the missing segment's target duration.

        media-buddy-rules.md §13: "如果还是失败 → 用同一首曲子循环
        (截取到所需时长)". Prefer the nearest preceding successful
        segment (so transitions stay close in feel); if none exists,
        use the nearest following one.

        Returns a new list with the same length as `segments`, with None
        entries replaced by cloned MusicResult objects whose local_path
        points to a freshly generated looped audio file. Entries already
        non-None are passed through.
        """
        if all(r is not None for r in results):
            return results

        from dataclasses import replace as _dc_replace
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            logger.warning(
                "music gap fill: ffmpeg missing on PATH — leaving %d gap(s) "
                "unfilled (rule 13 may be violated)",
                sum(1 for r in results if r is None),
            )
            return results

        def _nearest_donor(i: int):
            # Search outward — prefer left (preceding), then right.
            for radius in range(1, max(len(results), 1) + 1):
                lo = i - radius
                if 0 <= lo and results[lo] is not None:
                    return lo, results[lo]
                hi = i + radius
                if hi < len(results) and results[hi] is not None:
                    return hi, results[hi]
            return None, None

        filled = list(results)
        for i, r in enumerate(filled):
            if r is not None:
                continue
            donor_idx, donor = _nearest_donor(i)
            if donor is None:
                continue  # entire run failed — handled by caller
            target_dur = float(segments[i].duration)
            donor_path = Path(donor.local_path)
            if not donor_path.exists():
                logger.warning(
                    "music gap fill seg %d: donor seg %d path missing %s",
                    i, donor_idx, donor_path,
                )
                continue
            out_path = music_dir / f"seg_{i:03d}_loopfill.mp3"
            # -stream_loop -1 loops indefinitely; -t cuts to exact duration.
            cmd = [
                ffmpeg, "-y", "-stream_loop", "-1",
                "-i", str(donor_path),
                "-t", f"{target_dur:.3f}",
                "-c:a", "libmp3lame", "-b:a", "192k",
                str(out_path),
            ]
            try:
                r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r2.returncode != 0 or not out_path.exists():
                    logger.warning(
                        "music gap fill seg %d: ffmpeg loop failed (exit=%d): %s",
                        i, r2.returncode, (r2.stderr or "")[-300:],
                    )
                    continue
            except Exception as e:
                logger.warning("music gap fill seg %d exception: %s", i, e)
                continue
            # Clone the donor's MusicResult metadata but swap path + duration
            filled_result = _dc_replace(
                donor,
                local_path=str(out_path),
                duration=target_dur,
                title=f"{donor.title} [looped for seg {i}]",
            )
            filled[i] = filled_result
            logger.info(
                "music gap fill seg %d (%.1fs) ← donor seg %d (%s)",
                i, target_dur, donor_idx, donor.title,
            )
        return filled

    def _audit_compose_output(
        self,
        output_path: Path,
        tts_path: Optional[Path],
        footage_results: list,
    ) -> None:
        """Post-compose audit (media-buddy-rules.md §17). Minimal checks:

        1. Output file exists and has non-zero size.
        2. Video duration ≥ narration duration (rule 11 violation check).
        3. No duplicate source_id across footage_results (rule 12 check;
           the orchestrator should have already deduped, but cheap to
           re-verify here).

        Logs warnings rather than raising so a slightly-short video still
        ships — but the warnings will appear in pipeline logs so QA can
        triage. We may upgrade to a hard failure once the loop+pad path
        has been validated in real runs.
        """
        if not output_path.exists():
            logger.error("audit: output file missing: %s", output_path)
            return
        size = output_path.stat().st_size
        if size == 0:
            logger.error("audit: output file is empty: %s", output_path)
            return

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            logger.warning("audit: ffprobe missing — skipping duration check")
        else:
            try:
                r = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries",
                     "format=duration", "-of",
                     "default=noprint_wrappers=1:nokey=1", str(output_path)],
                    capture_output=True, text=True, timeout=20,
                )
                video_dur = float((r.stdout or "0").strip() or 0.0)
            except Exception as e:
                logger.warning("audit: ffprobe failed: %s", e)
                video_dur = 0.0

            narration_dur = (
                self._probe_audio_duration(tts_path) if tts_path else 0.0
            )
            if narration_dur > 0 and video_dur > 0:
                if video_dur + 0.5 < narration_dur:
                    logger.error(
                        "audit RULE 11 VIOLATION: video=%.2fs < narration=%.2fs "
                        "(diff %.2fs) — the tail of the narration was cut. "
                        "Check _mux_audio (should have padded) and per-cut "
                        "loop logic in _stage_compose.",
                        video_dur, narration_dur,
                        narration_dur - video_dur,
                    )
                else:
                    logger.info(
                        "audit ok: video=%.2fs covers narration=%.2fs",
                        video_dur, narration_dur,
                    )

        # Rule 12 — sanity check (orchestrator dedup already runs)
        seen: dict[str, int] = {}
        for i, r in enumerate(footage_results):
            sid = (
                getattr(r, "source_id", "") if not isinstance(r, dict)
                else r.get("source_id", "")
            )
            src = (
                getattr(r, "source", "") if not isinstance(r, dict)
                else r.get("source", "")
            )
            if not sid:
                continue
            key = f"{src}:{sid}"
            if key in seen:
                logger.warning(
                    "audit RULE 12 VIOLATION: source_id %s used by chunks "
                    "%d and %d — orchestrator dedup pass missed this",
                    key, seen[key], i,
                )
            else:
                seen[key] = i

    @staticmethod
    def _short_v2_max_asset_occurrences() -> int:
        try:
            return max(
                1,
                int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_ASSET_OCCURRENCES", "2")),
            )
        except ValueError:
            return 2

    @staticmethod
    def _short_v2_max_total_repeat_occurrences() -> int:
        try:
            return max(
                0,
                int(os.environ.get("MEDIA_BUDDY_SHORT_V2_MAX_TOTAL_REPEAT_OCCURRENCES", "1")),
            )
        except ValueError:
            return 1

    @staticmethod
    def _compose_asset_key(clip: object) -> str:
        if isinstance(clip, dict):
            source = str(clip.get("source") or "")
            source_id = str(clip.get("source_id") or "")
            local_path = str(clip.get("local_path") or clip.get("source") or "")
        else:
            source = str(getattr(clip, "source", "") or "")
            source_id = str(getattr(clip, "source_id", "") or "")
            local_path = str(getattr(clip, "local_path", "") or "")
        if source and source_id:
            return f"{source}:{source_id}"
        return local_path

    @classmethod
    def _compose_asset_counts(cls, clips: list | tuple | object) -> dict[str, int]:
        counts: dict[str, int] = {}
        for clip in clips:
            key = cls._compose_asset_key(clip)
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _compose_duplicate_reuse_count(counts: dict[str, int]) -> int:
        return sum(max(0, int(count) - 1) for count in counts.values())

    @classmethod
    def _compose_reuse_allowed(cls, counts: dict[str, int], asset_key: str) -> bool:
        if not asset_key:
            return True
        current = int(counts.get(asset_key, 0) or 0)
        if current <= 0:
            return True
        if current >= cls._short_v2_max_asset_occurrences():
            return False
        return (
            cls._compose_duplicate_reuse_count(counts)
            < cls._short_v2_max_total_repeat_occurrences()
        )

    @staticmethod
    def _compose_record_asset_use(counts: dict[str, int], asset_key: str) -> None:
        if asset_key:
            counts[asset_key] = int(counts.get(asset_key, 0) or 0) + 1

    @staticmethod
    def _estimate_chunk_durations(
        sentences: list[str], tts_path: Optional[Path],
    ) -> list[float]:
        """Per-chunk seconds. If TTS exists, distribute its measured duration
        across chunks proportional to character count. Otherwise estimate at
        ~3 chars/sec (Chinese narration average)."""
        char_counts = [max(1, len(s)) for s in sentences]
        total_chars = sum(char_counts)
        if tts_path and Path(tts_path).exists():
            total_dur = PipelineService._probe_audio_duration(tts_path)
            if total_dur > 0:
                return [
                    total_dur * c / total_chars for c in char_counts
                ]
        # Fallback: 3 chars/sec is a reasonable baseline for both EN and ZH
        return [c / 3.0 for c in char_counts]

    def _resolve_final_audio(
        self,
        tts_path: Optional[Path],
        bgm_path: Optional[Path],
        assets_dir: Path,
        project_id: Optional[str] = None,
    ) -> Optional[Path]:
        """Decide what audio file VideoCompose receives:
          - both: mix narration + bgm (分轨响度归一) → mixed.wav
          - tts only: tts_path
          - bgm only (no narration): bgm_path
          - neither: None

        混音时会分别测量人声/音乐两轨的电平并写进诊断(audio_mix.progress.json),
        让"做视频时就能检测两轨电平"落地、把"配乐忽大忽小"变成可观测。"""
        if tts_path and bgm_path:
            mixed = assets_dir / "mixed_audio.wav"
            levels = self.audio_mix.mix_narration_and_music(
                narration_path=tts_path,
                music_path=bgm_path,
                output_path=mixed,
            )
            if levels and mixed.exists():
                logger.info("Mixed narration + bgm → %s", mixed)
                if project_id:
                    try:
                        self._write_stage_progress(
                            project_id, "audio_mix", "completed", levels,
                        )
                    except Exception:  # noqa: BLE001 — 诊断落盘失败不许影响出片
                        logger.warning("persist audio_mix levels failed", exc_info=True)
                return mixed
            logger.warning("Mix failed; falling back to narration-only audio")
            return tts_path
        return tts_path or bgm_path

    @staticmethod
    def _merge_short_windows(
        visual_windows: list[tuple[float, float]],
        min_shot: float,
        max_shots: Optional[int] = None,
        protected_scene_indices: Optional[set[int]] = None,
    ) -> list[tuple[int, float, float]]:
        """Coalesce on-screen windows shorter than ``min_shot`` into the
        PREVIOUS window, keeping that window's owning scene (its clip).

        Returns ``[(clip_scene_index, start, end), ...]``. The merged window
        extends to the short window's end, so total coverage is unchanged and
        the video still covers the narration. ``min_shot <= 0`` is a 1:1
        passthrough (no merge — original per-scene behavior). A short FIRST
        window has no previous to merge into, so it is kept as-is.
        """
        protected_scene_indices = protected_scene_indices or set()
        out: list[tuple[int, float, float]] = []
        for si, (ws, we) in enumerate(visual_windows):
            if (
                min_shot > 0.0
                and out
                and si not in protected_scene_indices
                and out[-1][0] not in protected_scene_indices
                and (float(we) - float(ws)) < min_shot
            ):
                prev = out[-1]
                out[-1] = (prev[0], prev[1], float(we))
            else:
                out.append((si, float(ws), float(we)))
        if (
            max_shots and max_shots > 0
            and len(out) > max_shots
            and not protected_scene_indices
        ):
            source = out
            compressed: list[tuple[int, float, float]] = []
            total = len(source)
            # Distribute merges across the timeline instead of repeatedly
            # extending the first short window. This keeps shorts close to the
            # requested visual rhythm, e.g. 60s / 3s ~= 20 visual shots.
            for group_idx in range(max_shots):
                start_idx = int(math.floor(group_idx * total / max_shots))
                end_excl = int(math.floor((group_idx + 1) * total / max_shots))
                if end_excl <= start_idx:
                    end_excl = start_idx + 1
                if group_idx == max_shots - 1:
                    end_excl = total
                first = source[start_idx]
                last = source[end_excl - 1]
                compressed.append((first[0], first[1], last[2]))
            out = compressed
        return out

    @staticmethod
    def _fine_cut_scene_merge_protected(
        plan: dict | None, sentence: str = "", clip: dict | None = None,
    ) -> bool:
        """Hard visual anchors get their own shot window.

        The sentence-level visual planner uses ``must_show`` for objects/actions
        that cannot be borrowed from neighbouring narration. Fine-cut merging is
        allowed for soft/thematic fallback sentences, but a hard anchored scene
        must not be swallowed by the previous shot; otherwise a money/pricing
        clip can visually cover a later kettle/syrup subtitle.
        """
        plan = plan or {}
        must_show = plan.get("must_show") or []
        if bool(must_show) and not bool(plan.get("story_soft_fallback")):
            return True
        text = " ".join([
            str(sentence or ""),
            " ".join(str(x) for x in (plan.get("queries") or [])),
            str((clip or {}).get("query") or ""),
            str((clip or {}).get("source_tags") or ""),
        ]).lower()
        return any(term in text for term in (
            "cash", "money", "rupee", "rupees", "currency", "banknote",
            "cost", "price", "pricing", "profit", "revenue",
            "kettle", "pot", "syrup", "honey", "jaggery",
            "customer", "queue", "line", "waiting", "buying",
            "现金", "收钱", "数钱", "卢比", "成本", "定价", "利润", "收入",
            "铜壶", "不锈钢壶", "壶", "糖浆", "浓度", "椰枣蜜", "蜂蜜",
            "顾客", "排队", "复购", "购买",
        ))

    def _stage_compose(
        self, project_id, project_dir, script, tts_path, bgm_path, footage_results,
        output_format, include_subtitles, project_title: str = "",
        framing_style: str = "fill",
        output_language: str = "zh", subtitle_language: Optional[str] = None,
        subtitle_font_scale: float = 1.0,
    ) -> Path:
        output_format = _normalize_output_format(output_format)
        self._raise_if_stopped(project_id)
        existing = read_progress(self.PIPELINE_DIR, project_id, "compose")
        if existing and existing.get("status") == "completed":
            artifacts = existing.get("artifacts", {})
            output_path = Path(existing["artifacts"]["render_report"]["output_path"])
            if not output_path.exists():
                logger.info(
                    "compose checkpoint ignored; output file missing: %s",
                    output_path,
                )
            else:
                logger.info("resuming from completed compose checkpoint for project %s", project_id)
                self._write_stage_progress(project_id, "rough_cut", "completed", artifacts)
                self._write_stage_progress(project_id, "fine_cut", "completed", artifacts)
                self._write_stage_progress(
                    project_id,
                    "subtitles",
                    "completed" if include_subtitles else "skipped",
                    artifacts if include_subtitles else {"reason": "subtitles_disabled"},
                )
                self._write_stage_progress(project_id, "seo", "completed", artifacts)
                self._write_stage_progress(project_id, "export", "completed", artifacts)
                self._cleanup_intermediate_assets_after_export(project_dir, output_path)
                return output_path
        if existing and existing.get("status") == "completed":
            # Completed progress with a missing output is stale. Fall through
            # and rebuild only compose/export from existing upstream stages.
            pass
        elif existing and existing.get("status") == "failed":
            logger.info(
                "resuming project %s from failed compose checkpoint; "
                "upstream script/assets/music checkpoints will be reused",
                project_id,
            )

        output_dir = project_dir / "output"
        output_dir.mkdir(exist_ok=True)
        self._write_stage_progress(
            project_id,
            "seo",
            "running",
            {"profile": output_format, "project_title": project_title},
        )
        output_path = output_dir / _seo_video_filename(
            project_title, output_format, script=script,
        )
        self._write_stage_progress(
            project_id,
            "seo",
            "completed",
            {"profile": output_format, "output_filename": output_path.name},
        )

        # Phase 2.11j — TTS-timecode-driven cuts.
        # Build one cut per CHUNK (matching narrated sentences), with the
        # cut's duration set to the TTS sentence's spoken duration. When
        # a chunk has no clip (orchestrator dropped it), borrow from the
        # nearest neighbour with a clip (or the highest-quality fallback).
        # Result: visual chunk N is on screen exactly while the narrator
        # speaks sentence N — no more visual/audio drift.

        self._write_stage_progress(project_id, "rough_cut", "running")

        # 1. Index footage_results by scene_index (chunk index)
        clip_by_scene: dict[int, dict] = {}
        for i, r in enumerate(footage_results):
            self._raise_if_stopped(project_id)
            local = getattr(r, "local_path", None) if not isinstance(r, dict) else r.get("local_path")
            if not local:
                continue
            decision = getattr(r, "decision", "") if not isinstance(r, dict) else r.get("decision", "")
            if decision == "reject":
                logger.warning(
                    "compose skipping rejected footage for scene %d: %s",
                    i, local,
                )
                continue
            duration = getattr(r, "duration", 5.0) if not isinstance(r, dict) else r.get("duration", 5.0)
            scene_idx = (
                getattr(r, "scene_index", i) if not isinstance(r, dict)
                else r.get("scene_index", i)
            )
            plan = getattr(r, "plan", {}) if not isinstance(r, dict) else r.get("plan", {})
            source = getattr(r, "source", "") if not isinstance(r, dict) else r.get("source", "")
            source_id = getattr(r, "source_id", "") if not isinstance(r, dict) else r.get("source_id", "")
            source_tags = (
                getattr(r, "source_tags", "") if not isinstance(r, dict)
                else r.get("source_tags", "")
            )
            query = (
                (plan or {}).get("search_query", "")
                or (getattr(r, "query", "") if not isinstance(r, dict) else r.get("query", ""))
            )
            subject_h_pos = (
                getattr(r, "subject_h_pos", None) if not isinstance(r, dict)
                else r.get("subject_h_pos")
            )
            clip_by_scene[int(scene_idx)] = {
                "local_path": local,
                "duration": float(duration or 5.0),
                "query": query,
                "source": source or "",
                "source_id": source_id or "",
                "source_tags": source_tags or "",
                "plan": plan or {},
                "subject_h_pos": subject_h_pos,
            }

        if not clip_by_scene:
            raise RuntimeError("No valid footage clips downloaded")

        # 2. Read sentence boundaries from TTS sidecar
        sidecar = (Path(tts_path).with_suffix(".words.json")
                    if tts_path else None)
        # Use display list — TTS char alignment walks sentence chars
        # against TTS char tokens; the visual list's clause-truncated
        # entries would fall out of sync with the audio.
        sentences = self._expand_sentences_for_display(script)
        sentence_windows = None
        if sidecar and sidecar.exists():
            sentence_windows = self._align_sentences_to_words(sentences, sidecar)
        if sentence_windows is None:
            # No sidecar: fall back to even distribution (legacy path)
            audio_dur = self._probe_audio_duration(tts_path) if tts_path else 0.0
            n = len(sentences) or len(clip_by_scene)
            slot = (audio_dur / n) if n else 5.0
            sentence_windows = [(i * slot, (i + 1) * slot) for i in range(n)]
            logger.info(
                "no TTS sidecar — falling back to even %d×%.2fs slots", n, slot,
            )
        else:
            logger.info(
                "TTS sidecar drives cut timing: %d sentence windows",
                len(sentence_windows),
            )
        self._write_stage_progress(
            project_id,
            "rough_cut",
            "completed",
            {
                "scene_count": len(sentence_windows),
                "clip_count": len(clip_by_scene),
                "profile": output_format,
            },
        )

        # 3. For each sentence window, choose a clip — own first, then
        #    nearest neighbour, then any clip in the project.
        def _pick_clip_for(scene_idx: int) -> dict:
            if scene_idx in clip_by_scene:
                return clip_by_scene[scene_idx]
            # nearest neighbour
            for radius in range(1, max(len(sentences), 1) + 1):
                for cand in (scene_idx - radius, scene_idx + radius):
                    if cand in clip_by_scene:
                        logger.info(
                            "chunk %d had no clip — borrowing from chunk %d",
                            scene_idx, cand,
                        )
                        return clip_by_scene[cand]
            # last resort: any clip
            return next(iter(clip_by_scene.values()))

        portrait_outputs = ("youtube_shorts", "tiktok", "instagram_reels")
        is_portrait = output_format in portrait_outputs
        fill_orientation = (
            "portrait" if is_portrait
            else ("square" if output_format == "instagram_feed" else "landscape")
        )
        used_stock_ids: set[str] = {
            f"{c.get('source')}:{c.get('source_id')}"
            for c in clip_by_scene.values()
            if c.get("source") and c.get("source_id")
        }

        def _chunk_contract_for(scene_idx: int):
            try:
                return getattr(
                    self.orchestrator, "_harness_chunk_contracts", {},
                ).get(scene_idx)
            except Exception:
                return None

        def _reuse_rejection_for_scene(scene_idx: int, target_clip: dict, reuse_clip: dict) -> str:
            query = target_clip.get("query") or (
                sentences[scene_idx] if 0 <= scene_idx < len(sentences) else ""
            )
            sentence = sentences[scene_idx] if 0 <= scene_idx < len(sentences) else ""
            haystack = " ".join([
                str(reuse_clip.get("source_tags", "") or ""),
                str(reuse_clip.get("query", "") or ""),
                str(reuse_clip.get("source", "") or ""),
                str(reuse_clip.get("source_id", "") or ""),
            ])
            candidate = SimpleNamespace(
                source_tags=haystack,
                _raw=SimpleNamespace(category="", description=haystack),
            )
            return self._reject_stock_fill_candidate(
                query,
                candidate,
                str(target_clip.get("source_tags", "") or ""),
                chunk_contract=_chunk_contract_for(scene_idx),
                chunk_plan=target_clip.get("plan") or {},
                sentence=sentence,
            )

        def _stock_fill_clips(scene_idx: int, clip: dict, needed_s: float) -> list[dict]:
            """Fetch extra stock clips for long narration slots.

            This avoids replaying the same short clip and keeps AI generation
            as the last resort. Candidates are intentionally stock/library
            only; no AI generation calls are made here.
            """
            if needed_s <= 0.75:
                return []
            query = clip.get("query") or (
                sentences[scene_idx] if 0 <= scene_idx < len(sentences) else ""
            )
            if not query:
                return []
            try:
                candidates = list(self.footage.search_secondary(
                    query, fill_orientation, kind="video", limit=8,
                ))
            except Exception as e:
                logger.warning(
                    "chunk %d stock-fill search failed: %s", scene_idx, e,
                )
                return []

            fill_dir = project_dir / "assets" / "footage" / f"chunk_{scene_idx:03d}" / "fill"
            fillers: list[dict] = []
            remaining = needed_s
            # When ~3s shot-capping is on, each filler only contributes up to one
            # shot length toward coverage, so gather ENOUGH distinct clips to fill
            # a long slot with several short shots (not one long clip + freeze).
            try:
                _fill_cap = float(
                    os.environ.get(
                        "MEDIA_BUDDY_SHORTS_MAX_SHOT_SECONDS",
                        "3.5" if is_portrait else "0",
                    ) or 0
                )
            except ValueError:
                _fill_cap = 0.0
            primary_tags = str(clip.get("source_tags", "") or "")
            plan = clip.get("plan") or {}
            sentence = sentences[scene_idx] if 0 <= scene_idx < len(sentences) else ""
            # also pass the chunk's reject_if_true signals. Orchestrator built
            # this dict during run() — may be empty if cloud auth failed and the
            # is None and we fall back to the legacy banned-list + anchor-overlap
            # guard alone.
            _chunk_contract = _chunk_contract_for(scene_idx)
            defer_reframe = os.environ.get(
                "MEDIA_BUDDY_DEFER_REFRAME_TO_COMPOSE", "1"
            ).strip().lower() not in {"0", "false", "no", "off"}

            # ① 主线程先按元数据过滤(拒审/去重/fal_ai),不下载 —— 保持候选顺序。
            usable: list = []
            for cand in candidates:
                rejection = self._reject_stock_fill_candidate(
                    query, cand, primary_tags,
                    chunk_contract=_chunk_contract, chunk_plan=plan, sentence=sentence,
                )
                if rejection:
                    logger.info(
                        "chunk %d stock-fill rejected %s:%s (%s)",
                        scene_idx, cand.source, cand.source_id, rejection,
                    )
                    continue
                sid = f"{cand.source}:{cand.source_id}" if cand.source_id else ""
                if (sid and sid in used_stock_ids) or cand.source.startswith("fal_ai"):
                    continue
                usable.append(cand)

            # ② 按元数据时长估算只挑够填满这个槽的几条(时长未知按 4s 保守估),
            picked: list = []
            est = 0.0
            for cand in usable:
                picked.append(cand)
                d = float(cand.duration or 0.0)
                est += d if d > 0.5 else 4.0
                if est >= remaining:
                    break
            if not picked:
                return []

            # ③ 并行下载挑中的补片(串行下载是长视频合成慢的头号瓶颈)。landscape 本就
            #    跳过 reframe;portrait 且未 defer 时仍在下载线程里 reframe。顺序用 map 保持。
            def _dl_one(cand):
                try:
                    self._raise_if_stopped(project_id)
                    local = self.footage.download_candidate(cand, fill_dir)
                except Exception as e:
                    logger.warning(
                        "chunk %d stock-fill download failed %s:%s: %s",
                        scene_idx, cand.source, cand.source_id, e,
                    )
                    return (cand, None)
                if not local:
                    return (cand, None)
                lp = Path(local)
                if fill_orientation != "landscape" and not defer_reframe:
                    try:
                        lp = Path(self.orchestrator._reframe_if_needed(lp, fill_orientation))
                    except Exception as e:
                        logger.warning(
                            "chunk %d stock-fill reframe failed for %s: %s", scene_idx, lp, e,
                        )
                return (cand, lp)

            try:
                _fill_conc = max(1, int(os.environ.get("MEDIA_BUDDY_COMPOSE_FILL_CONCURRENCY", "4")))
            except ValueError:
                _fill_conc = 4
            if _fill_conc <= 1 or len(picked) <= 1:
                downloaded = [_dl_one(c) for c in picked]
            else:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(_fill_conc, len(picked))) as _ex:
                    downloaded = list(_ex.map(_dl_one, picked))  # 保持 picked 顺序

            # ④ 主线程按顺序消费(remaining / _fill_cap 记账与原来完全一致)。
            for cand, local_path in downloaded:
                if local_path is None:
                    continue
                sid = f"{cand.source}:{cand.source_id}" if cand.source_id else ""
                if sid and sid in used_stock_ids:
                    continue
                dur = float(cand.duration or 0.0)
                if dur <= 0:
                    dur = self._probe_audio_duration(local_path)
                dur = max(0.5, float(dur or remaining))
                fillers.append({
                    "local_path": str(local_path),
                    "duration": dur,
                    "source": cand.source,
                    "source_id": cand.source_id,
                    "query": query,
                })
                if sid:
                    used_stock_ids.add(sid)
                logger.info(
                    "chunk %d stock-fill: %s:%s %.2fs for %.2fs remaining",
                    scene_idx, cand.source, cand.source_id, dur, remaining,
                )
                _contribution = min(remaining, dur, _fill_cap) if _fill_cap > 0 else min(remaining, dur)
                remaining -= _contribution
                if remaining <= 0.25:
                    break
            return fillers

        self._write_stage_progress(
            project_id,
            "fine_cut",
            "running",
            {"scene_count": len(sentence_windows), "clip_count": len(clip_by_scene)},
        )
        audio_dur = self._probe_audio_duration(tts_path) if tts_path else 0.0
        visual_windows: list[tuple[float, float]] = []
        for i, (start_s, end_s) in enumerate(sentence_windows):
            slot_start = float(start_s)
            if i + 1 < len(sentence_windows):
                next_start = float(sentence_windows[i + 1][0])
                slot_end = max(float(end_s), next_start)
            else:
                slot_end = max(float(end_s), float(audio_dur or 0.0))
            if slot_end <= slot_start:
                slot_end = slot_start + max(0.5, float(end_s) - float(start_s))
            visual_windows.append((slot_start, slot_end))
        if visual_windows and audio_dur > 0:
            logger.info(
                "visual cuts include narration pauses: speech=%.2fs visual=%.2fs audio=%.2fs",
                sum(max(0.0, e - s) for s, e in sentence_windows),
                sum(max(0.0, e - s) for s, e in visual_windows),
                audio_dur,
            )
        # Visual-slot merge. Shorts default to roughly one visual shot every
        # three seconds (about 20 shots/minute), while non-portrait outputs keep
        # the old 1:1 sentence behavior unless explicitly configured.
        # coalesce on-screen windows shorter than the threshold into the
        # previous window (reusing that scene's clip) so a brief sub-sentence
        # doesn't flash a <1s clip — and so fewer distinct clips are needed,
        # easing repeated-footage pressure. Subtitles come from the TTS sidecar
        # (per-sentence) independently of these windows, so merging never shifts
        # caption timing; total coverage is unchanged (prev window extends to
        # cover the merged span, so the video still covers the narration).
        try:
            _min_shot = float(
                os.environ.get("MEDIA_BUDDY_MIN_SHOT_SECONDS", "0") or 0
            )
        except ValueError:
            _min_shot = 0.0
        try:
            _target_shot_s = float(
                os.environ.get(
                    "MEDIA_BUDDY_SHORTS_TARGET_SHOT_SECONDS",
                    "3.0" if is_portrait else "0",
                ) or 0
            )
        except ValueError:
            _target_shot_s = 0.0
        max_shots = None
        if _target_shot_s > 0.0 and visual_windows:
            visual_total = max(0.0, float(visual_windows[-1][1] - visual_windows[0][0]))
            max_shots = max(1, int(math.ceil(visual_total / _target_shot_s)))
        protected_scene_indices = {
            i for i in range(len(visual_windows))
            if self._fine_cut_scene_merge_protected(
                (clip_by_scene.get(i) or {}).get("plan") or {},
                sentences[i] if i < len(sentences) else "",
                clip_by_scene.get(i) or {},
            )
        }
        # 画面跟着语音内容走(短视频):某句的 clip 与前一句【不是同一素材】时 → 保护它不被并进
        # 上一句。否则一句短句(如单独讲"角蛙")会被吞进上一句、整段沿用上一句的画面(普通蛙),
        # 于是语音说到"角蛙"、画面却还是普通蛙。clip 变了 = 画面内容变了 = 这句必须切到自己的
        # 镜头。长句仍由下面的 _max_shot_s(_cap)切成 ≤3.5s 的多个 take(同一 clip),不会超长静止。
        _visual_align_env = os.environ.get("MEDIA_BUDDY_VISUAL_FOLLOW_SPEECH", "1").strip().lower()
        if is_portrait and _visual_align_env not in ("0", "false", "no", "off"):
            def _clip_key(c: Optional[dict]) -> str:
                c = c or {}
                return str(c.get("local_path") or c.get("source_id") or "")
            for i in range(1, len(visual_windows)):
                cur = _clip_key(clip_by_scene.get(i))
                prev = _clip_key(clip_by_scene.get(i - 1))
                if cur and cur != prev:
                    protected_scene_indices.add(i)
        windows_for_cuts = self._merge_short_windows(
            visual_windows, _min_shot, max_shots=max_shots,
            protected_scene_indices=protected_scene_indices,
        )
        if len(windows_for_cuts) < len(visual_windows):
            logger.info(
                "visual-slot merge: %d windows -> %d shots (min=%.2fs target=%.2fs max=%s)",
                len(visual_windows), len(windows_for_cuts), _min_shot,
                _target_shot_s, max_shots,
            )
        if protected_scene_indices:
            logger.info(
                "visual-slot protected hard-anchor scenes from merge/compression: %s",
                sorted(protected_scene_indices),
            )

        # ~3s shot cap (rule: "一般3秒钟一个镜头"). A long narration window is
        # split into several ≤_max_shot_s shots, each a DISTINCT on-theme clip,
        # instead of one 8s clip (or one clip + a long freeze) that goes boring.
        # 0 disables (old behavior). Default 3.5s for portrait shorts only.
        try:
            _max_shot_s = float(
                os.environ.get(
                    "MEDIA_BUDDY_SHORTS_MAX_SHOT_SECONDS",
                    "3.5" if is_portrait else "0",
                ) or 0
            )
        except ValueError:
            _max_shot_s = 0.0

        # 一个 take 的最小时长:切 ≤_max_shot_s 的镜头时,绝不留一个比这还短的余数 take
        # (否则窗口长度不是 _max_shot_s 整数倍时,最后会冒出一个 0.3~1 秒的镜头一闪而过)。
        _min_take = max(1.2, _min_shot * 0.6) if _max_shot_s > 0 else 0.0

        def _cap(avail: float) -> float:
            """把可用时长切成一个镜头:不超 _max_shot_s;但如果切完剩下的不到 _min_take,
            就把这点零头并进本镜(略超 cap 也不留闪一下的碎镜)。"""
            avail = max(0.0, float(avail))
            if _max_shot_s <= 0:
                return avail
            if avail <= _max_shot_s:
                return avail
            if avail - _max_shot_s < _min_take:
                return avail  # 余数太碎,整段吃进来,别留闪镜
            return _max_shot_s

        # Scarce-footage shot cap (rule: prefer ~3s, but when there's nothing
        # fresh to show and reuse is blocked by the dedup budget, a real clip may
        # stretch up to this many seconds instead of freezing. 0 = no scarce cap
        # (grow toward natural). Default 7s for portrait shorts.
        try:
            _scarce_max = float(
                os.environ.get(
                    "MEDIA_BUDDY_SHORTS_SCARCE_MAX_SHOT_SECONDS",
                    "7.0" if is_portrait else "0",
                ) or 0
            )
        except ValueError:
            _scarce_max = 0.0

        # All distinct on-theme clips already downloaded for this video — used to
        # fill a long slot with VARIETY (short reused shots) when fresh stock-fill
        # is exhausted, instead of holding one clip for a boring 8-10s.
        _all_clips = [
            (c, max(0.5, float(c.get("duration") or 0.5)))
            for c in clip_by_scene.values()
            if c.get("local_path")
        ]
        # Composition-layer reuse must obey the same video-level duplicate
        # budget as selection fallback. Count selected primary assets up front
        # because a reused future-scene clip is still a duplicate in the final
        # video even if its primary cut has not been appended yet.
        _compose_asset_counts = self._compose_asset_counts(list(clip_by_scene.values()))

        # ★ compose 头号瓶颈:补片是"一个镜头一个镜头串着搜+下"。这里先把所有需要补片
        # 的镜头一次性【并行】搜+下(44 核同时开几十个),后面装配循环直接取结果。
        _fills_by_scene: dict[int, list] = {}
        _fill_jobs: list[tuple[int, dict, float]] = []
        for _si, _ss, _es in windows_for_cuts:
            _cl = _pick_clip_for(_si)
            _cd = max(0.5, float(_es - _ss))
            _pt = _cap(min(_cd, max(0.5, float(_cl["duration"]))))
            _rem = _cd - _pt
            if _rem > 0.25:
                _fill_jobs.append((_si, _cl, _rem))
        if _fill_jobs:
            try:
                _fsc = max(1, int(os.environ.get("MEDIA_BUDDY_COMPOSE_FILL_SCENE_CONCURRENCY", "12")))
            except ValueError:
                _fsc = 12
            if _fsc <= 1 or len(_fill_jobs) <= 1:
                for _si, _cl, _rem in _fill_jobs:
                    _fills_by_scene[_si] = _stock_fill_clips(_si, _cl, _rem)
            else:
                from concurrent.futures import ThreadPoolExecutor as _TPE3, as_completed as _ac3
                with _TPE3(max_workers=min(_fsc, len(_fill_jobs))) as _fex:
                    _fmap = {
                        _fex.submit(_stock_fill_clips, _si, _cl, _rem): _si
                        for _si, _cl, _rem in _fill_jobs
                    }
                    for _fut in _ac3(_fmap):
                        try:
                            _fills_by_scene[_fmap[_fut]] = _fut.result()
                        except Exception as _e:
                            logger.warning("chunk %d parallel stock-fill failed: %s", _fmap[_fut], _e)
                            _fills_by_scene[_fmap[_fut]] = []

        cuts = []
        for scene_idx, start_s, end_s in windows_for_cuts:
            self._raise_if_stopped(project_id)
            clip = _pick_clip_for(scene_idx)
            cut_dur = max(0.5, float(end_s - start_s))
            clip_natural = max(0.5, float(clip["duration"]))
            # media-buddy-rules.md sections 11/14: visuals must cover the
            # narration slot. Prefer an extra local/stock clip for the tail;
            # only freeze the final frame when no cheap filler is available.
            primary_take = _cap(min(cut_dur, clip_natural))
            cuts.append({
                "source": clip["local_path"],
                "in_seconds": 0.0,
                "out_seconds": primary_take,
                "scene_index": scene_idx,
                "natural_duration": clip_natural,
                "loop": False,
                "subject_h_pos": clip.get("subject_h_pos"),
            })
            remaining = cut_dur - primary_take
            if remaining > 0.25:
                for filler in _fills_by_scene.get(scene_idx, []):
                    fill_natural = max(0.5, float(filler["duration"]))
                    fill_take = _cap(min(remaining, fill_natural))
                    cuts.append({
                        "source": filler["local_path"],
                        "in_seconds": 0.0,
                        "out_seconds": fill_take,
                        "scene_index": scene_idx,
                        "natural_duration": fill_natural,
                        "loop": False,
                        "fill_query": filler.get("query", ""),
                        "subject_h_pos": filler.get("subject_h_pos"),
                    })
                    remaining -= fill_take
                    if remaining <= 0.25:
                        break
            if remaining > 0.25 and _max_shot_s > 0 and _all_clips:
                # Not enough FRESH stock-fill for this long slot. Rather than hold
                # one clip for a boring 8-10s, reuse OTHER chunks' already-loaded
                # on-theme clips as distinct ~3s shots (no extra cost/search). Skip
                # this slot's own clip and rotate by scene so reuse is spread out.
                used_in_slot = {c["source"] for c in cuts}
                pool = [
                    (c, n) for (c, n) in _all_clips
                    if c.get("local_path") and c.get("local_path") not in used_in_slot
                ]
                ri = scene_idx
                guard = 0
                while remaining > 0.25 and pool and guard < 12:
                    reuse_clip, nat = pool[ri % len(pool)]
                    ri += 1
                    guard += 1
                    path = reuse_clip.get("local_path")
                    if path in used_in_slot:
                        continue
                    asset_key = self._compose_asset_key(reuse_clip)
                    if not self._compose_reuse_allowed(_compose_asset_counts, asset_key):
                        logger.info(
                            "chunk %d reuse-fill skipped %s due to repeat budget "
                            "(asset_count=%d total_repeats=%d max_total=%d)",
                            scene_idx,
                            asset_key,
                            int(_compose_asset_counts.get(asset_key, 0) or 0),
                            self._compose_duplicate_reuse_count(_compose_asset_counts),
                            self._short_v2_max_total_repeat_occurrences(),
                        )
                        continue
                    rejection = _reuse_rejection_for_scene(scene_idx, clip, reuse_clip)
                    if rejection:
                        logger.info(
                            "chunk %d reuse-fill rejected %s:%s (%s)",
                            scene_idx,
                            reuse_clip.get("source", ""),
                            reuse_clip.get("source_id", ""),
                            rejection,
                        )
                        continue
                    take = _cap(min(remaining, nat))
                    cuts.append({
                        "source": path,
                        "in_seconds": 0.0,
                        "out_seconds": take,
                        "scene_index": scene_idx,
                        "natural_duration": nat,
                        "loop": False,
                        "fill_query": reuse_clip.get("query", ""),
                        "fill_source": "reuse",
                        "subject_h_pos": reuse_clip.get("subject_h_pos"),
                    })
                    self._compose_record_asset_use(_compose_asset_counts, asset_key)
                    used_in_slot.add(path)
                    remaining -= take
            if remaining > 0.25 and cuts:
                # Footage scarce (no fresh fill + reuse hit the dedup budget).
                # HARD RULE: never freeze and never exceed the scarce cap (7s).
                # Cover the rest with REAL motion by slicing more segments from
                # THIS slot's OWN clips at advancing offsets (different parts of a
                # long clip = variety; wrap to 0 when a clip is exhausted). These
                # are the chunk's own footage, keyed by the same asset, so they do
                # shot is ≤ _scarce_max; we prefer ~3s but allow up to 7s here.
                seg_cap = _scarce_max if _scarce_max > 0 else (_max_shot_s or 7.0)
                slot_cuts = [c for c in cuts if c.get("scene_index") == scene_idx] or [cuts[-1]]
                next_off: dict[str, float] = {}
                nat_of: dict[str, float] = {}
                subpos_of: dict[str, object] = {}
                for c in slot_cuts:
                    src = c["source"]
                    next_off[src] = max(next_off.get(src, 0.0), float(c["out_seconds"]))
                    nat_of[src] = float(c["natural_duration"])
                    subpos_of[src] = c.get("subject_h_pos")
                order = sorted(nat_of, key=lambda s: -nat_of[s])  # longest clips first
                guard = 0
                while remaining > 0.25 and guard < 30:
                    guard += 1
                    progressed = False
                    for src in order:
                        if remaining <= 0.25:
                            break
                        nat = nat_of[src]
                        off = next_off.get(src, 0.0)
                        if off >= nat - 0.3:        # clip exhausted → replay from 0 (real motion)
                            off = 0.0
                        room = nat - off
                        if room < 0.5:
                            continue
                        seg = min(remaining, seg_cap, room)
                        if seg < 0.3:
                            continue
                        cuts.append({
                            "source": src,
                            "in_seconds": round(off, 3),
                            "out_seconds": round(off + seg, 3),
                            "scene_index": scene_idx,
                            "natural_duration": nat,
                            "loop": False,
                            "subject_h_pos": subpos_of.get(src),
                        })
                        next_off[src] = off + seg
                        remaining -= seg
                        progressed = True
                    if not progressed:
                        break

        # Cuts are unique-by-scene_index but multiple cuts may share
        # the same source clip (when a chunk had no clip). asset_manifest
        # only needs one entry per unique clip path — dedupe.
        valid = []
        seen_paths: set[str] = set()
        for c in cuts:
            sp = c["source"]
            if sp in seen_paths:
                continue
            seen_paths.add(sp)
            valid.append({"local_path": sp, "duration": 0.0,
                          "scene_index": c["scene_index"]})
        edit_decisions = {
            "render_runtime": "ffmpeg",
            "renderer_family": "documentary-montage",
            "cuts": cuts,
            "total_duration_seconds": sum(c["out_seconds"] - c["in_seconds"] for c in cuts),
        }
        fine_cut_debug_path = project_dir / "fine_cut_debug.json"
        try:
            debug_payload = {
                "project_id": str(project_id),
                "output_format": output_format,
                "audio_duration_seconds": audio_dur,
                "min_shot_seconds": _min_shot,
                "target_shot_seconds": _target_shot_s,
                "max_shot_seconds": _max_shot_s,
                "max_shots": max_shots,
                "protected_scene_indices": sorted(protected_scene_indices),
                "sentences": [
                    {
                        "scene_index": i,
                        "sentence": sentences[i] if i < len(sentences) else "",
                        "speech_window": list(sentence_windows[i]) if i < len(sentence_windows) else None,
                        "visual_window": list(visual_windows[i]) if i < len(visual_windows) else None,
                        "plan": (clip_by_scene.get(i) or {}).get("plan") or {},
                        "clip": {
                            k: (clip_by_scene.get(i) or {}).get(k)
                            for k in (
                                "local_path", "duration", "query", "source",
                                "source_id", "source_tags",
                            )
                        },
                    }
                    for i in range(max(len(sentences), len(visual_windows)))
                ],
                "windows_for_cuts": [
                    {"scene_index": si, "start": start, "end": end}
                    for si, start, end in windows_for_cuts
                ],
                "cuts": cuts,
            }
            fine_cut_debug_path.write_text(
                json.dumps(debug_payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("fine-cut debug artifact write failed: %s", e)
        asset_manifest = {
            "assets": [
                {"id": f"clip_{i}", "path": c["local_path"], "type": "video"}
                for i, c in enumerate(valid)
            ]
        }
        self._write_stage_progress(
            project_id,
            "fine_cut",
            "completed",
            {
                "cut_count": len(cuts),
                "asset_count": len(valid),
                "total_duration_seconds": round(edit_decisions["total_duration_seconds"], 2),
                "fine_cut_debug_path": str(fine_cut_debug_path),
            },
        )

        # Resolve final audio path: if both narration and bgm exist, mix them
        # with sidechain ducking; if only one, use it directly.
        final_audio = self._resolve_final_audio(
            tts_path, bgm_path, project_dir / "assets", project_id,
        )

        compose_inputs: dict = {
            "operation": "render",
            "edit_decisions": edit_decisions,
            "asset_manifest": asset_manifest,
            "output_path": str(output_path),
            "profile": output_format,
            "framing_style": framing_style,
        }
        if final_audio:
            compose_inputs["audio_path"] = str(final_audio)

        # Generate SRT — pull sentence text via cut.scene_index so visual + sub align
        srt_path = None
        portrait_outputs = ("youtube_shorts", "tiktok", "instagram_reels")
        is_portrait = output_format in portrait_outputs
        if include_subtitles:
            self._write_stage_progress(
                project_id,
                "subtitles",
                "running",
                {"profile": output_format, "portrait": is_portrait},
            )
            # Subtitles show the FULL sentence text — must use display.
            sentences = self._expand_sentences_for_display(script)
            srt_path = self._build_srt(
                sentences, cuts, project_dir / "subtitles.srt",
                narration_path=tts_path,
            )
            # 字幕语言(可独立于配音):繁体走 OpenCC、跨语言走批量翻译、双语产 pairs。
            # 时间戳一律沿用上面的 SRT/句 token,不改。空字段 → 跟随 output_language,
            # 与旧项目逐字节一致。全程 try/except → 失败退回旁白语言字幕,绝不挡出片。
            if srt_path:
                sub_primary, sub_secondary = self._resolve_subtitle_language({"subtitle_language": subtitle_language}, output_language)
                try:
                    if sub_secondary:
                        # 双语:竖屏 + 横屏都产 pairs(横屏烧录也会读它出主大副小两行)。
                        pairs = self._build_bilingual_pairs(
                            sentences, tts_path, cuts, output_language, sub_primary, sub_secondary,
                        )
                        if pairs:
                            import json as _json
                            (srt_path.parent / "subtitles.pairs.json").write_text(
                                _json.dumps(pairs, ensure_ascii=False), encoding="utf-8",
                            )
                        elif not self._subtitle_lang_matches_narration(sub_primary, output_language):
                            self._localize_single_subtitle(srt_path, sentences, tts_path, cuts, output_language, sub_primary)
                    elif not self._subtitle_lang_matches_narration(sub_primary, output_language):
                        # 单语目标(或双语落到横屏)→ 只本地化主语言 SRT
                        self._localize_single_subtitle(srt_path, sentences, tts_path, cuts, output_language, sub_primary)
                except Exception:
                    logger.warning(
                        "subtitle language localization failed; keeping narration-language subtitles",
                        exc_info=True,
                    )
            if srt_path and not is_portrait:
                # Landscape/square: burned post-compose by _burn_subtitles_landscape
                # dropped every 16:9 long-video subtitle. The real burn is below.
                logger.info(
                    "Subtitles (landscape, deferred to bottom burn post-compose): %s",
                    srt_path,
                )
            elif srt_path and is_portrait:
                logger.info(
                    "Subtitles (portrait, deferred to safe-zone burn post-compose): %s",
                    srt_path,
                )
            self._write_stage_progress(
                project_id,
                "subtitles",
                "completed" if srt_path else "skipped",
                {"subtitle_path": str(srt_path) if srt_path else None, "portrait": is_portrait},
            )
        else:
            self._write_stage_progress(
                project_id,
                "subtitles",
                "skipped",
                {"reason": "subtitles_disabled"},
            )

        self._write_stage_progress(
            project_id,
            "export",
            "running",
            {"output_path": str(output_path), "profile": output_format},
        )
        self._raise_if_stopped(project_id)
        result = self.compose_tool.execute(compose_inputs)
        if not result.success:
            error_msg = f"Compose failed: {getattr(result, 'error', 'unknown')}"
            write_progress(
                self.PIPELINE_DIR,
                project_id,
                "compose",
                "failed",
                {
                    "render_report": {
                        "output_path": str(output_path),
                        "profile": output_format,
                        "cut_count": len(cuts),
                    }
                },
                error=error_msg,
            )
            self._write_stage_progress(
                project_id,
                "export",
                "failed",
                {"output_path": str(output_path), "profile": output_format},
                error=error_msg,
            )
            raise RuntimeError(error_msg)

        # media-buddy-rules.md §17 (Final Validation Checklist) — minimal
        # form: probe the rendered file and assert duration ≥ narration
        # duration (rule 11). If output is shorter than narration, the
        # last seconds of dialogue were cut — surface this loudly rather
        # than letting the user discover it themselves.
        self._audit_compose_output(
            Path(output_path), tts_path, footage_results,
        )

        # Phase 2.11c — portrait subtitle safe-zone burn (post-compose).
        # so OpenMontage's hardcoded MarginV=100 / FontSize=24 doesn't apply.
        if include_subtitles and is_portrait and srt_path:
            self._write_stage_progress(
                project_id,
                "subtitles",
                "running",
                {"subtitle_path": str(srt_path), "portrait": is_portrait, "burn_safe_zone": True},
            )
            burned = self._burn_subtitles_safe_zone(
                Path(output_path), srt_path,
                font_scale=subtitle_font_scale,
            )
            if burned and burned != Path(output_path):
                # Atomically replace the compose output with the captioned one
                try:
                    Path(output_path).unlink(missing_ok=True)
                    burned.rename(output_path)
                except Exception as e:
                    logger.warning("subtitle replace failed: %s — using burned in place", e)
                    output_path = burned
            self._write_stage_progress(
                project_id,
                "subtitles",
                "completed",
                {"subtitle_path": str(srt_path), "portrait": is_portrait, "burn_safe_zone": True},
            )

        # VideoCompose dropped OpenMontage's subtitle burner, so 16:9 long
        # videos shipped with NO captions even though subtitles.srt existed.
        # This burns them bottom-center (YouTube footer style) post-compose,
        # entirely separate from the portrait safe-zone path above — portrait
        # stays on _burn_subtitles_safe_zone, untouched.
        if include_subtitles and not is_portrait and srt_path:
            self._write_stage_progress(
                project_id,
                "subtitles",
                "running",
                {"subtitle_path": str(srt_path), "portrait": False, "burn_bottom": True},
            )
            if os.environ.get("MEDIA_BUDDY_BURN_LANDSCAPE_SUBTITLES", "0") != "1":
                logger.info(
                    "landscape subtitle burn skipped; SRT sidecar kept: %s",
                    srt_path,
                )
                self._write_stage_progress(
                    project_id,
                    "subtitles",
                    "completed",
                    {
                        "subtitle_path": str(srt_path),
                        "portrait": False,
                        "burn_bottom": False,
                        "reason": "landscape_burn_disabled",
                    },
                )
            else:
                burned = self._burn_subtitles_landscape(
                    Path(output_path), srt_path,
                    font_scale=subtitle_font_scale,
                )
                if burned and burned != Path(output_path):
                    try:
                        Path(output_path).unlink(missing_ok=True)
                        burned.rename(output_path)
                    except Exception as e:
                        logger.warning("subtitle replace failed: %s — using burned in place", e)
                        output_path = burned
                self._write_stage_progress(
                    project_id,
                    "subtitles",
                    "completed",
                    {"subtitle_path": str(srt_path), "portrait": False, "burn_bottom": True},
                )

        write_progress(
            self.PIPELINE_DIR,
            project_id,
            "compose",
            "completed",
            {
                "render_report": {
                    "output_path": str(output_path),
                    "profile": output_format,
                    "cuts": cuts,
                }
            },
        )
        self._write_stage_progress(
            project_id,
            "export",
            "completed",
            {
                "render_report": {
                    "output_path": str(output_path),
                    "profile": output_format,
                    "cut_count": len(cuts),
                }
            },
        )
        self._cleanup_intermediate_assets_after_export(project_dir, Path(output_path))
        return output_path

    def _cleanup_intermediate_assets_after_export(
        self, project_dir: Path, output_path: Path,
    ) -> None:
        """Remove project-scoped downloaded/generated media after final export.

        The final MP4 lives under output/. The temporary assets/ directory holds
        downloaded clips, music, narration and mix files; keeping it around can
        expose stock/media files and can later pollute production decisions.
        """
        if os.environ.get("MEDIA_BUDDY_KEEP_PROJECT_ASSETS", "0") == "1":
            return
        try:
            project_root = Path(project_dir).resolve()
            assets_dir = (project_root / "assets").resolve()
            final_output = Path(output_path).resolve()
            if not final_output.exists():
                logger.warning(
                    "skip intermediate asset cleanup; final output missing: %s",
                    final_output,
                )
                return
            if assets_dir == project_root or project_root not in assets_dir.parents:
                logger.warning("skip unsafe asset cleanup path: %s", assets_dir)
                return
            if not assets_dir.exists():
                return
            shutil.rmtree(assets_dir)
            logger.info("cleaned intermediate project assets after export: %s", assets_dir)
        except Exception as e:
            logger.warning("intermediate asset cleanup failed: %s", e)

    @staticmethod
    def _reject_stock_fill_candidate(
        query: str, candidate, primary_source_tags: str = "",
        chunk_contract=None, chunk_plan: dict | None = None, sentence: str = "",
    ) -> str:
        """Reject unsafe filler clips before they enter the final timeline.

        Filler clips are selected after the primary chunk winner. They must
        preserve the primary clip's confirmed visual anchors, not merely avoid
        a blacklist. This keeps any vertical/domain workflow from drifting into
        unrelated visuals when a long sentence needs extra footage.

        fractal-script disaster surfaced that stock-fill could pull
        French-food clips from L0 library even when the orchestrator's
        primary path had already rejected them at reject_floor 3.0. Root
        cause: this guard only checked a hardcoded banned list + visual
        anchor overlap (which fails when query is Chinese — no English
        anchors extractable). Now the chunk's ChunkContract reject rules
        also gate filler candidates. No LLM call — text-level keyword scan
        only. The primary path already paid the LLM cost for the chunk
        winner; this is a cheap secondary safety net.
        """
        query_l = (query or "").lower()
        raw = getattr(candidate, "_raw", None)
        category = str(getattr(raw, "category", "") or "").lower()
        haystack = " ".join([
            str(getattr(candidate, "source_tags", "") or ""),
            str(getattr(raw, "description", "") or ""),
            category,
        ]).lower()
        intent_text = " ".join([
            str(query or ""),
            str(sentence or ""),
            " ".join(str(x) for x in ((chunk_plan or {}).get("must_show") or [])),
            " ".join(str(x) for x in ((chunk_plan or {}).get("queries") or [])),
        ]).lower()

        if (
            PipelineService._stock_fill_candidate_cash_like(haystack)
            and not PipelineService._stock_fill_allows_cash_intent(intent_text)
        ):
            return "cash_filler_without_cash_intent"

        banned_terms = (
            "keyboard", "backlit", "electronic", "technology", "tech",
            "computer", "laptop", "monitor", "screen", "button",
            "green_screen", "green screen", "studio", "tripod",
        )
        if category == "tech":
            return "banned_category_tech"
        for term in banned_terms:
            if term in haystack:
                return f"banned_visual_tag:{term}"

        # chunk_contract. Defensive: contract may be None for chunks the
        if chunk_contract is not None:
            hit = PipelineService._contract_reject_keyword_hit(
                chunk_contract, haystack,
            )
            if hit:
                return f"contract_reject_if_true:{hit}"

        candidate_terms = PipelineService._visual_anchor_terms(haystack)
        primary_terms = PipelineService._visual_anchor_terms(primary_source_tags)
        query_terms = PipelineService._visual_anchor_terms(query_l)

        if primary_terms:
            overlap = primary_terms & candidate_terms
            if not overlap:
                return "missing_primary_visual_anchor"
        elif query_terms:
            overlap = query_terms & candidate_terms
            if not overlap:
                return "missing_query_visual_anchor"

        return ""

    @staticmethod
    def _stock_fill_candidate_cash_like(haystack: str) -> bool:
        return any(term in (haystack or "").lower() for term in (
            "cash", "money", "rupee", "rupees", "currency", "banknote",
            "bank note", "bill", "bills", "₹", "rs.",
        ))

    @staticmethod
    def _stock_fill_allows_cash_intent(text: str) -> bool:
        text = (text or "").lower()
        return any(term in text for term in (
            "cash", "money", "rupee", "rupees", "currency", "banknote",
            "counting money", "paying", "payment", "price", "pricing",
            "profit", "cost", "revenue", "margin",
            "收钱", "现金", "数钱", "卢比", "定价", "成本", "利润", "收入", "毛利",
        ))

    @staticmethod
    def _contract_reject_keyword_hit(chunk_contract, haystack: str) -> str:
        """Scan candidate's haystack (lowercased tags+description+category)
        against the chunk contract's reject signals. Return the first hit
        keyword for logging; "" if clean.

        Pulls keywords from three contract fields:
        1. visual_must_not_convey  — concepts the candidate must NOT convey
        2. likely_wrong_matches[].wrong_match — AI-predicted wrong matches
        3. verification_tests[] where severity == "reject_if_true"

        Tokenisation is intentionally crude (split on whitespace + drop
        short tokens). Stop words skipped because contract reject phrases
        like "people eating" → both "people" and "eating" carry signal.
        """
        # Build the keyword pool from contract reject signals.
        sources: list[str] = []

        # 1. visual_must_not_convey — list[str]
        mnc = getattr(chunk_contract, "visual_must_not_convey", None) or []
        sources.extend(str(s) for s in mnc)

        # 2. likely_wrong_matches[].wrong_match — list[LikelyWrongMatch]
        lwm = getattr(chunk_contract, "likely_wrong_matches", None) or []
        for entry in lwm:
            wm = getattr(entry, "wrong_match", None)
            if wm:
                sources.append(str(wm))

        # 3. verification_tests where severity == "reject_if_true"
        vt = getattr(chunk_contract, "verification_tests", None) or []
        for test in vt:
            if getattr(test, "severity", "") == "reject_if_true":
                q = getattr(test, "question", None)
                if q:
                    sources.append(str(q))

        if not sources:
            return ""

        # Tokenize each source — split on whitespace + common punctuation,
        # keep tokens >= 4 chars to avoid noise ("the"/"is"/"of"/etc.)
        # being scanned. 4 chars cuts most English stop words while
        # keeping content nouns like "food", "cafe", "tofu".
        import re
        keywords: set[str] = set()
        for s in sources:
            for tok in re.split(r"[\s,;:.!?\-_/]+", s.lower()):
                if len(tok) >= 4 and tok.isascii():
                    keywords.add(tok)

        # Drop the most common English filler verbs/connectors that would
        # match too broadly in stock library tags ("show", "have", "with").
        # This list is tight — only words that show up as filler in
        # contract reject phrases AND would cause false positives.
        _FILLER_TOKENS = {
            "show", "shows", "with", "have", "having", "from", "into",
            "that", "this", "these", "those", "when", "where", "what",
            "candidate", "footage", "video", "clip", "scene", "scenes",
            "image", "images", "photo", "photos", "shot", "shots",
        }
        keywords -= _FILLER_TOKENS

        for kw in keywords:
            if kw in haystack:
                return kw
        return ""

    @staticmethod
    def _visual_anchor_terms(text: str) -> set[str]:
        """Extract reusable visual anchors for primary/filler consistency."""
        stop = {
            "the", "and", "with", "for", "from", "into", "onto", "that",
            "this", "shot", "stock", "clip", "selected", "video", "image",
            "photo", "close", "view", "focus", "focusing", "soft", "dark",
            "light", "lighting", "green", "red", "blue", "white", "black",
            "static", "camera", "pan", "motion", "hand", "interaction",
            "product", "beautiful", "beautifully", "presented", "main",
            "course", "served", "concept", "scene", "atmosphere",
        }
        aliases = {
            "café": "cafe",
            "caf": "cafe",
            "meals": "meal",
            "dishes": "dish",
            "foods": "food",
            "restaurants": "restaurant",
            "desserts": "dessert",
            "screens": "screen",
            "keyboards": "keyboard",
            "speakers": "speaker",
        }
        terms: set[str] = set()
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text or ""):
            token = aliases.get(token.lower(), token.lower())
            if token in stop:
                continue
            terms.add(token)
            for related in PipelineService._related_visual_terms(token):
                terms.add(related)
        # A few important visual compounds are more useful than their parts.
        lowered = (text or "").lower()
        for phrase in (
            "green screen", "backlit keyboard", "coffee cup", "red wine",
            "beef meal", "dining table", "french cafe",
        ):
            if phrase in lowered:
                terms.add(phrase.replace(" ", "_"))
        return terms

    @staticmethod
    def _related_visual_terms(term: str) -> set[str]:
        """Small semantic expansion for parent/child visual continuity."""
        related = {
            "cake": {"dessert", "pastry", "sweet", "chocolate"},
            "dessert": {"cake", "pastry", "sweet", "chocolate"},
            "pastry": {"cake", "dessert", "bakery", "sweet"},
            "cherry": {"cherries", "fruit", "dessert", "cake"},
            "cherries": {"cherry", "fruit", "dessert", "cake"},
            "chocolate": {"cake", "dessert", "sweet"},
            "bread": {"baguette", "bakery", "pastry", "food"},
            "baguette": {"bread", "bakery", "food"},
            "beef": {"meal", "dish", "stew", "food"},
            "stew": {"beef", "meal", "dish", "food"},
            "wine": {"glass", "dining", "restaurant", "meal"},
            "coffee": {"cafe", "cup", "drink", "breakfast"},
            "cafe": {"coffee", "restaurant", "table", "breakfast"},
            "seafood": {"appetizer", "dish", "meal", "food"},
            "appetizer": {"seafood", "dish", "meal", "food"},
        }
        return related.get(term, set())

    def _extract_scene_queries_for_sentences(
        self, sentences: list[str], max_scenes: int = 8
    ) -> list[str]:
        """
        Get one English director-style query per sentence (1:1 alignment).
        Truncates to `max_scenes` to keep output video length sane.
        """
        sentences = sentences[:max_scenes]
        if not sentences:
            return ["business professional"]
        try:
            queries = self.llm.extract_scene_queries(sentences)
            if queries and len(queries) == len(sentences):
                return queries
            logger.warning(
                "LLM returned %d queries for %d sentences — falling back",
                len(queries) if queries else 0, len(sentences),
            )
        except Exception as e:
            logger.warning("extract_scene_queries failed: %s — falling back", e)
        # Regex fallback returns 1:1 by construction
        return self._parse_scenes_per_sentence(sentences)

    @staticmethod
    def _parse_scenes_per_sentence(sentences: list[str]) -> list[str]:
        """Fallback: 1:1 sentence → naive English-ish query (best effort)."""
        out = []
        for sent in sentences:
            words = [w for w in sent.split() if len(w) > 3][:4]
            out.append(" ".join(words) if words else "scenic background")
        return out

    @staticmethod
    def _probe_audio_duration(path: Path) -> float:
        """Return duration in seconds via ffprobe. Returns 0.0 on any failure."""
        ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
        if not ffprobe or not Path(path).exists():
            return 0.0
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return float(json.loads(r.stdout)["format"]["duration"])
        except Exception as e:
            logger.warning("ffprobe failed for %s: %s", path, e)
            return 0.0

    @staticmethod
    def _parse_scenes(script_text: str, max_scenes: int = 8) -> list[str]:
        """Fallback: split script into scene queries by sentence."""
        sentences = re.split(r"(?<=[.!?])\s+", script_text.strip())
        queries = []
        for sent in sentences[:max_scenes]:
            words = [w for w in sent.split() if len(w) > 3][:4]
            if words:
                queries.append(" ".join(words))
        return queries or ["business professional"]

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences. Handles both Latin (.!?) and CJK (。！？)
        punctuation.

        ``(?<=[.!?。！？])\\s*`` matched even zero whitespace after a Latin
        period, so "1.26 维！" got cut between "1." and "26 维！" producing
        a 4-char fragment that hit empty_atoms in plan_chunks Round 3 →
        PlanChunksError. New rule:
          • CJK terminators (。！？) always split (zero whitespace OK,
            mid-CJK has no spaces).
          • Latin .!? splits ONLY when followed by whitespace, so decimals
            ("1.26") and abbreviations stay intact.
        """
        text = re.sub(r"\s+", " ", text.strip())
        parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", text)
        return [p.strip() for p in parts if p.strip()]

    # example". Used both for connector-based sentence splitting (Path A)
    # and for the is_abstract code override in plan plans.
    # Ordering: longer first so "比方说" wins over "比如".
    _MULTI_SHOT_CONNECTORS: tuple[str, ...] = (
        "比方说", "举个例子", "比如说", "比如", "例如", "譬如",
    )

    # trailing voiceover-only conclusion that attaches to a concrete
    # example already driving the visual. Letting them stay doubles the
    # atoms budget and triggers gramophone-style L5 generic matches:
    #   smoke 2 chunk 8: "比如你肺里的支气管和树木的形状，都是在有限空间
    #   内实现最大化覆盖的最佳方案" → atoms = [airways, trees, coverage],
    #   L1 "airways and trees maximizing shape coverage" (unfilmable),
    #   L5 "trees nature landscape b roll" (Pixabay turntable trap).
    # TTS still speaks the whole sentence (TTSService.synthesize() takes
    # the full script string, not the chunk list), compose's
    # _estimate_chunk_durations stretches the prior chunk's video time
    # proportionally to cover the dropped text's audio.
    # Anchored on a preceding comma so single-char "而" doesn't false-
    # match phrases like "而已 / 反而 / 进而" mid-sentence.
    _CLAUSE_TRUNCATE_RE = re.compile(
        r"[，,]\s*(?:都是|都|因为|所以|但是|然而|不过|而且|而)"
    )

    @classmethod
    def _truncate_at_clause_continuation(cls, sentence: str) -> str:
        """Cut a sub-sentence at the first clause-continuation connector.
        Returns the original sentence when there's no match or when the
        truncated prefix would be too short (<4 chars) to drive a useful
        stock search.
        """
        if not sentence:
            return sentence
        match = cls._CLAUSE_TRUNCATE_RE.search(sentence)
        if not match:
            return sentence
        truncated = sentence[:match.start()].rstrip("，,、 ；;:：").strip()
        if len(truncated) < 4:
            return sentence
        return truncated

    @classmethod
    def _split_on_connectors(cls, sentence: str) -> list[str]:
        """Split one sentence at the first "比如/例如/譬如/..." connector.

        narration habitually writes abstract claim FIRST, concrete example
        AFTER a connector ("比如/例如"). The concrete half searches well in
        stock libraries; the abstract half should fork to cinematic
        ideation. Splitting at the connector gives both halves their own
        chunk_index + their own search path.

        The connector itself stays with the second half so it remains a
        self-contained example sub-sentence:

            "分形结构的局部包含了整体的信息，比如肺里的支气管和树木的形状"
            → ["分形结构的局部包含了整体的信息",
               "比如肺里的支气管和树木的形状"]

        Returns ``[sentence]`` when no connector is found, or when the
        connector appears within the first 4 chars (avoids splitting at
        sentence start where the connector is part of the natural flow).
        """
        if not sentence or len(sentence) < 8:
            return [sentence] if sentence else []
        best_pos = -1
        best_conn = ""
        for conn in cls._MULTI_SHOT_CONNECTORS:
            # Skip the first 4 chars so we don't split right at the start
            # (e.g. "比如说，咖啡是日常饮品" should stay one piece).
            pos = sentence.find(conn, 4)
            if pos > 0 and (best_pos < 0 or pos < best_pos):
                best_pos = pos
                best_conn = conn
        if best_pos < 0:
            return [sentence]
        left = sentence[:best_pos].rstrip("，,、 ；;:：").strip()
        right = sentence[best_pos:].strip()
        # Defensive: refuse to split if either side ends up too short
        # (under 4 chars). Short fragments can't drive useful stock search.
        if len(left) < 4 or len(right) < 4:
            return [sentence]
        return [left, right]

    def _expand_sentences_for_visual(self, script: str) -> list[str]:
        """
        (atoms cascade, video search). Truncated at clause-continuation
        connectors so atoms stay clean.

        For TTS-aligned consumers (SRT subtitles, chunk durations, music
        arc), use :meth:`_expand_sentences_for_display` instead — that
        list keeps the full text so TTS char alignment doesn't fall out
        of sync when the truncated tail is still spoken in the audio.

        Both lists have the SAME LENGTH and SAME chunk indices so the
        chunk → outcome → subtitle/duration mapping stays consistent.

        Performs:

        1. Base sentence split (period / CJK punctuation)
        2. Connector-based split: each sentence containing "比如/例如/譬如"
           gets cut at the connector → 2 sub-sentences. Deterministic,
           cheap, no LLM call.
        3. Optional LLM-driven split for STILL-unsplit sentences flagged
           shot_count > 1 by ``LLMClient.assess_shot_counts``. Gated by
           ``MEDIA_BUDDY_LLM_MULTI_SHOT_SPLIT`` (default 1; set 0 to
           disable). One LLM call per pipeline run, cached by script hash.

        Cache shape: ``self._sentence_cache[hash] = (visual, display)``.
        """
        visual, _display = self._expand_sentences_for_pipeline(script)
        return visual

    def _expand_sentences_for_display(self, script: str) -> list[str]:
        """
        consumers (SRT subtitles, chunk_durations, music arc). Keeps the
        FULL TEXT of each sub-sentence — clause-continuation tails
        ("都是覆盖最佳方案") are preserved here even though they're
        truncated from the visual list, so TTS char alignment matches
        what the audio actually speaks.

        Same length as :meth:`_expand_sentences_for_visual` so chunk
        indices map 1:1 across both representations.
        """
        _visual, display = self._expand_sentences_for_pipeline(script)
        return display

    def _expand_sentences_for_pipeline(
        self, script: str,
    ) -> tuple[list[str], list[str]]:
        """Internal — computes both visual + display sentence lists,
        caches by script hash, and returns the pair. Callers use the
        public ``_expand_sentences_for_{visual,display}`` accessors.
        """
        cache = getattr(self, "_sentence_cache", None)
        if cache is None:
            self._sentence_cache = {}
            cache = self._sentence_cache
        coarse = bool(getattr(self, "_coarse_shots", False))
        h = hashlib.sha256((script or "").encode("utf-8")).hexdigest()
        if coarse:
            h = h + ":coarse"
        if h in cache:
            return cache[h]

        base = self._split_sentences(script)
        if not base:
            cache[h] = ([], [])
            return ([], [])

        # Stage 1: connector-based split (deterministic regex). Both
        # visual + display lists start identical here.
        connector_split: list[str] = []
        for s in base:
            connector_split.extend(self._split_on_connectors(s))

        # — visual list drops the trailing voiceover-only conclusion
        # ("都是...最佳方案") so atoms stay clean. Display list keeps
        # the full sub-sentence so TTS char alignment matches the audio.
        display_list = list(connector_split)
        visual_list = [
            self._truncate_at_clause_continuation(s) for s in connector_split
        ]
        # _truncate_at_clause_continuation never returns empty when given
        # non-empty input (it returns the original on too-short prefix),
        # so visual_list stays in 1:1 alignment with display_list.

        # Stage 2: LLM-driven split for long sentences without connectors.
        # LLM split divides the ORIGINAL sentence into N sub-sentences
        # that the LLM thinks each deserve their own shot. Both visual
        # and display get the SAME LLM splits (no truncation gap to
        # bridge here — TTS speaks each sub naturally).
        use_llm = os.environ.get(
            "MEDIA_BUDDY_LLM_MULTI_SHOT_SPLIT", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        if coarse:
            # Long video: connector-only granularity. Skips the multi-shot LLM
            # split entirely — fewer chunks (→ far fewer observer calls) and no
            # per-sentence split LLM cost.
            use_llm = False
        if not use_llm:
            logger.info(
                "_expand_sentences_for_visual: connector-only, "
                "%d → %d sentences",
                len(base), len(connector_split),
            )
            cache[h] = (visual_list, display_list)
            return cache[h]

        # Only consider sentences that did NOT already split (the connector
        # path already gave them their multi-shot). Also skip short ones —
        # under 25 chars rarely benefits from further splitting. Use the
        # DISPLAY list for these checks so the LLM sees the full text (the
        # visual_list may have already-truncated tails which would lower
        # length and skip candidates that should split).
        candidates_idx = [
            i for i, s in enumerate(display_list)
            if len(s) >= 25
            and not any(c in s for c in self._MULTI_SHOT_CONNECTORS)
        ]
        if not candidates_idx:
            logger.info(
                "_expand_sentences_for_visual: %d → %d (connector only, "
                "no LLM candidates)",
                len(base), len(connector_split),
            )
            cache[h] = (visual_list, display_list)
            return cache[h]

        candidates_sents = [display_list[i] for i in candidates_idx]
        try:
            shot_counts = self.llm.assess_shot_counts(candidates_sents)
        except Exception as e:
            logger.warning(
                "assess_shot_counts failed: %s — skipping LLM split", e,
            )
            cache[h] = (visual_list, display_list)
            return cache[h]

        if not shot_counts or len(shot_counts) != len(candidates_idx):
            cache[h] = (visual_list, display_list)
            return cache[h]

        sc_map = dict(zip(candidates_idx, shot_counts))
        final_visual: list[str] = []
        final_display: list[str] = []
        llm_split_count = 0
        for i in range(len(display_list)):
            n_shots = int(sc_map.get(i, 1) or 1)
            if n_shots > 1:
                subs = self.llm.split_sentence_into_subs(
                    display_list[i], n_shots,
                )
                if isinstance(subs, list) and len(subs) > 1:
                    logger.info(
                        "LLM split (n=%d) %r → %s",
                        n_shots, display_list[i][:40],
                        [t[:30] for t in subs],
                    )
                    # LLM-split subs go into BOTH visual and display
                    # (no clause truncation happens at this stage —
                    # subs are direct partitions of the full text).
                    final_visual.extend(subs)
                    final_display.extend(subs)
                    llm_split_count += 1
                    continue
            final_visual.append(visual_list[i])
            final_display.append(display_list[i])

        logger.info(
            "_expand_sentences_for_visual: %d base → %d connector → "
            "%d final (LLM split %d sentences)",
            len(base), len(connector_split), len(final_visual), llm_split_count,
        )
        cache[h] = (final_visual, final_display)
        return cache[h]

    @staticmethod
    def _srt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - h * 3600 - m * 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    @staticmethod
    @staticmethod
    def _wrap_cjk_for_ass(text: str, max_per_line: float = 7.0) -> str:
        r"""Hard-wrap a subtitle line for ASS rendering. libass does NOT
        auto-wrap mid-CJK (no whitespace = no break candidates), so a 22-char
        sentence in 115px font stays on one line and overflows.

        Strategy: count fullwidth CJK as 1.0, latin as 0.5; once a running
        line meets ``max_per_line``, break after the next CJK punctuation,
        or hard-break at ``max_per_line + 1.5`` to avoid runaway. Returns
        the same text with `\N` (ASS line separator) at break points."""
        # English / space-delimited: NEVER break mid-word — wrap on word boundaries
        # into lines of ~max_per_line*3 chars (CJK 7/line ≈ EN 21/line). The CJK
        # char-budget logic below would slice English words ("star|s"), so route
        # pure-Latin text here (mixed CJK+Latin lines stay on the CJK path).
        if PipelineService._is_latin_caption(text):
            budget = max(12.0, float(max_per_line) * 3.0)
            lines: list[str] = []
            cur_l = ""
            for w in text.split():
                if not cur_l:
                    cur_l = w
                elif len(cur_l) + 1 + len(w) <= budget:
                    cur_l += " " + w
                else:
                    lines.append(cur_l); cur_l = w
            if cur_l:
                lines.append(cur_l)
            return r"\N".join(lines)
        PUNCT = "，。！？、；：,.!?;:"

        def _wide(s: str) -> float:
            return sum(1.0 if (ord(c) > 0x2E80 or c in PUNCT) else 0.5 for c in s)

        # CJK 优先【按词换行】(jieba):不把词折到下一行,消除行内破句。
        bounds = _cjk_word_bounds(text)
        if bounds:
            words: list[str] = []
            prev = 0
            for b in bounds:
                if b > prev:
                    words.append(text[prev:b]); prev = b
            limit = float(max_per_line) + 1.5
            wl: list[str] = []
            cur_w = ""
            for w in words:
                if cur_w and _wide(cur_w) + _wide(w) > limit:
                    wl.append(cur_w); cur_w = w
                else:
                    cur_w += w
            if cur_w:
                wl.append(cur_w)
            packed = r"\N".join(s.strip() for s in wl if s.strip())
            if packed:
                PipelineService._warn_if_stacked(text, packed)
                return packed

        # 退回:jieba 不可用时的老逐字换行(标点优先,超限硬折)。
        out: list[str] = []
        cur = ""
        cur_len = 0.0
        for ch in text:
            is_full = (ord(ch) > 0x2E80) or (ch in PUNCT)
            cur += ch
            cur_len += 1.0 if is_full else 0.5
            if cur_len >= max_per_line and ch in PUNCT:
                out.append(cur); cur = ""; cur_len = 0.0
            elif cur_len >= max_per_line + 1.5:
                out.append(cur); cur = ""; cur_len = 0.0
        if cur:
            out.append(cur)
        packed = r"\N".join(s.strip() for s in out if s.strip())
        PipelineService._warn_if_stacked(text, packed)
        return packed

    @staticmethod
    def _warn_if_stacked(text: str, packed: str) -> None:
        """换行兜底真的折了行 → 吼一声。

        🚨 单行模式下【本不该走到这里】：每条字幕的字数已经压到
           一行放得下。真折了，说明某条路径的行宽比条宽还小 ——
           这时候叠两行**仍然比冲出画面强**，所以只记录、不改行为。
        """
        if not PipelineService._SUB_SINGLE_LINE or r"\N" not in packed:
            return
        logger.warning(
            "subtitle STACKED in single-line mode: %d lines for %r"
            " —— 这条路径的行宽小于条宽,去查 max_chars_per_line",
            packed.count(r"\N") + 1, str(text)[:40],
        )

    @staticmethod
    def _srt_to_ass(srt_path: Path, ass_path: Path,
                     width: int, height: int,
                     font_size: int, outline: int,
                     margin_v: int, margin_l: int, margin_r: int,
                     max_chars_per_line: float = 9.0) -> bool:
        """Convert an SRT file to an ASS file with explicit PlayResX/PlayResY.

        Why we do this instead of `subtitles=...:force_style=...`:
        libass renders FontSize using the formula
            real_pixels = FontSize × (output_height / PlayResY)
        When ffmpeg's subtitles filter auto-converts SRT→ASS internally, it
        defaults PlayResY=288. So `FontSize=44` actually rendered at
            44 × (1920/288) = 293 pixels — gigantic.
        `original_size` only affects positioning, NOT font scaling.
        `force_style` can override [V4+ Styles] entries but NOT [Script Info]
        keys like PlayResX/PlayResY.

        The only reliable fix: pre-generate an ASS file with PlayResX/PlayResY
        equal to the actual video dimensions. Then FontSize is interpreted as
        raw pixels.
        """
        # Parse the SRT into (start_ts, end_ts, text) tuples
        text = srt_path.read_text(encoding="utf-8", errors="replace")
        import re
        blocks = re.split(r"\r?\n\r?\n", text.strip())
        events: list[tuple[str, str, str]] = []
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            # Skip the optional sequence number on first line
            time_line_idx = 1 if re.fullmatch(r"\d+", lines[0]) else 0
            if time_line_idx >= len(lines):
                continue
            time_line = lines[time_line_idx]
            m = re.match(
                r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
                time_line,
            )
            if not m:
                continue
            sh, sm, ss, sms = m.group(1, 2, 3, 4)
            eh, em, es, ems = m.group(5, 6, 7, 8)
            # ASS uses h:mm:ss.cs (centiseconds, 0-99)
            start = f"{int(sh)}:{int(sm):02d}:{int(ss):02d}.{int(sms[:2] or 0):02d}"
            end = f"{int(eh)}:{int(em):02d}:{int(es):02d}.{int(ems[:2] or 0):02d}"
            body = " ".join(lines[time_line_idx + 1:])
            # Escape ASS-special chars: { } and newlines
            body = body.replace("{", r"\{").replace("}", r"\}")
            # Manually CJK-wrap so libass doesn't run a 22-char sentence off
            # the right margin. Portrait default ~9 fullwidth chars per line at
            # 96px font on 1080×1920 (864 usable / 96 ≈ 9). Landscape passes a
            # larger value (wider frame, smaller font) via max_chars_per_line.
            body = PipelineService._wrap_cjk_for_ass(body, max_per_line=max_chars_per_line)
            events.append((start, end, body))

        if not events:
            return False

        # Build the ASS file. Note PlayResX / PlayResY both = real video dims,
        # so 1 ASS pixel = 1 real pixel — FontSize is honest.
        # Alignment=2 → bottom-center anchor (1=BL, 2=BC, 3=BR, 4=ML, 5=MC,
        # 6=MR, 7=TL, 8=TC, 9=TR). MarginV is measured from the BOTTOM edge
        # for bottom-anchored alignments. WrapStyle=0 = smart auto-wrap by
        # width, keeping lines inside MarginL/MarginR.
        shadow = max(1, outline // 3)
        ass_lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "Collisions: Normal",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "ScaledBorderAndShadow: yes",
            "WrapStyle: 0",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, "
                "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
                "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
                "MarginV, Encoding"
            ),
            (
                f"Style: Default,{_SUBTITLE_FONT},"
                f"{font_size},"
                "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
                "-1,0,0,0,"   # Bold=true, others off
                "100,100,0,0,"
                "1,"          # BorderStyle=1 (outline+shadow, no opaque box)
                f"{outline},{shadow},"
                "2,"          # Alignment=2 = bottom-center
                f"{margin_l},{margin_r},{margin_v},1"
            ),
            "",
            "[Events]",
            (
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text"
            ),
        ]
        for start, end, body in events:
            ass_lines.append(
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}"
            )
        ass_path.write_text("\n".join(ass_lines), encoding="utf-8")
        return True

    @staticmethod
    def _write_ass_karaoke(
        kdata: list, ass_path: Path, *, width: int, height: int,
        font_size: int, outline: int, margin_v: int, margin_l: int, margin_r: int,
    ) -> bool:
        """逐字卡拉OK高亮 ASS:用 ElevenLabs 字符级毫秒时长生成 \\k 标签。
        已读=Primary(黄),未读=Secondary(白),libass 按音频进度逐字刷色。
        WrapStyle=2 → 不自动换行(一行)。"""
        try:
            shadow = max(1, outline // 3)

            def _t(sec: float) -> str:
                cs = int(round(float(sec) * 100))
                return f"{cs // 360000:d}:{(cs % 360000) // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}"

            lines = [
                "[Script Info]", "ScriptType: v4.00+", "Collisions: Normal",
                f"PlayResX: {width}", f"PlayResY: {height}",
                "ScaledBorderAndShadow: yes", "WrapStyle: 2", "",
                "[V4+ Styles]",
                ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                 "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                 "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                 "Alignment, MarginL, MarginR, MarginV, Encoding"),
                # PrimaryColour(已读)=黄 &H0000FFFF;SecondaryColour(未读)=白 &H00FFFFFF
                (f"Style: Default,{_SUBTITLE_FONT},{font_size},"
                 "&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,"
                 "-1,0,0,0,100,100,0,0,1,"
                 f"{outline},{shadow},2,{margin_l},{margin_r},{margin_v},1"),
                "", "[Events]",
                ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                 "MarginV, Effect, Text"),
            ]
            for cue in kdata:
                txt = "".join(
                    "{\\k%d}%s" % (max(0, int(d)), ch) for ch, d in (cue.get("k") or [])
                )
                if not txt:
                    continue
                lines.append(
                    f"Dialogue: 0,{_t(cue['start'])},{_t(cue['end'])},Default,,0,0,0,,{txt}"
                )
            Path(ass_path).write_text("\n".join(lines), encoding="utf-8")
            return True
        except Exception:
            logger.warning("karaoke ASS build failed", exc_info=True)
            return False

    def _strip_srt_punctuation(self, srt_path):
        """Burn-only: produce a punctuation-free copy of the SRT (<stem>.burn.srt)
        used solely for subtitle burn-in.

        Short-form captions read cleaner without trailing/pause punctuation
        (。，、？！… etc). The original subtitles.srt sidecar keeps punctuation
        (for download / export / reuse); only this burn copy is stripped. Falls
        back to the original srt_path on any error — never breaks rendering.
        """
        from pathlib import Path as _P
        try:
            src = _P(srt_path)
            raw = src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return srt_path
        # Punctuation to drop: CJK full-width + ASCII half-width. Digits, letters
        # and existing spaces are preserved.
        drop = set("。，、；：？！…⋯·～「」『』“”‘’（）()《》〈〉【】〔〕—–~,.?!;:\"'`")
        _NUMSEP = set(".,:：．，")  # keep decimal / thousands / ratio between digits: 43.3 / 5,000 / 3:1
        out = []
        for line in raw.replace("\r\n", "\n").split("\n"):
            t = line.strip()
            # index lines, timestamp lines and blank lines pass through untouched
            if (not t) or t.isdigit() or ("-->" in line):
                out.append(line.rstrip())
                continue
            chars = []
            for _i, ch in enumerate(line):
                if ch in drop:
                    if (ch in _NUMSEP and 0 < _i < len(line) - 1
                            and line[_i - 1].isdigit() and line[_i + 1].isdigit()):
                        chars.append(ch)  # 43.3 / 5,000 / 3:1 — digit-grouping separator kept
                    continue
                chars.append(ch)
            cleaned = " ".join("".join(chars).split())  # collapse spaces left by stripping
            out.append(cleaned)
        try:
            dst = src.with_suffix(".burn.srt")
            dst.write_text("\n".join(out), encoding="utf-8")
            return dst
        except Exception:
            return srt_path

    def _burn_subtitles_safe_zone(
        self, video_path: Path, srt_path: Path, font_scale: float = 1.0,
    ) -> Optional[Path]:
        """Phase 2.11c — 9:16 short-form subtitle burn.

            (Alignment=2 = bottom-center anchor)
            Big and clear like real short-form captions, not a TV-style
          - WrapStyle=0: smart auto-wrap by width (lines stay inside margins)
          - Microsoft YaHei Bold for CJK + Latin coverage on Windows

        to the video frame size. Without explicit PlayRes, libass scales font
        from a 288-tall reference canvas → real_px = FontSize × (1920/288) ≈
        7× FontSize. Result: 44 → 293px, fills the screen. The fix is the
        ASS file, not ffmpeg flags.

        Returns the new captioned mp4 path, or None on failure.
        """
        orig_srt = Path(srt_path)  # 卡拉OK逐字数据在它旁边(<stem>.kdata.json)
        srt_path = self._strip_srt_punctuation(srt_path)
        out_path = video_path.with_name(f"{video_path.stem}_captioned.mp4")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not on PATH — skipping subtitle burn")
            return None

        # Probe real video dimensions (default 1080x1920 if probe fails).
        width, height = 1080, 1920
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                pr = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(video_path),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if pr.returncode == 0:
                    parts = pr.stdout.strip().split("x")
                    if len(parts) == 2:
                        width = int(parts[0]); height = int(parts[1])
            except Exception as e:
                logger.warning("ffprobe failed; using 1080x1920: %s", e)

        # Pixel-honest sizes (PlayResX/Y = real dims, so no libass scaling).
        # On 1080×1920 → 96px.
        # 可用 MEDIA_BUDDY_SUBTITLE_FONT_PCT 微调。
        _font_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_FONT_PCT", "0.064") or "0.064")
        # 客户拖拉杆自选的字号缩放系数(1.0=标准)乘上去。
        font_size = max(48, int(round(height * _font_pct * font_scale)))
        _ol_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_OUTLINE_PCT", "0.025") or "0.025")
        outline = max(2, int(round(font_size * _ol_pct)))
        # Bottom-anchored: MarginV measured from BOTTOM edge (Alignment=2).
        _mv_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_MARGIN_V_PCT", "0.20") or "0.20")
        margin_v = int(round(height * _mv_pct))
        margin_l = int(round(width * 0.07))
        margin_r = int(round(width * 0.07))

        # Generate the ASS sidecar with correct PlayRes.
        ass_path = srt_path.with_suffix(".ass")
        # 逐字卡拉OK高亮:有 ElevenLabs 字符级毫秒数据(<srt>.kdata.json)且开关开时,
        # 用 \k 标签 ASS(读到哪个字哪个字变黄);否则走普通 SRT→ASS。
        # 默认关(用户觉得卡拉OK高亮丑,要白字)。要逐字高亮设
        # MEDIA_BUDDY_KARAOKE_SUBTITLES=1 重开。白字路径(_srt_to_ass)本就白字+黑边。
        used_karaoke = False
        if os.environ.get("MEDIA_BUDDY_KARAOKE_SUBTITLES", "0").strip().lower() not in (
            "0", "false", "no", "",
        ):
            kdata_path = orig_srt.with_suffix(".kdata.json")
            if kdata_path.exists():
                try:
                    import json as _json
                    kdata = _json.loads(kdata_path.read_text(encoding="utf-8"))
                    if kdata and self._write_ass_karaoke(
                        kdata, ass_path,
                        width=width, height=height,
                        font_size=font_size, outline=outline,
                        margin_v=margin_v, margin_l=margin_l, margin_r=margin_r,
                    ):
                        used_karaoke = True
                        logger.info("subtitle burn: karaoke ASS (%d cues)", len(kdata))
                except Exception:
                    used_karaoke = False
        # 双语字幕:字幕入口写了 subtitles.pairs.json → 直接生成「主大副小」两行 ASS,
        if not used_karaoke:
            pairs_path = orig_srt.with_name("subtitles.pairs.json")
            if pairs_path.exists():
                try:
                    import json as _json
                    pairs = _json.loads(pairs_path.read_text(encoding="utf-8"))
                except Exception:
                    pairs = None
                if pairs:
                    _bi_main_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_BILINGUAL_MAIN_PCT", "0.049") or "0.049")
                    _bi_sub_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_BILINGUAL_SUB_PCT", "0.032") or "0.032")
                    _bi_mv_pct = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_BILINGUAL_MARGIN_V_PCT", "0.14") or "0.14")
                    main_font = max(40, int(round(height * _bi_main_pct * font_scale)))
                    sub_font = max(28, int(round(height * _bi_sub_pct * font_scale)))
                    bi_margin_v = int(round(height * _bi_mv_pct))
                    _bi_main_pl = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_PER_LINE", "7") or "7")
                    _bi_sub_pl = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_EN_PER_LINE", "9") or "9")
                    if self._pairs_to_ass(
                        pairs, ass_path, width=width, height=height,
                        main_font=main_font, sub_font=sub_font, outline=outline,
                        margin_v=bi_margin_v, margin_l=margin_l, margin_r=margin_r,
                        main_per_line=_bi_main_pl, sub_per_line=_bi_sub_pl,
                    ):
                        used_karaoke = True  # 复用闸门:下面的 SRT→ASS 会被跳过
                        logger.info("subtitle burn: bilingual ASS (%d cues)", len(pairs))
        # 每行 7 个中文字换行(14 字一条 → 最多上下两行);可用 MEDIA_BUDDY_SUBTITLE_PER_LINE 调。
        _per_line = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_PER_LINE", "7") or "7")
        if not used_karaoke and not self._srt_to_ass(
            srt_path, ass_path,
            width=width, height=height,
            font_size=font_size, outline=outline,
            margin_v=margin_v, margin_l=margin_l, margin_r=margin_r,
            max_chars_per_line=_per_line,
        ):
            logger.warning("SRT→ASS conversion produced no events; skipping burn")
            return None

        # Escape Windows drive colon for the subtitles filter
        ass_str = str(ass_path).replace("\\", "/")
        if len(ass_str) > 1 and ass_str[1] == ":":
            ass_str = ass_str[0] + r"\:" + ass_str[2:]

        # encoder helper (libx264 → h264_mf → mpeg4 fallback). Same fix
        # we applied to ReframeService — symptom in user logs was
        # "Unknown encoder 'libx264' / Error opening output file ...
        # _captioned.mp4" → subtitles silently disappear from the final.
        from backend.lib.ffmpeg_locator import video_encoder_args
        encoder_args = video_encoder_args(
            ffmpeg, crf="16", preset="medium", bitrate="16000k",
            mpeg4_quality="2",
        )
        cmd = [
            ffmpeg, "-y", "-i", str(video_path),
            "-vf", f"subtitles='{ass_str}'",
            *encoder_args,
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_compose_timeout(600))
        except subprocess.TimeoutExpired:
            logger.warning("subtitle burn timed out")
            return None
        if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size <= 0:
            logger.warning(
                "subtitle burn failed (rc=%s): %s",
                r.returncode, (r.stderr or "")[-400:],
            )
            out_path.unlink(missing_ok=True)
            return None
        logger.info(
            "subtitle burn → %s (frame=%dx%d, font=%d, marginV=%d)",
            out_path.name, width, height, font_size, margin_v,
        )
        return out_path

    def _burn_subtitles_landscape(
        self, video_path: Path, srt_path: Path, font_scale: float = 1.0,
    ) -> Optional[Path]:
        """

        burner, so landscape videos shipped with NO captions even though
        subtitles.srt was generated. This restores them with a TV/YouTube-style
        footer — DISTINCT from the portrait safe-zone treatment and completely
        independent of ``_burn_subtitles_safe_zone`` (which stays untouched):

            short-form captions portrait uses
            of a long sentence, so the CJK wrap budget is raised accordingly

        Reuses the shared ``_srt_to_ass`` (PlayRes = real dims → honest pixel
        font sizing). Returns the captioned mp4, or None on failure.
        """
        orig_srt = Path(srt_path)                            # 双语 pairs.json 在它旁边
        srt_path = self._strip_srt_punctuation(srt_path)
        out_path = video_path.with_name(f"{video_path.stem}_captioned.mp4")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not on PATH — skipping landscape subtitle burn")
            return None

        # Probe real dims (default 1920x1080 if probe fails).
        width, height = 1920, 1080
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                pr = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(video_path),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if pr.returncode == 0:
                    parts = pr.stdout.strip().split("x")
                    if len(parts) == 2:
                        width = int(parts[0]); height = int(parts[1])
            except Exception as e:
                logger.warning("ffprobe failed; using 1920x1080: %s", e)

        # TV/YouTube footer sizing (pixel-honest via PlayRes in _srt_to_ass).
        # 客户普遍反映长视频字幕偏小,把"标准"档本身调大一圈;客户还能再用拉杆自选。env 可调。
        _ls_font_pct = float(
            os.environ.get("MEDIA_BUDDY_LANDSCAPE_SUBTITLE_FONT_PCT", "0.062") or "0.062"
        )
        # 客户拖拉杆自选的字号缩放系数(1.0=标准)乘上去。
        font_size = max(28, int(round(height * _ls_font_pct * font_scale)))
        outline = max(2, int(round(font_size * 0.045)))
        _ls_mv_pct = float(os.environ.get("MEDIA_BUDDY_LANDSCAPE_SUBTITLE_MARGIN_V_PCT", "0.12") or "0.12")
        margin_v = int(round(height * _ls_mv_pct))
        margin_l = int(round(width * 0.06))
        margin_r = int(round(width * 0.06))
        usable = max(1, width - margin_l - margin_r)
        max_cpl = float(max(16, min(int(round(usable / max(1, font_size))), 30)))

        ass_path = srt_path.with_suffix(".ass")
        # 双语:字幕入口写了 subtitles.pairs.json → 主大副小两行(横屏字号);否则单语 SRT→ASS。
        built = False
        pairs_path = orig_srt.with_name("subtitles.pairs.json")
        if pairs_path.exists():
            try:
                import json as _json
                pairs = _json.loads(pairs_path.read_text(encoding="utf-8"))
            except Exception:
                pairs = None
            if pairs:
                sub_font = max(20, int(round(font_size * 0.66)))
                if self._pairs_to_ass(
                    pairs, ass_path, width=width, height=height,
                    main_font=font_size, sub_font=sub_font, outline=outline,
                    margin_v=margin_v, margin_l=margin_l, margin_r=margin_r,
                    main_per_line=max_cpl, sub_per_line=max_cpl,
                ):
                    built = True
                    logger.info("landscape subtitle burn: bilingual ASS (%d cues)", len(pairs))
        if not built and not self._srt_to_ass(
            srt_path, ass_path,
            width=width, height=height,
            font_size=font_size, outline=outline,
            margin_v=margin_v, margin_l=margin_l, margin_r=margin_r,
            max_chars_per_line=max_cpl,
        ):
            logger.warning("landscape SRT→ASS produced no events; skipping burn")
            return None

        # Escape Windows drive colon for the subtitles filter.
        ass_str = str(ass_path).replace("\\", "/")
        if len(ass_str) > 1 and ass_str[1] == ":":
            ass_str = ass_str[0] + r"\:" + ass_str[2:]

        from backend.lib.ffmpeg_locator import video_encoder_args
        encoder_args = video_encoder_args(
            ffmpeg, crf="16", preset="medium", bitrate="16000k",
            mpeg4_quality="2",
        )
        cmd = [
            ffmpeg, "-y", "-i", str(video_path),
            "-vf", f"subtitles='{ass_str}'",
            *encoder_args,
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_compose_timeout(900))
        except subprocess.TimeoutExpired:
            logger.warning("landscape subtitle burn timed out")
            return None
        if r.returncode != 0 or not out_path.exists():
            logger.warning(
                "landscape subtitle burn failed (rc=%s): %s",
                r.returncode, (r.stderr or "")[-400:],
            )
            return None
        logger.info(
            "landscape subtitle burn → %s (frame=%dx%d, font=%d, marginV=%d)",
            out_path.name, width, height, font_size, margin_v,
        )
        return out_path

    @staticmethod
    def _denormalize_srt_display(srt_path: Optional[Path]) -> Optional[Path]:
        """
        ElevenLabs 正确读出来,但字幕文本来自 TTS 对齐 token,跟着变成了 "百分之80"。
        只动字幕文本,不碰语音和时间轴;时间行不含 "百分之",绝不会误伤。best-effort。"""
        if srt_path is None:
            return None
        try:
            import re as _re
            p = Path(srt_path)
            txt = p.read_text(encoding="utf-8")
            fixed = _re.sub(r"百分之(\d+(?:\.\d+)?)", r"\1%", txt)
            # 年份还原:TTS 侧把 "1749年" 归一成 "一七四九年" 读对了,字幕跟着变了,
            # 观众该看到原文 "1749年" → 逐字中文数字年份还原回阿拉伯数字。
            try:
                from backend.lib.tts_text_norm import restore_years_for_display
                fixed = restore_years_for_display(fixed)
            except Exception:  # noqa: BLE001
                pass
            if fixed != txt:
                p.write_text(fixed, encoding="utf-8")
        except Exception:
            logger.warning("srt display denormalize failed", exc_info=True)
        return srt_path

    @classmethod
    def _build_srt(
        cls, sentences: list[str], cuts: list[dict], srt_path: Path,
        narration_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """Generate an SRT file.

        Phase 2.11h — preferred path: when the TTS produced a per-word
        timestamp sidecar (``<narration>.words.json``), align each
        sentence to its actual spoken time. The viewer hears audio +
        sees subtitle that match to within ~50ms.

        Fallback path: clip-driven timing — each cut's visual duration
        defines the subtitle time slot. Used when no sidecar exists
        (legacy projects or non-Edge TTS providers).
        """
        if not sentences or not cuts:
            return None

        # Try the word-sidecar path first
        if narration_path is not None:
            sidecar = Path(narration_path).with_suffix(".words.json")
            if sidecar.exists():
                # Edge TTS emits SentenceBoundary tokens (text + start + end).
                # Build the SRT DIRECTLY from them: each spoken sentence becomes
                # sequential, non-overlapping cue(s) that match the audio exactly.
                # This avoids aligning the (LLM-over-split) display sentences to
                # fewer TTS sentences, which produced OVERLAPPING cues — captions
                # stacked at different heights and out of sync with the narration.
                sent_srt = cls._write_srt_from_sentence_tokens(sidecar, srt_path)
                if sent_srt is not None:
                    return cls._denormalize_srt_display(sent_srt)
                char_tokens = cls._read_char_tokens(sidecar)
                if char_tokens:
                    # 失败保留原 token(旧行为)。
                    char_tokens = cls._realign_char_tokens_to_display(
                        sentences, char_tokens,
                    ) or char_tokens
                    char_srt = cls._write_srt_from_char_tokens(
                        sentences, char_tokens, srt_path,
                    )
                    if char_srt is not None:
                        return cls._denormalize_srt_display(char_srt)
                    # 字符级 grouping 失配(如 ElevenLabs 把数字 normalize 得和脚本
                    # 不一致)→ 不能直接没字幕!退到 word-align,再退到 legacy 时钟驱动,
                    # 保证字幕**永不静默跳过**(最差也用切片时长给字幕)。
                    logger.warning(
                        "char-token SRT failed (alignment mismatch); "
                        "falling back to word/clip-driven subtitles",
                    )
                aligned = cls._align_sentences_to_words(sentences, sidecar)
                if aligned:
                    return cls._denormalize_srt_display(cls._write_srt_from_aligned(
                        sentences, aligned, srt_path,
                    ))

        # Legacy fallback — clip-driven timing
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        cursor = 0.0
        sub_no = 0
        for clip in cuts:
            clip_dur = float(clip["out_seconds"]) - float(clip["in_seconds"])
            scene_idx = clip.get("scene_index")
            sent = (
                sentences[scene_idx]
                if isinstance(scene_idx, int) and 0 <= scene_idx < len(sentences)
                else None
            )
            start = cursor
            end = cursor + clip_dur
            if sent:
                sub_no += 1
                lines.append(str(sub_no))
                lines.append(f"{cls._srt_time(start)} --> {cls._srt_time(end)}")
                lines.append(sent)
                lines.append("")
            cursor = end

        srt_path.write_text("\n".join(lines), encoding="utf-8")
        return cls._denormalize_srt_display(srt_path)

    @classmethod
    def _write_srt_from_sentence_tokens(
        cls, sidecar_path: Path, srt_path: Path, max_chars: Optional[int] = None,
    ) -> Optional[Path]:
        """Build an SRT straight from TTS SentenceBoundary tokens.

        Each token carries the spoken sentence text plus its real ``start`` /
        ``end``. Tokens are sequential, so cues NEVER overlap and always match
        the audio. A long sentence is split into sequential sub-cues by char
        count WITHIN that sentence's span (so sub-cues stay in order too).
        Returns ``None`` when the sidecar has no usable sentence tokens, letting
        the caller fall back to char/word alignment.
        """
        import json as _json
        try:
            tokens = _json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(tokens, list):
            return None
        sent = [
            t for t in tokens
            if isinstance(t, dict) and t.get("kind") == "sentence"
            and str(t.get("text") or "").strip()
        ]
        if not sent:
            return None
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        out: list[str] = []
        n = 0
        prev_end = 0.0
        for tok in sent:
            text = str(tok.get("text") or "").strip()
            try:
                start = max(float(tok.get("start", prev_end)), prev_end)
                end = float(tok.get("end", start))
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.4
            prev_end = end
            # 🚨 这条路径以前是【写死 18 字 + 从中间盲切】的：
            #    盲切还会把「载荷」「团队」从词中间劈开。
            # 改成和别的路径同一套：`_split_long_sentence` —— 上限跟随
            # `MAX_CHARS_PER_CUE`，断句走标点 → jieba 词边界 → 最后才硬切，
            # 时间按字数**按比例**分（原来按段数平均分，长段短段一样久）。
            eff = cls.MAX_CHARS_PER_CUE if max_chars is None else int(max_chars)
            for ch, cs, ce in cls._split_long_sentence(text, start, end, eff):
                if not str(ch).strip():
                    continue
                n += 1
                out.append(str(n))
                out.append(f"{cls._srt_time(cs)} --> {cls._srt_time(ce)}")
                out.append(str(ch).strip())
                out.append("")
        if n == 0:
            return None
        srt_path.write_text("\n".join(out), encoding="utf-8")
        return srt_path

    @staticmethod
    def _align_sentences_to_words(
        sentences: list[str], sidecar_path: Path,
    ) -> Optional[list[tuple[float, float]]]:
        """Read the TTS-side sidecar and emit per-sentence (start, end).

        Edge TTS 7.x emits one ``SentenceBoundary`` token per sentence
        (preferred — direct, accurate). Older versions emit per-word
        ``WordBoundary`` events; in that case we accumulate words until
        each sentence's normalised text is matched.

        Returns None on any read/alignment failure so caller can fall
        back to clip-driven timing.
        """
        import json as _json
        import re as _re
        try:
            tokens = _json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("token sidecar read failed: %s", e)
            return None
        if not tokens or not isinstance(tokens, list):
            return None

        # Detect kind. Older sidecars (no "kind" key) are word-level by default.
        sentence_tokens = [t for t in tokens if t.get("kind") == "sentence"]
        if sentence_tokens:
            # Direct-zip path. The boundary tokens may not be 1:1 with our
            # split (Edge sometimes merges adjacent fragments) — match by
            # walking both lists with a normalised-prefix comparison.
            aligned = PipelineService._zip_sentence_tokens(sentences, sentence_tokens)
            if aligned and not PipelineService._aligned_windows_are_monotonic(aligned):
                logger.warning("sentence sidecar produced non-monotonic windows; falling back")
                return None
            return aligned

        char_tokens = [t for t in tokens if t.get("kind") == "char"]
        if char_tokens:
            if not PipelineService._timing_tokens_are_monotonic(char_tokens):
                logger.warning("token sidecar has non-monotonic char timestamps; falling back")
                return None
            aligned = PipelineService._accumulate_chars_into_sentences(
                sentences, char_tokens,
            )
            if aligned and not PipelineService._aligned_windows_are_monotonic(aligned):
                logger.warning("char sidecar produced non-monotonic windows; falling back")
                return None
            return aligned

        # Word-level fallback (old edge-tts or other providers)
        aligned = PipelineService._accumulate_words_into_sentences(sentences, tokens)
        if aligned and not PipelineService._aligned_windows_are_monotonic(aligned):
            logger.warning("word sidecar produced non-monotonic windows; falling back")
            return None
        return aligned

    @staticmethod
    def _read_char_tokens(sidecar_path: Path) -> Optional[list[dict]]:
        import json as _json
        try:
            tokens = _json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("token sidecar read failed: %s", e)
            return None
        if not tokens or not isinstance(tokens, list):
            return None
        char_tokens = [t for t in tokens if t.get("kind") == "char"]
        if char_tokens and not PipelineService._timing_tokens_are_monotonic(char_tokens):
            logger.warning("token sidecar has non-monotonic char timestamps; ignoring sidecar")
            return None
        return char_tokens or None

    @staticmethod
    def _timing_tokens_are_monotonic(tokens: list[dict], *, tolerance: float = 0.05) -> bool:
        previous_start = -1.0
        previous_end = -1.0
        for token in tokens:
            try:
                start = float(token.get("start", 0.0))
                end = float(token.get("end", start))
            except Exception:
                return False
            if end < start:
                return False
            if previous_start >= 0 and start + tolerance < previous_start:
                return False
            if previous_end >= 0 and end + tolerance < previous_end:
                return False
            previous_start = start
            previous_end = end
        return True

    @staticmethod
    def _aligned_windows_are_monotonic(
        aligned: list[tuple[float, float]],
        *,
        tolerance: float = 0.05,
    ) -> bool:
        previous_start = -1.0
        previous_end = -1.0
        for start, end in aligned:
            if end < start:
                return False
            if previous_start >= 0 and start + tolerance < previous_start:
                return False
            if previous_end >= 0 and end + tolerance < previous_end:
                return False
            previous_start = start
            previous_end = end
        return True

    @staticmethod
    def _norm_chars(s: str) -> str:
        """Normalise text for sentence-vs-TTS-token comparison.

        + Latin punctuation. Without punct-stripping,
        ``_split_on_connectors`` strips the comma before "\u6bd4\u5982" so
        display sentence becomes "...\u4fe1\u606f" (no comma) but TTS still
        speaks "\u4fe1\u606f\uff0c\u6bd4\u5982..." \u2014 the alignment walk picks up the orphan
        "\uff0c" token between sentences, the next sentence's group starts
        with mismatched chars, ``_group_char_tokens_by_sentence``
        returns None, SRT is skipped (no subtitles in final video).
        Stripping punctuation to "" makes those orphan tokens invisible
        to alignment so the walk stays in sync.

        \u6545 TTS \u7684\u5b57\u7ea7 token \u662f\u3010\u7b80\u4f53\u3011\uff1b\u800c\u5b57\u5e55\u663e\u793a\u53e5\u6cbf\u7528\u7c98\u8d34\u539f\u6587\uff0c\u53ef\u80fd\u662f\u3010\u7e41\u4f53\u3011\u3002\u82e5\u4e0d\u7edf\u4e00\uff0c
        "\u7e41\u4f53\u53e5 \u2194 \u7b80\u4f53 token" \u4f1a\u9010\u5b57\u5168\u5931\u914d \u2192 \u5b57\u7ea7\u5bf9\u9f50\u9000\u56de\u7c97\u65f6\u95f4\u8f74\uff08\u5b57\u5e55\u53d8\u7cd9\uff09\u3002\u8fd9\u91cc\u628a\u3010\u5339\u914d\u7528\u3011
        \u6587\u672c\u4e24\u4fa7\u90fd\u6298\u53e0\u5230\u7b80\u4f53\uff08t2s \u57fa\u672c 1:1 \u7b49\u957f\uff09\uff1b**\u53ea\u5f71\u54cd\u5339\u914d\uff0c\u663e\u793a\u6587\u672c\u53e6\u8d70\u539f\u53e5\u3001\u4ecd\u662f\u7e41\u4f53**\u3002
        \u5bf9\u7b80\u4f53/\u82f1\u6587\u5185\u5bb9\u662f\u65e0\u64cd\u4f5c\uff1bOpenCC \u7f3a\u5931\u65f6\u4f18\u96c5\u964d\u7ea7\uff08\u4e0d\u6298\u53e0\uff0c\u9000\u56de\u65e7\u884c\u4e3a\uff0c\u7edd\u4e0d\u70b8\uff09\u3002
        """
        import re as _re
        s = _re.sub(
            r"["
            # whitespace + zero-width + invisibles
            r"\s\u3000\u200b\u200c\u200d\ufeff"
            # Chinese punctuation (codepoint escapes \u2014 terminal-safe)
            r"\u3001\u3002\uff01\uff0c\uff0e\uff1a\uff1b\uff1f"   # \u3001\u3002\uff01\uff0c\uff0e\uff1a\uff1b\uff1f
            r"\u201c\u201d\u2018\u2019"                            # \u201c\u201d\u2018\u2019
            r"\u300a\u300b\u300c\u300d\u300e\u300f"                # \u300a\u300b\u300c\u300d\u300e\u300f
            r"\uff08\uff09\u3010\u3011\u2014\u2026\u00b7"          # \uff08\uff09\u3010\u3011\u2014\u2026\u00b7
            # Latin punctuation
            r",.;:!?'\"`\-()\[\]{}"
            r"]+",
            "", s,
        )
        if s:
            try:
                from backend.lib.zh_convert import to_simplified
                s = to_simplified(s)
            except Exception:  # noqa: BLE001 \u2014 \u6298\u53e0\u5931\u8d25\u9000\u56de\u65e7\u884c\u4e3a,\u7edd\u4e0d\u6321\u51fa\u7247
                pass
        return s

    @staticmethod
    def _zip_sentence_tokens(
        sentences: list[str], stokens: list[dict],
    ) -> Optional[list[tuple[float, float]]]:
        """Match our split sentences against TTS sentence boundaries by
        prefix-accumulation. Each TTS boundary may cover one or more of
        our sentences (or vice versa) — we walk both side-by-side, taking
        whichever prefix matches first.
        """
        # Edge TTS can emit one boundary for a full spoken sentence while the
        # visual planner split that sentence into several shots. Slice the
        # parent window by normalized character position so each visual child
        # gets only its own time span.
        norm = PipelineService._norm_chars
        timeline: list[tuple[str, float, float]] = []
        for token in stokens:
            tnorm = norm(str(token.get("text", "")))
            if not tnorm:
                continue
            try:
                start = float(token.get("start", 0.0))
                end = float(token.get("end", start))
            except (TypeError, ValueError):
                logger.warning("sentence alignment token has invalid timing")
                return None
            if end < start:
                logger.warning("sentence alignment token has reversed timing")
                return None
            span = max(0.001, end - start)
            count = max(1, len(tnorm))
            for idx, ch in enumerate(tnorm):
                ch_start = start + span * (idx / count)
                ch_end = start + span * ((idx + 1) / count)
                timeline.append((ch, ch_start, ch_end))
        if not timeline:
            return None

        sliced: list[tuple[float, float]] = []
        cursor = 0
        stream_text = "".join(ch for ch, _start, _end in timeline)
        for sent in sentences:
            target = norm(sent)
            if not target:
                sliced.append((0.0, 0.0))
                continue
            end_cursor = cursor + len(target)
            if end_cursor > len(timeline):
                logger.warning(
                    "sentence alignment ran out of tokens at %r",
                    sent[:30],
                )
                return None
            observed = stream_text[cursor:end_cursor]
            if observed != target:
                logger.warning(
                    "sentence alignment mismatch at %r: expected=%r observed=%r",
                    sent[:30], target[:30], observed[:30],
                )
                return None
            sliced.append((timeline[cursor][1], timeline[end_cursor - 1][2]))
            cursor = end_cursor
        return sliced

    @staticmethod
    def _accumulate_chars_into_sentences(
        sentences: list[str], tokens: list[dict],
    ) -> Optional[list[tuple[float, float]]]:
        """Character-level ElevenLabs alignment.

        ElevenLabs returns per-character start/end seconds. We walk that stream
        into our script sentences, preserving millisecond-level timing for
        subtitles and visual cut decisions.
        """
        norm = PipelineService._norm_chars
        out: list[tuple[float, float]] = []
        token_idx = 0
        consumed_in_token = 0
        for sent in sentences:
            target = norm(sent)
            if not target:
                out.append((0.0, 0.0))
                continue
            collected = ""
            first_t = None
            last_t = None
            while token_idx < len(tokens) and len(collected) < len(target):
                token = tokens[token_idx]
                tnorm = norm(str(token.get("text", "")))
                if not tnorm:
                    token_idx += 1
                    consumed_in_token = 0
                    continue
                remaining = tnorm[consumed_in_token:]
                if not remaining:
                    token_idx += 1
                    consumed_in_token = 0
                    continue
                need = len(target) - len(collected)
                take = remaining[:need]
                if first_t is None:
                    first_t = token
                collected += take
                last_t = token
                if need >= len(remaining):
                    token_idx += 1
                    consumed_in_token = 0
                else:
                    consumed_in_token += need
                    break
            if first_t is None or last_t is None:
                logger.warning(
                    "char alignment ran out at sentence %r", sent[:30],
                )
                return None
            while token_idx < len(tokens):
                token = tokens[token_idx]
                if norm(str(token.get("text", ""))):
                    break
                last_t = token
                token_idx += 1
                consumed_in_token = 0
            out.append((float(first_t["start"]), float(last_t["end"])))
        return out

    @classmethod
    def _realign_char_tokens_to_display(
        cls, sentences: list[str], tokens: list[dict],
    ) -> Optional[list[dict]]:
        """把【TTS 归一化文本】的带时 token(如 "百分之2"、"一九零八")用 difflib 重映射回
        【显示文本】的逐字 token(含标点,与显示句 1:1),时间从原 token 借。

        年份是等长替换,`_group_char_tokens_by_sentence` 靠给 target 做同样年份归一化能对上;但
        粗粒度的 word/clip 兜底(这才是"字幕轻微没对齐"的真根因,与 whisper 无关)。

        difflib 对齐能吃下非等长替换:把两条【norm 后】字符流对齐,匹配上的显示字直接借真时间,
        没匹配上的(%↔百分之 这类)在相邻真值间线性插值。产出的 token 文本 = 显示文本(年份归一化、
        含标点、与显示句逐字 1:1),因此下游 `_group_char_tokens_by_sentence` 全等匹配、
        `_write_srt_from_char_tokens` 的 positional 切片全部照旧生效,无需改动。

        失败(空流/对齐太差)返回 None,调用方保留原 token(= 旧行为,最差退兜底,不会更糟)。
        """
        import difflib
        from backend.lib.tts_text_norm import normalize_years_for_tts
        norm = cls._norm_chars
        if not sentences or not tokens:
            return None
        # 1) 显示骨架:逐字(年份归一化,含标点);记 norm 后非空字 → 骨架下标
        skel: list[dict] = []
        dstr_chars: list[str] = []
        dmap: list[int] = []
        for sent in sentences:
            for ch in normalize_years_for_tts(str(sent or "")):
                skel.append({"kind": "char", "text": ch})
                nc = norm(ch)
                if nc:
                    dmap.append(len(skel) - 1)
                    dstr_chars.append(nc)
        if not skel or not dstr_chars:
            return None
        dstr = "".join(dstr_chars)
        # 2) 原 token 的 norm 字流 + 逐字真时间
        tstr_chars: list[str] = []
        ttimes: list[tuple[float, float]] = []
        for t in tokens:
            nc = norm(str(t.get("text", "")))
            if not nc:
                continue
            try:
                s0 = float(t.get("start", 0.0))
                e0 = float(t.get("end", s0))
            except (TypeError, ValueError):
                continue
            n = len(nc)
            span = (e0 - s0) / n if n else 0.0
            for i, c in enumerate(nc):
                tstr_chars.append(c)
                ttimes.append((s0 + i * span, s0 + (i + 1) * span))
        if not tstr_chars:
            return None
        tstr = "".join(tstr_chars)
        # 3) difflib 对齐:显示 norm 字 ← 原 token 真时间
        sm = difflib.SequenceMatcher(None, tstr, dstr, autojunk=False)
        dtime: list[Optional[tuple[float, float]]] = [None] * len(dstr)
        matched = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    dtime[j1 + k] = ttimes[i1 + k]
                    matched += 1
        if matched < max(1, int(0.5 * len(dstr))):
            return None  # 对齐太差,别信,退回旧行为
        total = ttimes[-1][1]
        # 未匹配 norm 字:相邻真值间线性插值
        last = 0.0
        j = 0
        while j < len(dstr):
            if dtime[j] is not None:
                last = dtime[j][1]
                j += 1
                continue
            run_end = j
            while run_end + 1 < len(dstr) and dtime[run_end + 1] is None:
                run_end += 1
            nxt = dtime[run_end + 1][0] if run_end + 1 < len(dstr) else total
            cnt = run_end - j + 1
            span = max(0.01, (nxt - last) / cnt)
            for t in range(cnt):
                s0 = last + t * span
                dtime[j + t] = (s0, s0 + span)
            last = dtime[run_end][1]
            j = run_end + 1
        # 4) 时间铺回骨架(标点字桥接到下一非空字),单调兜底
        times_full: list[Optional[tuple[float, float]]] = [None] * len(skel)
        for k, si in enumerate(dmap):
            times_full[si] = dtime[k]
        prev_end = 0.0
        for i in range(len(skel)):
            if times_full[i] is None:
                nxt = None
                for m in range(i + 1, len(skel)):
                    if times_full[m] is not None:
                        nxt = times_full[m][0]
                        break
                s0 = prev_end
                e0 = max(s0, nxt if nxt is not None else prev_end)
                times_full[i] = (s0, e0)
            s0, e0 = times_full[i]
            s0 = max(prev_end, float(s0))
            e0 = max(s0, float(e0))
            skel[i]["start"] = round(s0, 3)
            skel[i]["end"] = round(e0, 3)
            prev_end = e0
        logger.info(
            "char tokens realigned to display: %d skel tokens, match=%.2f (tstr=%d dstr=%d)",
            len(skel), matched / max(1, len(dstr)), len(tstr), len(dstr),
        )
        return skel

    @staticmethod
    def _group_char_tokens_by_sentence(
        sentences: list[str], tokens: list[dict],
    ) -> Optional[list[list[dict]]]:
        norm = PipelineService._norm_chars
        from backend.lib.tts_text_norm import normalize_years_for_tts
        out: list[list[dict]] = []
        token_idx = 0
        for sent in sentences:
            # ⚠️ 对齐 TTS 归一化:TTS 把 4 位数年份读成中文数字("1908年"→"一九零八年"),
            # qwen 的 char token 就是按**归一化后的文本**逐字建的(tts_service:1030)。这里
            # 匹配目标也做同样的年份归一化,token 才对得上、grouping 才不失配退到抢跑的
            # 时钟驱动字幕。年份**等长替换**,下游按字符位置切 token、显示仍用原句「1908年」
            # 都不受影响。(没修 % 归一化——它非等长,匹配了反而错位,维持原样退回兜底。)
            target = norm(normalize_years_for_tts(sent))
            if not target:
                out.append([])
                continue
            collected = ""
            group: list[dict] = []
            while token_idx < len(tokens) and len(collected) < len(target):
                token = tokens[token_idx]
                token_idx += 1
                group.append(token)
                tnorm = norm(str(token.get("text", "")))
                if tnorm:
                    collected += tnorm
            got = norm("".join(str(t.get("text", "")) for t in group))
            if got != target:
                # 诊断:定位首个分歧字符位置,打印两侧上下文,判断是"差几个字(可fuzzy救)"
                # 还是"结构性不同"。
                dpos = next((i for i in range(min(len(got), len(target))) if got[i] != target[i]),
                            min(len(got), len(target)))
                logger.warning(
                    "char token grouping mismatch at sentence %r | len got=%d target=%d "
                    "| diverge@%d expected=%r observed=%r",
                    sent[:30], len(got), len(target), dpos,
                    target[max(0, dpos - 8):dpos + 12], got[max(0, dpos - 8):dpos + 12],
                )
                return None
            out.append(group)
        return out

    @staticmethod
    def _subtitle_text_chunks(text: str, max_chars: int) -> list[str]:
        s = text.strip()
        if not s:
            return []
        # English / pure-Latin → word-aware packing (the CJK breaker + hard-cut
        # logic below is char-based and would split English words). Mixed CJK+Latin
        # lines stay on the CJK path.
        if PipelineService._is_latin_caption(s):
            return PipelineService._split_caption_en(s)
        if len(s) <= max_chars:
            return [s]

        # 在标点处切成自然分句。半角 . 和 : 不切(0.1 / 3:1 这类小数、比例要整体,
        # 否则数字被断 + 行尾的 . 被去标点删掉)。
        breakers = "，。！？、；：,!?;"
        pieces: list[str] = []
        buf = ""
        for ch in s:
            buf += ch
            if ch in breakers:
                pieces.append(buf)
                buf = ""
        if buf:
            pieces.append(buf)

        merged: list[str] = []
        cur = ""
        for piece in pieces:
            if not cur:
                cur = piece
            elif len(cur) + len(piece) <= max_chars:
                cur += piece
            else:
                merged.append(cur)
                cur = piece
        if cur:
            merged.append(cur)

        # 兜底:超长分句要断,但【优先断在最近的软停顿标点(、，,)之后】,避免把词组/枚举
        final: list[str] = []
        for chunk in merged:
            while len(chunk) > max_chars:
                # 在 [max_chars 前段] 内找最靠后的软停顿标点,断在它之后
                soft = -1
                lo = max(2, max_chars // 2)  # 别断太靠前留个孤字;至少留一半
                for j in range(min(len(chunk) - 1, max_chars) - 1, lo - 2, -1):
                    if chunk[j] in "、，,":
                        soft = j + 1
                        break
                if soft >= 2:
                    cut = soft
                else:
                    # 无标点可断 → 优先断在【中文词边界】(jieba),别把词从中间切开
                    cut = 0
                    bounds = _cjk_word_bounds(chunk)
                    if bounds:
                        cand = [b for b in bounds if lo <= b <= max_chars]
                        if cand:
                            cut = max(cand)
                    if cut < 2:
                        # jieba 不可用/无合适词边界 → 退回避数字硬切
                        cut = max_chars
                        while cut > 1 and (
                            (chunk[cut - 1].isdigit() and chunk[cut].isdigit())
                            or (chunk[cut - 1] in ".:" and chunk[cut].isdigit())
                            or (chunk[cut - 1].isdigit() and chunk[cut] in ".:")
                        ):
                            cut -= 1
                        if cut <= 1:
                            cut = max_chars  # 整段都是数字的极端情况,放弃保护
                final.append(chunk[:cut])
                chunk = chunk[cut:]
            if chunk:
                final.append(chunk)
        return [chunk.strip() for chunk in final if chunk.strip()]

    @staticmethod
    def _subtitle_display_text(text: str) -> str:
        """字幕**只去逗号和句号**(半/全角),其余符号全部保留(%、?、!、:、;、、、《》等)。

        逗号(，,)和句号(。.．｡)——句读噪声去掉、屏面干净;而 %、问号、感叹号、冒号、顿号、
        书名号这些**承载语义的符号一律保留**。

        数字感知:夹在两位数字之间的 . 或 ,(小数 43.3、千分位 5,000)是分隔符,**保留**——
        否则 "43.3米" 会显示成 "433米"(数字错)。
        """
        s = str(text)
        _REMOVE = "，,。.．｡"  # 逗号 + 句号(半角/全角/日文句点)
        out: list[str] = []
        for i, ch in enumerate(s):
            if ch in _REMOVE:
                if (0 < i < len(s) - 1
                        and s[i - 1].isdigit() and s[i + 1].isdigit()):
                    out.append(ch)  # 小数/千分位分隔符,保留
                # 否则丢弃逗号/句号
            else:
                out.append(ch)  # 其他符号一律保留
        return " ".join("".join(out).split())

    @classmethod
    def _write_srt_from_char_tokens(
        cls, sentences: list[str], tokens: list[dict], srt_path: Path,
    ) -> Optional[Path]:
        grouped = cls._group_char_tokens_by_sentence(sentences, tokens)
        if grouped is None:
            return None

        srt_path.parent.mkdir(parents=True, exist_ok=True)
        _PUNCT = set("。，、；：？！…⋯·～「」『』“”‘’（）()《》〈〉【】〔〕—–~,.?!;:\"'` ")
        lines: list[str] = []
        kdata: list[dict] = []   # 逐字卡拉OK数据(每条:start/end + [字,厘秒时长])
        sub_no = 0
        for sent, sentence_tokens in zip(sentences, grouped):
            if not sent.strip() or not sentence_tokens:
                continue
            cursor = 0
            for chunk in cls._subtitle_text_chunks(sent, cls.MAX_CHARS_PER_CUE):
                chunk_len = len(chunk)
                chunk_tokens = sentence_tokens[cursor:cursor + chunk_len]
                cursor += chunk_len
                timed = [
                    t for t in chunk_tokens
                    if cls._norm_chars(str(t.get("text", "")))
                ]
                if not timed:
                    continue
                start = float(timed[0]["start"])
                end = float(timed[-1]["end"])
                if end <= start:
                    continue
                display_text = cls._subtitle_display_text(chunk)
                if not display_text:
                    continue
                sub_no += 1
                lines.append(str(sub_no))
                lines.append(f"{cls._srt_time(start)} --> {cls._srt_time(end)}")
                lines.append(display_text)
                lines.append("")
                # 逐字卡拉OK:每个显示字 + 其时长(厘秒);标点/空格时长并入前一字。
                kc: list[list] = []
                for t in chunk_tokens:
                    ch = str(t.get("text", ""))
                    try:
                        dur = max(0, int(round((float(t["end"]) - float(t["start"])) * 100)))
                    except Exception:
                        dur = 0
                    if ch and ch.strip() and ch not in _PUNCT:
                        kc.append([ch, dur])
                    elif kc:
                        kc[-1][1] += dur
                if kc:
                    kdata.append({"start": round(start, 3), "end": round(end, 3), "k": kc})
        srt_path.write_text("\n".join(lines), encoding="utf-8")
        try:
            import json as _json
            srt_path.with_suffix(".kdata.json").write_text(
                _json.dumps(kdata, ensure_ascii=False), encoding="utf-8",
            )
        except Exception:
            pass
        logger.info("SRT built from ElevenLabs char timestamps (%d cues, kdata=%d)", sub_no, len(kdata))
        return srt_path

    @staticmethod
    def _accumulate_words_into_sentences(
        sentences: list[str], tokens: list[dict],
    ) -> Optional[list[tuple[float, float]]]:
        """Word-level fallback: walk word tokens, building each sentence
        by char-accumulation. Used only on old TTS providers that emit
        WordBoundary instead of SentenceBoundary."""
        norm = PipelineService._norm_chars
        out: list[tuple[float, float]] = []
        word_idx = 0
        for sent in sentences:
            target = norm(sent)
            if not target:
                out.append((0.0, 0.0))
                continue
            consumed = ""
            first_w = None
            last_w = None
            while word_idx < len(tokens) and len(consumed) < len(target):
                w = tokens[word_idx]
                wnorm = norm(str(w.get("text", "")))
                word_idx += 1
                if not wnorm:
                    continue
                if first_w is None:
                    first_w = w
                consumed += wnorm
                last_w = w
            if first_w is None or last_w is None:
                logger.warning(
                    "word alignment ran out at sentence %r", sent[:30],
                )
                return None
            out.append((float(first_w["start"]), float(last_w["end"])))
        return out

    #    「字幕不要堆叠成两排、三排…画面上最好只保留一条（一句话）。
    #      如果内容多了，要想办法按时间拆分…注意断句的地方要通顺」
    #
    # 做法：把【每条字幕的字数】压到和【每行的字数】一样大，换行就永远用不上。
    #      拆出来的多段按字数按比例分时间（已有逻辑），
    #      断句优先走标点 → jieba 词边界 → 最后才硬切。
    #
    # ⚠️ 两个上限必须【同源】于 `MEDIA_BUDDY_SUBTITLE_PER_LINE`：
    #    写死 7 的话，以后有人把 PER_LINE 调成 5，字幕又会叠回两行。
    # ⚠️ 双语字幕不在此列：它天生就是上中下英两行（客户特意选的）。
    #
    # 关掉开关 = 回到旧行为：14 字 = 上下两行、7 字/行。
    _SUB_PER_LINE = float(os.environ.get("MEDIA_BUDDY_SUBTITLE_PER_LINE", "7") or "7")
    #
    # 一开始按「画面上只保留一条」做成了强制单行。出样张给用户看之后他改了要求:
    #    「我们要给客户一整句,但是字体不能太小,如果有必要两行那就两行,
    #      字体要客户可以调整。」
    # 要么把句子切碎、要么把字缩小 —— 两个代价用户都不接受。
    # 所以现在:**一条装一整句(最多两行)**,断句往中间的词边界切,字号仍由客户拖拉杆控制。
    # 想回强制单行:MEDIA_BUDDY_SUBTITLE_SINGLE_LINE=1。
    _SUB_SINGLE_LINE = str(
        os.environ.get("MEDIA_BUDDY_SUBTITLE_SINGLE_LINE", "0")
    ).strip().lower() not in ("0", "false", "no", "off")
    _SUB_MAX_CHARS_ENV = os.environ.get("MEDIA_BUDDY_SUBTITLE_MAX_CHARS", "").strip()
    # 不是单行模式时 = 两行的容量(2 × 每行),同样同源于 PER_LINE,不写死 14。
    MAX_CHARS_PER_CUE = max(4, int(
        _SUB_MAX_CHARS_ENV
        or str(int(_SUB_PER_LINE if _SUB_SINGLE_LINE else _SUB_PER_LINE * 2))))
    # 🚨 **开单行模式就得真的一行装得下。**
    #
    # 把代码默认值整个盖掉 —— 开关开着,每条还是 14 字、照样叠两行。
    # **功能上了生产,却是个空转的摆设**,而且一声不吭。
    #
    # 所以这里【夹一刀】:单行模式下条宽不许超过行宽,并且把冲突吼出来。
    # 显式配置本该优先,但「开了单行又配了放不下的宽度」是自相矛盾,
    # 静默失效比夹一刀更糟。要更宽的字幕就把 PER_LINE 调大(字号会跟着变小)。
    if _SUB_SINGLE_LINE and MAX_CHARS_PER_CUE > int(_SUB_PER_LINE):
        logger.warning(
            "字幕单行模式:MEDIA_BUDDY_SUBTITLE_MAX_CHARS=%s 比每行的 %d 字还宽,"
            "会叠成多行 —— 已夹到 %d。要更宽请改 MEDIA_BUDDY_SUBTITLE_PER_LINE,"
            "或设 MEDIA_BUDDY_SUBTITLE_SINGLE_LINE=0 回旧行为。",
            _SUB_MAX_CHARS_ENV or MAX_CHARS_PER_CUE, int(_SUB_PER_LINE), int(_SUB_PER_LINE),
        )
        MAX_CHARS_PER_CUE = max(4, int(_SUB_PER_LINE))
    # English cues fit far more than 14 chars (Latin is narrow); 14 would make
    # 2-word cues. Use ~42 chars/cue (≈2 lines of 21) and never split a word.
    # 英文字窄：换行预算是中文的 3 倍（和 `_wrap_cjk_for_ass` 里的 *3 一致）。
    # 14 字对英文太短（一条只能放两个词），而且永远不从词中间切。
    _SUB_MAX_CHARS_EN_ENV = os.environ.get("MEDIA_BUDDY_SUBTITLE_MAX_CHARS_EN", "").strip()
    # 🚨 英文两行的上限**不能直接算 2×每行**。英文按【整词】打包,
    #    一行 21 字的预算实际只装得下 16~19 字(最后一个词放不下就换行)。
    MAX_CHARS_PER_CUE_EN = max(16, int(
        _SUB_MAX_CHARS_EN_ENV
        or str(int(max(18.0, _SUB_PER_LINE * 3.0) * (1 if _SUB_SINGLE_LINE else 1.7)))))
    # 英文同理:换行预算是中文的 3 倍(和 `_wrap_cjk_for_ass` 里的 *3 一致)。
    if _SUB_SINGLE_LINE and MAX_CHARS_PER_CUE_EN > int(_SUB_PER_LINE * 3.0):
        logger.warning(
            "字幕单行模式(英文):MEDIA_BUDDY_SUBTITLE_MAX_CHARS_EN=%s 比每行的 %d 还宽,"
            "已夹到 %d。",
            _SUB_MAX_CHARS_EN_ENV or MAX_CHARS_PER_CUE_EN,
            int(_SUB_PER_LINE * 3.0), int(_SUB_PER_LINE * 3.0),
        )
        MAX_CHARS_PER_CUE_EN = max(16, int(_SUB_PER_LINE * 3.0))

    @staticmethod
    def _is_latin_caption(text: str) -> bool:
        """Treat a caption as English/Latin (word-pack, never split mid-word) ONLY
        when it has a space AND no CJK ideograph. A mostly-Chinese line with an
        embedded English term/acronym (e.g. "苹果 CEO 库克说") still has a space but
        contains CJK → keep the CJK logic (which already handles Latin runs at half
        width), so we don't misroute it into English word-packing."""
        s = str(text)
        if " " not in s:
            return False
        return not any(
            (0x3400 <= ord(ch) <= 0x9FFF) or (0xF900 <= ord(ch) <= 0xFAFF)
            for ch in s
        )

    @staticmethod
    def _split_caption_en(text: str) -> list[str]:
        """Word-aware caption chunking for space-delimited (English) text: pack
        whole words into cues of <= MAX_CHARS_PER_CUE_EN chars, NEVER splitting a
        word mid-way (a single over-long word is kept whole)."""
        eff = PipelineService.MAX_CHARS_PER_CUE_EN
        chunks: list[str] = []
        cur = ""
        for w in str(text).split():
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= eff:
                cur += " " + w
            else:
                chunks.append(cur)
                cur = w
        if cur:
            chunks.append(cur)
        return chunks

    # ── 字幕语言(可独立于配音/输出语言)──────────────────────────
    # subtitle_language ∈ {zh-Hans, zh-Hant, en, zh-Hans+en, zh-Hant+en}。
    # 空 → 跟随 output_language(zh→zh-Hans, en→en),旧项目行为逐字节不变。
    @staticmethod
    def _resolve_subtitle_language(project_data: dict, output_language: str) -> "tuple[str, Optional[str]]":
        raw = str((project_data or {}).get("subtitle_language") or "").strip()
        if not raw:
            raw = "zh-Hans" if str(output_language or "zh").lower() == "zh" else "en"
        if "+" in raw:
            primary, secondary = raw.split("+", 1)
            return (primary.strip() or "zh-Hans"), (secondary.strip() or None)
        return raw, None

    @staticmethod
    def _subtitle_lang_matches_narration(target: str, narration_lang: str) -> bool:
        """True when subtitle text == spoken text, so no convert/translate is needed:
        Simplified subtitle over a Chinese narration, or English over English."""
        n = str(narration_lang or "zh").lower()
        return (target == "zh-Hans" and n == "zh") or (target == "en" and n == "en")

    def _localize_caption_texts(self, texts: list, narration_lang: str, target: str) -> list:
        """Turn caption strings (in the narration language) into ``target`` — order and
        length preserved. Simplified-over-Chinese / English-over-English = passthrough;
        Traditional = OpenCC (offline); cross-language = ONE batched LLM call. Never
        raises — degrades to the input text."""
        items = list(texts or [])
        if not items:
            return items
        n = str(narration_lang or "zh").lower()
        if target == "en":
            return items if n == "en" else self.llm.translate_segments(items, "en")
        # target is Simplified or Traditional Chinese
        base = items if n == "zh" else self.llm.translate_segments(items, "zh")
        if target == "zh-Hant":
            try:
                from backend.lib import zh_convert
                base = zh_convert.to_traditional_batch(base)
            except Exception:
                logger.warning("zh_convert failed; Traditional subtitle kept Simplified", exc_info=True)
        return base

    def _localize_srt_file(self, srt_path: Path, narration_lang: str, target: str) -> None:
        """In-place rewrite a single-language SRT into ``target`` — same cue count and
        timestamps, only the text changes. Used when the subtitle language differs from
        the narration and there is no second (bilingual) line."""
        import re as _re
        try:
            raw = Path(srt_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        blocks = _re.split(r"\r?\n\r?\n", raw.strip())
        parsed: list = []          # (header_lines, text)
        texts: list = []
        for block in blocks:
            nonblank = [ln for ln in block.splitlines() if ln.strip()]
            if len(nonblank) < 2:
                parsed.append((nonblank, ""))
                continue
            ti = 1 if _re.fullmatch(r"\d+", nonblank[0].strip()) else 0
            header = nonblank[:ti + 1]
            text = " ".join(nonblank[ti + 1:]).strip()
            parsed.append((header, text))
            texts.append(text)
        if not texts:
            return
        loc = self._localize_caption_texts(texts, narration_lang, target)
        out: list = []
        li = 0
        for header, text in parsed:
            if not text:
                continue
            new_text = loc[li] if li < len(loc) else text
            li += 1
            out.extend(header)
            out.append(new_text)
            out.append("")
        try:
            Path(srt_path).write_text("\n".join(out), encoding="utf-8")
        except Exception:
            logger.warning("localized SRT write failed; keeping original", exc_info=True)

    def _sentence_windows(self, sentences: list, narration_path: Optional[Path], cuts: list):
        """Return ``[(start, end, sentence_text)]`` — COHERENT (punctuated) sentence text
        with real per-sentence timing. Text comes from ``sentences`` (the script's display
        sentences); the sidecar only supplies timing. Timing source, in order:
          1. Edge-style sentence-boundary tokens (carry their own text + time), else
          2. Qwen-style char tokens grouped by sentence (Qwen writes ONLY char tokens —
          3. clip-driven fallback (each cut's visual duration → its sentence).
        Returns ``None`` when nothing usable is found."""
        import json as _json
        sents = [str(s or "").strip() for s in (sentences or [])]
        windows: list = []
        if narration_path is not None:
            sidecar = Path(narration_path).with_suffix(".words.json")
            if sidecar.exists():
                tokens = None
                try:
                    tokens = _json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    tokens = None
                if isinstance(tokens, list):
                    sent_toks = [
                        t for t in tokens if isinstance(t, dict)
                        and t.get("kind") == "sentence" and str(t.get("text") or "").strip()
                    ]
                    if sent_toks:
                        prev = 0.0
                        for t in sent_toks:
                            try:
                                s = max(float(t.get("start", prev)), prev)
                                e = float(t.get("end", s))
                            except (TypeError, ValueError):
                                continue
                            if e <= s:
                                e = s + 0.4
                            prev = e
                            windows.append((s, e, str(t.get("text")).strip()))
                        if windows:
                            return windows
                    char_tokens = self._read_char_tokens(sidecar)
                    if char_tokens and sents:
                        char_tokens = self._realign_char_tokens_to_display(
                            sents, char_tokens,
                        ) or char_tokens
                        grouped = self._group_char_tokens_by_sentence(sents, char_tokens)
                        if grouped:
                            for sent, toks in zip(sents, grouped):
                                if not sent or not toks:
                                    continue
                                try:
                                    s = float(toks[0].get("start"))
                                    e = float(toks[-1].get("end"))
                                except (TypeError, ValueError):
                                    continue
                                if e <= s:
                                    e = s + 0.4
                                windows.append((s, e, sent))
                            if windows:
                                return windows
        cursor = 0.0
        for clip in (cuts or []):
            try:
                dur = float(clip["out_seconds"]) - float(clip["in_seconds"])
            except Exception:
                continue
            si = clip.get("scene_index")
            sent = sents[si] if isinstance(si, int) and 0 <= si < len(sents) else None
            if sent:
                windows.append((cursor, cursor + dur, sent))
            cursor += dur
        return windows or None

    @staticmethod
    def _split_at_punctuation_timed(text: str, start: float, end: float) -> list:
        """Split a sentence into COHERENT clauses at major punctuation only (never
        mid-word / mid-phrase), apportioning ``[start,end]`` by clause char length.
        No hard char cap — long clauses stay whole and wrap on screen, so Chinese and
        English break at the SAME semantic boundary. Returns ``[(clause, s, e)]``."""
        s = str(text or "").strip()
        if not s:
            return []
        # 顿号 、 不断句(保住「眼镜蛇、狮子、鬣狗」这类枚举整体);只在主要子句标点断。
        breakers = "，。！？；,.!?;"
        pieces: list = []
        buf = ""
        for ch in s:
            buf += ch
            if ch in breakers:
                pieces.append(buf)
                buf = ""
        if buf.strip():
            pieces.append(buf)
        pieces = [p for p in pieces if p.strip()]
        if not pieces:
            return [(s, start, end)]
        total = sum(len(p) for p in pieces) or 1
        span = max(0.0, float(end) - float(start))
        out: list = []
        acc = 0
        for p in pieces:
            cs = start + span * (acc / total)
            acc += len(p)
            ce = start + span * (acc / total)
            if ce <= cs:
                ce = cs + 0.3
            out.append((p.strip(), cs, ce))
        return out

    def _build_bilingual_pairs(
        self, sentences: list, narration_path: Optional[Path], cuts: list,
        narration_lang: str, primary: str, secondary: str,
    ) -> Optional[list]:
        """Build ``[{start,end,primary,secondary}]`` cues for bilingual subtitles.

        Splits each COHERENT sentence into clauses at punctuation (never mid-phrase),
        translates each clause to the primary + secondary language so Chinese and
        English break at the SAME boundary, and apportions time within the sentence.
        Returns ``None`` when no timing/text is available → caller falls back to
        single-language."""
        windows = self._sentence_windows(sentences, narration_path, cuts)
        if not windows:
            return None
        block_texts: list = []
        block_times: list = []
        for s, e, sent in windows:
            for clause, cs, ce in self._split_at_punctuation_timed(sent, s, e):
                if clause.strip():
                    block_texts.append(clause.strip())
                    block_times.append((cs, ce))
        if not block_texts:
            return None
        primary_raw = self._localize_caption_texts(block_texts, narration_lang, primary)
        secondary_raw = self._localize_caption_texts(block_texts, narration_lang, secondary)

        def _disp(txt: str, lang: str) -> str:
            return self._subtitle_display_text(txt) if str(lang).startswith("zh") else str(txt).strip()

        pairs: list = []
        for i, (cs, ce) in enumerate(block_times):
            p = _disp(primary_raw[i] if i < len(primary_raw) else block_texts[i], primary)
            q = _disp(secondary_raw[i] if i < len(secondary_raw) else block_texts[i], secondary)
            if not p and not q:
                continue
            pairs.append({"start": cs, "end": ce, "primary": p, "secondary": q})
        return pairs or None

    def _build_localized_srt(
        self, sentences: list, narration_path: Optional[Path], cuts: list,
        srt_path: Path, narration_lang: str, target: str,
    ) -> bool:
        """Rebuild ``subtitles.srt`` in ``target`` by translating WHOLE coherent
        sentences (reliable, count-stable), then splitting each translation into width
        cues with apportioned time. Returns True on success; False when no windows are
        available (caller falls back to per-cue ``_localize_srt_file``)."""
        windows = self._sentence_windows(sentences, narration_path, cuts)
        if not windows:
            return False
        texts = [w[2] for w in windows]
        loc = self._localize_caption_texts(texts, narration_lang, target)
        lines: list[str] = []
        n = 0
        for (s, e, _sent), translated in zip(windows, loc):
            text = str(translated or "").strip()
            if not text:
                continue
            for sub, cs, ce in self._split_long_sentence(text, s, e, self.MAX_CHARS_PER_CUE):
                sub = str(sub or "").strip()
                if not sub:
                    continue
                n += 1
                lines.append(str(n))
                lines.append(f"{self._srt_time(cs)} --> {self._srt_time(ce)}")
                lines.append(sub)
                lines.append("")
        if n == 0:
            return False
        try:
            Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
            return True
        except Exception:
            logger.warning("localized SRT rebuild write failed; keeping original", exc_info=True)
            return False

    def _localize_single_subtitle(
        self, srt_path: Path, sentences: list, narration_path: Optional[Path], cuts: list,
        narration_lang: str, target: str,
    ) -> None:
        """Localize a single-language subtitle track to ``target``. Prefers the
        coherent sentence rebuild; falls back to per-cue rewrite of the existing SRT."""
        if not self._build_localized_srt(sentences, narration_path, cuts, srt_path, narration_lang, target):
            self._localize_srt_file(srt_path, narration_lang, target)

    @staticmethod
    def _pairs_to_ass(
        pairs: list, ass_path: Path, *, width: int, height: int,
        main_font: int, sub_font: int, outline: int,
        margin_v: int, margin_l: int, margin_r: int,
        main_per_line: float = 9.0, sub_per_line: float = 9.0,
    ) -> bool:
        """Bilingual ASS: each cue = big primary line(s) + smaller secondary line(s).
        Same PlayRes/pixel-honest approach as ``_srt_to_ass``; the Default style holds
        the primary size, the secondary line drops to ``sub_font`` via an inline
        ``\\fs`` override. Both lines are white with a black outline, bottom-centered."""
        if not pairs:
            return False
        shadow = max(1, outline // 3)

        def _t(sec: float) -> str:
            cs = int(round(float(sec) * 100))
            return f"{cs // 360000:d}:{(cs % 360000) // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}"

        ass_lines = [
            "[Script Info]", "ScriptType: v4.00+", "Collisions: Normal",
            f"PlayResX: {width}", f"PlayResY: {height}",
            "ScaledBorderAndShadow: yes", "WrapStyle: 0", "",
            "[V4+ Styles]",
            ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
             "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
             "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding"),
            (f"Style: Default,{_SUBTITLE_FONT},{main_font},"
             "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
             "-1,0,0,0,100,100,0,0,1,"
             f"{outline},{shadow},2,{margin_l},{margin_r},{margin_v},1"),
            "", "[Events]",
            ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"),
        ]
        n = 0
        for cue in pairs:
            try:
                start = _t(cue["start"]); end = _t(cue["end"])
            except Exception:
                continue
            # strip braces so caption text can't inject ASS override tags
            primary = str(cue.get("primary") or "").strip().replace("{", "").replace("}", "")
            secondary = str(cue.get("secondary") or "").strip().replace("{", "").replace("}", "")
            parts = []
            if primary:
                parts.append(PipelineService._wrap_cjk_for_ass(primary, main_per_line))
            if secondary:
                parts.append("{\\fs%d}%s" % (sub_font, PipelineService._wrap_cjk_for_ass(secondary, sub_per_line)))
            if not parts:
                continue
            body = r"\N".join(parts)
            ass_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}")
            n += 1
        if n == 0:
            return False
        try:
            Path(ass_path).write_text("\n".join(ass_lines), encoding="utf-8")
            return True
        except Exception:
            return False

    @classmethod
    def _write_srt_from_aligned(
        cls, sentences: list[str],
        aligned: list[tuple[float, float]],
        srt_path: Path,
    ) -> Optional[Path]:
        """Emit SRT from per-sentence aligned (start, end) times.

        Long sentences are auto-split into multiple cues to keep each
        cue ≤2 lines on screen. Time is apportioned by character count
        across the sub-cues so they still sync to the spoken audio."""
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        sub_no = 0
        for sent, (start, end) in zip(sentences, aligned):
            if not sent.strip() or end <= start:
                continue
            for sub_text, sub_start, sub_end in cls._split_long_sentence(
                sent, start, end, cls.MAX_CHARS_PER_CUE,
            ):
                display_text = cls._subtitle_display_text(sub_text)
                if not display_text:
                    continue
                sub_no += 1
                lines.append(str(sub_no))
                lines.append(
                    f"{cls._srt_time(sub_start)} --> {cls._srt_time(sub_end)}"
                )
                lines.append(display_text)
                lines.append("")
        srt_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("SRT built from word-aligned timestamps (%d cues)", sub_no)
        return srt_path

    @staticmethod
    def _split_long_sentence(
        sent: str, start: float, end: float, max_chars: int,
    ) -> list[tuple[str, float, float]]:
        """Split ``sent`` into ≤ max_chars chunks at CJK/ASCII punctuation.

        Time is divided proportionally to each chunk's char count. Returns
        list of ``(text, start, end)``. Short-sentence fast path returns
        the original tuple unchanged."""
        s = sent.strip()
        if PipelineService._is_latin_caption(s):
            # English/Latin: pack WHOLE words to the wider EN width
            # (MAX_CHARS_PER_CUE_EN ≈ 42), never chopping a word. The CJK
            # hard-split below counts raw chars and would butcher English
            # ("navigation" → "naviga"+"tion") — that was the severe破句 bug,
            # because this builder was called with the 14-char CJK cap.
            final = PipelineService._split_caption_en(s) or [s]
        else:
            if len(s) <= max_chars:
                return [(s, start, end)]

            # Step 1: pre-split at every punctuation char (keep punct attached
            # to its preceding piece for readable cuts).
            breakers = "，。！？、；：,.!?;:"
            pieces: list[str] = []
            buf = ""
            for ch in s:
                buf += ch
                if ch in breakers:
                    pieces.append(buf)
                    buf = ""
            if buf:
                pieces.append(buf)

            # Step 2: greedy-merge adjacent pieces while keeping ≤ max_chars.
            merged: list[str] = []
            cur = ""
            for p in pieces:
                if not cur:
                    cur = p
                elif len(cur) + len(p) <= max_chars:
                    cur += p
                else:
                    merged.append(cur)
                    cur = p
            if cur:
                merged.append(cur)

            # Step 3: 超长片段要断,但【优先软停顿标点 → 中文词边界(jieba) → 兜底硬切】,
            # 不再盲切 c[:max_chars](那会把"数字/载荷"从中间切开)。与 _subtitle_text_chunks 一致。
            #
            # 旧写法在 [max/2, max] 里取**最靠后**的边界 —— 一句 18 字、上限 14,
            # 就切成「帝国大厦每年被闪电击中大约」+「二十五次,」:前一条撑满、
            # 后一条只剩几个字,而且断在「大约|二十五次」这种词组中间。
            #
            # 改成找**最靠近正中间**的边界:同样两条,现在是
            # 「帝国大厦每年被闪电击中」+「大约二十五次,」—— 两条都读得通。
            # 优先级不变:软停顿标点 → 中文词边界(jieba) → 兜底硬切。
            final = []
            for c in merged:
                while len(c) > max_chars:
                    # 规则:**在不切碎尾巴的前提下,取最靠后的词边界**。
                    #   取最靠后 → 前一条尽量完整(「帝国大厦每年被闪电击中」)
                    #   留住尾巴 → 后一条不会只剩一两个字(「,」这种)
                    # 只往中间切也不好:会切成「帝国大厦每年被闪电」+「击中…」,
                    # 把「被闪电击中」这个动词短语劈开。
                    #   贪到撑满(旧写法)→「…大约」+「二十五次,」,把词组劈开
                    #   切正中间     →「…被闪电」+「击中…」,把动词短语劈开
                    lo = max(2, max_chars // 3)                 # 别切出个孤零零的头
                    hi = min(len(c) - 1, max_chars)
                    target = min(float(max_chars), len(c) * 0.62)
                    def _near(cands):
                        cands = [b for b in cands if lo <= b <= hi
                                 and not _cuts_a_number(c, b)
                                 and not _dangling_tail(c, b)]
                        return min(cands, key=lambda b: abs(b - target)) if cands else -1
                    # ① 软停顿标点(切在它后面)
                    cut = _near([j + 1 for j in range(len(c) - 1) if c[j] in "、，,"])
                    # ② 中文词边界
                    if cut < 2:
                        cut = _near(_cjk_word_bounds(c) or [])
                    # ③ 空格(英文/混排)
                    if cut < 2:
                        cut = _near([j + 1 for j in range(len(c) - 1) if c[j] == " "])
                    if cut < 2:
                        cut = max_chars                  # 实在没边界 → 兜底硬切
                    final.append(c[:cut])
                    c = c[cut:]
                if c:
                    final.append(c)

        if not final:
            return [(s, start, end)]
        if len(final) == 1:
            return [(final[0].strip(), start, end)]

        # Step 4: apportion time by char count
        total = float(end - start)
        char_total = sum(len(c) for c in final) or 1
        out: list[tuple[str, float, float]] = []
        cursor = start
        for i, c in enumerate(final):
            if i == len(final) - 1:
                seg_end = end
            else:
                seg_end = cursor + total * (len(c) / char_total)
            out.append((c.strip(), cursor, seg_end))
            cursor = seg_end
        return out
