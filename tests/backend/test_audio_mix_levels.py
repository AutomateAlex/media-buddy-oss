"""分轨响度归一的纯逻辑测试 —— 算增益 + 解析 ebur128 集成响度。

覆盖"配乐盖过人声"修复的可单测部分:给定两轨集成响度,算出把人声推到目标、
音乐压到"低于人声固定 LU 差"所需的增益(带钳制);以及从 ffmpeg ebur128 的
stderr 里正确取到集成响度 I。
"""
from backend.services.audio_mix_service import _compute_gains, _parse_ebur128_i

T = -16.0   # voice target LUFS
G = 14.0    # music gap LU  → music target -30


def test_typical_light_voice_loud_music():
    # 人声偏轻(-22)、音乐偏响(-12):人声 +6dB 顶到 -16;音乐 -18dB 压到 -30
    v, m = _compute_gains(-22.0, -12.0, T, G)
    assert v == 6.0    # -16 - (-22)
    assert m == -18.0  # (-16-14) - (-12) = -30 + 12


def test_already_balanced_no_gain():
    v, m = _compute_gains(-16.0, -30.0, T, G)
    assert v == 0.0 and m == 0.0


def test_voice_clamped_up_when_extremely_quiet():
    # 人声 -40 需 +24dB,但钳制上限 +18(防近静音放大成噪声)
    v, m = _compute_gains(-40.0, -30.0, T, G)
    assert v == 18.0 and m == 0.0


def test_voice_clamped_down_when_too_loud():
    # 人声 -2(过响)需 -14dB,钳到 -12
    v, _ = _compute_gains(-2.0, -30.0, T, G)
    assert v == -12.0


def test_music_clamped_up_when_quiet():
    # 音乐 -55 需 +25dB 才到 -30,钳到 +6
    _, m = _compute_gains(-16.0, -55.0, T, G)
    assert m == 6.0


def test_loud_music_master_pushed_down():
    # 响母带 -6 → 需 -24dB(在 -40..6 范围内),照压
    _, m = _compute_gains(-16.0, -6.0, T, G)
    assert m == -24.0


def test_none_inputs_fall_back():
    assert _compute_gains(None, -12.0, T, G) == (None, None)
    assert _compute_gains(-22.0, None, T, G) == (None, None)
    assert _compute_gains(None, None, T, G) == (None, None)


def test_gap_widens_music_further_down():
    # gap 越大音乐越沉底:gap=16 → 音乐目标 -32
    _, m = _compute_gains(-16.0, -12.0, T, 16.0)
    assert m == -20.0  # (-16-16) - (-12) = -32 + 12


# ── ebur128 解析 ──────────────────────────────────────────────────────

_EBUR128_STDERR = """\
[Parsed_ebur128_0 @ 0x1] t: 0.1  TARGET:-23 LUFS    M: -20.5 S:-120.7     I: -20.5 LUFS       LRA:  0.0 LU
[Parsed_ebur128_0 @ 0x1] t: 0.2  TARGET:-23 LUFS    M: -19.1 S:-120.7     I: -19.8 LUFS       LRA:  0.0 LU
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -16.2 LUFS
    Threshold: -26.2 LUFS

  Loudness range:
    LRA:         4.1 LU
"""


def test_parse_takes_summary_integrated():
    # 逐帧也打 I:,取最后一个(Summary 段的最终集成值)
    assert _parse_ebur128_i(_EBUR128_STDERR) == -16.2


def test_parse_no_match_returns_none():
    assert _parse_ebur128_i("no loudness here") is None
    assert _parse_ebur128_i("") is None


def test_parse_silence_below_floor_returns_none():
    # 近静音(≤ -70 地板)不据此算增益,交兜底
    assert _parse_ebur128_i("    I:         -70.0 LUFS") is None
    assert _parse_ebur128_i("    I:         -120.7 LUFS") is None


def test_parse_inf_returns_none():
    # 纯静音 ffmpeg 打 -inf,正则不匹配非数字 → 无有效值
    assert _parse_ebur128_i("    I:         -inf LUFS") is None
