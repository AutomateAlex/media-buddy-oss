"""升级式疗程 + 终态选优(Spec v2.1 §7/§8)。

P10「升级换药」:**禁止同方法同参数重试**。每疗程必须换方法:
  疗程1 定点修复(只动该动的一段)
  疗程2 受控重写(同资料包重开一稿,prompt 里写明上版死因)
  疗程3 降需求重写(只讲一个问题答一个问题,目标降到 0.85×档位)

终态不再是"退回 v1",而是**候选选优**:
  资格线(编造事实)一票否决 → 收口合格者优先 → 时长最接近内部目标者胜。
  **v1 没有特权** —— 它只是候选之一。
"""
import json

import pytest


_SCRIPT = (
    "1745年，一个中年男人走进巴黎沙龙，随手送出钻石当礼物，还说自己能把银变成金。"
    "在场的不是傻子，可连卡萨诺瓦都承认亲眼见过。"
    "他讲六门以上语言，小提琴拉到和帕格尼尼齐名，路易十五让他住进香波尔城堡。"
    "没有人查得到他的出生记录，他先后换过十几个化名，每换一个就换一座城市。"
    "这些技能和情报网络，让贵族们觉得他不是骗子。"
    "但他最厉害的一招是暗示自己长生不老。"
    "1710年有人在威尼斯见过他，1745年再见，他外貌没变。"
)


def _judge(ok, reason="", new_facts=False):
    return json.dumps({"closure_ok": ok, "closure_reason": reason,
                       "introduced_new_facts": new_facts}, ensure_ascii=False)


def _tail(t):
    return json.dumps({"replace_last_sentence_count": 2, "replacement_tail": t},
                      ensure_ascii=False)


class Llm:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.systems = []

    def _call(self, system, user, purpose=None):
        self.systems.append(system + "\n" + user)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(script, llm, duration=40):
    from backend.lib.script_gate import run_script_gate

    return run_script_gate(script_v1=script, duration_seconds=duration,
                           voice="qwen_cherry", speed=1.4, series=None, llm=llm)


# ── 候选选优:v1 没有特权 ────────────────────────────────────────────

def test_picks_the_closed_candidate_over_the_original():
    from backend.lib.script_gate import pick_final

    cands = [
        {"version": "v1", "script": "原稿。", "closure_ok": False,
         "new_facts": False, "predicted": 50.0},
        {"version": "v2", "script": "修好的稿。", "closure_ok": True,
         "new_facts": False, "predicted": 70.0},
    ]
    best = pick_final(cands, target_seconds=55.0)

    assert best["version"] == "v2", "收口合格者优先,哪怕它更长(P2)"


def test_fabricated_facts_are_disqualified_even_if_closed():
    """资格线:编造事实一票否决 —— 收口再好也不能出厂。"""
    from backend.lib.script_gate import pick_final

    cands = [
        {"version": "v1", "script": "原稿。", "closure_ok": False,
         "new_facts": False, "predicted": 50.0},
        {"version": "v2", "script": "编造的稿。", "closure_ok": True,
         "new_facts": True, "predicted": 55.0},
    ]
    best = pick_final(cands, target_seconds=55.0)

    assert best["version"] == "v1"


def test_among_closed_candidates_the_closest_to_target_wins():
    from backend.lib.script_gate import pick_final

    cands = [
        {"version": "v2", "script": "a", "closure_ok": True,
         "new_facts": False, "predicted": 75.0},
        {"version": "v3", "script": "b", "closure_ok": True,
         "new_facts": False, "predicted": 56.0},
    ]
    best = pick_final(cands, target_seconds=55.0)

    assert best["version"] == "v3"


def test_all_unclosed_still_picks_the_closest_length_not_blindly_v1():
    """全都没收口时,也该挑长度最合规的 —— v1 不因为"是原稿"就自动获胜。"""
    from backend.lib.script_gate import pick_final

    cands = [
        {"version": "v1", "script": "原稿。", "closure_ok": False,
         "new_facts": False, "predicted": 95.0},
        {"version": "v2", "script": "改过的。", "closure_ok": False,
         "new_facts": False, "predicted": 57.0},
    ]
    best = pick_final(cands, target_seconds=55.0)

    assert best["version"] == "v2"


def test_empty_candidate_list_returns_none():
    from backend.lib.script_gate import pick_final

    assert pick_final([], target_seconds=55.0) is None


# ── 疗程升级:必须换方法 ────────────────────────────────────────────

def test_treatment_two_rewrites_with_the_previous_failure_reason():
    """疗程2 是**受控重写**,不是把疗程1 再跑一遍(P10 禁止同方法同参数重试)。

    上版死因必须写进 prompt —— 否则模型不知道自己错在哪,换汤不换药。
    """
    from backend.lib.script_gate import treatment_prompt

    p = treatment_prompt(2, _SCRIPT, "两个核心问题未回答", "要给解释", 350)

    assert "两个核心问题未回答" in p
    assert "重写" in p
    assert "禁止再犯" in p or "不得重复" in p


def test_treatment_three_lowers_the_requirement():
    """疗程3 降需求:只讲一个问题答一个问题,目标降到 0.85×档位。"""
    from backend.lib.script_gate import treatment_prompt

    p = treatment_prompt(3, _SCRIPT, "还是没答上", "要给解释", 350)

    assert "一个问题" in p
    assert "298" in p or "297" in p or "299" in p     # 0.85 × 350


def test_treatments_escalate_rather_than_repeat():
    from backend.lib.script_gate import treatment_prompt

    p2 = treatment_prompt(2, _SCRIPT, "r", "rule", 350)
    p3 = treatment_prompt(3, _SCRIPT, "r", "rule", 350)

    assert p2 != p3, "两个疗程必须换方法,不能是同一份 prompt"


# ── 调用预算封顶 ────────────────────────────────────────────────────

def test_global_call_budget_caps_the_whole_gate(monkeypatch):
    """P10 全局预算:耗尽直接进终态,不许各模块各自追加。"""
    monkeypatch.setenv("MB_GATE_CALL_BUDGET", "3")

    llm = Llm(_judge(False, "没收口"), _tail("没修好。"), _judge(False, "还没好"),
              _tail("再试。"), _judge(False, "仍没好"))
    res = _run(_SCRIPT, llm)

    assert res.llm_calls <= 3


def test_budget_exhaustion_is_recorded_in_the_failure_reason(monkeypatch):
    monkeypatch.setenv("MB_GATE_CALL_BUDGET", "1")

    llm = Llm(_judge(False, "没收口"))
    res = _run(_SCRIPT, llm)

    assert res.failure_reason in ("budget_exhausted", "closure_unfixed")
