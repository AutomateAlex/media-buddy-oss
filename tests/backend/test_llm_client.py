from unittest.mock import patch, MagicMock


def test_build_system_prompt_includes_format():
    from backend.lib.llm_client import LLMClient

    client = LLMClient(provider="anthropic")
    prompt = client._build_script_system_prompt("youtube_shorts", 30, "")
    assert "9:16" in prompt or "Short" in prompt
    assert "30" in prompt


def test_build_system_prompt_overrides_reward_cta_context():
    from backend.lib.llm_client import LLMClient

    client = LLMClient(provider="anthropic")
    bad_context = "旧模板：评论区留言，三位随机评论者将获赠一杯茶。"
    prompt = client._build_script_system_prompt("youtube_shorts", 30, bad_context)

    assert prompt.rfind("FINAL NON-NEGOTIABLE CTA SAFETY") > prompt.rfind("获赠一杯茶")
    assert "forbidden anti-examples" in prompt


def test_analyze_music_arc_normalizes_segments():
    """LLM returns 2 segments for a 10-chunk script; output must cover [0..9]
    contiguously, with normalized fields."""
    from backend.lib.llm_client import LLMClient
    client = LLMClient(provider="anthropic")
    fake_response = (
        '{"segments":[{"start_chunk_index":0,"end_chunk_index":4,'
        '"narrative_role":"intro","mood":"calm","energy":"low",'
        '"search_keywords":"calm acoustic piano"},'
        '{"start_chunk_index":5,"end_chunk_index":9,'
        '"narrative_role":"climax","mood":"uplifting","energy":"high",'
        '"search_keywords":"uplifting cinematic"}]}'
    )
    sentences = [f"sentence {i}." for i in range(10)]
    durations = [3.0] * 10
    with patch.object(client, "_call", return_value=fake_response):
        out = client.analyze_music_arc(sentences, durations)
    assert len(out) == 2
    assert out[0]["start_chunk_index"] == 0
    assert out[0]["end_chunk_index"] == 4
    assert out[1]["start_chunk_index"] == 5
    assert out[1]["end_chunk_index"] == 9
    assert out[0]["mood"] == "calm"
    assert out[1]["energy"] == "high"


def test_analyze_music_arc_no_context_prompt_unchanged():
    """P2-14 — without content_context, the composer prompt must NOT contain the
    CONTENT CONTEXT block (byte-level guarantee of zero behaviour change)."""
    from backend.lib.llm_client import LLMClient
    client = LLMClient(provider="anthropic")
    fake = (
        '{"segments":[{"start_chunk_index":0,"end_chunk_index":2,'
        '"narrative_role":"intro","mood":"calm","energy":"low",'
        '"search_keywords":"calm piano"}]}'
    )
    captured = {}

    def _capture(system, user):
        captured["system"] = system
        return fake

    with patch.object(client, "_call", side_effect=_capture):
        client.analyze_music_arc(["a.", "b.", "c."], [3.0, 3.0, 3.0])
    assert "CONTENT CONTEXT" not in captured["system"]
    assert "Tonality guidance" not in captured["system"]


def test_analyze_music_arc_injects_content_context():
    """P2-14 — when content_context is passed, the composer prompt carries the
    series/industry/title/mood-lock signals + tonality guidance."""
    from backend.lib.llm_client import LLMClient
    client = LLMClient(provider="anthropic")
    fake = (
        '{"segments":[{"start_chunk_index":0,"end_chunk_index":2,'
        '"narrative_role":"intro","mood":"tense","energy":"high",'
        '"search_keywords":"dark suspense synth"}]}'
    )
    captured = {}

    def _capture(system, user):
        captured["system"] = system
        return fake

    ctx = {
        "series_name": "Crime Files",
        "description": "true-crime mysteries",
        "industry": "crime",
        "title": "The Vanishing",
        "mood_lock": "dark_tense",
    }
    with patch.object(client, "_call", side_effect=_capture):
        client.analyze_music_arc(["a.", "b.", "c."], [3.0, 3.0, 3.0], content_context=ctx)
    sys = captured["system"]
    assert "CONTENT CONTEXT" in sys
    assert "Crime Files" in sys
    assert "crime" in sys
    assert "The Vanishing" in sys
    assert "HARD CONSTRAINT" in sys and "dark_tense" in sys
    assert "Tonality guidance" in sys


def test_analyze_music_arc_falls_back_on_llm_error():
    """If LLM throws or returns garbage, return one safe segment covering all chunks."""
    from backend.lib.llm_client import LLMClient
    client = LLMClient(provider="anthropic")
    sentences = ["one.", "two.", "three."]
    durations = [2.0, 2.0, 2.0]
    with patch.object(client, "_call", side_effect=RuntimeError("LLM down")):
        out = client.analyze_music_arc(sentences, durations)
    assert len(out) == 1
    assert out[0]["start_chunk_index"] == 0
    assert out[0]["end_chunk_index"] == 2


def test_unknown_provider_raises():
    from backend.lib.llm_client import LLMClient
    import pytest

    client = LLMClient(provider="unknown_xyz")
    with pytest.raises(ValueError, match="Unknown provider"):
        client.generate_script("test")


def test_closure_purpose_is_registered():
    """P7:代码只引用档位,运维改 env 就能换模型/调成本,不必动代码。"""
    from backend.lib.llm_client import LLMClient

    assert LLMClient._PURPOSE_ENV_VARS.get("closure") == "MEDIA_BUDDY_CLOSURE_MODEL"


def test_closure_falls_back_to_verify_when_unconfigured(monkeypatch):
    """未配置 closure 档 → 回落 verify 档,不许掉到默认大模型上(那会贵很多)。"""
    from backend.lib.llm_client import LLMClient

    monkeypatch.delenv("MEDIA_BUDDY_CLOSURE_MODEL", raising=False)
    monkeypatch.setenv("MEDIA_BUDDY_VERIFY_MODEL", "vendor/cheap-verify")

    assert LLMClient._purpose_model("closure", "openrouter") == "vendor/cheap-verify"


def test_closure_override_wins_over_verify(monkeypatch):
    """配了 closure 档就用它 —— 这正是"把收口单独指到更便宜的模型"的用法。"""
    from backend.lib.llm_client import LLMClient

    monkeypatch.setenv("MEDIA_BUDDY_VERIFY_MODEL", "vendor/cheap-verify")
    monkeypatch.setenv("MEDIA_BUDDY_CLOSURE_MODEL", "vendor/even-cheaper")

    assert LLMClient._purpose_model("closure", "openrouter") == "vendor/even-cheaper"


def test_closure_falls_back_to_alibaba_verify_on_alibaba(monkeypatch):
    from backend.lib.llm_client import LLMClient

    monkeypatch.delenv("MEDIA_BUDDY_ALIBABA_CLOSURE_MODEL", raising=False)
    monkeypatch.setenv("MEDIA_BUDDY_ALIBABA_VERIFY_MODEL", "qwen-cheap")

    assert LLMClient._purpose_model("closure", "alibaba") == "qwen-cheap"
