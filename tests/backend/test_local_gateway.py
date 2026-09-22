"""LocalGateway — same surface as the old CloudGateway, backed by the user's
own API keys. These tests never touch the network: every outbound call is
monkeypatched at the single `_post_json` / `_fal_subscribe` seam.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from backend.lib import cloud_gateway
from backend.lib.cloud_auth import NotAuthenticatedError
from backend.lib.local_gateway import LocalGateway


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "OMNI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    cloud_gateway.reset_default_gateway()
    yield
    cloud_gateway.reset_default_gateway()


# ---------------------------------------------------------------- identity

def test_default_gateway_is_local_and_a_cloud_gateway_subclass():
    gw = cloud_gateway.get_default_gateway()
    assert isinstance(gw, LocalGateway)
    assert isinstance(gw, cloud_gateway.CloudGateway)
    assert cloud_gateway.get_default_gateway() is gw


def test_is_authenticated_follows_openrouter_key(monkeypatch):
    gw = LocalGateway()
    assert gw.is_authenticated() is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert gw.is_authenticated() is True


def test_get_quota_is_unlimited(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    q = asyncio.run(LocalGateway().get_quota())
    assert q["plan"] == "local"
    assert q["unlimited"] is True


# ---------------------------------------------------------------- llm

def test_llm_completion_requires_key():
    with pytest.raises(NotAuthenticatedError):
        asyncio.run(LocalGateway().llm_completion(
            messages=[{"role": "user", "content": "hi"}], idempotency_key="k",
        ))


def test_llm_completion_posts_to_openrouter_and_returns_content(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    captured = {}

    async def fake_post(self, url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers)
        return 200, {
            "choices": [{"message": {"content": "hello back"}}],
            "model": json["model"],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    monkeypatch.setattr(LocalGateway, "_post_json", fake_post)
    gw = LocalGateway()
    out = asyncio.run(gw.llm_completion(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        idempotency_key="abc",
        max_tokens=77,
        model_id="anthropic/claude-haiku-4-5",
    ))
    assert out["content"] == "hello back"
    assert out["model_used"] == "anthropic/claude-haiku-4-5"
    assert out["job_id"] == "abc"
    assert captured["url"].startswith("https://openrouter.ai/api/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["json"]["max_tokens"] == 77
    assert captured["json"]["messages"][1]["content"] == "u"


def test_llm_completion_uses_default_model_when_unspecified(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = {}

    async def fake_post(self, url, *, json, headers, timeout):
        seen["model"] = json["model"]
        return 200, {"choices": [{"message": {"content": "x"}}]}

    monkeypatch.setattr(LocalGateway, "_post_json", fake_post)
    asyncio.run(LocalGateway().llm_completion(
        messages=[{"role": "user", "content": "u"}], idempotency_key="k",
    ))
    assert seen["model"] == LocalGateway.DEFAULT_TEXT_MODEL


def test_llm_completion_maps_upstream_errors(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    async def fake_post(self, url, *, json, headers, timeout):
        return 402, {"error": {"message": "insufficient credits"}}

    monkeypatch.setattr(LocalGateway, "_post_json", fake_post)
    with pytest.raises(cloud_gateway.UpstreamError) as ei:
        asyncio.run(LocalGateway().llm_completion(
            messages=[{"role": "user", "content": "u"}], idempotency_key="k",
        ))
    assert "402" in str(ei.value)


# ---------------------------------------------------------------- tts

def test_tts_synthesize_requires_elevenlabs_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(NotAuthenticatedError):
        asyncio.run(LocalGateway().tts_synthesize(
            text="hi", voice_id="v1", idempotency_key="k",
        ))


def test_tts_synthesize_with_timestamps_decodes_audio_and_alignment(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    audio = b"ID3fake-mp3"
    alignment = {"characters": ["h", "i"], "character_start_times_seconds": [0.0, 0.1],
                 "character_end_times_seconds": [0.1, 0.2]}
    captured = {}

    async def fake_post(self, url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers)
        return 200, {
            "audio_base64": base64.b64encode(audio).decode(),
            "alignment": alignment,
            "normalized_alignment": alignment,
        }

    monkeypatch.setattr(LocalGateway, "_post_json", fake_post)
    out_bytes, meta = asyncio.run(LocalGateway().tts_synthesize(
        text="hi", voice_id="v1", idempotency_key="k", with_timestamps=True, speed=1.1,
    ))
    assert out_bytes == audio
    assert meta["alignment"] == alignment
    assert meta["normalized_alignment"] == alignment
    assert "/v1/text-to-speech/v1/with-timestamps" in captured["url"]
    assert captured["headers"]["xi-api-key"] == "el-test"
    assert captured["json"]["voice_settings"]["speed"] == pytest.approx(1.1)


def test_tts_voices_lists_edge_first_with_required_fields(monkeypatch):
    voices = asyncio.run(LocalGateway().tts_voices())
    assert voices["model"]
    assert isinstance(voices["voices"], list)


# ---------------------------------------------------------------- video


