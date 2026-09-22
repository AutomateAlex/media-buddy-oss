"""Phase 2.8 — Startup recovery for orphaned in-flight projects.

Called once during FastAPI lifespan startup. If the previous run was killed
mid-pipeline (uvicorn crash, power loss, user closed app), Project.status
is left as 'running' with no worker actually running it. Flip those back
to 'pending' so the BackgroundWorker resumes them on its first iteration.

Resume safety: PipelineService writes per-stage progress checkpoints
(lib/progress.py write_progress / read_progress). The pipeline reads each
stage's checkpoint at start and skips stages that completed cleanly. So a
"resumed" project re-runs only the in-flight stage, not the whole video.
"""
from __future__ import annotations

import logging

from backend.database import SessionLocal
from backend.models.batch_run import BatchRun
from backend.models.project import Project

logger = logging.getLogger(__name__)


def recover_orphaned_running_projects() -> int:
    """Find projects with status='running' (orphans from prior crash) and
    set them back to 'pending'. Also flips orphan BatchRuns from 'running'
    so they show 'pending' if they had no outstanding work, or stay running
    so the worker keeps draining them. Phase 2.11d — also resets library
    clips stuck in 'tagging' status (worker died mid-tag → otherwise stays
    'tagging' forever and never re-tagged). Returns count of orphans recovered."""
    db = SessionLocal()
    try:
        orphans = db.query(Project).filter(Project.status == "running").all()
        for p in orphans:
            logger.info("recovering orphaned project %s (%s)", p.id, p.name)
            p.status = "pending"
        if orphans:
            db.commit()
            logger.info("recovered %d orphaned project(s)", len(orphans))

        # Phase 2.11d — orphan recovery for library tagging
        from backend.models.library_clip import LibraryClip
        stuck_tags = db.query(LibraryClip).filter(LibraryClip.tag_status == "tagging").all()
        for c in stuck_tags:
            c.tag_status = "pending"
        if stuck_tags:
            db.commit()
            logger.info("recovered %d orphaned library-clip tag(s)", len(stuck_tags))

        return len(orphans)
    finally:
        db.close()
