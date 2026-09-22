"""上云 P1 — per-request user identity for multi-tenant cloud mode.

On the desktop the backend is single-user: there is one logged-in account per
machine and no per-request user scoping. On the cloud SaaS, many users share one
backend, so every request carries the logged-in user's JWT and we scope all
queries to that user.

`current_user_id(request)` extracts the user id (the JWT `sub` claim) from the
`Authorization: Bearer <jwt>` header. In desktop/local mode (no such header) it
returns None, and callers treat None as "the single local user" — so existing
single-user behavior is completely unchanged.

Multi-tenant mode is gated by env `MEDIA_BUDDY_MULTI_TENANT=1`. In that mode the
backend is internet-facing, so the incoming JWT's signature is VERIFIED here
render worker use to MINT these access tokens; (hosted build only)
``mint_user_access_token`` and lib/desktop-auth.ts ``verifyAccessToken``).
Signature/exp failure → the request resolves to NO user, so `owner_filter` fails
closed and `is_admin_request` returns False. Without this a caller could forge
any `sub`/`email` and read/modify any tenant's data or impersonate an admin.

Desktop/local mode (multi-tenant off) keeps the previous lenient decode: the
token comes from the local keychain and is already trusted.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional

from fastapi import Request

try:  # PyJWT (already a dependency — see api/auth.py)
    import jwt as _pyjwt
except Exception:  # pragma: no cover - import guard
    _pyjwt = None

logger = logging.getLogger("user_context")


def multi_tenant_enabled() -> bool:
    return (os.environ.get("MEDIA_BUDDY_MULTI_TENANT") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _jwt_secret() -> str:
    return (os.environ.get("DESKTOP_JWT_SECRET") or "").strip()


def _token_from_request(request: Request) -> Optional[str]:
    """The Bearer JWT (or, for GET resource tags that can't set headers, the
    ``?jwt=`` query param — the hosted web client appends it to ``<video>`` /
    ``<img>`` / ``<audio>`` URLs, see _withToken in api/client.ts)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:].strip() or None
    if request.method == "GET":
        q = request.query_params.get("jwt")
        if q:
            return q.strip() or None
    return None


def _decode_jwt_payload_unverified(token: str) -> dict:
    """Base64-decode the JWT payload WITHOUT verifying the signature. Only used
    on the desktop (local keychain token, already trusted)."""
    try:
        parts = token.split(".")
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64)) or {}
    except Exception:
        return {}


def _verified_claims(token: str) -> dict:
    """Claims from a token whose HS256 signature (``DESKTOP_JWT_SECRET``) AND
    ``exp`` are valid. ANY failure — forged/expired/unsigned/malformed token,
    missing secret, PyJWT unavailable — returns ``{}`` so every caller fails
    CLOSED (no user resolved → owner_filter matches nothing, admin denied)."""
    secret = _jwt_secret()
    if not secret or _pyjwt is None:
        # Multi-tenant is on but we cannot verify — misconfiguration. Fail closed
        # and log loudly rather than silently trusting unverified tokens.
        logger.error(
            "JWT cannot be verified in multi-tenant mode: %s",
            "DESKTOP_JWT_SECRET is not set" if not secret else "PyJWT not installed",
        )
        return {}
    try:
        return _pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp"], "verify_exp": True},
        ) or {}
    except Exception:
        return {}


def _claims(request: Request) -> dict:
    """Trusted claims for this request. Multi-tenant: signature-verified.
    Desktop: unverified decode of the local keychain token (unchanged)."""
    token = _token_from_request(request)
    if not token:
        return {}
    if multi_tenant_enabled():
        return _verified_claims(token)
    return _decode_jwt_payload_unverified(token)


def token_present_but_invalid(request: Request) -> bool:
    """请求**带了**令牌，但它验不过（伪造 / 过期 / 密钥缺失）。


    但**验不过时的表现是「没有用户」**，于是 `owner_filter` 空过滤，
    客户点自己的频道拿到的是 **404「频道不存在」**。

    前端有一套现成的自救逻辑：**遇 401 就悄悄续期再重试一次**
    （见 `lib/webAuth.ts`）。可 404 不是 401 —— 那套逻辑永远不触发，
    客户被直接踢回登录页。上线一小时内 `/api/series/{id}` 的 404
    从**上线前 8 小时的 0 条**涨到 **8 条**，被迫重新登录从 5 次涨到 12 次。

       不是错误状态。它必须回 401 让前端续期，回 404 等于把
       「你该续期了」说成「你的东西没了」。

    ⚠️ **没带令牌的请求返回 False** —— 那是匿名访问，公开端点照常放行，
       不能因为这次修复把它们也挡了。
    """

    if not multi_tenant_enabled():
        return False
    token = _token_from_request(request)
    if not token:
        return False
    return not _verified_claims(token)


def current_user_id(request: Request) -> Optional[str]:
    """Resolve the acting user id from the (verified, in cloud mode) JWT, or None
    in desktop mode / when no valid token is present."""
    claims = _claims(request)
    sub = claims.get("sub") or claims.get("user_id")
    return str(sub) if sub else None


# Sentinel that matches no row — used to return an empty set when a multi-tenant
# request arrives without a resolvable user (fail closed, never leak others' data).
_NO_MATCH = "__no_such_user__"


def owner_filter(query, model, request: Request):
    """Restrict a SQLAlchemy query to the current user's rows.

    Desktop / single-user mode (MEDIA_BUDDY_MULTI_TENANT off): returns the query
    UNCHANGED — behavior identical to before. Cloud mode: filters
    `model.user_id == <current user>` (or an empty set if no user resolved)."""
    if not multi_tenant_enabled():
        return query
    uid = current_user_id(request)
    return query.filter(model.user_id == (uid or _NO_MATCH))


def owner_value(request: Request) -> Optional[str]:
    """The user_id to stamp on newly-created rows. None in desktop mode."""
    return current_user_id(request) if multi_tenant_enabled() else None


def is_admin_request(request: Request) -> bool:
    """True iff the request's JWT identifies an operator/admin.

    Desktop mode (multi-tenant off) = always admin (one local operator). Cloud
    mode: the JWT is signature-verified, then its `sub` must be in env
    ``ADMIN_USER_IDS`` (comma-separated) or its `email` in ``ADMIN_EMAILS``. Used
    to gate the internal "suspect videos" board — customers must never see it."""
    if not multi_tenant_enabled():
        return True
    claims = _claims(request)  # signature-verified in multi-tenant mode
    uid = str(claims.get("sub") or claims.get("user_id") or "")
    email = str(claims.get("email") or "").strip().lower()
    admin_ids = {x.strip() for x in (os.environ.get("ADMIN_USER_IDS") or "").split(",") if x.strip()}
    admin_emails = {x.strip().lower() for x in (os.environ.get("ADMIN_EMAILS") or "").split(",") if x.strip()}
    return bool((uid and uid in admin_ids) or (email and email in admin_emails))
