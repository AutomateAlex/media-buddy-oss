"""

Validates _purpose_model resolution:
  1. env var (when set) wins, normalized for openrouter shortform
  2. _PURPOSE_DEFAULT_MODEL hits for verify + 4 director purposes (commit 8)
  3. None for unmapped purposes (caller falls back to self.model)
"""
from backend.lib.llm_client import LLMClient


# ----------------------------------------------------------------------
# Built-in defaults (commit 8)
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Env var override wins, with shortform normalization (commit 8)
# ----------------------------------------------------------------------
def test_verify_env_override_wins_anthropic(monkeypatch):
    """MEDIA_BUDDY_VERIFY_MODEL takes precedence over the built-in default."""
    monkeypatch.setenv("MEDIA_BUDDY_VERIFY_MODEL", "claude-sonnet-4-6")
    assert LLMClient._purpose_model("verify", "anthropic") == "claude-sonnet-4-6"


def test_env_shortform_normalized_for_openrouter(monkeypatch):
    """
    'anthropic/' vendor prefix so OpenRouter actually routes it (was 404
    silently before — same fal.ai shortform trap)."""
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "claude-sonnet-4-6")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "anthropic/claude-sonnet-4-6"
    # Anthropic direct keeps the bare form
    assert LLMClient._purpose_model(
        "plan_chunks", "anthropic"
    ) == "claude-sonnet-4-6"


def test_env_full_prefix_passes_through(monkeypatch):
    """User who supplies the prefix themselves gets it untouched (no
    double-prefixing)."""
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "anthropic/claude-haiku-4-5")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "anthropic/claude-haiku-4-5"


def test_env_gpt_shortform_routes_to_openai_on_openrouter(monkeypatch):
    """gpt-* and o1-* prefixes route through openai/ vendor."""
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "gpt-4o-mini")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "openai/gpt-4o-mini"
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "o1-mini")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "openai/o1-mini"


def test_env_gemini_shortform_routes_to_google_on_openrouter(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "gemini-2.5-flash")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "google/gemini-2.5-flash"


def test_env_unknown_vendor_left_alone(monkeypatch):
    """Unknown prefixes → return as-is. Lets cloud surface the 404
    explicitly instead of silently mangling."""
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "weird-model-9000")
    assert LLMClient._purpose_model(
        "plan_chunks", "openrouter"
    ) == "weird-model-9000"


def test_contracts_env_controls_both_purposes(monkeypatch):
    """Single MEDIA_BUDDY_CONTRACTS_MODEL flip flips both build_video_contract
    AND build_chunk_contracts (same family of structured-extraction calls)."""
    monkeypatch.setenv("MEDIA_BUDDY_CONTRACTS_MODEL", "claude-sonnet-4-6")
    assert LLMClient._purpose_model(
        "build_chunk_contracts", "openrouter"
    ) == "anthropic/claude-sonnet-4-6"
    assert LLMClient._purpose_model(
        "build_video_contract", "openrouter"
    ) == "anthropic/claude-sonnet-4-6"


# ----------------------------------------------------------------------
# Fallthrough behavior (unmapped purposes)
# ----------------------------------------------------------------------
def test_none_purpose_returns_none():
    """No purpose arg → provider default. Used by uncategorized _call sites."""
    assert LLMClient._purpose_model(None, "anthropic") is None
    assert LLMClient._purpose_model("", "anthropic") is None


def test_unknown_purpose_returns_none():
    """Purposes not in the env map fall through to provider default."""
    assert LLMClient._purpose_model("some_new_purpose", "anthropic") is None


def test_env_value_whitespace_trimmed(monkeypatch):
    """Leading/trailing whitespace in env value is stripped (defensive — env
    vars sometimes pick up trailing newlines from CI configs)."""
    monkeypatch.setenv("MEDIA_BUDDY_PLAN_MODEL", "  claude-haiku-4-5\n")
    assert LLMClient._purpose_model("plan_chunks", "anthropic") == "claude-haiku-4-5"


# ----------------------------------------------------------------------
# Direct _normalize_model_id_for_provider unit tests
# ----------------------------------------------------------------------
def test_normalize_anthropic_provider_is_passthrough():
    """Anthropic direct doesn't need vendor prefix."""
    assert LLMClient._normalize_model_id_for_provider(
        "claude-haiku-4-5", "anthropic"
    ) == "claude-haiku-4-5"


def test_normalize_openrouter_with_existing_slash_passthrough():
    assert LLMClient._normalize_model_id_for_provider(
        "anthropic/claude-haiku-4-5", "openrouter"
    ) == "anthropic/claude-haiku-4-5"


def test_normalize_openrouter_claude_gets_anthropic_prefix():
    assert LLMClient._normalize_model_id_for_provider(
        "claude-opus-4-7", "openrouter"
    ) == "anthropic/claude-opus-4-7"
