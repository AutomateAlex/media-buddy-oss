"""Tests for AudioMixService — concat + narration/music mix."""
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_concat_single_segment_just_copies(tmp_path):
    from backend.services.audio_mix_service import AudioMixService
    src = tmp_path / "seg_000.mp3"
    src.write_bytes(b"\x00\x01\x02")
    dst = tmp_path / "out.mp3"

    svc = AudioMixService()
    svc.mixer = MagicMock()  # not used here
    out = svc.concat_with_crossfade([src], dst)
    assert out == dst
    assert dst.exists()
    assert dst.read_bytes() == b"\x00\x01\x02"


def test_concat_multiple_segments_invokes_ffmpeg(tmp_path):
    from backend.services.audio_mix_service import AudioMixService
    seg1 = tmp_path / "s1.mp3"
    seg1.write_bytes(b"\x00")
    seg2 = tmp_path / "s2.mp3"
    seg2.write_bytes(b"\x00")
    seg3 = tmp_path / "s3.mp3"
    seg3.write_bytes(b"\x00")
    dst = tmp_path / "bgm.mp3"

    svc = AudioMixService()
    svc.mixer = MagicMock()

    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stderr="")
    with patch("backend.services.audio_mix_service.subprocess.run", fake_run):
        # Pretend ffmpeg actually wrote the file
        dst.write_bytes(b"\x00")
        out = svc.concat_with_crossfade([seg1, seg2, seg3], dst, crossfade_seconds=1.5)
    assert out == dst
    args = fake_run.call_args[0][0]
    # filter_complex chains acrossfade across 3 inputs → 2 acrossfade nodes
    fc_str = args[args.index("-filter_complex") + 1]
    assert fc_str.count("acrossfade") == 2
    assert "[out]" in fc_str
    assert "d=1.5" in fc_str


def test_mix_narration_and_music_invokes_ffmpeg_with_proven_filter(tmp_path):
    """Phase 2.7 — mixer goes straight to manual ffmpeg (skips OM full_mix
    which has the asplit bug). Verify the verified filter graph is used:
    aformat to stereo 48kHz, plain amix (no sidechain), 3 kHz EQ cut."""
    from backend.services.audio_mix_service import AudioMixService
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"\x00")
    music = tmp_path / "m.mp3"
    music.write_bytes(b"\x00")
    dst = tmp_path / "mixed.wav"

    svc = AudioMixService()
    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stderr="")
    with patch("backend.services.audio_mix_service.subprocess.run", fake_run):
        dst.write_bytes(b"\x00")  # pretend ffmpeg created it
        ok = svc.mix_narration_and_music(narration, music, dst)
    # v2 returns a levels dict on success (mocked LUFS → fallback path), not bool.
    assert ok  # truthy levels dict
    args = fake_run.call_args[0][0]
    fc = args[args.index("-filter_complex") + 1]
    # Verified filter graph features:
    assert "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo" in fc
    assert "equalizer=f=3000" in fc
    assert "amix=inputs=2" in fc
    assert "loudnorm=I=-16" in fc
    # Critical: NO sidechain ducking (proven to mute music)
    assert "sidechaincompress" not in fc


def test_default_path_has_no_sidechain_when_director_off(tmp_path, monkeypatch):
    """P2-14 — with MEDIA_BUDDY_AI_BGM_DIRECTOR unset/off, the mix must use the
    proven static graph: no sidechaincompress. Guards zero-behaviour-change."""
    from backend.services.audio_mix_service import AudioMixService
    monkeypatch.delenv("MEDIA_BUDDY_AI_BGM_DIRECTOR", raising=False)
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"\x00")
    music = tmp_path / "m.mp3"
    music.write_bytes(b"\x00")
    dst = tmp_path / "mixed.wav"

    svc = AudioMixService()
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch("backend.services.audio_mix_service.subprocess.run", fake_run):
        dst.write_bytes(b"\x00")
        svc.mix_narration_and_music(narration, music, dst)
    fc = fake_run.call_args[0][0][
        fake_run.call_args[0][0].index("-filter_complex") + 1
    ]
    assert "sidechaincompress" not in fc


def test_director_on_uses_voice_first_sidechain_ducking(tmp_path, monkeypatch):
    """P2-14 — when MEDIA_BUDDY_AI_BGM_DIRECTOR is on, the mix ducks BGM under
    narration via sidechaincompress, and both inputs are aformat'd to stereo
    48k BEFORE the sidechain (the root-cause fix vs the old broken attempt)."""
    from backend.services.audio_mix_service import AudioMixService
    monkeypatch.setenv("MEDIA_BUDDY_AI_BGM_DIRECTOR", "1")
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"\x00")
    music = tmp_path / "m.mp3"
    music.write_bytes(b"\x00")
    dst = tmp_path / "mixed.wav"

    svc = AudioMixService()
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch("backend.services.audio_mix_service.subprocess.run", fake_run):
        dst.write_bytes(b"\x00")
        levels = svc.mix_narration_and_music(narration, music, dst)
    assert levels  # truthy levels dict on success
    assert levels.get("ducking") is True
    fc = fake_run.call_args[0][0][
        fake_run.call_args[0][0].index("-filter_complex") + 1
    ]
    assert "sidechaincompress" in fc
    # narration keys the compressor (voice-first ducking)
    assert "[narr_key]" in fc
    assert "amix=inputs=2" in fc
    # aformat must precede the sidechain node — layout alignment is the fix
    assert fc.index("aformat") < fc.index("sidechaincompress")


def test_mix_with_video_duration_applies_bgm_fade_out(tmp_path):
    """When video_duration is provided, music fades out at end-2.5s."""
    from backend.services.audio_mix_service import AudioMixService
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"\x00")
    music = tmp_path / "m.mp3"
    music.write_bytes(b"\x00")
    dst = tmp_path / "mixed.wav"

    svc = AudioMixService()
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch("backend.services.audio_mix_service.subprocess.run", fake_run):
        dst.write_bytes(b"\x00")
        svc.mix_narration_and_music(
            narration, music, dst,
            video_duration=36.0, bgm_fade_out_seconds=2.5,
        )
    fc = fake_run.call_args[0][0][fake_run.call_args[0][0].index("-filter_complex") + 1]
    # Music should have fade-out at 33.5s (=36-2.5) for 2.5s duration
    assert "afade=t=out:st=33.5:d=2.5" in fc
    # Final atrim to video duration
    assert "atrim=end=36.000" in fc
