# -*- coding: utf-8 -*-
"""智能对话/粘贴文案 两个入口的「制作参数记忆」接线守卫。


> 配音设定，长视频短视频里没有保存设定的，每次都要重新设定，经常弄错，还费时费力。

## 根因：存和读用了**两个不同的模式**

`WorkspacePages.tsx` 里，每个频道 × 长短 × 入口模式各存一份制作参数
（时长/配音/语速/字幕/输出语言/背景音乐），key 形如：

    media-buddy.entry-defaults.{channelId}.{short|long}.{chat|script}

**保存**走当前模式：

    saveEntryDefaults(channelId, 'short', assistantMode, {...})

**首帧初始化**却写死 `'chat'`：

    ...readEntryDefaults(channelId, 'short', 'chat')      ← 🔴 bug

于是客户在【粘贴文案】模式下存的设定，下次进来读的是【对话式】那份 —— 读不到。

更阴的是：下面那个「切模式时重载默认」的 `useEffect` 带着 `modeDefaultInit`
守卫会**跳过首帧**（本意是躲音色 hydration 竞态），所以首帧漏掉的值
**再也补不回来**，除非客户手动切一次模式再切回来。

## 这组守什么

**存和读必须同源。** 不测 UI、不测浏览器行为，只钉死这一条不变量 ——
它就是这次 bug 的全部。
"""

from __future__ import annotations

import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKSPACE = REPO / "src" / "frontend" / "src" / "pages" / "WorkspacePages.tsx"


def _src() -> str:
    return io.open(WORKSPACE, encoding="utf-8").read()


def test_首帧读默认不许把模式写死():
    """🚨 **本组核心。** 首帧初始化必须按「上次真正用的模式」读。

    写死 `'chat'` = 粘贴文案入口的设定永远读不回来，
    """

    bad = re.findall(r"readEntryDefaults\(\s*channelId\s*,\s*'(?:short|long)'\s*,\s*'(chat|script)'\s*\)",
                     _src())
    assert not bad, (
        "首帧初始化把入口模式写死成 %r —— 另一个入口存的设定会读不回来。\n"
        "应改成 readEntryDefaults(channelId, <长短>, readEntryMode(channelId, <长短>))"
        % sorted(set(bad)))


def test_存和读必须同源():
    """⚠️ 存用 `assistantMode`、读用 `readEntryMode` —— 两者必须指向同一份数据。

    `assistantMode` 的初始值本身就是 `readEntryMode(...)`，所以同源。
    但如果哪天有人把 `assistantMode` 的初始化改成别的来源，这条会红。
    """

    src = _src()
    # 每个 assistantMode 的 useState 初始值都必须来自 readEntryMode
    inits = re.findall(r"useState<'chat' \| 'script'>\(\(\) => ([^)]+\))\)", src)
    assert inits, "找不到 assistantMode 的初始化 —— 结构变了，这条守卫要跟着改"
    for i in inits:
        assert "readEntryMode" in i, f"assistantMode 初始值不是来自 readEntryMode: {i}"


def test_长短两条路都要修():
    """⚠️ 长视频和短视频是**两段几乎复制粘贴的代码**，改一边漏一边是这里的常态。

    两边都必须按「上次用的模式」读首帧默认。
    """

    src = _src()
    for vt in ("short", "long"):
        pat = r"readEntryDefaults\(channelId, '%s', readEntryMode\(channelId, '%s'\)\)" % (vt, vt)
        assert re.search(pat, src), f"{vt} 视频的首帧默认没有按上次的模式读"


def test_保存的字段要覆盖客户会改的那些():
    """

    ⚠️ 少存一个字段 = 那一项每次都要重设，而其他项正常 —— 这种「只丢一半」
       比全丢更难被发现。
    """

    src = _src()
    calls = re.findall(r"saveEntryDefaults\([^)]*?,\s*\{(.*?)\}\s*\)", src, re.S)
    assert len(calls) >= 2, f"应该有长短两处保存，找到 {len(calls)} 处"
    for body in calls:
        #    它当初是面板里的独立 `useState('normal')`,**在 config 之外**,
        #    所以整套保存机制碰不到它,客户每次进来都回到「普通」。
        #    这正是本条测试注释里说的「只丢一半」:音色记住了、档位没记住。
        for field in ("voiceProvider", "voiceSpeed", "outputLanguage",
                      "includeSubtitles", "includeMusic", "duration", "voiceTier"):
            assert field in body, f"保存时漏了 {field} —— 这一项客户每次都要重设"


def test_切模式重载跳过首帧是有意的():
    """📌 记录一个**不该被"顺手修掉"的设计**。

    `modeDefaultInit` 守卫故意跳过首帧，是为了躲开音色列表 hydration 的竞态
    （首帧音色还没加载完，这时候塞默认值会被随后的 hydration 覆盖）。

    ⚠️ 所以首帧的默认值**只能**由 useState 初始值提供 —— 这正是上面那条
       守卫必须存在的原因。谁要是把 `modeDefaultInit` 删了，
       得先确认 hydration 竞态已经不存在。
    """

    src = _src()
    assert "modeDefaultInit" in src, (
        "modeDefaultInit 守卫没了 —— 如果是有意删的，请确认音色 hydration "
        "竞态已解决，并把这条测试一起更新")


def test_高级英语档位必须放在config里():
    """🚨 `voiceTier` 一旦回到面板的独立 `useState`,保存机制又会碰不到它。


    ⚠️ 而且两个配置面板(`ShortConfigPanel` / `LongConfigPanel`)
       **都拿不到 `channelId`** —— 想在面板里自己存也存不了。
       所以「放进 config」不只是更干净,是唯一可行的路。
    """

    src = _src()
    assert "const [voiceTier, setVoiceTier] = useState" not in src, (
        "voiceTier 又变回独立 state 了 —— 它会绕过保存机制,客户每次进来都回到「普通」")
    assert "config.voiceTier" in src, "voiceTier 没从 config 里取"


