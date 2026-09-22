"""Coverr — currently gated by COVERR_API_KEY (their public free API
returns 401 even on documented free tier as of 2026-04). Listed here for
completeness; auto-disabled when no key set."""
from __future__ import annotations

import os
from pathlib import Path

from .base import Candidate, SearchFilters, http_get_json, stream_download

_SEARCH_URL = "https://api.coverr.co/videos"


class CoverrSource:
    name = "coverr"
    display_name = "Coverr"
    install_instructions = (
        "Optional. Coverr's public API now requires an API key — "
        "set COVERR_API_KEY in Settings if you have one."
    )

    def is_available(self) -> bool:
        return bool(os.environ.get("COVERR_API_KEY"))

    def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not query.strip():
            return []
        if filters.kind not in ("video", "any"):
            return []
        api_key = os.environ.get("COVERR_API_KEY", "")
        if not api_key:
            return []
        params = {
            "query": query,
            "page_size": max(3, min(int(filters.per_page or 10), 50)),
            "page": max(1, int(filters.page or 1)),
        }
        try:
            data = http_get_json(
                _SEARCH_URL, params=params,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except Exception:
            return []
        out: list[Candidate] = []
        for v in data.get("hits") or data.get("data") or []:
            urls = v.get("urls") or v.get("video_urls") or {}
            mp4 = urls.get("mp4_download") or urls.get("mp4") or ""
            if not mp4:
                continue
            duration = float(v.get("duration") or 0.0)
            # no native duration param). Same pattern as pixabay.
            if filters.max_duration is not None and duration and duration > float(filters.max_duration):
                continue
            if filters.min_duration is not None and duration and duration < float(filters.min_duration):
                continue
            out.append(Candidate(
                source=self.name,
                source_id=str(v.get("id") or ""),
                source_url=str(v.get("url") or ""),
                download_url=str(mp4),
                kind="video",
                width=int(v.get("width") or 0),
                height=int(v.get("height") or 0),
                duration=duration,
                license=str(v.get("license") or "Coverr (varies)"),
                source_tags=str(v.get("title") or ""),
                thumbnail_url=str(v.get("poster") or ""),
            ))
        return out

    def download(self, candidate: Candidate, out_path: Path) -> Path:
        return stream_download(candidate.download_url, Path(out_path))
