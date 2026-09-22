"""M2 结构预算:把"写多少字"升级成"每个节拍写多少字,结尾额度预留"。

## 为什么

现在只告诉模型一个总字数,它把预算花在铺陈上,到结尾没额度了 —— 这正是

## 为什么这一步是**省钱**的

结构预算是纯 prompt 改动,**不增加任何 LLM 调用**;而稿子第一次就写对,
就不用进 script_gate 的修复轮(每轮 2 次调用,最多 5 次)。
它的收益直接体现在"进修复轮的比例"上。
"""
import pytest


def _budget(duration=60, voice="qwen_cherry", speed=1.4):
    from backend.lib.tts_pacing import beat_budget

    return beat_budget(duration, voice, speed)


# ── 分配算法(确定性代码生成,不靠 LLM)──────────────────────────────

def test_landing_quota_is_explicitly_reserved():
    """落点必须有**自己的额度**,而不是"剩下多少算多少"。"""
    b = _budget()

    assert b["landing"]["sentences"] == 2
    assert b["landing"]["chars"] > 0
    assert 0.18 <= b["landing"]["chars"] / b["total_chars"] <= 0.26


def test_hook_gets_one_sentence_about_12_percent():
    b = _budget()

    assert b["hook"]["sentences"] == 1
    assert 0.09 <= b["hook"]["chars"] / b["total_chars"] <= 0.16


def test_body_takes_the_remainder():
    b = _budget()

    assert b["body"]["sentences"] >= 2
    total = b["hook"]["sentences"] + b["body"]["sentences"] + b["landing"]["sentences"]
    assert total == b["sentences"]


def test_never_fewer_than_five_sentences():
    """极短片(15 秒)也要留得下"钩子+正文+落点"三段。"""
    b = _budget(duration=15)

    assert b["sentences"] >= 5
    assert b["landing"]["sentences"] == 2


@pytest.mark.parametrize("duration", [15, 30, 45, 60, 90, 120])
@pytest.mark.parametrize("speed", [0.8, 1.0, 1.2, 1.4])
def test_allocation_always_adds_up_and_stays_positive(duration, speed):
    """任何时长/语速组合下分配都自洽,不许出现 0 或负数额度。"""
    b = _budget(duration=duration, speed=speed)

    for part in ("hook", "body", "landing"):
        assert b[part]["chars"] > 0, part
        assert b[part]["sentences"] >= 1, part
    assert abs(sum(b[p]["chars"] for p in ("hook", "body", "landing"))
               - b["total_chars"]) <= 3


def test_total_matches_the_single_budget_source():
    """总额仍来自 tts_pacing 的目标字数 —— 不许出现第二套预算。"""
    from backend.lib.tts_pacing import short_script_target_chars

    b = _budget()
    assert b["total_chars"] == short_script_target_chars(60, "qwen_cherry", 1.4)


# ── 进 prompt ───────────────────────────────────────────────────────

def test_prompt_reserves_the_ending_and_says_so():
    from backend.lib.llm_client import LLMClient

    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4, tts_provider="qwen_cherry",
    )

    assert "落点" in p
    assert "不许挪用" in p, "必须明说结尾额度是预留的"


def test_prompt_still_carries_the_hard_closure_constraint():
    """结构预算是新增的,不能把已验证的收口硬约束挤掉。"""
    from backend.lib.llm_client import LLMClient

    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4, tts_provider="qwen_cherry",
    )

    assert "HARD STORY-CLOSURE CONSTRAINT" in p
    assert "HARD LENGTH CONSTRAINT" in p


def test_structural_budget_can_be_switched_off(monkeypatch):
    """回滚开关(spec §8):关掉后退回纯字数 prompt,一行配置的事。"""
    from backend.lib.llm_client import LLMClient

    monkeypatch.setenv("MB_STRUCT_BUDGET", "0")
    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4, tts_provider="qwen_cherry",
    )

    assert "不许挪用" not in p
    assert "HARD LENGTH CONSTRAINT" in p     # 字数约束仍在


def test_no_extra_llm_call_is_introduced():
    """M2 是纯 prompt 改动 —— 分配由确定性代码算出,不许为此多调一次模型。"""
    import inspect

    from backend.lib import tts_pacing

    src = inspect.getsource(tts_pacing.beat_budget)
    for forbidden in ("_call", "llm", "openai", "httpx", "requests"):
        assert forbidden not in src, f"beat_budget 不该碰 {forbidden}"
