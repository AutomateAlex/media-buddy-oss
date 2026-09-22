# -*- coding: utf-8 -*-
"""音色注册表守卫：**一张表说了算，而且必须真的只有一张。**


在建注册表之前，音色定义散落在 **8 处**，靠注释里的「必须保持一致」人肉同步。

**一天之内这个病犯了两次，而且两次都是「客户选了 A、出来是 B」：**

|---|---|
| Google 高级英语音色被换成 `azure_aria` | 管线守卫只认识 Azure/ElevenLabs/千问三家 |
| 3 个中文 ElevenLabs 音色出来是同一个人 | 目录里有、运行时表里没有 → 兜底成 `mypick1` |

第二个是**建注册表的过程中自动暴露的** —— 藏了很久，没人发现。

## 这组守什么

全是**自动规则**，不是「我记得的那几个音色」：

1. 全仓不许再出现第二处音色名单定义
2. 客户能选的音色，运行时表里**必须**有对应的供应商 id
3. 已知损坏的音色不许偷偷放回目录
4. 下游那 8 张表必须真的从注册表派生
"""

from __future__ import annotations

import ast
import inspect
import io
import pathlib

import pytest

from backend.services import voice_registry as R
from backend.services.voice_registry import Voice

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"


# ── 一、注册表自身的完整性 ────────────────────────────────


def test_客户能选的音色运行时必须认得():
    """🚨 **本组最重要的一条 —— 它就是那个「三个音色一个声音」的 bug。**

    目录里有、运行时表里没有 → 合成时兜底成别人的声音，
    **不报错、不告警**，客户只会觉得「这音色怎么和试听不一样」。
    """

    bad = [v.key for v in R.VOICES if v.catalog and not v.native]
    assert not bad, (
        "这些音色客户能选，但没有供应商 id，合成时会变成别人的声音：\n  "
        + "\n  ".join(bad))


def test_key不许重复():
    """一个 key 两条记录 → 查表结果取决于顺序，是最难查的那种 bug。"""

    keys = [v.key for v in R.VOICES]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"重复的 key：{dupes}"
    assert len(R.BY_KEY) == len(R.VOICES), "BY_KEY 和 VOICES 数量对不上"


def test_已知损坏的不许放回目录():
    """⚠️ `KNOWN_BROKEN` 里那三个会播成 `mypick1`。

    要恢复它们，得**先补上真实的 ElevenLabs voice id**，不是把 key 加回目录。
    """

    back = [v.key for v in R.VOICES if v.key in R.KNOWN_BROKEN]
    assert not back, f"已知损坏的音色被放回注册表了：{back}"


def test_客户可见的音色字段齐全():
    """少一个字段前端下拉框就出现空白项。"""

    for v in R.VOICES:
        if not v.catalog:
            continue
        assert v.display_name.strip(), f"{v.key} 没有显示名"
        assert v.gender.strip(), f"{v.key} 没有性别（头像颜色靠它）"


def test_语言认不出来时返回空串而不是瞎猜():
    """🚨 **返回 `""` 的含义是「不知道」，不是「不会说英文」。**

    守卫拿到 `""` 应当**放行**。
    """

    assert R.voice_language("完全不存在的音色") == ""
    assert R.voice_language("") == ""
    assert R.voice_language(None) == ""          # type: ignore[arg-type]


# ── 二、下游必须真的从注册表派生 ──────────────────────────


@pytest.mark.parametrize("mod,name,vendor", [
    ("backend.services.tts_service", "AZURE_VOICES", "azure"),
    ("backend.services.tts_service", "QWEN_VOICES", "qwen"),
    ("backend.services.tts_service", "ELEVENLABS_VOICES", "elevenlabs"),
])
def test_运行时表是派生的(mod, name, vendor):
    """派生 = 加音色只改注册表一处。写死 = 又回到八处同步的老路。"""

    import importlib

    table = getattr(importlib.import_module(mod), name)
    assert table == R._native_map(vendor), f"{name} 和注册表对不上"


@pytest.mark.parametrize("name,catalog", [
    ("_QWEN_ZH_CATALOG", "qwen_zh"),
    ("_ELEVENLABS_EN_CATALOG", "elevenlabs_en"),
])
def test_客户目录是派生的(name, catalog):
    """⚠️ 顺序也要一致 —— 那是前端下拉框的顺序，人工排过的。"""

    from backend.api import tts as T

    got = getattr(T, name)
    assert got == R.catalog_for(catalog), f"{name} 和注册表对不上（含顺序）"


def test_管线不许再内联写死音色名单():
    """🚨 `pipeline_service` 里原本有**两处**内联的 15 个 ElevenLabs 名字，

    注释写着「必须与 api/tts.py 的 _ELEVENLABS_EN_CATALOG、
    上面长视频例外的内联集合**三处一致**」—— 三处人肉同步就是三处漂移风险。
    """

    from backend.services import pipeline_service as P

    src = inspect.getsource(P)
    # 内联集合的特征：同一行里出现两个以上写死的音色 id
    assert '"elevenlabs_rachel", "elevenlabs_bella"' not in src, \
        "又把音色名单内联写死回去了 —— 请从 voice_registry 派生"
    assert "_registry_en_eleven()" in src, "没有从注册表派生"


# ── 三、全仓不许再出现第二处名单 ──────────────────────────


