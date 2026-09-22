"""Phase 2.10a — Studio chat session endpoints."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import owner_filter, owner_value, multi_tenant_enabled
from backend.models.studio_session import StudioSession
from backend.schemas.studio import (
    ChatMessage, StudioSendMessage, StudioSendMessageResponse,
    StudioSessionCreate, StudioSessionRead,
)

router = APIRouter()


def _set_request_gateway_token(request: Request) -> None:
    """
    unauthenticated → 'cloud not authenticated; user must log in', so the
    director silently falls back to a canned reply (looks generic/repeated).
    Mirror the worker's set_worker_token, scoped to this request (same fix as
    publish.py / series.py / tts.py)."""
    if not multi_tenant_enabled():
        return
    auth_hdr = request.headers.get("authorization") or ""
    token = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else None
    if token:
        from backend.lib.cloud_auth import get_default_auth
        get_default_auth().set_request_token(token)


def _clear_request_gateway_token() -> None:
    if not multi_tenant_enabled():
        return
    from backend.lib.cloud_auth import get_default_auth
    get_default_auth().set_request_token(None)


def _to_read(s: StudioSession) -> StudioSessionRead:
    def _normalize_message(m: dict) -> dict:
        item = dict(m)
        content = str(item.get("content") or "")
        if item.get("role") == "assistant" and content.startswith("AI 暂时不可用"):
            item["content"] = (
                "云端编导刚才有点不稳定，我已切到本地编导模式。"
                "你可以继续输入方向，我会继续帮你打磨。"
            )
            item.pop("blocked_by_safety", None)
            item.pop("safety_reason", None)
        return item

    return StudioSessionRead(
        id=s.id,
        series_id=s.series_id,
        status=s.status,
        messages=[ChatMessage(**_normalize_message(m)) for m in (s.messages or [])
                  if isinstance(m, dict) and m.get("role") in ("user", "assistant", "system", "tool")],
        spec=s.spec or {},
        created_project_ids=list(s.created_project_ids or []),
        created_batch_run_id=s.created_batch_run_id,
        turn_count=int(s.turn_count or 0),
        created_at=s.created_at,
        finished_at=s.finished_at,
    )


@router.post("/sessions", response_model=StudioSessionRead, status_code=201)
def create_session(req: StudioSessionCreate, request: Request, db: Session = Depends(get_db)):
    s = StudioSession(user_id=owner_value(request), series_id=req.series_id, status="gathering",
                       messages=[], spec={})
    db.add(s)
    db.commit()
    db.refresh(s)

    if req.initial_message and req.initial_message.strip():
        # Run one agent turn synchronously so first response is ready
        from backend.services.studio_agent import StudioAgent
        _set_request_gateway_token(request)
        try:
            StudioAgent().step(db, s, req.initial_message.strip())
        finally:
            _clear_request_gateway_token()
        db.refresh(s)
    return _to_read(s)


@router.get("/sessions/{session_id}", response_model=StudioSessionRead)
def get_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    s = owner_filter(db.query(StudioSession).filter(StudioSession.id == session_id), StudioSession, request).first()
    if not s:
        raise HTTPException(404, "session not found")
    return _to_read(s)


@router.get("/sessions", response_model=list[StudioSessionRead])
def list_sessions(
    request: Request,
    series_id: str | None = None,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    q = owner_filter(db.query(StudioSession), StudioSession, request)
    if series_id:
        q = q.filter(StudioSession.series_id == series_id)
    rows = q.order_by(StudioSession.created_at.desc()).limit(limit).all()
    return [_to_read(s) for s in rows]


@router.post("/sessions/{session_id}/messages", response_model=StudioSendMessageResponse)
def send_message(
    session_id: str,
    body: StudioSendMessage,
    request: Request,
    db: Session = Depends(get_db),
):
    s = owner_filter(db.query(StudioSession).filter(StudioSession.id == session_id), StudioSession, request).first()
    if not s:
        raise HTTPException(404, "session not found")
    if s.status in ("submitted", "aborted"):
        raise HTTPException(409, f"session already {s.status}")
    from backend.lib.rate_limit import enforce_rate_limit
    enforce_rate_limit(request, "studio_msg", per_min=30)  # H2:挡脚本狂刷编导对话烧 LLM

    from backend.services.studio_agent import StudioAgent
    _set_request_gateway_token(request)
    try:
        result = StudioAgent().step(db, s, body.content)
    finally:
        _clear_request_gateway_token()
    db.refresh(s)
    last = (s.messages or [])[-1] if s.messages else {
        "role": "assistant", "content": "", "ts": datetime.now(timezone.utc).isoformat(),
    }
    return StudioSendMessageResponse(
        session=_to_read(s),
        last_message=ChatMessage(**{
            k: v for k, v in last.items()
            if k in ("role", "content", "ts", "tool_calls", "tool_results")
        }),
        blocked_by_safety=bool(result.get("blocked")),
        safety_reason=result.get("safety_reason"),
    )


@router.post("/sessions/{session_id}/abort", status_code=204)
def abort_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    s = owner_filter(db.query(StudioSession).filter(StudioSession.id == session_id), StudioSession, request).first()
    if not s:
        raise HTTPException(404, "session not found")
    s.status = "aborted"
    s.finished_at = datetime.now(timezone.utc)
    db.commit()
