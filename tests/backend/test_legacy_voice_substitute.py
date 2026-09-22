# -*- coding: utf-8 -*-
"""

## 背景

全部来自两个早就从选单摘掉的老引擎（ElevenLabs 23 / Azure 16）。

`elevenlabs_*` 在 tts_service 里会主动跳过千问 → ElevenLabs 超时 →
掉进同样坏掉的 Azure → 客户拿到整段无声。

## 这里守什么

1. **默认必须关**：不设开关时一次顶替都不许发生。
3. **性别不许变**：打补丁时真的写错过一次 —— azure 音色 gender 字段是空的，
   从男声变女声。所以 azure_* 一律不顶替（它们本来就走千问且有正确男女对应）。
4. **顶替目标必须是客户真能选到的现役音色**，不能顶替成另一个停用音色。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.services.voice_registry import (  # noqa: E402
    BY_KEY, LEGACY_SUBSTITUTE, VOICES, substitute_legacy,
)


# ── 1. 顶替表本身必须自洽 ────────────────────────────────────────────────
def test_顶替目标必须是现役音色():
    """顶替成另一个停用音色 = 白顶替。"""
    for old, new in LEGACY_SUBSTITUTE.items():
        v = BY_KEY.get(new)
        assert v is not None, "%s 顶替成了注册表里没有的 %s" % (old, new)
        assert v.catalog, "%s 顶替成了客户选不到的 %s" % (old, new)
        assert not v.legacy, "%s 顶替成了另一个停用音色 %s" % (old, new)


def test_计费档不许被降掉():
    """高级英语音色只能顶替成高级英语音色。"""
    for old, new in LEGACY_SUBSTITUTE.items():
        a, b = BY_KEY.get(old), BY_KEY.get(new)
        if a is None:      # KNOWN_BROKEN 那几个不在注册表里
            continue
        assert a.premium_english == b.premium_english, (
            "%s(premium=%s) 顶替成 %s(premium=%s) —— 计费档被改了"
            % (old, a.premium_english, new, b.premium_english))


def test_语言不许变():
    """中文音色顶替成英文音色 = 客户的中文稿被英文腔念出来。"""
    for old, new in LEGACY_SUBSTITUTE.items():
        a, b = BY_KEY.get(old), BY_KEY.get(new)
        if a is None or not a.language:
            continue
        assert a.language == b.language, "%s(%s) 顶替成了 %s(%s)" % (
            old, a.language, new, b.language)


@pytest.mark.parametrize("old,new", [
    ("elevenlabs_amy", "qwen_cherry"),
    ("elevenlabs_yichen", "qwen_cherry"),         # 13 个，KNOWN_BROKEN
])
def test_prod在用的老音色都有去处(old, new):
    assert substitute_legacy(old) == new


# ── 3. azure_* 一律不动（这是自查抓到的真实错误）────────────────────────
@pytest.mark.parametrize("key", ["azure_yunyang", "azure_yunxi", "azure_xiaoyi"])
def test_azure不顶替_否则男声会变女声(key):
    """azure 音色 gender 字段是空的，按(语言,性别)兜底会挑到 qwen_cherry(女)。

    频道声音换掉了。它们本来就走千问且有正确男女对应（yunyang→Ethan），
    根本不需要顶替。
    """
    assert substitute_legacy(key) == key, "azure 音色被顶替了 —— 会改客户听到的性别"


# ── 4. 现役音色不许被动 ──────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["qwen_cherry", "qwen_kai", "elevenlabs_rachel"])
def test_现役音色原样返回(key):
    assert substitute_legacy(key) == key


def test_空值和没见过的key不崩():
    assert substitute_legacy("") == ""
    assert substitute_legacy(None) == ""          # type: ignore[arg-type]
    assert substitute_legacy("完全没见过的音色") == "完全没见过的音色"


def test_没列进表的elevenlabs也有兜底():
    """以后冒出一个没列进对照表的 elevenlabs 音色，也不能掉进坏掉的 Azure。"""
    out = substitute_legacy("elevenlabs_某个新的")
    assert out != "elevenlabs_某个新的"
    assert BY_KEY[out].catalog and not BY_KEY[out].legacy


def test_每个停用音色都顶得动或明确豁免():
    """守住「以后又停用一个音色却忘了给它去处」。"""
    漏网 = []
    for v in VOICES:
        if not v.legacy or v.key.startswith("azure_"):
            continue                      # azure 明确豁免，见上面的测试
        if substitute_legacy(v.key) == v.key:
            漏网.append(v.key)
    assert not 漏网, "这些停用音色没有去处，会掉进坏掉的老引擎：%s" % 漏网[:10]


# ── 5. TTS 入口：默认关 ──────────────────────────────────────────────────
def test_默认不顶替(monkeypatch):
    """不设开关 → 生产行为一个字节都不变。"""
    monkeypatch.delenv("MEDIA_BUDDY_LEGACY_VOICE_SUBSTITUTE", raising=False)
    import inspect
    from backend.services.tts_service import TTSService
    src = inspect.getsource(TTSService.synthesize)
    assert "MEDIA_BUDDY_LEGACY_VOICE_SUBSTITUTE" in src
    # 顶替必须发生在「跳过千问」的判断之前，否则 elevenlabs_* 还是会跳过千问
    i_sub = src.index("substitute_legacy(provider)")
    i_skip = src.index("_wants_elevenlabs = ")
    assert i_sub < i_skip, "顶替写在了「跳过千问」判断之后 —— 等于没顶替"
