import types


def test_get_falls_back_when_keyring_returns_empty(monkeypatch):
    from backend.lib import secret_store

    fallback = {"PEXELS_API_KEY": "fallback-value"}
    fake_keyring = types.SimpleNamespace(
        get_password=lambda service, key: None,
    )

    monkeypatch.setattr(secret_store, "_force_fallback", lambda key: False)
    monkeypatch.setattr(secret_store, "_keyring_available", lambda: True)
    monkeypatch.setattr(secret_store, "_read_fallback", lambda: fallback)
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)

    assert secret_store.get("PEXELS_API_KEY") == "fallback-value"
