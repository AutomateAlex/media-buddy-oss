from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from pathlib import Path
import os

_data_dir_raw = os.environ.get("MEDIA_BUDDY_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_raw).expanduser()
    if _data_dir_raw
    else Path.home() / ".media-buddy-oss"
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Set DATABASE_URL (e.g. postgresql+psycopg://user:pwd@host:5432/postgres) to switch.
# When unset, behavior is byte-identical to before (local single-user SQLite).
_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    # costs ~1.5s (TLS + SCRAM over the cross-region link). NullPool (open per
    # op) made every query pay that — ~20 reads/s. A persistent pool REUSES
    # connections so the handshake is paid once and amortized. We're on the
    # TRANSACTION pooler (6543), so an idle pooled client connection does NOT
    # pin a DB connection — Supavisor multiplexes — so holding a small pool per
    # process is cheap and correct. pre_ping drops stale conns; recycle refreshes.
    engine = create_engine(
        _database_url,
        pool_size=int(os.environ.get("MB_DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("MB_DB_MAX_OVERFLOW", "10")),
        pool_pre_ping=True,
        pool_recycle=600,
    )
else:
    engine = create_engine(
        f"sqlite:///{DATA_DIR / 'media_buddy.db'}",
        connect_args={"check_same_thread": False},
    )

# DDL / Base.metadata.create_all prefers a SESSION or DIRECT connection — the
# transaction pooler can be finicky with multi-statement DDL + reflection. Point
# MIGRATION_DATABASE_URL at the session pooler (5432) or the direct db host;
# falls back to the runtime engine when unset (e.g. desktop SQLite).
_migration_url = os.environ.get("MIGRATION_DATABASE_URL")
if _migration_url:
    from sqlalchemy.pool import NullPool as _NullPool

    migration_engine = create_engine(_migration_url, poolclass=_NullPool)
else:
    migration_engine = engine

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
