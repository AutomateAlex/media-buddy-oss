"""文字验收闸:体检 → 分诊 → 隔离式定点修复 → 复检 → 终态裁决。

修复合约(P11 修改即隔离):
- 改结尾 → 模型返回 JSON `{replace_last_sentence_count, replacement_tail}`,代码拼接;
- 改长度 → 代码只把**正文段**发给模型,模型返回正文,代码拼回钩子与落点。

所以本文件里修复器的"返回值"不再是整篇稿子。这是刻意的:
模型没有重写正文的写入权,"正文逐字不变"是**构造出来的**,不是事后查出来的

出厂仍超长时长度保险丝再花 2 次(≤ 7)。
"""
import json

import pytest


_GOOD_TAIL = "他骗过整个欧洲，靠的不是别人蠢，而是把查得到的真本事和查不到的传说搅在一起。"

_UNFINISHED = (
    "1745年，一个中年男人走进巴黎沙龙，随手送出钻石当礼物，还说自己能把银变成金。"
    "在场的不是傻子，可连卡萨诺瓦都承认亲眼见过。"
    "他讲六门以上语言，小提琴拉到和帕格尼尼齐名，路易十五让他住进香波尔城堡。"
    "没有人查得到他的出生记录，他先后换过十几个化名，每换一个就换一座城市。"
    "这些技能和情报网络，让贵族们觉得他不是骗子。"
    "但他最厉害的一招是暗示自己长生不老。"
    "1710年有人在威尼斯见过他，1745年再见，他外貌没变。"
)


def _spliced(script, tail, n=2):
    """按 replace_tail 的语义算出拼接结果 —— 期望值与代码同源,不手抄。"""
    from backend.lib.script_gate import _sentences

    sents = _sentences(script)
    return "".join(sents[:-n]) + tail


_GOOD = _spliced(_UNFINISHED, _GOOD_TAIL)


class FakeLlm:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _call(self, system, user, purpose=None):
        self.calls.append({"system": system, "user": user, "purpose": purpose})
        if not self.responses:
            raise AssertionError("调用次数超出预设,说明成本契约被破坏")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _judge(ok, reason="", new_facts=False):
    return json.dumps(
        {"closure_ok": ok, "closure_reason": reason, "introduced_new_facts": new_facts},
        ensure_ascii=False,
    )


def _tail(tail, n=2):
    """replace_tail 修复器的返回格式。"""
    return json.dumps(
        {"replace_last_sentence_count": n, "replacement_tail": tail},
        ensure_ascii=False,
    )


def _run(script, llm, duration=40, voice="qwen_cherry", speed=1.4, series=None):
    from backend.lib.script_gate import run_script_gate

    return run_script_gate(
        script_v1=script, duration_seconds=duration, voice=voice,
        speed=speed, series=series, llm=llm,
    )


# ── 放行路径 ────────────────────────────────────────────────────────

def test_clean_script_passes_with_a_single_judge_call():
    llm = FakeLlm(_judge(True))
    res = _run(_GOOD, llm)

    assert res.final_script == _GOOD
    assert res.chosen_version == "v1"
    assert res.rounds_used == 0
    assert res.degraded is False
    assert res.llm_calls == 1


def test_pattern_hit_short_circuits_the_judge():
    """确定性 pattern 命中 → 直接判断尾,不花 LLM 调用做判断。"""
    bad = "米粒吸饱水后体积涨到三倍，锅底那层几乎不流动。破解方法只有一句话。怎么做？"
    llm = FakeLlm(_tail("答案是提前把米泡够时间。"), _judge(True),
                  "重写的一稿内容。", _judge(True), "再重写一稿。", _judge(True))
    res = _run(bad, llm)

    # 体检那次没花钱(pattern 命中直接判断尾),所以第一次调用就是修复
    assert res.checks[0].source == "pattern"
    assert "closure" in llm.calls[0]["system"] or "结尾" in llm.calls[0]["system"]


# ── 隔离式定点修复 ──────────────────────────────────────────────────

def test_repair_sees_the_full_script_but_can_only_write_the_ending():
    """模型看得到全文(才写得出承接的结尾),但只有结尾的写入权。"""
    llm = FakeLlm(_judge(False, "预告了最厉害的一招却只举一个例子"),
                  _tail(_GOOD_TAIL), _judge(True))
    res = _run(_UNFINISHED, llm)

    assert _UNFINISHED[:100] in llm.calls[1]["user"], "修复器必须看到原稿"
    assert res.final_script == _GOOD
    assert res.chosen_version == "v2"
    assert res.final_script.startswith(_UNFINISHED[:80])   # 正文逐字不变


def test_repair_uses_the_closure_purpose_slot_not_a_hardcoded_model():
    llm = FakeLlm(_judge(False, "没收口"), _tail(_GOOD_TAIL), _judge(True))
    _run(_UNFINISHED, llm)

    assert all(c["purpose"] == "closure" for c in llm.calls)


