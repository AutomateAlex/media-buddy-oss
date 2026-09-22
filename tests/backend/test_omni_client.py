"""Tests for OmniClient — multimodal video critic."""
from unittest.mock import MagicMock, patch

import pytest


def test_parse_score_json_strict():
    from backend.lib.omni_client import _parse_score_json
    raw = '{"score":9,"visual_match":9,"pacing":8,"mood":9,"composition":8,"issues":[],"suggestion":""}'
    d = _parse_score_json(raw, "dashscope")
    assert d["score"] == 9.0
    assert d["provider"] == "dashscope"
    assert d["issues"] == []


def test_parse_score_json_strips_markdown_fence():
    """Models often wrap JSON in ```json ... ``` — extractor must survive."""
    from backend.lib.omni_client import _parse_score_json
    raw = '```json\n{"score": 8.5, "visual_match": 9, "pacing": 8, "mood": 8, "composition": 9}\n```'
    d = _parse_score_json(raw, "openrouter")
    assert d["score"] == 8.5
    assert d["composition"] == 9.0


def test_parse_score_json_handles_prose_prefix():
    from backend.lib.omni_client import _parse_score_json
    raw = '这段视频质量不错。\n{"score": 7.5, "visual_match": 7, "pacing": 8, "mood": 7, "composition": 8, "issues": ["a bit dark"]}'
    d = _parse_score_json(raw, "x")
    assert d["score"] == 7.5
    assert d["issues"] == ["a bit dark"]


def test_parse_score_json_raises_when_no_json():
    from backend.lib.omni_client import _parse_score_json
    with pytest.raises(ValueError):
        _parse_score_json("just prose, no json here", "x")


def test_factory_falls_through_to_openrouter_when_dashscope_missing(monkeypatch):
    """
    fallthrough — the factory still returns the OpenRouter shell, which will
    raise at call time if cloud isn't paired."""
    from backend.lib import omni_client
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("OMNI_PROVIDER", "dashscope")
    c = omni_client.get_omni_client()
    assert c.name == "openrouter"


def test_factory_explicit_openrouter(monkeypatch):
    """
    """
    from backend.lib import omni_client
    monkeypatch.setenv("OMNI_PROVIDER", "openrouter")

    fake_gw = MagicMock()
    fake_gw.is_authenticated.return_value = True
    # returns True without touching the real keychain.
    monkeypatch.setattr(
        omni_client.OpenRouterOmniClient,
        "_gateway",
        lambda self: fake_gw,
    )
    c = omni_client.get_omni_client()
    assert c.name == "openrouter"


