"""Phase 2.7a — Series ORM + Project FK migration tests."""
import uuid

import pytest


@pytest.fixture
def db_session():
    """Shared backend.database engine (matches test_moderation.py pattern)."""
    from backend.database import Base, SessionLocal, engine
    import backend.models  # noqa: F401 — registers Series + Project + others
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    s = SessionLocal()
    yield s
    s.close()


def test_series_persists_with_defaults(db_session):
    from backend.models.series import Series
    s = Series(name=f"WW2 Stories {uuid.uuid4().hex[:6]}", industry_tag="history")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.id is not None
    assert s.output_format == "youtube_landscape"
    assert s.pipeline_mode == "fast"
    assert s.bgm_enabled is True
    assert s.nsfw_threshold == "strict"
    assert s.daily_video_cap == 100
    assert s.daily_cost_cap_usd == 200.0
    assert s.industry_tag == "history"


def test_series_stores_jsons_and_director_prompt(db_session):
    from backend.models.series import Series
    s = Series(
        name=f"Finance Daily {uuid.uuid4().hex[:6]}",
        industry_tag="finance",
        director_prompt="You are a finance educator. Calm, data-driven tone.",
        forbidden_topics=["nudity", "violence", "weapon_glorify"],
        learned_patterns={"preferred_hooks": ["data", "question"], "bad_sources": ["wikimedia"]},
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.forbidden_topics == ["nudity", "violence", "weapon_glorify"]
    assert s.learned_patterns["preferred_hooks"] == ["data", "question"]
    assert s.director_prompt.startswith("You are a finance")


def test_project_can_be_attached_to_series(db_session):
    from backend.models.series import Series
    from backend.models.project import Project

    s = Series(name=f"Beauty Trends {uuid.uuid4().hex[:6]}", industry_tag="beauty")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)

    p = Project(
        name=f"Lipstick reel {uuid.uuid4().hex[:6]}",
        mode="creative",
        prompt="Trending matte lipstick demo",
        series_id=s.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    assert p.series_id == s.id

    # Series-less projects still allowed (nullable FK)
    p2 = Project(name=f"One-off {uuid.uuid4().hex[:6]}", mode="script", script_text="hi")
    db_session.add(p2)
    db_session.commit()
    db_session.refresh(p2)
    assert p2.series_id is None


def test_legacy_campaign_id_backfills_to_series_id():
    """Simulate a legacy DB that has campaign_id rows but no series_id column.
    apply_lightweight_migrations() should add series_id and copy values over."""
    from sqlalchemy import create_engine, text
    import backend.models

    eng = create_engine("sqlite:///:memory:")

    # Build a legacy schema (projects without series_id) directly
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projects (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                mode VARCHAR NOT NULL,
                campaign_id VARCHAR
            )
        """))
        conn.execute(text(
            "INSERT INTO projects (id, name, mode, campaign_id) "
            "VALUES ('p1', 'old', 'script', 'camp-legacy-42')"
        ))

    # Run the migration against this fresh engine
    backend.models.apply_lightweight_migrations(engine=eng)

    with eng.begin() as conn:
        row = conn.execute(text(
            "SELECT campaign_id, series_id FROM projects WHERE name='old'"
        )).fetchone()
    assert row is not None
    assert row[0] == "camp-legacy-42"
    assert row[1] == "camp-legacy-42"


def test_legacy_migration_is_idempotent():
    """Running the migration twice shouldn't error or duplicate work."""
    from sqlalchemy import create_engine, text
    import backend.models

    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE projects (id VARCHAR PRIMARY KEY, name VARCHAR, mode VARCHAR, campaign_id VARCHAR)"
        ))
    backend.models.apply_lightweight_migrations(engine=eng)
    backend.models.apply_lightweight_migrations(engine=eng)  # second call must not raise

    with eng.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(projects)")).fetchall()}
    assert "series_id" in cols


def test_cache_keys_remain_global_no_series_id():
    """
    Same args → same key, regardless of which Series asks. Moderation re-runs
    per Series after the cache hit, so kids stays kids-safe."""
    from backend.services.asset_cache import _hash_key as asset_hash
    from backend.services.generated_cache import _hash_key as gen_hash

    k1 = asset_hash("starry sky", ["pexels", "pixabay"], "landscape")
    k2 = asset_hash("starry sky", ["pexels", "pixabay"], "landscape")
    assert k1 == k2

    g1 = gen_hash("drone shot of city at golden hour", "seedance_lite", "16:9", 5.0)
    g2 = gen_hash("drone shot of city at golden hour", "seedance_lite", "16:9", 5.0)
    assert g1 == g2