def test_unparseable_repair_return_is_a_failed_round_not_a_crash():
    llm = FakeLlm(_judge(False, "没收口"), "模型返回了一段大白话",
                  _GOOD, _judge(True))
    res = _run(_UNFINISHED, llm)

    assert res.rounds_used == 2
    assert res.final_script == _GOOD      # 疗程2 受控重写救回来了


# ── 五类分诊 ────────────────────────────────────────────────────────

def test_triage_length_ok_closure_bad_asks_to_keep_body():
    from backend.lib.script_gate import triage

    assert triage("pass", closure_ok=False) == "fix_ending"
    assert triage("gray", closure_ok=False) == "fix_ending"


def test_triage_too_long_closure_ok_asks_to_compress_middle():
    from backend.lib.script_gate import triage

    assert triage("repair_long", closure_ok=True) == "compress_middle"


def test_triage_too_short_closure_ok_asks_to_add_evidence():
    from backend.lib.script_gate import triage

    assert triage("repair_short", closure_ok=True) == "extend_middle"


def test_triage_both_bad_merges_into_one_instruction():
    from backend.lib.script_gate import triage

    assert triage("repair_long", closure_ok=False) == "compress_and_fix_ending"
    assert triage("repair_short", closure_ok=False) == "extend_and_fix_ending"


def test_triage_all_ok_is_pass():
    from backend.lib.script_gate import triage

    assert triage("pass", closure_ok=True) == "pass"
    assert triage("gray", closure_ok=True) == "pass"


def test_combined_triage_fixes_the_ending_first():
    """又超长又断尾时**先修结尾**(P2 收口 > 长度),长度留给下一轮或长度保险丝。

    一次只动一个地方,是隔离能成立的前提;一次改两处就又回到整稿重写的老路。
    """
    from backend.lib.script_gate import _apply_repair

    seen = {}

    def ask(system, user):
        seen["system"] = system
        return _tail("新的落点。")

    out = _apply_repair(_UNFINISHED, "compress_and_fix_ending", "r", "规则", 300, ask)

    assert out.endswith("新的落点。")
    assert "结尾修改者" in seen["system"]     # 走 replace_tail,不是压正文


# ── 复检合并调用 ────────────────────────────────────────────────────

def test_recheck_rejects_a_repair_that_invented_new_facts():
    llm = FakeLlm(
        _judge(False, "没收口"),
        _tail(_GOOD_TAIL), _judge(True, "", new_facts=True),   # 编了新事实 → 资格线出局
        _GOOD, _judge(True),                                   # 疗程2 重写,合格
    )
    res = _run(_UNFINISHED, llm)

    assert res.rounds_used == 2
    assert res.final_script == _GOOD


def test_recheck_missing_fields_fails_open():
    llm = FakeLlm(_judge(False, "没收口"), _tail(_GOOD_TAIL), "不是 JSON 的大白话")
    res = _run(_UNFINISHED, llm)

    assert res.final_script == _GOOD
    assert res.degraded is False


# ── fail-open ───────────────────────────────────────────────────────

def test_judge_exception_passes_the_script_through():
    llm = FakeLlm(RuntimeError("判断器挂了"))
    res = _run(_GOOD, llm)

    assert res.final_script == _GOOD
    assert res.chosen_version == "v1"


def test_repair_exception_keeps_the_original():
    llm = FakeLlm(_judge(False, "没收口"), RuntimeError("修复调用超时"),
                  RuntimeError("再来一次也超时"))
    res = _run(_UNFINISHED, llm)

    assert res.final_script == _UNFINISHED
    assert res.degraded is True


# ── 轮次上限与终态裁决 ──────────────────────────────────────────────

def test_stops_after_the_treatment_ladder_and_respects_the_budget():
    """三级疗程走完就停,且总调用受全局预算封顶(P10)。"""
    llm = FakeLlm(
        _judge(False, "没收口"), _tail("还是没收住。"), _judge(False, "还是没收口"),
        "重写一稿。", _judge(False, "仍没收口"),
        "降需求再写一稿。", _judge(False, "还是不行"),
    )
    res = _run(_UNFINISHED, llm)

    assert res.rounds_used == 3
    assert res.llm_calls <= 8            # 全局预算
    assert res.degraded is True


def test_never_falls_back_to_a_template_script():
    """P4:断尾原稿 > 通用模板稿。闸内不得出现模板兜底。"""
    llm = FakeLlm(_judge(False, "没收口"), RuntimeError("boom"), RuntimeError("boom"))
    res = _run(_UNFINISHED, llm)

    assert res.final_script == _UNFINISHED


def test_predicted_seconds_belongs_to_the_chosen_version():
    from backend.lib.tts_pacing import count_chars, estimate_seconds

    llm = FakeLlm(_judge(False, "没收口"), _tail(_GOOD_TAIL), _judge(True))
    res = _run(_UNFINISHED, llm)

    assert res.chosen_version == "v2"
    expected = estimate_seconds(count_chars(_GOOD), "qwen_cherry", 1.4)
    assert abs(res.chosen_predicted_seconds - expected) < 0.01


