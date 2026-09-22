"""Tests for MusicService — segment time mapping + Pixabay/Freesound routing."""
from pathlib import Path
from unittest.mock import MagicMock, patch


def _seg(start, end, mood="calm", energy="medium", role="intro", kw="ambient music"):
    from backend.services.music_service import MusicSegment
    return MusicSegment(
        start_chunk_index=start,
        end_chunk_index=end,
        start_seconds=0.0,
        end_seconds=0.0,
        narrative_role=role,
        mood=mood,
        energy=energy,
        search_keywords=kw,
    )


def test_map_segments_to_seconds_distributes_per_chunk_durations():
    """Each segment's seconds bounds = cumulative tts_duration of its chunks."""
    from backend.services.music_service import map_segments_to_seconds

    raw = [
        {"start_chunk_index": 0, "end_chunk_index": 1,
         "narrative_role": "intro", "mood": "calm", "energy": "low",
         "search_keywords": "calm acoustic"},
        {"start_chunk_index": 2, "end_chunk_index": 3,
         "narrative_role": "climax", "mood": "uplifting", "energy": "high",
         "search_keywords": "uplifting cinematic"},
    ]
    durations = [3.0, 4.5, 2.0, 6.0]   # total 15.5s
    out = map_segments_to_seconds(raw, durations)
    assert len(out) == 2
    assert out[0].start_seconds == 0.0
    assert out[0].end_seconds == 7.5      # 3.0 + 4.5
    assert out[1].start_seconds == 7.5
    assert out[1].end_seconds == 15.5     # 7.5 + 2.0 + 6.0
    assert out[1].mood == "uplifting"








def test_build_bgm_track_calls_concat_with_segment_paths(tmp_path):
    from backend.services.music_service import MusicService, MusicResult

    svc = MusicService.__new__(MusicService)
    fake_mix = MagicMock()
    fake_mix.concat_with_crossfade.return_value = tmp_path / "bgm.mp3"
    svc._audio_mix = fake_mix
    svc.pixabay = MagicMock()
    svc.freesound = MagicMock()

    # Two segment files with non-empty content
    p1 = tmp_path / "seg_000.mp3"
    p1.write_bytes(b"\x00")
    p2 = tmp_path / "seg_001.mp3"
    p2.write_bytes(b"\x00")

    results = [
        MusicResult(local_path=str(p1), title="A", duration=60.0,
                    source="pixabay_music", source_url="", license="CC0", query="x"),
        MusicResult(local_path=str(p2), title="B", duration=60.0,
                    source="pixabay_music", source_url="", license="CC0", query="y"),
    ]
    out = svc.build_bgm_track(results, tmp_path / "bgm.mp3")
    assert out is not None
    fake_mix.concat_with_crossfade.assert_called_once()
    paths_arg = fake_mix.concat_with_crossfade.call_args[0][0]
    assert [str(p) for p in paths_arg] == [str(p1), str(p2)]
