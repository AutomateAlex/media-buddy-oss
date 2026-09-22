"""Pexels video stock source.

Wraps the Pexels Videos API (https://www.pexels.com/api/documentation/).
Free key required (set ``PEXELS_API_KEY`` env). License is the Pexels
content license — free for commercial use, no attribution required.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .base import Candidate, SearchFilters, http_get_json, stream_download

_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
_IMAGE_SEARCH_URL = "https://api.pexels.com/v1/search"


def _select_best_file(video_files: list[dict],
                       min_width: Optional[int]) -> Optional[dict]:
    """Pick the smallest mp4 video_file that still meets ``min_width``.

    Pexels returns multiple variants per video (uhd/hd/sd at various
    resolutions). We don't want the 4K source for a 1080p output —
    that wastes bandwidth and disk for no visible win. Sort ascending
    by width, take the first that hits the floor.
    """
    if not video_files:
        return None
    candidates = [
        f for f in video_files
        if (f.get("file_type") or "").lower() in ("video/mp4", "")
        and (f.get("link") or "").startswith("http")
        and (f.get("width") or 0) > 0
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: int(f.get("width") or 0))
    floor = int(min_width or 0)
    for f in candidates:
        if int(f.get("width") or 0) >= floor:
            return f
    # Nothing meets floor — return the largest as a best-effort
    return candidates[-1]


class PexelsSource:
    """Pexels video adapter."""

    name = "pexels"
    display_name = "Pexels (videos)"
    install_instructions = (
        "Set PEXELS_API_KEY in Settings (free key at https://www.pexels.com/api/)."
    )

    def is_available(self) -> bool:
        return bool(os.environ.get("PEXELS_API_KEY"))

    def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not query.strip():
            return []
        api_key = os.environ.get("PEXELS_API_KEY", "")
        if not api_key:
            return []
        if filters.kind == "image":
            return self._search_images(query, filters, api_key)
        if filters.kind not in ("video", "any"):
            return []

        params: dict = {
            "query": query,
            "per_page": max(1, min(int(filters.per_page or 15), 80)),
            "page": max(1, int(filters.page or 1)),
        }
        if filters.orientation in ("landscape", "portrait", "square"):
            params["orientation"] = filters.orientation
        if filters.min_duration is not None:
            params["min_duration"] = int(filters.min_duration)
        if filters.max_duration is not None:
            params["max_duration"] = int(filters.max_duration)

        data = http_get_json(
            _VIDEO_SEARCH_URL, params=params,
            headers={"Authorization": api_key},
        )
        out: list[Candidate] = []
        for v in data.get("videos") or []:
            best = _select_best_file(v.get("video_files") or [], filters.min_width)
            if best is None:
                continue
            user = v.get("user") or {}
            out.append(Candidate(
                source=self.name,
                source_id=str(v.get("id") or ""),
                source_url=str(v.get("url") or ""),
                download_url=str(best.get("link") or ""),
                kind="video",
                width=int(best.get("width") or v.get("width") or 0),
                height=int(best.get("height") or v.get("height") or 0),
                duration=float(v.get("duration") or 0.0),
                creator=str(user.get("name") or ""),
                license="Pexels License",
                source_tags=query,
                thumbnail_url=str(v.get("image") or ""),
                extra={
                    "fps": best.get("fps"),
                    "quality": best.get("quality"),
                    "file_id": best.get("id"),
                    # multi-frame preview array (video_pictures: [{nr, picture}])
                    # we can judge without downloading the clip. Stash it for
                    # the observer-debug report / future frame evidence.
                    "video_pictures": v.get("video_pictures") or [],
                },
            ))
        return out

    def download(self, candidate: Candidate, out_path: Path) -> Path:
        return stream_download(candidate.download_url, Path(out_path))

    def _search_images(
        self, query: str, filters: SearchFilters, api_key: str,
    ) -> list[Candidate]:
        """

        Different endpoint from /videos/search. Returns Candidate(kind=
        'image', duration=0); orchestrator's L5 image cascade picks one
        and runs ImageMotionService → Ken Burns mp4 → primary clip.
        """
        params: dict = {
            "query": query,
            "per_page": max(1, min(int(filters.per_page or 15), 80)),
            "page": max(1, int(filters.page or 1)),
        }
        if filters.orientation in ("landscape", "portrait", "square"):
            params["orientation"] = filters.orientation

        data = http_get_json(
            _IMAGE_SEARCH_URL, params=params,
            headers={"Authorization": api_key},
        )
        out: list[Candidate] = []
        for ph in data.get("photos") or []:
            src_set = ph.get("src") or {}
            url = (
                src_set.get("large2x") or src_set.get("large")
                or src_set.get("original") or ""
            )
            if not url:
                continue
            w = int(ph.get("width") or 0)
            h = int(ph.get("height") or 0)
            photographer = ph.get("photographer") or ""
            out.append(Candidate(
                source=self.name,
                source_id=str(ph.get("id") or ""),
                source_url=str(ph.get("url") or ""),
                download_url=str(url),
                kind="image",
                width=w,
                height=h,
                duration=0.0,
                creator=str(photographer),
                license="Pexels License",
                source_tags=query,
                thumbnail_url=str(src_set.get("medium") or url),
                extra={"alt": ph.get("alt") or ""},
            ))
        return out
