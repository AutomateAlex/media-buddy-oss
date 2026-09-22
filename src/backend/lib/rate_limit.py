"""轻量每用户限流兜底(H2)。

有些贵操作(AI 查重 /dedupe、契合 /fit-check、编导对话)会真金白银调 LLM,但**不走
放大。这里给每用户 + 每类操作加一个**内存滑动窗口**限流,挡住脚本狂刷。

设计取舍(诚实说明):
- **进程内**计数(不依赖 DB/Redis)——真「轻量」,但每个 API 进程各算各的,多进程/多机
  时有效上限 ≈ 单进程上限 × 进程数。作为「防脚本狂刷」的兜底足够;要**精确的全局日配额**
  需加持久计数表(另议)。
- 桌面/单机(非多租户)不限。

用法::
    from backend.lib.rate_limit import enforce_rate_limit
    enforce_rate_limit(request, "dedupe", per_min=20)
超限 → HTTP 429(带 retry_after)。
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import HTTPException, Request

from backend.lib.user_context import current_user_id, multi_tenant_enabled

_lock = threading.Lock()
# key = (kind, uid) → 最近命中的时间戳队列(秒)
_hits: dict[tuple[str, str], Deque[float]] = defaultdict(deque)
# 简易内存增长护栏:key 数超过阈值时清掉最久没动的一批(防内存泄漏,极端也就丢几条计数)。
_MAX_KEYS = 50000


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except Exception:
        return default


def enforce_rate_limit(request: Request, kind: str, per_min: int, window_seconds: int = 60) -> None:
    """每用户每 `window_seconds` 内 `kind` 操作不超过 `per_min` 次,超了抛 429。
    上限可用 env `MB_RATELIMIT_<KIND大写>` 覆盖(0=不限)。多租户才生效。"""
    if not multi_tenant_enabled():
        return
    limit = _int_env(f"MB_RATELIMIT_{kind.upper()}", per_min)
    if limit <= 0:
        return
    uid = current_user_id(request) or "anon"
    now = time.time()
    cutoff = now - window_seconds
    key = (kind, uid)
    with _lock:
        if len(_hits) > _MAX_KEYS:
            _hits.clear()  # 极端兜底,宁可漏放也不涨爆内存
        dq = _hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(dq[0] + window_seconds - now) + 1
            raise HTTPException(status_code=429, detail={
                "error": "rate_limited",
                "message": "操作太频繁,请稍后再试~",
                "retry_after": max(1, retry),
            })
        dq.append(now)
