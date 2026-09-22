"""Phase 2.8 — BackgroundWorker tests.

Cover:
  - Sequential drain (one project at a time)
  - Cost cap halts a batch + cancels its remaining pending projects
  - Orphan recovery flips running → pending on startup
  - notify_new_work() wakes the loop
  - Pipeline failure marks project failed but loop survives
"""
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db():
    from backend.database import Base, SessionLocal, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    s = SessionLocal()
    yield s
    s.close()


def _drain_queue(db):
    """Mark all pending projects cancelled so a fresh test starts clean."""
    from backend.models.project import Project
    db.query(Project).filter(Project.status == "pending").update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    db.commit()


def _make_series(db, **kwargs):
    from backend.models.series import Series
    s = Series(name=f"S {uuid.uuid4().hex[:6]}", **kwargs)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_pending_project(db, name="p", series_id=None, batch_run_id=None, cost_usd=0.0):
    from backend.models.project import Project
    p = Project(
        name=name, mode="script", script_text="x",
        series_id=series_id, batch_run_id=batch_run_id, cost_usd=cost_usd,
        status="pending",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_batch(db, series_id, cap=200.0, total_cost=0.0):
    from backend.models.batch_run import BatchRun
    b = BatchRun(
        series_id=series_id, status="running",
        requested_count=0, daily_cost_cap_usd=cap, total_cost_usd=total_cost,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def fake_pipeline():
    """Patch PipelineService to a synchronous stub that records what it ran."""
    calls = []

    class FakePipelineService:
        def __init__(self, **kwargs):
            self._kwargs = kwargs

        def run(self, project_id, project_data):
            calls.append({
                "project_id": project_id,
                "had_series": self._kwargs.get("series") is not None,
                "tts_provider": self._kwargs.get("tts_provider"),
            })
            return {
                "output_path": f"/tmp/{project_id}.mp4",
                "pipeline_dir": f"/tmp/{project_id}",
            }

    with patch("backend.services.pipeline_service.PipelineService", FakePipelineService):
        yield calls


# ------------------------------------------------------------------
# _process_one — basic drain + status transitions
# ------------------------------------------------------------------

def test_process_one_returns_false_when_nothing_pending(db, fake_pipeline):
    _drain_queue(db)
    from backend.workers.background_worker import BackgroundWorker
    assert BackgroundWorker()._process_one() is False
    assert fake_pipeline == []


def test_process_one_runs_oldest_pending_project(db, fake_pipeline):
    _drain_queue(db)
    p1 = _make_pending_project(db, name="first")
    time.sleep(0.01)  # ensure differing created_at
    p2 = _make_pending_project(db, name="second")

    from backend.workers.background_worker import BackgroundWorker
    worker = BackgroundWorker()
    assert worker._process_one() is True

    # Only the oldest should have been processed
    assert len(fake_pipeline) == 1
    assert fake_pipeline[0]["project_id"] == p1.id

    # p1 → completed, p2 still pending
    db.expire_all()
    from backend.models.project import Project
    assert db.query(Project).filter(Project.id == p1.id).first().status == "completed"
    assert db.query(Project).filter(Project.id == p2.id).first().status == "pending"


def test_process_one_marks_failed_when_pipeline_raises(db):
    _drain_queue(db)
    p = _make_pending_project(db, name="will-fail")

    class FailingPipelineService:
        def __init__(self, **k): pass
        def run(self, *_, **__): raise RuntimeError("boom")

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.pipeline_service.PipelineService", FailingPipelineService):
        BackgroundWorker()._process_one()

    db.expire_all()
    from backend.models.project import Project
    assert db.query(Project).filter(Project.id == p.id).first().status == "failed"


def test_process_one_pauses_when_auth_required(db):
    _drain_queue(db)
    p = _make_pending_project(db, name="needs-login")

    from backend.services.pipeline_service import PipelineAuthRequired

    class AuthRequiredPipelineService:
        def __init__(self, **k): pass
        def run(self, *_, **__): raise PipelineAuthRequired("cloud not authenticated")

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.pipeline_service.PipelineService", AuthRequiredPipelineService):
        BackgroundWorker()._process_one()

    db.expire_all()
    from backend.models.project import Project
    assert db.query(Project).filter(Project.id == p.id).first().status == "paused_auth_required"


def test_process_one_requeues_when_cloud_temporarily_unavailable(db, monkeypatch):
    _drain_queue(db)
    p = _make_pending_project(db, name="cloud-blip")
    monkeypatch.setenv("MEDIA_BUDDY_CLOUD_RETRY_DELAY_SECONDS", "0")

    from backend.lib.cloud_auth import CloudTemporaryUnavailableError

    class TemporaryCloudPipelineService:
        def __init__(self, **k): pass
        def run(self, *_, **__): raise CloudTemporaryUnavailableError("timeout")

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.pipeline_service.PipelineService", TemporaryCloudPipelineService):
        BackgroundWorker()._process_one()

    db.expire_all()
    from backend.models.project import Project
    assert db.query(Project).filter(Project.id == p.id).first().status == "pending"


def test_process_one_preserves_stopped_when_pipeline_finishes_after_stop(db):
    _drain_queue(db)
    p = _make_pending_project(db, name="will-stop")

    class StopThenFinishPipelineService:
        def __init__(self, **k): pass
        def run(self, project_id, project_data):
            from backend.database import SessionLocal
            from backend.models.project import Project
            s = SessionLocal()
            try:
                project = s.query(Project).filter(Project.id == project_id).first()
                project.status = "stopped"
                s.commit()
            finally:
                s.close()
            return {
                "output_path": f"/tmp/{project_id}.mp4",
                "pipeline_dir": f"/tmp/{project_id}",
            }

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.pipeline_service.PipelineService", StopThenFinishPipelineService):
        BackgroundWorker()._process_one()

    db.expire_all()
    from backend.models.project import Project
    assert db.query(Project).filter(Project.id == p.id).first().status == "stopped"


# ------------------------------------------------------------------
# Idle-time library tagging priority
# ------------------------------------------------------------------

def test_pending_tag_skips_when_project_running(db, monkeypatch):
    from backend.models.project import Project

    monkeypatch.setenv("LIBRARY_AUTO_TAGGING", "1")
    db.query(Project).filter(Project.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    p = Project(name="active-video", mode="script", script_text="x", status="running")
    db.add(p)
    db.commit()

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.library_service.LibraryService") as mock_library:
        assert BackgroundWorker()._process_pending_tag() is False
        mock_library.assert_not_called()


def test_pending_tag_waits_after_pipeline_finish(db, monkeypatch):
    from backend.models.project import Project

    monkeypatch.setenv("LIBRARY_AUTO_TAGGING", "1")
    db.query(Project).filter(Project.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    db.commit()

    from backend.workers.background_worker import BackgroundWorker
    worker = BackgroundWorker()
    worker._last_pipeline_finished_at = time.monotonic()
    with patch("backend.services.library_service.LibraryService") as mock_library:
        assert worker._process_pending_tag() is False
        mock_library.assert_not_called()


def test_pending_tag_disabled_by_default(db):
    from backend.models.project import Project

    db.query(Project).filter(Project.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    db.commit()

    from backend.workers.background_worker import BackgroundWorker
    with patch("backend.services.library_service.LibraryService") as mock_library:
        assert BackgroundWorker()._process_pending_tag() is False
        mock_library.assert_not_called()


# ------------------------------------------------------------------
# Cost cap
# ------------------------------------------------------------------

def test_cost_cap_aborts_batch_and_cancels_remaining_pending(db, fake_pipeline):
    _drain_queue(db)
    series = _make_series(db, daily_cost_cap_usd=10.0)
    # Pretend a batch already overshot its cap (e.g. a prior run incurred cost)
    batch = _make_batch(db, series.id, cap=1.0, total_cost=1.50)
    p1 = _make_pending_project(db, name="b-1", series_id=series.id, batch_run_id=batch.id)
    p2 = _make_pending_project(db, name="b-2", series_id=series.id, batch_run_id=batch.id)

    from backend.workers.background_worker import BackgroundWorker
    worker = BackgroundWorker()
    # Process — should NOT run any pipeline because batch is over cap
    worker._process_one()

    # No fake-pipeline call happened
    assert fake_pipeline == []

    db.expire_all()
    from backend.models.batch_run import BatchRun
    from backend.models.project import Project
    b2 = db.query(BatchRun).filter(BatchRun.id == batch.id).first()
    assert b2.status == "aborted_cost_cap"
    assert "cost cap hit" in (b2.abort_reason or "")
    # Pending children are cancelled
    assert db.query(Project).filter(Project.id == p1.id).first().status == "cancelled"
    assert db.query(Project).filter(Project.id == p2.id).first().status == "cancelled"


def test_cost_cap_does_not_affect_non_batch_projects(db, fake_pipeline):
    _drain_queue(db)
    series = _make_series(db)
    batch = _make_batch(db, series.id, cap=1.0, total_cost=1.50)
    _make_pending_project(db, name="batch-1", series_id=series.id, batch_run_id=batch.id)
    standalone = _make_pending_project(db, name="standalone")  # not in any batch

    from backend.workers.background_worker import BackgroundWorker
    BackgroundWorker()._process_one()

    # The standalone project should have been processed
    assert len(fake_pipeline) == 1
    assert fake_pipeline[0]["project_id"] == standalone.id


# ------------------------------------------------------------------
# Series snapshot
# ------------------------------------------------------------------

def test_series_snapshot_survives_session_close(db, fake_pipeline):
    """The Series snapshot passed to PipelineService must work after the
    DB session that loaded it is closed (the snapshot detaches)."""
    _drain_queue(db)
    series = _make_series(
        db, director_prompt="Calm narrator.", industry_tag="history",
        forbidden_topics=["nudity"],
    )
    _make_pending_project(db, name="np", series_id=series.id)

    from backend.workers.background_worker import BackgroundWorker

    captured = {}

    class CapturingPipelineService:
        def __init__(self, **kwargs):
            s = kwargs.get("series")
            # Read every attribute LLMClient would touch — simulating a
            # cross-session use after the worker thread closed its session.
            captured["director_prompt"] = s.director_prompt
            captured["industry_tag"] = s.industry_tag
            captured["forbidden_topics"] = list(s.forbidden_topics)
            captured["pipeline_mode"] = s.pipeline_mode

        def run(self, *a, **k):
            return {"output_path": "/x", "pipeline_dir": "/x"}

    with patch("backend.services.pipeline_service.PipelineService", CapturingPipelineService):
        BackgroundWorker()._process_one()

    assert captured["director_prompt"] == "Calm narrator."
    assert captured["industry_tag"] == "history"
    assert captured["forbidden_topics"] == ["nudity"]
    assert captured["pipeline_mode"] == "fast"


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------

def test_start_and_stop_lifecycle(fake_pipeline):
    from backend.workers.background_worker import BackgroundWorker
    w = BackgroundWorker()
    assert w.is_running() is False
    w.start()
    assert w.is_running() is True
    # Idempotent: starting again is a no-op
    w.start()
    assert w.is_running() is True
    w.stop(timeout=2.0)
    assert w.is_running() is False


def test_notify_new_work_wakes_idle_loop(db, fake_pipeline):
    """When the loop is sleeping (no pending), a notify_new_work() should
    wake it within well under POLL_INTERVAL seconds."""
    _drain_queue(db)
    from backend.workers.background_worker import BackgroundWorker
    w = BackgroundWorker()
    w.POLL_INTERVAL = 60.0  # ensure we'd otherwise sleep a long time
    w.start()
    try:
        # Give the loop a moment to enter wait()
        time.sleep(0.3)
        # Add a project + notify
        p = _make_pending_project(db, name="late-arrival")
        w.notify_new_work()
        # Within 1.5s the loop should have processed it
        for _ in range(15):
            time.sleep(0.1)
            db.expire_all()
            from backend.models.project import Project
            row = db.query(Project).filter(Project.id == p.id).first()
            if row.status == "completed":
                break
        assert row.status == "completed", f"project still {row.status}"
    finally:
        w.stop(timeout=2.0)


# ------------------------------------------------------------------
# Orphan recovery
# ------------------------------------------------------------------

def _drain_all(db):
    """Cancel every non-terminal Project — both pending AND running — so
    the orphan recovery test starts from a known-clean state."""
    from backend.models.project import Project
    db.query(Project).filter(Project.status.in_(("pending", "running"))).update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    db.commit()


def test_orphan_recovery_resets_running_to_pending(db):
    """Simulates a crash: a project was 'running' when uvicorn died.
    recover_orphaned_running_projects() flips it back to 'pending'."""
    _drain_all(db)
    from backend.models.project import Project
    p = Project(name="orphan", mode="script", script_text="x", status="running")
    db.add(p)
    db.commit()
    db.refresh(p)

    from backend.workers.startup_recovery import recover_orphaned_running_projects
    n = recover_orphaned_running_projects()
    assert n == 1

    db.expire_all()
    p2 = db.query(Project).filter(Project.id == p.id).first()
    assert p2.status == "pending"


def test_orphan_recovery_with_no_orphans_is_noop(db):
    _drain_all(db)
    from backend.workers.startup_recovery import recover_orphaned_running_projects
    assert recover_orphaned_running_projects() == 0
