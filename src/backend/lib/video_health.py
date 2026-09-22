"""Post-render video health gate + anomaly classification.

Runs after compose, BEFORE billing settlement. HARD defects (a few-second
"video", an empty / CTA-only script) raise ``VideoHealthError`` so the worker
marks the job failed WITHOUT charging the customer. SOFT anomalies (degraded
footage) are recorded for the admin "suspect videos" board, colour-graded
(🔴 red / 🟠 orange / 🟡 yellow) so the most urgent bugs surface first.

Reuses signals already produced per video:
  - script issue: ``_short_script_quality_issue`` (passed in, avoids a circular import)
  - footage: ``observer_debug.json`` summary (adjacent_same_asset_count,
    source_counts → local_fallback ratio, pass_rate_pct, generic_query_repaired_count,
    duplicate_asset_reuse_count)
  - duration: ffprobe of the final mp4 (passed in)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RED, ORANGE, YELLOW = "red", "orange", "yellow"


def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except Exception:
        return default


MIN_DURATION_RATIO = _envf("MB_HEALTH_MIN_DURATION_RATIO", 0.5)   # mp4 < planned×this → HARD
MIN_ABS_DURATION = _envf("MB_HEALTH_MIN_ABS_DURATION", 5.0)       # mp4 < this seconds → HARD (catches the 4s bug)
FALLBACK_HIGH = _envf("MB_HEALTH_FALLBACK_HIGH", 1.0)            # local_fallback ratio == all → ORANGE
FALLBACK_SOME = _envf("MB_HEALTH_FALLBACK_SOME", 0.3)            # > this (but < HIGH) → YELLOW
PASS_RATE_LOW = _envf("MB_HEALTH_PASS_RATE_LOW", 50.0)          # observer pass_rate_pct < this → YELLOW


class VideoHealthError(Exception):
    """Raised on a HARD defect so the pipeline fails BEFORE billing settlement
    (→ customer is NOT charged). Carries a stable ``code`` + a customer-friendly
    message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HealthResult:
    verdict: str          # healthy | soft_flag | hard_fail
    flags: list           # [{"type": str, "level": red|orange|yellow, "detail": str}]
    signals: dict         # raw numbers for the video_health DB row

    @property
    def is_hard(self) -> bool:
        return self.verdict == "hard_fail"

    def top_red(self) -> Optional[dict]:
        return next((f for f in self.flags if f["level"] == RED), None)


def _load_observer_summary(project_dir) -> dict:
    try:
        data = json.loads((Path(project_dir) / "observer_debug.json").read_text(encoding="utf-8"))
        return data.get("summary") or {}
    except Exception:
        return {}


