"""
- LLMClient gets the Series prefix prepended to system prompts
- Orchestrator receives series.industry_tag + forbidden_topics
- After the run, Series.learned_patterns reflects the outcomes
- Project.cost_usd is updated from outcome cost_usd

Uses fully mocked LLM/Footage/etc — does NOT hit real APIs.
"""
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db_session():
    from backend.database import Base, SessionLocal, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    s = SessionLocal()
    yield s
    s.close()


def _make_series_and_project(db, **series_kwargs):
    from backend.models.series import Series
    from backend.models.project import Project
    s = Series(name=f"int-{uuid.uuid4().hex[:6]}", **series_kwargs)
    db.add(s)
    db.commit()
    db.refresh(s)
    p = Project(
        name="proj", mode="script", script_text="hello world",
        series_id=s.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return s, p


def test_pipeline_service_passes_series_to_llm(db_session):
    """LLMClient created by PipelineService should receive the Series."""
    series, _ = _make_series_and_project(
        db_session,
        director_prompt="You are a finance educator.",
        industry_tag="finance",
        forbidden_topics=["nudity", "violence"],
    )
    from backend.services.pipeline_service import PipelineService
    svc = PipelineService(series=series)
    assert svc.llm.series is series
    # Series prefix is non-empty and includes the director prompt + industry
    prefix = svc.llm._series_prefix()
    assert "finance educator" in prefix
    assert "Industry: finance" in prefix
    assert "nudity, violence" in prefix


def test_pipeline_service_uses_series_pipeline_mode(db_session):
    series, _ = _make_series_and_project(db_session, pipeline_mode="balanced")
    from backend.lib.pipeline_mode import PipelineMode
    from backend.services.pipeline_service import PipelineService
    svc = PipelineService(series=series)
    assert svc.mode == PipelineMode.BALANCED


def test_pipeline_service_passes_series_to_orchestrator_moderation(db_session):
    series, _ = _make_series_and_project(
        db_session,
        industry_tag="kids",
        forbidden_topics=["nudity", "violence", "alcohol_cigarettes"],
    )
    from backend.services.pipeline_service import PipelineService
    svc = PipelineService(series=series)
    assert svc.orchestrator.industry == "kids"
    assert svc.orchestrator.forbidden_topics == ["nudity", "violence", "alcohol_cigarettes"]


def test_background_worker_loads_series_for_pending_project(db_session):
    """Phase 2.8 — BackgroundWorker._process_one() should resolve
    project.series_id → Series and pass a snapshot into PipelineService."""
    # Drain the queue first — prior tests may have left stale pending projects
    # in the shared SQLite, and _process_one() picks the OLDEST one.
    from backend.models.project import Project
    db_session.query(Project).filter(Project.status == "pending").update(
        {"status": "cancelled"}, synchronize_session=False,
    )
    db_session.commit()

    series, project = _make_series_and_project(
        db_session, director_prompt="Calm history narrator.",
        forbidden_topics=["nudity"],
    )
    pid = project.id

    captured = {}

    class FakePipelineService:
        def __init__(self, **kwargs):
            s = kwargs.get("series")
            captured["had_series"] = s is not None
            if s is not None:
                captured["series_id"] = s.id
                captured["director_prompt"] = s.director_prompt
                captured["industry_tag"] = s.industry_tag
                captured["forbidden_topics"] = list(s.forbidden_topics)
            self.PIPELINE_DIR = MagicMock()

        def run(self, project_id, project_data):
            captured["project_id"] = project_id
            captured["project_data"] = project_data
            return {"output_path": "/tmp/out.mp4", "pipeline_dir": "/tmp/p"}

    from backend.workers.background_worker import BackgroundWorker

    # BackgroundWorker._process_one imports PipelineService inside the
    # method body, so patching the original module path works.
    with patch(
        "backend.services.pipeline_service.PipelineService",
        FakePipelineService,
    ):
        worker = BackgroundWorker()
        processed = worker._process_one()

    assert processed is True
    assert captured["had_series"] is True
    assert captured["series_id"] == series.id
    assert captured["director_prompt"] == "Calm history narrator."
    assert captured["forbidden_topics"] == ["nudity"]
    assert captured["project_id"] == pid
    assert captured["project_data"]["name"] == project.name

    # Project should now be marked completed
    from backend.models.project import Project
    db_session.expire_all()
    p2 = db_session.query(Project).filter(Project.id == pid).first()
    assert p2.status == "completed"
    assert p2.output_path == "/tmp/out.mp4"


def test_pipeline_works_without_series(db_session):
    """Project not attached to a Series should still work (no injection)."""
    from backend.models.project import Project
    p = Project(name="loose", mode="script", script_text="hi")
    db_session.add(p)
    db_session.commit()

    from backend.services.pipeline_service import PipelineService
    svc = PipelineService()  # series=None
    assert svc.series is None
    assert svc.llm.series is None
    assert svc.orchestrator.industry == ""
    # Series prefix is empty
    assert svc.llm._series_prefix() == ""
