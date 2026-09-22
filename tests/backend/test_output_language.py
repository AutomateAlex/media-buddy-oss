"""输出语言功能:翻译 purpose 路由 + 英文音色守卫 + 迁移幂等。"""
from sqlalchemy import create_engine, inspect, text




def test_translate_script_empty_passthrough_and_calls_router(monkeypatch):
    from backend.lib.llm_client import LLMClient
    c = LLMClient(provider="openrouter")
    assert c.translate_script("", "en") == ""
    assert c.translate_script("   ", "en") == "   "
    captured = {}

    def fake_call(system, user, *, purpose=None):
        captured["purpose"] = purpose
        captured["system"] = system
        return "Hello world."

    monkeypatch.setattr(c, "_call", fake_call)
    out = c.translate_script("你好世界。", "en")
    assert out == "Hello world."
    assert captured["purpose"] == "translate"
    assert "English" in captured["system"]


def test_is_english_voice():
    from backend.services.pipeline_service import _is_english_voice
    assert _is_english_voice("azure_aria") is True
    assert _is_english_voice("azure_brian") is True
    assert _is_english_voice("azure_yunyang") is False
    assert _is_english_voice("azure_xiaoxiao") is False
    assert _is_english_voice("") is False
    assert _is_english_voice("unknown_voice") is False


def test_migration_adds_output_language_idempotent():
    """apply_lightweight_migrations 给 projects/series/batch_runs 加列,重跑不报错。"""
    import backend.models  # noqa: F401 — register tables
    from backend.database import Base
    from backend.models import apply_lightweight_migrations

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)  # fresh install already has the columns
    # run twice — must be a no-op, no error, no dup column
    apply_lightweight_migrations(engine)
    apply_lightweight_migrations(engine)

    insp = inspect(engine)
    proj_cols = {c["name"] for c in insp.get_columns("projects")}
    series_cols = {c["name"] for c in insp.get_columns("series")}
    batch_cols = {c["name"] for c in insp.get_columns("batch_runs")}
    assert "output_language" in proj_cols
    assert "output_language" in series_cols
    assert "output_language" in batch_cols
    assert {"tts_provider", "duration_seconds", "output_language"} <= batch_cols


def test_migration_backfills_legacy_db_missing_column():
    """模拟老库(projects 表缺 output_language)→ 迁移补上。"""
    import backend.models  # noqa: F401
    from backend.models import apply_lightweight_migrations
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR PRIMARY KEY, name VARCHAR)"))
        conn.execute(text("CREATE TABLE series (id VARCHAR PRIMARY KEY, name VARCHAR)"))
        conn.execute(text("CREATE TABLE batch_runs (id VARCHAR PRIMARY KEY)"))
    apply_lightweight_migrations(engine)
    insp = inspect(engine)
    assert "output_language" in {c["name"] for c in insp.get_columns("projects")}
    assert "output_language" in {c["name"] for c in insp.get_columns("series")}
    assert "output_language" in {c["name"] for c in insp.get_columns("batch_runs")}