def evaluate_video_health(
    *,
    project_dir,
    output_duration: float,
    planned_seconds: Optional[float],
    script_issue: Optional[str],   # empty_script | cta_only_script | ... | None
    output_format: str,
) -> HealthResult:
    """Classify a freshly-composed video. Pure (no DB / no raise) — caller decides
    what to do with a hard verdict."""
    flags: list = []
    summary = _load_observer_summary(project_dir)

    src_counts = summary.get("source_counts") or {}
    total_src = sum(int(v) for v in src_counts.values()) if src_counts else 0
    local_n = int(src_counts.get("local_fallback", 0) or 0)
    local_ratio = (local_n / total_src) if total_src else 0.0
    adjacent = int(summary.get("adjacent_same_asset_count", 0) or 0)
    dup_reuse = int(summary.get("duplicate_asset_reuse_count", 0) or 0)
    pass_rate = summary.get("pass_rate_pct")
    generic_repaired = int(summary.get("generic_query_repaired_count", 0) or 0)
    rejected_forbidden = int(summary.get("rejected_forbidden_assets", 0) or 0)
    subject_missing = int(summary.get("subject_missing_count", 0) or 0)
    duration_ratio = (output_duration / planned_seconds) if planned_seconds else None

    # ---------- 🔴 RED — customer gets garbage / billing wrong → auto-fail, no charge ----------
    if script_issue in ("empty_script", "cta_only_script"):
        flags.append({"type": script_issue, "level": RED, "detail": "脚本为空或只剩 CTA 套话"})
    too_short_abs = output_duration > 0 and output_duration < MIN_ABS_DURATION
    too_short_ratio = duration_ratio is not None and duration_ratio < MIN_DURATION_RATIO
    if too_short_abs or too_short_ratio:
        d = f"成片 {output_duration:.1f}s"
        if planned_seconds:
            d += f" / 计划 {int(planned_seconds)}s（{duration_ratio:.0%}）"
        flags.append({"type": "duration_too_short", "level": RED, "detail": d})

    # ---------- 🟠 ORANGE — usable but visibly poor ----------
    if total_src and local_ratio >= FALLBACK_HIGH:
        flags.append({"type": "all_local_fallback", "level": ORANGE,
                      "detail": f"全部 {total_src} 个镜头都是兜底素材"})
    if adjacent > 0:
        flags.append({"type": "adjacent_repeat", "level": ORANGE,
                      "detail": f"{adjacent} 处相邻镜头重复（像卡住）"})
    if generic_repaired > 0:
        flags.append({"type": "generic_query", "level": ORANGE,
                      "detail": f"{generic_repaired} 个镜头退化成泛词搜索（题材可能撞车）"})

    # ---------- 🟡 YELLOW — minor, can defer ----------
    if total_src and FALLBACK_SOME <= local_ratio < FALLBACK_HIGH:
        flags.append({"type": "some_local_fallback", "level": YELLOW,
                      "detail": f"{local_ratio:.0%} 镜头用了兜底素材"})
    if pass_rate is not None and float(pass_rate) < PASS_RATE_LOW:
        flags.append({"type": "low_pass_rate", "level": YELLOW,
                      "detail": f"选片通过率仅 {float(pass_rate):.0f}%"})
    if dup_reuse > 0:
        flags.append({"type": "duplicate_reuse", "level": YELLOW,
                      "detail": f"{dup_reuse} 个素材被重复使用"})
    if subject_missing > 0:
        flags.append({"type": "subject_missing", "level": YELLOW,
                      "detail": f"{subject_missing} 个镜头没搜到主体素材"})
    if rejected_forbidden > 0:
        # The guardrail caught wrong-world clips (e.g. Mayan footage in a Roman
        # video). The shot was recovered, but it means the LLM/stock still
        # surfaced an off-world asset — worth a glance at the queries.
        flags.append({"type": "guardrail_wrong_world", "level": YELLOW,
                      "detail": f"{rejected_forbidden} 个错世界素材被护栏拦下（已自动换片，可复查 query）"})

    has_red = any(f["level"] == RED for f in flags)
    verdict = "hard_fail" if has_red else ("soft_flag" if flags else "healthy")
    signals = {
        "output_duration": round(float(output_duration), 2),
        "planned_seconds": int(planned_seconds) if planned_seconds else None,
        "duration_ratio": round(duration_ratio, 3) if duration_ratio is not None else None,
        "local_fallback_ratio": round(local_ratio, 3),
        "adjacent_same_asset_count": adjacent,
        "duplicate_asset_reuse_count": dup_reuse,
        "pass_rate_pct": float(pass_rate) if pass_rate is not None else None,
        "generic_query_repaired_count": generic_repaired,
        "rejected_forbidden_assets": rejected_forbidden,
        "subject_missing_count": subject_missing,
        "script_issue": script_issue,
        "output_format": output_format,
    }
    return HealthResult(verdict=verdict, flags=flags, signals=signals)


def persist_video_health(
    result: HealthResult,
    *,
    project_id,
    video_job_id=None,
    user_id=None,
    pacing=None,
) -> None:
    """Best-effort write of one ``video_health`` row. NEVER lets a monitoring
    write break a video — swallows all errors.

    ``pacing``(M6)= 语速标定的反馈数据:{predicted_seconds, voice, speed,
    char_count, chosen_version, degraded}。实际时长复用本行已有的 final_seconds。
    几个月无人察觉,正是因为缺它。
    """
    pacing = pacing or {}
    try:
        from backend.database import SessionLocal
        from backend.models.video_health import VideoHealth

        s = result.signals
        row = VideoHealth(
            project_id=str(project_id),
            video_job_id=str(video_job_id) if video_job_id else None,
            user_id=str(user_id) if user_id else None,
            output_format=s.get("output_format"),
            planned_seconds=s.get("planned_seconds"),
            final_seconds=s.get("output_duration"),
            duration_ratio=s.get("duration_ratio"),
            script_issue=s.get("script_issue"),
            local_fallback_ratio=s.get("local_fallback_ratio"),
            adjacent_same_asset_count=s.get("adjacent_same_asset_count"),
            pass_rate_pct=s.get("pass_rate_pct"),
            query_degraded=bool(s.get("generic_query_repaired_count")),
            verdict=result.verdict,
            flags=result.flags,
            predicted_seconds=pacing.get("predicted_seconds"),
            voice=pacing.get("voice"),
            speed=pacing.get("speed"),
            char_count=pacing.get("char_count"),
            chosen_version=pacing.get("chosen_version"),
            degraded=pacing.get("degraded"),
            gate_calls=pacing.get("gate_calls"),
            gate_latency_ms=pacing.get("gate_latency_ms"),
            gate_cost_usd=pacing.get("gate_cost_usd"),
            failure_reason=pacing.get("failure_reason"),
            rounds_used=pacing.get("rounds_used"),
        )
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("persist video_health failed for %s (non-fatal)", project_id)
