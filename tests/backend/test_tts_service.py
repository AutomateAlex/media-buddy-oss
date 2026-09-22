"""TTS service tests.

Chain in this build: Qwen first (MEDIA_BUDDY_TTS_QWEN, default on) → on
failure, ElevenLabs (MEDIA_BUDDY_ENABLE_ELEVENLABS) → free Microsoft Edge
voices as the ungated last resort.

The tests below pin the routing contract: every legacy / unknown / local-only
provider string a stored project might still carry (edge_*, piper_tts,
openai_tts, elevenlabs_tts, garbage) must land on the Edge fallback rather
than crash.
"""
from unittest.mock import MagicMock, patch


def _route(provider, **kw):
    from backend.services.tts_service import TTSService
    svc = TTSService()
    with patch.object(svc, "_synthesize_edge", return_value=MagicMock(success=True)) as m:
        svc.synthesize("Hello", "/tmp/out.mp3", provider=provider, **kw)
    m.assert_called_once()
    return m.call_args[0]


def test_synthesize_edge_provider_routes_to_edge():
    assert _route("edge_xiaoxiao")[2] == "zh-CN-YunyangNeural"


def test_synthesize_piper_provider_falls_back_to_edge():
    """Piper was removed (GPL conflict). piper_tts now falls back to the default Edge voice."""
    assert _route("piper_tts")[2] == "zh-CN-YunyangNeural"


def test_synthesize_openai_routes_to_edge():
    assert _route("openai_tts", voice_id="nova")[2] == "zh-CN-YunyangNeural"


def test_synthesize_elevenlabs_disabled_routes_to_edge(monkeypatch):
    """ElevenLabs is off unless MEDIA_BUDDY_ENABLE_ELEVENLABS=1; stale selections use Edge."""
    monkeypatch.delenv("MEDIA_BUDDY_ENABLE_ELEVENLABS", raising=False)
    assert _route("elevenlabs_tts", voice_id="abc")[2] == "zh-CN-YunyangNeural"


def test_synthesize_unknown_provider_falls_back_to_edge():
    assert _route("nonexistent_provider_xyz")[2] == "zh-CN-YunyangNeural"


def test_legacy_azure_provider_maps_to_same_edge_voice():
    """Projects saved with azure_* keep rendering: same Microsoft voice via Edge."""
    assert _route("azure_yunyang")[2] == "zh-CN-YunyangNeural"


def test_elevenlabs_tts_returns_error_without_key(monkeypatch):
    from backend.services.tts_service import TTSService
    for k in ("MEDIA_BUDDY_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for i in range(2, 8):
        monkeypatch.delenv(f"MEDIA_BUDDY_ELEVENLABS_API_KEY_{i}", raising=False)
    r = TTSService()._synthesize_elevenlabs("Hi", "/tmp/x.mp3", voice_id=None)
    assert not r.success
    assert r.error


def test_elevenlabs_alignment_writes_character_tokens():
    from backend.services.tts_service import TTSService

    alignment = {
        "characters": ["你", "好", "。"],
        "character_start_times_seconds": [0.0, 0.1234, 0.4567],
        "character_end_times_seconds": [0.12, 0.45, 0.6],
    }

    tokens = TTSService._elevenlabs_alignment_to_sentence_tokens("你好。", alignment)

    assert tokens == [
        {"kind": "char", "text": "你", "start": 0.0, "end": 0.12},
        {"kind": "char", "text": "好", "start": 0.123, "end": 0.45},
        {"kind": "char", "text": "。", "start": 0.457, "end": 0.6},
    ]


def test_elevenlabs_alignment_uses_original_chinese_text_when_api_masks_chars():
    from backend.services.tts_service import TTSService

    alignment = {
        "characters": ["?", "?", "?", "?", "?", " ", "M"],
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
    }

    tokens = TTSService._elevenlabs_alignment_to_sentence_tokens(
        "你好，我是 M", alignment,
    )

    assert [t["text"] for t in tokens] == ["你", "好", "，", "我", "是", " ", "M"]


def test_elevenlabs_alignment_rejects_missing_timestamps():
    from backend.services.tts_service import TTSService

    assert TTSService._elevenlabs_alignment_to_sentence_tokens("你好", None) == []
    assert TTSService._elevenlabs_alignment_to_sentence_tokens(
        "你好",
        {
            "characters": ["你", "好"],
            "character_start_times_seconds": [0.0],
            "character_end_times_seconds": [0.1, 0.2],
        },
    ) == []


def test_clamp_tokens_to_duration_keeps_chunk_inside_its_audio_span():
    """A chunk's last token spilling past its probed MP3 duration must be
    clamped, otherwise the next chunk's shifted tokens overlap → the join
    reads non-monotonic and real timing is thrown away."""
    from backend.services.tts_service import TTSService

    # last token ends at 5.2s but the chunk audio is only 5.0s
    tokens = [
        {"kind": "char", "text": "a", "start": 0.0, "end": 2.5},
        {"kind": "char", "text": "b", "start": 2.5, "end": 5.2},
    ]
    clamped = TTSService._clamp_tokens_to_duration(tokens, 5.0)
    assert all(t["end"] <= 5.0 and t["start"] <= 5.0 for t in clamped)
    assert clamped[-1]["end"] == 5.0


def test_enforce_monotonic_preserves_real_pacing_not_even_spacing():
    """The boundary-overlap repair must keep genuine per-word pacing (with
    pauses), NOT flatten everything to a constant step — the old even-char
    rebuild is what made long-video subtitles drift out of sync."""
    from backend.services.tts_service import TTSService

    # chunk-2 tokens (start=2.5) land before chunk-1's later token (start=3.0)
    # because chunk-1's alignment ran past its probed audio duration → the
    # assembled stream goes backwards → flagged non-monotonic.
    tokens = [
        {"kind": "char", "text": "a", "start": 0.0, "end": 1.0},
        {"kind": "char", "text": "b", "start": 3.0, "end": 5.0},   # long word / pause
        {"kind": "char", "text": "c", "start": 2.5, "end": 4.0},   # start < prev start
        {"kind": "char", "text": "d", "start": 4.0, "end": 4.5},
    ]
    assert not TTSService._timing_tokens_are_monotonic(tokens)
    repaired = TTSService._enforce_monotonic_tokens(tokens)
    # now monotonic
    assert TTSService._timing_tokens_are_monotonic(repaired)
    # the well-ordered long word keeps its genuine 2.0s length (a pause),
    # rather than being flattened to a constant step like even-char timing.
    assert repaired[0]["end"] == 1.0
    assert repaired[1]["end"] == 5.0
    assert repaired[2]["start"] == 5.0        # nudged to prev end, not flattened
    # NOT evenly spaced: step sizes differ (proves real pacing kept)
    steps = [round(t["end"] - t["start"], 3) for t in repaired]
    assert len(set(steps)) > 1
