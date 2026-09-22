"""Pixabay video stock source.

Wraps the Pixabay Videos API (https://pixabay.com/api/docs/#api_videos).
Free key required (set ``PIXABAY_API_KEY`` env). License is the Pixabay
Content License — free for commercial use, no attribution required.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .base import Candidate, SearchFilters, http_get_json, stream_download

_API_URL = "https://pixabay.com/api/videos/"
_API_IMAGE_URL = "https://pixabay.com/api/"  # photo API root (no /videos suffix)


def _select_best_variant(videos_dict: dict,
                          min_width: Optional[int]) -> Optional[dict]:
    """Pick the smallest variant that still meets ``min_width``.

    Pixabay returns 4 variants (large/medium/small/tiny). Same logic as
    Pexels: take the smallest acceptable, fall back to largest if none
    pass the floor."""
    by_size: list[tuple[int, dict]] = []
    for label in ("tiny", "small", "medium", "large"):
        v = videos_dict.get(label) or {}
        url = v.get("url") or ""
        w = int(v.get("width") or 0)
        if url and w > 0:
            by_size.append((w, v))
    if not by_size:
        return None
    by_size.sort(key=lambda p: p[0])
    floor = int(min_width or 0)
    for w, v in by_size:
        if w >= floor:
            return v
    return by_size[-1][1]


class PixabaySource:
    """Pixabay video adapter."""

    name = "pixabay_video"
    display_name = "Pixabay (videos)"
    install_instructions = (
        "Set PIXABAY_API_KEY in Settings (free key at https://pixabay.com/api/docs/)."
    )

    def is_available(self) -> bool:
        return bool(os.environ.get("PIXABAY_API_KEY"))

    def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not query.strip():
            return []
        api_key = os.environ.get("PIXABAY_API_KEY", "")
        if not api_key:
            return []
        if filters.kind == "image":
            return self._search_images(query, filters, api_key)
        if filters.kind not in ("video", "any"):
            return []

        params: dict = {
            "key": api_key,
            "q": query,
            "per_page": max(3, min(int(filters.per_page or 20), 200)),
            "page": max(1, int(filters.page or 1)),
            "safesearch": "true",
            "video_type": "film",
        }
        if filters.min_width:
            params["min_width"] = int(filters.min_width)

        data = http_get_json(_API_URL, params=params)
        out: list[Candidate] = []
        for hit in data.get("hits") or []:
            best = _select_best_variant(hit.get("videos") or {}, filters.min_width)
            if best is None:
                continue
            duration = float(hit.get("duration") or 0.0)
            # Pixabay max_duration: filter client-side (their API has no native filter)
            if filters.max_duration is not None and duration > float(filters.max_duration):
                continue
            if filters.min_duration is not None and duration < float(filters.min_duration):
                continue
            # orientation: derive from width/height (no API filter available)
            w = int(best.get("width") or 0)
            h = int(best.get("height") or 0)
            if filters.orientation == "landscape" and w < h:
                continue
            if filters.orientation == "portrait" and w > h:
                continue
            if filters.orientation == "square" and abs(w - h) / max(w, h, 1) > 0.05:
                continue

            tags_csv = str(hit.get("tags") or "")
            out.append(Candidate(
                source=self.name,
                source_id=str(hit.get("id") or ""),
                source_url=str(hit.get("pageURL") or ""),
                download_url=str(best.get("url") or ""),
                kind="video",
                width=w,
                height=h,
                duration=duration,
                creator=str(hit.get("user") or ""),
                license="Pixabay License",
                source_tags=tags_csv,
                thumbnail_url=str(best.get("thumbnail") or ""),
                extra={
                    "views": hit.get("views"),
                    "downloads": hit.get("downloads"),
                    "likes": hit.get("likes"),
                    "size_bytes": best.get("size"),
                },
            ))
        return out

    def download(self, candidate: Candidate, out_path: Path) -> Path:
        return stream_download(candidate.download_url, Path(out_path))

    def _search_images(
        self, query: str, filters: SearchFilters, api_key: str,
    ) -> list[Candidate]:
        """

        Same key as the videos endpoint. Pixabay's photo endpoint is the
        root /api/ with image_type=photo. Returns Candidate(kind='image',
        duration=0) so the orchestrator's image-cascade can distinguish
        these from videos and run them through ImageMotionService.
        """
        params: dict = {
            "key": api_key,
            "q": query,
            "per_page": max(3, min(int(filters.per_page or 20), 200)),
            "page": max(1, int(filters.page or 1)),
            "safesearch": "true",
            "image_type": "photo",
        }
        if filters.min_width:
            params["min_width"] = int(filters.min_width)
        if filters.orientation == "landscape":
            params["orientation"] = "horizontal"
        elif filters.orientation == "portrait":
            params["orientation"] = "vertical"

        data = http_get_json(_API_IMAGE_URL, params=params)
        out: list[Candidate] = []
        for hit in data.get("hits") or []:
            url = str(hit.get("largeImageURL") or hit.get("webformatURL") or "")
            if not url:
                continue
            w = int(hit.get("imageWidth") or hit.get("webformatWidth") or 0)
            h = int(hit.get("imageHeight") or hit.get("webformatHeight") or 0)
            if filters.orientation == "landscape" and w < h:
                continue
            if filters.orientation == "portrait" and w > h:
                continue
            tags_csv = str(hit.get("tags") or "")
            out.append(Candidate(
                source=self.name,
                source_id=str(hit.get("id") or ""),
                source_url=str(hit.get("pageURL") or ""),
                download_url=url,
                kind="image",
                width=w,
                height=h,
                duration=0.0,
                creator=str(hit.get("user") or ""),
                license="Pixabay License",
                source_tags=tags_csv,
                thumbnail_url=str(hit.get("previewURL") or url),
                extra={
                    "views": hit.get("views"),
                    "downloads": hit.get("downloads"),
                },
            ))
        return out
