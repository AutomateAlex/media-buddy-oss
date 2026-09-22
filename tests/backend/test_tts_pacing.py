"""口播语速标定 + 长度三带模型(短视频字数预算)。

客户按 1 分钟付费却只拿到 47 秒的片子,模型也因为预算太小而把故事讲一半就收笔。

每音色 6 条。存原始值而不是均值,是为了阈值日后可复算(见 test_golden_fixture_*)。
"""
import statistics

import pytest


_GOLDEN = [
    ("qwen_kai", 277, 1.2, 47.93), ("qwen_kai", 264, 1.2, 50.20),
    ("qwen_kai", 270, 1.2, 45.30), ("qwen_kai", 274, 1.2, 50.33),
    ("qwen_kai", 199, 1.2, 36.70), ("qwen_kai", 261, 1.2, 48.20),
    ("qwen_neil", 241, 1.2, 41.13), ("qwen_neil", 226, 1.2, 40.70),
    ("qwen_neil", 322, 1.4, 49.77), ("qwen_neil", 287, 1.4, 39.27),
    ("qwen_neil", 306, 1.4, 49.33), ("qwen_neil", 319, 1.4, 51.70),
    ("qwen_cherry", 151, 1.4, 27.47), ("qwen_cherry", 153, 1.4, 23.33),
    ("qwen_cherry", 153, 1.4, 24.40), ("qwen_cherry", 156, 1.4, 23.20),
    ("qwen_cherry", 200, 1.2, 35.33), ("qwen_cherry", 151, 1.4, 22.33),
    ("qwen_ethan", 284, 1.4, 37.07), ("qwen_ethan", 233, 1.4, 35.17),
    ("qwen_ethan", 239, 1.4, 35.13), ("qwen_ethan", 305, 1.4, 41.53),
    ("qwen_ethan", 267, 1.4, 36.97), ("qwen_ethan", 269, 1.4, 40.90),
    ("qwen_elias", 247, 1.4, 50.50), ("qwen_elias", 209, 1.4, 39.13),
    ("qwen_elias", 306, 1.4, 57.93), ("qwen_elias", 306, 1.4, 48.90),
    ("qwen_elias", 306, 1.4, 61.00), ("qwen_elias", 250, 1.4, 46.63),
    ("qwen_moon", 261, 1.2, 51.70), ("qwen_moon", 222, 1.2, 49.53),
    ("qwen_moon", 248, 1.2, 46.10), ("qwen_moon", 252, 1.2, 44.50),
    ("qwen_moon", 221, 1.2, 48.63), ("qwen_moon", 259, 1.2, 47.30),
    ("azure_yunyang", 210, 1.0, 42.40), ("azure_yunyang", 224, 1.0, 47.17),
    ("azure_yunyang", 217, 1.0, 45.27), ("azure_yunyang", 218, 1.0, 44.97),
    ("azure_yunyang", 217, 1.0, 47.47), ("azure_yunyang", 225, 1.0, 47.00),
    ("azure_yunxi", 302, 1.0, 65.07), ("azure_yunxi", 231, 1.0, 50.63),
    ("azure_yunxi", 255, 1.0, 54.10), ("azure_yunxi", 174, 1.0, 40.13),
    ("azure_yunxi", 292, 1.0, 63.53), ("azure_yunxi", 241, 1.0, 51.77),
]

_VOICES = sorted({v for v, *_ in _GOLDEN})


def _errors_by_voice():
    from backend.lib.tts_pacing import estimate_seconds

    out = {}
    for voice, chars, speed, actual in _GOLDEN:
        err = abs(estimate_seconds(chars, voice, speed) / actual - 1)
        out.setdefault(voice, []).append(err)
    return out


# ── golden fixture:标定准不准 ──────────────────────────────────────────

@pytest.mark.parametrize("voice", _VOICES)
def test_golden_fixture_median_error_within_10pct(voice):
    """每音色**中位**绝对误差 ≤ 10% —— 这才是"标定准不准"。"""
    errs = _errors_by_voice()[voice]
    assert statistics.median(errs) <= 0.10, (
        f"{voice} 中位误差 {statistics.median(errs)*100:.1f}%"
    )


@pytest.mark.parametrize("voice", _VOICES)
def test_golden_fixture_no_sample_beyond_25pct(voice):
    """

    """
    errs = _errors_by_voice()[voice]
    assert max(errs) <= 0.25, f"{voice} 最大误差 {max(errs)*100:.1f}%"


def test_golden_fixture_keeps_raw_triples_not_averages():
    """fixture 必须存原始三元组,否则日后调阈值无从复算。"""
    assert len(_GOLDEN) >= 36
    for voice, chars, speed, seconds in _GOLDEN:
        assert chars > 0 and speed > 0 and seconds > 0


# ── P9:全系统唯一的数字数方法 ─────────────────────────────────────────

