"""Find FFmpeg even when not on PATH and inject its bin dir at startup."""
import os
import shutil
import subprocess
from pathlib import Path

# Common Windows install locations to scan when shutil.which fails.
# First match wins.
WINDOWS_CANDIDATES = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
    r"C:\tools\ffmpeg\bin",
]

POSIX_CANDIDATES = [
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",
    "/opt/ffmpeg/bin",
]


def _bundled_ffmpeg_dir() -> Path | None:
    """Return the bin dir of the FFmpeg shipped alongside this app.

    Two layouts are supported:
    - Packaged Electron build — ``server-manager.ts`` exports
      ``MEDIABUDDY_RESOURCES_DIR=<resources>``; we look in
      ``<resources>/ffmpeg/``.
    - Dev / source checkout — walk up from this file to repo root and
      check ``vendor/ffmpeg/<platform>/``.
    """
    plat = "win64" if os.name == "nt" else ("macos" if os.uname().sysname == "Darwin" else "linux64")  # type: ignore[attr-defined]
    pkg_root = os.environ.get("MEDIABUDDY_RESOURCES_DIR")
    if pkg_root:
        d = Path(pkg_root) / "ffmpeg"
        if d.is_dir():
            return d
    # ffmpeg_locator.py lives at src/backend/lib/, so parents[3] = repo root
    repo_root = Path(__file__).resolve().parents[3]
    d = repo_root / "vendor" / "ffmpeg" / plat
    if d.is_dir():
        return d
    return None


def ensure_ffmpeg_on_path() -> str | None:
    """
    If ffmpeg is on PATH, return its full path.
    Otherwise scan well-known locations; if found, prepend that bin dir to
    os.environ["PATH"] so subsequent subprocess calls can locate ffmpeg.
    Returns the full path to the binary, or None if not found anywhere.
    """
    bin_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    # Phase 2.11l — bundled FFmpeg first (so installed clients work
    # offline without ever needing a system FFmpeg). Falls through to
    # PATH/well-known dirs only if the bundle is missing (dev machines
    # before scripts/fetch-ffmpeg.ps1 ran).
    bundled_dir = _bundled_ffmpeg_dir()
    if bundled_dir:
        candidate = bundled_dir / bin_name
        if candidate.is_file():
            os.environ["PATH"] = str(bundled_dir) + os.pathsep + os.environ.get("PATH", "")
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    # Allow user override via env var
    explicit = os.environ.get("FFMPEG_PATH")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            os.environ["PATH"] = str(p.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(p)

    candidates = WINDOWS_CANDIDATES if os.name == "nt" else POSIX_CANDIDATES
    for bin_dir in candidates:
        candidate = Path(bin_dir) / bin_name
        if candidate.is_file():
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return str(candidate)

    return None


_ENCODER_CACHE: dict[str, str] = {}


def _encoders_output(ffmpeg: str) -> str:
    if ffmpeg in _ENCODER_CACHE:
        return _ENCODER_CACHE[ffmpeg]
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        _ENCODER_CACHE[ffmpeg] = ""
        return ""
    output = result.stdout or ""
    _ENCODER_CACHE[ffmpeg] = output
    return output


def _encoder_names(ffmpeg: str) -> set[str]:
    """Parse `ffmpeg -encoders` into exact encoder names.

    Do not use substring checks here: some bundled builds print
    configuration text such as `--disable-libx264`, which mentions an
    encoder that is not actually available.
    """
    names: set[str] = set()
    for line in _encoders_output(ffmpeg).splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        if len(flags) == 6 and flags[0] in {"V", "A", "S"}:
            names.add(name)
    return names


def video_encoder_args(
    ffmpeg: str,
    *,
    crf: str = "20",
    preset: str = "veryfast",
    bitrate: str = "6000k",
    mpeg4_quality: str = "4",
) -> list[str]:
    """Return encoder args supported by the active FFmpeg build.

    The bundled Windows FFmpeg intentionally does not always ship libx264.
    Prefer H.264 when possible, then fall back to broadly available MPEG-4
    Part 2 so packaged builds can still render instead of failing at runtime.

    compose settings (preset=medium, crf=16, near-lossless) can't encode a
    multi-minute 1080p video within the compose timeout → 'timed out after 900s'.
    faster preset / higher CRF. Unset (desktop) = the caller's values, so
    desktop output quality is unchanged.
    """
    preset = (os.environ.get("MEDIA_BUDDY_X264_PRESET") or "").strip() or preset
    crf = (os.environ.get("MEDIA_BUDDY_X264_CRF") or "").strip() or crf
    encoders = _encoder_names(ffmpeg)
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", preset, "-crf", crf]
    if os.name == "nt" and "h264_mf" in encoders:
        return ["-c:v", "h264_mf", "-b:v", bitrate]
    return ["-c:v", "mpeg4", "-q:v", mpeg4_quality]
