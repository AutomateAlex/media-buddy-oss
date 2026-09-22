"""统一 LLM 路由器:阿里直连(主) + 失败回落 OpenRouter(兜底)。"""
import pytest

from backend.lib.llm_client import LLMClient


def test_chain_unchanged_without_alibaba_key(monkeypatch):
    monkeypatch.delenv("MEDIA_BUDDY_QWEN_API_KEY", raising=False)
    monkeypatch.delenv("MEDIA_BUDDY_QWEN_BASE_URL", raising=False)
    c = LLMClient(provider="openrouter")
    assert c._provider_chain("verify") == ["openrouter"]  # 桌面/未配 → 行为不变










def test_provider_specific_env_can_override_alibaba(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_QWEN_API_KEY", "k")
    monkeypatch.setenv("MEDIA_BUDDY_QWEN_BASE_URL", "https://x/compatible-mode/v1")
    monkeypatch.setenv("MEDIA_BUDDY_VERIFY_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setenv("MEDIA_BUDDY_ALIBABA_VERIFY_MODEL", "qwen-max")
    c = LLMClient(provider="openrouter")

    assert c._purpose_model("verify", "alibaba") == "qwen-max"








def test_alibaba_research_pack_strict_rejects_unusable_pack(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_QWEN_API_KEY", "k")
    monkeypatch.setenv("MEDIA_BUDDY_QWEN_BASE_URL", "https://x/compatible-mode/v1")
    c = LLMClient(provider="openrouter")

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "太短，没有来源"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())

    with pytest.raises(RuntimeError, match="grounding_pack_unavailable"):
        c.build_research_pack("巴菲特名言", strict=True)
