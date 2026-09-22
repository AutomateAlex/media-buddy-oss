"""Phase 2.9b — Local audio asset library (BGM tracks).

A LibraryAudio is a music track downloaded from Pixabay Music or Freesound
that we want to keep around for reuse across projects. Tracks live under
~/.media-buddy-oss/library/audio/ and are indexed by mood + energy + tags.

Tagging here is *much cheaper* than visual tagging: the LLM-generated
MusicSegment (used to drive the pipeline's music search) already provides
mood/energy/narrative_role/keywords. We persist those at ingest time, so
status goes straight to 'indexed' — no async LLM call needed.

totally different shape (provider voice_id + sample audio + user metadata).
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String

from backend.database import Base


class LibraryAudio(Base):
    __tablename__ = "library_audio"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on desktop / globally-shared tracks; per-user on cloud.
    user_id = Column(String, index=True, nullable=True)

    # File location — canonical path under ~/.media-buddy-oss/library/audio/
    local_path = Column(String, nullable=False, unique=True)

    # Provenance
    source     = Column(String, index=True)  # "pixabay_music" | "freesound"
    source_id  = Column(String, index=True)
    source_url = Column(String)
    title      = Column(String)
    license    = Column(String)

    # Tech specs (probed at ingest)
    duration_seconds = Column(Float, default=0.0)

    # Pipeline-supplied tagging (no LLM call needed for music — the segment
    # we used to find the track already carries the metadata).
    mood            = Column(String, index=True)  # calm|uplifting|tense|melancholic|playful|dramatic|reflective
    energy          = Column(String, index=True)  # low|medium|high
    narrative_role  = Column(String)              # intro|build-up|climax|resolution|outro|transition
    tags            = Column(JSON, default=list)  # ["acoustic", "piano", "ambient", ...]
    description     = Column(String)              # title + brief context

    # Status — almost always 'indexed' at ingest. 'failed' if file disappeared.
    tag_status = Column(String, default="indexed", index=True)
    tag_error  = Column(String)

    # Bookkeeping
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    indexed_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    used_count    = Column(Integer, default=0, index=True)
    last_used_at  = Column(DateTime)
