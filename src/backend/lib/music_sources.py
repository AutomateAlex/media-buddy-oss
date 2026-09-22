"""

Replaces the AGPLv3-licensed OM tools.audio.{pixabay_music,freesound_music}.
Same conceptual job: take a query + duration window, return a downloaded
royalty-free music track. Cleaner interface than OM's tool/execute shape:
direct ``search_and_download(query, ...) -> MusicTrack | None``.

Two sources:
  - ``PixabayMusicSource`` — no API key needed for the public search page.
    Uses Pixabay's public music search and grabs the mp3 URLs from the
    response HTML/JSON. License: Pixabay Content License (commercial OK).
  - ``FreesoundMusicSource`` — needs ``FREESOUND_API_KEY``. Returns CC-
    licensed sounds (CC0, CC-BY) from freesound.org's API.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .stock_sources.base import http_get_json, stream_download

logger = logging.getLogger(__name__)


@dataclass
class MusicTrack:
    """A downloaded music track and its metadata."""
    local_path: str
    title: str
    duration: float
    license: str
    source_url: str
    source: str = ""               # "pixabay_music" | "freesound"
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# Pixabay Music — Pixabay does NOT publish a JSON music API. Their public
# music search page emits ``window.__BOOTSTRAP_URL__ = "/api/.../bootstrap"``
# pointing at a per-page JSON endpoint that carries the track list. We
# follow that pattern: fetch the search page (sets cookies), discover
# the bootstrap URL, fetch JSON, pick a track within duration window,
# stream the mp3.
# ---------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Pixabay's bot detector inspects Sec-Ch-Ua / Sec-Fetch-* — a bare
# python-requests UA gets 403. Mimic Chrome's full navigation headers.
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class PixabayMusicSource:
    """Pixabay royalty-free music. No API key — scrapes the public search
    page's bootstrap JSON. Stability is "experimental" because Pixabay's
    HTML can change without notice; we ship Freesound as a backup source.
    """

    name = "pixabay_music"
    display_name = "Pixabay Music"

    def is_available(self) -> bool:
        return True

    def search_and_download(
        self,
        query: str,
        output_path: Path,
        min_duration: float = 30.0,
        max_duration: float = 600.0,
    ) -> Optional[MusicTrack]:
        if not query.strip():
            return None
        tracks = self._discover_tracks(query)
        if not tracks:
            return None
        chosen = self._pick_in_window(tracks, min_duration, max_duration)
        if chosen is None:
            return None
        url = chosen.get("audio_url")
        if not url:
            return None
        try:
            import requests
            with requests.Session() as s:
                s.headers.update({"User-Agent": _USER_AGENT,
                                   "Referer": "https://pixabay.com/music/"})
                r = s.get(url, stream=True, timeout=120)
                r.raise_for_status()
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            logger.warning("Pixabay music download failed: %s", e)
            return None
        return MusicTrack(
            local_path=str(output_path),
            title=str(chosen.get("title") or "Pixabay Track"),
            duration=float(chosen.get("duration") or 0.0),
            license="Pixabay Content License",
            source_url=f"https://pixabay.com/music/search/?q={query.replace(' ', '+')}",
            source=self.name,
        )

    def _discover_tracks(self, query: str) -> list[dict]:
        """Return [{title, audio_url, duration, ...}, ...] for a query.

        Pixabay does TLS-fingerprint-level bot detection that flags the
        `requests` library (HTTP/2 ALPN + cipher list distinct from real
        Chrome). Stdlib ``urllib`` is in their allowlist for now, so we
        use it for the HTML+bootstrap fetches. The mp3 download itself
        goes through a CDN with looser checks — `requests` works there.
        """
        import json as _json
        import urllib.request
        import http.cookiejar
        from urllib.parse import quote

        slug = quote(re.sub(r"\s+", "-", query.strip().lower()), safe="-")
        search_url = f"https://pixabay.com/music/search/{slug}/"
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        def _open(url: str, accept_json: bool = False, referer: str | None = None) -> str:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", _USER_AGENT)
            for k, v in _BROWSER_HEADERS.items():
                req.add_header(k, v)
            if accept_json:
                req.add_header("Accept", "application/json, text/plain, */*")
                req.add_header("Sec-Fetch-Dest", "empty")
                req.add_header("Sec-Fetch-Mode", "cors")
                req.add_header("Sec-Fetch-Site", "same-origin")
            if referer:
                req.add_header("Referer", referer)
            with opener.open(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")

        # Step 1: fetch search page (sets cookies)
        try:
            html = _open(search_url)
        except Exception as e:
            logger.warning("Pixabay search page fetch failed: %s", e)
            return []

        # Step 2: bootstrap JSON
        m = re.search(r'window\.__BOOTSTRAP_URL__\s*=\s*["\']([^"\']+)["\']', html)
        if m:
            bp = m.group(1)
            bootstrap_url = f"https://pixabay.com{bp}" if bp.startswith("/") else bp
            try:
                body = _open(bootstrap_url, accept_json=True, referer=search_url)
                data = _json.loads(body)
                results = (data.get("page") or {}).get("results") or []
                tracks = []
                for item in results:
                    src = (item.get("sources") or {}).get("src")
                    if not src:
                        continue
                    tracks.append({
                        "title": item.get("name") or "Unknown",
                        "audio_url": src,
                        "duration": float(item.get("duration") or 0.0),
                        "id": item.get("id"),
                    })
                if tracks:
                    return tracks
            except Exception as e:
                logger.info("Pixabay bootstrap parse failed (%s) — trying scrape", e)

        # Step 3 fallback: brute-force CDN URL scan
        mp3_urls = re.findall(
            r'(https?://cdn\.pixabay\.com/audio/[^\s"\'<>]+\.mp3)', html,
        )
        seen, tracks = set(), []
        for url in mp3_urls:
            if url in seen:
                continue
            seen.add(url)
            tracks.append({"title": query, "audio_url": url, "duration": 0.0})
        return tracks

    @staticmethod
    def _pick_in_window(tracks: list[dict], lo: float, hi: float) -> Optional[dict]:
        """Pick the first track within [lo, hi] seconds. If duration is
        unknown (0), accept it — fallback scrape doesn't carry duration."""
        for t in tracks:
            d = float(t.get("duration") or 0.0)
            if d == 0.0 or lo <= d <= hi:
                return t
        return tracks[0] if tracks else None


