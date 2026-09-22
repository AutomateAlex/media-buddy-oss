"""结构化定点修复(P11 修改即隔离)。

## 为什么改成这样

原来的做法:让模型返回**整篇**改好的稿子,再用字符级守卫检查它有没有乱动正文。
守卫只能判废,那一轮白花。

新做法:**模型只返回它该改的那一段,其余由代码保管拼接**。
改结尾时它拿不到重写正文的机会;压正文时它根本看不到结尾。
这是"用结构消除错误",而不是"用检查发现错误"——字符守卫随之退役。
"""
import json

import pytest


_SCRIPT = (
    "1745年，一个中年男人走进巴黎沙龙，说自己能把银变成金。"      # hook
    "他讲六门以上语言，路易十五让他住进香波尔城堡。"              # body
    "没有人查得到他的出生记录，他换过十几个化名。"                # body
    "他在俄罗斯出现时，正好赶上叶卡捷琳娜二世政变。"              # body
    "但他最厉害的一招是暗示自己长生不老。"                        # payoff
    "1710年有人在威尼斯见过他，1745年再见，他外貌没变。"          # payoff
)


class Ask:
    """记录每次调用看到了什么 —— 隔离的关键就在"模型看不到什么"。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []
        self.user_only = []

    def __call__(self, system, user):
        # system + user 一起记:断言的是"模型被告知了什么",两边都算
        self.seen.append(f"{system}\n{user}")
        self.user_only.append(user)
        return self.responses.pop(0)


# ── 确定性切分 ──────────────────────────────────────────────────────

def test_splits_into_hook_body_payoff():
    """§5 未做之前的退档方案:首句=钩子,末 2 句=落点,中间=正文。"""
    from backend.lib.script_gate import split_script

    hook, body, payoff = split_script(_SCRIPT)

    assert hook.startswith("1745年")
    assert "但他最厉害的一招" in payoff
    assert "他外貌没变" in payoff
    assert "叶卡捷琳娜" in body
    assert hook + body + payoff == _SCRIPT.replace(" ", "")


def test_split_degrades_gracefully_on_tiny_scripts():
    from backend.lib.script_gate import split_script

    hook, body, payoff = split_script("只有一句话。")
    assert (hook + body + payoff).strip() == "只有一句话。"


# ── replace_tail:模型只交结尾 ───────────────────────────────────────

def test_replace_tail_splices_and_leaves_the_body_byte_identical():
    from backend.lib.script_gate import repair_replace_tail, split_script

    new_tail = "他骗过整个欧洲，靠的是把查得到的真本事和查不到的传说搅在一起。"
    ask = Ask(json.dumps({"replace_last_sentence_count": 2,
                          "replacement_tail": new_tail}, ensure_ascii=False))

    out = repair_replace_tail(_SCRIPT, "没收口", "要给解释", ask)

    assert out.endswith(new_tail)
    # 正文逐字不变 —— 由代码构造保证,不靠事后比对
    old_hook, old_body, _ = split_script(_SCRIPT)
    assert out.startswith(old_hook + old_body)


def test_replace_tail_model_cannot_rewrite_the_body_even_if_it_tries():
    """核心不变式:模型返回什么都改不了正文 —— 它只有结尾的写入权。"""
    from backend.lib.script_gate import repair_replace_tail, split_script

    ask = Ask(json.dumps({
        "replace_last_sentence_count": 2,
        "replacement_tail": "全新的结论句。",
        # 模型试图夹带整篇重写 —— 必须被无视
        "full_script": "完全不同的一篇稿子。它把正文全改了。",
    }, ensure_ascii=False))

    out = repair_replace_tail(_SCRIPT, "没收口", "要给解释", ask)

    hook, body, _ = split_script(_SCRIPT)
    assert out == hook + body + "全新的结论句。"
    assert "完全不同的一篇稿子" not in out


def test_replace_tail_rejects_unparseable_or_empty_returns():
    from backend.lib.script_gate import repair_replace_tail

    assert repair_replace_tail(_SCRIPT, "x", "y", Ask("这不是 JSON")) is None
    assert repair_replace_tail(
        _SCRIPT, "x", "y", Ask(json.dumps({"replacement_tail": "   "})),
    ) is None


def test_replace_tail_prompt_forbids_raising_new_questions():
    """实测教训:修复器越修问题越多(v1「两个核心问题未回答」→ v2「两个新问题未回答」)。"""
    from backend.lib.script_gate import repair_replace_tail

    ask = Ask(json.dumps({"replace_last_sentence_count": 1,
                          "replacement_tail": "结论句。"}, ensure_ascii=False))
    repair_replace_tail(_SCRIPT, "两个核心问题未回答", "要给解释", ask)

    prompt = ask.seen[0]
    assert "新问题" in prompt or "新的悬念" in prompt
    assert "两个核心问题未回答" in prompt, "必须把复检判词带给修复器"


# ── compress/expand_body:模型看不到结尾 ─────────────────────────────

def test_compress_body_never_shows_the_ending_to_the_model():
    """结尾不在 prompt 里,模型就不可能改坏它 —— 这是隔离的全部意义。"""
    from backend.lib.script_gate import repair_body, split_script

    ask = Ask("压缩后的正文内容。")
    out = repair_body(_SCRIPT, "compress_body", 200, "要给解释", ask)

    hook, _body, payoff = split_script(_SCRIPT)
    assert payoff not in ask.seen[0], "结尾绝不能出现在压正文的 prompt 里(system+user 都不许)"
    assert out == hook + "压缩后的正文内容。" + payoff


def test_expand_body_keeps_hook_and_payoff_verbatim():
    from backend.lib.script_gate import repair_body, split_script

    ask = Ask("补充了一个论据的正文。")
    out = repair_body(_SCRIPT, "expand_body", 400, "要给解释", ask)

    hook, _b, payoff = split_script(_SCRIPT)
    assert out.startswith(hook)
    assert out.endswith(payoff)


def test_body_repair_rejects_an_empty_return():
    from backend.lib.script_gate import repair_body

    assert repair_body(_SCRIPT, "compress_body", 200, "r", Ask("  ")) is None


def test_compress_target_aims_at_the_internal_goal_not_the_cap():
    """§7:压缩瞄内部目标(0.92×档位),不瞄档位上限 —— 否则压完还贴着红线。

    注意 prompt 里出现的是**补偿后**的数(见 test_compress_asks_below_target_*),
    这里只验证它确实低于传入的目标,而不是照抄档位上限。
    """
    import re

    from backend.lib.script_gate import repair_body

    ask = Ask("压好的正文。")
    repair_body(_SCRIPT, "compress_body", 250, "规则", ask)

    nums = [int(n) for n in re.findall(r"\d+", ask.seen[0])]
    assert any(0 < n < 250 for n in nums), "压缩目标应低于传入值(超写补偿)"


# ── 字符守卫退役 ────────────────────────────────────────────────────

def test_character_guard_is_no_longer_a_judging_gate():
    """P11:隔离落地后,守卫降级为 debug 断言,不再判废。

    它当初存在的理由(模型可能乱改)已经由结构消除;继续用它判废只会误杀。
    """
    from backend.lib import script_gate

    assert not hasattr(script_gate, "body_preserved") or (
        getattr(script_gate, "_GUARD_IS_DEBUG_ONLY", False)
    ), "body_preserved 若保留,必须标记为仅调试用"


def test_compress_asks_below_target_to_offset_measured_overshoot():
    """

    所以要按补偿后的数去要 —— 和字/秒标定同一个道理:
    按模型的**实际行为**校准,而不是假设它听话。
    """
    from backend.lib.script_gate import COMPRESS_OVERSHOOT_COMPENSATION, repair_body

    ask = Ask("压好的正文。")
    repair_body(_SCRIPT, "compress_body", 250, "规则", ask)

    asked = int(250 * COMPRESS_OVERSHOOT_COMPENSATION)
    assert str(asked) in ask.seen[0], f"应按补偿后的 {asked} 字去要,而不是 250"
    assert 0.7 <= COMPRESS_OVERSHOOT_COMPENSATION <= 0.85


def test_expand_does_not_get_the_compression_compensation():
    """扩写不补偿 —— 超写对"太短"是帮忙,再往下压就本末倒置了。"""
    from backend.lib.script_gate import repair_body

    ask = Ask("补长的正文。")
    repair_body(_SCRIPT, "expand_body", 400, "规则", ask)

    assert "400" in ask.seen[0]
