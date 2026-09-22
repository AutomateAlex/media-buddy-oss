"""收口标准:跟 Channel 走,复用既有的 CHANNEL_RULES。

为什么不新建 closure_standards.yaml(spec v1.2 M4 原方案):
项目里已经有一套结构化频道注册表(`CHANNEL_RULES`,按 `series.industry_tag` 解析),
而且 `mystery_explainer` 的 positioning 里**早就写着**「结尾给出最可信的主流解释」——
引发这次排查的圣日耳曼那条片子,违反的正是这条**已存在**的规则,只是从没人执行。
再建一份 yaml 会变成双事实源,且新旧两套说法相反(yaml 说"可以停在悬念点")。
另外项目里既没有 config/ 目录也没装 PyYAML,新配置格式还要引入依赖。
"""
import dataclasses

import pytest


class _FakeSeries:
    def __init__(self, industry_tag=None):
        self.industry_tag = industry_tag


# ── 标准从哪来 ──────────────────────────────────────────────────────

def test_mystery_channel_requires_a_real_explanation_at_the_end():
    """悬疑频道的收口标准 = 它自己 positioning 里那句,不是泛化的"允许留悬念"。"""
    from backend.services.channel_intelligence_service import CHANNEL_RULES

    rule = CHANNEL_RULES["mystery_explainer"]
    assert rule.closure_rule, "mystery_explainer 必须有收口标准"
    assert "解释" in rule.closure_rule


def test_closure_rule_survives_every_channel_rule_rebuild():
    """回归:CHANNEL_RULES 有 5 处**逐字段重建** ChannelRule 的地方
    (_EXTRA_ANCHORS / _EXTRA_OFF_DOMAIN / _CLEAN_EXTRA_* / _merge_channel_rule)。
    任何一处漏拷新字段,收口标准就会被静默丢掉、且不报错。

    这是"同一件事散在多处"的老毛病 —— 本次已全部改用 dataclasses.replace,
    此测试锁死:所有重建路径跑完后,带标准的频道仍然带着标准。
    """
    from backend.services.channel_intelligence_service import (
        CHANNEL_RULES, _merge_channel_rule,
    )

    assert CHANNEL_RULES["mystery_explainer"].closure_rule

    # 模拟一次别名合并:目标频道没标准、来源频道有 → 必须继承下来
    src = "mystery_explainer"
    dst = next(k for k, v in CHANNEL_RULES.items() if not v.closure_rule)
    before = CHANNEL_RULES[dst]
    try:
        _merge_channel_rule(src, dst)
        assert CHANNEL_RULES[dst].closure_rule == CHANNEL_RULES[src].closure_rule
    finally:
        CHANNEL_RULES[dst] = before


def test_channel_rule_is_a_dataclass_so_replace_works():
    """重建必须走 dataclasses.replace,否则加字段又会漏拷。"""
    from backend.services.channel_intelligence_service import ChannelRule

    assert dataclasses.is_dataclass(ChannelRule)
    r = ChannelRule(label="x", anchors=(), closure_rule="收口要求")
    assert dataclasses.replace(r, label="y").closure_rule == "收口要求"


# ── 解析链路 ────────────────────────────────────────────────────────

def test_resolves_rule_through_series_industry_tag():
    from backend.lib.closure_standards import closure_rule_for

    text = closure_rule_for(_FakeSeries("mystery_explainer"))
    assert "解释" in text


@pytest.mark.parametrize("series", [None, _FakeSeries(None), _FakeSeries("不存在的频道")])
def test_falls_back_to_default_when_channel_unknown(series):
    """series 可为 None(projects.series_id 是 nullable),必须有降级。"""
    from backend.lib.closure_standards import DEFAULT_CLOSURE_RULE, closure_rule_for

    assert closure_rule_for(series) == DEFAULT_CLOSURE_RULE


def test_default_rule_demands_the_question_be_answered():
    from backend.lib.closure_standards import DEFAULT_CLOSURE_RULE

    assert "回答" in DEFAULT_CLOSURE_RULE


@pytest.mark.parametrize("ending", [
    "破解方法只有一句话：在米粒最容易堵车的那个窗口期，主动打断它。怎么做？",
    "加了盐，酶的活性明显下降，面条更耐煮。那加多少、什么时候加？",
])
def test_dangling_howto_question_is_caught_for_free(ending):
    """"问完即止"式断尾:确定性规则抓,不花 LLM 调用。"""
    from backend.lib.closure_standards import dangling_pattern_hit

    assert dangling_pattern_hit(ending) == "unanswered_howto"


@pytest.mark.parametrize("ending", [
    "那问题来了：这种连自己都骗过去的假死，到底是弱势还是另一种生存智慧？",
    "这不是健身效果，是写在基因里的生存策略。",
])
def test_good_endings_are_not_flagged_by_patterns(ending):
    """讨论式开放问题和结论句都是好结尾,绝不能误杀。"""
    from backend.lib.closure_standards import dangling_pattern_hit

    assert dangling_pattern_hit(ending) is None


def test_pattern_config_is_data_not_code():
    """P5:pattern 是耗材,可随时增删,不写死在判断逻辑里。"""
    from backend.lib.closure_standards import DANGLING_PATTERNS

    assert isinstance(DANGLING_PATTERNS, tuple)
    assert all({"id", "regex", "note"} <= set(p) for p in DANGLING_PATTERNS)
