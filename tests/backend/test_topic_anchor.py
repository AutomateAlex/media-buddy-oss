"""

根因:SUBJECT_PHRASES 表对 CJK 走纯子串匹配,"北美浣熊"命中单字条目"熊"
→ 锚点=bear,污染三条下游(判官 main_subject / motif 兜底池 / FLUX prompt)。
判官反把真浣熊素材拒了。

修复(MB_ANCHOR_FROM_DIRECTOR=1,默认开):
① 锚点优先从导演(LLM)已产出的英文 must_show/queries 里提取
   (推导发生在 build_global_plan 之后,导演产出已在手,无需回填);
② 英文产出拿不到才落静态中文表,且最长词优先+命中即消费
   ("浣熊"消费后单字"熊"无从命中;"棕熊"仍正常落到"熊"→bear 不误伤);
③ 都拿不到保持 motif_pool 现状行为。
开关 MB_ANCHOR_FROM_DIRECTOR=0 回退旧逻辑(含旧 bug,行为可预期)。
"""
from __future__ import annotations

import pytest

from backend.services.short_video import visual_focus as vf


@pytest.fixture()
def flag_on(monkeypatch):
    """默认路径:开关未设置(=开)。显式删掉,防外部环境污染。"""
    monkeypatch.delenv("MB_ANCHOR_FROM_DIRECTOR", raising=False)


@pytest.fixture()
def flag_off(monkeypatch):
    monkeypatch.setenv("MB_ANCHOR_FROM_DIRECTOR", "0")


def _raccoon_director_chunks() -> list[dict]:
    """模拟导演已理解对主体的英文产出(07e43e59 的形态)。"""
    return [
        {
            "sentence": "北美浣熊为什么总在水里洗东西?",
            "queries": ["raccoon washing food in stream", "raccoon paws underwater close up"],
            "must_show": ["raccoon"],
        },
        {
            "sentence": "它的爪子布满神经末梢。",
            "queries": ["raccoon paw close up", "raccoon touching object"],
            "must_show": ["raccoon paw"],
        },
        {
            "sentence": "夜里它翻遍垃圾桶。",
            "queries": ["raccoon night trash can", "raccoon urban night"],
            "must_show": ["raccoon"],
        },
    ]


# ---------------------------------------------------------------------------
# ① 主修复:导演英文产出优先,中文表劫持不再发生
# ---------------------------------------------------------------------------

def test_director_output_wins_over_cjk_table(flag_on):
    """"北美浣熊的秘密" + 导演产出全是 raccoon → 锚点必须是 raccoon,不是 bear。"""
    brief = "北美浣熊的秘密"
    anchor = vf._derive_topic_anchor(_raccoon_director_chunks(), [brief], brief, [])
    assert anchor == "raccoon"


def test_policy_topic_anchor_single_source(flag_on):
    """判官 main_subject / motif 兜底 / FLUX 都读 short_video_visual_policy.topic_anchor
    与 video_meta.short_video_topic_anchor —— 验证 repair 后两处同源且为 raccoon。"""
    brief = "北美浣熊的秘密"
    chunks = _raccoon_director_chunks()
    sentences = [c["sentence"] for c in chunks]
    plan = {"chunks": chunks, "video_meta": {}}
    repaired = vf.repair_short_video_plan(plan, sentences, user_brief=brief, domain_pack=None)
    assert repaired["video_meta"]["short_video_topic_anchor"] == "raccoon"
    for chunk in repaired["chunks"]:
        assert chunk["short_video_visual_policy"]["topic_anchor"] == "raccoon"


# ---------------------------------------------------------------------------
# ② 兜底层:无导演产出时走中文表,最长词优先
# ---------------------------------------------------------------------------

def test_table_fallback_raccoon_longest_match(flag_on):
    """无导演产出:"浣熊"命中新词条 raccoon,单字"熊"被消费不再劫持。"""
    brief = "浣熊为什么洗东西"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "raccoon"


def test_table_fallback_red_panda(flag_on):
    brief = "小熊猫的尾巴"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "red panda"


def test_table_fallback_bumblebee(flag_on):
    brief = "熊蜂授粉的秘密"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "bumblebee"


def test_table_fallback_seahorse(flag_on):
    brief = "海马爸爸生孩子"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "seahorse"


def test_table_fallback_brown_bear_not_hurt(flag_on):
    """"棕熊"表里没有更长词条,仍应正常落到"熊"→bear,最长词优先不误伤。"""
    brief = "棕熊的冬眠"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "bear"


def test_table_fallback_plain_bear(flag_on):
    brief = "熊的冬眠"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "bear"


# ---------------------------------------------------------------------------
# ③ 开关回退:MB_ANCHOR_FROM_DIRECTOR=0 → 旧逻辑(含旧 bug,行为可预期)
# ---------------------------------------------------------------------------

def test_flag_off_reverts_to_legacy_behavior(flag_off):
    """旧逻辑:中文表先行,"北美浣熊"仍被单字"熊"劫持为 bear(回退可预期)。"""
    brief = "北美浣熊的秘密"
    assert vf._derive_topic_anchor([], [brief], brief, []) == "bear"


def test_flag_off_director_output_does_not_preempt(flag_off):
    """旧逻辑里 brief 命中中文表就直接返回,导演产出不插队。"""
    brief = "北美浣熊的秘密"
    anchor = vf._derive_topic_anchor(_raccoon_director_chunks(), [brief], brief, [])
    assert anchor == "bear"
