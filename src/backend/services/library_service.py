"""Phase 2.9a — Local visual asset library.

LibraryService owns:
  - The library directory (~/.media-buddy-oss/library/clips/ + thumbnails/)
  - Ingesting clips (move file + ffprobe metadata + DB row + thumbnail)
  - Searching clips (lexical SQL across tags + category + description)
  - Tagging via OmniClient (Qwen3-VL) — usually called from BackgroundWorker idle
  - Stats / delete / retag

  - Ingest moves files into the library (canonical ownership)
  - Only "approved" clips (those returned in pipeline outcomes) get ingested
  - Idempotent on (source, source_id) — re-ingest of the same clip is a no-op
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.database import SessionLocal, DATA_DIR
from backend.models.library_clip import LibraryClip

logger = logging.getLogger(__name__)


MAX_LIBRARY_CLIP_SECONDS = 60.0


# Match these inside FootageService.search_top_n's filter.
_KNOWN_ASPECT_RATIOS: list[tuple[str, float]] = [
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("1:1", 1.0),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("21:9", 21 / 9),
]


def _aspect_label(width: int, height: int) -> str:
    if not width or not height:
        return ""
    ratio = width / height
    label, _ = min(_KNOWN_ASPECT_RATIOS, key=lambda x: abs(ratio - x[1]))
    return label


def _sanitize_for_filename(s: str) -> str:
    """Strip path-unsafe chars from source_id so we can use it in the canonical
    library filename. Preserve readability where possible."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip())
    return s[:80]  # cap length


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_TAG_STOPWORDS = {
    "and", "the", "with", "from", "into", "onto", "near", "over", "under",
    "video", "videos", "photo", "photos", "image", "images", "stock",
    "https", "http", "www", "com", "pexels", "pixabay", "coverr",
    # these, queries like "animated representation OF 26 dimensions"
    # leak "of" → ilike("%of%") matches any description blob containing
    # the substring (e.g. "coffee cup OF hot drink"), score=2 from blob
    # hit alone, and the unrelated clip gets surfaced. Filter at source.
    "of", "to", "in", "on", "at", "by", "as", "an", "is", "be", "or",
    "for", "a", "this", "that", "these", "those", "their", "its", "it",
    "are", "was", "were", "has", "have", "had", "do", "does", "did",
}


