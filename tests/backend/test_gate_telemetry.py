"""


设计上**不新造统计** —— 项目已有 `LLMCallCounter`(per-purpose × per-model 的
把闸这一段的切片取出来,连同耗时和失败归因一起落库。
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

    def _call(self, system, user, purpose=None):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(script, llm, duration=40):
    from backend.lib.script_gate import run_script_gate

    return run_script_gate(script_v1=script, duration_seconds=duration,
                           voice="qwen_cherry", speed=1.4, series=None, llm=llm)


# ── 耗时 ────────────────────────────────────────────────────────────

def test_latency_is_measured():
    res = _run(_SCRIPT, Llm(_judge(True)))

    assert res.latency_ms >= 0
    assert isinstance(res.latency_ms, int)


# ── 失败归因 ────────────────────────────────────────────────────────

def test_no_failure_reason_when_everything_passes():
    res = _run(_SCRIPT, Llm(_judge(True)))

    assert res.failure_reason == ""


def test_failure_reason_records_unfixed_closure():
    """两轮都没修好 → 归因必须写明是"收口没修好",而不是空着让人猜。"""
    llm = Llm(_judge(False, "没收口"), _tail("还是没收住。"), _judge(False, "还是没收口"),
              _tail("仍然没收住。"), _judge(False, "仍然没收口"))
    res = _run(_SCRIPT, llm)

    assert res.failure_reason == "closure_unfixed"
    assert res.degraded is True


def test_failure_reason_records_repair_call_failure():
    llm = Llm(_judge(False, "没收口"), RuntimeError("超时"), RuntimeError("又超时"))
    res = _run(_SCRIPT, llm)

    assert res.failure_reason == "closure_unfixed"


def test_failure_reason_records_length_still_over():
    """收口修好了但长度压不下来 —— 归因要能和"收口没修好"区分开。"""
    from backend.lib.tts_pacing import count_chars, estimate_seconds

    unit = "另外还要补充一段相当长的说明内容用来把稿子撑长一些。"
    long_script = _SCRIPT + unit * 12
    assert estimate_seconds(count_chars(long_script), "qwen_cherry", 1.4) / 60 > 1.10

    still_long = "仍然很长的正文段落内容需要继续压缩才行。" * 22
    llm = Llm(_judge(False, "没收口"), _tail("一个真正的落点。"), _judge(True),
              still_long, _judge(True), still_long, _judge(True))
    res = _run(long_script, llm, duration=60)

    assert res.failure_reason == "length_over"
    assert res.chosen_predicted_seconds > 60


def test_cost_slice_is_zero_without_an_active_counter():
    """桌面单机/测试里没有 counter —— 取不到就返回 0,不许抛。"""
    from backend.lib.script_gate import gate_cost_usd
    from backend.services.llm_call_counter import set_active_counter

    set_active_counter(None)
    assert gate_cost_usd() == 0.0


# ── telemetry 绝不能影响出片 ────────────────────────────────────────

def test_telemetry_failure_never_breaks_the_gate(monkeypatch):
    """统计出错 → 照常出片。它是观测手段,不是必经之路。"""
    import backend.lib.script_gate as sg

    def boom():
        raise RuntimeError("counter 挂了")

    monkeypatch.setattr(sg, "gate_cost_usd", boom)
    res = _run(_SCRIPT, Llm(_judge(True)))

    assert res.final_script == _SCRIPT
    assert res.cost_usd == 0.0          # 取不到就记 0,不影响主流程


# ── 落库 ────────────────────────────────────────────────────────────

def test_row_carries_the_full_telemetry_set():
    from backend.lib.pacing_feedback import pacing_row_from_gate

    class Gate:
        chosen_version = "v2"
        chosen_predicted_seconds = 57.3
        degraded = False
        final_script = "定稿。" * 30
        llm_calls = 3
        latency_ms = 4210
        cost_usd = 0.0042
        failure_reason = ""
        rounds_used = 1

    row = pacing_row_from_gate(Gate(), voice="qwen_kai", speed=1.2)

    assert row["gate_calls"] == 3
    assert row["gate_latency_ms"] == 4210
    assert row["gate_cost_usd"] == 0.0042
    assert row["failure_reason"] == ""
    assert row["rounds_used"] == 1


def test_row_survives_a_gate_without_telemetry_fields():
    """老的/残缺的 GateResult 不该让攒数据这一步崩掉。"""
    from backend.lib.pacing_feedback import pacing_row_from_gate

    class Old:
        chosen_version = "v1"
        chosen_predicted_seconds = 50.0
        degraded = False
        final_script = "稿子。"

    row = pacing_row_from_gate(Old(), voice="qwen_kai", speed=1.2)

    assert row["gate_calls"] is None
    assert row["voice"] == "qwen_kai"


def test_video_health_has_the_telemetry_columns():
    from backend.models.video_health import VideoHealth

    for col in ("gate_calls", "gate_latency_ms", "gate_cost_usd",
                "failure_reason", "rounds_used"):
        assert hasattr(VideoHealth, col), col
