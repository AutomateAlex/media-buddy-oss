"""Diagnostics/log endpoints for the desktop app."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.diagnostics import (
    BACKEND_LOG,
    cloud_snapshot,
    configure_file_logging,
    public_error,
    public_path,
    read_progress_errors,
    tail_log,
)
from backend.lib.user_context import is_admin_request
from backend.models.project import Project

router = APIRouter()


@router.get("/diagnostics")
def diagnostics(
    request: Request,
    lines: int = Query(default=300, ge=20, le=1000),
    db: Session = Depends(get_db),
):
    """Return a redacted diagnostic bundle. ADMIN ONLY — it exposes the backend
    log tail and every tenant's recent projects, so a regular customer must not
    see it (cloud multi-tenant). Desktop single-operator mode = always admin."""
    if not is_admin_request(request):
        raise HTTPException(status_code=403, detail="admin only")
    configure_file_logging()
    recent_projects = (
        db.query(Project)
        .order_by(Project.created_at.desc())
        .limit(20)
        .all()
    )
    failed_projects = [
        {
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "current_stage": p.current_stage,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "errors": {
                stage: public_error(err)
                for stage, err in read_progress_errors(p.id).items()
            },
        }
        for p in recent_projects
        if p.status == "failed"
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "log_path": public_path(str(BACKEND_LOG)),
        "log_lines": tail_log(lines),
        "cloud": cloud_snapshot(),
        "recent_projects": [
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "current_stage": p.current_stage,
                "output_path": public_path(p.output_path),
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in recent_projects
        ],
        "failed_projects": failed_projects,
    }
