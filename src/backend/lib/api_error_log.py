"""Best-effort recorder for failed API requests → the ``api_errors`` table.

Called from the global exception handlers in main.py. MUST never raise into the
response path: any failure here is swallowed. Auth/permission/not-found noise
(401/403/404/405) is skipped so the board stays a high-signal list of real
operation failures (rejected batches, failed generates, 5xx).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Skip expected/noisy statuses — these are not "operation bugs" worth a board row.
_SKIP_STATUS = {401, 403, 404, 405}

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONGNUM_RE = re.compile(r"/\d{4,}")


def normalize_path(path: str) -> str:
    """Collapse per-request ids so the board groups by route, not by instance:
    /api/series/<uuid>/batch → /api/series/{id}/batch."""
    p = _UUID_RE.sub("{id}", str(path or ""))
    p = _LONGNUM_RE.sub("/{id}", p)
    return p[:200]


def record_api_error(request, status_code: int, error_code: str | None = None) -> None:
    try:
        status_code = int(status_code)
        if status_code < 400 or status_code in _SKIP_STATUS:
            return
        path = ""
        method = ""
        user_id = None
        try:
            path = normalize_path(request.url.path)
            method = str(request.method or "")[:10]
        except Exception:
            pass
        try:
            from backend.lib.user_context import current_user_id
            user_id = current_user_id(request)
        except Exception:
            user_id = None

        from backend.database import SessionLocal
        from backend.models.api_error import ApiError

        db = SessionLocal()
        try:
            db.add(ApiError(
                status_code=status_code,
                method=method,
                path=path,
                error_code=(str(error_code)[:80] if error_code else None),
                user_id=user_id,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("record_api_error failed", exc_info=True)
