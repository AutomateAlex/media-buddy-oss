"""简体 → 繁体字幕转换(OpenCC)。

字幕语言选「繁体」时,先拿到简体基线文本,再用 OpenCC 做字形/用词转换。
默认配置 ``s2twp``(简 → 台湾正字 + 台湾惯用词:视频→影片、软件→軟體、鼠标→滑鼠);
可用 env ``MEDIA_BUDDY_S2T_CONFIG`` 切 ``s2t``(仅字形)/ ``s2hk``(港式)等。

设计:懒加载 + 进程内缓存 converter;OpenCC 缺失或转换失败 → 原样返回简体
(记 warning),绝不因为简繁转换炸掉出片。
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_S2T_CONFIG = "s2twp"


def _config_name() -> str:
    return (os.environ.get("MEDIA_BUDDY_S2T_CONFIG") or DEFAULT_S2T_CONFIG).strip() or DEFAULT_S2T_CONFIG


@lru_cache(maxsize=4)
def _converter(config: str):
    """Build (and cache) an OpenCC converter for ``config``. Returns ``None`` when
    OpenCC is unavailable or the config can't be loaded — callers then degrade to
    Simplified. Cached per-config; import state is fixed for a process's lifetime."""
    try:
        from opencc import OpenCC  # opencc-python-reimplemented (pure-Python, no system deps)
    except Exception:
        logger.warning("OpenCC not installed; Traditional subtitles fall back to Simplified")
        return None
    for name in (config, f"{config}.json"):  # reimplemented takes 's2twp'; C++ binding may want '.json'
        try:
            return OpenCC(name)
        except Exception:
            continue
    logger.warning("OpenCC config %r failed to load; falling back to Simplified", config)
    return None


def to_traditional(text: str) -> str:
    """Convert one Simplified-Chinese string to Traditional. Safe: returns the input
    unchanged when OpenCC is missing/failing or the text is empty."""
    if not text:
        return text
    cc = _converter(_config_name())
    if cc is None:
        return text
    try:
        return cc.convert(text)
    except Exception:
        logger.warning("OpenCC convert failed; keeping Simplified", exc_info=True)
        return text


@lru_cache(maxsize=2)
def _t2s_converter():
    """OpenCC Traditional→Simplified converter (cached). ``None`` when OpenCC is
    unavailable — callers then keep the original text."""
    try:
        from opencc import OpenCC
    except Exception:
        logger.warning("OpenCC not installed; TTS Traditional→Simplified normalization is a no-op")
        return None
    for name in ("t2s", "t2s.json"):
        try:
            return OpenCC(name)
        except Exception:
            continue
    logger.warning("OpenCC t2s config failed to load; keeping original text")
    return None


def to_simplified(text: str) -> str:
    """Convert Traditional (or mixed) Chinese to Simplified — used to normalize TTS
    input. Traditional characters fed to a Mainland generative voice (qwen3-tts) make
    the model drift into foreign accents / slurred delivery, so narration is always
    synthesized from Simplified. Subtitles keep their own language (Simplified baseline
    → ``to_traditional`` when the project asks for Traditional). Safe: returns the input
    unchanged when OpenCC is missing/failing or the text is empty."""
    if not text:
        return text
    cc = _t2s_converter()
    if cc is None:
        return text
    try:
        return cc.convert(text)
    except Exception:
        logger.warning("OpenCC t2s convert failed; keeping original", exc_info=True)
        return text


def to_traditional_batch(texts: list[str]) -> list[str]:
    """Convert a list of Simplified strings to Traditional, order- and length-preserving."""
    cc = _converter(_config_name())
    if cc is None:
        return list(texts or [])
    out: list[str] = []
    for t in (texts or []):
        if not t:
            out.append(t)
            continue
        try:
            out.append(cc.convert(t))
        except Exception:
            out.append(t)
    return out
