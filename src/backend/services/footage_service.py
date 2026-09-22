"""Multi-source footage service.

Fans out each scene query across Pexels + Pixabay + Coverr in parallel,
collects candidates, picks the best one (by source priority), downloads it.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Phase 2.11f — fully decoupled from OpenMontage; stock adapters now live
# in backend.lib.stock_sources (in-house, MIT-compatible).

logger = logging.getLogger(__name__)


@dataclass
class FootageResult:
    """Result from fetching footage for a single scene."""
    scene_index: int
    query: str
    local_path: Optional[str] = None
    duration: float = 0.0
    resolution: str = ""
    source: str = ""           # which adapter served this clip ("pexels" / "pixabay" / "coverr")
    source_id: str = ""
    source_url: str = ""
    source_tags: str = ""
    error: Optional[str] = None


@dataclass
class FootageCandidate:
    """
    so we can score multiple candidates before paying download bandwidth."""
    source: str                # adapter name, e.g. "pexels"
    source_id: str
    source_url: str
    thumbnail_url: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    license: str = ""
    query: str = ""
    source_tags: str = ""
    # Direct download URL from the source Candidate. May be empty, in which
    # case the asset is fetched via source.download().
    download_url: str = ""
    # Subject horizontal position (0.0=left .. 0.5=center .. 1.0=right) in the
    # thumbnail, filled in by the observer vision judge after the candidate is
    # accepted, so portrait reframing can crop toward the subject. None=unknown.
    subject_h_pos: Optional[float] = None
    # Hold onto the raw OM Candidate so the orchestrator can call source.download()
    _raw: object = None


# Production stock is limited to mainstream, controllable sources. Public
# archive adapters remain importable for old code/tests, but they are not
# instantiated or searched in production flows.
PRODUCTION_STOCK_SOURCES = {
    "pexels",
    "pixabay_video",
    "coverr",
}

# 只用 Pexels / Pixabay / Coverr。其余全部不用。
# 免费商用·免署名。已弃用 wikimedia(CC-BY-SA 须署名)、archive_org/loc
# (per-item varies)、nasa/nara/pond5_pd(公有领域，质量/风格不合，不用)。
SOURCE_PRIORITY = ["pexels", "pixabay_video", "coverr"]
SECONDARY_SOURCE_PRIORITY = list(SOURCE_PRIORITY)


# Sources that raise this many search-time exceptions within a single
# FootageService run get parked for the rest of that run (per-process cooldown).
# We don't bother with time-based cooldowns because a FootageService is only
# alive for one pipeline run.
SOURCE_FAILURE_BUDGET = 2


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _library_clip_to_candidate(clip, query: str) -> "FootageCandidate":
    """Wrap a LibraryClip ORM row as a FootageCandidate. The orchestrator
    treats this exactly like a stock candidate (mm_critic still scores it,
    moderation still runs against the current Series), but download_candidate
    short-circuits to the existing local file instead of fetching."""
    source_tags = " ".join([
        str(getattr(clip, "category", "") or ""),
        str(getattr(clip, "description", "") or ""),
        " ".join(getattr(clip, "tags", []) or []),
        " ".join(getattr(clip, "mood_tags", []) or []),
        " ".join(getattr(clip, "motion_tags", []) or []),
        Path(str(getattr(clip, "local_path", "") or "")).name,
    ]).strip()
    return FootageCandidate(
        source="library",
        source_id=str(getattr(clip, "id", "") or ""),
        source_url=str(getattr(clip, "source_url", "") or ""),
        thumbnail_url=str(getattr(clip, "thumbnail_path", "") or ""),
        duration=float(getattr(clip, "duration_seconds", 0.0) or 0.0),
        width=int(getattr(clip, "width", 0) or 0),
        height=int(getattr(clip, "height", 0) or 0),
        license="",
        query=query,
        source_tags=source_tags,
        _raw=clip,
    )


def _to_footage_candidate(raw_candidate, source_name: str, query: str) -> "FootageCandidate":
    """Convert OM Candidate object into our FootageCandidate dataclass."""
    return FootageCandidate(
        source=source_name,
        source_id=getattr(raw_candidate, "source_id", "") or "",
        source_url=getattr(raw_candidate, "source_url", "") or "",
        thumbnail_url=getattr(raw_candidate, "thumbnail_url", "") or "",
        duration=float(getattr(raw_candidate, "duration", 0.0) or 0.0),
        width=int(getattr(raw_candidate, "width", 0) or 0),
        height=int(getattr(raw_candidate, "height", 0) or 0),
        license=str(getattr(raw_candidate, "license", "") or ""),
        query=query,
        source_tags=str(getattr(raw_candidate, "source_tags", "") or ""),
        download_url=str(getattr(raw_candidate, "download_url", "") or ""),
        _raw=raw_candidate,
    )


class FootageService:
    """Multi-source stock footage aggregator.

    Phase 2.9a — accepts an optional `library` (LibraryService). When set,
    `search_top_n` checks the local indexed library first (L0 cache) before
    fanning out to the 9 remote sources. Library hits are returned as
    FootageCandidate(source="library") and download_candidate short-circuits
    to the existing on-disk file (no network).
    """

    def __init__(
        self,
        library: Optional[object] = None,
        source_waves: Optional[list[list[str]]] = None,
        max_clip_duration: float = 60.0,
        preferred_clip_duration: float = 30.0,
    ) -> None:
        # Phase 2.11e — wave-based search config.
        # `source_waves` is a list of source-name lists; each wave runs
        # serially. Wave N+1 only fires if Wave N produced < n candidates.
        # When None, falls back to single-wave (legacy: all sources in parallel).
        self.library = library
        self._source_waves = source_waves  # list[list[str]] | None
        self._max_clip_duration = float(max_clip_duration)
        self._preferred_clip_duration = float(preferred_clip_duration)
        # Phase 2.11f — in-house stock adapters (replaces OpenMontage).
        from backend.lib.stock_sources import (
            CoverrSource,
            PexelsSource,
            PixabaySource,
            SearchFilters,
        )

        self.SearchFilters = SearchFilters

        # Instantiate every adapter; record the available ones only. Sources marked
        # with ⚙ require an API key (set in Settings); all others run with no
        # credentials and serve public-domain or freely-licensed content.
        all_sources = [
            PexelsSource(),               # ⚙ key
            PixabaySource(),              # ⚙ key
            CoverrSource(),               # ⚙ key (auto-disabled if none)
        ]
        # Phase 2.11d — only disable sources whose endpoints are broken at
        # the SERVER side (not our config issue). Key-required sources
        # (pixabay/coverr) are gated by _safe_is_available checking placeholder
        # values, so they auto-disable when no real key is set.
        DISABLED_SOURCES: set[str] = set()
        # the operator skip specific sources at runtime without code changes.
        import os as _os
        _skip_env = _os.environ.get("MEDIA_BUDDY_SKIP_SOURCES", "")
        SKIP_AT_RUNTIME = {s.strip() for s in _skip_env.split(",") if s.strip()}
        if SKIP_AT_RUNTIME:
            logger.info("Footage sources skipped via env: %s", sorted(SKIP_AT_RUNTIME))
        self.sources = [
            s for s in all_sources
            if self._safe_is_available(s)
            and s.name in PRODUCTION_STOCK_SOURCES
            and s.name not in DISABLED_SOURCES
            and s.name not in SKIP_AT_RUNTIME
        ]
        # Per-run failure tracking — sources that throw too often get benched.
        self._failure_counts: dict[str, int] = {}
        self._benched: set[str] = set()
        if not self.sources:
            logger.warning("No footage sources available — pipeline will fail unless keys are set")
        else:
            logger.info("Footage sources active: %s", [s.name for s in self.sources])


    # Phase 2.11d — env vars that smell like placeholders. We've seen
    # ".env" files where keys were left as "...", "your_key_here", "xxx", etc.
    # OpenMontage adapters trust os.environ presence and return is_available=True,
    # then 400 / 401 on every call. Reject obvious placeholders here so the
    # source pool reflects reality.
    _PLACEHOLDER_KEY_PREFIXES = (
        "...", "xxx", "your_", "<", "todo", "fixme", "placeholder",
    )

    @classmethod
    def _is_placeholder_value(cls, val: str) -> bool:
        if not val:
            return True
        v = val.strip().lower()
        if len(v) < 16:  # real API keys are 20+ chars
            return True
        return any(v.startswith(p) for p in cls._PLACEHOLDER_KEY_PREFIXES)

    # Map source name → env var that holds its credential. Only sources requiring
    # a key are listed.
    #   - Coverr started returning 401 even on the documented "free tier".
    #     Their API now requires auth — gate on COVERR_API_KEY.
    _SOURCE_KEY_ENV = {
        "pexels": "PEXELS_API_KEY",
        "pixabay_video": "PIXABAY_API_KEY",
        "coverr": "COVERR_API_KEY",
    }

    @classmethod
    def _safe_is_available(cls, source) -> bool:
        """Some adapters import optional deps inside is_available; isolate any crash.
        Phase 2.11d — also rejects placeholder API keys to avoid 400/401 spam."""
        import os
        name = getattr(source, "name", "")
        key_env = cls._SOURCE_KEY_ENV.get(name)
        if key_env and not bool(getattr(source, "uses_cloud_gateway", False)):
            val = os.environ.get(key_env, "")
            if cls._is_placeholder_value(val):
                logger.info(
                    "Source %s skipped: %s is empty / placeholder. "
                    "Get a free key at the source's docs page to enable.",
                    name, key_env,
                )
                return False
        try:
            return bool(source.is_available())
        except Exception as e:
            logger.warning("Source %s is_available() raised: %s", name, e)
            return False

    def is_available(self) -> bool:
        return bool(self.sources)

    @property
    def source_names(self) -> list[str]:
        return [s.name for s in self.sources]

    def fetch_for_scenes(
        self,
        scene_queries: list[str],
        output_dir: Path,
        orientation: str = "landscape",
        clips_per_scene: int = 1,
        exclude_sources: Optional[set[str]] = None,
    ) -> list[FootageResult]:
        """Fetch best matching clip per scene query, parallel across sources.
        ``exclude_sources`` lets callers (e.g. ChunkEngine retry loop) ban
        sources already tried for this shot."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Phase 2.11e — server-side max_duration; non-honoring sources still
        # get post-filtered in _fetch_one_scene below.
        # 240×426 thumbnail-quality variant and call it a day. Output
        # canvas is 720×1280 (portrait) / 1280×720 (landscape); a 240-wide
        # source upscaled 3× looks like a smear.
        filters = self.SearchFilters(
            kind="video",
            orientation=orientation if orientation in ("landscape", "portrait", "square") else None,
            per_page=max(clips_per_scene, 3),
            max_duration=self._max_clip_duration,
            min_width=720,
        )

        results: list[FootageResult] = []
        for scene_idx, query in enumerate(scene_queries):
            results.append(
                self._fetch_one_scene(scene_idx, query, output_dir, filters, exclude_sources)
            )
        return results

    def _fetch_one_scene(
        self, scene_idx: int, query: str, output_dir: Path, filters,
        exclude_sources: Optional[set[str]] = None,
    ) -> FootageResult:
        """Search all sources in parallel, return the best candidate downloaded."""
        if not self.sources:
            return FootageResult(
                scene_index=scene_idx, query=query,
                error="No footage sources available",
            )

        excluded = set(exclude_sources or ())
        active = [
            s for s in self.sources
            if s.name not in self._benched and s.name not in excluded
        ]
        if not active:
            return FootageResult(
                scene_index=scene_idx, query=query,
                error=(
                    "No footage sources available "
                    f"(benched={sorted(self._benched)} excluded={sorted(excluded)})"
                ),
            )

        # Phase 2.11e — wave-based search (legacy fetch_for_scenes path).
        # Stop early on first non-empty wave; we only need 1 best candidate
        # for this scene anyway.
        waves = self._compute_waves(active)
        candidates_by_source: dict[str, list] = {}
        for wave_idx, wave_sources in enumerate(waves):
            wave_results = self._run_wave(wave_sources, query, filters)
            candidates_by_source.update(wave_results)
            if candidates_by_source:
                # We have something — pick best and skip further waves.
                logger.info(
                    "_fetch_one_scene %r wave %d/%d found %d sources with results; stopping",
                    query, wave_idx + 1, len(waves), len(candidates_by_source),
                )
                break

        if not candidates_by_source:
            return FootageResult(
                scene_index=scene_idx, query=query,
                error=f"All sources returned 0 candidates for: {query}",
            )

        # Pick best candidate using source priority.
        chosen_source_name, chosen_candidate = self._pick_best(candidates_by_source)
        chosen_source = next((s for s in self.sources if s.name == chosen_source_name), None)
        if chosen_source is None:
            return FootageResult(
                scene_index=scene_idx, query=query,
                error="Internal: chosen source disappeared",
            )

        # Download
        filename = f"scene_{scene_idx:03d}_{chosen_source.name}_{chosen_candidate.source_id}.mp4"
        local_path = output_dir / filename
        try:
            actual_path = chosen_source.download(chosen_candidate, local_path)
            return FootageResult(
                scene_index=scene_idx,
                query=query,
                local_path=str(actual_path),
                duration=chosen_candidate.duration,
                resolution=f"{chosen_candidate.width}x{chosen_candidate.height}"
                if chosen_candidate.width else "",
                source=chosen_source.name,
                source_id=chosen_candidate.source_id,
                source_url=chosen_candidate.source_url,
                source_tags=getattr(chosen_candidate, "source_tags", "") or "",
            )
        except Exception as e:
            logger.warning("Download failed (%s): %s", chosen_source.name, e)
            return FootageResult(
                scene_index=scene_idx, query=query,
                source=chosen_source.name,
                error=f"Download failed from {chosen_source.name}: {e}",
            )

    def search_top_n(
        self,
        query: str,
        n: int = 10,
        orientation: str = "landscape",
        exclude_sources: Optional[set[str]] = None,
    ) -> list[FootageCandidate]:
        """Phase 2.6 — search 9 sources in parallel, merge candidates, return
        top N (no download). Caller scores then downloads only the winner.

        Phase 2.9a — checks local library FIRST (L0 cache). If we have at
        least 1 library hit, prepend them so the orchestrator scores them
        first. Library hits' download_candidate() returns the existing path
        without hitting the network.

        Order: source priority preserves preferred-first ordering, so candidate[0]
        is best Pexels, then Pixabay, etc.
        """
        excluded = set(exclude_sources or ())
        effective_orientation = (
            orientation if orientation == "landscape" else None
        )
        filters = self.SearchFilters(
            kind="video",
            orientation=effective_orientation,
            per_page=max(3, min(n, 10)),
            max_duration=self._max_clip_duration,
            min_width=720,
        )

        # ---- L0: local library cache ----
        # Phase 2.11e — match the relaxation we did for stock sources: for
        # portrait/square targets, DON'T hard-filter on aspect_ratio. The
        # downstream ReframeService can letterbox a 16:9 library clip to 9:16.
        # Hard-filtering here was causing 86 indexed 16:9 clips to be ignored
        # when the user asked for shorts — only the 36 native 9:16 clips
        # showed up, even when many 16:9 had a perfect topical match.
        # For landscape targets we still keep the filter (most stock is 16:9
        # natively, free precision). Also pull the full n clips, not min(n, 5)
        # — relevance is the bottleneck, not candidate count.
        library_candidates: list[FootageCandidate] = []
        if self.library is not None:
            aspect_filter = "16:9" if orientation == "landscape" else None
            try:
                lib_clips = self.library.search(
                    query, top_n=max(n, 10),
                    aspect_ratio=aspect_filter, only_indexed=True,
                )
            except Exception as e:
                logger.warning("library L0 search failed: %s", e)
                lib_clips = []
            for clip in lib_clips:
                library_candidates.append(_library_clip_to_candidate(clip, query))
            if library_candidates:
                logger.info(
                    "L0 library hits for %r (orientation=%s): %d clips",
                    query, orientation, len(library_candidates),
                )
            else:
                logger.info(
                    "L0 library miss for %r (orientation=%s) — falling through to stock",
                    query, orientation,
                )

        # If library alone covers our needs, skip the remote fan-out entirely.
        if len(library_candidates) >= n:
            return library_candidates[:n]

        if not self.sources:
            return library_candidates[:n]
        active = [
            s for s in self.sources
            if s.name not in self._benched and s.name not in excluded
        ]
        if not active:
            return library_candidates[:n]
        # Phase 2.11 — when target is portrait/square, do NOT hard-filter on
        # orientation. The downstream orchestrator letterboxes 16:9 sources
        # to 9:16 (or 1:1) via ReframeService. Filtering at search time would
        # collapse the candidate pool ~10x because portrait stock is sparse.
        # For landscape targets we keep the filter (most stock is landscape
        # natively, so it's free precision).
        # Phase 2.11e — pass max_duration server-side. Sources that honor it
        # (Pexels, Pixabay) trim the result list before returning. Others
        # ignore it and we filter post-search via _filter_candidates_by_duration.

        # Phase 2.11e — wave-based search. Run waves serially; only advance
        # to the next wave if the current cumulative pool is < n usable
        # candidates. Caps simultaneous SSL handshakes per chunk at the
        # widest wave's size (typically 2-3 sources), down from "all 9 at once".
        waves = self._compute_waves(active)
        candidates_by_source: dict[str, list] = {}
        for wave_idx, wave_sources in enumerate(waves):
            wave_results = self._run_wave(wave_sources, query, filters)
            for src_name, cands in wave_results.items():
                # Same source appearing in multiple waves: merge candidates,
                # preserving order. (Shouldn't happen with a sane config but
                # be safe.)
                existing = candidates_by_source.get(src_name) or []
                candidates_by_source[src_name] = existing + cands
            cumulative = sum(len(v) for v in candidates_by_source.values())
            wave_names = [s.name for s in wave_sources]
            logger.info(
                "search_top_n %r wave %d/%d (%s) → +%d candidates, total=%d (target=%d)",
                query, wave_idx + 1, len(waves), wave_names,
                sum(len(v) for v in wave_results.values()), cumulative, n,
            )
            if cumulative + len(library_candidates) >= n:
                # Have enough — skip remaining waves to save bandwidth + time.
                break

        # Library hits first, then the remaining stock pool.
        merged: list[FootageCandidate] = list(library_candidates)
        remaining = n - len(merged)
        if remaining <= 0:
            return merged[:n]

        # Phase 2.11d — round-robin merge across sources to maximize diversity.
        # Old behavior was strict priority: take all of Pexels (8), then all of
        # because it filled first. With round-robin, the candidate pool gets
        # 1 from each source per round, so n=8 with 6 active sources yields
        # something like Pexels:2 / Pixabay:2 / Coverr:1 /
        # NASA:1 / LOC:1 — much more diverse and resilient to Pexels rate
        # limits or moderation blocks.
        queues: dict[str, list] = {}
        ordered_sources: list[str] = []
        # Priority order first (Pexels gets first pick in each round)
        for s in SOURCE_PRIORITY:
            if candidates_by_source.get(s):
                queues[s] = list(candidates_by_source[s])
                ordered_sources.append(s)
        # Any leftover sources not in priority list
        for s, cands in candidates_by_source.items():
            if s not in queues and cands:
                queues[s] = list(cands)
                ordered_sources.append(s)

        while len(merged) < n:
            added_this_round = False
            for source_name in ordered_sources:
                if queues[source_name]:
                    cand = queues[source_name].pop(0)
                    merged.append(_to_footage_candidate(cand, source_name, query))
                    added_this_round = True
                    if len(merged) >= n:
                        break
            if not added_this_round:
                break  # all sources exhausted
        return merged

    def download_candidate(
        self, candidate: FootageCandidate, output_dir: Path,
    ) -> Optional[Path]:
        """Download a chosen FootageCandidate. Returns local path or None.

        Phase 2.9a — `source == "library"` short-circuits: the file already
        exists locally; we just bump used_count and return its path. No
        network round-trip, no copy.
        """
        # L0: library hit — file is already on disk under ~/.media-buddy-oss/library/clips/
        if candidate.source == "library" and candidate._raw is not None:
            try:
                local_path = Path(getattr(candidate._raw, "local_path", "") or "")
                if not local_path.exists():
                    logger.warning(
                        "library candidate %s missing on disk: %s",
                        candidate.source_id, local_path,
                    )
                    return None
                if self.library is not None:
                    try:
                        self.library.bump_used(candidate.source_id)
                    except Exception as e:
                        logger.warning("library bump_used failed: %s", e)
                return local_path
            except Exception as e:
                logger.warning("library download_candidate failed: %s", e)
                return None

        source = next((s for s in self.sources if s.name == candidate.source), None)
        if source is None or candidate._raw is None:
            return None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{candidate.source}_{candidate.source_id}.mp4"
        local_path = output_dir / filename
        try:
            actual = source.download(candidate._raw, local_path)
            return Path(actual) if actual else None
        except Exception as e:
            logger.warning("download_candidate %s failed: %s", candidate.source, e)
            return None

    @staticmethod
    def _safe_search(source, query: str, filters) -> list:
        """Call source.search() with exception isolation + content-safety gate.

        This is the single funnel through which EVERY remote stock query passes
        (search_one_wave / search_secondary all reach here
        via _run_wave), so the content-safety gate here covers short + long
        video and all sources: it (1) rewrites/drops sexual/violent/gory queries
        before they go out, and (2) rejects unsafe results before they return.
        """
        try:
            from backend.lib.content_safety_filter import (
                asset_is_unsafe,
                sanitize_query,
            )
        except Exception:
            return source.search(query, filters)
        safe_q, changed = sanitize_query(query)
        if changed:
            if not safe_q:
                logger.info("content-safety: dropped unsafe query %r", query)
                return []
            logger.info(
                "content-safety: rewrote query %r -> %r", query, safe_q
            )
            query = safe_q
        results = source.search(query, filters) or []
        kept = []
        for c in results:
            reason = asset_is_unsafe(
                getattr(c, "source_tags", "") or "",
                getattr(c, "source_url", "") or "",
            )
            if reason:
                logger.info(
                    "content-safety: dropped %s result (%s)",
                    getattr(c, "source", "?"), reason,
                )
                continue
            kept.append(c)
        return kept


    def _compute_waves(self, active_sources: list) -> list[list]:
        """Group active sources into waves per ``self._source_waves``.

        - Sources listed in the wave config but not active (benched / no key)
          are silently skipped within their wave.
        - Sources active but not mentioned in any wave get appended as a
          final "leftover" wave so they're never permanently shut out.
        - When no wave config is provided, returns a single wave containing
          every active source (legacy behavior).
        """
        if not self._source_waves:
            return [active_sources]
        name_to_src = {s.name: s for s in active_sources}
        used: set[str] = set()
        waves: list[list] = []
        for wave_names in self._source_waves:
            wave = [name_to_src[n] for n in wave_names if n in name_to_src]
            if wave:
                waves.append(wave)
                used.update(s.name for s in wave)
        leftover = [s for s in active_sources if s.name not in used]
        if leftover:
            waves.append(leftover)
        return waves or [active_sources]

    def _filter_candidates_by_duration(self, raw_candidates: list) -> list:
        """Drop candidates with duration > self._max_clip_duration.

        Kept stable in original source-relevance order. Candidates with
        unknown / zero duration pass through (we can't filter what we don't
        know — let mm_critic catch obvious mismatches instead)."""
        max_d = self._max_clip_duration
        if max_d <= 0:
            return raw_candidates
        kept = []
        for c in raw_candidates:
            d = float(getattr(c, "duration", 0.0) or 0.0)
            if d <= 0 or d <= max_d:
                kept.append(c)
        return kept

    def num_active_waves(
        self, exclude_sources: Optional[set[str]] = None,
    ) -> int:
        """How many waves the search loop would iterate, given current
        bound its wave loop."""
        excluded = set(exclude_sources or ())
        active = [
            s for s in self.sources
            if s.name not in self._benched and s.name not in excluded
        ]
        return len(self._compute_waves(active))

    def search_one_wave(
        self,
        query: str,
        wave_idx: int,
        orientation: str,
        exclude_sources: Optional[set[str]] = None,
    ) -> list["FootageCandidate"]:
        """Run a SINGLE wave's remote search + optional library prefix.

        search_top_n with caller-controlled progression. The orchestrator
        calls this per wave, runs harness on the accumulated pool, and
        decides whether the result is good enough or whether to advance
        to the next wave. blind_observe cache (#3) makes incremental
        re-ranking ~free for already-seen candidates.

        On wave_idx=0, library hits are prepended (free local cache).
        Returns candidates as FootageCandidate dataclass instances,
        duration-filtered, in source-relevance order.
        """
        excluded = set(exclude_sources or ())
        effective_orientation = (
            orientation if orientation == "landscape" else None
        )
        filters = self.SearchFilters(
            kind="video",
            orientation=effective_orientation,
            per_page=8,
            max_duration=self._max_clip_duration,
            min_width=720,
        )

        out: list[FootageCandidate] = []

        # Wave 0 freebie: local library cache (no remote cost)
        if wave_idx == 0 and self.library is not None:
            aspect_filter = "16:9" if orientation == "landscape" else None
            try:
                lib_clips = self.library.search(
                    query, top_n=10,
                    aspect_ratio=aspect_filter, only_indexed=True,
                )
            except Exception as e:
                logger.warning("library L0 search failed: %s", e)
                lib_clips = []
            for clip in lib_clips:
                out.append(_library_clip_to_candidate(clip, query))
            if lib_clips:
                logger.info(
                    "search_one_wave %r wave 0 library prefix: %d hits",
                    query, len(lib_clips),
                )

        # Remote stock sources for this wave
        active = [
            s for s in self.sources
            if s.name not in self._benched and s.name not in excluded
        ]
        waves = self._compute_waves(active)
        if wave_idx >= len(waves):
            return out  # past the last wave
        wave_sources = waves[wave_idx]
        if not wave_sources:
            logger.info(
                "search_one_wave %r wave %d/%d: empty (all benched/excluded)",
                query, wave_idx + 1, len(waves),
            )
            return out
        wave_results = self._run_wave(wave_sources, query, filters)
        for src_name, cands in wave_results.items():
            for raw in cands:
                out.append(_to_footage_candidate(raw, src_name, query))

        logger.info(
            "search_one_wave %r wave %d/%d (%s) → %d remote candidates",
            query, wave_idx + 1, len(waves),
            [s.name for s in wave_sources],
            sum(len(v) for v in wave_results.values()),
        )
        return out


    def search_library(
        self,
        query: str,
        orientation: str,
        limit: int = 8,
    ) -> list["FootageCandidate"]:
        """Search the local visual asset library only.

        v2 uses this before remote stock: already-downloaded clips are free and
        render immediately.
        """
        if self.library is None or not query or not query.strip():
            return []
        aspect_filter = "16:9" if orientation == "landscape" else None
        try:
            lib_clips = self.library.search(
                query,
                top_n=max(limit, 10),
                aspect_ratio=aspect_filter,
                only_indexed=True,
            )
        except Exception as e:
            logger.warning("library v2 search failed: %s", e)
            return []
        out = [_library_clip_to_candidate(clip, query) for clip in lib_clips]
        logger.info(
            "search_library %r orientation=%s -> %d candidates",
            query, orientation, len(out),
        )
        return out[:limit]

    def search_secondary(
        self,
        query: str,
        orientation: str,
        kind: str = "video",
        limit: int = 8,
    ) -> list["FootageCandidate"]:
        """Remote stock sources (Pexels + Pixabay + Coverr), searched in parallel.

        Returns [] if no source is active.
        """
        if not query or not query.strip():
            return []
        secondary = [
            s for s in self.sources
            if (
                s.name in SECONDARY_SOURCE_PRIORITY
                and s.name not in self._benched
            )
        ]
        if not secondary:
            return []
        effective_orientation = (
            orientation if orientation == "landscape" else None
        )
        filters = self.SearchFilters(
            kind=kind,
            orientation=effective_orientation,
            per_page=limit,
            max_duration=self._max_clip_duration,
            min_width=720,
        )
        wave_results = self._run_wave(secondary, query, filters)
        out: list[FootageCandidate] = []
        for src_name, cands in wave_results.items():
            for raw in cands:
                out.append(_to_footage_candidate(raw, src_name, query))
        logger.info(
            "search_secondary %r kind=%s sources=%s → %d candidates",
            query, kind, [s.name for s in secondary],
            len(out),
        )
        return out

    def _run_wave(
        self, wave_sources: list, query: str, filters,
    ) -> dict[str, list]:
        """Run one wave's parallel search. Returns {source_name: [candidates]}.
        Filters out clips longer than self._max_clip_duration before returning.
        """
        if not wave_sources:
            return {}
        candidates_by_source: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=len(wave_sources)) as ex:
            future_to_source = {
                ex.submit(self._safe_search, src, query, filters): src
                for src in wave_sources
            }
            for fut in as_completed(future_to_source):
                src = future_to_source[fut]
                try:
                    cands = fut.result(timeout=30) or []
                    cands = self._filter_candidates_by_duration(cands)
                    if cands:
                        candidates_by_source[src.name] = cands
                    self._failure_counts.pop(src.name, None)
                except Exception as e:
                    self._record_failure(src.name, e, query)
        return candidates_by_source

    def _record_failure(self, source_name: str, exc: Exception, query: str) -> None:
        """Track per-source exception streaks. After SOURCE_FAILURE_BUDGET in a
        single run, bench the source for the rest of the FootageService's life."""
        n = self._failure_counts.get(source_name, 0) + 1
        self._failure_counts[source_name] = n
        msg = str(exc)
        # Trim long error bodies (e.g., HTML 500 pages) to keep logs readable
        if len(msg) > 200:
            msg = msg[:200] + "..."
        if n >= SOURCE_FAILURE_BUDGET and source_name not in self._benched:
            self._benched.add(source_name)
            logger.warning(
                "Source %s benched after %d failures (latest on query %r): %s",
                source_name, n, query, msg,
            )
        else:
            logger.warning(
                "Source %s search failed for %r (%d/%d): %s",
                source_name, query, n, SOURCE_FAILURE_BUDGET, msg,
            )

    @staticmethod
    def _pick_best(candidates_by_source: dict[str, list]):
        """Return (source_name, best_candidate). Source priority then top-1 from that source."""
        for preferred in SOURCE_PRIORITY:
            if preferred in candidates_by_source and candidates_by_source[preferred]:
                return preferred, candidates_by_source[preferred][0]
        # Fallback: any source with any candidate
        for name, cands in candidates_by_source.items():
            if cands:
                return name, cands[0]
        raise RuntimeError("Empty candidates_by_source despite check")
