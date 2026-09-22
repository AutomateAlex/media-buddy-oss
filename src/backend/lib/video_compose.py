"""

Replaces the AGPLv3 OpenMontage ``tools.video.video_compose`` import. The
upstream component supported multiple render runtimes (Remotion, ffmpeg)
and a complex effects DSL we never used; we only ever called it with
``render_runtime="ffmpeg"`` + a flat list of cuts. So we write a focused
ffmpeg-based composer here that does exactly the four things we need:

    1. trim each source clip to (in_seconds, out_seconds)
    2. scale + pad to the requested profile's resolution
    3. concat all trimmed clips into one video
    4. mux with the supplied audio (if any), trimming to its duration

Subtitle burning is intentionally OUT of scope here — pipeline_service
handles it post-compose (so the bottom-safe-zone treatment works
uniformly for portrait + landscape).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.lib.ffmpeg_locator import video_encoder_args

logger = logging.getLogger(__name__)


def _compose_timeout(default: int) -> int:
    """ffmpeg compose timeout (seconds), env-overridable for slow/large boxes.
    A ~10-min 1080p concat of 80+ clips on a 2-core box exceeds the 900s default;
    set MEDIA_BUDDY_COMPOSE_TIMEOUT on the worker to give it room."""
    raw = (os.environ.get("MEDIA_BUDDY_COMPOSE_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(default, int(raw))
        except ValueError:
            pass
    return default


# Profile → output resolution. These match what the rest of the pipeline
# (reframe, subtitle safe-zone) expects.
PROFILE_DIMENSIONS: dict[str, tuple[int, int]] = {
    "youtube_landscape": (1920, 1080),
    "youtube_shorts":    (1080, 1920),
    "tiktok":            (1080, 1920),
    "instagram_reels":   (1080, 1920),
    "instagram_feed":    (1080, 1080),
}
DEFAULT_PROFILE = "youtube_landscape"
TARGET_FPS = 30

PROFILE_ALIASES = {
    "youtube_short": "youtube_shorts",
    "youtube-short": "youtube_shorts",
    "youtube-shorts": "youtube_shorts",
    "short": "youtube_shorts",
    "shorts": "youtube_shorts",
    "yt_short": "youtube_shorts",
    "yt_shorts": "youtube_shorts",
    "reels": "instagram_reels",
    "instagram-reels": "instagram_reels",
    "ig_reels": "instagram_reels",
    "douyin": "tiktok",
    "vertical": "youtube_shorts",
    "vertical_9_16": "youtube_shorts",
    "portrait": "youtube_shorts",
}


def _normalize_profile(profile: str | None) -> str:
    key = str(profile or DEFAULT_PROFILE).strip().lower()
    return PROFILE_ALIASES.get(key, key)


@dataclass
class ComposeResult:
    """Mirrors OpenMontage's ToolResult shape so ``pipeline_service``
    (which reads ``result.success`` / ``result.error`` / ``result.artifacts``)
    keeps working with no changes."""
    success: bool
    output_path: Optional[str] = None
    error: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def _probe_duration(path: Path) -> float:
    """Return container duration in seconds, 0.0 on failure."""
    ffprobe = _find_ffprobe()
    if ffprobe is None:
        return 0.0
    try:
        r = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _subject_crop_damping() -> float:
    """主体裁切阻尼系数。1.0=完全按判官给的主体位置裁、0=永远居中。
    又留一点抗噪。env ``MEDIA_BUDDY_SUBJECT_CROP_DAMPING`` 可调(夹在 [0,1])。"""
    try:
        return max(0.0, min(1.0, float(os.environ.get("MEDIA_BUDDY_SUBJECT_CROP_DAMPING", "0.95"))))
    except (TypeError, ValueError):
        return 0.95


def subject_crop_x_expr(subject_h_pos: object, *, damping: Optional[float] = None) -> Optional[str]:
    """ffmpeg ``crop`` x expression that keeps the main subject in frame for the
    landscape→portrait cover-crop, or ``None`` to use the default center crop.

    ``subject_h_pos`` is the subject's horizontal center as a fraction of width
    (0.0 = left edge, 0.5 = center, 1.0 = right edge) — produced by the observer
    vision judge — or ``None`` when unknown. We damp toward center so detection
    noise can't slam the subject hard against an edge, then build
    ``x = clip(iw*p - ow/2, 0, iw-ow)`` (commas escaped for inline use in a
    filter_complex). Returns ``None`` (→ keep today's byte-identical center crop)
    when the value is missing/out of range/≈center, or the feature is disabled
    via ``MEDIA_BUDDY_SUBJECT_AWARE_CROP=0``.
    """
    if os.environ.get("MEDIA_BUDDY_SUBJECT_AWARE_CROP", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    if subject_h_pos is None:
        return None
    try:
        p = float(subject_h_pos)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (0.0 <= p <= 1.0):
        return None
    damp = _subject_crop_damping() if damping is None else damping
    p = 0.5 + (p - 0.5) * damp
    if abs(p - 0.5) < 1e-3:
        return None
    return f"clip(iw*{p:.4f}-ow/2\\,0\\,iw-ow)"


def _scale_pad_filter(width: int, height: int, crop_x_expr: Optional[str] = None) -> str:
    """Generic cover scaler.

    The reframe step normally prepares clips for the target aspect, but this
    final defensive pass must still never emit black bars for portrait social
    videos. Scale to cover, then crop to the requested canvas — centered by
    default, or shifted toward the subject when ``crop_x_expr`` is given.
    """
    crop = f"crop={width}:{height}"
    if crop_x_expr:
        crop = f"crop={width}:{height}:x={crop_x_expr}"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"{crop},"
        f"setsar=1,fps={TARGET_FPS}"
    )


def _probe_dims(path: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream, (0, 0) on failure."""
    ffprobe = _find_ffprobe()
    if ffprobe is None:
        return (0, 0)
    try:
        r = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        parts = (r.stdout or "").strip().split("x")
        if len(parts) >= 2:
            return (int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return (0, 0)


def _blur_bg_config() -> dict:
    """Read the short-video blurred-background settings from env (defaults
    per render."""
    def _flag(name: str, default: bool) -> bool:
        raw = os.environ.get(name, "1" if default else "0").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _num(name: str, default: float) -> float:
        try:
            return float((os.environ.get(name) or "").strip() or default)
        except ValueError:
            return default

    # Three-state global switch:
    #   "1"/"on"  → force blur on for every portrait short (global override)
    #   "0"/"off" → force blur off everywhere
    #   unset     → None: respect the per-project framing_style choice
    raw_bg = (os.environ.get("MEDIA_BUDDY_SHORTS_BLUR_BACKGROUND") or "").strip().lower()
    if raw_bg in {"1", "true", "yes", "on"}:
        force: Optional[bool] = True
    elif raw_bg in {"0", "false", "no", "off"}:
        force = False
    else:
        force = None

    return {
        "force": force,
        "skip_native": _flag("MEDIA_BUDDY_SHORTS_BLUR_SKIP_NATIVE_PORTRAIT", True),
        "sigma": _num("MEDIA_BUDDY_SHORTS_BLUR_SIGMA", 20.0),
        "dim": _num("MEDIA_BUDDY_SHORTS_BLUR_DIM", 0.7),
        "fg_size": int(_num("MEDIA_BUDDY_SHORTS_FG_SIZE", 1080)),
        "fg_y": _num("MEDIA_BUDDY_SHORTS_FG_Y", 0.35),
        # Blur the background at 1/downscale resolution then upscale back — a
        # blurred bg doesn't need full res, and gblur cost scales with pixels,
        # so this is dramatically cheaper for a visually identical result.
        "downscale": max(1, int(_num("MEDIA_BUDDY_SHORTS_BLUR_DOWNSCALE", 4))),
    }


def _blur_bg_cut(
    idx: int, in_s: float, trim_dur: float, duration: float,
    freeze_pad: float, width: int, height: int, cfg: dict,
    subject_h_pos: object = None,
) -> str:
    """Per-cut subgraph: blurred zoomed background (cover+gblur+dim) with a
    centred-ish square foreground crop overlaid. Multiple ``;``-joined segments
    ending in ``[v{idx}]`` (compatible with the outer ``";".join``). One-pass —
    folds into the existing compose encode, no per-clip pre-render."""
    fg = int(cfg["fg_size"])
    sigma = cfg["sigma"]
    dim = cfg["dim"]
    fgy = cfg["fg_y"]
    down = max(1, int(cfg.get("downscale", 4)))
    # Background: cover-crop, then (downscale → gblur on the small image →
    # upscale) so the per-frame blur cost drops ~down^2×, then dim.
    if down > 1:
        bw = max(2, (width // down) // 2 * 2)
        bh = max(2, (height // down) // 2 * 2)
        small_sigma = max(0.5, sigma / down)
        bg_blur = (
            f"scale={bw}:{bh}:flags=bilinear,gblur=sigma={small_sigma:g},"
            f"scale={width}:{height}:flags=bilinear"
        )
    else:
        bg_blur = f"gblur=sigma={sigma:g}"
    # Foreground (the visible subject): shift the square crop toward the subject
    # when we know where it is, so an off-center animal isn't sliced to an edge.
    # Background stays centered (it's blurred — offset would be invisible).
    xexpr = subject_crop_x_expr(subject_h_pos)
    fg_crop = f"crop={fg}:{fg}:x={xexpr}" if xexpr else f"crop={fg}:{fg}"
    base = (
        f"[{idx}:v]trim=start={in_s:.3f}:duration={trim_dur:.3f},"
        f"setpts=PTS-STARTPTS,split[bg{idx}][fg{idx}];"
        f"[bg{idx}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={width}:{height},{bg_blur},"
        f"colorchannelmixer=rr={dim:g}:gg={dim:g}:bb={dim:g},setsar=1,fps={TARGET_FPS}[bgo{idx}];"
        f"[fg{idx}]scale={fg}:{fg}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"{fg_crop},setsar=1,fps={TARGET_FPS}[fgo{idx}];"
    )
    overlay = f"[bgo{idx}][fgo{idx}]overlay=(W-w)/2:(H-h)*{fgy:g}"
    if freeze_pad > 0.05:
        overlay += (
            f",tpad=stop_mode=clone:stop_duration={freeze_pad:.3f},"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS"
        )
    return f"{base}{overlay}[v{idx}]"


class VideoCompose:
    """In-house FFmpeg-based video composer.

    Public API matches what ``pipeline_service`` calls:
        result = composer.execute({
            "operation": "render",
            "edit_decisions": {"cuts": [...], "render_runtime": "ffmpeg"},
            "asset_manifest": {"assets": [...]},     # informational
            "output_path": "/path/to/final.mp4",
            "profile": "youtube_shorts",
            "audio_path": "/path/to/audio.wav",      # optional
        })
        result.success / result.error / result.output_path
    """

    name = "video_compose"

    def execute(self, inputs: dict[str, Any]) -> ComposeResult:
        op = inputs.get("operation", "render")
        if op != "render":
            return ComposeResult(success=False, error=f"unsupported operation: {op}")

        ed = inputs.get("edit_decisions") or {}
        cuts = ed.get("cuts") or []
        if not cuts:
            return ComposeResult(success=False, error="no cuts provided")

        runtime = ed.get("render_runtime", "ffmpeg")
        if runtime != "ffmpeg":
            return ComposeResult(
                success=False,
                error=f"unsupported render_runtime: {runtime} (only ffmpeg is supported)",
            )

        profile = _normalize_profile(inputs.get("profile") or DEFAULT_PROFILE)
        if profile not in PROFILE_DIMENSIONS:
            logger.warning(
                "Unknown profile %r — falling back to %s",
                profile, DEFAULT_PROFILE,
            )
            profile = DEFAULT_PROFILE
        out_w, out_h = PROFILE_DIMENSIONS[profile]

        out_path_s = inputs.get("output_path")
        if not out_path_s:
            return ComposeResult(success=False, error="missing output_path")
        out_path = Path(out_path_s)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg = _find_ffmpeg()
        if ffmpeg is None:
            return ComposeResult(success=False, error="ffmpeg not on PATH")

        audio_path_s = inputs.get("audio_path")
        audio_path = Path(audio_path_s) if audio_path_s else None

        # Stage A: video-only pass — trim/scale/pad/concat all cuts into a
        # single visual reel. Subtitles are NOT burned here (pipeline_service
        # handles that post-compose with the bottom-safe-zone style).
        framing_style = str(inputs.get("framing_style") or "fill").strip().lower()
        _parallel = os.environ.get(
            "MEDIA_BUDDY_COMPOSE_PARALLEL_SEGMENTS", "0",
        ).strip().lower() in ("1", "true", "yes", "on")
        try:
            if _parallel and out_w >= out_h and framing_style != "blur":
                logger.info("compose: parallel-segment path (44-core) for %d cuts", len(cuts))
                video_only = self._render_video_track_parallel(
                    ffmpeg, cuts, out_w, out_h, out_path,
                    subtitle_ass=inputs.get("subtitle_ass"),
                )
            else:
                video_only = self._render_video_track(
                    ffmpeg, cuts, out_w, out_h, out_path, framing_style,
                    subtitle_ass=inputs.get("subtitle_ass"),
                )
        except Exception as e:
            logger.exception("video render failed")
            return ComposeResult(success=False, error=f"video render: {e}")

        if not video_only or not video_only.exists():
            return ComposeResult(success=False, error="video render produced no output")

        # Stage B: mux audio (if provided)
        if audio_path and audio_path.exists():
            try:
                final = self._mux_audio(ffmpeg, video_only, audio_path, out_path)
            except Exception as e:
                logger.exception("audio mux failed")
                return ComposeResult(success=False, error=f"mux: {e}")
            if not final or not final.exists():
                return ComposeResult(success=False, error="audio mux produced no output")
            # video_only was an intermediate — clean up
            try:
                if video_only != out_path:
                    video_only.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            # No audio — promote the video-only file to the final path
            if video_only != out_path:
                try:
                    out_path.unlink(missing_ok=True)
                    video_only.rename(out_path)
                except Exception as e:
                    return ComposeResult(success=False, error=f"finalize: {e}")

        return ComposeResult(
            success=True,
            output_path=str(out_path),
            artifacts=[str(out_path)],
            data={
                "profile": profile,
                "resolution": f"{out_w}x{out_h}",
                "cut_count": len(cuts),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _render_video_track_parallel(
        self, ffmpeg: str, cuts: list[dict],
        width: int, height: int, final_out_path: Path,
        subtitle_ass: Optional[str] = None,
    ) -> Optional[Path]:
        """44 核提速版:每个 cut 各自并行归一化编码成同规格分段(1920x1080/同帧率/
        yuv420p),再用 concat 解复用器 stream-copy 秒拼。把'一个大 ffmpeg 串着解码几十个
        clip'拆成'几十个小 ffmpeg 同时跑'。仅 landscape 无模糊路径;门控
        MEDIA_BUDDY_COMPOSE_PARALLEL_SEGMENTS=1。有字幕时最后一遍拼接顺带烧(仍一次)。"""
        from concurrent.futures import ThreadPoolExecutor
        from backend.lib.ffmpeg_locator import video_encoder_args

        fps = (os.environ.get("MEDIA_BUDDY_COMPOSE_FPS") or "30").strip() or "30"
        enc = video_encoder_args(
            ffmpeg,
            crf=(os.environ.get("MEDIA_BUDDY_X264_CRF") or "20").strip() or "20",
            preset=(os.environ.get("MEDIA_BUDDY_X264_PRESET") or "faster").strip() or "faster",
            bitrate="8000k", mpeg4_quality="3",
        )
        seg_dir = final_out_path.parent / f"{final_out_path.stem}_segs"
        seg_dir.mkdir(parents=True, exist_ok=True)
        jobs: list[tuple[str, str, Path]] = []
        for cut in cuts:
            src = cut.get("source")
            if not src or not Path(src).exists():
                continue
            in_s = float(cut.get("in_seconds", 0.0) or 0.0)
            out_s = float(cut.get("out_seconds", 0.0) or 0.0)
            duration = max(0.05, out_s - in_s)
            natural = float(cut.get("natural_duration", 0.0) or 0.0)
            needs_loop = bool(cut.get("loop", False))
            trim_duration, freeze_pad = duration, 0.0
            if needs_loop and natural > 0 and natural < duration:
                available = max(0.05, natural - in_s) if natural > in_s else natural
                trim_duration = min(duration, max(0.05, available))
                freeze_pad = max(0.0, duration - trim_duration)
            sp = _scale_pad_filter(width, height, None)
            if freeze_pad > 0.05:
                vf = (f"trim=start={in_s:.3f}:duration={trim_duration:.3f},setpts=PTS-STARTPTS,{sp},"
                      f"tpad=stop_mode=clone:stop_duration={freeze_pad:.3f},"
                      f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,fps={fps}")
            else:
                vf = f"trim=start={in_s:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS,{sp},fps={fps}"
            seg = seg_dir / f"seg_{len(jobs):04d}.mp4"
            jobs.append((str(src), vf, seg))
        if not jobs:
            raise RuntimeError("no usable cuts (all source files missing?)")

        def _enc(job: tuple[str, str, Path]) -> Optional[Path]:
            src, vf, seg = job
            cmd = [ffmpeg, "-y", "-i", src, "-vf", vf, "-an",
                   "-vsync", "cfr", "-pix_fmt", "yuv420p",
                   "-video_track_timescale", "15360", *enc, str(seg)]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=_compose_timeout(300), encoding="utf-8", errors="replace")
            if r.returncode != 0 or not seg.exists():
                logger.warning("segment encode failed (%s): %s", seg.name, (r.stderr or "")[-300:])
                return None
            return seg
        conc = max(1, int(os.environ.get("MEDIA_BUDDY_COMPOSE_SEGMENT_CONCURRENCY", "8")))
        with ThreadPoolExecutor(max_workers=conc) as ex:
            segs = [s for s in ex.map(_enc, jobs) if s is not None]
        if not segs:
            raise RuntimeError("all segment encodes failed")

        list_f = seg_dir / "concat_list.txt"
        list_f.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        intermediate = final_out_path.with_name(f"{final_out_path.stem}.video_only.mp4")
        if subtitle_ass:
            _ass = str(subtitle_ass).replace("\\", "/")
            if len(_ass) > 1 and _ass[1] == ":":
                _ass = _ass[0] + r"\:" + _ass[2:]
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_f),
                   "-vf", f"subtitles='{_ass}'", *enc, "-pix_fmt", "yuv420p", "-an",
                   "-movflags", "+faststart", str(intermediate)]
        else:
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_f),
                   "-c", "copy", "-movflags", "+faststart", str(intermediate)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_compose_timeout(600), encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"parallel concat exit {r.returncode}: {(r.stderr or '')[-400:]}")
        return intermediate

    def _render_video_track(
        self, ffmpeg: str, cuts: list[dict],
        width: int, height: int, final_out_path: Path,
        framing_style: str = "fill",
        subtitle_ass: Optional[str] = None,
    ) -> Optional[Path]:
        """Build one ffmpeg call: -i each source, filter_complex with
        trim+scale+pad per cut + final concat → ``video_only.mp4``.

        media-buddy-rules.md §11: when a cut's requested duration exceeds
        the source clip's natural duration, the cut is extended with a
        freeze-frame tail so it fills the narration slot without visibly
        replaying the same motion.

        Returns the intermediate path (next stage muxes audio)."""
        inputs_args: list[str] = []
        filter_lines: list[str] = []
        # Short-video blurred-background mode (portrait only). See plan
        is_portrait = height > width
        blur_cfg = _blur_bg_config()
        # Per-project choice (framing_style=="blur") decides, unless the global
        # env override forces on/off. Default (no env, framing_style="fill") →
        # legacy cover-crop fill.
        force = blur_cfg["force"]
        want_blur = force if force is not None else (framing_style == "blur")
        blur_on = is_portrait and bool(want_blur)
        _dims_cache: dict[str, tuple[int, int]] = {}
        for i, cut in enumerate(cuts):
            src = cut.get("source")
            if not src or not Path(src).exists():
                logger.warning("cut %d missing source: %r", i, src)
                continue
            # Use a compact ffmpeg input index for usable cuts. The original
            # cut index may have gaps when missing files are skipped; ffmpeg
            # input labels cannot have those gaps.
            ffmpeg_i = len(filter_lines)
            in_s = float(cut.get("in_seconds", 0.0) or 0.0)
            out_s = float(cut.get("out_seconds", 0.0) or 0.0)
            duration = max(0.05, out_s - in_s)
            needs_loop = bool(cut.get("loop", False))
            natural = float(cut.get("natural_duration", 0.0) or 0.0)
            trim_duration = duration
            freeze_pad = 0.0
            if needs_loop and natural > 0 and natural < duration:
                inputs_args.extend(["-i", str(src)])
                available = max(0.05, natural - in_s) if natural > in_s else natural
                trim_duration = min(duration, max(0.05, available))
                freeze_pad = max(0.0, duration - trim_duration)
                logger.info(
                    "cut %d: freezing tail of %.2fs clip to fill %.2fs slot",
                    i, natural, duration,
                )
            else:
                inputs_args.extend(["-i", str(src)])
            # Decide framing for this cut: blurred-bg square foreground, or the
            # legacy cover-crop fill. Native ~9:16 sources fill directly (a
            # square crop would needlessly clip their top/bottom).
            use_blur = blur_on
            if use_blur and blur_cfg["skip_native"]:
                dims = _dims_cache.get(src)
                if dims is None:
                    dims = _probe_dims(Path(src))
                    _dims_cache[src] = dims
                sw, sh = dims
                if sw > 0 and sh > 0 and (sh / sw) >= 1.5:
                    use_blur = False
            sub_pos = cut.get("subject_h_pos")
            if use_blur:
                line = _blur_bg_cut(
                    ffmpeg_i, in_s, trim_duration, duration, freeze_pad,
                    width, height, blur_cfg, subject_h_pos=sub_pos,
                )
            else:
                _xexpr = subject_crop_x_expr(sub_pos) if is_portrait else None
                line = (
                    f"[{ffmpeg_i}:v]trim=start={in_s:.3f}:duration={duration:.3f},"
                    f"setpts=PTS-STARTPTS,{_scale_pad_filter(width, height, _xexpr)}[v{ffmpeg_i}]"
                )
                if freeze_pad > 0.05:
                    line = (
                        f"[{ffmpeg_i}:v]trim=start={in_s:.3f}:duration={trim_duration:.3f},"
                        f"setpts=PTS-STARTPTS,{_scale_pad_filter(width, height, _xexpr)},"
                        f"tpad=stop_mode=clone:stop_duration={freeze_pad:.3f},"
                        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v{ffmpeg_i}]"
                    )
            filter_lines.append(line)

        if not filter_lines:
            raise RuntimeError("no usable cuts (all source files missing?)")

        n = len(filter_lines)
        labels = "".join(f"[v{i}]" for i in range(n))
        filter_lines.append(f"{labels}concat=n={n}:v=1:a=0[outv]")
        # 一遍烧字幕:字幕滤镜接在拼接后面,和拼接编码同一遍完成,省掉第二遍整条重编码。
        _map_label = "[outv]"
        if subtitle_ass:
            _ass = str(subtitle_ass).replace("\\", "/")
            if len(_ass) > 1 and _ass[1] == ":":     # Windows 盘符冒号转义
                _ass = _ass[0] + r"\:" + _ass[2:]
            filter_lines.append(f"[outv]subtitles='{_ass}'[outsub]")
            _map_label = "[outsub]"
        filter_complex = ";".join(filter_lines)

        intermediate = final_out_path.with_name(
            f"{final_out_path.stem}.video_only.mp4"
        )
        # 长片(几十个 clip)的 filter_complex 内联会撑爆命令行(Windows ~32KB 上限 →
        # WinError 206;Linux 也有 ARG_MAX)。写到脚本文件用 -filter_complex_script,
        # 语义完全等价、两平台通用。
        fc_script = final_out_path.with_name(f"{final_out_path.stem}.filtergraph.txt")
        fc_script.write_text(filter_complex, encoding="utf-8")
        cmd = [
            ffmpeg, "-y",
            *inputs_args,
            "-filter_complex_script", str(fc_script),
            "-map", _map_label,
            *video_encoder_args(
                ffmpeg, crf="16",
                preset=os.environ.get("MEDIA_BUDDY_COMPOSE_PRESET", "medium"),
                bitrate=os.environ.get("MEDIA_BUDDY_COMPOSE_BITRATE", "16000k"),
                mpeg4_quality="2",
            ),
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            str(intermediate),
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_compose_timeout(900),
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            tail = (r.stderr or "")[-800:]
            raise RuntimeError(f"ffmpeg video pass exit {r.returncode}: {tail}")
        return intermediate

    def _mux_audio(
        self, ffmpeg: str, video: Path, audio: Path, out_path: Path,
    ) -> Optional[Path]:
        """Mux a video-only mp4 with the supplied audio file.

        media-buddy-rules.md §11 (Completeness Over Duration): output
        duration MUST be ≥ audio duration. Previously used ``-shortest``
        which stopped at whichever stream ended first — if video was
        shorter than narration, the last N seconds of narration were
        cut. Now we measure audio duration and pad video with a freeze
        frame of its last frame so the final stream covers all audio.

        Audio is re-encoded to AAC (broad compatibility). The video
        stream is RE-ENCODED here (not copy) because tpad needs to
        process frames; this is one extra encode pass but it's how we
        guarantee completeness.
        """
        audio_dur = _probe_duration(audio)
        video_dur = _probe_duration(video)
        if audio_dur <= 0 or video_dur <= 0:
            # ffprobe missing or failed — fall back to legacy -shortest
            # behavior so we don't break compose entirely. Logged so QA
            # notices.
            logger.warning(
                "mux: could not probe durations (audio=%.2f video=%.2f) — "
                "falling back to -shortest (rule 11 may be violated)",
                audio_dur, video_dur,
            )
            cmd = [
                ffmpeg, "-y",
                "-i", str(video), "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart",
                str(out_path),
            ]
        elif video_dur >= audio_dur - 0.05:
            # Video already covers narration. Match audio length exactly
            # to avoid trailing silent black frame on the user's end.
            cmd = [
                ffmpeg, "-y",
                "-i", str(video), "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{audio_dur:.3f}",
                "-movflags", "+faststart",
                str(out_path),
            ]
        else:
            # Video shorter than audio — pad video by freezing its last
            # frame until audio ends. tpad stop_mode=clone clones the
            # final frame; stop_duration is the number of seconds to add.
            # encoder helper to pick whatever H.264 / fallback this build
            # actually supports.
            pad_sec = audio_dur - video_dur
            logger.info(
                "mux: video (%.2fs) < audio (%.2fs); padding with freeze "
                "frame for %.2fs to satisfy rule 11",
                video_dur, audio_dur, pad_sec,
            )
            encoder_args = video_encoder_args(
                ffmpeg, crf="16",
                preset=os.environ.get("MEDIA_BUDDY_COMPOSE_PRESET", "medium"),
                bitrate=os.environ.get("MEDIA_BUDDY_COMPOSE_BITRATE", "16000k"),
            )
            cmd = [
                ffmpeg, "-y",
                "-i", str(video), "-i", str(audio),
                "-filter_complex",
                f"[0:v]tpad=stop_mode=clone:stop_duration={pad_sec:.3f}[v]",
                "-map", "[v]", "-map", "1:a:0",
                *encoder_args, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{audio_dur:.3f}",
                "-movflags", "+faststart",
                str(out_path),
            ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_compose_timeout(600),
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            tail = (r.stderr or "")[-800:]
            raise RuntimeError(f"ffmpeg mux exit {r.returncode}: {tail}")
        return out_path