# ---------------------------------------------------------------------
# Freesound — proper API, requires key
# ---------------------------------------------------------------------

_FREESOUND_SEARCH = "https://freesound.org/apiv2/search/text/"


class FreesoundMusicSource:
    """Freesound CC-licensed sounds. Requires ``FREESOUND_API_KEY``
    (free at https://freesound.org/apiv2/apply/)."""

    name = "freesound"
    display_name = "Freesound (CC)"

    def is_available(self) -> bool:
        return bool(os.environ.get("FREESOUND_API_KEY"))

    def search_and_download(
        self,
        query: str,
        output_path: Path,
        min_duration: float = 30.0,
        max_duration: float = 600.0,
    ) -> Optional[MusicTrack]:
        if not query.strip():
            return None
        api_key = os.environ.get("FREESOUND_API_KEY", "")
        if not api_key:
            return None
        try:
            data = http_get_json(
                _FREESOUND_SEARCH,
                params={
                    "query": query,
                    "filter": (
                        # 字体/音乐版权 — only CC0 (true public-domain dedication):
                        # no attribution, no non-commercial limits. "Attribution"
                        # (CC-BY, needs credit) and "Attribution Noncommercial"
                        # (CC-BY-NC, forbids commercial use) are unsafe for videos
                        # users distribute commercially, so they're excluded.
                        f"duration:[{int(min_duration)} TO {int(max_duration)}] "
                        f"license:(\"Creative Commons 0\")"
                    ),
                    "fields": "id,name,duration,license,url,previews",
                    "page_size": 15,
                    "token": api_key,
                },
                headers={"User-Agent": _USER_AGENT},
                timeout=20.0,
            )
        except Exception as e:
            logger.warning("Freesound search failed: %s", e)
            return None

        results = data.get("results") or []
        if not results:
            return None
        track = results[0]
        # Use the high-quality preview (mp3 ~128kbps) — full source download
        # requires OAuth, but previews are CDN URLs free-for-use under same license.
        previews = track.get("previews") or {}
        url = (
            previews.get("preview-hq-mp3")
            or previews.get("preview-lq-mp3")
            or ""
        )
        if not url:
            return None
        try:
            stream_download(url, Path(output_path),
                              headers={"User-Agent": _USER_AGENT})
        except Exception as e:
            logger.warning("Freesound download failed: %s", e)
            return None
        return MusicTrack(
            local_path=str(output_path),
            title=str(track.get("name") or "Freesound Track"),
            duration=float(track.get("duration") or 0.0),
            license=str(track.get("license") or "Creative Commons"),
            source_url=str(track.get("url") or ""),
            source=self.name,
        )
