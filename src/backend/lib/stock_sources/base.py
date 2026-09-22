"""Stock source protocol — the small contract every adapter satisfies.

A ``StockSource`` exposes three behaviors:
  - ``is_available()`` — cheap pre-check (env keys, optional deps)
  - ``search(query, filters)`` — return ``list[Candidate]`` (no download)
  - ``download(candidate, out_path)`` — fetch the file, return final path

Adapters are intentionally dumb: convert API JSON → ``Candidate``. No
ranking, no de-dup, no filtering beyond what the upstream API itself
accepts. Caller (FootageService) handles cross-source merging, scoring,
caching, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class Candidate:
    """A pre-download search result, normalized across providers."""

    source: str                                          # adapter name e.g. "pexels"
    source_id: str                                       # unique within that source
    source_url: str                                      # human-readable landing page
    download_url: str                                    # direct file URL
    kind: str = "video"                                  # "video" | "image"
    width: int = 0
    height: int = 0
    duration: float = 0.0                                # seconds (0 for images)
    creator: str = ""                                    # attribution name
    license: str = ""                                    # license string / URL
    source_tags: str = ""                                # title + tags joined
    thumbnail_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific

    @property
    def clip_id(self) -> str:
        return f"{self.source}_{self.source_id}"


@dataclass
class SearchFilters:
    """Filters a source MAY apply when searching. Adapters ignore what
    they don't support — caller assumes liberal filtering and accepts
    that some providers will return slightly out-of-spec results."""

    kind: str = "video"                       # "video" | "image" | "any"
    min_duration: Optional[float] = None      # seconds
    max_duration: Optional[float] = None      # seconds
    orientation: Optional[str] = None         # "landscape" | "portrait" | "square"
    min_width: Optional[int] = None
    per_page: int = 20
    page: int = 1


@runtime_checkable
class StockSource(Protocol):
    """Every adapter satisfies this small protocol."""

    name: str

    def is_available(self) -> bool: ...

    def search(self, query: str, filters: SearchFilters) -> list[Candidate]: ...

    def download(self, candidate: Candidate, out_path: Path) -> Path: ...


# ---------- Helpers shared by adapters ----------

_DEFAULT_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB


def stream_download(url: str, out_path: Path,
                     headers: Optional[dict] = None,
                     timeout: float = 120.0,
                     chunk_size: int = 1 << 16,
                     max_bytes: Optional[int] = _DEFAULT_MAX_DOWNLOAD_BYTES) -> Path:
    """Download ``url`` to ``out_path`` (streaming, no whole-file in memory).

    Creates parent dirs as needed. Raises on HTTP errors. Returns the
    final path on success.

    so every stock adapter gets safe-by-default protection — even ones
    that forget to filter by duration or size at search time. Calibrated
    for our short-form output: a 60-second 1080p clip is ~30-80 MB, a
    60-second 4K clip ~150-200 MB. Anything larger is a multi-minute
    documentary that does not belong in a YouTube Short.

    Pass ``max_bytes=None`` explicitly to disable the cap (do not do
    this for stock-source downloads). Adapters with their own tighter
    ceiling (wikimedia, archive_org) override with the same value to
    keep the intent visible at the call site.

    Implementation: pre-checks Content-Length upfront and aborts
    mid-stream if cumulative bytes exceed the cap (catches servers that
    lie about Content-Length).
    """
    import requests
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        if max_bytes is not None:
            cl = r.headers.get("Content-Length")
            if cl and int(cl) > max_bytes:
                raise ValueError(
                    f"download rejected: Content-Length={cl} exceeds max_bytes={max_bytes}"
                )
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if max_bytes is not None and downloaded > max_bytes:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"download aborted at {downloaded} bytes (cap {max_bytes})"
                        )
    return out_path


def http_get_json(url: str, params: Optional[dict] = None,
                   headers: Optional[dict] = None,
                   timeout: float = 30.0) -> dict:
    """GET ``url`` and return parsed JSON. Raises on HTTP errors."""
    import requests
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()
