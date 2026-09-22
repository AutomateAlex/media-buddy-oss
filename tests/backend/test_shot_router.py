"""Tests for ShotRouter — single provider per chunk."""


def test_static_shot_routes_to_seedance():
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.FAST)
    d = r.route(shot_type="atmosphere")
    assert d.provider == "seedance_lite"
    assert d.fallback_provider == "pika_v2_turbo"
    assert d.max_retries == 0  # Fast mode no retry


def test_human_action_routes_to_pika():
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.BALANCED)
    d = r.route(shot_type="human_action")
    assert d.provider == "pika_v2_turbo"
    assert d.fallback_provider == "seedance_lite"
    assert d.max_retries == 1  # Balanced allows retry


def test_hero_shot_routes_to_kling_in_balanced():
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.BALANCED)
    d = r.route(shot_type="atmosphere", is_hero_shot=True)
    assert d.provider == "kling_v16_std"
    assert d.fallback_provider == "pika_v2_turbo"


def test_hero_shot_does_not_route_to_kling_in_fast():
    """Fast mode disables Kling entirely; hero falls back to pika."""
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.FAST)
    d = r.route(shot_type="vfx", is_hero_shot=True)
    assert d.provider == "pika_v2_turbo"
    assert d.fallback_provider == "seedance_lite"


def test_complex_motion_routes_to_kling_in_premium():
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.PREMIUM)
    d = r.route(shot_type="complex_motion")
    assert d.provider == "kling_v16_std"


def test_unknown_shot_type_falls_back_to_seedance():
    from backend.services.shot_router import ShotRouter
    from backend.lib.pipeline_mode import PipelineMode
    r = ShotRouter(mode=PipelineMode.FAST)
    d = r.route(shot_type="unknown_alien_thing")
    assert d.provider == "seedance_lite"
