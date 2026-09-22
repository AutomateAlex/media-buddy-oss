import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_project_model_creates_table(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db",
        connect_args={"check_same_thread": False},
    )
    from backend.database import Base
    from backend.models.project import Project
    Base.metadata.create_all(bind=engine)
    with Session(engine) as s:
        p = Project(name="Test", mode="script", script_text="Hello world")
        s.add(p)
        s.commit()
        assert p.id is not None
        assert p.status == "pending"
        assert p.output_format == "youtube_landscape"


def test_project_id_is_uuid_string(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_uuid.db",
        connect_args={"check_same_thread": False},
    )
    from backend.database import Base
    from backend.models.project import Project
    Base.metadata.create_all(bind=engine)
    with Session(engine) as s:
        p = Project(name="UUID Test", mode="creative")
        s.add(p)
        s.commit()
        import uuid
        uuid.UUID(p.id)  # raises ValueError if not valid UUID
