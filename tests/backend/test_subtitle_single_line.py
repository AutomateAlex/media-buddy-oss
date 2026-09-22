# -*- coding: utf-8 -*-
"""

## 口径怎么变的

先按「画面上只保留一条」做成了**强制单行**。出样张给用户看之后他改了要求：

> 我们要给客户一整句，但是字体不能太小，如果有必要两行那就两行，
> 字体要客户可以调整。

强制单行 = 一条只能放 7 个字（标准字号下一行的容量），要么把句子切碎、
要么把字缩小 —— 两个代价都不接受。所以现在：

- 一条装**一整句**（到逗号为止），装不下才切，**最多两行**
- 字号仍由客户拖拉杆控制（`subtitle_font_scale`），这里一个字都不动它
  **不许从数字中间切开**、**不许把虚词甩在句尾**

## 这里测的是真结果

真拿长句跑「切条 → 换行」，看切出来的每一条长什么样，不是只检查常量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.services.pipeline_service import PipelineService as PS  # noqa: E402

NEWLINE = chr(92) + "N"          # ASS 的换行标记
PER_LINE = PS._SUB_PER_LINE


LONG_ZH = (
    "帝国大厦每年被闪电击中大约二十五次，其中最猛烈的一次发生在夏季雷暴期间，"
    "整栋楼的避雷系统在不到一秒内把电流导入地下，楼里的人几乎毫无察觉。"
)
LONG_EN = (
    "The Empire State Building is struck by lightning about twenty five times a year, "
    "and the most violent strike happened during a summer thunderstorm when the entire "
    "lightning protection system channelled the current into the ground in under a second."
)


def _cues(text: str, start: float = 0.0, end: float = 20.0):
    return PS._split_long_sentence(text, start, end, PS.MAX_CHARS_PER_CUE)


@pytest.mark.parametrize("text", [LONG_ZH, LONG_EN])
def test_最多两行不许三行(text):
    """两行可以（用户认了），三行不行 —— 那才是他说的「堆叠成两排、三排」。"""
    cues = _cues(text)
    assert len(cues) > 1, "这么长的句子居然没被拆开，切分根本没跑"
    for t, _s, _e in cues:
        lines = PS._wrap_cjk_for_ass(t, max_per_line=PER_LINE).count(NEWLINE) + 1
        assert lines <= 2, "这一条占了 %d 行：%r" % (lines, t)


@pytest.mark.parametrize("text", [LONG_ZH, LONG_EN])
def test_拆开之后字一个都不能少(text):
    """按时间拆分不等于可以丢字。"""
    joined = "".join(t for t, _s, _e in _cues(text))
    strip = lambda s: "".join(s.split())  # noqa: E731
    assert strip(joined) == strip(text), "拆分把字弄丢了或弄重了"


@pytest.mark.parametrize("text", [LONG_ZH, LONG_EN])
def test_时间是顺着走的不许重叠(text):
    cues = _cues(text, 3.0, 23.0)
    prev = 3.0 - 1e-6
    for t, s, e in cues:
        assert s >= prev - 1e-6, "第 %r 条的开始时间往回跳了" % t[:10]
        assert e >= s, "第 %r 条的结束时间早于开始时间" % t[:10]
        prev = s
    assert cues[0][1] >= 3.0 - 1e-6 and cues[-1][2] <= 23.0 + 1e-6, "拆出来的时间跑出了句子的区间"


def test_英文不许从词中间切开():
    """「navigation」被切成「naviga」+「tion」是历史上真出过的严重破句。"""
    words = set(LONG_EN.replace(",", "").split())
    for t, _s, _e in _cues(LONG_EN):
        for w in t.replace(",", "").split():
            assert w in words, "把词切碎了：%r" % w


def test_中文断句尽量走标点或词边界():
    """不通顺的断句客户一眼就看得出来。这里只卡最容易犯的：不许以标点开头。"""
    for t, _s, _e in _cues(LONG_ZH):
        assert t.strip()[0] not in "，。！？、；：,.!?;:", "一条字幕以标点开头：%r" % t


def test_两个上限必须同源():
    """写死 7 的话，以后有人把 PER_LINE 调成 5，字幕又会叠回两行。"""
    import inspect
    src = inspect.getsource(PS)
    head = src[:src.index("MAX_CHARS_PER_CUE_EN")]
    assert "_SUB_PER_LINE" in head.split("MAX_CHARS_PER_CUE")[-1], \
        "每条字幕的上限没有从 PER_LINE 派生"
    assert PS.MAX_CHARS_PER_CUE <= int(PER_LINE) * 2, \
        "每条的字数超过两行装得下的量 —— 会叠成三行"


def test_旧的18字盲切路径已经接上统一切分():
    """`_write_srt_from_sentence_tokens` 以前写死 18 字、还从词中间硬切。

    """
    import inspect
    src = inspect.getsource(PS._write_srt_from_sentence_tokens)
    assert "max_chars: int = 18" not in src, "还写死着 18 字"
    assert "_split_long_sentence" in src, "没接上统一的切分逻辑"
    assert "text[i:i + max_chars]" not in src, "还在从中间盲切"


def test_兜底折行了要能查到():
    """真折了行只记录、不改行为（叠两行仍然比冲出画面强），但必须留痕。"""
    import inspect
    src = inspect.getsource(PS._warn_if_stacked)
    assert "logger.warning" in src
    assert "STACKED" in src, "没有能 grep 的标记"


def test_从句子时间戳直接建srt也不超过两行(tmp_path):
    """真跑 `_write_srt_from_sentence_tokens`（改动最大的那个函数）。

    它以前写死 18 字、还从词中间盲切 —— 只看源码的守卫挡不住行为回归。
    """
    import json
    sidecar = tmp_path / "tokens.json"
    sidecar.write_text(json.dumps([
        {"kind": "sentence", "text": LONG_ZH, "start": 0.0, "end": 18.0},
        {"kind": "sentence", "text": "短句。", "start": 18.0, "end": 19.2},
        {"kind": "sentence", "text": LONG_EN, "start": 19.2, "end": 40.0},
    ], ensure_ascii=False), encoding="utf-8")
    srt = tmp_path / "out.srt"
    assert PS._write_srt_from_sentence_tokens(sidecar, srt) is not None

    blank = chr(10) + chr(10)          # 空行分隔一条条 cue
    blocks = [b.splitlines() for b in srt.read_text(encoding="utf-8").split(blank) if b.strip()]
    assert len(blocks) >= 6, "长句没被拆开"

    prev_end = -1.0
    for b in blocks:
        text = " ".join(b[2:]).strip()
        lines = PS._wrap_cjk_for_ass(text, max_per_line=PER_LINE).count(NEWLINE) + 1
        assert lines <= 2, "这一条占了 %d 行：%r" % (lines, text)
        # 时间必须顺着走，不许重叠（重叠 = 两条字幕同时出现在画面上）
        lo, hi = b[1].split(" --> ")
        to_s = lambda x: (int(x[0:2]) * 3600 + int(x[3:5]) * 60  # noqa: E731
                          + float(x[6:].replace(",", ".")))
        assert to_s(lo) >= prev_end - 1e-3, "第 %r 条和上一条时间重叠了" % text[:12]
        prev_end = to_s(hi)


# ────────── 🚨 「开关开了 ≠ 生效」：服务器上真踩过的坑 ──────────

def test_服务器上写死的宽度不许把单行模式架空(monkeypatch):
    """
    把代码默认值整个盖掉 —— 开关开着，每条还是 14 字、照样叠两行。

    **功能上了生产，却是个空转的摆设，而且一声不吭。**

    这条测试守的是：单行模式下条宽被夹到行宽，并且冲突会被吼出来。
    只在本地跑（本地没设那个环境变量）是发现不了的，所以这里显式设上它。
    """
    import importlib
    import backend.services.pipeline_service as m
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_SINGLE_LINE", "1")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_PER_LINE", "7")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_MAX_CHARS", "14")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_MAX_CHARS_EN", "42")
    m2 = importlib.reload(m)
    try:
        assert m2.PipelineService.MAX_CHARS_PER_CUE == 7,             "服务器写死的 14 又把单行模式架空了"
        assert m2.PipelineService.MAX_CHARS_PER_CUE_EN == 21,             "英文那条同样被架空了"
    finally:
        importlib.reload(m)   # 还回去，别污染后面的测试


def test_关掉开关时显式配置照样说了算(monkeypatch):
    """夹一刀只在单行模式下做。关掉开关 = 回旧行为，显式配置优先。"""
    import importlib
    import backend.services.pipeline_service as m
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_SINGLE_LINE", "0")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_MAX_CHARS", "14")
    m2 = importlib.reload(m)
    try:
        assert m2.PipelineService.MAX_CHARS_PER_CUE == 14
    finally:
        importlib.reload(m)


def test_冲突要吼出来不许闷声夹掉(monkeypatch, caplog):
    """夹了不说 = 下一个人照样看不懂为什么字幕没变。"""
    import importlib
    import logging
    import backend.services.pipeline_service as m
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_SINGLE_LINE", "1")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_PER_LINE", "7")
    monkeypatch.setenv("MEDIA_BUDDY_SUBTITLE_MAX_CHARS", "14")
    with caplog.at_level(logging.WARNING):
        importlib.reload(m)
    try:
        assert any("单行模式" in r.message for r in caplog.records), "夹了却没吭声"
    finally:
        importlib.reload(m)


# ────────── 断句质量：两条硬规则 ──────────

NUM_CASE = ("华为在1987年由任正非创办，当时注册资本只有两万一千元人民币，"
            "连一间像样的办公室都租不起，团队挤在深圳一间不足三十平米的民宅里办公。")


def test_不许从数字中间切开():
    """

    观众看到「两万」会以为就是两万 —— 这类错比断句难看严重得多。
    """
    cues = [t for t, _s, _e in _cues(NUM_CASE)]
    joined = "".join(cues)
    for num in ("1987", "两万一千"):
        assert num in joined, "原文里的 %r 没了" % num
        assert any(num in t for t in cues), "%r 被切到两条里去了：%r" % (num, cues)


def test_不许把虚词甩在句尾():
    """实测切出过「华为在1987年由」+「任正非创办，」—— 以「由」结尾读起来是断的。"""
    from backend.services.pipeline_service import _DANGLING_TAIL
    for text in (NUM_CASE, LONG_ZH):
        for t, _s, _e in _cues(text):
            tail = t.strip().rstrip("，。！？、；：,.!?;:")
            if not tail:
                continue
            assert tail[-1] not in _DANGLING_TAIL, "这一条以虚词结尾：%r" % t


def test_两条规则被否决光了也要能切():
    """规则是**否决候选点**，不是不许切。全被否决 → 兜底硬切。

    不切的后果是字幕冲出画面，比难看严重。
    """
    text = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"
    cues = _cues(text)
    assert len(cues) > 1, "全被否决时没有兜底硬切 —— 这条会冲出画面"
    assert "".join(t for t, _s, _e in cues) == text, "兜底硬切把字弄丢了"
