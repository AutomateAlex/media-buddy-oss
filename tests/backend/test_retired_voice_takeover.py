# -*- coding: utf-8 -*-
"""停用供应商的**自动接管**:客户存着的老音色不能把出片带死。


配音设定是**存下来的**。客户几个月前选了 `elevenlabs_adam`,这个值就一直躺在
`projects.tts_provider` 里 —— 我们停用 ElevenLabs 和 Azure 之后,
**存量设定不会自己变**。

于是出片走进一条死链:

    ElevenLabs 音色 → `_wants_elevenlabs=True` → **跳过千问、跳过谷歌**

最阴的是那句「跳过千问、跳过谷歌」:它当初是为了防**安慰剂开关**
(客户选了高级英语却听到千问),现在却变成主动绕开唯一还活着的两条路。


## 这组守什么

1. **一个都不能漏** —— 漏一个就是那批客户永远出不了片
3. **接管必须在死链之前** —— 放晚了等于没放
4. **同一个客户永远同一个声音** —— 不能每次出片换个人
"""

from __future__ import annotations

import io
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_每个停用音色都有人接管():
    """🚨 **本组核心。** 漏一个 = 那批客户的出片永远挂在 TTS 上。"""

    from backend.services.voice_registry import legacy_voices, replacement_for

    orphans = [v.key for v in legacy_voices() if not replacement_for(v.key)]
    assert not orphans, (
        "这些停用音色没人接管 —— 还存着它们的客户会一直出片失败:\n  %s" % orphans)


def test_接管不许改计费档():
    """🚨 **钱的事,零容忍。**

    反过来 = 我们白送。所以 `premium_english` 是**硬条件**,不是尽量。
    """

    from backend.services.voice_registry import BY_KEY, legacy_voices, replacement_for

    bad = [(v.key, replacement_for(v.key)) for v in legacy_voices()
           if BY_KEY[replacement_for(v.key)].premium_english != v.premium_english]
    assert not bad, f"接管改了 premium_english 档：{bad}"


def test_接管出来的必须是在用的音色():
    """⚠️ 接到另一个停用音色上 = 换了个死法,一样出不了片。"""

    from backend.services.voice_registry import BY_KEY, legacy_voices, replacement_for

    bad = [(v.key, r) for v in legacy_voices()
           if (r := replacement_for(v.key)) and BY_KEY[r].legacy]
    assert not bad, f"接管到了另一个**停用**音色上：{bad}"


def test_在用的音色一个都不许被换掉():
    """📌 客户现在选的是好的,碰它就是无缘无故换掉他的声音。"""

    from backend.services.voice_registry import active_voices, replacement_for

    bad = [v.key for v in active_voices() if replacement_for(v.key)]
    assert not bad, f"在用的音色被接管了：{bad}"


def test_不认识的音色不许乱接():
    """⚠️ fail-safe：拿不准就别动。"""

    from backend.services.voice_registry import replacement_for

    for k in ("", "  ", "完全不存在的音色", "elevenlabs_不存在"):
        assert replacement_for(k) is None, k


def test_同一个客户永远同一个声音():
    """🚨 **跨进程必须稳定。**

    Python 的 `hash()` 每个进程都不一样(PYTHONHASHSEED 随机),
    用它挑替身会让同一个客户**每次出片换一个人说话** —— 比出不了片更诡异,
    而且单进程测试**永远发现不了**。所以这条真开三个子进程比。
    """

    code = (
        "import sys; sys.path.insert(0, %r);"
        "from backend.services.voice_registry import replacement_for as f;"
        "print(f('elevenlabs_adam'), f('elevenlabs_amy'), f('azure_aria'))"
        % str(REPO / "src")
    )
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True).stdout.strip() for _ in range(3)}
    assert len(outs) == 1, f"三个进程挑出了不同的替身，客户每次出片换个人：{outs}"
    assert outs != {""}, "子进程根本没跑起来"


def test_接管必须发生在死链之前():
    """🚨 **顺序就是全部。**

    `_wants_elevenlabs` 一旦为真,千问和谷歌两条活路就已经被绕过去了。
    接管放在它后面 = 等于没放,而且**测试照样能全绿**(只测映射表的话)。
    """

    src = io.open(REPO / "src" / "backend" / "services" / "tts_service.py",
                  encoding="utf-8").read()
    take = src.find("replacement_for(provider)")
    dead = src.find("_wants_elevenlabs = provider")
    assert take != -1, "找不到接管那行 —— 结构变了，这条守卫要跟着改"
    assert dead != -1, "找不到 _wants_elevenlabs —— 结构变了"
    assert take < dead, (
        "接管被挪到 `_wants_elevenlabs` 后面了 —— 那时候活路已经被绕过去，"
        "接管等于没做")


def test_接管挂了也不许挡出片():
    """⚠️ fail-open：接管只是锦上添花，它自己出错不该让客户的片子死掉。"""

    import inspect

    from backend.services.tts_service import TTSService

    src = inspect.getsource(TTSService.synthesize)
    i = src.find("replacement_for(provider)")
    assert i != -1
    window = src[max(0, i - 400): i + 500]
    assert "except Exception" in window, "接管没有兜底 —— 它一抛异常整条出片就死了"