def test_count_chars_is_non_whitespace_including_punctuation():
    """P9 口径:非空白字符,**含中文标点**。

    """
    from backend.lib.tts_pacing import count_chars

    assert count_chars("你好，世界。") == 6          # 含 2 个标点
    assert count_chars(" 你好 \n 世界 ") == 4        # 空白不计
    assert count_chars("") == 0
    assert count_chars(None) == 0


# ── estimate_seconds ────────────────────────────────────────────────

def test_estimate_seconds_inverts_the_budget():
    from backend.lib.tts_pacing import estimate_seconds, short_script_target_chars

    for voice in _VOICES:
        for speed in (1.0, 1.2, 1.4):
            t = short_script_target_chars(60, voice, speed)
            # 目标字数反算回来,应约等于 60 × TARGET_FACTOR
            assert 50.0 <= estimate_seconds(t, voice, speed) <= 60.0 + 1e-6


def test_estimate_seconds_scales_with_speed():
    from backend.lib.tts_pacing import estimate_seconds

    fast = estimate_seconds(300, "qwen_kai", 1.4)
    slow = estimate_seconds(300, "qwen_kai", 1.0)
    assert fast < slow  # 语速越快,同样字数念得越短


# ── 三带模型 ────────────────────────────────────────────────────────

def test_length_verdict_three_bands():
    from backend.lib.tts_pacing import chars_per_second_1x, length_verdict

    voice, speed, duration = "qwen_kai", 1.2, 60.0
    rate = chars_per_second_1x(voice) * speed

    def chars_for(ratio):
        return int(duration * ratio * rate)

    assert length_verdict(chars_for(0.95), voice, speed, duration) == "pass"
    assert length_verdict(chars_for(0.86), voice, speed, duration) == "pass"
    assert length_verdict(chars_for(0.80), voice, speed, duration) == "gray"
    assert length_verdict(chars_for(1.05), voice, speed, duration) == "gray"
    assert length_verdict(chars_for(0.60), voice, speed, duration) == "repair"
    assert length_verdict(chars_for(1.40), voice, speed, duration) == "repair"


def test_length_verdict_never_repairs_unmeasured_voices():
    """

    拿一个编造的预估去触发 LLM 改稿,是**为噪声改稿**(违反 P8),
    而且会把 ElevenLabs(月产 236 条)全部卷进无谓的修复。
    """
    from backend.lib.tts_pacing import length_verdict

    for voice in ("elevenlabs_mingyao", "azure_xiaoxiao", "某个新音色"):
        for chars in (50, 200, 500, 2000):
            assert length_verdict(chars, voice, 1.2, 60.0) == "pass", voice


def test_length_bands_are_configurable(monkeypatch):
    """阈值可调,但**必须整体保持单调** —— 只抬 TRIGGER_HIGH 不抬灾难线会被校验拦下。"""
    import importlib

    monkeypatch.setenv("MB_LEN_TRIGGER_HIGH", "1.45")
    monkeypatch.setenv("MB_LEN_HARD_HIGH", "1.60")
    import backend.lib.tts_pacing as tp
    importlib.reload(tp)
    try:
        rate = tp.chars_per_second_1x("qwen_kai") * 1.2
        assert tp.length_verdict(int(60 * 1.40 * rate), "qwen_kai", 1.2, 60.0) == "gray"
    finally:
        monkeypatch.delenv("MB_LEN_TRIGGER_HIGH", raising=False)
        monkeypatch.delenv("MB_LEN_HARD_HIGH", raising=False)
        importlib.reload(tp)


# ── 护栏(恒等式)────────────────────────────────────────────────────

def test_target_lands_in_pass_band_guardrail():
    """恒等式护栏:只守 TARGET_FACTOR 不漂出放行带,**不验证 rate 正确性**。

    (rate 正确性由上面的 golden fixture 守短期、由 M6 回写闭环守长期。)
    """
    from backend.lib.tts_pacing import length_verdict, short_script_target_chars

    for voice in _VOICES:
        for speed in (0.7, 1.0, 1.2, 1.4, 2.0):
            for duration in (15, 30, 45, 60, 90):
                t = short_script_target_chars(duration, voice, speed)
                assert length_verdict(t, voice, speed, duration) == "pass"


# ── 沿用档不变 ──────────────────────────────────────────────────────

def test_unmeasured_voices_keep_the_old_budget_byte_for_byte():
    """没实测过的音色(ElevenLabs/Azure/未知)目标字数一个字都不能变。"""
    from backend.lib.tts_pacing import short_script_target_chars

    for provider in ("elevenlabs_mingyao", "azure_xiaoxiao", "什么新音色"):
        for speed in (1.0, 1.2, 1.4):
            for duration in (15, 30, 60):
                assert short_script_target_chars(duration, provider, speed) == int(
                    duration * 3.5 * speed
                ), provider


def test_unmeasured_qwen_voice_uses_the_qwen_default():
    from backend.lib.tts_pacing import chars_per_second_1x, QWEN_DEFAULT_RATE_1X

    assert chars_per_second_1x("qwen_vincent") == QWEN_DEFAULT_RATE_1X