_QUERY_SYNONYMS = {
    "abstract": ["concept", "visual", "background", "mathematical", "geometric"],
    "concept": ["abstract", "visual", "idea", "metaphor"],
    "math": ["mathematical", "geometry", "geometric", "concept", "abstract"],
    "mathematical": ["math", "geometry", "geometric", "concept", "abstract", "pattern"],
    "geometry": ["geometric", "mathematical", "line", "circle", "grid", "pattern"],
    "geometric": ["geometry", "mathematical", "line", "circle", "grid", "pattern"],
    "fractal": ["mathematical", "geometric", "pattern", "branch", "recursive", "abstract"],
    "fractals": ["fractal", "mathematical", "geometric", "pattern", "branch", "recursive", "abstract"],
    "dimension": ["dimensional", "geometry", "geometric", "mathematical", "space", "abstract"],
    "dimensions": ["dimension", "dimensional", "geometry", "geometric", "mathematical", "space", "abstract"],
    "line": ["lines", "thread", "connection", "network", "geometric"],
    "lines": ["line", "threads", "connection", "network", "geometric"],
    "circle": ["circles", "ring", "rings", "loop", "geometric"],
    "circles": ["circle", "rings", "loop", "geometric"],
    "earth": ["planet", "globe", "world", "global", "network", "connection", "digital"],
    "planet": ["earth", "globe", "world", "global", "network", "connection", "digital"],
    "globe": ["earth", "planet", "world", "global", "network", "connection", "digital"],
    "global": ["world", "earth", "planet", "network", "connection", "digital"],
    "network": ["connection", "connections", "connected", "data", "digital", "global", "lines"],
    "networks": ["network", "connection", "connections", "data", "digital", "global", "lines"],
    "connection": ["network", "connections", "connected", "data", "digital", "lines"],
    "connected": ["connection", "connections", "network", "data", "digital", "lines"],
    "data": ["digital", "network", "connection", "technology", "abstract"],
    "digital": ["data", "network", "connection", "technology", "abstract"],
    "french": ["food", "cuisine", "restaurant", "dining", "cafe", "coffee", "wine", "cheese", "bread", "dessert"],
    "france": ["french", "food", "cuisine", "restaurant", "cafe", "wine"],
    "cuisine": ["food", "meal", "dish", "restaurant", "dining"],
    "culinary": ["food", "cuisine", "meal", "dish", "restaurant"],
    "dining": ["food", "meal", "restaurant", "table", "friends", "cafe"],
    "gourmet": ["food", "dish", "restaurant", "chef", "plate"],
    "meal": ["food", "dish", "restaurant", "table"],
    "dish": ["food", "meal", "plate"],
    "appetizer": ["food", "dish", "plate", "seafood"],
    "beef": ["food", "meal", "dish"],
    "stew": ["food", "meal", "dish"],
    "wine": ["drink", "glass", "restaurant", "dining"],
    "coffee": ["cafe", "beverage", "drink", "barista"],
    "cafe": ["coffee", "restaurant", "table", "barista"],
    "café": ["cafe", "coffee", "restaurant", "table", "barista"],
    "bread": ["food", "bakery", "meal"],
    "baguette": ["bread", "food", "bakery"],
    "cheese": ["food", "dish", "plate"],
    "dessert": ["food", "cake", "sweet"],
    "friends": ["people", "group", "conversation", "dining", "cafe"],
    "people": ["person", "friends", "group"],
    "table": ["dining", "restaurant", "food", "coffee"],
}


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def _query_terms(query: str) -> list[str]:
    """Tokenize a search query and expand broad concept words.

    LLM-generated queries often contain abstract terms like "French culture";
    local clips are tagged concretely ("coffee", "wine", "food"). Expanding
    those bridges makes the local library useful before falling through to
    external stock providers.
    """
    text = _strip_accents((query or "").lower())
    terms: list[str] = []
    for tok in re.findall(r"[a-z][a-z0-9_-]{1,}", text):
        tok = tok.strip("_-")
        if not tok or tok in _TAG_STOPWORDS:
            continue
        if tok not in terms:
            terms.append(tok)
        for extra in _QUERY_SYNONYMS.get(tok, []):
            extra = _strip_accents(extra.lower())
            if extra not in terms:
                terms.append(extra)
    return terms[:40]


def _clip_search_blob(clip: LibraryClip) -> str:
    values = [
        clip.category or "",
        clip.description or "",
        " ".join(clip.tags or []),
        " ".join(clip.mood_tags or []),
        " ".join(clip.motion_tags or []),
        Path(clip.local_path or "").name,
    ]
    return _strip_accents(" ".join(values).lower())


def _clip_search_score(clip: LibraryClip, terms: list[str]) -> int:
    if not terms:
        return 0
    blob = _clip_search_blob(clip)
    tags = set(str(t).lower() for t in (clip.tags or []))
    category = (clip.category or "").lower()
    score = 0
    for term in terms:
        if term in tags:
            score += 6
        if category == term:
            score += 4
        if term in blob:
            score += 2
    return score


