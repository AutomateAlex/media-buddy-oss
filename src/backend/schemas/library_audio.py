"""Phase 2.9b — Audio library schemas."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class LibraryAudioRead(BaseModel):
    id: str
    local_path: str
    source: Optional[str] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    title: Optional[str] = None
    license: Optional[str] = None
    duration_seconds: float = 0.0
    mood: Optional[str] = None
    energy: Optional[str] = None
    narrative_role: Optional[str] = None
    tags: list[str] = []
    description: Optional[str] = None
    tag_status: str
    discovered_at: datetime
    indexed_at: Optional[datetime] = None
    used_count: int = 0
    last_used_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class LibraryAudioStats(BaseModel):
    total: int
    by_mood: dict[str, int]
    by_energy: dict[str, int]
    disk_bytes: int


class LibraryAudioListResponse(BaseModel):
    items: list[LibraryAudioRead]
    total: int
    page: int
    page_size: int
