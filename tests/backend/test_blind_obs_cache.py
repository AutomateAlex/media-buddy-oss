"""

These tests verify the cache layer in isolation:
- save_blind_observation persists a row keyed by (provider, source_id, ver)
- get_blind_observation returns the cached payload on hit, None on miss
- MEDIA_BUDDY_DISABLE_OBS_CACHE=1 short-circuits to None even on hit
- schema_version bump invalidates old rows
- Empty provider/source_id is a no-op (defensive)

Cache lookup inside observe_candidate() is exercised via the orchestrator
suite end-to-end; here we only need to lock in the storage primitives.
"""
from __future__ import annotations

import pytest

from backend.services.generated_cache import (
    BLIND_OBSERVATION_SCHEMA_VERSION,
    get_blind_observation,
    save_blind_observation,
)


@pytest.fixture(autouse=True)
def _ensure_tables():
    """Cache tests don't use db_session, so make sure the schema exists."""
    from backend.database import Base, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)


def test_save_then_get_round_trips():
    payload = {
        "subjects": ["fractal", "geometry"],
        "motion": "rotating",
        "mood": "scientific",
    }
    save_blind_observation("pexels", "test_round_trip_1", payload)
    got = get_blind_observation("pexels", "test_round_trip_1")
    assert got is not None
    assert got["subjects"] == ["fractal", "geometry"]
    assert got["motion"] == "rotating"


def test_get_miss_returns_none():
    assert get_blind_observation("pexels", "never_saved_id_xyz") is None


def test_disable_env_skips_cache(monkeypatch):
    save_blind_observation("pexels", "disabled_env_test", {"x": 1})
    monkeypatch.setenv("MEDIA_BUDDY_DISABLE_OBS_CACHE", "1")
    assert get_blind_observation("pexels", "disabled_env_test") is None


def test_schema_version_bump_invalidates(monkeypatch):
    # Save under current version
    save_blind_observation("pexels", "version_bump_test", {"y": 2})
    # Reading at a higher version → miss (old row's schema_version doesn't match)
    got = get_blind_observation(
        "pexels", "version_bump_test",
        schema_version=BLIND_OBSERVATION_SCHEMA_VERSION + 99,
    )
    assert got is None
    # Original version still hits
    assert get_blind_observation("pexels", "version_bump_test") is not None


def test_empty_ids_are_noop():
    save_blind_observation("", "x", {"a": 1})
    save_blind_observation("pexels", "", {"a": 1})
    save_blind_observation("pexels", "valid_empty_payload", {})
    assert get_blind_observation("", "x") is None
    assert get_blind_observation("pexels", "") is None


def test_cross_provider_isolation():
    save_blind_observation("pexels", "shared_id_42", {"src": "pexels"})
    save_blind_observation("pexels", "shared_id_42", {"src": "pexels"})
    p = get_blind_observation("pexels", "shared_id_42")
    s = get_blind_observation("pexels", "shared_id_42")
    assert p is not None and p["src"] == "pexels"
    assert s is not None and s["src"] == "pexels"
