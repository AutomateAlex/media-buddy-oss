"""TTS service — Edge TTS + native OpenAI / ElevenLabs.

Phase 2.11f — fully decoupled from OpenMontage. Native OpenAI and
ElevenLabs HTTP clients replaced the OM tool-registry fallback path.

Phase 2.11g — removed Piper TTS. The ``piper-tts`` PyPI package is
GPL-3.0-or-later (espeak-ng dependency), which would force the entire
product to GPL. Edge TTS covers the same offline-friendly + free + CJK
requirements without any copyleft contamination.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from backend.services import voice_registry as _registry

logger = logging.getLogger(__name__)

# ElevenLabs v3 is the product default. It supports expressive audio tags
# such as [thoughtful], [excited], and [whispering].
ELEVENLABS_MODEL_ID = os.environ.get("MEDIA_BUDDY_ELEVENLABS_MODEL_ID", "eleven_v3")

# Fallback order when the requested provider is unavailable.
FALLBACK_CHAIN = ["azure_yunyang"]  # last resort: this Microsoft voice, rendered by free Edge TTS (no key)

# Edge TTS voice catalogue exposed in legacy/internal callers.
# Keys are the provider strings sent from the frontend; values are MS Neural voice IDs.
EDGE_VOICES: dict[str, str] = {
    "edge_xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "edge_xiaoyi": "zh-CN-XiaoyiNeural",
    "edge_yunyang": "zh-CN-YunyangNeural",
    "edge_yunjian": "zh-CN-YunjianNeural",
    "edge_yunxi": "zh-CN-YunxiNeural",
    "edge_ava": "en-US-AvaNeural",
    "edge_andrew": "en-US-AndrewNeural",
}

# 从 `voice_registry` 派生 —— **别在这里维护名单**。
#    「必须保持一致」人肉同步,漏一处就是「客户选了 A、出来是 B」。
#    加音色请改 `services/voice_registry.py`,这里自动跟随。
AZURE_VOICES = _registry._native_map('azure')

# voice IDs. The set must stay in sync with ALLOWED_VOICE_IDS in
# the voice registry. Adding a
# voice in one place without the other will hit 400 voice_not_allowed.
# 从 `voice_registry` 派生 —— **别在这里维护名单**。
#    「必须保持一致」人肉同步,漏一处就是「客户选了 A、出来是 B」。
#    加音色请改 `services/voice_registry.py`,这里自动跟随。
ELEVENLABS_VOICES = _registry._native_map('elevenlabs')

ELEVENLABS_PROVIDER_ALIASES: dict[str, str] = {}  # legacy aliases are handled by voice_registry.substitute_legacy


@dataclass
class TTSResult:
    success: bool
    output_path: Optional[str] = None
    error: Optional[str] = None


# ── 千问 TTS(qwen3-tts-instruct-flash · DashScope 多模态生成)──────────────
# 语速由 instructions 预设控制(生成式,非逐帧精确);词级时间码 Phase2 走 ASR,
# Phase1 先用段内均匀字符时间码(_even_char_tokens),字幕够用。
QWEN_TTS_PRESETS: dict[str, str] = {
    "normal":   "自然亲切的中文讲解，正常语速，吐字清楚，停顿自然。",
    "fast":     "自然有活力的中文讲解，语速约为正常速度的1.35倍，吐字清楚，不要拖音。",
    "fast_175": "语速约为正常速度的1.75倍，节奏紧凑，减少无意义停顿，但必须保持吐字清晰。",
    "fast_200": "语速约为正常速度的2倍，节奏非常紧凑，停顿短，仍需清楚读出每个词。",
}


def _qwen_tts_enabled() -> bool:
    """千问 TTS 是否作为生产默认(默认 on;设 0 回退旧 Azure/ElevenLabs 链路)。"""
    return os.environ.get("MEDIA_BUDDY_TTS_QWEN", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


# 客户可选的音色。key = 我们的 provider_key,value = (languageCode, Gemini 音色名)。
#
# 🚨 provider_key **绝不能出现任何供应商名字** —— 它会进试听请求的 URL
#    (`/api/tts/preview?provider=...`),客户按 F12 就看得见。
#    `hdv` = HD Voice,中性。`synthesize()` 靠**查这张表**分流,不靠前缀。
# ⚠️ 同一个音色名配不同 languageCode 会说不同口音,所以 key 里要带 `us`/`gb` 区分,
#    否则两组会撞成一个。
# 从 `voice_registry` 派生 —— **别在这里维护名单**。
#    「必须保持一致」人肉同步,漏一处就是「客户选了 A、出来是 B」。
#    加音色请改 `services/voice_registry.py`,这里自动跟随。

# 模型链:**从左到右试**,前面的挂了/被限流就落到后面。
# ⚠️ 顺序有意义:3.1(效果最好)在前,2.5(便宜且更稳)在后兜底。别对调。
#
#
#     Cloud TTS(我们走这条)          Gemini Developer API
#     ─────────────────────          ────────────────────
#     gemini-2.5-flash-tts           gemini-2.5-flash-preview-tts
#     gemini-2.5-pro-tts             gemini-2.5-pro-preview-tts
#     gemini-3.1-flash-tts-preview   gemini-3.1-flash-tts-preview   ← 只有这个同名
#
#    写错**只有兜底档会 404**,主用档一切正常,平时根本发现不了 ——
#    等到真需要兜底那天才炸。

# Cloud TTS 合成端点。


# 服务账号令牌缓存。
#
# 🚨 **必须缓存** —— 令牌有效期 1 小时,而每段旁白都要发一次请求。
#    每次重新签发 = 每条片子多几十次多余的网络往返 + 可能被 Google 限流。
# ⚠️ 提前 5 分钟过期,别卡在边界上用一个正在失效的令牌。
_GOOGLE_TOKEN_CACHE: dict[str, Any] = {"token": "", "exp": 0.0}


def _qwen_preset_for_speed(speed: float) -> str:
    s = float(speed or 1.0)
    if s <= 1.05:
        return "normal"
    if s <= 1.5:
        return "fast"
    if s <= 1.85:
        return "fast_175"
    return "fast_200"


def _qwen_tts_base() -> str:
    """TTS 用 DashScope 原生 /api/v1(不是 LLM 的 /compatible-mode/v1)。
    优先 env MEDIA_BUDDY_QWEN_TTS_BASE;否则从 MEDIA_BUDDY_QWEN_BASE_URL 派生。"""
    b = os.environ.get("MEDIA_BUDDY_QWEN_TTS_BASE", "").strip().rstrip("/")
    if b:
        return b
    src = os.environ.get("MEDIA_BUDDY_QWEN_BASE_URL", "").strip().rstrip("/")
    if src.endswith("/compatible-mode/v1"):
        return src[: -len("/compatible-mode/v1")] + "/api/v1"
    if src.endswith("/api/v1"):
        return src
    return "https://dashscope-intl.aliyuncs.com/api/v1"


def _qwen_language(text: str) -> str:
    """中文内容用 Chinese(方案明确不用 Auto);英文为主则 English。"""
    import re as _re
    cjk = len(_re.findall(r"[㐀-鿿]", text or ""))
    latin = len(_re.findall(r"[A-Za-z]", text or ""))
    return "Chinese" if cjk >= latin else "English"


# 用户在 UI 选的 Azure 中文音色 → 千问音色(方案4音色:Cherry阳光女/Serena温柔女/
# Ethan清朗男/Chelsie软萌女)。命名规律:azure_yun*=云*=男声、azure_xiao*=晓*=女声。
_AZURE_TO_QWEN_VOICE: dict[str, str] = {
    "azure_xiaoxiao": "Serena",   # 晓晓 温暖女声
    "azure_xiaoyi":   "Cherry",   # 晓伊 年轻活泼女声
    "azure_yunyang":  "Ethan",    # 云扬 深沉专业男声
    "azure_yunjian":  "Ethan",    # 云健 力量感男声
}


_SEGMENT_MIN_RATIO = 0.80
# 偏短时重合成几次。千问约 9 成调用是好的,一次重试基本就能拿到完整的。
_SEGMENT_MAX_RETRIES = 2


def _segment_duration_ok(duration: float, text: str, provider: str, speed,
                        lang: str | None = None) -> bool:
    """这段音频的长度对得上它的文本吗?

    「文本送进去了、计了费,声音却没出来」是**静默的内容丢失** ——
    千问照样返回 finish_reason="stop" 和正常音频 URL,而我们原来只检查这两样。


    任何异常一律返回 True(放行)—— 校验永远不许把出片搞挂。
    """
    if os.environ.get("MB_TTS_SEGMENT_CHECK", "1") == "0":
        return True
    try:
        from backend.lib import tts_pacing as _tp
        from backend.lib.lang_profile import profile_for

        #    中英文的语速表是两个量纲(中文按字/秒、英文按词/秒)。猜反 → 拿错表 →
        #    好音频被判成「太短」白白重合成一遍(日志里那条
        lang = str(lang or "").strip().lower()
        if lang not in ("zh", "en"):
            lang = "en" if _qwen_language(text) == "English" else "zh"
        if lang == "en":
            _rate, is_fallback = _tp.english_words_per_second(provider)
            if is_fallback:
                return True
        elif not _tp.is_measured(provider):
            return True
        prof = profile_for(lang)
        units = prof.count(text)
        if units <= 0:
            return True
        expected = prof.estimate(units, provider, speed)
        if expected <= 0:
            return True
        return float(duration or 0.0) >= expected * _SEGMENT_MIN_RATIO
    except Exception:  # noqa: BLE001 — 校验失败照放行
        logger.warning("segment duration check failed; accepting audio", exc_info=True)
        return True


def _qwen_atempo_filter(speed: float) -> Optional[str]:
    """
    atempo 保音高、单滤镜支持 0.5–2.0,>2.0 串联。speed≈1.0 返回 None(不加滤镜)。"""
    s = max(0.5, min(3.0, float(speed or 1.0)))
    if abs(s - 1.0) < 0.02:
        return None
    if s <= 2.0:
        return "atempo=%.4f" % s
    return "atempo=2.0,atempo=%.4f" % (s / 2.0)


# 千问 MVP 4 音色(方案第8节)——前端直接显示这4个,provider_key=qwen_<name>。
# provider_key = "qwen_" + 音色ID小写(空格→下划线);值 = DashScope 认的音色 ID。
# 前端展示目录见 tts.py `_QWEN_ZH_CATALOG` 与 frontend src/data/qwenVoices.ts(三处须一致)。
# 从 `voice_registry` 派生 —— **别在这里维护名单**。
#    「必须保持一致」人肉同步,漏一处就是「客户选了 A、出来是 B」。
#    加音色请改 `services/voice_registry.py`,这里自动跟随。
QWEN_VOICES = _registry._native_map('qwen')


def _qwen_voice_for(provider: Optional[str], voice_id: Optional[str] = None) -> str:
    """把用户选的音色(provider)映射到千问音色,honor 用户的音色选择。"""
    p = str(provider or "").lower()
    if p in QWEN_VOICES:            # 直接的千问音色 key
        return QWEN_VOICES[p]
    if p in _AZURE_TO_QWEN_VOICE:   # 旧 Azure key → 千问(向后兼容旧项目)
        return _AZURE_TO_QWEN_VOICE[p]
    base = p.replace("azure_tw_", "").replace("azure_hk_", "").replace("azure_", "")
    if base.startswith("yun"):
        return "Ethan"   # 云* 男声
    if base.startswith(("xiao", "hsiao", "hiu")):
        return "Cherry"  # 晓* 女声
    return os.environ.get("MEDIA_BUDDY_QWEN_TTS_VOICE", "Cherry").strip() or "Cherry"


def _qwen_pacing_key(voice: Optional[str]) -> str:
    """


    段完整性校验(`_segment_duration_ok`)原来直接拿**客户选的 provider**
    去查配速表。但客户选的可能是**旧的 Azure key**(`azure_yunyang`),
    真正发声的是 Ethan。于是:

        量的尺子  azure_yunyang  4.78 字/秒 → 241 字要 36.0s,阈值 28.8s
        实际发声  qwen_ethan     5.03 字/秒 → 241 字要 34.2s,阈值 27.4s
        实际音频                              27.8s

    27.8 卡在两把尺子中间 → **每一段都被判「太短」白白重合成**。

    这修的不只是假阳性,还有更危险的**假阴性**:
    `is_measured()` 判 False → 校验**整个跳过**。千问真吞了内容也照样放行。
    换成音色 key 之后这条路第一次真的查得动。

    ## 为什么不用反查字典

    `"qwen_" + 音色名小写、空格换下划线` 精确复现 `QWEN_VOICES` 的全部 key
    (含 `Eldric Sage` → `qwen_eldric_sage`)。多一张要跟着维护的表
    = 多一个会忘记同步的地方。

    ⚠️ 认不出来的音色(env 覆盖的、千问新出的)落在 `startswith("qwen")`
       分支拿 `QWEN_DEFAULT_RATE_1X`(4.5,已测音色的中位附近)—— 仍然安全。
    """
    v = str(voice or "").strip().lower().replace(" ", "_")
    return f"qwen_{v}" if v else "qwen_cherry"


class TTSService:
    def __init__(self) -> None:
        self.project_id: Optional[str] = None
        # 这条片子【设定】的出片语言("zh"/"en")。强制对齐要按它告诉 whisper 听哪种话。
        # None = 调用方没给 → 退回按文本猜(见 `_forced_align_tokens`)。
        self.output_language: Optional[str] = None

    def set_project_id(self, project_id: Optional[str]) -> None:
        self.project_id = project_id

    def synthesize(
        self,
        text: str,
        output_path: str,
        provider: str = "azure_yunyang",
        voice_id: Optional[str] = None,
        model: str = "en_US-lessac-medium",
        speed: float = 1.0,
        align_words: bool = True,
        output_language: Optional[str] = None,
    ) -> TTSResult:
        # 逐字强制对齐(faster-whisper)开关。**关字幕时由调用方传 False** ——
        # 字幕不出了,唯一还吃真毫秒时间轴的只剩「画面跟着语音切镜头」,
        # 那个退回估算即可(误差 1~3s,没字幕时看不出来),而 whisper 短片要 13~18s、
        # ⚠️ 存实例属性而非全参数透传:provider 分支多,逐个加参数容易漏一条。
        #    worker 每任务新建 PipelineService(内含独立 TTSService),没有跨任务串台风险。
        self._align_words = bool(align_words)
        # 这条片子【设定】的出片语言。强制对齐要按它告诉 whisper 听哪种话。
        # 每次调用都重设,不让上一条片子的值留下来。
        self.output_language = (str(output_language).strip().lower()
                                if output_language else None)
        # 转成中文读法后再合成 —— 只影响音频发音,不改脚本/字幕的文本来源。
        import re as _re
        text = str(text or "")
        # 繁体→简体(送 TTS 前统一)：繁体字喂给大陆生成式音色(qwen3-tts)会让模型漂口音/
        # **只影响【配音发音】**——字幕从简体基线再按 subtitle_language 转繁/简(见 zh_convert)。
        # env MEDIA_BUDDY_TTS_FORCE_SIMPLIFIED=0 可关(默认开);OpenCC 缺失时优雅降级为原文。
        if os.environ.get("MEDIA_BUDDY_TTS_FORCE_SIMPLIFIED", "1").strip().lower() not in (
            "0", "false", "no", "off",
        ):
            try:
                from backend.lib.zh_convert import to_simplified
                text = to_simplified(text)
            except Exception:  # noqa: BLE001 — 简繁转换绝不许挡出片
                logger.warning("TTS simplified normalization failed; using original text", exc_info=True)
        text = _re.sub(r"(\d+(?:\.\d+)?)\s*%", r"百分之\1", text)
        # 年份逐字读:qwen TTS 有时把 "1749年" 读成 "十七四十九年"(4位数当基数拆)。
        # 送 TTS 前把明确年份转成 "一七四九年" → 读法变确定;字幕侧 _denormalize_srt_display
        # 再还原回数字。年份等长替换(4数字→4中文字),不影响字幕时间轴。
        try:
            from backend.lib.tts_text_norm import normalize_years_for_tts
            text = normalize_years_for_tts(text)
        except Exception:  # noqa: BLE001 — 归一化失败不许挡出片
            pass
        provider = ELEVENLABS_PROVIDER_ALIASES.get(str(provider or ""), str(provider or ""))
        # ── 🚨 停用供应商的自动接管。**必须在这里,不能更晚。** ──────────
        #
        # 客户的配音设定是**存下来的**。他几个月前选了 `elevenlabs_adam`,
        # 这个值就一直躺在 `projects.tts_provider` 里 —— 我们停用 ElevenLabs
        # 和 Azure 之后,**存量设定不会自己变**。
        #
        # 于是出片走进一条死链(下面几十行就是那条链):
        #
        #     ElevenLabs 音色 → `_wants_elevenlabs=True` → **跳过千问、跳过谷歌**
        #
        # 最阴的是那句「跳过千问、跳过谷歌」:它当初是为了防「安慰剂开关」
        # (客户选了高级英语却听到千问),现在却变成**主动绕开唯一还活着的两条路**。
        #
        #
        # 🚨 **必须在 `_wants_elevenlabs` 之前接管** —— 放到后面就等于没放,
        #    因为那个标志一旦为真,整条活路就已经被绕过去了。
        #
        # ⚠️ 接管**只在合成这一刻生效,不改客户存的值**:
        #    · 万一哪天供应商恢复,原值还在,不用逐条改回来;
        #    · 改存量数据是另一件事,要单独问客户。
        try:
            from backend.services.voice_registry import replacement_for
            _live = replacement_for(provider)
            if _live:
                logger.warning(
                    "TTS voice %r is retired; taking over with %r "
                    "(same billing tier, closest language/gender)",
                    provider, _live,
                )
                provider = _live
        except Exception:  # noqa: BLE001 — 接管失败不许挡出片,按老路走
            logger.exception("legacy voice takeover failed; keeping %r", provider)
        try:  # conservative TTS char-cost accounting (uses the requested provider
            # rate; if ElevenLabs falls back to Azure below we simply over-estimate)
            from backend.services.llm_call_counter import add_active_tts_chars
            add_active_tts_chars(len(text or ""), provider)
        except Exception:
            pass
        # 不顶替的话:`elevenlabs_*` 会跳过千问 → ElevenLabs 超时 → 掉进同样坏掉的
        # 顶替后它顺着正常路径走(中文→千问 / 英文→Google),老客户无感知。
        # 默认**关**;开关一关立刻回到原样,不动数据库。
        if os.environ.get("MEDIA_BUDDY_LEGACY_VOICE_SUBSTITUTE", "0").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            from backend.services.voice_registry import substitute_legacy
            _sub = substitute_legacy(provider)
            if _sub != provider:
                logger.info(
                    "legacy voice %s → %s (老引擎已停用,自动顶替为现役音色)",
                    provider, _sub,
                )
                provider = _sub
                # 顶替后原来的供应商音色 id 不再适用,清掉让下游按新 provider 解析
                voice_id = None
        # 显式选了 ElevenLabs 音色(高级英语配音)→ 必须跳过"千问优先",否则千问会先
        # 合成成功并直接返回,客户听到的还是千问 = 安慰剂开关。此时直接落到下面的
        # ElevenLabs 分支(失败仍回退 Azure,渲染不会死在 TTS)。
        _wants_elevenlabs = provider in ELEVENLABS_VOICES or provider == "elevenlabs_tts"
        #    成功即返回;失败自动落到下面旧的 Azure/ElevenLabs 链路兜底(双保险)。
        if _qwen_tts_enabled() and not _wants_elevenlabs:
            qres = self._synthesize_qwen(
                text, output_path, speed=speed,
                voice=_qwen_voice_for(provider, voice_id),
                # (qwen_cherry)索引的,不是千问 API 的音色名(Cherry)。
                provider=provider,
            )
            if qres.success:
                return qres
            logger.warning(
                "Qwen TTS failed (%s); falling back to legacy Azure/ElevenLabs path", qres.error,
            )
        if provider in ELEVENLABS_VOICES or provider == "elevenlabs_tts":
            # ElevenLabs (with ms-level per-character timestamps → precise subtitle
            # stays on Azure. On any ElevenLabs failure we fall back to Azure so a
            # render never dies on TTS.
            if os.environ.get("MEDIA_BUDDY_ENABLE_ELEVENLABS", "0").strip().lower() in (
                "1", "true", "yes", "on",
            ):
                el_voice = (
                    ELEVENLABS_VOICES.get(provider)
                    or voice_id
                    or next(iter(ELEVENLABS_VOICES.values()))
                )
                res = self._synthesize_elevenlabs(text, output_path, el_voice, speed=speed)
                if res.success:
                    return res
                logger.warning(
                    "ElevenLabs TTS failed (%s); falling back to azure_yunyang", res.error,
                )
            else:
                logger.warning("ElevenLabs TTS is disabled; using azure_yunyang instead")
            provider = "azure_yunyang"
        # Last resort: free Microsoft Edge neural voices (no key). Legacy
        # ``azure_*`` provider keys map to the same voice names, so projects
        # saved with them keep rendering.
        if provider not in AZURE_VOICES:
            logger.warning("TTS provider %r is unavailable; using the free Edge voice instead", provider)
            provider = FALLBACK_CHAIN[0]
        voice = AZURE_VOICES[provider]
        edge_res = self._synthesize_edge(text, output_path, voice, speed=speed)
        if edge_res.success:
            return edge_res
        # Edge failed outright → rescue the narration with ElevenLabs when the
        # user enabled it (MEDIA_BUDDY_TTS_ELEVEN_RESCUE=0 turns this off).
        _el_on = os.environ.get("MEDIA_BUDDY_ENABLE_ELEVENLABS", "0").strip().lower() in ("1", "true", "yes", "on")
        _rescue_on = os.environ.get("MEDIA_BUDDY_TTS_ELEVEN_RESCUE", "1").strip().lower() in ("1", "true", "yes", "on")
        if _el_on and _rescue_on:
            el_voice = next(iter(ELEVENLABS_VOICES.values()), None)
            if el_voice:
                logger.warning(
                    "Edge TTS failed (%s); rescuing whole narration with ElevenLabs", edge_res.error,
                )
                rescue = self._synthesize_elevenlabs(text, output_path, el_voice, speed=speed)
                if rescue.success:
                    return rescue
                logger.warning("ElevenLabs rescue also failed (%s)", rescue.error)
        return edge_res

    # ------------------------------------------------------------------
    # Edge TTS (Microsoft Edge browser's neural TTS, free, no key)
    # ------------------------------------------------------------------
    def _synthesize_edge(self, text: str, output_path: str, voice: str, speed: float = 1.0) -> TTSResult:
        """Phase 2.11h — uses ``Communicate.stream()`` (not ``.save()``) so we
        also capture per-word time boundaries that Edge TTS emits alongside
        each audio chunk. Boundaries are dumped to ``<output>.words.json`` —
        a sidecar file the subtitle/SRT generator reads to align cuts to
        the actual spoken words instead of guessing by linear interpolation.

        Sidecar format (one entry per word, time in seconds):
            [{"text": "你", "start": 0.10, "end": 0.34}, ...]

        Falls back to silently skipping the sidecar if the streaming path
        fails — basic mp3 output is still produced, just without word
        timestamps. Subtitle code already has a "no-sidecar" fallback that
        does the linear-interpolation approach.
        """
        try:
            import edge_tts
        except ImportError:
            return TTSResult(success=False, error="edge-tts not installed (pip install edge-tts)")

        out = Path(output_path)
        if out.suffix.lower() != ".mp3":
            out = out.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        sidecar = out.with_suffix(".words.json")

        async def _run() -> list[dict]:
            """Return time-stamped tokens. Each entry has type=sentence|word."""
            rate_pct = int(round((max(0.7, min(2.0, float(speed or 1.0))) - 1.0) * 100))
            communicate = edge_tts.Communicate(text, voice, rate=f"{rate_pct:+d}%")
            tokens: list[dict] = []
            with open(out, "wb") as audio_file:
                async for ev in communicate.stream():
                    et = ev.get("type")
                    if et == "audio":
                        audio_file.write(ev["data"])
                    elif et in ("SentenceBoundary", "WordBoundary"):
                        # Edge TTS uses 100ns ticks for offset/duration.
                        offset_s = float(ev["offset"]) / 1e7
                        duration_s = float(ev["duration"]) / 1e7
                        tokens.append({
                            "kind": "sentence" if et == "SentenceBoundary" else "word",
                            "text": ev.get("text", ""),
                            "start": round(offset_s, 4),
                            "end": round(offset_s + duration_s, 4),
                        })
            return tokens

        try:
            try:
                tokens = asyncio.run(_run())
            except RuntimeError:
                # Already inside an event loop (rare; defensive)
                loop = asyncio.new_event_loop()
                try:
                    tokens = loop.run_until_complete(_run())
                finally:
                    loop.close()
        except Exception as e:
            return TTSResult(success=False, error=f"edge-tts failed: {e}")

        if not out.exists() or out.stat().st_size == 0:
            return TTSResult(success=False, error="edge-tts produced empty file")

        # Persist token timings sidecar (best-effort — never fail synth on this)
        if tokens:
            try:
                import json as _json
                sidecar.write_text(
                    _json.dumps(tokens, ensure_ascii=False),
                    encoding="utf-8",
                )
                kinds = {}
                for t in tokens:
                    kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
                logger.info(
                    "Edge TTS synthesized %s using %s (%s → %s)",
                    out.name, voice,
                    ", ".join(f"{n} {k}" for k, n in kinds.items()),
                    sidecar.name,
                )
            except Exception as e:
                logger.warning("token sidecar write failed: %s", e)
        else:
            logger.info("Edge TTS synthesized %s using %s (no boundaries)", out.name, voice)

        return TTSResult(success=True, output_path=str(out))

    # ------------------------------------------------------------------
    #
    # The desktop binary no longer holds ELEVENLABS_API_KEY. Audio bytes
    # are proxied through `/api/v1/ai/tts/synthesize` so that quota and
    # subscription gates apply uniformly with LLM + video.
    # ------------------------------------------------------------------
    @staticmethod
    def _probe_audio_duration(path: Path) -> float:
        """Container duration in seconds via ffprobe; 0.0 on failure."""
        import shutil
        import subprocess
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0.0
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            return float((r.stdout or "0").strip() or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _write_silence_mp3(out: Path, duration: float, ffmpeg: str) -> bool:
        """Render a silent mp3 matching Azure's 24kHz mono 48kbps so it concats
        cleanly with real Azure segments. Used as the per-sentence fallback when
        synthesis fails, so the narration timeline stays intact."""
        import subprocess
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-f", "lavfi",
                 "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
                 "-t", f"{max(0.1, float(duration)):.3f}",
                 "-c:a", "libmp3lame", "-b:a", "48k", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            return r.returncode == 0 and out.exists() and out.stat().st_size > 0
        except Exception:
            return False

    @staticmethod
    def _split_tts_gateway_chunks(text: str, max_chars: int = 900) -> list[str]:
        """

        Azure keys stay in the cloud; this only avoids sending a full
        10-30 minute script through one serverless request.
        """
        import re as _re

        parts = _re.split(r"(?<=[。！？!?…\n])", text or "")
        sentences = [p.strip() for p in parts if p and p.strip()]
        if not sentences and text.strip():
            sentences = [text.strip()]

        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(sentence) > max_chars:
                # 超长句(无句末标点却很长的流水句):在【子句停顿处】(逗号/顿号/分号/冒号)切,
                # 绝不再每 max_chars 字盲切句子中间 —— 那会让千问把词组从中间截断,听感破句。
                if current:
                    chunks.append(current)
                    current = ""
                for piece in TTSService._split_long_at_clauses(sentence, max_chars):
                    chunks.append(piece)
                continue
            candidate = f"{current}\n{sentence}".strip() if current else sentence
            if len(candidate) > max_chars and current:
                chunks.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _split_long_at_clauses(sentence: str, max_chars: int) -> list[str]:
        """把一个超长句在【子句标点】(，、；：,;:)后切成 ≤max_chars 的片段,尽量落在
        自然停顿处而非句子中间。只有当单个子句仍超长(无内部标点的极端情况)才最后兜底硬切。"""
        import re as _re
        parts = [p for p in _re.split(r"(?<=[，、；：,;:])", sentence or "") if p and p.strip()]
        out: list[str] = []
        cur = ""
        for p in parts:
            if len(p) > max_chars:
                # 单个子句还超长(几乎不会发生):先冲掉累积,再对这个子句兜底硬切
                if cur.strip():
                    out.append(cur.strip())
                    cur = ""
                for s in range(0, len(p), max_chars):
                    piece = p[s:s + max_chars].strip()
                    if piece:
                        out.append(piece)
                continue
            candidate = f"{cur}{p}"
            if len(candidate) > max_chars and cur.strip():
                out.append(cur.strip())
                cur = p
            else:
                cur = candidate
        if cur.strip():
            out.append(cur.strip())
        return [o for o in out if o]

    @staticmethod
    def _shift_timing_tokens(tokens: list[dict], offset: float) -> list[dict]:
        shifted: list[dict] = []
        for token in tokens:
            try:
                start = float(token.get("start", 0.0)) + offset
                end = float(token.get("end", start)) + offset
            except Exception:
                continue
            shifted.append({
                **token,
                "start": round(start, 3),
                "end": round(max(start, end), 3),
            })
        return shifted

    def _delivery_language(self, text: str) -> str:
        """配音该用哪种语言念("Chinese"/"English")。**先看设定,猜只当兜底。**

        🚨 这个值同时决定两件事:发给千问的 `language_type`,以及那段
           风格指令用中文版还是英文版。猜反 = **整条片子念错语言**,
           比字幕对不齐严重得多。

        的设定对齐啊,不然不是胡乱在搞吗」。出片语言是客户选的,它是权威;
        数中日韩字 vs 拉丁字母只是没有设定时的下策。
        """
        setting = str(self.output_language or "").strip().lower()
        if setting == "zh":
            return "Chinese"
        if setting == "en":
            return "English"
        return _qwen_language(text)

    def _align_language(self, text: str) -> str:
        """告诉 whisper 该听哪种话。**先看这条片子的设定,猜只当兜底。**

           它是权威。按文本猜(数中日韩字 vs 拉丁字母)在**混合内容**上会猜反 ——
           英文稿里塞一堆中文人名/地名,或中文稿里引一长段英文,都会翻车,
           那就又回到「听错语言 → 一个字对不上 → 字幕时间全靠猜」的老坑。

        生产 30 天数据同向:中文出片 91 次一次没崩,英文 32 次崩 9 次。

        ⚠️ 设定只认 zh/en 两个值(管线也只支持这两种)。给了别的或没给 → 退回猜。
        """
        setting = str(self.output_language or "").strip().lower()
        if setting in ("zh", "en"):
            return setting
        return "zh" if _qwen_language(text or "") == "Chinese" else "en"

    def _forced_align_tokens(self, audio_path, text: str) -> Optional[list[dict]]:
        """用 faster-whisper 强制对齐,返回【真实毫秒】逐字 token;不可用/失败 → None(调用方退回估算)。

        以【独立 venv + 子进程】方式调 scripts/forced_align.py(faster-whisper 依赖重,不入主 venv)。
        env:
          MEDIA_BUDDY_FORCED_ALIGN=0    关闭(默认开)
          MEDIA_BUDDY_ALIGN_PYTHON      aligner venv 的 python(默认空 = 不启用)
          MEDIA_BUDDY_ALIGN_MODEL/CACHE 传给子脚本
        """
        # 关字幕 → 跳过(见 synthesize 里 _align_words 的说明)。默认 True,老调用不受影响。
        if not getattr(self, "_align_words", True):
            logger.info("forced align skipped: subtitles are off for this project")
            return None
        if os.environ.get("MEDIA_BUDDY_FORCED_ALIGN", "1").strip().lower() in ("0", "false", "no", "off"):
            return None
        align_py = os.environ.get("MEDIA_BUDDY_ALIGN_PYTHON", "").strip()
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "forced_align.py")
        if not os.path.exists(align_py) or not os.path.exists(script):
            return None
        import tempfile
        import time
        import subprocess
        import json as _json
        txt_file = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
                tf.write(text or "")
                txt_file = tf.name
            t0 = time.time()
            #
            # 脚本里原来写死 `language="zh"`。英文旁白被强制按中文转写 →
            # 出来一堆音译汉字 → 和英文稿逐字比对 → match_rate 0.00~0.06 →
            # 字幕时间全靠插值猜 → 客户看到「字幕和音频对不齐」。
            #
            # 复用现成的 `_qwen_language`(按中日韩字 vs 拉丁字母数),
            # 不再自己写一套判定。
            _env = dict(os.environ)
            _env["MEDIA_BUDDY_ALIGN_LANG"] = self._align_language(text)
            r = subprocess.run(
                [align_py, script, str(audio_path), txt_file],
                capture_output=True, text=True, timeout=600, env=_env,
            )
            if r.returncode != 0 or not r.stdout.strip():
                logger.warning("forced align: subprocess failed rc=%s err=%s", r.returncode, (r.stderr or "")[-200:])
                return None
            data = _json.loads(r.stdout)
            if not data.get("ok") or not data.get("tokens"):
                logger.warning("forced align: not ok (%s)", data.get("error"))
                return None
            # 🚨 **match_rate 低要吼出来。** 以前不管多低都打「forced align OK」,
            #    0.00 也照样发货 —— 客户看到字幕全飘,日志里却一片祥和。
            #
            # ⚠️ 低了**仍然照用**,不退回均分估算:whisper 的段落时间是真实音频时间,
            #    拿稿子在真实段落里插值,大概率还是比「整段平均分」准。
            #    没量过就不改行为 —— 这里先把它变成**能查的**,别再悄悄发货。
            _rate = float(data.get("match_rate", 0.0) or 0.0)
            _floor = float(os.environ.get("MEDIA_BUDDY_ALIGN_MIN_MATCH", "0.60") or 0.60)
            _lang = _env["MEDIA_BUDDY_ALIGN_LANG"]
            if _rate < _floor:
                logger.warning(
                    "forced align LOW MATCH: match_rate=%.2f < %.2f (lang=%s, %d tokens, %.1fs)"
                    " — 字幕时间基本靠插值,成片可能对不齐",
                    _rate, _floor, _lang, len(data["tokens"]), time.time() - t0,
                )
            else:
                logger.info(
                    "forced align OK: %d tokens, match_rate=%.2f, lang=%s, %.1fs",
                    len(data["tokens"]), _rate, _lang, time.time() - t0,
                )
            return data["tokens"]
        except Exception as e:  # noqa: BLE001 — 对齐失败绝不挂出片,退回估算
            logger.warning("forced align exception: %s %s", type(e).__name__, str(e)[:150])
            return None
        finally:
            if txt_file:
                try:
                    os.unlink(txt_file)
                except Exception:
                    pass

    @staticmethod
    def _even_char_tokens(text: str, *, start: float, duration: float) -> list[dict]:
        chars = list(text or "")
        if not chars:
            return []
        step = max(0.01, duration / max(1, len(chars)))
        return [
            {
                "kind": "char",
                "text": ch,
                "start": round(start + i * step, 3),
                "end": round(start + (i + 1) * step, 3),
            }
            for i, ch in enumerate(chars)
        ]

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
    def _clamp_tokens_to_duration(tokens: list[dict], duration: float) -> list[dict]:
        """Clamp a chunk's tokens into its actual audio span [0, duration].

        Alignment timestamps can run a hair past the probed MP3 length
        (trailing silence / rounding). Left unclamped, the next chunk's
        shifted tokens overlap this chunk's tail → the assembled stream
        reads as non-monotonic and the whole video's real per-word timing
        gets discarded → subtitles drift out of sync with the voice. Clamping
        keeps each chunk inside its own audio slot so the join stays monotonic."""
        if duration <= 0:
            return tokens
        clamped: list[dict] = []
        for token in tokens:
            try:
                start = min(max(0.0, float(token.get("start", 0.0))), duration)
                end = min(max(start, float(token.get("end", start))), duration)
            except Exception:
                continue
            clamped.append({**token, "start": round(start, 3), "end": round(end, 3)})
        return clamped

    @staticmethod
    def _enforce_monotonic_tokens(tokens: list[dict]) -> list[dict]:
        """Make a token stream monotonic WITHOUT destroying real timing.

        Pushes each token's start to >= the previous token's end (a forward
        squeeze), preserving the genuine per-word pacing from TTS alignment.
        Replaces the old 'rebuild even character timing' path, which spread
        every char evenly across the whole duration and made subtitles drift
        out of sync with the voice (esp. multi-chunk long videos). Even timing
        is now only a last resort, used when no usable tokens exist at all."""
        repaired: list[dict] = []
        prev_end = 0.0
        for token in tokens:
            try:
                start = float(token.get("start", 0.0))
                end = float(token.get("end", start))
            except Exception:
                continue
            start = max(start, prev_end)
            end = max(end, start)
            repaired.append({**token, "start": round(start, 3), "end": round(end, 3)})
            prev_end = end
        return repaired


    def _synthesize_qwen(
        self, text: str, output_path: str, speed: float = 1.0, voice: Optional[str] = None,
        provider: str = "",
    ) -> TTSResult:
        """千问 TTS(qwen3-tts-instruct-flash)：中文标点感知分段(≤480,给 Qwen 600 上限
        留余量)→逐段 DashScope HTTP 合成→下载 24h 临时 WAV→ffmpeg 统一转 mp3→concat→
        任一段 3 次重试仍失败=整条失败(交给 reaper 重跑),不塞静音。"""
        import json as _json
        import shutil
        import subprocess
        import tempfile
        import time
        import urllib.error
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor as _Pool

        key = os.environ.get("MEDIA_BUDDY_QWEN_API_KEY", "").strip()
        if not key:
            return TTSResult(success=False, error="Qwen TTS: MEDIA_BUDDY_QWEN_API_KEY not set")
        base = _qwen_tts_base()
        model = os.environ.get("MEDIA_BUDDY_QWEN_TTS_MODEL", "qwen3-tts-instruct-flash").strip() \
            or "qwen3-tts-instruct-flash"
        voice = (voice or os.environ.get("MEDIA_BUDDY_QWEN_TTS_VOICE", "Cherry").strip() or "Cherry")
        # 🚨 校验要按**真正发声的音色**查配速表,不是客户选的 provider ——
        #    provider 可能是旧 Azure key,发声的却是映射过去的千问音色。
        #    拿错尺子会把好音频判成「太短」白白重合成(见 _qwen_pacing_key)。
        pacing = _qwen_pacing_key(voice)
        # optimize_instructions=True 让千问【每块各自重解释】风格指令 → 长旁白拆成 N 块独立
        # 生成式调用时,块间音色/气口漂移(听感"中途换了个人")。设 0 让各块用【同一份】
        # 指令,前后统一。
        #
        #    (「moon-广播腔+关重解释」)。置 1 可回到旧行为。
        optimize = os.environ.get("MEDIA_BUDDY_QWEN_TTS_OPTIMIZE", "0").strip().lower() not in (
            "0", "false", "no", "off",
        )
        # 整条统一响度:【拼接后对整条】做一次 loudnorm → 全片人声电平/动态一致,弱化块边界
        whole_track_ln = os.environ.get("MEDIA_BUDDY_TTS_WHOLE_TRACK_LOUDNORM", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        atempo = _qwen_atempo_filter(speed)
        language = self._delivery_language(text)
        # 语速用 FFmpeg atempo 精确控制(见 _qwen_atempo_filter);instruction 只管风格/自然度。
        # 按检测到的语种给对应语言的交付指令:英文文本别再套中文讲解指令(否则英文口播打折)。
        # env MEDIA_BUDDY_QWEN_TTS_INSTRUCTIONS 仍优先覆盖两种语言。
        instructions = os.environ.get("MEDIA_BUDDY_QWEN_TTS_INSTRUCTIONS", "").strip()
        if not instructions:
            #
            # 上一版是「语气平稳、不要夸张的抑扬顿挫」—— 当初为治「奇怪的抑扬顿挫」
            #
            # 现在这版是 A/B 盲听出来的:做了 5 种指令 × 8 个男声 × 4 个微调,
            # 用户选定「YouTuber 强指令 + 广播腔 + 关掉段间重解释」。
            #
            # ⚠️ 改这段前先做样本让人听。听感不能靠读代码判断,也不能靠日志。
            instructions = (
                "Read this like a top English-language YouTube explainer host. "
                "Hook the viewer in the first two sentences; noticeably emphasise and "
                "slightly slow down on specific numbers, dates and amounts so they land; "
                "put a short pause before turns like \"but\" or \"here is the surprising part\"; "
                "make questions sound like real questions to the viewer. "
                "Confident, steady, energetic but never shouty — a seasoned host, not a "
                "reading machine. On top of that, use a broadcast delivery: crisp "
                "articulation, deep steady breath support, full chest resonance and weight, "
                "the composed authority of a radio host or documentary narrator; land and "
                "settle the end of each sentence instead of letting it drift."
                if language == "English"
                else "请用标准普通话，像一位头部中文 YouTube 知识博主做口播那样朗读。"
                     "开头两句要有抓人的劲儿，把观众拽进来；"
                     "讲到具体数字、时间、金额时明显加重并略微放慢，让观众记住；"
                     "遇到「但是」「真正让人意外的是」这类转折，前面留一个短停顿再推出去；"
                     "问句要真的像在问观众。全程语气稳定自信、有能量但不吵，"
                     "像很熟练的主播而不是朗读机器。吐字清楚，绝不带方言或外语口音。"
                     "在此基础上加上广播腔：字正腔圆、咬字扎实，气息沉稳绵长，"
                     "胸腔共鸣饱满、声音有厚度，像电台主播或纪录片解说那样"
                     "从容、有分量、有权威感；每句话的落点要沉下去收住，不要飘。"
            )

        out = Path(output_path)
        if out.suffix.lower() != ".mp3":
            out = out.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return TTSResult(success=False, error="Qwen TTS: ffmpeg not on PATH")
        # 所以中文段长必须≤~280。取 250 留余量(标点/指令),否则 400 InvalidParameter。
        chunks = self._split_tts_gateway_chunks(text, max_chars=250)
        if not chunks:
            return TTSResult(success=False, error="Qwen TTS: empty text")

        url = base + "/services/aigc/multimodal-generation/generation"
        seg_dir = Path(tempfile.mkdtemp(prefix="qwen_tts_"))
        seg_paths: list[Path] = []
        all_tokens: list[dict] = []
        total_duration = 0.0
        total_chars = 0
        try:
            # ── 分段并行 ──────────────────────────────────────────────
            #
            #
            #
            # 中文长片:16 段串行,TTS 花了 **8.7 分钟**。
            #
            # 📌 教训:动手优化之前**先量哪条路用得多**。「一次只动一条通道」的原则
            #    没错,错在挑通道时看的是恰好翻到的英文样本,没去数。
            #
            # 🚨 顺序:字幕时间码是累加出来的(`start=total_duration`),线程池按完成
            #    顺序返回。直接拼 → 音频和字幕全乱,**而且不报错**。所以并行只做
            #    「合成一段」,累加和拼接留到排序之后串行做。
            #
            # ⚠️ 千问比 Google 多一层**段完整性重试**:它偶尔吞掉一段文本的尾部,
            #    原样搬进 worker,一行没动 —— 丢了它客户会拿到少念半句话的片子。
            try:
                tts_conc = max(1, min(8, int(
                    os.environ.get("MEDIA_BUDDY_TTS_CONCURRENCY", "4") or "4")))
            except ValueError:
                tts_conc = 4

            def _one_segment(index: int, chunk: str):
                """合成一段(含段完整性重试)。**绝不碰 total_duration /
                seg_paths / all_tokens / total_chars** —— 那些是跨段累加量,
                在这里读写就是竞态。返回 (index, seg, duration, chars, error)。"""
                # 段完整性重试(见 _segment_duration_ok):
                # 千问偶尔会吞掉一段文本的尾部,却照样返回 finish_reason="stop"
                # 全部尝试都偏短时取**最长的一版**:短一点的片子远好过整条失败。
                best_seg: Optional[Path] = None
                best_dur = -1.0
                best_chars = 0
                for redo in range(_SEGMENT_MAX_RETRIES + 1):
                    audio_url: Optional[str] = None
                    chars = 0
                    last_err = ""
                    for attempt in range(3):
                        try:
                            body = {"model": model, "input": {
                                "text": chunk,
                                "voice": voice,
                                "language_type": language,
                                "instructions": instructions,
                                "optimize_instructions": optimize,
                            }}
                            req = urllib.request.Request(
                                url, data=_json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + key,
                                         "Content-Type": "application/json"},
                                method="POST",
                            )
                            with urllib.request.urlopen(req, timeout=90) as r:
                                d = _json.loads(r.read())
                            o = (d.get("output") or {})
                            audio = (o.get("audio") or {})
                            if o.get("finish_reason") == "stop" and audio.get("url"):
                                audio_url = str(audio["url"])
                                chars = int((d.get("usage") or {}).get("characters") or len(chunk))
                                break
                            last_err = "no audio (finish=%s)" % o.get("finish_reason")
                        except urllib.error.HTTPError as e:
                            detail = ""
                            try:
                                detail = e.read().decode()[:120]
                            except Exception:
                                pass
                            last_err = "HTTP %d %s" % (e.code, detail)
                            if e.code in (400, 401, 403):  # 鉴权/参数错,不重试
                                return index, None, 0.0, 0, ("Qwen TTS: " + last_err)
                        except Exception as e:  # noqa: BLE001 — 网络/超时,退避重试
                            last_err = "%s %s" % (type(e).__name__, str(e)[:120])
                        if attempt < 2:
                            time.sleep(1.0 * (attempt + 1))
                    if not audio_url:
                        return index, None, 0.0, 0, ("Qwen TTS chunk %d failed: %s" % (index, last_err))
                    # 下载 24h 临时 WAV → 立刻落本地(链接只保证 24h)
                    wav = seg_dir / ("seg_%04d_%d.wav" % (index, redo))
                    try:
                        urllib.request.urlretrieve(audio_url, str(wav))
                    except Exception as e:  # noqa: BLE001
                        return index, None, 0.0, 0, ("Qwen TTS chunk %d download failed: %s" % (index, e))
                    if not wav.exists() or wav.stat().st_size == 0:
                        return index, None, 0.0, 0, ("Qwen TTS chunk %d empty download" % index)
                    # 统一转 mp3/44100/mono + atempo 精确控速,便于后续 concat -c copy。
                    # 【响度归一·治忽大忽小】每段是独立 API 合成、响度各不同,裸拼会段间跳。
                    # 在此对【每一段】loudnorm 到 -16 LUFS,拼接后整条人声轨电平统一。
                    # env MEDIA_BUDDY_TTS_CHUNK_LOUDNORM=0 可关(默认开)。
                    seg = seg_dir / ("seg_%04d_%d.mp3" % (index, redo))
                    conv = [ffmpeg, "-y", "-i", str(wav)]
                    _af: list[str] = []
                    if atempo:
                        _af.append(atempo)
                    if not whole_track_ln and os.environ.get("MEDIA_BUDDY_TTS_CHUNK_LOUDNORM", "1").strip().lower() not in ("0", "false", "no", "off"):
                        _af.append("loudnorm=I=-16:TP=-1.5:LRA=11")
                    if _af:
                        conv += ["-filter:a", ",".join(_af)]
                    conv += ["-ar", "44100", "-ac", "1", "-b:a", "128k", str(seg)]
                    r = subprocess.run(conv, capture_output=True, text=True, timeout=120)
                    if r.returncode != 0 or not seg.exists() or seg.stat().st_size == 0:
                        return index, None, 0.0, 0, ("Qwen TTS chunk %d mp3 convert failed" % index)
                    duration = self._probe_audio_duration(seg)
                    if duration <= 0:
                        duration = max(0.4, len(chunk) / 4.5)
                    if duration > best_dur:
                        best_seg, best_dur, best_chars = seg, duration, chars
                    if _segment_duration_ok(duration, chunk, pacing, speed,
                                           lang=self.output_language):
                        break
                    logger.warning(
                        "Qwen TTS segment %d audio too short for its text "
                        "(%.1fs for %d chars, voice=%s speed=%.2f) - resynthesizing (%d/%d)",
                        index, duration, len(chunk), voice, float(speed or 1.0),
                        redo + 1, _SEGMENT_MAX_RETRIES,
                    )
                if best_seg is None:
                    return index, None, 0.0, 0, ("Qwen TTS chunk %d produced no audio" % index)
                seg, duration, chars = best_seg, best_dur, best_chars
                if not _segment_duration_ok(duration, chunk, pacing, speed,
                                       lang=self.output_language):
                    # 重试用尽仍偏短 —— 照发最长的一版,但**大声记下来**,
                    # 否则又变成一次没人知道的静默丢字。
                    logger.error(
                        "Qwen TTS segment %d still short after %d retries "
                        "(%.1fs for %d chars) - shipping longest take; narration may be incomplete",
                        index, _SEGMENT_MAX_RETRIES, duration, len(chunk),
                    )
                return index, seg, duration, chars, ""

            # 并行合成各段;段与段之间没有依赖,慢在等 DashScope 返回。
            if len(chunks) == 1 or tts_conc == 1:
                results = [_one_segment(i, c) for i, c in enumerate(chunks)]
            else:
                with _Pool(max_workers=min(tts_conc, len(chunks))) as ex:
                    results = list(ex.map(lambda a: _one_segment(*a), enumerate(chunks)))

            # 🚨 必须按索引重排 —— ex.map 保序,但换成 as_completed 就不保了;
            #    显式排一次,让这条不依赖上面用的是哪个 API。
            results.sort(key=lambda t: t[0])

            for index, seg, duration, chars, err in results:
                if err:
                    return TTSResult(success=False, error=err)
                chunk = chunks[index]
                # ⚠️ 时间码在这里才算 —— 排序之后、单线程、按顺序累加。
                all_tokens.extend(self._even_char_tokens(
                    chunk, start=total_duration, duration=duration))
                seg_paths.append(seg)
                total_duration += duration
                total_chars += chars


            # 拼接
            if len(seg_paths) == 1:
                out.write_bytes(seg_paths[0].read_bytes())
            else:
                list_file = seg_dir / "list.txt"
                list_file.write_text(
                    "\n".join("file '%s'" % p.as_posix() for p in seg_paths), encoding="utf-8",
                )
                r = subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                     "-c", "copy", str(out)],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    return TTSResult(success=False,
                                     error="Qwen TTS concat failed: %s" % ((r.stderr or "")[-200:]))

            # 整条统一响度(whole_track_ln):对拼好的整条做一次 loudnorm,全片电平/动态一致,
            # 弱化块边界。失败一律保留原文件(绝不因归一化搞挂出片)。时长基本不变,不影响下方
            # 字幕漂移校准(它按最终文件真实时长比例缩放)。
            if whole_track_ln and out.exists() and out.stat().st_size > 0:
                try:
                    ln_tmp = out.with_name(out.stem + "_ln.mp3")
                    r_ln = subprocess.run(
                        [ffmpeg, "-y", "-i", str(out),
                         "-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11",
                         "-ar", "44100", "-ac", "1", "-b:a", "128k", str(ln_tmp)],
                        capture_output=True, text=True, timeout=300,
                    )
                    if r_ln.returncode == 0 and ln_tmp.exists() and ln_tmp.stat().st_size > 0:
                        ln_tmp.replace(out)
                    else:
                        logger.warning("whole-track loudnorm failed; keeping un-normalized narration")
                except Exception:  # noqa: BLE001 — 归一化绝不许挡出片
                    logger.warning("whole-track loudnorm crashed; keeping un-normalized narration", exc_info=True)

            # 【字幕后半漂移·根治】token 时间轴是按"各段单独探测时长之和"(total_duration)建的,
            # 但 mp3 `concat -c copy` 每个拼接点会塞进 ~20-50ms 编码填充,真实拼好的音频比
            # total_duration 长、且间隙逐段累积 → 字幕越到后面比语音越早(前对后错)。
            # 这里探测【最终拼接文件】的真实时长,把整条 token 时间轴按比例校准到真实音频上。
            # 段长相近时按比例缩放是精确的(间隙随段号线性累积、token 随时间线性分布)。
            if len(seg_paths) > 1 and all_tokens and total_duration > 0:
                real_dur = self._probe_audio_duration(out)
                if real_dur > 0:
                    ratio = real_dur / total_duration
                    if abs(ratio - 1.0) > 0.003:  # 有可感知累积间隙才校准
                        logger.info(
                            "Qwen TTS subtitle drift fix: segs_sum=%.2fs real=%.2fs ratio=%.4f — rescaling %d tokens",
                            total_duration, real_dur, ratio, len(all_tokens),
                        )
                        for _t in all_tokens:
                            _t["start"] = round(float(_t.get("start", 0.0)) * ratio, 3)
                            _t["end"] = round(float(_t.get("end", 0.0)) * ratio, 3)

            # 【真毫秒对齐】上面 all_tokens 是按字数均摊的【估算】。这里用 faster-whisper 对
            # 【最终拼接音频】做强制对齐,拿到每个字的【真实毫秒时间】,替换估算 → 字幕严丝合缝。
            # 用 whisper 的时间 + 我们已知文稿的文字(无同音字错)。失败/无 aligner → 保留估算,不挂片。
            aligned = self._forced_align_tokens(out, text)
            if aligned:
                all_tokens = aligned

            # 时间码单调修正 + 写 sidecar(字幕生成器读它)
            if all_tokens and not self._timing_tokens_are_monotonic(all_tokens):
                all_tokens = self._enforce_monotonic_tokens(all_tokens)
            if not all_tokens:
                all_tokens = self._even_char_tokens(
                    text, start=0.0, duration=max(total_duration, self._probe_audio_duration(out), 0.4),
                )
            out.with_suffix(".words.json").write_text(
                _json.dumps(all_tokens, ensure_ascii=False), encoding="utf-8",
            )
            cost_cny = total_chars * 0.8 / 10000.0
            logger.info(
                "Qwen TTS synthesized %s voice=%s speed=%.2f(atempo=%s) lang=%s segs=%d dur=%.1fs chars=%d cost=%.4f CNY",
                out, voice, float(speed or 1.0), atempo or "1", language,
                len(seg_paths), total_duration, total_chars, cost_cny,
            )
            return TTSResult(success=True, output_path=str(out))
        finally:
            try:
                shutil.rmtree(seg_dir, ignore_errors=True)
            except Exception:
                pass

    @staticmethod
    def _elevenlabs_keys() -> list[str]:
        """ElevenLabs 的**密钥池**,按顺序试。

        ## 为什么要有池子

        备用的 Azure 又「所有 chunk 静音」(Azure 已弃用)→
        单点密钥 = 单点故障,而这个单点已经炸过一次。

        ## 配置方式(两种都支持,可混用)

            MEDIA_BUDDY_ELEVENLABS_API_KEY      主键
            MEDIA_BUDDY_ELEVENLABS_API_KEY_2    备用键(可以有 _3 _4 …)

        或者一个变量里用逗号分隔:

            MEDIA_BUDDY_ELEVENLABS_API_KEY=key1,key2

        ⚠️ **顺序就是优先级**,前面的先用完再用后面的。
        ⚠️ 去重 —— 同一把 key 配两遍时不要白白多试一次。
        """

        raw: list[str] = []
        primary = os.environ.get("MEDIA_BUDDY_ELEVENLABS_API_KEY", "")
        raw.extend(primary.split(","))
        for i in range(2, 8):
            raw.extend(os.environ.get(f"MEDIA_BUDDY_ELEVENLABS_API_KEY_{i}", "").split(","))
        out: list[str] = []
        for k in raw:
            k = k.strip()
            if k and k not in out:
                out.append(k)
        return out

    @staticmethod
    def _elevenlabs_key_exhausted(exc: Exception) -> bool:
        """这个异常是不是「**这把 key 不能用了**」——该换下一把?

        ⚠️ 只认**密钥级**的故障。网络抖动、超时、5xx 都**不算** ——
           那种情况换 key 也没用,换了只会把第二把也拖进重试风暴。

        认这几种:
          401 额度用完 / key 被吊销 / 权限不足
          429 触发限流(这把 key 当前不可用)
        """

        msg = repr(exc)
        return ("401" in msg or "429" in msg
                or "quota" in msg.lower() or "unauthorized" in msg.lower())

    @staticmethod
    def _elevenlabs_direct(
        text: str, voice_id: str, api_key: str, speed: float = 1.0,
    ) -> tuple[bytes, dict]:
        """Worker-direct ElevenLabs with-timestamps. Returns (audio_bytes, meta)
        caller's token/sidecar code is unchanged. Uses the RAW alignment (it keeps
        punctuation/decimals 1:1 with the script — verified for Chinese, so no
        Azure 43.3->433 normalization bug)."""
        import base64
        import httpx as _httpx
        model = os.environ.get(
            "MEDIA_BUDDY_ELEVENLABS_DIRECT_MODEL", "eleven_multilingual_v2",
        )
        body = {"text": text, "model_id": model}
        # 语速统一交给下面的 ffmpeg atempo(变速不变调),不再用 voice_settings.speed:
        # eleven 对中文几乎不认它,而且会和 atempo 叠加导致过快。tempo 由每条视频选的
        # 语速挡位(speed)决定:正常1.0/稍快1.2/快1.4/极快1.6,1.0=原速。夹在
        # [0.5,2.0](atempo 单级上限)。
        tempo = max(0.5, min(2.0, float(speed or 1.0)))
        r = _httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=body, timeout=120,
        )
        r.raise_for_status()
        d = r.json()
        audio = base64.b64decode(d.get("audio_base64") or "")
        alignment = d.get("alignment")
        # 拉伸时长(变速不变调),并把 with-timestamps 的字符级时间戳同比缩放,字幕保持同步。
        # 关键修复:以前这里用写死的 env(1.15)忽略了传入的 speed,导致客户在界面上换语速
        # "没反应"。现在 tempo = 传入的 speed(挡位),所有音色统一加速,确定性生效。
        # 1.0 = 原速不处理。env MEDIA_BUDDY_ELEVENLABS_SPEED 已退役(不再强制覆盖)。
        if audio and abs(tempo - 1.0) > 1e-3:
            new_audio, alignment = TTSService._atempo_audio_and_alignment(
                audio, alignment, tempo,
            )
            if new_audio:
                audio = new_audio
        return audio, {
            "alignment": alignment,
            "normalized_alignment": None,
            "cost_eur": "0",
        }

    @staticmethod
    def _atempo_audio_and_alignment(
        audio_bytes: bytes, alignment, tempo: float,
    ) -> tuple[Optional[bytes], dict]:
        """Speed up an MP3 by ``tempo`` (pitch-preserving, via ffmpeg ``atempo``)
        and scale the ElevenLabs char-level alignment timestamps by ``1/tempo`` so
        burned subtitles stay in sync. Returns ``(new_bytes, new_alignment)``; on
        any failure returns ``(None, original_alignment)`` so the caller keeps the
        unmodified audio (a render never dies for a speed tweak)."""
        import subprocess
        import tempfile
        try:
            tempo = max(1.0, min(2.0, float(tempo)))
            with tempfile.TemporaryDirectory() as td:
                src = os.path.join(td, "in.mp3")
                dst = os.path.join(td, "out.mp3")
                with open(src, "wb") as f:
                    f.write(audio_bytes)
                cp = subprocess.run(
                    ["ffmpeg", "-y", "-i", src, "-filter:a", f"atempo={tempo:.4f}",
                     "-c:a", "libmp3lame", "-q:a", "2", dst],
                    capture_output=True, timeout=120,
                )
                if cp.returncode != 0 or not os.path.exists(dst):
                    return None, alignment
                with open(dst, "rb") as f:
                    new_bytes = f.read()
        except Exception:
            return None, alignment
        if not new_bytes:
            return None, alignment
        new_align = alignment
        if isinstance(alignment, dict):
            inv = 1.0 / tempo
            new_align = dict(alignment)
            for k in ("character_start_times_seconds", "character_end_times_seconds"):
                vals = alignment.get(k)
                if isinstance(vals, list):
                    new_align[k] = [round(float(v) * inv, 4) for v in vals]
        return new_bytes, new_align

    def _synthesize_elevenlabs(
        self, text: str, output_path: str, voice_id: Optional[str], speed: float = 1.0,
    ) -> TTSResult:
        import asyncio
        import uuid

        from backend.lib.cloud_auth import NotAuthenticatedError
        from backend.lib.cloud_gateway import (
            GatewayError,
            QuotaExceededError,
            SubscriptionInactiveError,
            UpstreamError,
            get_default_gateway,
        )

        vid = voice_id or next(iter(ELEVENLABS_VOICES.values()))

        out = Path(output_path)
        if out.suffix.lower() != ".mp3":
            out = out.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)

        #    见 `_elevenlabs_keys` 的说明。
        el_keys = self._elevenlabs_keys()
        el_key = el_keys[0] if el_keys else ""
        if el_key:
            # ElevenLabs 单次合成有字符上限(~5000 含标点)。长视频文案会超限 →
            # 整条请求被拒 → 退回 Azure 又全静音 → 出片失败(长视频高级英语必挂)。
            # 拼接音频 → 按累计时长偏移合并字符级时间戳(字幕保持同步)。短视频=单段,
            # 走下面原有单发路径,行为完全不变。
            try:
                el_max = max(500, int(os.environ.get("MEDIA_BUDDY_ELEVENLABS_MAX_CHARS", "3500") or 3500))
            except ValueError:
                el_max = 3500
            el_chunks = self._split_tts_gateway_chunks(text, max_chars=el_max)
            if len(el_chunks) > 1:
                return self._elevenlabs_direct_chunked(text, el_chunks, vid, el_key, speed, out)
            # 410 for ElevenLabs (never deployed), so the worker calls ElevenLabs
            # directly with a key in .env. Char-level ms alignment keeps
            # punctuation/decimals natively — no Azure 43.3->433 bug.
            # ⚠️ **逐把试**。只在「这把 key 不能用了」时才换下一把 ——
            #    网络抖动/超时/5xx 换 key 没意义,还会把第二把也拖进重试风暴
            #    (判据见 `_elevenlabs_key_exhausted`)。
            audio_bytes = meta = None
            last_err: Exception | None = None
            for idx, k in enumerate(el_keys):
                try:
                    audio_bytes, meta = self._elevenlabs_direct(text, vid, k, speed)
                    if idx:
                        logger.warning(
                            "ElevenLabs 第 %d 把 key 生效(前 %d 把已失效)——"
                            "去后台确认额度,别等全部用完", idx + 1, idx)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if not self._elevenlabs_key_exhausted(e):
                        break          # 不是密钥问题 → 换了也没用,直接报错
                    logger.warning(
                        "ElevenLabs 第 %d 把 key 不可用(%s),换下一把",
                        idx + 1, repr(e)[:80])
            if audio_bytes is None:
                return TTSResult(
                    success=False,
                    error=(f"ElevenLabs direct failed(试了 {len(el_keys)} 把 key): "
                           f"{repr(last_err)[:160]}"))
        else:
            # Process-wide singleton — see cloud_auth.get_default_auth() docstring.
            gw = get_default_gateway()
            if not gw.is_authenticated():
                return TTSResult(
                    success=False,
                    error="not signed in to Media Buddy cloud — log in first",
                )
            try:
                audio_bytes, meta = asyncio.run(gw.tts_synthesize(
                    text=text,
                    voice_id=vid,
                    model_id=ELEVENLABS_MODEL_ID,
                    with_timestamps=True,
                    speed=speed,
                    idempotency_key=str(uuid.uuid4()),
                    project_id=self.project_id,
                ))
            except NotAuthenticatedError as e:
                return TTSResult(success=False, error=f"cloud not authenticated: {e}")
            except QuotaExceededError as e:
                return TTSResult(success=False, error=str(e))
            except SubscriptionInactiveError as e:
                return TTSResult(success=False, error=f"subscription inactive: {e.payload}")
            except UpstreamError as e:
                return TTSResult(success=False, error=f"ElevenLabs upstream failed: {e.payload}")
            except GatewayError as e:
                return TTSResult(success=False, error=f"gateway error: {e}")

        if not audio_bytes:
            return TTSResult(success=False, error="ElevenLabs TTS produced empty payload")

        with open(out, "wb") as f:
            f.write(audio_bytes)
        if not out.exists() or out.stat().st_size == 0:
            return TTSResult(success=False, error="ElevenLabs TTS produced empty file")

        sidecar = out.with_suffix(".words.json")
        try:
            alignment = meta.get("normalized_alignment") or meta.get("alignment")
            self._log_elevenlabs_alignment_debug(meta, alignment)
            sentence_tokens = self._elevenlabs_alignment_to_sentence_tokens(text, alignment)
            if not sentence_tokens:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                timestamp_error = meta.get("timestamp_error")
                suffix = f": {timestamp_error}" if timestamp_error else ""
                return TTSResult(
                    success=False,
                    error=(
                        "ElevenLabs TTS returned audio without usable millisecond "
                        "timestamps; aborting to avoid subtitle/cut desync"
                        f"{suffix}"
                    ),
                )
            if sentence_tokens:
                import json as _json
                sidecar.write_text(
                    _json.dumps(sentence_tokens, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info(
                    "ElevenLabs timing sidecar wrote %d sentence tokens → %s",
                    len(sentence_tokens), sidecar.name,
                )
        except Exception as e:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            return TTSResult(
                success=False,
                error=f"ElevenLabs timestamp sidecar write failed: {e}",
            )

        logger.info(
            "ElevenLabs TTS synthesized %s (voice=%s, job=%s, cost=%s EUR)",
            out, vid, meta.get("job_id", "?"), meta.get("cost_eur", "?"),
        )
        return TTSResult(success=True, output_path=str(out))

    def _elevenlabs_direct_chunked(
        self, text: str, chunks: list[str], vid: str, el_key: str,
        speed: float, out: Path,
    ) -> TTSResult:
        """长文案分段直连 ElevenLabs 合成 → 拼接音频 → 合并字符级时间戳。
        结构对齐 `_synthesize_azure` 的分段循环:逐段合成(3 次重试)、失败插静音、
        按累计时长偏移合并 token、ffmpeg concat、单调性修复、写 .words.json。
        """
        import json as _json
        import shutil
        import subprocess
        import tempfile
        import time

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return TTSResult(success=False, error="ffmpeg not on PATH for ElevenLabs concat")

        seg_dir = Path(tempfile.mkdtemp(prefix="eleven_direct_tts_"))
        seg_paths: list[Path] = []
        all_tokens: list[dict] = []
        total_duration = 0.0
        # 密钥池 + 当前用到第几把。⚠️ `key_idx` 在**段与段之间保持** ——
        # 一旦某把 key 废了,后面所有段直接用新的,不再回头撞它。
        # 不这样的话,长文案 10 段 = 10 次白白的 401 + 退避等待。
        keys = self._elevenlabs_keys() or [el_key]
        key_idx = keys.index(el_key) if el_key in keys else 0
        failed_chunks = 0
        try:
            for index, chunk in enumerate(chunks):
                audio_bytes: bytes | None = None
                meta: dict = {}
                last_error = ""
                for attempt in range(3):
                    try:
                        audio_bytes, meta = self._elevenlabs_direct(
                            chunk, vid, keys[key_idx], speed)
                        if audio_bytes:
                            break
                        last_error = "empty payload"
                    except Exception as e:  # noqa: BLE001
                        last_error = repr(e)[:180]
                        # 🚨 这把 key 废了 → 换下一把,**并记住换过了**。
                        if (self._elevenlabs_key_exhausted(e)
                                and key_idx + 1 < len(keys)):
                            key_idx += 1
                            logger.warning(
                                "ElevenLabs 分段合成:第 %d 把 key 不可用,"
                                "换第 %d 把(第 %d 段)",
                                key_idx, key_idx + 1, index + 1)
                            continue      # 立刻用新 key 重试,不必等退避
                    if attempt < 2:
                        time.sleep(1.0 * (attempt + 1))

                seg = seg_dir / f"seg_{index:04d}.mp3"
                if audio_bytes:
                    seg.write_bytes(audio_bytes)
                    duration = self._probe_audio_duration(seg)
                    if duration <= 0:
                        duration = max(0.4, len(chunk) / 4.5)
                    alignment = meta.get("normalized_alignment") or meta.get("alignment")
                    chunk_tokens = self._elevenlabs_alignment_to_sentence_tokens(chunk, alignment)
                    if chunk_tokens:
                        chunk_tokens = self._clamp_tokens_to_duration(chunk_tokens, duration)
                        all_tokens.extend(self._shift_timing_tokens(chunk_tokens, total_duration))
                    else:
                        logger.warning(
                            "ElevenLabs chunk %d returned no usable timing; using even char timing",
                            index,
                        )
                        all_tokens.extend(self._even_char_tokens(
                            chunk, start=total_duration, duration=duration,
                        ))
                else:
                    failed_chunks += 1
                    duration = max(0.4, len(chunk) / 4.5)
                    logger.warning(
                        "ElevenLabs chunk %d failed after retries (%s); inserting %.2fs silence",
                        index, last_error, duration,
                    )
                    if not self._write_silence_mp3(seg, duration, ffmpeg):
                        continue
                    all_tokens.extend(self._even_char_tokens(
                        chunk, start=total_duration, duration=duration,
                    ))

                seg_paths.append(seg)
                total_duration += duration

            if not seg_paths:
                return TTSResult(success=False, error=f"ElevenLabs TTS: all {len(chunks)} chunks failed")
            if failed_chunks >= len(chunks):
                return TTSResult(
                    success=False,
                    error=f"ElevenLabs TTS: all {len(chunks)} chunks fell back to silence",
                )

            if len(seg_paths) == 1:
                out.write_bytes(seg_paths[0].read_bytes())
            else:
                list_file = seg_dir / "list.txt"
                list_file.write_text(
                    "\n".join(f"file '{p.as_posix()}'" for p in seg_paths),
                    encoding="utf-8",
                )
                r = subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                     "-i", str(list_file), "-c", "copy", str(out)],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    return TTSResult(
                        success=False,
                        error=f"ElevenLabs TTS concat failed: {(r.stderr or '')[-200:]}",
                    )

            if not all_tokens:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                return TTSResult(
                    success=False,
                    error="ElevenLabs TTS produced audio without usable timestamps",
                )
            if not self._timing_tokens_are_monotonic(all_tokens):
                logger.warning(
                    "ElevenLabs chunked TTS non-monotonic timestamps; repairing in place",
                )
                all_tokens = self._enforce_monotonic_tokens(all_tokens)
                if not all_tokens or not self._timing_tokens_are_monotonic(all_tokens):
                    all_tokens = self._even_char_tokens(
                        text, start=0.0,
                        duration=max(total_duration, self._probe_audio_duration(out), 0.4),
                    )
            out.with_suffix(".words.json").write_text(
                _json.dumps(all_tokens, ensure_ascii=False), encoding="utf-8",
            )
            logger.info(
                "ElevenLabs direct chunked synthesized %s voice=%s chunks=%d failed=%d dur=%.1fs",
                out, vid, len(chunks), failed_chunks, total_duration,
            )
            return TTSResult(success=True, output_path=str(out))
        finally:
            try:
                shutil.rmtree(seg_dir, ignore_errors=True)
            except Exception:
                pass

    @staticmethod
    def _log_elevenlabs_alignment_debug(meta: dict, alignment: object) -> None:
        """Log enough structure to debug cloud/ElevenLabs timestamp mismatches.

        Do not log the full text or full alignment arrays; these can be large
        and may contain user script content. The first five characters are
        enough to distinguish valid Chinese/English arrays from null or a
        schema wrapper problem.
        """
        chars = starts = ends = None
        if isinstance(alignment, dict):
            chars = alignment.get("characters")
            starts = alignment.get("character_start_times_seconds")
            ends = alignment.get("character_end_times_seconds")
        logger.info(
            "ElevenLabs timestamp meta debug: keys=%s alignment_type=%s "
            "chars_len=%s starts_len=%s ends_len=%s chars_first5=%s "
            "timestamp_error=%r",
            sorted(meta.keys()),
            type(alignment).__name__,
            len(chars) if isinstance(chars, list) else None,
            len(starts) if isinstance(starts, list) else None,
            len(ends) if isinstance(ends, list) else None,
            chars[:5] if isinstance(chars, list) else None,
            meta.get("timestamp_error"),
        )

    @staticmethod
    def _elevenlabs_alignment_to_sentence_tokens(
        text: str,
        alignment: object,
    ) -> list[dict]:
        """Convert ElevenLabs character timings to Media Buddy sentence tokens.

        The cloud endpoint returns per-character start/end arrays. The pipeline
        consumes sentence-level tokens, so we walk normalized characters and
        derive one token per script sentence.
        """
        if not isinstance(alignment, dict):
            return []
        chars = alignment.get("characters")
        starts = alignment.get("character_start_times_seconds")
        ends = alignment.get("character_end_times_seconds")
        if not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list):
            return []
        if not chars or len(chars) != len(starts) or len(chars) != len(ends):
            return []

        # Walk the ORIGINAL script text, borrowing per-character timing from the
        # alignment. Azure normalizes the alignment's `characters` array (it drops
        # punctuation — e.g. the "." in "43.3" → "433", commas, etc.), so it no
        # longer matches the script 1:1. The alignment is still an in-order
        # SUBSEQUENCE of the script, so we re-insert the dropped characters
        # (decimals/punctuation) at the adjacent boundary. This keeps the exact
        # script text in subtitles (43.3 stays 43.3) with real per-char timing.
        tokens: list[dict] = []
        ci = 0
        n = min(len(starts), len(ends), len(chars))
        last_end = 0.0
        try:
            last_end = float(starts[0]) if starts else 0.0
        except (TypeError, ValueError):
            last_end = 0.0
        for och in str(text):
            if not och:
                continue
            if ci < n and str(chars[ci]) == och:
                try:
                    start_f = float(starts[ci])
                    end_f = float(ends[ci])
                except (TypeError, ValueError):
                    ci += 1
                    continue
                if end_f < start_f:
                    end_f = start_f
                tokens.append({"kind": "char", "text": och,
                               "start": round(start_f, 3), "end": round(end_f, 3)})
                last_end = end_f
                ci += 1
            else:
                # char the alignment dropped (punctuation / decimal point) —
                # re-insert it at the current boundary so the script text survives.
                tokens.append({"kind": "char", "text": och,
                               "start": round(last_end, 3), "end": round(last_end, 3)})
        return tokens

        import re as _re

        def norm(s: str) -> str:
            return _re.sub(r"[\s　​‌‍]+", "", s)

        sentences = [
            s.strip()
            for s in _re.split(r"(?<=[。！？!?])\s+|[\r\n]+", text)
            if s.strip()
        ]
        if not sentences:
            sentences = [text.strip()] if text.strip() else []

        tokens: list[dict] = []
        char_idx = 0
        for sent in sentences:
            target = norm(sent)
            if not target:
                continue
            collected = ""
            first_idx = None
            last_idx = None
            while char_idx < len(chars) and len(collected) < len(target):
                ch = str(chars[char_idx])
                ch_norm = norm(ch)
                if ch_norm:
                    if first_idx is None:
                        first_idx = char_idx
                    collected += ch_norm
                    last_idx = char_idx
                char_idx += 1
            if first_idx is None or last_idx is None:
                return []
            tokens.append({
                "kind": "sentence",
                "text": sent,
                "start": round(float(starts[first_idx]), 4),
                "end": round(float(ends[last_idx]), 4),
            })
        return tokens

