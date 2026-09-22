from datetime import datetime, timezone

def test_project_create_validates_mode():
    from backend.schemas.project import ProjectCreate
    import pytest
    with pytest.raises(Exception):
        ProjectCreate(name="Test", mode="invalid_mode")


def test_project_read_from_attributes():
    from backend.schemas.project import ProjectRead
    class FakeProject:
        id = "abc-123"
        name = "Test"
        mode = "script"
        status = "pending"
        prompt = None
        script_text = "Hello"
        output_format = "youtube_landscape"
        tts_provider = "edge_xiaoxiao"
        current_stage = None
        output_path = None
        celery_task_id = None
        created_at = datetime.now(timezone.utc)
        updated_at = None
    r = ProjectRead.model_validate(FakeProject())
    assert r.id == "abc-123"

def test_pipeline_status_defaults():
    from backend.schemas.pipeline import PipelineStatus
    ps = PipelineStatus(project_id="x", overall_status="pending")
    assert ps.stages == []
    assert ps.current_stage is None
