# -*- coding: utf-8 -*-
"""ElevenLabs 密钥池:一把用完自动换下一把。


备用的 Azure 又「所有 chunk 静音」(Azure 已弃用)→


单点密钥 = 单点故障。而这个单点**已经炸过一次**。
"""

from __future__ import annotations

import os

import pytest

from backend.services.tts_service import TTSService


def _env(**kv):
    """临时设置环境变量,退出时恢复。"""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        old = {k: os.environ.get(k) for k in kv}
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return ctx()


# ── 密钥池的读取 ──────────────────────────────────────────

def test_single_key_still_works():
    """⚠️ 老配置(只有一把)必须**行为完全不变** —— 不能因为加了池子就要求改配置。"""

    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY="k1",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2=None):
        assert TTSService._elevenlabs_keys() == ["k1"]


def test_numbered_env_vars_are_picked_up_in_order():
    """顺序就是优先级:主键先用,用完才用备用。"""

    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY="k1",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2="k2",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_3="k3"):
        assert TTSService._elevenlabs_keys() == ["k1", "k2", "k3"]


def test_comma_separated_also_works():
    """一个变量里塞多把也支持 —— 服务器上少改一行配置。"""

    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY="k1, k2 ,k3",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2=None):
        assert TTSService._elevenlabs_keys() == ["k1", "k2", "k3"]


def test_duplicates_are_dropped():
    """⚠️ 同一把配两遍时不许白白多试一次 —— 那是 2 倍的 401 等待。"""

    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY="k1,k2",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2="k1"):
        assert TTSService._elevenlabs_keys() == ["k1", "k2"]


def test_no_key_returns_empty_not_crash():
    """一把都没配 → 空列表(调用方会退到云网关),**不许抛异常**。"""

    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY=None,
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2=None):
        assert TTSService._elevenlabs_keys() == []


def test_blank_and_whitespace_are_ignored():
    with _env(MEDIA_BUDDY_ELEVENLABS_API_KEY="  ,  , k1 ,,",
              MEDIA_BUDDY_ELEVENLABS_API_KEY_2="   "):
        assert TTSService._elevenlabs_keys() == ["k1"]


# ── 什么算「这把 key 废了」 ────────────────────────────────

@pytest.mark.parametrize("msg", [
    # 🚨 逐字复刻生产日志里的那一条
    'HTTPStatusError("Client error \'401 Unauthorized\' for url '
    '\'https://api.elevenlabs.io/v1/text-to-speech/VR6AewLTigWG4xSOukaG/with-timestamps\'")',
    "HTTPStatusError('429 Too Many Requests')",
    "RuntimeError('quota exceeded')",
    "Exception('Unauthorized')",
])
def test_key_level_failures_trigger_switch(msg):
    assert TTSService._elevenlabs_key_exhausted(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "ReadTimeout('timed out')",
    "ConnectError('connection refused')",
    "HTTPStatusError('500 Internal Server Error')",
    "HTTPStatusError('503 Service Unavailable')",
])
def test_transient_failures_do_not_trigger_switch(msg):
    """🚨 网络抖动/超时/5xx **不许换 key**。

    换了也没用 —— 服务端挂了不是密钥的问题;
    而且换过去会把第二把 key 也拖进同一场重试风暴,
    等于**把两把 key 一起烧掉**。
    """

    assert TTSService._elevenlabs_key_exhausted(Exception(msg)) is False


# ── 接线:真的会换吗 ──────────────────────────────────────

def test_the_switch_is_wired_into_both_paths():
    """🚨 短文案走单发、长文案走分段 —— **两条路都要能换**。

    只接单发的话,长视频照样全挂(而长视频恰恰是 ElevenLabs 高级英语音色的主场)。
    用源码断言,因为真正跑一次要联网 + 花钱。
    """

    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "backend" / "services" / "tts_service.py").read_text(
        encoding="utf-8-sig")

    # 单发路径:逐把试
    assert "for idx, k in enumerate(el_keys):" in src, "单发路径没接密钥池"
    assert "self._elevenlabs_direct(text, vid, k, speed)" in src

    # 分段路径:带 key_idx,且段与段之间保持
    assert "keys[key_idx]" in src, "分段路径还在用单把 key"
    assert "key_idx += 1" in src, "分段路径不会换 key"

    # 两条路都只在「密钥级故障」时换
    assert src.count("_elevenlabs_key_exhausted(e)") >= 2, "有路径没判故障类型"


def test_old_single_key_env_name_is_unchanged():
    """⚠️ 变量名不许改 —— 用户的 .env 里已经配着它,改名 = 当场全挂。"""

    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "backend" / "services" / "tts_service.py").read_text(
        encoding="utf-8-sig")
    assert 'MEDIA_BUDDY_ELEVENLABS_API_KEY"' in src
