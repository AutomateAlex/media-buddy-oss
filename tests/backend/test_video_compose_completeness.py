"""media-buddy-rules.md §11 + §14: _mux_audio must not truncate narration
even when the video reel is shorter. Tests focus on the construction of
the ffmpeg command (we don't actually run ffmpeg in unit tests — that
takes too long and depends on the binary being present)."""
from pathlib import Path
from unittest.mock import patch


def test_mux_command_pads_when_video_shorter_than_audio(tmp_path, monkeypatch):
    """Video 5s, audio 10s → ffmpeg cmd MUST include tpad filter and
    set output -t to audio_dur. No -shortest."""
    from backend.lib import video_compose as vc

    vid = tmp_path / "v.mp4"
    aud = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    vid.write_bytes(b"\x00")
    aud.write_bytes(b"\x00")

    def fake_probe(p: Path) -> float:
        return 5.0 if p == vid else 10.0
    monkeypatch.setattr(vc, "_probe_duration", fake_probe)

    captured = {}

    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    monkeypatch.setattr(vc.subprocess, "run", fake_run)

    composer = vc.VideoCompose()
    composer._mux_audio("ffmpeg", vid, aud, out)

    cmd = captured["cmd"]
    cmd_str = " ".join(str(c) for c in cmd)
    assert "tpad=stop_mode=clone" in cmd_str
    assert "stop_duration=5.000" in cmd_str
    assert "-t" in cmd and "10.000" in cmd
    assert "-shortest" not in cmd_str  # MUST be gone


def test_mux_command_no_pad_when_video_covers_audio(tmp_path, monkeypatch):
    """Video already ≥ audio → no tpad, use -c:v copy + -t audio_dur."""
    from backend.lib import video_compose as vc

    vid = tmp_path / "v.mp4"
    aud = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    vid.write_bytes(b"\x00")
    aud.write_bytes(b"\x00")

    monkeypatch.setattr(
        vc, "_probe_duration",
        lambda p: 12.0 if p == vid else 10.0,
    )

    captured = {}

    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    monkeypatch.setattr(vc.subprocess, "run", fake_run)

    vc.VideoCompose()._mux_audio("ffmpeg", vid, aud, out)

    cmd_str = " ".join(str(c) for c in captured["cmd"])
    assert "tpad" not in cmd_str
    assert "-c:v copy" in cmd_str
    assert "-t" in captured["cmd"]


def test_mux_falls_back_to_shortest_when_probe_fails(tmp_path, monkeypatch):
    """ffprobe missing → still try to mux (log warning); use -shortest
    rather than refusing to render."""
    from backend.lib import video_compose as vc

    vid = tmp_path / "v.mp4"
    aud = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    vid.write_bytes(b"\x00")
    aud.write_bytes(b"\x00")

    monkeypatch.setattr(vc, "_probe_duration", lambda p: 0.0)

    captured = {}
    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    monkeypatch.setattr(vc.subprocess, "run", fake_run)

    vc.VideoCompose()._mux_audio("ffmpeg", vid, aud, out)
    assert "-shortest" in captured["cmd"]