def test_closure_fixed_and_length_ok_returns_immediately():
    llm = FakeLlm(_judge(False, "没收口"), _tail(_GOOD_TAIL), _judge(True))
    res = _run(_UNFINISHED, llm)

    assert res.rounds_used == 1
    assert res.llm_calls == 3
    assert res.chosen_version == "v2"


def test_never_ships_an_over_long_script_without_trying_to_compress():
    """

    原因:摘掉截断器后,"两轮都没修好"这条路上没有东西再管长度,
    而保险丝当时设在 2 倍(120 秒),91.4 秒正好从缝里漏出去。
    现在改为:出厂前仍判太长 → 再花一轮**只压正文**(结尾由代码保管,动不了)。
    """
    from backend.lib.tts_pacing import count_chars, estimate_seconds

    unit = "另外还要补充一段相当长的说明内容用来把稿子撑长一些。"
    over_long = _UNFINISHED + unit * 12
    ratio = estimate_seconds(count_chars(over_long), "qwen_cherry", 1.4) / 60
    assert 1.15 < ratio < 1.9, f"夹具必须落在缝里(实际 {ratio:.2f} 倍)"

    llm = FakeLlm(
        _judge(False, "没收口"),
        _tail("没修好的结尾。"), _judge(False, "还是没收口"),
        "重写一稿仍然很长。" * 30, _judge(False, "仍没收口"),
        "降需求写的一稿也很长。" * 28, _judge(False, "还是不行"),
        "压缩后的短正文。", _judge(False, "没收口"),      # 保险丝:只改正文
    )
    res = _run(over_long, llm, duration=60)

    assert res.chosen_predicted_seconds <= 60.0, (
        f"出厂稿仍超长 {res.chosen_predicted_seconds:.1f}s —— 客户会多付钱"
    )


def test_length_fuse_cannot_touch_the_ending():
    """压长度走 compress_body —— 结尾根本不在 prompt 里,构造上改不了。"""
    from backend.lib.script_gate import repair_body, split_script

    seen = {}

    def ask(system, user):
        seen["all"] = system + user
        return "压缩后的正文。"

    out = repair_body(_UNFINISHED, "compress_body", 150, "规则", ask)
    _h, _b, payoff = split_script(_UNFINISHED)

    assert payoff not in seen["all"]
    assert out.endswith(payoff)


# ── 开关 ────────────────────────────────────────────────────────────

def test_gate_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setenv("MB_SCRIPT_GATE", "0")
    llm = FakeLlm()
    res = _run(_UNFINISHED, llm)

    assert res.final_script == _UNFINISHED
    assert res.llm_calls == 0


def test_semantic_judge_can_be_switched_off_keeping_patterns(monkeypatch):
    monkeypatch.setenv("MB_SCRIPT_CLOSURE_JUDGE", "0")
    llm = FakeLlm(_judge(True))
    res = _run(_GOOD, llm)

    assert res.llm_calls == 0
    assert res.final_script == _GOOD


def test_every_exit_path_goes_through_the_length_guard():
    """回归锁死:**所有出口都必须经过 finish()** —— 它挂着长度保险丝。

    绕过保险丝直接出厂,4/4 都是 79 秒左右的稿子(目标 60)。
    单一出口是这道保险丝能生效的前提,所以用静态检查钉死。
    """
    import inspect
    import re

    from backend.lib import script_gate

    src = inspect.getsource(script_gate.run_script_gate)
    # 允许 gate 关闭时的早退(那条在 finish 定义之前,且本就不该走闸)
    body = src.split("def finish(", 1)[1]
    bare = re.findall(r"^\s+return result\s*$", body, re.M)
    assert not bare, f"发现 {len(bare)} 处绕过 finish() 的裸 return result"


def test_repaired_but_still_long_script_gets_the_guard():
    """"收口修好了、两轮压缩也没压够"这条路,必须被保险丝接住。

    从"修复成功"的出口直接出厂,保险丝没机会介入。
    """
    from backend.lib.tts_pacing import count_chars, estimate_seconds

    unit = "另外还要补充一段相当长的说明内容用来把稿子撑长一些。"
    long_script = _UNFINISHED + unit * 12
    still_long_body = "仍然很长的正文段落内容需要继续压缩才行。" * 22
    assert estimate_seconds(count_chars(long_script), "qwen_cherry", 1.4) / 60 > 1.10

    llm = FakeLlm(
        _judge(False, "没收口"),
        _tail("一个真正的落点结论句。"), _judge(True),   # r1:收口修好,仍超长
        still_long_body, _judge(True),                  # r2:压了但没压够
        still_long_body, _judge(True),                  # r3
        "终于压到位的短正文。", _judge(True),            # 保险丝:再压一次
    )
    res = _run(long_script, llm, duration=60)

    assert "+fuse" in res.chosen_version, "轮次耗尽仍超长时,保险丝必须介入"
    assert res.chosen_predicted_seconds <= 60.0
    assert 7 <= res.llm_calls <= 10, "保险丝动用的是预留额度"
