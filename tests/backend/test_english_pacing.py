"""英文路径的长度控制(Spec v2.1 §12)。

## 为什么单独一套

所有中文长度控制都作用在**中文稿**上,而翻译发生在它们**之后** ——
译文长度从没有任何环节量过。影响面:约 236 条/月(第二大音色 elevenlabs_amy)。

不能直接套中文的表:英文按**词/秒**计,中文按**字/秒**,量纲不同。
所以另建一张词/秒表,初期用保守默认值 + fallback 标记,**不假装已经标定过**
"""
import pytest


def test_count_words_is_the_single_english_counting_method():
    from backend.lib.tts_pacing import count_words

    assert count_words("The Count of St Germain fooled Europe") == 7
    assert count_words("  spaced   out   words  ") == 3
    assert count_words("") == 0
    assert count_words(None) == 0


def test_english_estimate_uses_words_not_chars():
    """同一段英文,按词算和按字算差着数量级 —— 用错量纲比不控制还危险。"""
    from backend.lib.tts_pacing import count_chars, count_words, estimate_seconds_en

    text = "The Count of Saint Germain fooled the whole of European high society"
    assert count_chars(text) > count_words(text) * 3
    sec = estimate_seconds_en(count_words(text), "elevenlabs_amy", 1.0)
    assert 3.0 < sec < 12.0


def test_unmeasured_english_voice_is_flagged_as_fallback():
    """没实测过就明说是兜底值,**不假装已经标定过**。"""
    from backend.lib.tts_pacing import english_words_per_second

    rate, is_fallback = english_words_per_second("elevenlabs_amy")
    assert rate > 0
    assert is_fallback is True


def test_unmeasured_english_voice_only_flags_gross_deviation():
    """**没实测过**的音色只抓明显超标 —— 拿没标定的数做精细判断就是拿噪声改稿。"""
    from backend.lib.tts_pacing import english_length_verdict

    assert english_length_verdict(130, "某个没测过的音色", 1.0, 60) == "pass"
    assert english_length_verdict(150, "某个没测过的音色", 1.0, 60) == "pass"   # 略超不管
    assert english_length_verdict(260, "某个没测过的音色", 1.0, 60) == "repair"


def test_measured_english_voice_uses_the_normal_trigger():
    """**实测过**的音色按正常线(1.10)判 —— 预估可信了就该好好判。"""
    from backend.lib.tts_pacing import english_length_verdict

    assert english_length_verdict(150, "qwen_cherry", 1.2, 60) == "pass"
    assert english_length_verdict(200, "qwen_cherry", 1.2, 60) == "repair"


def test_english_target_words_scales_with_duration_and_speed():
    from backend.lib.tts_pacing import english_target_words

    assert english_target_words(60, "elevenlabs_amy", 1.0) > english_target_words(
        30, "elevenlabs_amy", 1.0)
    assert english_target_words(60, "elevenlabs_amy", 1.4) > english_target_words(
        60, "elevenlabs_amy", 1.0)


# ── 翻译后复检 ──────────────────────────────────────────────────────

def test_translated_script_within_budget_passes_untouched():
    from backend.lib.english_gate import check_translated_script

    text = " ".join(["word"] * 140)
    out, acted = check_translated_script(
        text, duration_seconds=60, voice="elevenlabs_amy", speed=1.0, llm=None)

    assert out == text
    assert acted is False


def test_grossly_long_translation_gets_one_tightening_pass():
    """>1.25× → 一次凝练重译(压表达、不删事实、保留结尾)。"""
    from backend.lib.english_gate import check_translated_script

    long_text = " ".join(["word"] * 300)
    tightened = " ".join(["word"] * 150)

    class Llm:
        def __init__(self):
            self.calls = 0

        def _call(self, system, user, purpose=None):
            self.calls += 1
            return tightened

    llm = Llm()
    out, acted = check_translated_script(
        long_text, duration_seconds=60, voice="elevenlabs_amy", speed=1.0, llm=llm)

    assert out == tightened
    assert acted is True
    assert llm.calls == 1, "只做一次 —— 英文表还没实测数据,不值得反复折腾"


def test_translation_tightening_failure_keeps_the_original():
    """fail-open:凝练失败就照发原译文,绝不阻断出片。"""
    from backend.lib.english_gate import check_translated_script

    long_text = " ".join(["word"] * 300)

    class Llm:
        def _call(self, system, user, purpose=None):
            raise RuntimeError("boom")

    out, acted = check_translated_script(
        long_text, duration_seconds=60, voice="elevenlabs_amy", speed=1.0, llm=Llm())

    assert out == long_text


def test_tightening_prompt_forbids_dropping_facts_or_the_ending():
    """压表达可以,删事实和砍结尾不行 —— 后者正是我们花了整轮修好的东西。"""
    from backend.lib.english_gate import _tighten_prompt

    p = _tighten_prompt(150)
    # prompt 里是**补偿后**的数(见 test_tightener_asks_below_target_*),
    # 这里只验证它低于传入值,且事实与结尾的红线还在。
    import re
    nums = [int(n) for n in re.findall(r"\d+", p)]
    assert any(0 < n < 150 for n in nums)
    for must in ("fact", "ending"):
        assert must in p.lower()


