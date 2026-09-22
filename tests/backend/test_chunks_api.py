"""Phase 2.7d — Chunks list + replace endpoint tests."""
import uuid

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


def _seed_project_with_chunks(client) -> tuple[str, str]:
    """Create a Project + one Chunk + one Shot. Returns (project_id, chunk_id)."""
    r = client.post("/api/projects/", json={
        "name": f"timeline-test {uuid.uuid4().hex[:6]}",
        "mode": "script",
        "script_text": "the only sentence",
    })
    project_id = r.json()["id"]

    from backend.database import SessionLocal
    from backend.models.chunk import Chunk, Shot

    db = SessionLocal()
    try:
        chunk = Chunk(
            id=str(uuid.uuid4()),
            project_id=project_id,
            chunk_index=0,
            sentence="the only sentence",
            shot_type="atmosphere",
            target_shot_count=1,
            status="approved",
            mm_critic_avg=8.5,
            text_critic_avg=7.5,
        )
        db.add(chunk)
        db.flush()
        shot = Shot(
            id=str(uuid.uuid4()),
            chunk_id=chunk.id,
            shot_index=0,
            query="cinematic atmosphere shot",
            text_critic_score=7.5,
            mm_critic_score=8.5,
            selected_source="pexels",
            selected_source_id="px-123",
            selected_source_url="https://example/x",
            selected_local_path="/tmp/clip.mp4",
            status="approved",
        )
        db.add(shot)
        db.commit()
        return project_id, chunk.id
    finally:
        db.close()


def test_chunks_endpoint_returns_chunks_with_shots(client):
    pid, cid = _seed_project_with_chunks(client)
    r = client.get(f"/api/projects/{pid}/chunks")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    chunk = body[0]
    assert chunk["id"] == cid
    assert chunk["chunk_index"] == 0
    assert chunk["sentence"] == "the only sentence"
    assert chunk["mm_critic_avg"] == 8.5
    assert chunk["shot_type"] == "atmosphere"
    assert chunk["is_hero_shot"] is False
    assert len(chunk["shots"]) == 1
    shot = chunk["shots"][0]
    assert shot["shot_index"] == 0
    assert shot["query"] == "cinematic atmosphere shot"
    assert shot["selected_source"] == "pexels"


def test_chunks_endpoint_404_unknown_project(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}/chunks")
    assert r.status_code == 404


def test_replace_chunk_resets_state_and_stores_override(client):
    pid, cid = _seed_project_with_chunks(client)
    r = client.post(f"/api/projects/{pid}/chunks/{cid}/replace", json={
        "prompt": "modern coffee shop interior, sunlight",
        "strategy": "stock",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["chunk_id"] == cid

    # Verify state reset + override stored
    from backend.database import SessionLocal
    from backend.models.chunk import Chunk
    db = SessionLocal()
    try:
        c = db.query(Chunk).filter(Chunk.id == cid).first()
        assert c.status == "pending"
        assert c.mm_critic_avg is None
        assert c.text_critic_avg is None
        assert c.retry_count == 0
        assert c.extra["override_query"] == "modern coffee shop interior, sunlight"
        assert c.extra["replace_strategy"] == "stock"
        # Shots also reset
        for s in c.shots:
            assert s.status == "pending"
            assert s.mm_critic_score is None
            assert s.selected_local_path is None
    finally:
        db.close()


def test_replace_chunk_404_unknown_chunk(client):
    pid, _ = _seed_project_with_chunks(client)
    r = client.post(f"/api/projects/{pid}/chunks/{uuid.uuid4()}/replace", json={})
    assert r.status_code == 404


def test_replace_without_prompt_only_resets_state(client):
    pid, cid = _seed_project_with_chunks(client)
    r = client.post(f"/api/projects/{pid}/chunks/{cid}/replace", json={
        "strategy": "auto",
    })
    assert r.status_code == 200

    from backend.database import SessionLocal
    from backend.models.chunk import Chunk
    db = SessionLocal()
    try:
        c = db.query(Chunk).filter(Chunk.id == cid).first()
        assert c.status == "pending"
        assert "override_query" not in (c.extra or {})  # nothing stored
        assert c.extra["replace_strategy"] == "auto"
    finally:
        db.close()