def test_measured_qwen_rates_are_faster_than_the_legacy_constant():
    from backend.lib.tts_pacing import _MEASURED_RATE_1X

    fast = [v for v, r in _MEASURED_RATE_1X.items() if r > 4.2]
    assert len(fast) >= 4
    assert _MEASURED_RATE_1X["qwen_elias"] < 4.0


# ── 其它 ────────────────────────────────────────────────────────────

def test_cap_sits_above_target_but_floor_sits_below():
    from backend.lib.tts_pacing import (
        short_script_target_chars, short_script_max_chars, short_script_min_chars,
    )

    for provider in ("qwen_kai", "azure_yunyang"):
        target = short_script_target_chars(60, provider, 1.2)
        assert short_script_min_chars(60, provider, 1.2) < target
        assert short_script_max_chars(60, provider, 1.2) > target


def test_prompt_range_stays_inside_the_truncation_cap():
    """回归:prompt 允许上限必须低于硬截断阈值,否则模型写到上沿反被砍掉结尾。"""
    from backend.lib.tts_pacing import short_script_char_range, short_script_max_chars

    voices = (*_VOICES, "qwen_vincent", "azure_yunyang", "elevenlabs_amy", "未知")
    for provider in voices:
        for speed in (0.7, 1.0, 1.2, 1.4, 2.0):
            for duration in (15, 30, 45, 60, 90):
                _lo, hi = short_script_char_range(duration, provider, speed)
                cap = short_script_max_chars(duration, provider, speed)
                assert hi <= cap, f"{provider}@{speed}x/{duration}s: {hi} > {cap}"


def test_speed_is_clamped_to_the_supported_range():
    from backend.lib.tts_pacing import short_script_target_chars as T

    assert T(60, "qwen_kai", 99) == T(60, "qwen_kai", 2.0)
    assert T(60, "qwen_kai", 0) == T(60, "qwen_kai", 1.0)


# ── 灾难线 + env 启动校验(Spec v2.1 §6.4/§6.5)──────────────────────

def test_disaster_line_sits_above_the_repair_trigger():
    """灾难线(1.25)是**独立于修复触发线(1.10)的第四条线**。

    分工:>1.10 触发修复;>1.25 才是"灾难",决定 fail-closed 策略。
    两者不能颠倒,否则要么修复永不触发,要么正常波动被当灾难。
    """
    from backend.lib.tts_pacing import HARD_HIGH, _TRIGGER_HIGH

    assert HARD_HIGH > _TRIGGER_HIGH
    assert HARD_HIGH == pytest.approx(1.25)


def test_is_disaster_length_flags_only_real_disasters():
    from backend.lib.tts_pacing import chars_per_second_1x, is_disaster_length

    voice, speed, duration = "qwen_kai", 1.2, 60.0
    rate = chars_per_second_1x(voice) * speed

    # 66 秒(1.10,灰区上界)不是灾难
    assert is_disaster_length(int(60 * 1.10 * rate), voice, speed, duration) is False
    assert is_disaster_length(int(60 * 1.52 * rate), voice, speed, duration) is True


def test_unmeasured_voice_is_never_called_a_disaster():
    """没实测过的音色预估本就不可信,不能拿它判灾难(同 length_verdict 的纪律)。"""
    from backend.lib.tts_pacing import is_disaster_length

    assert is_disaster_length(9999, "elevenlabs_mingyao", 1.2, 60.0) is False


def test_band_config_must_be_monotonic_or_startup_fails():
    """阈值配错(比如把 PASS_HIGH 配得比 TRIGGER_HIGH 还大)不该静默生效。

    这类错误的表现是"闸行为诡异但不报错",排查代价极高 —— 宁可拒绝启动。
    """
    from backend.lib.tts_pacing import validate_bands

    validate_bands(0.75, 0.85, 1.00, 1.10, 1.25)          # 合法,不抛

    with pytest.raises(ValueError, match="MB_LEN"):
        validate_bands(0.75, 0.85, 1.20, 1.10, 1.25)      # PASS_HIGH > TRIGGER_HIGH
    with pytest.raises(ValueError, match="MB_LEN"):
        validate_bands(0.90, 0.85, 1.00, 1.10, 1.25)      # TRIGGER_LOW > PASS_LOW
    with pytest.raises(ValueError, match="MB_LEN"):
        validate_bands(0.75, 0.85, 1.00, 1.30, 1.25)      # TRIGGER_HIGH >= HARD_HIGH


def test_current_config_is_valid_at_import():
    """现役配置必须自洽 —— 模块导入时就校验过了,这里再钉一遍。"""
    from backend.lib.tts_pacing import (
        HARD_HIGH, _PASS_HIGH, _PASS_LOW, _TRIGGER_HIGH, _TRIGGER_LOW, validate_bands,
    )

    validate_bands(_TRIGGER_LOW, _PASS_LOW, _PASS_HIGH, _TRIGGER_HIGH, HARD_HIGH)