def _hardcoded_voice_lists() -> list[str]:
    """扫全仓，找出**注册表之外**还在成规模写死音色 id 的地方。

    判据：一个字面量集合/列表/字典里出现 **≥5 个** `xxx_yyy` 形式的音色 id。
    5 个以上才算「名单」，零星引用（默认音色、兜底）不算。
    """

    import re

    VOICE_ID = re.compile(r'^(qwen|azure|elevenlabs|hdv)_[a-z0-9_]+$')
    #    `tts_pacing._MEASURED_RATE_1X` / `studio_agent.VOICE_WPS` 是每个音色的
    #    它们回答的是「这个音色说多快」,不是「有哪些音色」——搬进注册表会丢掉出处。
    #    真正的风险不是「两张表并存」,是「两张表打架」→ 由
    #    `test_别处提到的音色注册表都得认识` 守住。
    EXEMPT = {"tts_pacing.py", "studio_agent.py"}
    out: list[str] = []
    for path in (BACKEND).rglob("*.py"):
        if ("__pycache__" in str(path) or path.name == "voice_registry.py"
                or path.name in EXEMPT):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            vals = []
            if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                vals = [e.value for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            elif isinstance(node, ast.Dict):
                vals = [k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            hits = [v for v in vals if VOICE_ID.match(v)]
            if len(hits) >= 5:
                out.append(f"{path.relative_to(REPO)}:{node.lineno}  {hits[:3]}… 共 {len(hits)} 个")
    return out


def test_全仓只有注册表一处名单():
    """🚨 **这条是整个重构的验收标准。**

    其他地方联动着就一并处理掉了，不会出现这种漏一个就出现 bug 的情况。」

    ⚠️ 自动规则：以后谁再在别处贴一张音色名单，这条会自己红。
    """

    extra = _hardcoded_voice_lists()
    assert not extra, (
        "注册表之外还有这些地方写死了音色名单，它们迟早会和注册表漂移：\n  "
        + "\n  ".join(extra)
        + "\n→ 改成从 backend.services.voice_registry 派生")


def test_别处提到的音色注册表都得认识():
    """🚨 **这条才是真正的防漂移闸。**

    **打架才有问题**:标定表里写了一个注册表没有的音色 = 那条数据永远用不上;
    或者音色名打错一个字母 = 静默退回默认语速,脚本长度算错,没人会发现。

    ⚠️ 这条就是那 3 个孤儿音色(`elevenlabs_yichen` 等)的泛化守卫 ——
       它们当初正是「目录里有、运行时表里没有」。
    """

    from backend.lib.tts_pacing import _MEASURED_RATE_1X
    from backend.services.studio_agent import VOICE_WPS

    import re as _re

    # 只查**长得像具体音色**的键。`edge_*` / `openai_tts` / `piper_tts` /
    # `_default` 是老的**通用兜底档**（按供应商而非按音色给速率），不是注册表条目。
    LOOKS_LIKE_VOICE = _re.compile(r"^(qwen|azure|elevenlabs|hdv)_(?!tts$)[a-z0-9_]+$")

    unknown = []
    for name, table in (("tts_pacing._MEASURED_RATE_1X", _MEASURED_RATE_1X),
                        ("studio_agent.VOICE_WPS", VOICE_WPS)):
        for k in table:
            if not LOOKS_LIKE_VOICE.match(str(k)):
                continue
            # ⚠️ `KNOWN_BROKEN` 里的算「已登记在案」——它们在注册表里被**故意**排除，
            #    标定表里残留的那条是死数据（永远查不到），无害，不必强删。
            if k in R.BY_KEY or k in R.KNOWN_BROKEN:
                continue
            unknown.append(f"{name}: {k}")
    assert not unknown, (
        "这些音色在标定表里有、注册表里没有 —— 要么是打错字，要么是删音色时漏了：\n  "
        + "\n  ".join(unknown))


def test_客户目录里不许有两个同性格的音色():
    """

    原来的 15 个高级英语音色里有**两对同性格**:

        Firm 坚定    Orus(美音男) + Kore(美音女)
        Clear 清晰   Iapetus(美音男) + Erinome(美音女)

    技术上没毛病(8 段音频 md5/解码 PCM/时长三项全不同),
    **选品上是错的** —— 用户盲听后直接说「有几个是一模一样的」。
    客户看到 8 个美音,实际只听得出 5~6 种,等于白给了几个选项。

    ⚠️ 只在**同一个目录 + 同一个口音**内查重:
       美音的 Firm 和英音的 Firm 是两种听感,不算重复。
    """

    from collections import Counter

    by_group: dict[tuple[str, str], list[Voice]] = {}
    for v in R.VOICES:
        if not v.catalog:
            continue
        accent = str(v.native[0]) if isinstance(v.native, tuple) else ""
        by_group.setdefault((v.catalog, accent), []).append(v)

    dupes = []
    for (cat, accent), vs in by_group.items():
        chars = [v.character for v in vs if v.character]
        for ch, n in Counter(chars).items():
            if n > 1:
                who = [v.key for v in vs if v.character == ch]
                dupes.append(f"{cat}/{accent} 里 {n} 个「{ch}」: {who}")
    assert not dupes, (
        "客户目录里有同性格的音色，听起来会像同一个人：\n  " + "\n  ".join(dupes))


def test_同一个目录里不许出现同一个人名():
    """⚠️ 同一个音色名配不同口音是**两条记录**（Puck 美音 / Puck 英音）。

    同时放进同一个目录会让客户看到两个「Puck」，分不清差别在哪。
    """

    from collections import Counter

    for cat in {v.catalog for v in R.VOICES if v.catalog}:
        names = [str(v.native[1]) if isinstance(v.native, tuple) else v.key
                 for v in R.VOICES if v.catalog == cat]
        dupes = [n for n, c in Counter(names).items() if c > 1]
        assert not dupes, f"目录 {cat} 里有重复人名：{dupes}"
