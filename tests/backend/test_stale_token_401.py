# -*- coding: utf-8 -*-
"""令牌过期时必须回 401，不能回 404。


那天上线开启了 JWT 签名 + `exp` 校验（安全上完全正确）。但验不过时
请求被当成**没有用户** → `owner_filter` 空过滤 → 客户点自己的频道拿到
**404「频道不存在」**，然后被踢回登录页，刷新一次重演一次。

前端本来就有自救逻辑：遇 401 悄悄续期再重试一次。可 404 不是 401，
那套逻辑从来没被触发。

    `/api/series/{id}` 的 404：0 → 8
    被迫重新登录：5 → 12

⚠️ 访问令牌每小时过期一次是**正常事件**，不是错误状态。
   回 404 等于把「你该续期了」说成「你的东西没了」。
"""

from __future__ import annotations

import time

import pytest

jwt = pytest.importorskip("jwt")

SECRET = "test-secret-for-stale-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_MULTI_TENANT", "1")
    monkeypatch.setenv("DESKTOP_JWT_SECRET", SECRET)
    monkeypatch.setenv("MEDIA_BUDDY_DISABLE_TOKEN_CHECK", "1")
    from fastapi.testclient import TestClient

    import backend.models  # noqa: F401
    from backend.database import Base, engine

    Base.metadata.create_all(bind=engine)
    from backend.main import app

    return TestClient(app)


def _token(sub: str, *, ttl: int) -> str:
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + ttl}, SECRET, algorithm="HS256"
    )


def test_an_expired_token_gets_401_not_404(client):
    """🚨 **这条就是整个修复的意义所在。**

    过期令牌必须换来 401 —— 那是前端唯一听得懂的「去续期」信号。
    回 404 的话客户看到的是「你的频道不见了」。
    """

    r = client.get(
        "/api/series/",
        headers={"Authorization": f"Bearer {_token('u-1', ttl=-60)}"},
    )
    assert r.status_code == 401, f"过期令牌拿到 {r.status_code}，前端不会去续期"


def test_a_forged_token_also_gets_401(client):
    """签名对不上的同样是 401（而不是静默当成匿名）。"""

    bad = jwt.encode({"sub": "u-1", "exp": int(time.time()) + 600},
                     "wrong-secret", algorithm="HS256")
    assert client.get(
        "/api/series/", headers={"Authorization": f"Bearer {bad}"}
    ).status_code == 401


def test_a_valid_token_is_untouched(client):
    """⚠️ 好令牌一个都不许误伤 —— 这一道只针对**坏令牌**。"""

    r = client.get(
        "/api/series/",
        headers={"Authorization": f"Bearer {_token('u-1', ttl=3600)}"},
    )
    assert r.status_code != 401, "有效令牌被这道中间件误杀了"


def test_a_request_with_no_token_is_untouched(client):
    """⚠️ 没带令牌 = 匿名访问，公开端点照常放行。

    在这里挡住的话，健康检查、登录页等等会一起坏掉 ——
    修一个故障造出一个更大的。
    """

    assert client.get("/health").status_code != 401


def test_auth_endpoints_are_never_blocked(client):
    """🚨 **续期请求自己往往还带着那张过期的访问令牌。**

    在这里挡住它，前端就永远续不了期 ——
    故障会从「偶尔被踢」升级成「谁都登不上」。
    """

    r = client.post(
        "/api/auth/web-refresh",
        json={"refresh_token": "whatever"},
        headers={"Authorization": f"Bearer {_token('u-1', ttl=-60)}"},
    )
    # ⚠️ 判据看的是 `token_expired` 这个标记，不是状态码 ——
    assert "token_expired" not in r.text, (
        "续期端点被这道中间件挡住了 —— 前端将永远无法续期，"
        "故障会从「偶尔被踢」升级成「谁都登不上」"
    )
