"""Tests for the per-install API token middleware (Phase 2.11g)."""
import os
import importlib

from fastapi.testclient import TestClient


def _client_with_token_check_enabled(monkeypatch):
    """Re-import main with MEDIA_BUDDY_DISABLE_TOKEN_CHECK off so the
    middleware actually fires. Done per-test so we don't pollute other
    fixtures."""
    monkeypatch.delenv("MEDIA_BUDDY_DISABLE_TOKEN_CHECK", raising=False)
    import backend.main as main_mod
    importlib.reload(main_mod)
    return TestClient(main_mod.app), main_mod


def test_token_required_for_api_endpoints(monkeypatch):
    client, _ = _client_with_token_check_enabled(monkeypatch)
    r = client.get("/api/system/status")
    assert r.status_code == 401
    body = r.json()
    assert body.get("header") == "X-MediaBuddy-Token"


def test_health_endpoint_is_public(monkeypatch):
    client, _ = _client_with_token_check_enabled(monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_correct_token_grants_access(monkeypatch):
    client, main_mod = _client_with_token_check_enabled(monkeypatch)
    tok = main_mod.get_token()
    r = client.get("/api/system/status", headers={"X-MediaBuddy-Token": tok})
    assert r.status_code == 200


def test_wrong_token_rejected(monkeypatch):
    client, _ = _client_with_token_check_enabled(monkeypatch)
    r = client.get(
        "/api/system/status",
        headers={"X-MediaBuddy-Token": "this-is-not-the-token"},
    )
    assert r.status_code == 401


# Reset state after this test module so other tests behave normally.
def teardown_module(_):
    os.environ["MEDIA_BUDDY_DISABLE_TOKEN_CHECK"] = "1"
    import backend.main as main_mod
    importlib.reload(main_mod)
