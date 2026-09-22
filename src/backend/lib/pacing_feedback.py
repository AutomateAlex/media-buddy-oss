"""语速标定的反馈闭环 —— 让标定错误**能被发现**。

## 为什么有这个模块

**写错难免,真正的问题是错了几个月都没人发现** —— 全库 10558 条 chunk 的
`tts_duration_seconds` 全是 0,从来没有把真实时长写回去过。没有闭环,
系数就永远是拍脑袋。

## 设计

每条成片记一行「预估 vs 实际 + 音色/语速/字数 + 出厂版本」,
按音色滚动统计**中位**偏离,超阈值告警。

系数修订仍由人看过数据再动 `tts_pacing._MEASURED_RATE_1X`。
"""
from __future__ import annotations

import logging
import statistics
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 低于这个值的"漂移"分辨不出是标定错了还是正常波动。
DRIFT_ALERT_THRESHOLD = 0.10
# 少于这么多样本不下结论 —— 两三条数据判不了标定准不准。
DEFAULT_MIN_SAMPLES = 20


def pacing_row_from_gate(gate_result, *, voice: str, speed) -> dict[str, Any]:
    """从验收闸的结果攒出一行校准数据。

    `predicted_seconds` 必须取 **chosen_version 对应的预估** —— 如果记成某个
    被否掉的中间版本,校准数据就被污染了(评审 S5-2)。
    闸被关掉或异常时没有 GateResult,这里必须照常返回一行(预估留空),
    不许因为攒数据把出片搞挂。
    """
    from backend.lib.tts_pacing import count_chars

    row: dict[str, Any] = {
        "voice": str(voice or ""),
        "speed": float(speed or 1.0),
        "predicted_seconds": None,
        "chosen_version": None,
        "degraded": None,
        "char_count": None,
        "gate_calls": None,
        "gate_latency_ms": None,
        "gate_cost_usd": None,
        "failure_reason": None,
        "rounds_used": None,
    }
    if gate_result is None:
        return row
    try:
        row["predicted_seconds"] = float(gate_result.chosen_predicted_seconds)
        row["chosen_version"] = str(gate_result.chosen_version)
        row["degraded"] = bool(gate_result.degraded)
        row["char_count"] = count_chars(gate_result.final_script)
        for key, attr in (("gate_calls", "llm_calls"),
                          ("gate_latency_ms", "latency_ms"),
                          ("gate_cost_usd", "cost_usd"),
                          ("failure_reason", "failure_reason"),
                          ("rounds_used", "rounds_used")):
            if hasattr(gate_result, attr):
                row[key] = getattr(gate_result, attr)
    except Exception:  # noqa: BLE001 — 攒数据永远不许影响出片
        logger.warning("pacing row assembly failed; storing partial row", exc_info=True)
    return row


def pacing_drift_report(
    rows, *, min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, dict[str, Any]]:
    """按音色算「预估/实际 - 1」的中位偏离。

    用**中位数**而不是均值:一条离群样本(比如 TTS 出错只出了 5 秒)不该
    把整个音色判成漂移。
    """
    by_voice: dict[str, list[float]] = {}
    for row in rows or []:
        voice = str((row or {}).get("voice") or "").strip()
        pred = _pos_float(row.get("predicted_seconds"))
        actual = _pos_float(row.get("final_seconds"))
        if not voice:
            continue
        by_voice.setdefault(voice, [])
        if pred is None or actual is None:
            continue
        by_voice[voice].append(pred / actual - 1.0)

    report: dict[str, dict[str, Any]] = {}
    for voice, devs in by_voice.items():
        if len(devs) < max(1, int(min_samples)):
            report[voice] = {
                "samples": len(devs), "median_deviation": None,
                "alert": False, "reason": "insufficient_samples",
            }
            continue
        med = statistics.median(devs)
        alert = abs(med) > DRIFT_ALERT_THRESHOLD
        report[voice] = {
            "samples": len(devs), "median_deviation": med,
            "alert": alert,
            "reason": "drift" if alert else "ok",
        }
        if alert:
            logger.error(
                "pacing drift: voice=%s median deviation %+.1f%% over %d samples — "
                "预估%s实际。请人工复核 tts_pacing 里该音色的系数(不自动改)。",
                voice, med * 100, len(devs), "高于" if med > 0 else "低于",
            )
    return report


def _pos_float(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
