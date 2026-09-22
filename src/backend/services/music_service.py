"""Music service — picks copyright-safe background music for a video.

Two stock sources via OpenMontage:
- Pixabay Music (no key, web-scrape, CC0 commercial-use)  PRIMARY
- Freesound (FREESOUND_API_KEY required, CC licenses)     FALLBACK

Music is segmented along the video's narrative arc (1-N segments). Each
segment gets its own track; segments are concatenated with crossfade by
AudioMixService.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Phase 2.11f — in-house music sources (replaces OpenMontage AGPL imports).
from backend.lib.music_sources import PixabayMusicSource, FreesoundMusicSource
from backend.services.audio_mix_service import AudioMixService

logger = logging.getLogger(__name__)


@dataclass
class MusicSegment:
    """One segment of the narrative arc that gets its own music."""
    start_chunk_index: int
    end_chunk_index: int
    start_seconds: float
    end_seconds: float
    narrative_role: str        # intro|build-up|climax|resolution|outro|transition
    mood: str                  # calm|uplifting|tense|melancholic|playful|dramatic|reflective
    energy: str                # low|medium|high
    search_keywords: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass
class MusicResult:
    """Outcome of fetching music for one segment."""
    local_path: str
    title: str
    duration: float
    source: str          # "pixabay_music" or "freesound"
    source_url: str
    license: str
    query: str


def map_segments_to_seconds(
    raw_segments: list[dict], chunk_durations: list[float],
) -> list[MusicSegment]:
    """Convert LLM-returned segments (chunk indices) into time-bounded segments.
    `chunk_durations[i]` is the TTS duration of chunk i in seconds."""
    out: list[MusicSegment] = []
    cumulative: list[float] = [0.0]
    for d in chunk_durations:
        cumulative.append(cumulative[-1] + max(0.0, float(d or 0)))
    for raw in raw_segments:
        s = int(raw["start_chunk_index"])
        e = int(raw["end_chunk_index"])
        if s < 0 or e >= len(chunk_durations) or s > e:
            continue
        out.append(MusicSegment(
            start_chunk_index=s,
            end_chunk_index=e,
            start_seconds=cumulative[s],
            end_seconds=cumulative[e + 1],
            narrative_role=str(raw.get("narrative_role", "intro")),
            mood=str(raw.get("mood", "calm")),
            energy=str(raw.get("energy", "medium")),
            search_keywords=str(raw.get("search_keywords", "ambient music")),
        ))
    return out


class MusicService:
    """Pixabay-first, Freesound-fallback music picker + segment assembler.

    Phase 2.9b — accepts an optional audio_library (AudioLibraryService).
    When set, find_for_segment checks the local library first by mood +
    energy + min_duration, before scraping Pixabay. Successful downloads
    are auto-ingested.
    """

    def __init__(
        self,
        audio_mix: Optional[AudioMixService] = None,
        audio_library: Optional[object] = None,
    ) -> None:
        self.pixabay = PixabayMusicSource()
        self.freesound = FreesoundMusicSource()
        self._audio_mix = audio_mix
        self.audio_library = audio_library


    @property
    def audio_mix(self) -> AudioMixService:
        if self._audio_mix is None:
            self._audio_mix = AudioMixService()
        return self._audio_mix

    def is_available(self) -> bool:
        # Pixabay scraping is always available; Freesound only if key set.
        # Either makes the service usable.
        return True

    def find_for_segment(
        self,
        segment: MusicSegment,
        output_dir: Path,
        segment_index: int,
    ) -> Optional[MusicResult]:
        """Licensed music only: local no-copyright pool
        → Freesound CC0. The Pixabay scrape source was removed (copyright-gray +
        bot 403s). Returns None on complete failure (caller proceeds without music
        for this segment)."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Allow some buffer for crossfades and ducking padding.
        target = segment.duration
        min_dur = max(15.0, target * 0.9)
        max_dur = max(min_dur + 30.0, target * 2.5)

        out_path = output_dir / f"seg_{segment_index:03d}.mp3"

        # Phase 2.9b — L0: try local audio library first.
        lib_result = self._try_library(segment, min_dur, max_dur)
        if lib_result is not None:
            return lib_result


        # Pixabay music REMOVED — it scraped Pixabay's HTML (no music API), which
        # is copyright-gray and now 403s on datacenter IPs. Licensed sources only.
        if os.environ.get("FREESOUND_API_KEY"):
            result = self._try_freesound(segment, min_dur, max_dur, out_path)
            if result is not None:
                self._ingest_into_library(result, segment)
                return result

        logger.warning(
            "MusicService: no music found for segment %d (mood=%s, query=%r)",
            segment_index, segment.mood, segment.search_keywords,
        )
        return None

    # ------------------------------------------------------------------
    # Phase 2.9b — L0 library lookup + auto-ingest
    # ------------------------------------------------------------------
    def _try_library(self, seg: MusicSegment, min_dur: float, max_dur: float):
        # getattr — tests sometimes construct via __new__ without running __init__
        if getattr(self, "audio_library", None) is None:
            return None
        try:
            hits = self.audio_library.search(
                seg.search_keywords,
                mood=seg.mood, energy=seg.energy,
                min_duration=min_dur, top_n=3,
            )
        except Exception as e:
            logger.warning("audio library L0 search failed: %s", e)
            return None
        for h in hits:
            if h.duration_seconds and h.duration_seconds > max_dur:
                continue  # too long — skip
            from pathlib import Path as _P
            if not h.local_path or not _P(h.local_path).exists():
                continue
            try:
                self.audio_library.bump_used(h.id)
            except Exception:
                pass
            logger.info(
                "L0 audio library hit for seg (mood=%s): %s",
                seg.mood, h.title or h.id,
            )
            return MusicResult(
                local_path=h.local_path,
                title=h.title or "Library track",
                duration=h.duration_seconds or 0.0,
                source=h.source or "library",
                source_url=h.source_url or "",
                license=h.license or "",
                query=seg.search_keywords,
            )
        return None

    def _ingest_into_library(self, result: MusicResult, segment: MusicSegment) -> None:
        if getattr(self, "audio_library", None) is None:
            return
        try:
            self.audio_library.ingest_track(
                result.local_path,
                source=result.source,
                source_id=Path(result.local_path).stem[:64],
                source_url=result.source_url,
                title=result.title,
                license=result.license,
                duration=result.duration,
                mood=segment.mood,
                energy=segment.energy,
                narrative_role=segment.narrative_role,
                tags=[t for t in segment.search_keywords.split() if t][:10],
                description=f"{result.title} — {segment.search_keywords}",
                move=False,  # keep file in pipeline dir; library copies
            )
        except Exception as e:
            logger.warning("audio library auto-ingest failed: %s", e)


    def _try_pixabay(self, seg, min_dur, max_dur, out_path):
        try:
            t = self.pixabay.search_and_download(
                query=seg.search_keywords,
                output_path=Path(out_path),
                min_duration=min_dur,
                max_duration=max_dur,
            )
        except Exception as e:
            logger.warning("Pixabay music failed: %s", e)
            return None
        if t is None:
            logger.info("Pixabay returned no music for %r", seg.search_keywords)
            return None
        return MusicResult(
            local_path=t.local_path,
            title=t.title,
            duration=t.duration,
            source=t.source or "pixabay_music",
            source_url=t.source_url
                or f"https://pixabay.com/music/?q={seg.search_keywords.replace(' ', '+')}",
            license=t.license,
            query=seg.search_keywords,
        )

    def _try_freesound(self, seg, min_dur, max_dur, out_path):
        try:
            t = self.freesound.search_and_download(
                query=seg.search_keywords,
                output_path=Path(out_path),
                min_duration=min_dur,
                max_duration=max_dur,
            )
        except Exception as e:
            logger.warning("Freesound failed: %s", e)
            return None
        if t is None:
            logger.info("Freesound returned no music for %r", seg.search_keywords)
            return None
        return MusicResult(
            local_path=t.local_path,
            title=t.title,
            duration=t.duration,
            source=t.source or "freesound",
            source_url=t.source_url,
            license=t.license,
            query=seg.search_keywords,
        )

    def build_bgm_track(
        self,
        results: list[MusicResult],
        output_path: Path,
        crossfade_seconds: float = 1.5,
    ) -> Optional[Path]:
        """Concatenate the per-segment music files with acrossfade transitions."""
        paths = [Path(r.local_path) for r in results if r and Path(r.local_path).exists()]
        if not paths:
            return None
        return self.audio_mix.concat_with_crossfade(
            paths, Path(output_path), crossfade_seconds=crossfade_seconds,
        )
