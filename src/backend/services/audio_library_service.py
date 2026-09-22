"""Phase 2.9b — Local BGM library.

Mirrors LibraryService (visual) but for music tracks. Differences:
  - Tagging metadata comes from the LLM-generated MusicSegment (mood,
    energy, keywords) — no extra LLM call needed
  - Tracks are tiny vs videos, so disk footprint is minor
  - Storage: ~/.media-buddy-oss/library/audio/{source}_{source_id}.{ext}

Pipeline integration:
  - MusicService checks this library FIRST before scraping Pixabay/Freesound
  - After a successful external download, MusicService auto-ingests
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import or_

from backend.database import SessionLocal, DATA_DIR
from backend.models.library_audio import LibraryAudio

logger = logging.getLogger(__name__)


def _sanitize_for_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip())
    return s[:80]


class AudioLibraryService:
    LIBRARY_ROOT = DATA_DIR / "library"
    AUDIO_DIR    = LIBRARY_ROOT / "audio"

    def __init__(self):
        self.AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def ingest_track(
        self,
        src_path: Path | str,
        *,
        source: str,
        source_id: str,
        source_url: str = "",
        title: str = "",
        license: str = "",
        duration: Optional[float] = None,
        mood: str = "",
        energy: str = "",
        narrative_role: str = "",
        tags: Optional[list[str]] = None,
        description: str = "",
        move: bool = True,
    ) -> Optional[LibraryAudio]:
        """Move src_path into the audio library. Idempotent on (source, source_id)."""
        src = Path(src_path)
        if not src.exists():
            logger.warning("AudioLibraryService.ingest: src missing %s", src)
            return None

        db = SessionLocal()
        try:
            if source and source_id:
                existing = db.query(LibraryAudio).filter_by(
                    source=source, source_id=source_id,
                ).first()
                if existing is not None:
                    return existing

            ext = src.suffix.lower() or ".mp3"
            base = f"{_sanitize_for_filename(source or 'unknown')}_{_sanitize_for_filename(source_id or uuid.uuid4().hex[:8])}"
            target = self.AUDIO_DIR / (base + ext)
            if target.exists() and target.resolve() != src.resolve():
                target = self.AUDIO_DIR / f"{base}_{uuid.uuid4().hex[:6]}{ext}"

            if src.resolve() != target.resolve():
                if move:
                    try:
                        shutil.move(str(src), str(target))
                    except Exception as e:
                        logger.warning(
                            "AudioLibraryService.ingest: move failed (%s) — copying",
                            e,
                        )
                        shutil.copy2(str(src), str(target))
                else:
                    shutil.copy2(str(src), str(target))

            d = float(duration) if duration is not None else self._probe_duration(target)

            row = LibraryAudio(
                local_path=str(target),
                source=source or "",
                source_id=source_id or "",
                source_url=source_url or "",
                title=title or "",
                license=license or "",
                duration_seconds=d,
                mood=(mood or "").strip().lower() or None,
                energy=(energy or "").strip().lower() or None,
                narrative_role=(narrative_role or "").strip().lower() or None,
                tags=[str(t).strip().lower() for t in (tags or []) if t][:15],
                description=str(description or title or "")[:500],
                tag_status="indexed",
                indexed_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(
        self,
        query: str = "",
        *,
        mood: Optional[str] = None,
        energy: Optional[str] = None,
        min_duration: float = 0.0,
        top_n: int = 5,
        user_id: Optional[str] = None,
    ) -> list[LibraryAudio]:
        """Lookup. mood + energy match exactly; query splits to terms ANDed
        across tags + title + description. user_id (multi-tenant) scopes to that
        user's tracks; None = no filter (desktop + intra-render BGM selection)."""
        db = SessionLocal()
        try:
            q = db.query(LibraryAudio).filter(LibraryAudio.tag_status == "indexed")
            if user_id is not None:
                q = q.filter(LibraryAudio.user_id == user_id)
            if min_duration > 0:
                q = q.filter(LibraryAudio.duration_seconds >= min_duration)
            if mood:
                q = q.filter(LibraryAudio.mood == mood.strip().lower())
            if energy:
                q = q.filter(LibraryAudio.energy == energy.strip().lower())
            terms = [t for t in re.split(r"\s+", query.strip()) if t]
            for term in terms:
                like = f"%{term.lower()}%"
                q = q.filter(or_(
                    LibraryAudio.tags.contains(term.lower()),
                    LibraryAudio.title.ilike(like),
                    LibraryAudio.description.ilike(like),
                ))
            q = q.order_by(LibraryAudio.used_count.desc(), LibraryAudio.indexed_at.desc())
            return q.limit(top_n).all()
        finally:
            db.close()

    def get(self, audio_id: str) -> Optional[LibraryAudio]:
        db = SessionLocal()
        try:
            return db.query(LibraryAudio).filter(LibraryAudio.id == audio_id).first()
        finally:
            db.close()

    def list_paginated(
        self,
        page: int = 1,
        page_size: int = 24,
        mood: Optional[str] = None,
        energy: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> tuple[list[LibraryAudio], int]:
        db = SessionLocal()
        try:
            q = db.query(LibraryAudio)
            if user_id is not None:
                q = q.filter(LibraryAudio.user_id == user_id)
            if mood:
                q = q.filter(LibraryAudio.mood == mood.strip().lower())
            if energy:
                q = q.filter(LibraryAudio.energy == energy.strip().lower())
            total = q.count()
            rows = (
                q.order_by(LibraryAudio.discovered_at.desc())
                .offset((page - 1) * page_size).limit(page_size).all()
            )
            return rows, total
        finally:
            db.close()

    def bump_used(self, audio_id: str) -> None:
        db = SessionLocal()
        try:
            r = db.query(LibraryAudio).filter(LibraryAudio.id == audio_id).first()
            if r is not None:
                r.used_count = (r.used_count or 0) + 1
                r.last_used_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def delete(self, audio_id: str) -> bool:
        db = SessionLocal()
        try:
            r = db.query(LibraryAudio).filter(LibraryAudio.id == audio_id).first()
            if r is None:
                return False
            if r.local_path:
                try:
                    Path(r.local_path).unlink(missing_ok=True)
                except Exception:
                    pass
            db.delete(r)
            db.commit()
            return True
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def get_stats(self, user_id: Optional[str] = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            from sqlalchemy import func
            _own = (lambda q: q.filter(LibraryAudio.user_id == user_id)) if user_id is not None else (lambda q: q)
            total = _own(db.query(LibraryAudio)).count()
            by_mood: dict[str, int] = {}
            for mood, count in _own(db.query(
                LibraryAudio.mood, func.count(LibraryAudio.id)
            )).group_by(LibraryAudio.mood).all():
                by_mood[str(mood or "uncategorized")] = int(count)
            by_energy: dict[str, int] = {}
            for energy, count in _own(db.query(
                LibraryAudio.energy, func.count(LibraryAudio.id)
            )).group_by(LibraryAudio.energy).all():
                by_energy[str(energy or "uncategorized")] = int(count)
            disk_bytes = sum(
                (Path(p[0]).stat().st_size if p[0] and Path(p[0]).exists() else 0)
                for p in _own(db.query(LibraryAudio.local_path)).all()
            )
            return {
                "total": total,
                "by_mood": by_mood,
                "by_energy": by_energy,
                "disk_bytes": disk_bytes,
            }
        finally:
            db.close()

    # ------------------------------------------------------------------
    # FFprobe helper
    # ------------------------------------------------------------------
    def _probe_duration(self, path: Path) -> float:
        try:
            r = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "json", str(path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return 0.0
            return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration", 0.0))
        except Exception:
            return 0.0
