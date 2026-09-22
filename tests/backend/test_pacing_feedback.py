"""M6:把真实成片时长写回,让标定错误**可以被发现**。

全是 0,从来没写回过。没有闭环,系数就永远是拍脑袋。

本模块补的就是这个闭环:每条成片记下「预估 vs 实际」,按音色滚动统计中位偏离,
超阈值告警。**只告警不自动改系数** —— 自动校准会被离群样本带偏,
系数修订仍由人看过数据再改。
"""
import pytest


def _row(voice, predicted, actual, speed=1.4, chars=300):
    return {
        "voice": voice, "predicted_seconds": predicted, "final_seconds": actual,
        "speed": speed, "char_count": chars,
    }


# ── 漂移统计(纯函数,可离线跑)──────────────────────────────────────

def test_no_alert_when_estimates_track_reality():
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = [_row("qwen_kai", 55.0, 56.0), _row("qwen_kai", 55.0, 54.0),
            _row("qwen_kai", 55.0, 55.5)]
    report = pacing_drift_report(rows, min_samples=3)

    assert report["qwen_kai"]["alert"] is False
    assert abs(report["qwen_kai"]["median_deviation"]) < 0.05


def test_alerts_when_a_voice_drifts_more_than_10_percent():
    """典型形态:预估 55 秒,实际只有 42 秒(预估系统性偏高)。"""
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = [_row("qwen_kai", 55.0, 42.0) for _ in range(5)]
    report = pacing_drift_report(rows, min_samples=3)

    assert report["qwen_kai"]["alert"] is True
    assert report["qwen_kai"]["median_deviation"] > 0.10
    assert report["qwen_kai"]["samples"] == 5


def test_uses_median_so_one_outlier_cannot_trigger_a_false_alarm():
    """用中位数而不是均值:一条离群样本不该把整档判成漂移。"""
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = [_row("qwen_kai", 55.0, 55.0) for _ in range(6)]
    rows.append(_row("qwen_kai", 55.0, 5.0))      # 一条离谱的
    report = pacing_drift_report(rows, min_samples=3)

    assert report["qwen_kai"]["alert"] is False


def test_stays_silent_until_there_are_enough_samples():
    """样本不够就不下结论 —— 两条数据判不了标定准不准。"""
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = [_row("qwen_kai", 55.0, 20.0), _row("qwen_kai", 55.0, 20.0)]
    report = pacing_drift_report(rows, min_samples=20)

    assert report["qwen_kai"]["alert"] is False
    assert report["qwen_kai"]["reason"] == "insufficient_samples"


def test_each_voice_is_judged_separately():
    """标定是按音色的,不能把所有音色混在一起算。"""
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = ([_row("qwen_kai", 55.0, 55.0) for _ in range(3)]
            + [_row("qwen_elias", 55.0, 40.0) for _ in range(3)])
    report = pacing_drift_report(rows, min_samples=3)

    assert report["qwen_kai"]["alert"] is False
    assert report["qwen_elias"]["alert"] is True


def test_ignores_rows_with_missing_or_zero_values():
    from backend.lib.pacing_feedback import pacing_drift_report

    rows = [_row("qwen_kai", 55.0, 0), _row("qwen_kai", 0, 55.0),
            _row("qwen_kai", None, 55.0), _row("qwen_kai", 55.0, 55.0)]
    report = pacing_drift_report(rows, min_samples=1)

    assert report["qwen_kai"]["samples"] == 1


def test_never_mutates_the_calibration_table():
    """**只告警,不自动改系数** —— 自动校准会被离群样本带偏。

    行为断言:哪怕报告判定为严重漂移,系数表也必须原封不动。
    (系数修订由人看过数据后手改 tts_pacing._MEASURED_RATE_1X。)
    """
    from backend.lib import tts_pacing
    from backend.lib.pacing_feedback import pacing_drift_report

    before = dict(tts_pacing._MEASURED_RATE_1X)
    report = pacing_drift_report(
        [_row("qwen_kai", 55.0, 30.0) for _ in range(5)], min_samples=3,
    )

    assert report["qwen_kai"]["alert"] is True          # 确实判成了漂移
    assert tts_pacing._MEASURED_RATE_1X == before       # 但系数一个没动


# ── 写回:字段与出厂版本必须对应 ────────────────────────────────────

def test_video_health_carries_the_pacing_columns():
    from backend.models.video_health import VideoHealth

    for col in ("predicted_seconds", "voice", "speed", "char_count",
                "chosen_version", "degraded"):
        assert hasattr(VideoHealth, col), col


def test_lightweight_migration_adds_the_columns_idempotently(tmp_path):
    """项目惯例:启动时幂等加列。跑两遍不许报错。"""
    from sqlalchemy import create_engine, inspect

    from backend.database import Base
    from backend.models import apply_lightweight_migrations
    import backend.models.video_health  # noqa: F401  确保表已注册

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    apply_lightweight_migrations(engine)
    apply_lightweight_migrations(engine)      # 幂等

    cols = {c["name"] for c in inspect(engine).get_columns("video_health")}
    assert {"predicted_seconds", "voice", "char_count"} <= cols


def test_predicted_must_come_from_the_shipped_version():
    """S5-2:预估必须对应**最终出厂**的那一版,否则校准数据被污染。"""
    from backend.lib.pacing_feedback import pacing_row_from_gate

    class Gate:
        chosen_version = "v2"
        chosen_predicted_seconds = 57.3
        degraded = False
        final_script = "定稿正文。" * 20

    row = pacing_row_from_gate(Gate(), voice="qwen_kai", speed=1.2)

    assert row["predicted_seconds"] == 57.3
    assert row["chosen_version"] == "v2"
    assert row["char_count"] == 100      # count_chars 口径(P9)


def test_row_survives_a_missing_gate_result():
    """闸被关掉/异常时没有 GateResult —— 不许因此崩掉出片。"""
    from backend.lib.pacing_feedback import pacing_row_from_gate

    row = pacing_row_from_gate(None, voice="qwen_kai", speed=1.2)

    assert row["predicted_seconds"] is None
    assert row["voice"] == "qwen_kai"
