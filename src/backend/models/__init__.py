"""ORM models — import all here so Base.metadata.create_all picks them up."""
from backend.models.project import Project  # noqa: F401
from backend.models.chunk import Chunk, Shot  # noqa: F401
from backend.models.cache import (  # noqa: F401
    AssetCacheEntry,
    BlindObservationCacheEntry,
    GeneratedCacheEntry,
)
from backend.models.series import Series  # noqa: F401
from backend.models.batch_run import BatchRun  # noqa: F401
from backend.models.library_clip import LibraryClip  # noqa: F401
from backend.models.library_audio import LibraryAudio  # noqa: F401
from backend.models.studio_session import StudioSession  # noqa: F401
from backend.models.script_intelligence import ScriptManuscript  # noqa: F401
from backend.models.video_health import VideoHealth  # noqa: F401
from backend.models.api_error import ApiError  # noqa: F401
def apply_lightweight_migrations(engine=None) -> None:
    """Add columns introduced after the original schema was deployed. Idempotent.
    Skip cleanly when the column already exists or the table doesn't exist yet
    (fresh install — create_all will produce the right schema).

    Accepts an optional engine for tests; defaults to backend.database.engine.
    """
    from sqlalchemy import inspect, text
    if engine is None:
        from backend.database import engine as default_engine
        engine = default_engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        if "chunks" in tables:
            existing = {col["name"] for col in inspector.get_columns("chunks")}
            chunk_cols = [
                ("shot_type",     "VARCHAR DEFAULT 'atmosphere'"),
                ("is_hero_shot",  "INTEGER DEFAULT 0"),
                ("motion_tags",   "TEXT DEFAULT '[]'"),
                ("visual_tags",   "TEXT DEFAULT '[]'"),
                ("priority",      "VARCHAR DEFAULT 'normal'"),
            ]
            for name, ddl in chunk_cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}"))

        if "projects" in tables:
            existing = {col["name"] for col in inspector.get_columns("projects")}
            # Phase 2.7a — projects.series_id FK to series.id.
            # SQLite ALTER TABLE ADD COLUMN can't add FK constraints inline; we
            # add a plain column. New code reads/writes series_id; FK semantics
            # are enforced at the application layer.
            if "series_id" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN series_id VARCHAR"))
                # Backfill from legacy campaign_id when present.
                if "campaign_id" in existing:
                    conn.execute(text(
                        "UPDATE projects SET series_id = campaign_id "
                        "WHERE campaign_id IS NOT NULL AND series_id IS NULL"
                    ))
            # Phase 2.7c — projects.batch_run_id + projects.cost_usd
            if "batch_run_id" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN batch_run_id VARCHAR"))
            if "cost_usd" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN cost_usd FLOAT DEFAULT 0.0"))
            # Phase 2.11d — projects.studio_session_id FK to studio_sessions
            if "studio_session_id" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN studio_session_id VARCHAR"))
            if "tts_speed" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN tts_speed FLOAT DEFAULT 1.0"))
            if "duration_seconds" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN duration_seconds FLOAT"))
            # 输出语言功能 — 中文草稿出英文视频(出片前翻译 + 英文音色)。
            if "output_language" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN output_language VARCHAR DEFAULT 'zh'"))
            if "downloaded_at" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN downloaded_at TIMESTAMP"))
            if "framing_style" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN framing_style VARCHAR DEFAULT 'fill'"))
            if "subtitle_language" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN subtitle_language VARCHAR"))
            # FLOAT DEFAULT 安全(不是 BOOLEAN DEFAULT 0 那个会 abort 事务的坑)。
            if "subtitle_font_scale" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN subtitle_font_scale FLOAT DEFAULT 1.0"))
            # 绝不能写 DEFAULT 0 —— pg 会因 int→bool 类型不符 abort 整个迁移事务、连带后面所有列。
            if "verbatim" not in existing:
                conn.execute(text("ALTER TABLE projects ADD COLUMN verbatim BOOLEAN DEFAULT FALSE"))

        if "library_clips" in tables:
            existing = {col["name"] for col in inspector.get_columns("library_clips")}
            if "file_hash" not in existing:
                conn.execute(text("ALTER TABLE library_clips ADD COLUMN file_hash VARCHAR"))

        # Phase 2.11k — Series.style_preset_id (visual style lock)
        # NOTE: real table name is "series" (see Series.__tablename__ + FK series.id).
        # The old "seriess" check below never matched (latent no-op); kept for safety
        # but the correct "series" block is what actually runs for existing DBs.
        if "seriess" in tables:
            existing = {col["name"] for col in inspector.get_columns("seriess")}
            if "style_preset_id" not in existing:
                conn.execute(text("ALTER TABLE seriess ADD COLUMN style_preset_id VARCHAR"))
        if "series" in tables:
            existing_series = {col["name"] for col in inspector.get_columns("series")}
            if "style_preset_id" not in existing_series:
                conn.execute(text("ALTER TABLE series ADD COLUMN style_preset_id VARCHAR"))
            # 输出语言功能 — 频道默认输出语言(新项目继承)。
            if "output_language" not in existing_series:
                conn.execute(text("ALTER TABLE series ADD COLUMN output_language VARCHAR DEFAULT 'zh'"))
            # NULL=预设频道。Postgres 用 JSONB;SQLite 无 JSONB 走 JSON/TEXT。
            if "channel_rule_json" not in existing_series:
                _json_type = "JSONB" if engine.dialect.name == "postgresql" else "JSON"
                conn.execute(text(f"ALTER TABLE series ADD COLUMN channel_rule_json {_json_type}"))

        # 输出语言功能 — 批次级出片设置(音色 / 时长 / 语言)。
        if "batch_runs" in tables:
            existing_br = {col["name"] for col in inspector.get_columns("batch_runs")}
            for name, ddl in (
                ("tts_provider",     "VARCHAR"),
                ("duration_seconds", "FLOAT"),
                ("output_language",  "VARCHAR DEFAULT 'zh'"),
            ):
                if name not in existing_br:
                    conn.execute(text(f"ALTER TABLE batch_runs ADD COLUMN {name} {ddl}"))

        # the SQL that reverse-derives whitelist additions from real usage.
        if "shots" in tables:
            existing = {col["name"] for col in inspector.get_columns("shots")}
            shot_cols = [
                ("cascade_depth", "INTEGER DEFAULT 3"),
                ("prompts",       "TEXT DEFAULT '{}'"),
                ("used_level",    "VARCHAR"),
                ("used_wave",     "INTEGER"),
                ("asset_type",    "VARCHAR DEFAULT 'video'"),
                ("motion_type",   "VARCHAR"),
                ("director_intent", "TEXT"),
                ("selected_prompt", "TEXT"),
                ("verify_reason",   "TEXT"),
                ("confidence",      "FLOAT"),
                ("fallback_reason", "VARCHAR"),
            ]
            for name, ddl in shot_cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE shots ADD COLUMN {name} {ddl}"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_shots_depth_level "
                "ON shots (cascade_depth, used_level)"
            ))

        # 上云 P1 — multi-tenant owner column on user-owned tables (idempotent).
        # NULL on single-user desktop; set to the logged-in user on cloud SaaS.
        for tbl in (
            "projects", "series", "batch_runs", "studio_sessions",
            "library_clips", "library_audio", "script_manuscripts",
        ):
            if tbl not in tables:
                continue
            cols = {col["name"] for col in inspector.get_columns(tbl)}
            if "user_id" not in cols:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN user_id VARCHAR"))
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS idx_{tbl}_user_id ON {tbl} (user_id)"
                ))

        # 「预估 vs 实际」记下来,让标定漂移可被发现。
        if "video_health" in tables:
            cols = {col["name"] for col in inspector.get_columns("video_health")}
            for name, ddl in (
                ("predicted_seconds", "FLOAT"),
                ("voice",             "VARCHAR"),
                ("speed",             "FLOAT"),
                ("char_count",        "INTEGER"),
                ("chosen_version",    "VARCHAR"),
                # DEFAULT FALSE 不是 DEFAULT 0 —— Postgres 会报
                # "column is of type boolean but default expression is of type integer",
                # 整个迁移事务回滚、API 起不来。SQLite 也认 FALSE,两边通用。
                #  单测跑在 SQLite 上,抓不到这个方言差异。)
                ("degraded",          "BOOLEAN DEFAULT FALSE"),
                ("gate_calls",        "INTEGER"),
                ("gate_latency_ms",   "INTEGER"),
                ("gate_cost_usd",     "FLOAT"),
                ("failure_reason",    "VARCHAR"),
                ("rounds_used",       "INTEGER"),
            ):
                if name not in cols:
                    conn.execute(text(
                        f"ALTER TABLE video_health ADD COLUMN {name} {ddl}"
                    ))
            if "voice" not in cols:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_video_health_voice "
                    "ON video_health (voice)"
                ))

