"""M5:拆掉事后裁切,并把两道旧闸和新的三带模型对账。

核心原则:**长度是写稿时的设计约束,不是出片后的裁剪动作。**
`_truncate_short_script` 从尾部砍,等于"长度超了就优先牺牲故事完整性"——方向反了,
而且它砍完不复检,正是本次要根除的模式。
"""
import pytest


class _Series:
    industry_tag = "mystery_explainer"


# ── M5.1 事后裁切必须从主链路消失 ──────────────────────────────────

def test_truncator_is_no_longer_wired_into_the_script_stage():
    """回归锁死:主链路里不许再出现事后砍尾。

    而截断器阈值 403 字 —— 它会把**刚修好的结尾**又砍掉,
    新闸的成果被旧闸抵消。
    """
    from backend.services.pipeline_service import PipelineService

    # 查编译后代码实际引用的全局名,而不是源码文本 —— 后者会被注释里的提及干扰。
    names = set(PipelineService._stage_script.__code__.co_names)
    assert "_truncate_short_script" not in names, "出稿阶段不许再调用事后截断"


def test_runaway_script_gets_one_compress_round_not_a_blind_cut():
    """模型彻底跑飞时,保险丝**压正文**,不做机械删句、更不砍尾。

    压缩走隔离式 compress_body:代码摘出钩子与落点,只把正文交给模型,
    再拼回去 —— 结尾从头到尾没进过 prompt,构造上动不了。
    """
    import json

    from backend.lib.script_gate import run_script_gate, split_script

    unit = "这是一句很长很长的正文内容它足够具体也足够长用来撑满字数。"
    runaway = unit * 12
    short_body = "压缩后的极短正文。"

    def judge(ok):
        return json.dumps({"closure_ok": ok, "closure_reason": "",
                           "introduced_new_facts": False}, ensure_ascii=False)

    class Llm:
        def __init__(self):
            self.queue = [judge(True), short_body, judge(True), short_body, judge(True)]

        def _call(self, system, user, purpose=None):
            return self.queue.pop(0)

    res = run_script_gate(script_v1=runaway, duration_seconds=15, voice="qwen_cherry",
                          speed=1.4, series=None, llm=Llm())

    hook, _body, payoff = split_script(runaway)
    assert short_body in res.final_script, "正文应被压缩"
    assert res.final_script.startswith(hook)      # 钩子逐字保留
    assert res.final_script.endswith(payoff)      # 结尾逐字保留(代码拼回)
    assert len(res.final_script) < len(runaway)


def test_runaway_that_cannot_be_compressed_ships_long_with_an_alarm(caplog):
    """压缩也失败 → **放长出片**并打 error 告警,绝不砍尾、绝不阻断出片。"""
    import json
    import logging

    from backend.lib.script_gate import run_script_gate

    runaway = "这是一句很长很长的正文内容它足够具体也足够长用来撑满字数。" * 12

    def judge(ok):
        return json.dumps({"closure_ok": ok, "closure_reason": "",
                           "introduced_new_facts": False}, ensure_ascii=False)

    class Llm:
        def _call(self, system, user, purpose=None):
            raise RuntimeError("压缩失败")

    class LlmOkJudge(Llm):
        def __init__(self):
            self.first = True

        def _call(self, system, user, purpose=None):
            if self.first:
                self.first = False
                return judge(True)
            raise RuntimeError("压缩失败")

    with caplog.at_level(logging.ERROR):
        res = run_script_gate(script_v1=runaway, duration_seconds=15,
                              voice="qwen_cherry", speed=1.4, series=None,
                              llm=LlmOkJudge())

    assert res.final_script == runaway          # 放长出片,内容一个字没丢
    assert any("length guard" in r.message.lower() for r in caplog.records)


# ── M5.2 硬废稿判定对账 ────────────────────────────────────────────

def test_hard_waste_threshold_uses_the_single_counting_method():
    """P9:硬废稿的字数线必须经 tts_pacing.count_chars 算,不许自带第二套数法。

    旧口径 `_script_content_units` 数 CJK+拉丁词×2、**不含标点**,
    两套并存,阈值必然算错。
    """
    from backend.services.pipeline_service import _short_script_hard_waste_chars
    from backend.lib.tts_pacing import estimate_seconds

    chars = _short_script_hard_waste_chars(60, "qwen_kai", 1.2)
    # 废稿线 = 放行带下限(0.85)的一半 = 0.425 × 时长
    assert abs(estimate_seconds(chars, "qwen_kai", 1.2) / 60 - 0.425) < 0.02


def test_merely_short_script_goes_to_the_gate_not_to_a_template():
    """P4:偏短(但不是残稿)必须归 M3 修,不许退通用模板稿。"""
    from backend.services.pipeline_service import _short_script_quality_issue
    from backend.lib.tts_pacing import short_script_target_chars

    # 造一条 0.6×时长 的稿子:偏短,但远不是残稿
    target = short_script_target_chars(60, "qwen_kai", 1.2)
    script = "。".join(f"这是第{i}句有真实内容的正文" for i in range(12)) + "。"
    script = script[: int(target * 0.65)] + "。"

    issue = _short_script_quality_issue(
        script, "youtube_shorts", 60, tts_provider="qwen_kai", tts_speed=1.2,
    )
    assert issue is None, f"偏短稿不该被判硬废稿(得到 {issue}),应交给验收闸"


def test_真正的残稿_still_rejected():
    """但真正的空/残稿仍然要拦下来 —— 这道闸的存在意义没变。"""
    from backend.services.pipeline_service import _short_script_quality_issue

    assert _short_script_quality_issue("", "youtube_shorts", 60) == "empty_script"
    assert _short_script_quality_issue(
        "评论区告诉我你最想继续看的问题，我下条继续拆。", "youtube_shorts", 60,
    ) == "cta_only_script"


# ── M5.3 post-TTS 灾难闸纳管 ───────────────────────────────────────

def test_disaster_gate_threshold_comes_from_tts_pacing_not_a_magic_number():
    """阈值不许再是散落的 0.35 魔数。"""
    from backend.lib.tts_pacing import disaster_floor_seconds
    from backend.services.pipeline_service import _minimum_short_narration_seconds

    assert _minimum_short_narration_seconds("youtube_shorts", 60) == pytest.approx(
        disaster_floor_seconds(60)
    )
    assert _minimum_short_narration_seconds("youtube_landscape", 600) == 0


def test_disaster_gate_only_catches_catastrophes_not_merely_short_videos():
    """它兜的是"TTS 彻底出错/静音",不是"片子短了点"——后者归三带模型。"""
    from backend.lib.tts_pacing import disaster_floor_seconds

    # 60 秒目标:哪怕只出了 40 秒(老 bug 的典型值),也不该被这道闸判失败
    assert disaster_floor_seconds(60) < 40
