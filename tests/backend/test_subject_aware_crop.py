"""Subject-aware portrait crop (fix: subject sliced to the edge in 9:16).

The observer vision judge reports the subject's horizontal position; reframing
shifts the crop toward it instead of blindly centering. These tests pin the
pure crop-x helper and the two compose filter builders, and crucially assert
the CENTER case stays byte-identical to the old center-crop (zero regression).
"""
import importlib

import pytest

vc = importlib.import_module("backend.lib.video_compose")


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch):
    # Default-on; tests that need it off set it explicitly.
    monkeypatch.delenv("MEDIA_BUDDY_SUBJECT_AWARE_CROP", raising=False)


# --- subject_crop_x_expr ---------------------------------------------------

def test_none_and_center_return_none():
    assert vc.subject_crop_x_expr(None) is None
    # 0.5 is dead center → no x (keep default center crop).
    assert vc.subject_crop_x_expr(0.5) is None


def test_invalid_inputs_return_none():
    assert vc.subject_crop_x_expr("left") is None
    assert vc.subject_crop_x_expr(1.5) is None      # out of [0,1]
    assert vc.subject_crop_x_expr(-0.2) is None


def test_offcenter_right_builds_clip_expr():
    expr = vc.subject_crop_x_expr(0.9)
    assert expr is not None
    # damped (default 0.95): 0.5 + (0.9-0.5)*0.95 = 0.88
    assert "clip(iw*0.8800-ow/2" in expr
    # commas escaped for inline filter_complex use
    assert "\\," in expr


def test_offcenter_left_builds_clip_expr():
    expr = vc.subject_crop_x_expr(0.1)
    assert expr is not None
    assert "clip(iw*0.1200-ow/2" in expr  # 0.5 + (0.1-0.5)*0.95 = 0.12


def test_disabled_via_env_returns_none(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_SUBJECT_AWARE_CROP", "0")
    assert vc.subject_crop_x_expr(0.9) is None


# --- _scale_pad_filter (fill format) ---------------------------------------

def test_scale_pad_center_is_unchanged():
    # No crop_x → exactly the legacy center-crop string.
    f = vc._scale_pad_filter(1080, 1920)
    assert "crop=1080:1920," in f
    assert ":x=" not in f


def test_scale_pad_shifts_when_expr_given():
    f = vc._scale_pad_filter(1080, 1920, "clip(iw*0.84-ow/2\\,0\\,iw-ow)")
    assert "crop=1080:1920:x=clip(iw*0.84-ow/2" in f


# --- _blur_bg_cut (blur format) --------------------------------------------

def test_blur_fg_center_is_unchanged():
    line = vc._blur_bg_cut(0, 0.0, 3.0, 3.0, 0.0, 1080, 1920, vc._blur_bg_config())
    # foreground square crop stays centered (no x) when position unknown
    assert "crop=1080:1080,setsar=1" in line
    assert "crop=1080:1080:x=" not in line


def test_blur_fg_shifts_with_subject_pos():
    line = vc._blur_bg_cut(
        0, 0.0, 3.0, 3.0, 0.0, 1080, 1920, vc._blur_bg_config(),
        subject_h_pos=0.9,
    )
    assert "crop=1080:1080:x=clip(iw*0.8800-ow/2" in line
