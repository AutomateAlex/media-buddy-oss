"""Tests for ImageMotionService — Ken Burns / zoompan image-to-video.

Pure unit tests: subprocess.run is mocked, so these run fast and don't
require ffmpeg on PATH. A separate slow-test (gated on ffmpeg present)
could verify the actual filter graph produces playable mp4, but that's
not required for CI.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backend.services.image_motion_service import (
    DEFAULT_MOTION,
    FPS,
    ImageMotionService,
    _MOTION_STYLES,
    _TARGET_DIMS,
)


@pytest.fixture
def fake_image(tmp_path) -> Path:
    """A non-empty file at .jpg path — content irrelevant since
    subprocess is mocked."""
    p = tmp_path / "in.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fakejpg")
    return p


def _write_then_succeed(cmd, **kwargs):
    """Helper side_effect that writes a fake mp4 then returns success.

    Required because Path.write_bytes returns the byte count (truthy int),
    so a one-line ``write or MagicMock()`` short-circuits and the
    MagicMock never gets returned to subprocess.run's caller — which
    breaks the `r.returncode` check.
    """
    Path(cmd[-1]).write_bytes(b"\x00" * 4096)
    return MagicMock(returncode=0, stderr="", stdout="")


class TestImageMotionService:
    def test_init_without_ffmpeg_logs_warning(self, caplog):
        """No ffmpeg on PATH → init succeeds but apply() will fail later."""
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value=None):
            ImageMotionService()
        # Warning emitted but no exception

    def test_apply_returns_output_path_on_success(self, fake_image, tmp_path):
        out = tmp_path / "out.mp4"

        def _fake_run(cmd, **kwargs):
            # Simulate ffmpeg writing a valid mp4
            Path(cmd[-1]).write_bytes(b"\x00" * 4096)  # >1KB
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=["-c:v", "libx264"]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=_fake_run) as mock_run:
            svc = ImageMotionService()
            result = svc.apply(fake_image, out, duration=5.0, motion="ken_burns",
                               orientation="portrait")
        assert result == out
        assert out.exists()
        # ffmpeg was actually called
        assert mock_run.called

    def test_apply_returns_none_on_ffmpeg_failure(self, fake_image, tmp_path):
        out = tmp_path / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=["-c:v", "libx264"]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="bad input")):
            svc = ImageMotionService()
            result = svc.apply(fake_image, out)
        assert result is None

    def test_apply_returns_none_when_source_missing(self, tmp_path):
        out = tmp_path / "out.mp4"
        missing = tmp_path / "does_not_exist.jpg"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"):
            svc = ImageMotionService()
            result = svc.apply(missing, out)
        assert result is None

    def test_apply_returns_none_when_ffmpeg_missing(self, fake_image, tmp_path):
        out = tmp_path / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value=None):
            svc = ImageMotionService()
            result = svc.apply(fake_image, out)
        assert result is None

    def test_clamps_duration_to_valid_range(self, fake_image, tmp_path):
        """Duration < 2 or > 12 should be clamped to the range."""
        out = tmp_path / "out.mp4"

        def _capture_cmd(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"\x00" * 4096)
            # Check duration arg
            t_idx = cmd.index("-t")
            assert 2.0 <= float(cmd[t_idx + 1]) <= 12.0
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=_capture_cmd):
            svc = ImageMotionService()
            # Too short — should bump to 2.0
            assert svc.apply(fake_image, out, duration=0.5) == out
            # Too long — should cap at 12.0
            assert svc.apply(fake_image, out, duration=30.0) == out

    def test_unknown_motion_falls_back_to_default(self, fake_image, tmp_path):
        out = tmp_path / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=_write_then_succeed):
            svc = ImageMotionService()
            # Bogus motion name — apply should still succeed (uses default)
            result = svc.apply(fake_image, out, motion="bogus_motion_xyz")
        assert result == out

    def test_orientation_picks_correct_canvas(self, fake_image, tmp_path):
        """portrait → 1080×1920, landscape → 1920×1080."""
        out = tmp_path / "out.mp4"
        captured = {}

        def _capture_cmd(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"\x00" * 4096)
            # Find the vf string
            vf_idx = cmd.index("-vf")
            captured["vf"] = cmd[vf_idx + 1]
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=_capture_cmd):
            svc = ImageMotionService()
            svc.apply(fake_image, out, orientation="landscape")
            assert "1920x1080" in captured["vf"]
            svc.apply(fake_image, out, orientation="portrait")
            assert "1080x1920" in captured["vf"]

    def test_subprocess_timeout_returns_none(self, fake_image, tmp_path):
        import subprocess as real_subprocess
        out = tmp_path / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=real_subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)):
            svc = ImageMotionService()
            assert svc.apply(fake_image, out) is None

    def test_list_motions_returns_known_styles(self):
        motions = ImageMotionService.list_motions()
        assert "ken_burns" in motions
        assert "push_in" in motions
        assert "pan_left" in motions
        assert "pan_right" in motions
        assert "slow_zoom" in motions
        assert len(motions) == len(_MOTION_STYLES)

    def test_default_motion_is_ken_burns(self):
        assert DEFAULT_MOTION == "ken_burns"

    def test_fps_constant(self):
        assert FPS == 30
        assert _TARGET_DIMS["portrait"] == (1080, 1920)
        assert _TARGET_DIMS["landscape"] == (1920, 1080)

    def test_apply_writes_to_subdirectory(self, fake_image, tmp_path):
        """If output_path parent doesn't exist, create it."""
        nested_out = tmp_path / "deep" / "nested" / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   side_effect=_write_then_succeed):
            svc = ImageMotionService()
            result = svc.apply(fake_image, nested_out)
        assert result == nested_out
        assert nested_out.exists()

    def test_empty_output_file_returns_none(self, fake_image, tmp_path):
        """If ffmpeg returns 0 but output is empty/missing, treat as failure."""
        out = tmp_path / "out.mp4"
        with patch("backend.services.image_motion_service.shutil.which",
                   return_value="/usr/bin/ffmpeg"), \
             patch("backend.services.image_motion_service.video_encoder_args",
                   return_value=[]), \
             patch("backend.services.image_motion_service.subprocess.run",
                   return_value=MagicMock(returncode=0, stderr="", stdout="")):
            # subprocess.run returns success but didn't actually write the file
            svc = ImageMotionService()
            assert svc.apply(fake_image, out) is None
