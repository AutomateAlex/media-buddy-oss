"""Phase 2.8 — In-process BackgroundWorker (replaces Celery + Redis on desktop).

A single daemon thread drains pending Projects from SQLite one at a time.
Within each Project, chunk-level parallelism is unchanged (Orchestrator's
ThreadPoolExecutor still does its 9-chunks-at-once thing). What we removed
is the OUTER process-level concurrency that Celery+Redis provided.

Why serial across Projects (not parallel)?
- Customer machines can't reliably handle 4 concurrent video pipelines
  (CPU + memory + external API rate limits)
- Sequential makes cost-cap accounting trivial: check between projects
- "Computer left on overnight" UX → batch of 100 works as expected

Crash recovery: on uvicorn shutdown / power loss, in-flight projects are left
with status='running'. On next startup, recover_orphaned_running_projects()
flips them back to pending so the worker resumes (PipelineService has
checkpoint resume via lib/progress.py).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from backend.database import SessionLocal
from backend.models.batch_run import BatchRun
from backend.models.project import Project
from backend.models.series import Series

logger = logging.getLogger(__name__)


class BackgroundWorker:
    """Single daemon thread. One project at a time. SQLite as the queue.

    Lifecycle is owned by FastAPI lifespan (main.py). External code
    interacts only via .start() / .stop() / .notify_new_work().
    """

    POLL_INTERVAL = 5.0   # seconds when nothing pending
    CRASH_BACKOFF = 1.0   # seconds when an iteration raised
    TAGGING_IDLE_GRACE_SECONDS = 120.0

    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_pipeline_finished_at = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="bg-worker",
            )
            self._thread.start()
            logger.info("BackgroundWorker started")

    def stop(self, timeout: float = 30.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("BackgroundWorker did not stop within %.1fs", timeout)
            else:
                logger.info("BackgroundWorker stopped")

    def notify_new_work(self) -> None:
        """Wake the loop immediately. Called by API endpoints after creating
        pending Projects. Spamming it is harmless (idempotent set)."""
        self._wake_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Pipelines win over tagging — drain pending Projects first.
                if self._process_one():
                    self._last_pipeline_finished_at = time.monotonic()
                    continue
                # Idle path: opportunistic library tagging.
                if self._process_pending_tag():
                    continue
                self._wake_event.wait(timeout=self.POLL_INTERVAL)
                self._wake_event.clear()
            except Exception:
                logger.exception("BackgroundWorker iteration crashed")
                time.sleep(self.CRASH_BACKOFF)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _process_pending_tag(self) -> bool:
        """Tag one pending LibraryClip if available + an Omni provider is
        configured + we're under the soft budget cap. Returns True iff a
        clip was claimed (regardless of tagging success).

        Phase 2.11d — yield to pending Projects: if a Project is waiting,
        skip tagging this cycle so the user's video gets priority. Each tag
        call costs 10-60s (ffmpeg keyframe + Qwen-VL), and a backlog of 25+
        clips would stall the user's pipeline for 5-25 minutes.
        """
        import os

        auto_tagging = (os.environ.get("LIBRARY_AUTO_TAGGING") or "").strip().lower()
        if auto_tagging not in ("1", "true", "yes", "on"):
            return False

        try:
            from backend.services.library_service import LibraryService
            from backend.lib.omni_client import get_omni_client
        except Exception:
            return False

        if (
            self._last_pipeline_finished_at
            and time.monotonic() - self._last_pipeline_finished_at < self.TAGGING_IDLE_GRACE_SECONDS
        ):
            return False

        # Project priority guard — bail out if anything is pending so the
        # main loop's _process_one handles it next iteration.
        try:
            from backend.database import SessionLocal
            from backend.models.project import Project
            db_check = SessionLocal()
            try:
                has_active_video_work = (
                    db_check.query(Project)
                    .filter(Project.status.in_(("pending", "running")))
                    .first() is not None
                )
            finally:
                db_check.close()
            if has_active_video_work:
                return False
        except Exception:
            pass

        if os.environ.get("MEDIA_BUDDY_ENABLE_LOCAL_LIBRARY_MAINTENANCE", "0") != "1":
            return False

        library = LibraryService()

        # Soft budget cap to prevent runaway costs (e.g. 5000 unindexed clips).
        budget = float(os.environ.get("LIBRARY_TAGGING_BUDGET_USD", "10.0") or 10.0)
        stats = library.get_stats()
        if stats.get("tagging_cost_usd", 0.0) >= budget:
            return False  # paused; manual resume = bump env or zero cost manually

        omni = get_omni_client()
        if not omni.is_available():
            logger.info("library tagging paused: no omni provider configured")
            return False

        clip_id = library.claim_next_pending()
        if clip_id is None:
            return False

        try:
            clip = library.get(clip_id)
            if clip is None:
                return True
            result = omni.tag_clip(clip.local_path)
            library.apply_tagging_result(
                clip_id, result, provider=omni.name, cost_usd=0.01,
            )
            logger.info(
                "tagged clip %s → category=%s tags=%s",
                clip_id, result.get("category"),
                (result.get("tags") or [])[:5],
            )
        except Exception as e:
            logger.warning("Tagging failed for clip %s: %s", clip_id, e)
            try:
                library.mark_failed(clip_id, str(e))
            except Exception:
                pass
        return True

    def _process_one(self) -> bool:
        """Pick + run the oldest pending Project. Returns True if one was
        found (regardless of pipeline success/failure)."""
        # Step 1: snapshot what to do (release session before long pipeline)
        plan = self._claim_next_pending()
        if plan is None:
            return False

        project_id, project_data, series_snapshot, batch_run_id = plan

        # Step 2: run pipeline synchronously (long-blocking, no DB session held)
        try:
            from backend.services.pipeline_service import PipelineService
            from backend.services.pipeline_service import PipelineAuthRequired
            from backend.lib.cloud_auth import CloudTemporaryUnavailableError
            svc = PipelineService(
                tts_provider=project_data.get("tts_provider") or "azure_yunyang",
                series=series_snapshot,
            )
            result = svc.run(project_id, project_data)
            self._mark_completed(project_id, result, batch_run_id)
        except PipelineAuthRequired as exc:
            logger.warning("Pipeline paused for login refresh on project %s: %s", project_id, exc)
            self._mark_auth_required(project_id, str(exc), batch_run_id)
        except CloudTemporaryUnavailableError as exc:
            logger.warning("Pipeline will retry after temporary cloud issue on project %s: %s", project_id, exc)
            self._mark_retry_later(project_id, str(exc), batch_run_id)
        except Exception as exc:
            logger.exception("Pipeline failed for project %s", project_id)
            self._mark_failed(project_id, str(exc), batch_run_id)
        return True

    # ------------------------------------------------------------------
    # DB helpers (each opens + closes its own session)
    # ------------------------------------------------------------------
    def _claim_next_pending(self):
        """Find the oldest pending Project, mark it running, and return a
        snapshot of fields needed for the pipeline. Also enforces cost cap
        for batch projects: caps a batch right here before its next project
        runs.

        Returns a tuple (project_id, project_data_dict, series_orm_or_None,
        batch_run_id_or_None) or None if nothing pending.
        """
        db = SessionLocal()
        try:
            # Cost-cap sweep: any batch whose cumulative cost already exceeds
            # its cap, flip it to aborted_cost_cap and cancel its pendings.
            self._enforce_cost_caps(db)

            project = (
                db.query(Project)
                .filter(Project.status == "pending")
                .order_by(Project.created_at.asc())
                .first()
            )
            if project is None:
                return None

            # Detach Series from session by snapshotting the fields we need.
            series_obj = None
            if project.series_id:
                series_orm = db.query(Series).filter(Series.id == project.series_id).first()
                if series_orm is not None:
                    series_obj = _SeriesSnapshot(series_orm)

            project.status = "running"
            db.commit()

            return (
                project.id,
                {
                    "name": project.name,
                    "mode": project.mode,
                    "script_text": project.script_text,
                    "prompt": project.prompt,
                    "output_format": project.output_format,
                    "duration_seconds": project.duration_seconds,
                    "tts_provider": project.tts_provider,
                    "tts_speed": float(project.tts_speed or 1.0),
                    "include_subtitles": bool(project.include_subtitles),
                    "output_language": getattr(project, "output_language", None) or "zh",
                    "subtitle_language": getattr(project, "subtitle_language", None),
                    "subtitle_font_scale": float(getattr(project, "subtitle_font_scale", None) or 1.0),
                    "verbatim": bool(getattr(project, "verbatim", None) or False),
                },
                series_obj,
                project.batch_run_id,
            )
        finally:
            db.close()

    def _enforce_cost_caps(self, db) -> None:
        """For each running BatchRun, if total_cost_usd >= daily_cost_cap_usd,
        mark batch aborted_cost_cap and cancel its remaining pending projects."""
        running_batches = (
            db.query(BatchRun).filter(BatchRun.status.in_(("pending", "running"))).all()
        )
        for batch in running_batches:
            if (batch.total_cost_usd or 0.0) < (batch.daily_cost_cap_usd or 0.0):
                continue
            logger.warning(
                "BatchRun %s hit cost cap ($%.2f / $%.2f) — aborting",
                batch.id, batch.total_cost_usd or 0.0,
                batch.daily_cost_cap_usd or 0.0,
            )
            batch.status = "aborted_cost_cap"
            batch.abort_reason = (
                f"cost cap hit at ${(batch.total_cost_usd or 0.0):.2f} "
                f"of ${(batch.daily_cost_cap_usd or 0.0):.2f}"
            )
            db.query(Project).filter(
                Project.batch_run_id == batch.id,
                Project.status == "pending",
            ).update({"status": "cancelled"}, synchronize_session=False)
        db.commit()

    def _mark_completed(self, project_id: str, result: dict, batch_run_id: Optional[str]) -> None:
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == project_id).first()
            if p is None:
                return
            if p.status == "stopped":
                logger.info("Project %s completed after stop request; preserving stopped status", project_id)
                return
            p.status = "completed"
            p.output_path = result.get("output_path")
            p.pipeline_dir = result.get("pipeline_dir")
            db.commit()
            if batch_run_id:
                self._refresh_batch_aggregates(db, batch_run_id)
        finally:
            db.close()

    def _mark_failed(self, project_id: str, error: str, batch_run_id: Optional[str]) -> None:
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == project_id).first()
            if p is None:
                return
            if p.status == "stopped":
                logger.info("Project %s failed after stop request; preserving stopped status", project_id)
                return
            p.status = "failed"
            db.commit()
            if batch_run_id:
                self._refresh_batch_aggregates(db, batch_run_id)
        finally:
            db.close()

    def _mark_retry_later(self, project_id: str, error: str, batch_run_id: Optional[str]) -> None:
        retry_delay = float(os.environ.get("MEDIA_BUDDY_CLOUD_RETRY_DELAY_SECONDS", "30") or 30)
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == project_id).first()
            if p is None:
                return
            if p.status == "stopped":
                logger.info("Project %s hit cloud retry after stop request; preserving stopped status", project_id)
                return
            p.status = "pending"
            # Keep current_stage so the UI can say what is waiting. The next
            # run resumes from progress checkpoints and retries only unfinished
            # work instead of burning a whole new video.
            db.commit()
            if batch_run_id:
                self._refresh_batch_aggregates(db, batch_run_id)
        finally:
            db.close()
        # Avoid a hot retry loop when the user is offline or the cloud is
        # briefly unhealthy. Sleep after closing the DB session.
        if retry_delay > 0:
            time.sleep(retry_delay)

    def _mark_auth_required(self, project_id: str, error: str, batch_run_id: Optional[str]) -> None:
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == project_id).first()
            if p is None:
                return
            if p.status == "stopped":
                logger.info("Project %s requested auth after stop request; preserving stopped status", project_id)
                return
            p.status = "paused_auth_required"
            # current_stage was written by PipelineService as the exact stage
            # that needs login before continuing.
            db.commit()
            if batch_run_id:
                self._refresh_batch_aggregates(db, batch_run_id)
        finally:
            db.close()

    def _refresh_batch_aggregates(self, db, batch_run_id: str) -> None:
        from datetime import datetime, timezone
        batch = db.query(BatchRun).filter(BatchRun.id == batch_run_id).first()
        if batch is None:
            return
        children = db.query(Project).filter(Project.batch_run_id == batch_run_id).all()
        batch.completed_count = sum(1 for c in children if c.status == "completed")
        batch.failed_count    = sum(1 for c in children if c.status == "failed")
        batch.total_cost_usd  = float(sum((c.cost_usd or 0.0) for c in children))
        # Finalize when nothing is in flight anymore
        in_flight = sum(
            1 for c in children
            if c.status in ("pending", "running", "paused_auth_required")
        )
        if in_flight == 0 and batch.status not in ("aborted_cost_cap", "stopped"):
            if batch.failed_count == 0:
                batch.status = "completed"
            elif batch.completed_count == 0:
                batch.status = "failed"
            else:
                batch.status = "partial"
            batch.finished_at = datetime.now(timezone.utc)
        db.commit()


class _SeriesSnapshot:
    """Detached snapshot of a Series row — provides the duck-typed surface
    LLMClient + Orchestrator expect, but doesn't hold a DB session.

    LLMClient reads: director_prompt, industry_tag, forbidden_topics,
                      learned_patterns, pipeline_mode
    Orchestrator reads: industry_tag, forbidden_topics, pipeline_mode
    """
    __slots__ = (
        "id", "name", "director_prompt", "industry_tag", "forbidden_topics",
        "learned_patterns", "pipeline_mode",
    )

    def __init__(self, orm):
        self.id = orm.id
        self.name = orm.name
        self.director_prompt = orm.director_prompt
        self.industry_tag = orm.industry_tag
        self.forbidden_topics = list(orm.forbidden_topics or [])
        self.learned_patterns = dict(orm.learned_patterns or {})
        self.pipeline_mode = orm.pipeline_mode


# Module-level singleton — uvicorn lifespan owns its lifecycle.
_singleton: Optional[BackgroundWorker] = None


def get_background_worker() -> BackgroundWorker:
    global _singleton
    if _singleton is None:
        _singleton = BackgroundWorker()
    return _singleton


def reset_background_worker_for_tests() -> None:
    """Test-only: drop the singleton so the next get_background_worker()
    creates a fresh instance. Never call this from production code."""
    global _singleton
    if _singleton is not None and _singleton.is_running():
        _singleton.stop(timeout=5.0)
    _singleton = None
