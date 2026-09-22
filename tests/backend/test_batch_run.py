"""
BackgroundWorker (no more Celery). The dispatcher is now just
notify_new_work() on the worker singleton, which we patch."""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from backend.database import Base, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    from backend.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def stub_worker():
    """Don't actually start the BG worker thread during these unit tests —
    we only verify the API contract + that notify_new_work() was called."""
    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work") as mock_notify:
        yield mock_notify


def _make_series(client, **overrides) -> str:
    body = {"name": f"S {uuid.uuid4().hex[:6]}", "daily_video_cap": 50}
    body.update(overrides)
    r = client.post("/api/series/", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_batch_creates_projects_and_notifies(client, stub_worker):
    sid = _make_series(client)
    r = client.post(f"/api/series/{sid}/batch", json={
        "scripts": ["script one", "script two", "script three"],
        "concurrency": 2,
        "daily_cost_cap_usd": 50.0,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["series_id"] == sid
    assert body["requested_count"] == 3
    assert body["concurrency"] == 2
    assert body["daily_cost_cap_usd"] == 50.0
    # 2.8 — batch is "running" the moment we enqueue
    assert body["status"] == "running"
    bid = body["id"]

    # The worker should have been notified exactly once
    assert stub_worker.called

    # Children should be created with mode/script + linked AND status=pending
    r = client.get(f"/api/batch/{bid}/projects")
    assert r.status_code == 200
    children = r.json()
    assert len(children) == 3
    assert all(p["series_id"] == sid for p in children)
    assert {p["mode"] for p in children} == {"script"}
    assert all(p["status"] == "pending" for p in children)


def test_create_batch_caps_at_series_daily_cost_cap(client):
    sid = _make_series(client, daily_cost_cap_usd=30.0)
    r = client.post(f"/api/series/{sid}/batch", json={
        "scripts": ["a", "b"],
        "daily_cost_cap_usd": 100.0,  # higher than series 30 → series wins
    })
    assert r.json()["daily_cost_cap_usd"] == 30.0


def test_batch_rejects_oversize(client):
    sid = _make_series(client, daily_video_cap=2)
    r = client.post(f"/api/series/{sid}/batch", json={
        "scripts": ["a", "b", "c"],  # 3 > cap 2
    })
    assert r.status_code == 400
    assert "exceeds Series daily_video_cap" in r.text


def test_batch_rejects_empty(client):
    sid = _make_series(client)
    r = client.post(f"/api/series/{sid}/batch", json={"scripts": []})
    assert r.status_code == 400


def test_batch_creative_mode_uses_prompts(client):
    sid = _make_series(client)
    r = client.post(f"/api/series/{sid}/batch", json={
        "prompts": ["video about coffee", "video about tea"],
    })
    assert r.status_code == 201
    bid = r.json()["id"]
    children = client.get(f"/api/batch/{bid}/projects").json()
    assert all(p["mode"] == "creative" for p in children)


def test_get_batch_404_for_unknown_id(client):
    r = client.get(f"/api/batch/{uuid.uuid4()}")
    assert r.status_code == 404


def test_pipeline_run_endpoint_marks_pending_and_notifies(client, stub_worker):
    """POST /api/pipeline/run no longer dispatches a Celery task — it just
    flips Project.status='pending' and notifies the BackgroundWorker."""
    pr = client.post("/api/projects/", json={
        "name": f"single-{uuid.uuid4().hex[:6]}",
        "mode": "script",
        "script_text": "hello world",
    })
    pid = pr.json()["id"]
    r = client.post("/api/pipeline/run", json={"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["status"] == "queued"
    assert stub_worker.called

    # Project should be in pending state
    r2 = client.get(f"/api/projects/{pid}")
    assert r2.json()["status"] == "pending"


def test_pipeline_run_409_when_already_running(client, stub_worker):
    pr = client.post("/api/projects/", json={
        "name": f"busy-{uuid.uuid4().hex[:6]}",
        "mode": "script",
        "script_text": "hello",
    })
    pid = pr.json()["id"]
    # Force project into running state
    from backend.database import SessionLocal
    from backend.models.project import Project
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == pid).first()
        p.status = "running"
        db.commit()
    finally:
        db.close()

    r = client.post("/api/pipeline/run", json={"project_id": pid})
    assert r.status_code == 409


def test_project_restart_clears_old_pipeline_checkpoints(client, stub_worker, tmp_path, monkeypatch):
    pr = client.post("/api/projects/", json={
        "name": f"restart-{uuid.uuid4().hex[:6]}",
        "mode": "script",
        "script_text": "hello world",
    })
    pid = pr.json()["id"]

    import backend.api.projects as projects_api
    from backend.database import SessionLocal
    from backend.models.chunk import Chunk, Shot
    from backend.models.project import Project

    monkeypatch.setattr(projects_api, "PIPELINE_DIR", tmp_path / "pipelines")
    old_dir = projects_api.PIPELINE_DIR / pid
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "compose.json").write_text("old", encoding="utf-8")

    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == pid).first()
        p.status = "completed"
        p.output_path = str(old_dir / "old.mp4")
        p.pipeline_dir = str(old_dir)
        p.cost_usd = 1.23
        chunk = Chunk(id=f"chunk-{pid}", project_id=pid, chunk_index=0, sentence="hello")
        shot = Shot(id=f"shot-{pid}", chunk=chunk, shot_index=0)
        db.add(chunk)
        db.add(shot)
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/projects/{pid}/restart")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["output_path"] is None
    assert not old_dir.exists()
    assert stub_worker.called

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == pid).first()
        assert project.pipeline_dir is None
        assert project.cost_usd == 0.0
        assert db.query(Chunk).filter(Chunk.project_id == pid).count() == 0
        assert db.query(Shot).filter(Shot.id == f"shot-{pid}").count() == 0
    finally:
        db.close()
