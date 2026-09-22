"""Phase 2.11 — Smart aspect reframer (letterbox MVP).

Takes any video and outputs a target-aspect version. MVP strategy is
pure letterbox: scale the source to fit inside the target frame at its
original aspect, then pad the rest with black bars. **No content is
ever cropped.** This matches the "9:16 with black bars on top/bottom"
look common on news/repost YouTube Shorts channels.

Trigger points (called from orchestrator + pipeline):
  - After a stock candidate is downloaded and the output_format is
    portrait/square → reframe to that aspect
  - After an AI-generated clip is produced (aspect_ratio param isn't always honored
    cleanly across providers) → defensive reframe

Future upgrade (Phase 2.11b — not in this MVP):
  - "smart" strategy: detect faces/subjects via MediaPipe, compute pan track,
    crop+pan to keep subject centered without black bars
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid

from backend.lib.ffmpeg_locator import video_encoder_args
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Target dimensions per aspect. Standard short-form: 1080x1920 / 1920x1080 / 1080x1080.
_TARGET_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1":  (1080, 1080),
    "4:5":  (1080, 1350),  # IG feed extended
}


def _aspect_label(width: int, height: int) -> str:
    if not width or not height:
        return ""
    ratio = width / height
    candidates = [
        ("16:9", 16 / 9),
        ("9:16", 9 / 16),
        ("1:1", 1.0),
        ("4:3", 4 / 3),
        ("3:4", 3 / 4),
        ("4:5", 4 / 5),
        ("21:9", 21 / 9),
    ]
    label, _ = min(candidates, key=lambda x: abs(ratio - x[1]))
    return label


def _orientation_to_aspect(orientation: str) -> Optional[str]:
    return {
        "portrait":  "9:16",
        "landscape": "16:9",
        "square":    "1:1",
    }.get(orientation)


class ReframeService:
    """Stateless service. One method: `to_aspect()`."""

    SAME_RATIO_TOLERANCE = 0.05

    # Strategies for filling the gaps when source aspect ≠ target.
    # blur_pad (default) — TikTok/抖音 standard: blurred copy of source as
    #   background, original centered inside. Looks polished, no dead bars.
    # letterbox — black bars top/bottom. Preserves content but looks raw;
    #   matches news-clip repost style.
    SUPPORTED_STRATEGIES = ("blur_pad", "letterbox")

    def to_aspect(
        self,
        src_path: Path | str,
        target_aspect: str = "9:16",
        out_dir: Optional[Path] = None,
        strategy: str = "blur_pad",
        subject_h_pos: object = None,
    ) -> Optional[Path]:
        """Reframe `src_path` to `target_aspect`. Returns the output Path,
        or `None` on failure. If source already matches target, returns
        the original path unchanged (no copy).

        Strategies:
          - "blur_pad" (default): blurred fill background + centered original
          - "letterbox": flat black bars
        """
        src = Path(src_path)
        if not src.exists():
            logger.warning("ReframeService: source missing %s", src)
            return None
        if target_aspect not in _TARGET_DIMS:
            logger.warning("ReframeService: unsupported target_aspect %r", target_aspect)
            return None
        if strategy not in self.SUPPORTED_STRATEGIES:
            logger.warning("ReframeService: unknown strategy %r — defaulting to blur_pad",
                           strategy)
            strategy = "blur_pad"

        # 1. Probe source dimensions
        info = self._probe(src)
        sw, sh = info.get("width", 0), info.get("height", 0)
        if not sw or not sh:
            logger.warning("ReframeService: ffprobe gave no width/height for %s", src)
            return None
        src_aspect = _aspect_label(sw, sh)

        # 2. Already correct? (within tolerance) → no-op
        target_w, target_h = _TARGET_DIMS[target_aspect]
        if self._is_same_ratio(sw, sh, target_w, target_h):
            logger.debug("ReframeService: %s already %s, skipping reframe", src.name, target_aspect)
            return src

        # 3. Build output path
        if out_dir is None:
            out_dir = src.parent / "reframed"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{src.stem}_{target_aspect.replace(':', 'x')}_{strategy}_{uuid.uuid4().hex[:6]}.mp4"
        dst = out_dir / out_name

        # 4. Build ffmpeg filter graph for the chosen strategy
        ffmpeg = self._find_bin("ffmpeg")
        if not ffmpeg:
            logger.warning("ReframeService: ffmpeg not found on PATH")
            return None

        # encoder 'libx264'" → reframe silently fails → orchestrator
        # returns the original landscape clip → VideoCompose pads it
        # with black bars instead of blur. Use video_encoder_args helper
        # to pick whichever H.264 / fallback encoder this ffmpeg supports.
        encoder_args = video_encoder_args(
            ffmpeg, crf="20", preset="veryfast", bitrate="6000k",
        )

        if strategy == "letterbox":
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1"
            )
            cmd = [
                ffmpeg, "-y", "-i", str(src),
                "-vf", vf,
                *encoder_args,
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(dst),
            ]
        else:
            #   bg: source scale-to-fill canvas → crop → gblur σ=25 + darken
            #   fg: source scale so SHORTER side = target_w, then center-crop
            #       to a square (target_w × target_w) — subject keeps centre
            #       framing while filling the horizontal axis.
            subject_size = target_w
            subject_y = int(round(target_h * 0.15))
            # Center square crop, or shifted toward the subject when known.
            from backend.lib.video_compose import subject_crop_x_expr
            _xexpr = subject_crop_x_expr(subject_h_pos)
            _fg_crop = (
                f"crop={subject_size}:{subject_size}:x={_xexpr}"
                if _xexpr else f"crop={subject_size}:{subject_size}"
            )
            filter_complex = (
                f"[0:v]split[bg_in][fg_in];"
                f"[bg_in]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                # The bundled Windows FFmpeg currently has gblur and
                # colorchannelmixer, but not eq. Keep this graph within that
                # smaller packaged-filter surface so reframe cannot silently
                # fall back to landscape source clips with black bars.
                f"crop={target_w}:{target_h},gblur=sigma=25,"
                f"colorchannelmixer=rr=0.72:gg=0.72:bb=0.72[bg];"
                # Source might be landscape (a>1) or portrait (a<=1). Scale
                # the shorter side to subject_size, then crop square — centered,
                # or shifted toward the subject when we know its position.
                f"[fg_in]scale='if(gt(a,1),-2,{subject_size})':'if(gt(a,1),{subject_size},-2)',"
                f"{_fg_crop}[fg];"
                f"[bg][fg]overlay=(W-w)/2:{subject_y},setsar=1[out]"
            )
            cmd = [
                ffmpeg, "-y", "-i", str(src),
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", "0:a?",  # carry audio if present (optional)
                *encoder_args,
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(dst),
            ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            logger.warning("ReframeService: ffmpeg timed out on %s", src.name)
            return None
        except Exception as e:
            logger.warning("ReframeService: ffmpeg crash %s: %s", src.name, e)
            return None
        if r.returncode != 0 or not dst.exists():
            logger.warning(
                "ReframeService: ffmpeg failed (%s) for %s — stderr tail: %s",
                r.returncode, src.name, (r.stderr or "")[-400:],
            )
            return None
        logger.info(
            "ReframeService: %s %s → %s (%s)",
            src.name, src_aspect, target_aspect, strategy,
        )
        return dst

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_same_ratio(sw: int, sh: int, tw: int, th: int) -> bool:
        if not sw or not sh or not tw or not th:
            return False
        sr = sw / sh
        tr = tw / th
        return abs(sr - tr) / tr < ReframeService.SAME_RATIO_TOLERANCE

    @staticmethod
    def _find_bin(name: str) -> Optional[str]:
        return shutil.which(name)

    @staticmethod
    def _probe(path: Path) -> dict:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return {}
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return {}
            data = json.loads(r.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            return {
                "width":    int(stream.get("width", 0) or 0),
                "height":   int(stream.get("height", 0) or 0),
                "duration": float(stream.get("duration", 0) or 0),
            }
        except Exception:
            return {}
