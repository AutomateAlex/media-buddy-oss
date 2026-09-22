"""Phase 2.7d — Chunk + Shot read schemas for the Timeline editor."""
from typing import Optional

from pydantic import BaseModel


class ShotRead(BaseModel):
    id: str
    shot_index: int
    query: Optional[str] = None
    text_critic_score: Optional[float] = None
    selected_source: Optional[str] = None
    selected_source_url: Optional[str] = None
    selected_local_path: Optional[str] = None
    selected_clip_duration: Optional[float] = None
    mm_critic_score: Optional[float] = None
    status: str

    model_config = {"from_attributes": True}


class ChunkRead(BaseModel):
    id: str
    chunk_index: int
    sentence: str
    shot_type: str
    is_hero_shot: bool
    status: str
    tts_duration_seconds: Optional[float] = None
    text_critic_avg: Optional[float] = None
    mm_critic_avg: Optional[float] = None
    retry_count: int = 0
    shots: list[ShotRead] = []

    model_config = {"from_attributes": True}


class ChunkReplaceRequest(BaseModel):
    """POST body for /api/projects/{id}/chunks/{chunk_id}/replace.

    `prompt` overrides the LLM-generated search query for this chunk.
    `strategy` is "stock" | "ai" | "auto" — auto = let orchestrator decide.
    """
    prompt: Optional[str] = None
    strategy: str = "auto"
