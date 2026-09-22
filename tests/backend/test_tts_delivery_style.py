# -*- coding: utf-8 -*-
"""

## 背景

用户：「配音不太适合自媒体来用，我想要自媒体和 YouTuber 那种稳定的感觉」。

查下来根因是**我们自己的提示词在要求它念得平**：
    「语气平稳、语速均匀稳定、不要夸张的抑扬顿挫」
那句当初是为治「奇怪的抑扬顿挫」加的，矫枉过正成了「没有情绪、像念稿」。

做了 5 种指令 × 8 个男声 × 4 个微调，用户盲听选定：
**YouTuber 强指令 + 广播腔 + 关掉段间重解释**，女声(Cherry)也确认好听。

## 这里守什么

风格是**听出来的**，不是读代码读出来的。所以这些测试守的不是「好不好听」，
而是「那次听感调优的结论有没有被后人无意中改回去」：

1. 「语气平稳 / 不要抑扬顿挫」这类**压平情绪**的词不许回到默认指令里
2. 让它有起伏的关键要求必须还在（加重数字、转折前停顿、广播腔）
3. `optimize` 默认必须是 0（段间重解释会让前后「像换了个人」）
4. 中英文都要有这一套（英文原来只有一句干巴巴的）
5. env 覆盖必须还能用（出问题要能一键切回去）
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.services.tts_service import TTSService  # noqa: E402


def _string_literals(fn) -> str:
    """把函数体里**真正的字符串**拼起来（不含注释）。

    ⚠️ 直接扫源码会把**注释**也算进去。本会话已经因此误判三次：
       写「上一版是『不要夸张的抑扬顿挫』」这样的注释，守卫就会当成
       那句提示词还在。注释解释历史是好事，不该因此报警。
       所以用 AST 只取字符串常量。
    """
    import ast as _ast
    import textwrap
    tree = _ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return "\n".join(
        n.value for n in _ast.walk(tree)
        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
    )


# 只看真字符串 —— 判断「提示词里有没有这句话」用它
SRC = _string_literals(TTSService._synthesize_qwen)
# 看代码结构（env 名、分支）用整段源码
RAW = inspect.getsource(TTSService._synthesize_qwen)


# ── 1. 压平情绪的说法不许回来 ────────────────────────────────────────────
@pytest.mark.parametrize("坏话", [
    "语气平稳、语速均匀稳定",
    "不要夸张的抑扬顿挫",
    "从头到尾节奏保持一致",
    "不要忽快忽慢",
])
def test_压平情绪的指令不许回到默认里(坏话):
    """这些词组合起来就是在要求它「别有情绪」—— 正是用户抱怨的那个声音。

    请先做样本让人听，别直接改字。
    """
    assert 坏话 not in SRC, (
        "默认风格指令里又出现了「%s」—— 这会把配音打回念稿腔。"
        "改之前先做 A/B 样本让人听。" % 坏话)


# ── 2. 让它有起伏的关键要求必须在 ────────────────────────────────────────
@pytest.mark.parametrize("必须有", [
    "像一位头部中文 YouTube 知识博主",   # 整体定调
    "开头两句要有抓人的劲儿",             # 钩子
    "明显加重并略微放慢",                 # 数字要落地
    "前面留一个短停顿再推出去",           # 转折的呼吸
    "问句要真的像在问观众",               # 不要平铺直叙
    "广播腔：字正腔圆",                   # 用户点名要的
    "胸腔共鸣饱满",
    "落点要沉下去收住",                   # 句尾不飘
])
def test_自媒体广播腔的关键要求还在(必须有):
    assert 必须有 in SRC, "盲听选定的风格要求「%s」被删掉了" % 必须有


# ── 3. 段间重解释默认必须关 ──────────────────────────────────────────────
def test_optimize默认是0():
    """开着的话每块各自重解释风格 → 块间「像换了个人」。

    用户选定的那版（moon-广播腔+关重解释）就是关着的。
    """
    assert 'MEDIA_BUDDY_QWEN_TTS_OPTIMIZE", "0"' in RAW, \
        "optimize 默认又变回 1 了 —— 长旁白会出现「中途换了个人」"


# ── 4. 英文也要有同一套 ──────────────────────────────────────────────────
@pytest.mark.parametrize("必须有", [
    "top English-language YouTube explainer host",
    "broadcast delivery",
    "full chest resonance",
])
def test_英文指令也升级了(必须有):
    """英文原来只有一句「Natural, warm, professional」，等于没指导。"""
    assert 必须有 in SRC, "英文风格指令缺了「%s」" % 必须有


def test_英文旧的干巴巴那句已经换掉():
    assert "Natural, warm, professional English narration" not in SRC


# ── 5. env 还能一键切回去 ────────────────────────────────────────────────
def test_env仍可覆盖():
    """听感是主观的。真出问题时必须能不改代码就切回去。"""
    assert 'os.environ.get("MEDIA_BUDDY_QWEN_TTS_INSTRUCTIONS", "")' in RAW
    assert "MEDIA_BUDDY_QWEN_TTS_OPTIMIZE" in RAW


# ── 6. 中英文各一套，别串了 ──────────────────────────────────────────────
def test_按语言分派():
    """英文稿套中文讲解指令会让英文口播打折（原代码注释里写着的教训）。"""
    assert 'if language == "English"' in RAW