def _tokenize_tags(*values: Any, max_tags: int = 24) -> list[str]:
    """Build a compact English keyword list from search/provider metadata."""
    out: list[str] = []

    def add_one(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add_one(item)
            return
        text = str(raw).lower()
        for tok in re.findall(r"[a-z][a-z0-9_-]{1,}", text):
            tok = tok.strip("_-")
            if not tok or tok in _TAG_STOPWORDS or len(tok) < 2:
                continue
            if tok not in out:
                out.append(tok)
            if len(out) >= max_tags:
                return

    for value in values:
        add_one(value)
        if len(out) >= max_tags:
            break
    return out[:max_tags]


def _category_from_tags(tags: list[str], shot_type: str = "") -> str:
    tagset = set(tags)
    shot = (shot_type or "").lower()
    category_rules = [
        ("food", {"food", "restaurant", "kitchen", "chef", "coffee", "cafe", "meal", "drink", "barista"}),
        ("people", {"person", "people", "woman", "man", "child", "family", "worker", "customer", "portrait"}),
        ("nature", {"nature", "forest", "mountain", "beach", "sea", "ocean", "river", "sky", "tree", "flower"}),
        ("urban", {"city", "street", "traffic", "building", "downtown", "road", "sidewalk"}),
        ("interior", {"interior", "room", "home", "office", "indoor", "house", "apartment"}),
        ("business", {"business", "meeting", "office", "laptop", "team", "startup", "finance"}),
        ("tech", {"tech", "technology", "computer", "phone", "screen", "robot", "ai"}),
        ("sports", {"sport", "sports", "fitness", "running", "gym", "ball", "workout"}),
        ("vehicles", {"car", "vehicle", "train", "plane", "boat", "bus", "bicycle", "traffic"}),
        ("animals", {"animal", "dog", "cat", "bird", "fish", "horse", "wildlife"}),
        ("textures", {"texture", "pattern", "fabric", "wood", "stone", "wall"}),
        ("abstract", {"abstract", "background", "gradient", "bokeh", "light"}),
    ]
    if "human" in shot:
        return "people"
    for category, needles in category_rules:
        if tagset & needles:
            return category
    return "other"


class LibraryService:
    """Owns ingest + search + tag for the local clip library."""

    LIBRARY_ROOT = DATA_DIR / "library"
    CLIPS_DIR    = LIBRARY_ROOT / "clips"
    THUMBS_DIR   = LIBRARY_ROOT / "thumbnails"

    # Controlled vocabulary for category. The LLM is asked to pick from
    # this list; if it returns something else we set category="other".
    CATEGORIES = (
        "people", "nature", "urban", "interior", "food",
        "business", "abstract", "tech", "sports",
        "vehicles", "animals", "textures", "other",
    )

    def __init__(self):
        self.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        self.THUMBS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def ingest(
        self,
        src_path: Path | str,
        *,
        source: str,
        source_id: str,
        source_url: str = "",
        duration: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        move: bool = True,
        metadata_tags: Optional[list[str]] = None,
        metadata_category: str = "",
        metadata_description: str = "",
        metadata_mood_tags: Optional[list[str]] = None,
        metadata_motion_tags: Optional[list[str]] = None,
        tag_provider: str = "metadata",
    ) -> Optional[LibraryClip]:
        """Move src_path into the library. Idempotent: returns existing row
        if (source, source_id) is already indexed. Returns None if src_path
        is missing.

        `move=True` (default): the source file is moved out of its prior
        location. If it's already inside CLIPS_DIR, just records the row.
        """
        src = Path(src_path)
        if not src.exists():
            logger.warning("LibraryService.ingest: src missing %s", src)
            return None

        probed = self._probe(src) if (
            duration is None or width is None or height is None
        ) else {}
        d = float(duration if duration is not None else probed.get("duration", 0.0) or 0.0)
        w = int(width if width is not None else probed.get("width", 0) or 0)
        h = int(height if height is not None else probed.get("height", 0) or 0)
        if d > MAX_LIBRARY_CLIP_SECONDS:
            logger.info(
                "LibraryService.ingest: skip clip longer than %.0fs (%s %.2fs)",
                MAX_LIBRARY_CLIP_SECONDS,
                src,
                d,
            )
            return None

        try:
            file_hash = _sha256_file(src)
        except Exception:
            file_hash = ""

        db = SessionLocal()
        try:
            # Idempotency by (source, source_id)
            if source and source_id:
                existing = db.query(LibraryClip).filter_by(
                    source=source, source_id=source_id,
                ).first()
                if existing is not None:
                    if float(existing.duration_seconds or 0.0) > MAX_LIBRARY_CLIP_SECONDS:
                        self._delete_row_files(existing)
                        db.delete(existing)
                        db.commit()
                        return None
                    return existing

            if file_hash:
                existing = db.query(LibraryClip).filter_by(file_hash=file_hash).first()
                if existing is not None:
                    return existing

            if source_url:
                existing = db.query(LibraryClip).filter_by(source_url=source_url).first()
                if existing is not None:
                    return existing

            # Pick canonical filename
            ext = src.suffix.lower() or ".mp4"
            base = f"{_sanitize_for_filename(source or 'unknown')}_{_sanitize_for_filename(source_id or uuid.uuid4().hex[:8])}"
            target = self.CLIPS_DIR / (base + ext)

            # If target exists but pointed to a different file, append a
            # hash to avoid clobbering.
            if target.exists() and target.resolve() != src.resolve():
                target = self.CLIPS_DIR / f"{base}_{uuid.uuid4().hex[:6]}{ext}"

            # Move (or copy if move=False, or skip if same path)
            if src.resolve() != target.resolve():
                if move:
                    try:
                        shutil.move(str(src), str(target))
                    except Exception as e:
                        logger.warning(
                            "LibraryService.ingest: move failed (%s) — falling back to copy",
                            e,
                        )
                        shutil.copy2(str(src), str(target))
                else:
                    shutil.copy2(str(src), str(target))

            seed_tags = _tokenize_tags(metadata_tags or [])
            seed_category = (
                metadata_category
                if metadata_category in self.CATEGORIES
                else _category_from_tags(seed_tags)
            )
            has_seed = bool(seed_tags or metadata_description or metadata_mood_tags or metadata_motion_tags)

            clip = LibraryClip(
                local_path=str(target),
                file_hash=file_hash or None,
                source=source or "",
                source_id=source_id or "",
                source_url=source_url or "",
                duration_seconds=d,
                width=w,
                height=h,
                aspect_ratio=_aspect_label(w, h),
                category=seed_category if has_seed else None,
                tags=seed_tags,
                description=(metadata_description or "")[:1000] if has_seed else None,
                mood_tags=_tokenize_tags(metadata_mood_tags or [], max_tags=8),
                motion_tags=_tokenize_tags(metadata_motion_tags or [], max_tags=8),
                tag_status="indexed" if has_seed else "pending",
                tag_provider=tag_provider if has_seed else None,
                tag_cost_usd=0.0,
                indexed_at=datetime.now(timezone.utc) if has_seed else None,
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)

            # Best-effort thumbnail (don't fail ingest if ffmpeg balks)
            try:
                thumb_path = self._extract_thumbnail(target, clip.id)
                if thumb_path:
                    clip.thumbnail_path = str(thumb_path)
                    db.commit()
            except Exception as e:
                logger.warning("LibraryService.ingest: thumbnail failed for %s: %s", target.name, e)

            return clip
        finally:
            db.close()

    def ingest_outcomes(self, outcomes: Iterable) -> int:
        """
        path). Each outcome has local_path/source/source_id/duration/width/
        height. Returns count of new clips added."""
        added = 0
        for oc in outcomes:
            local_path = getattr(oc, "local_path", None)
            if not local_path:
                continue
            source = getattr(oc, "source", "") or ""
            if source == "library":
                continue
            if source == "local_fallback":
                continue
            source_id = getattr(oc, "source_id", "") or ""
            if not source or not source_id:
                # Synthesize an id from the file basename so the same clip
                # in two pipelines doesn't double-ingest.
                source = source or "unknown"
                source_id = source_id or Path(local_path).stem[:64]
            plan = getattr(oc, "plan", {}) or {}
            search_query = plan.get("search_query", "") or getattr(oc, "query", "")
            visual_tags = plan.get("visual_tags", []) or []
            motion_tags = plan.get("motion_tags", []) or []
            source_tags = getattr(oc, "source_tags", "") or ""
            seed_tags = _tokenize_tags(
                search_query, source_tags, visual_tags, motion_tags,
                plan.get("shot_type", ""),
            )
            seed_category = _category_from_tags(seed_tags, plan.get("shot_type", ""))
            description = ""
            if search_query:
                description = f"Stock clip selected for: {search_query}"
            res = self.ingest(
                local_path,
                source=source,
                source_id=source_id,
                source_url=getattr(oc, "source_url", "") or "",
                duration=getattr(oc, "duration", None),
                # outcomes don't have width/height; let _probe fill them
                move=False,  # keep file in pipeline dir; library copies
                metadata_tags=seed_tags,
                metadata_category=seed_category,
                metadata_description=description,
                metadata_motion_tags=motion_tags,
                tag_provider="pipeline_metadata",
            )
            if res is not None:
                added += 1
        return added

    # ------------------------------------------------------------------
    # Local imports (user's own footage)
    # ------------------------------------------------------------------
    # Recognized video extensions when walking a folder.
    LOCAL_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}

    def import_local_file(
        self,
        src_path: Path | str,
        *,
        move: bool = False,
        custom_source_id: str = "",
    ) -> Optional[LibraryClip]:
        """Ingest a single user-supplied video file.

        Files keep `source="local_import"` and get a stable source_id from
        their on-disk path (so re-importing the same file is a no-op).
        Status starts at `pending` so BackgroundWorker auto-tags it via the
        same Qwen3-VL flow as stock clips. `move=False` keeps the original
        in place; library copies it.
        """
        src = Path(src_path)
        if not src.exists() or not src.is_file():
            return None
        if src.suffix.lower() not in self.LOCAL_VIDEO_EXTS:
            return None
        # Stable id: filename + size makes it idempotent on re-import without
        # full content hashing (which would be slow on big libraries).
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        sid = custom_source_id or f"{src.stem[:60]}_{size}"
        return self.ingest(
            src,
            source="local_import",
            source_id=sid,
            source_url=str(src.resolve()),  # original location, for reference
            move=move,
        )

    def import_local_folder(
        self,
        folder_path: Path | str,
        *,
        recursive: bool = True,
    ) -> dict[str, int]:
        """Walk a folder, ingest every video file inside. Returns counters.

        Files are COPIED into the library (move=False), so the user's
        original folder is untouched.
        """
        folder = Path(folder_path)
        if not folder.exists() or not folder.is_dir():
            return {"scanned": 0, "ingested": 0, "skipped": 0, "error": "folder not found"}

        scanned = ingested = skipped = 0
        iterator = folder.rglob("*") if recursive else folder.iterdir()
        for f in iterator:
            try:
                if not f.is_file():
                    continue
                if f.suffix.lower() not in self.LOCAL_VIDEO_EXTS:
                    continue
                scanned += 1
                # Skip files that are already inside our library (prevents
                # accidentally re-ingesting if user points at the library dir)
                try:
                    if str(f.resolve()).startswith(str(self.CLIPS_DIR.resolve())):
                        skipped += 1
                        continue
                except Exception:
                    pass
                row = self.import_local_file(f, move=False)
                if row is not None:
                    ingested += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning("import_local_folder: skipping %s — %s", f, e)
                skipped += 1
        return {"scanned": scanned, "ingested": ingested, "skipped": skipped}

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------
    def backfill_from_pipelines(self, pipeline_root: Optional[Path] = None) -> dict[str, int]:
        """Walk every project's assets checkpoint and ingest its
        footage_results. Reads checkpoints via lib/progress.read_progress.
        Returns {scanned_projects, ingested_clips, skipped}."""
        from backend.lib.progress import read_progress
        if pipeline_root is None:
            pipeline_root = DATA_DIR / "pipelines"
        if not pipeline_root.exists():
            return {"scanned_projects": 0, "ingested_clips": 0, "skipped": 0}

        scanned = ingested = skipped = 0
        for project_dir in pipeline_root.iterdir():
            if not project_dir.is_dir():
                continue
            scanned += 1
            cp = read_progress(pipeline_root, project_dir.name, "assets")
            if not cp or cp.get("status") != "completed":
                skipped += 1
                continue
            footage = (cp.get("artifacts") or {}).get("asset_manifest", {}).get("footage_results", [])
            for entry in footage:
                lp = entry.get("local_path")
                if not lp or not Path(lp).exists():
                    continue
                src = entry.get("source") or "unknown"
                sid = entry.get("source_id") or Path(lp).stem
                clip = self.ingest(
                    lp,
                    source=src,
                    source_id=sid,
                    source_url=entry.get("source_url", ""),
                    duration=entry.get("duration"),
                    move=False,  # never move backfilled files; pipeline owns
                )
                if clip is not None:
                    ingested += 1
        return {"scanned_projects": scanned, "ingested_clips": ingested, "skipped": skipped}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(
        self,
        query: str = "",
        *,
        top_n: int = 5,
        aspect_ratio: Optional[str] = None,
        min_duration: float = 0.0,
        category: Optional[str] = None,
        only_indexed: bool = True,
        user_id: Optional[str] = None,
    ) -> list[LibraryClip]:
        """Lexical search with OR matching + relevance scoring.

        Falls back to most-recently-discovered when query is empty.

        Note: SQLite stores JSON columns as TEXT, so LIKE on the raw column
        works for tag membership ("contains substring"). Good enough for MVP.
        ``user_id`` (multi-tenant) scopes to that user's clips; None = no filter
        (desktop + the render worker's footage search, which is intra-render).
        """
        db = SessionLocal()
        try:
            q = db.query(LibraryClip)
            if user_id is not None:
                q = q.filter(LibraryClip.user_id == user_id)
            if only_indexed:
                q = q.filter(LibraryClip.tag_status == "indexed")
            if aspect_ratio:
                q = q.filter(LibraryClip.aspect_ratio == aspect_ratio)
            if min_duration > 0:
                q = q.filter(LibraryClip.duration_seconds >= min_duration)
            if category:
                q = q.filter(LibraryClip.category == category)

            terms = _query_terms(query)
            if not terms:
                q = q.order_by(LibraryClip.used_count.desc(), LibraryClip.indexed_at.desc())
                return q.limit(top_n).all()

            clauses = []
            for term in terms:
                like = f"%{term}%"
                clauses.extend([
                    LibraryClip.tags.contains(term),
                    LibraryClip.description.ilike(like),
                    LibraryClip.category.ilike(like),
                    LibraryClip.mood_tags.contains(term),
                    LibraryClip.motion_tags.contains(term),
                ])
            q = q.filter(or_(*clauses))
            rows = q.limit(max(top_n * 20, 100)).all()
            # filter above can pull in rows via ilike substring matches that
            # carry no real signal (e.g. one stray word fragment in a
            # description blob). Without this filter, a query like
            # "26 dimensions" would return library:pixabay_video_3158 tagged
            # "warm sunlight french coffee cup" — overlap is genuinely 0 but
            # the row gets surfaced to pad top_n. Returning [] is correct;
            # orchestrator falls through to Pexels/Pixabay live search.
            scored = [(c, _clip_search_score(c, terms)) for c in rows]
            scored = [(c, s) for c, s in scored if s > 0]
            scored.sort(
                key=lambda cs: (
                    cs[1],
                    int(cs[0].used_count or 0),
                    cs[0].indexed_at or cs[0].discovered_at or datetime.min,
                ),
                reverse=True,
            )
            return [c for (c, _) in scored[:top_n]]
        finally:
            db.close()

    def get(self, clip_id: str) -> Optional[LibraryClip]:
        db = SessionLocal()
        try:
            return db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
        finally:
            db.close()

    def list_paginated(
        self,
        page: int = 1,
        page_size: int = 24,
        category: Optional[str] = None,
        tag_status: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> tuple[list[LibraryClip], int]:
        db = SessionLocal()
        try:
            q = db.query(LibraryClip)
            if user_id is not None:  # 多租户:只列该用户的素材(桌面传 None 不过滤)
                q = q.filter(LibraryClip.user_id == user_id)
            if category:
                q = q.filter(LibraryClip.category == category)
            if tag_status:
                q = q.filter(LibraryClip.tag_status == tag_status)
            total = q.count()
            rows = (
                q.order_by(LibraryClip.discovered_at.desc())
                .offset((page - 1) * page_size).limit(page_size).all()
            )
            return rows, total
        finally:
            db.close()

    def bump_used(self, clip_id: str) -> None:
        db = SessionLocal()
        try:
            c = db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
            if c is not None:
                c.used_count = (c.used_count or 0) + 1
                c.last_used_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    @staticmethod
    def _delete_row_files(c: LibraryClip) -> None:
        for path_attr in ("local_path", "thumbnail_path"):
            p = getattr(c, path_attr, None)
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass

    def delete(self, clip_id: str) -> bool:
        db = SessionLocal()
        try:
            c = db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
            if c is None:
                return False
            self._delete_row_files(c)
            db.delete(c)
            db.commit()
            return True
        finally:
            db.close()

    def cleanup_duplicates_and_long_clips(
        self,
        *,
        max_duration_seconds: float = MAX_LIBRARY_CLIP_SECONDS,
    ) -> dict[str, int]:
        """Remove clips that should never be in the library.

        Rules:
          - clips longer than max_duration_seconds are deleted;
          - exact duplicate files (sha256), duplicate source ids, and duplicate
            source urls are collapsed to one row.
        """
        db = SessionLocal()
        try:
            removed_long = 0
            removed_duplicates = 0
            hashed = 0

            for c in db.query(LibraryClip).all():
                if not c.file_hash and c.local_path and Path(c.local_path).exists():
                    try:
                        c.file_hash = _sha256_file(Path(c.local_path))
                        hashed += 1
                    except Exception:
                        pass
            db.commit()

            rows = db.query(LibraryClip).all()
            for c in rows:
                source_text = " ".join([
                    str(c.source or ""),
                    str(c.source_id or ""),
                    str(c.source_url or ""),
                    str(c.local_path or ""),
                ]).lower()
                if "local_fallback" in source_text:
                    self._delete_row_files(c)
                    db.delete(c)
                    removed_duplicates += 1
                    continue
                if float(c.duration_seconds or 0.0) > max_duration_seconds:
                    self._delete_row_files(c)
                    db.delete(c)
                    removed_long += 1
            db.commit()

            rows = db.query(LibraryClip).all()
            rows.sort(
                key=lambda c: (
                    -int(c.used_count or 0),
                    0 if c.tag_status == "indexed" else 1,
                    (c.discovered_at or datetime.min).isoformat(),
                )
            )
            seen: dict[tuple[str, str], str] = {}
            delete_ids: set[str] = set()
            for c in rows:
                keys: list[tuple[str, str]] = []
                if c.file_hash:
                    keys.append(("hash", str(c.file_hash)))
                if c.source and c.source_id:
                    keys.append(("source_id", f"{c.source}:{c.source_id}"))
                if c.source_url:
                    keys.append(("source_url", str(c.source_url)))
                duplicate = any(key in seen for key in keys)
                if duplicate:
                    delete_ids.add(c.id)
                    continue
                for key in keys:
                    seen[key] = c.id

            if delete_ids:
                for c in db.query(LibraryClip).filter(LibraryClip.id.in_(delete_ids)).all():
                    self._delete_row_files(c)
                    db.delete(c)
                    removed_duplicates += 1
                db.commit()

            return {
                "removed_long": removed_long,
                "removed_duplicates": removed_duplicates,
                "hashed": hashed,
            }
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def get_stats(self, user_id: Optional[str] = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            from sqlalchemy import func
            _own = (lambda q: q.filter(LibraryClip.user_id == user_id)) if user_id is not None else (lambda q: q)
            total = _own(db.query(LibraryClip)).count()
            by_status: dict[str, int] = {}
            for status, count in _own(db.query(
                LibraryClip.tag_status, func.count(LibraryClip.id)
            )).group_by(LibraryClip.tag_status).all():
                by_status[str(status or "")] = int(count)
            by_category: dict[str, int] = {}
            for cat, count in _own(db.query(
                LibraryClip.category, func.count(LibraryClip.id)
            )).filter(LibraryClip.tag_status == "indexed").group_by(
                LibraryClip.category
            ).all():
                by_category[str(cat or "uncategorized")] = int(count)
            disk_bytes = sum(
                (Path(p[0]).stat().st_size if p[0] and Path(p[0]).exists() else 0)
                for p in db.query(LibraryClip.local_path).all()
            )
            total_cost = float(db.query(func.coalesce(func.sum(LibraryClip.tag_cost_usd), 0.0)).scalar() or 0.0)
            return {
                "total": total,
                "by_status": by_status,
                "by_category": by_category,
                "disk_bytes": disk_bytes,
                "tagging_cost_usd": total_cost,
            }
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Tagging
    # ------------------------------------------------------------------
    def claim_next_pending(self) -> Optional[str]:
        """Pick the oldest pending clip, mark it 'tagging', return its id.
        Done in a tight DB session so two workers wouldn't double-claim."""
        db = SessionLocal()
        try:
            row = (
                db.query(LibraryClip)
                .filter(LibraryClip.tag_status == "pending")
                .order_by(LibraryClip.discovered_at.asc())
                .first()
            )
            if row is None:
                return None
            row.tag_status = "tagging"
            db.commit()
            return row.id
        finally:
            db.close()

    def apply_tagging_result(
        self,
        clip_id: str,
        result: dict[str, Any],
        provider: str,
        cost_usd: float = 0.0,
    ) -> None:
        """Persist tagging result. `result` shape:
            {
              category: str,
              tags: list[str],
              description: str,
              mood_tags: list[str],
              motion_tags: list[str],
            }
        """
        db = SessionLocal()
        try:
            c = db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
            if c is None:
                return
            cat = (result.get("category") or "").strip().lower()
            if cat not in self.CATEGORIES:
                cat = "other"
            c.category    = cat
            c.tags        = [str(t).strip().lower() for t in (result.get("tags") or []) if t][:20]
            c.description = str(result.get("description") or "")[:1000]
            c.mood_tags   = [str(t).strip().lower() for t in (result.get("mood_tags") or []) if t][:8]
            c.motion_tags = [str(t).strip().lower() for t in (result.get("motion_tags") or []) if t][:8]
            c.tag_provider = provider
            c.tag_cost_usd = float(cost_usd or 0.0)
            c.tag_status   = "indexed"
            c.tag_error    = None
            c.indexed_at   = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

    def mark_failed(self, clip_id: str, error: str) -> None:
        db = SessionLocal()
        try:
            c = db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
            if c is None:
                return
            c.tag_status = "failed"
            c.tag_error  = (error or "")[:500]
            db.commit()
        finally:
            db.close()

    def retry_failed_tags(self) -> int:
        """Move failed clips back to pending so the worker can tag them again.

        This is intentionally broad: most failed tagging states are transient
        maintenance), and users need a one-click way to recover the library
        once the account is healthy again.
        """
        db = SessionLocal()
        try:
            rows = db.query(LibraryClip).filter(LibraryClip.tag_status == "failed").all()
            for c in rows:
                c.tag_status = "pending"
                c.tag_error = None
            db.commit()
            return len(rows)
        finally:
            db.close()

    def retag(self, clip_id: str) -> bool:
        """Reset a clip back to pending so the worker re-tags it."""
        db = SessionLocal()
        try:
            c = db.query(LibraryClip).filter(LibraryClip.id == clip_id).first()
            if c is None:
                return False
            c.tag_status = "pending"
            c.tag_error  = None
            db.commit()
            return True
        finally:
            db.close()

    # ------------------------------------------------------------------
    # FFmpeg helpers
    # ------------------------------------------------------------------
    def _probe(self, video_path: Path) -> dict[str, Any]:
        """ffprobe → {duration, width, height}. Tolerates failures."""
        try:
            r = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:format=duration",
                    "-of", "json",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return {}
            data = json.loads(r.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            fmt = data.get("format") or {}
            return {
                "width":    int(stream.get("width", 0) or 0),
                "height":   int(stream.get("height", 0) or 0),
                "duration": float(fmt.get("duration", 0.0) or 0.0),
            }
        except Exception as e:
            logger.warning("LibraryService._probe failed for %s: %s", video_path, e)
            return {}

    def _extract_thumbnail(self, video_path: Path, clip_id: str) -> Optional[Path]:
        """ffmpeg → 1 frame jpg at 1s mark. Falls back to 0s if clip < 1s."""
        out = self.THUMBS_DIR / f"{clip_id}.jpg"
        try:
            r = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", "1.0", "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=480:-2",
                    "-q:v", "5",
                    str(out),
                ],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0 or not out.exists():
                # Retry at 0s
                r = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(video_path),
                        "-frames:v", "1",
                        "-vf", "scale=480:-2",
                        "-q:v", "5",
                        str(out),
                    ],
                    capture_output=True, text=True, timeout=15,
                )
            return out if out.exists() else None
        except Exception:
            return None
