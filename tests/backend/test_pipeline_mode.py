"""Tests for pipeline mode resolution + config."""


def test_resolve_mode_explicit_arg():
    from backend.lib.pipeline_mode import PipelineMode, resolve_mode
    assert resolve_mode("balanced") == PipelineMode.BALANCED
    assert resolve_mode(PipelineMode.PREMIUM) == PipelineMode.PREMIUM


def test_resolve_mode_env_fallback(monkeypatch):
    from backend.lib.pipeline_mode import PipelineMode, resolve_mode
    monkeypatch.setenv("PIPELINE_MODE", "balanced")
    assert resolve_mode() == PipelineMode.BALANCED
    monkeypatch.delenv("PIPELINE_MODE")
    assert resolve_mode() == PipelineMode.FAST


def test_resolve_mode_unknown_falls_back_to_fast(monkeypatch):
    from backend.lib.pipeline_mode import PipelineMode, resolve_mode
    monkeypatch.setenv("PIPELINE_MODE", "ultra")
    assert resolve_mode() == PipelineMode.FAST


def test_get_mode_config_fast_disables_kling():
    from backend.lib.pipeline_mode import PipelineMode, get_mode_config
    cfg = get_mode_config(PipelineMode.FAST)
    assert cfg["allow_kling"] is False
    assert cfg["allow_retry"] is False


def test_get_mode_config_balanced_enables_retry_and_kling():
    from backend.lib.pipeline_mode import PipelineMode, get_mode_config
    cfg = get_mode_config(PipelineMode.BALANCED)
    assert cfg["allow_kling"] is True
    assert cfg["allow_retry"] is True
    assert cfg["score_acceptable"] == 7.2
