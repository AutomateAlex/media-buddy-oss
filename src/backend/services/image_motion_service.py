"""Image Motion Service — convert a still image into a short video clip
with motion (Ken Burns / push-in / pan / zoom) so it can stand in for a
video asset in the timeline.

per user spec sections 11-12 — "if L5 video unavailable → L5 image →
image motion → primary clip". Images alone are static and look like
placeholders; the motion treatment gives the clip life and matches the
visual feel of a real B-roll shot.

Output: 30 fps mp4, target resolution matching orientation
(1920×1080 landscape / 1080×1920 portrait), duration 4-8 s.

Motion styles:
  - "ken_burns"  — combined slow zoom + slight pan (most cinematic)
  - "push_in"    — center-anchored zoom from 1.0× → 1.15×
  - "pull_out"   — center-anchored zoom from 1.15× → 1.0×
  - "pan_left"   — horizontal pan with 1.05× constant zoom
  - "pan_right"  — opposite of pan_left
  - "slow_zoom"  — alias for push_in (legacy name)

The output mp4 uses video_encoder_args from ffmpeg_locator so packaged
ffmpeg builds without libx264 still produce a working clip (same lesson
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from backend.lib.ffmpeg_locator import video_encoder_args

logger = logging.getLogger(__name__)

# Target dimensions per orientation. Matches reframe_service _TARGET_DIMS.
_TARGET_DIMS = {
    "landscape": (1920, 1080),
    "portrait":  (1080, 1920),
    "square":    (1080, 1080),
}

# Motion styles → (start zoom, end zoom, pan x offset fraction, pan y offset fraction).
# Pan offsets are fractions of the post-scale image margin (0 = no pan).
_MOTION_STYLES: dict[str, tuple[float, float, float, float]] = {
    "ken_burns":  (1.00, 1.18,  0.30, -0.30),  # zoom + diagonal drift
    "push_in":    (1.00, 1.15,  0.0,   0.0),
    "pull_out":   (1.15, 1.00,  0.0,   0.0),
    "pan_left":   (1.05, 1.05,  0.50,  0.0),
    "pan_right":  (1.05, 1.05, -0.50,  0.0),
    "slow_zoom":  (1.00, 1.10,  0.0,   0.0),  # alias / gentle push_in
}

# Default motion choice when caller doesn't specify
DEFAULT_MOTION = "ken_burns"

# Output fps — 30 is the orchestrator's standard B-roll fps
FPS = 30


class ImageMotionService:
    """Convert an image to a motion mp4 ready for the timeline."""

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            logger.warning(
                "ImageMotionService: ffmpeg not on PATH — apply() will fail"
            )

    def apply(
        self,
        image_path: Path,
        output_path: Path,
        duration: float = 5.0,
        motion: str = DEFAULT_MOTION,
        orientation: str = "portrait",
    ) -> Optional[Path]:
        """Render `image_path` as a `duration`-second mp4 with the requested
        motion. Returns the output path on success, ``None`` on failure
        (no exception; caller decides whether to retry / abort).

        Args:
          image_path: input image (jpg/png/webp — anything ffmpeg can decode)
          output_path: target mp4 path. Parent dir is created if needed.
          duration: seconds of output. Clamped to [2.0, 12.0].
          motion: one of `_MOTION_STYLES` keys; falls back to DEFAULT_MOTION
            on unknown value.
          orientation: "landscape" | "portrait" | "square"
        """
        image_path = Path(image_path)
        output_path = Path(output_path)
        if not image_path.exists():
            logger.warning("ImageMotionService: source image %s missing",
                           image_path)
            return None
        if self._ffmpeg is None:
            return None

        duration = max(2.0, min(12.0, float(duration)))
        motion_key = motion if motion in _MOTION_STYLES else DEFAULT_MOTION
        target_w, target_h = _TARGET_DIMS.get(
            orientation, _TARGET_DIMS["portrait"],
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # zoompan filter: simulate the motion. zoompan has a fixed
        # 25 fps default; we set d (duration in frames) accordingly.
        # We pre-scale the image larger than canvas so zoompan has headroom
        # to pan + zoom without exposing edges. Up-scaling factor 4× chosen
        # to support the strongest motion preset (zoom 1.18×) with margin.
        # The 'scale' before zoompan also smooths pixelation (Lanczos).
        z_start, z_end, pan_x_frac, pan_y_frac = _MOTION_STYLES[motion_key]
        total_frames = int(round(duration * FPS))
        # zoom: ease-in-out via 1.0 + (z_end-z_start) * sin(t)... simpler is
        # linear interpolation since zoompan re-evaluates per frame.
        # We use `zoom='zoom + (target_zoom-1.0)/N'` style accumulator.
        # Final zoom equation (linear): z(n) = z_start + (z_end - z_start) * n/N
        # where n = on (current frame in segment), N = total_frames
        # zoompan exposes `on` (output frame number) — we use that.
        # x_off / y_off pan along (W - canvas_w) / 2 + drift
        # Pre-scale source larger than canvas so motion has room
        prescale = 4
        pre_w = target_w * prescale
        pre_h = target_h * prescale

        zoom_expr = (
            f"{z_start}+({z_end}-{z_start})*on/{total_frames}"
        )
        # Pan: center + drift over time. drift fraction is a fraction of
        # pre_w * (1 - 1/zoom) (i.e., the margin between pre-scaled image
        # and the visible window at current zoom). For Ken Burns we want
        # the drift to happen smoothly across the clip.
        # Simplified: linear interpolation from center to center+offset.
        center_x = f"(iw-iw/zoom)/2"
        center_y = f"(ih-ih/zoom)/2"
        x_drift = f"({pan_x_frac})*(iw/zoom)*on/{total_frames}"
        y_drift = f"({pan_y_frac})*(ih/zoom)*on/{total_frames}"
        x_expr = f"{center_x}+{x_drift}"
        y_expr = f"{center_y}+{y_drift}"

        vf = (
            # 1. Loop the still image at FPS so we have something to filter
            #    on. -loop 1 + -t handle this at the input side.
            # 2. Pre-scale up using lanczos — COVER the pre-canvas (preserve the
            #    source aspect, then crop) so a landscape source in a portrait
            #    frame is filled + panned, never STRETCHED/squished. Plain
            #    scale=W:H ignored aspect and distorted off-aspect sources
            #    (e.g. a 1024x768 FLUX still forced into 9:16).
            f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={pre_w}:{pre_h},"
            # 3. zoompan applies time-varying zoom+pan, output target size
            f"zoompan="
            f"z='{zoom_expr}':"
            f"x='{x_expr}':y='{y_expr}':"
            f"d={total_frames}:s={target_w}x{target_h}:fps={FPS},"
            # 4. setsar=1 — avoids weird aspect ratio metadata
            f"setsar=1"
        )

        encoder_args = video_encoder_args(
            self._ffmpeg, crf="20", preset="veryfast", bitrate="6000k",
        )
        cmd = [
            self._ffmpeg, "-y",
            "-loop", "1",
            "-i", str(image_path),
            "-t", f"{duration:.2f}",
            "-vf", vf,
            "-r", str(FPS),
            "-pix_fmt", "yuv420p",
            *encoder_args,
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ImageMotionService: ffmpeg timed out for %s", image_path,
            )
            return None
        except Exception as e:
            logger.warning(
                "ImageMotionService: ffmpeg launch failed: %s", e,
            )
            return None

        if r.returncode != 0:
            logger.warning(
                "ImageMotionService: ffmpeg failed (rc=%d) for %s\nstderr tail: %s",
                r.returncode, image_path,
                (r.stderr or "")[-400:],
            )
            return None

        if not output_path.exists() or output_path.stat().st_size < 1024:
            logger.warning(
                "ImageMotionService: output %s is missing or too small",
                output_path,
            )
            return None

        logger.info(
            "ImageMotionService: %s → %s (%s, %.1fs)",
            image_path.name, output_path.name, motion_key, duration,
        )
        return output_path

    @staticmethod
    def list_motions() -> list[str]:
        """Available motion preset names — handy for selection UIs."""
        return list(_MOTION_STYLES.keys())

    def probe_image_dims(self, image_path: Path) -> tuple[int, int]:
        """Return (width, height) of the source image, or (0, 0) on failure.
        Useful to decide whether to skip motion (e.g. image too small)."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return (0, 0)
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "json", str(image_path)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return (0, 0)
            data = json.loads(r.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            return (
                int(stream.get("width", 0) or 0),
                int(stream.get("height", 0) or 0),
            )
        except Exception:
            return (0, 0)