def test_english_path_uses_a_qwen_voice_not_elevenlabs():
    """

    ElevenLabs / Azure 已全面停用。我一度按 ElevenLabs 建英文表 —— 假设错了。
    这条测试把事实钉住,免得下次又按停用的厂商去标定。
    """
    from backend.lib.tts_pacing import english_words_per_second

    rate, is_fallback = english_words_per_second("qwen_cherry")
    assert rate > 0
    assert is_fallback is False, "qwen_cherry 的英文词/秒已实测(8 条样本)"


def test_retired_voices_are_not_silently_treated_as_current():
    """已停用音色仍保留标定值(老任务重跑时别再按错的 3.5 算),
    但**不该有人往里加新的** —— 这条测试是给未来的自己看的提醒。"""
    from backend.lib.tts_pacing import _MEASURED_RATE_1X

    retired = [v for v in _MEASURED_RATE_1X if v.startswith(("azure", "elevenlabs"))]
    assert len(retired) == 2, (
        "Azure/ElevenLabs 已停用;新增音色应该只有千问。"
        f"当前停用档音色:{sorted(retired)}"
    )


_EN_GOLDEN = [
    ("qwen_cherry", 164, 1.2, 60.00), ("qwen_cherry", 142, 1.2, 57.63),
    ("qwen_cherry", 180, 1.2, 71.90), ("qwen_cherry", 142, 1.2, 58.50),
    ("qwen_cherry", 154, 1.2, 60.27), ("qwen_cherry", 154, 1.2, 54.60),
    ("qwen_cherry", 182, 1.2, 63.83), ("qwen_cherry", 162, 1.2, 57.70),
]


def test_english_estimate_matches_real_renders():
    """8 条实测样本:预估与实际的中位误差 ≤10%、单条 ≤25%(同中文表的标准)。"""
    import statistics

    from backend.lib.tts_pacing import estimate_seconds_en

    errs = [abs(estimate_seconds_en(w, v, sp) / sec - 1)
            for v, w, sp, sec in _EN_GOLDEN]
    assert statistics.median(errs) <= 0.10
    assert max(errs) <= 0.25


def test_measured_english_voice_is_no_longer_flagged_fallback():
    """实测过就不再是兜底 —— 这个标志位是"这个数可不可信"的唯一依据。"""
    from backend.lib.tts_pacing import english_words_per_second

    rate, is_fallback = english_words_per_second("qwen_cherry")
    assert is_fallback is False
    assert 2.0 < rate < 2.4


def test_the_75_second_render_would_now_be_caught():
    """

    """
    from backend.lib.tts_pacing import english_length_verdict, estimate_seconds_en

    sec = estimate_seconds_en(226, "qwen_cherry", 1.4)
    assert sec > 70, f"新标定应预估到 70 秒以上,实际 {sec:.1f}"
    assert english_length_verdict(226, "qwen_cherry", 1.4, 60) == "repair"


# ── 凝练器也会超写:同一个毛病第四次出现 ────────────────────────────

def test_tightener_asks_below_target_to_offset_its_overshoot():
    """

    **模型不听字数**。所以同样按实际行为补偿,而不是假设它听话。
    """
    from backend.lib.english_gate import TIGHTEN_OVERSHOOT_COMPENSATION, _tighten_prompt

    p = _tighten_prompt(170)
    asked = int(170 * TIGHTEN_OVERSHOOT_COMPENSATION)

    assert str(asked) in p, f"应按补偿后的 {asked} 词去要"
    assert 0.75 <= TIGHTEN_OVERSHOOT_COMPENSATION <= 0.9


def test_tightening_result_is_rechecked_not_blindly_trusted():
    """压完必须复检。实测那条压完仍 70.7 秒 —— 不复检就永远不知道压没压够。"""
    from backend.lib.english_gate import check_translated_script

    long_text = " ".join(["word"] * 300)
    still_long = " ".join(["word"] * 250)      # 压了但没压够

    class Llm:
        def _call(self, system, user, purpose=None):
            return still_long

    out, acted = check_translated_script(
        long_text, duration_seconds=60, voice="qwen_cherry", speed=1.4, llm=Llm())

    # 仍然采用(比原文短就是改善),但必须留痕
    assert out == still_long
    assert acted is True


def test_tightening_that_makes_it_longer_is_rejected():
    """凝练结果比原文还长 → 显然没听懂,退回原文。"""
    from backend.lib.english_gate import check_translated_script

    long_text = " ".join(["word"] * 300)
    longer = " ".join(["word"] * 400)

    class Llm:
        def _call(self, system, user, purpose=None):
            return longer

    out, _acted = check_translated_script(
        long_text, duration_seconds=60, voice="qwen_cherry", speed=1.4, llm=Llm())

    assert out == long_text
