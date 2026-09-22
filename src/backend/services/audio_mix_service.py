"""Audio mixing service — concat music segments + mix with narration.

Phase 2.11f — fully decoupled from OpenMontage. The mixing logic was
already self-contained in ``_manual_mix_with_ducking`` (a hand-tuned
ffmpeg filter graph); the OM ``AudioMixer`` import was unused dead code.
For segment concatenation with crossfade we shell out to ffmpeg directly.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from backend.lib.omni_client import _find_bin  # reuse ffmpeg path resolver

logger = logging.getLogger(__name__)


def _float_env(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.2f", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


DEFAULT_MUSIC_VOLUME = _float_env("MEDIA_BUDDY_BGM_VOLUME", 0.14, 0.0, 1.0)
DEFAULT_MUSIC_MIX_WEIGHT = _float_env("MEDIA_BUDDY_BGM_MIX_WEIGHT", 0.45, 0.0, 1.0)

# ── 分轨响度归一目标(根治"配乐盖过人声":每条片都量到同一把尺子)──────────
# 人声归一到 VOICE_TARGET_LUFS;音乐归一到 VOICE_TARGET-MUSIC_GAP_LU(低固定 LU 差)。
# 清晰主导、音乐仅作背景。想让音乐更沉底调大、更有存在感调小(env 可覆盖)。
VOICE_TARGET_LUFS = _float_env("MEDIA_BUDDY_VOICE_TARGET_LUFS", -16.0, -40.0, -6.0)
MUSIC_GAP_LU = _float_env("MEDIA_BUDDY_MUSIC_GAP_LU", 18.0, 0.0, 40.0)

# 增益钳制:防近静音被放大成噪声 / 极端母带被过度处理;撞到边界会告警。
_VOICE_GAIN_CLAMP = (-12.0, 18.0)
_MUSIC_GAIN_CLAMP = (-40.0, 6.0)
# ebur128 集成响度低于此值视为"近静音/无效测量",不据此算增益(走兜底固定系数)。
_SILENCE_FLOOR_LUFS = -70.0


def _parse_ebur128_i(stderr: str) -> Optional[float]:
    """从 ffmpeg `ebur128` 的 stderr 里解析集成响度 I(LUFS)。

    ebur128 每帧都打 `I: <num> LUFS`,最后 Summary 段那条是最终集成值 → 取最后一个。
    测不出 / 近静音(-inf 或 ≤ 静音地板)返回 None(交调用方走兜底)。
    """
    if not stderr:
        return None
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", stderr)
    if not matches:
        return None
    try:
        val = float(matches[-1])
    except ValueError:
        return None
    return None if val <= _SILENCE_FLOOR_LUFS else val


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _compute_gains(
    voice_i: Optional[float],
    music_i: Optional[float],
    voice_target: float = VOICE_TARGET_LUFS,
    gap: float = MUSIC_GAP_LU,
) -> tuple[Optional[float], Optional[float]]:
    """两轨集成响度 → (人声增益 dB, 音乐增益 dB)。纯算术 + 钳制。

    任一输入为 None(测量失败/静音)→ 返回 (None, None),调用方据此走兜底。
    人声推到 voice_target;音乐推到 voice_target-gap(固定低于人声 gap LU)。
    """
    if voice_i is None or music_i is None:
        return None, None
    voice_gain = _clamp(voice_target - voice_i, *_VOICE_GAIN_CLAMP)
    music_gain = _clamp((voice_target - gap) - music_i, *_MUSIC_GAIN_CLAMP)
    return voice_gain, music_gain


class AudioMixService:
    """Two operations: concat music segments + mix narration with music."""

    def __init__(self) -> None:
        # No external mixer dep — _manual_mix_with_ducking owns the ffmpeg
        # filter graph end-to-end.
        pass

    def concat_with_crossfade(
        self,
        segment_paths: list[Path],
        output_path: Path,
        crossfade_seconds: float = 1.5,
    ) -> Optional[Path]:
        """Concatenate audio files with `acrossfade` between adjacent pairs.
        Single segment → just copy. Returns the output path on success."""
        if not segment_paths:
            return None
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Single segment — straight copy (still demux/remux to normalize format)
        if len(segment_paths) == 1:
            try:
                shutil.copy(str(segment_paths[0]), str(output_path))
                return output_path
            except OSError as e:
                logger.warning("concat single-copy failed: %s", e)
                return None

        ffmpeg = _find_bin("ffmpeg")
        cmd: list[str] = [ffmpeg, "-y"]
        for p in segment_paths:
            cmd.extend(["-i", str(p)])

        # Build filter_complex: chain acrossfade across all inputs.
        filter_parts: list[str] = []
        prev_label = "[0:a]"
        d = max(0.1, crossfade_seconds)
        for i in range(1, len(segment_paths)):
            out_label = f"[a{i}]" if i < len(segment_paths) - 1 else "[out]"
            filter_parts.append(
                f"{prev_label}[{i}:a]acrossfade=d={d}:c1=tri:c2=tri{out_label}"
            )
            prev_label = out_label
        filter_complex = ";".join(filter_parts)
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[out]",
            str(output_path),
        ])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("ffmpeg concat failed (run): %s", e)
            return None
        if r.returncode != 0:
            logger.warning(
                "ffmpeg concat exit %d. stderr: %s",
                r.returncode, r.stderr[-500:] if r.stderr else "",
            )
            return None
        if not output_path.exists() or output_path.stat().st_size == 0:
            return None
        return output_path

    def _measure_lufs(self, path: Path) -> Optional[float]:
        """量一条音轨的集成响度(LUFS,EBU R128)。失败/静音返回 None。"""
        ffmpeg = _find_bin("ffmpeg")
        cmd = [
            ffmpeg, "-hide_banner", "-nostats",
            "-i", str(path), "-af", "ebur128", "-f", "null", "-",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("ebur128 measure failed (run) for %s: %s", path, e)
            return None
        return _parse_ebur128_i(r.stderr or "")

    def mix_narration_and_music(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        music_volume: float = DEFAULT_MUSIC_VOLUME,
        video_duration: float | None = None,
        bgm_fade_out_seconds: float = 2.5,
    ) -> Optional[dict]:
        """Mix narration + background music, each loudness-normalized to a target
        so the voice always sits a fixed LU above the music (see
        _manual_mix_with_ducking). Returns a dict of measured/applied levels on
        success (for diagnostics), or None on failure.

        Skips OpenMontage's full_mix entirely — it has a known asplit bug that
        silently corrupts when narration is mono and music is stereo. Goes
        straight to a verified manual ffmpeg filter graph.
        """
        narration_path = Path(narration_path)
        music_path = Path(music_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not narration_path.exists():
            logger.warning("Narration not found: %s", narration_path)
            return None
        if not music_path.exists():
            logger.warning("Music not found: %s", music_path)
            return None

        # P2-14 — env-gated voice-first sidechain ducking. Default OFF → keep the
        # proven static-normalization graph (byte-identical behaviour). Read env
        # at call time (not import) so tests can toggle it per-case.
        if os.environ.get(
            "MEDIA_BUDDY_AI_BGM_DIRECTOR", "0"
        ).strip().lower() in ("1", "true", "yes", "on"):
            return self._mix_with_sidechain_ducking(
                narration_path, music_path, output_path,
                music_volume=music_volume,
                video_duration=video_duration,
                bgm_fade_out_seconds=bgm_fade_out_seconds,
            )

        return self._manual_mix_with_ducking(
            narration_path, music_path, output_path,
            music_volume=music_volume,
            video_duration=video_duration,
            bgm_fade_out_seconds=bgm_fade_out_seconds,
        )

    def _manual_mix_with_ducking(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        music_volume: float = DEFAULT_MUSIC_VOLUME,
        video_duration: float | None = None,
        bgm_fade_out_seconds: float = 2.5,
    ) -> Optional[dict]:
        """Hand-written ffmpeg filtergraph. Returns a dict of measured/applied
        audio levels on success, None on failure.

        Architecture:
          1. aformat both inputs to stereo 48 kHz fltp BEFORE amix.
             CRITICAL: narration is mono 24 kHz, bgm is stereo 48 kHz —
             amix of mismatched layouts silently corrupts and loses music
             entirely. This is the #1 root cause we hit.
          2. **分轨响度归一(根治"配乐盖过人声")**:先各测一遍集成响度(ebur128),
             人声用线性增益推到 VOICE_TARGET_LUFS、音乐推到 VOICE_TARGET-MUSIC_GAP_LU。
             纯增益不压缩,保住各自动态;这跟历史上踩过的"ducking+loudnorm 把音乐压没"
             是两回事(那是动态侧链,这里是静态定标)。测不出 → 兜底回老的固定系数。
          3. Music processing keeps: fade in 2s, EQ cut 3 kHz -4 dB(辅音让路),
             optional fade-out at end.
          4. amix **normalize=0**(关键):两轨电平已各自定标,不能让 amix 再按权重和
             归一把它打乱;峰值兜底交给末端 loudnorm TP=-1.5。兜底路径仍用老的
             weights=1.0 0.85(与改动前逐字一致)。
          5. loudnorm I=-16 LUFS(平台目标 + 峰值安全);均匀增益,保住已设好的 gap。
          6. If video_duration provided, atrim audio to match + BGM fade out.
        """
        ffmpeg = _find_bin("ffmpeg")
        common_fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

        # ── 分轨测量 → 算增益(测不出/静音就兜底回固定系数)──
        voice_i = self._measure_lufs(narration_path)
        music_i = self._measure_lufs(music_path)
        voice_gain_db, music_gain_db = _compute_gains(voice_i, music_i)
        fallback = voice_gain_db is None
        levels: dict = {
            "voice_lufs_in": voice_i,
            "music_lufs_in": music_i,
            "voice_target_lufs": VOICE_TARGET_LUFS,
            "music_gap_lu": MUSIC_GAP_LU,
            "fallback": fallback,
        }

        if fallback:
            # 兜底:老行为逐字复现(人声原样,音乐固定 volume,amix 老权重)。
            logger.warning(
                "audio mix: LUFS 测量失败(voice=%s music=%s)→ 退回固定系数 volume=%.2f",
                voice_i, music_i, music_volume,
            )
            narr_chain = [common_fmt]
            music_gain_filter = f"volume={music_volume}"
            amix = ("amix=inputs=2:duration=first:dropout_transition=2:"
                    "weights=1.0 0.85")
        else:
            if voice_gain_db in _VOICE_GAIN_CLAMP or music_gain_db in _MUSIC_GAIN_CLAMP:
                logger.warning(
                    "audio mix: 增益撞钳制(voice %.1f→%+.1fdB, music %.1f→%+.1fdB)"
                    " — 素材电平极端,回捞看看",
                    voice_i, voice_gain_db, music_i, music_gain_db,
                )
            narr_chain = [common_fmt, f"volume={voice_gain_db}dB"]
            music_gain_filter = f"volume={music_gain_db}dB"
            amix = ("amix=inputs=2:duration=first:dropout_transition=2:"
                    "weights=1 1:normalize=0")
            music_out = VOICE_TARGET_LUFS - MUSIC_GAP_LU
            levels.update(
                voice_lufs_out=VOICE_TARGET_LUFS, music_lufs_out=music_out,
                voice_gain_db=round(voice_gain_db, 1),
                music_gain_db=round(music_gain_db, 1), gap_lu=MUSIC_GAP_LU,
            )
            logger.info(
                "audio mix levels: voice %.1f→%.1f LUFS (%+.1fdB), "
                "music %.1f→%.1f LUFS (%+.1fdB), gap %.1f LU",
                voice_i, VOICE_TARGET_LUFS, voice_gain_db,
                music_i, music_out, music_gain_db, MUSIC_GAP_LU,
            )

        # 音乐处理链:格式 → 增益 → fade in → [fade out] → EQ
        music_chain = [common_fmt, music_gain_filter, "afade=t=in:d=2.0"]
        if video_duration is not None and video_duration > bgm_fade_out_seconds + 1:
            fade_start = max(0.0, video_duration - bgm_fade_out_seconds)
            music_chain.append(f"afade=t=out:st={fade_start}:d={bgm_fade_out_seconds}")
        music_chain.append("equalizer=f=3000:t=q:w=1.5:g=-4")

        # 末端处理:
        #  - 正常路径:两轨已【静态定标】到目标(人声每段已归一到 -16、这里再静态推到 -16),
        #    整个 mix 已在 -16 附近。此时**不再用单遍 loudnorm**——它其实是【动态模式】
        #    (按时间窗口自适应调增益),正是"音量忽大忽小"的另一个来源。改用真峰限幅
        #    alimiter(只防削波、不动态调整),电平稳定不泵。
        #  - 兜底路径(LUFS 测不出、电平未知):保留动态 loudnorm 把它拉到目标。
        #  - env MEDIA_BUDDY_FINAL_DYNAMIC_LOUDNORM=1 可强制回老的动态 loudnorm。
        _force_dyn = os.environ.get("MEDIA_BUDDY_FINAL_DYNAMIC_LOUDNORM", "0").strip().lower() in ("1", "true", "yes", "on")
        if fallback or _force_dyn:
            final_stage = "loudnorm=I=-16:LRA=11:TP=-1.5"
        else:
            # limit=0.841 ≈ -1.5 dBFS 峰值上限,保住峰值安全又不做动态增益。
            final_stage = "alimiter=limit=0.841:asc=1"
        filter_parts = [
            f"[0:a]{','.join(narr_chain)}[narr]",
            f"[1:a]{','.join(music_chain)}[mus]",
            f"[narr][mus]{amix}[mix]",
            f"[mix]{final_stage}[norm]",
        ]
        # Optionally trim final mix to video duration to prevent narration
        # over-running the visible video frames after mux.
        if video_duration is not None:
            filter_parts.append(f"[norm]atrim=end={video_duration:.3f}[out]")
            out_label = "[out]"
        else:
            out_label = "[norm]"
        filter_complex = ";".join(filter_parts)
        cmd = [
            ffmpeg, "-y",
            "-i", str(narration_path),
            "-i", str(music_path),
            "-filter_complex", filter_complex,
            "-map", out_label,
            "-c:a", "pcm_s16le",  # WAV-friendly
            str(output_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("Manual mix run failed: %s", e)
            return None
        if r.returncode != 0:
            logger.warning(
                "Manual mix exit %d. stderr: %s",
                r.returncode, r.stderr[-500:] if r.stderr else "",
            )
            return None
        if not output_path.exists() or output_path.stat().st_size == 0:
            return None
        logger.info("Manual mix succeeded: %s", output_path)
        return levels

    def _mix_with_sidechain_ducking(
        self,
        narration_path: Path,
        music_path: Path,
        output_path: Path,
        music_volume: float = DEFAULT_MUSIC_VOLUME,
        video_duration: float | None = None,
        bgm_fade_out_seconds: float = 2.5,
    ) -> Optional[dict]:
        """P2-14 — voice-first mix with REAL sidechain ducking (opt-in via
        MEDIA_BUDDY_AI_BGM_DIRECTOR). Returns the same ``levels`` dict contract
        as ``_manual_mix_with_ducking`` (with ``ducking=True``), None on failure.

        vs the static path: the BGM here is additionally compressed *keyed by the
        narration* — whenever the presenter speaks, the music is pushed down; in
        the gaps it recovers. It STILL keeps v2's per-track LUFS normalization as
        the base level (so the gap-under-voice guarantee holds), then layers
        dynamic ducking on top.

        Suspected root-cause fix vs the old broken sidechain attempt: BOTH inputs
        are aformat'd to stereo/48k fltp BEFORE the sidechain — a mono-key /
        stereo-main layout mismatch LIKELY corrupted the historical graph. This
        is a hypothesis, not verified: the final alimiter/loudnorm is kept, so if
        the old diagnosis (ducking crushed music) was right, this can still
        quiet: raise sidechain threshold, soften ratio, or lower MUSIC_GAP_LU.
        """
        ffmpeg = _find_bin("ffmpeg")
        common_fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

        # Base levels: reuse the same static per-track normalization as the manual
        # path so the voice-above-music gap is preserved before ducking is added.
        voice_i = self._measure_lufs(narration_path)
        music_i = self._measure_lufs(music_path)
        voice_gain_db, music_gain_db = _compute_gains(voice_i, music_i)
        fallback = voice_gain_db is None
        levels: dict = {
            "voice_lufs_in": voice_i,
            "music_lufs_in": music_i,
            "voice_target_lufs": VOICE_TARGET_LUFS,
            "music_gap_lu": MUSIC_GAP_LU,
            "fallback": fallback,
            "ducking": True,
        }

        if fallback:
            logger.warning(
                "sidechain mix: LUFS 测量失败(voice=%s music=%s)→ 退回固定系数 volume=%.2f",
                voice_i, music_i, music_volume,
            )
            narr_gain_chain = [common_fmt]
            music_gain_filter = f"volume={music_volume}"
            amix = ("amix=inputs=2:duration=first:dropout_transition=2:"
                    "weights=1.0 0.85")
        else:
            narr_gain_chain = [common_fmt, f"volume={voice_gain_db}dB"]
            music_gain_filter = f"volume={music_gain_db}dB"
            amix = ("amix=inputs=2:duration=first:dropout_transition=2:"
                    "weights=1 1:normalize=0")
            music_out = VOICE_TARGET_LUFS - MUSIC_GAP_LU
            levels.update(
                voice_lufs_out=VOICE_TARGET_LUFS, music_lufs_out=music_out,
                voice_gain_db=round(voice_gain_db, 1),
                music_gain_db=round(music_gain_db, 1), gap_lu=MUSIC_GAP_LU,
            )

        # Narration: format + static gain, then split into mix-main + sidechain-key.
        narr_chain = ",".join(narr_gain_chain)

        # Music: format + static gain + fade in/(out) + EQ notch (same as manual).
        music_chain = [common_fmt, music_gain_filter, "afade=t=in:d=2.0"]
        if video_duration is not None and video_duration > bgm_fade_out_seconds + 1:
            fade_start = max(0.0, video_duration - bgm_fade_out_seconds)
            music_chain.append(f"afade=t=out:st={fade_start}:d={bgm_fade_out_seconds}")
        music_chain.append("equalizer=f=3000:t=q:w=1.5:g=-4")

        # sidechaincompress: main = music, key = narration. threshold/ratio/attack/
        sidechain = "sidechaincompress=threshold=0.05:ratio=6:attack=20:release=400"

        _force_dyn = os.environ.get(
            "MEDIA_BUDDY_FINAL_DYNAMIC_LOUDNORM", "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        if fallback or _force_dyn:
            final_stage = "loudnorm=I=-16:LRA=11:TP=-1.5"
        else:
            final_stage = "alimiter=limit=0.841:asc=1"

        filter_parts = [
            f"[0:a]{narr_chain},asplit=2[narr_main][narr_key]",
            f"[1:a]{','.join(music_chain)}[mus]",
            f"[mus][narr_key]{sidechain}[mus_ducked]",
            f"[narr_main][mus_ducked]{amix}[mix]",
            f"[mix]{final_stage}[norm]",
        ]
        if video_duration is not None:
            filter_parts.append(f"[norm]atrim=end={video_duration:.3f}[out]")
            out_label = "[out]"
        else:
            out_label = "[norm]"
        filter_complex = ";".join(filter_parts)
        cmd = [
            ffmpeg, "-y",
            "-i", str(narration_path),
            "-i", str(music_path),
            "-filter_complex", filter_complex,
            "-map", out_label,
            "-c:a", "pcm_s16le",
            str(output_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("Sidechain mix run failed: %s", e)
            return None
        if r.returncode != 0:
            logger.warning(
                "Sidechain mix exit %d. stderr: %s",
                r.returncode, r.stderr[-500:] if r.stderr else "",
            )
            return None
        if not output_path.exists() or output_path.stat().st_size == 0:
            return None
        logger.info("Sidechain-ducked mix succeeded: %s", output_path)
        return levels
