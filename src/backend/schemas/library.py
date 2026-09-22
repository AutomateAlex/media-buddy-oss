"""Phase 2.9a — Library schemas."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class LibraryClipRead(BaseModel):
    id: str
    local_path: str
    thumbnail_path: Optional[str] = None
    file_hash: Optional[str] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    aspect_ratio: Optional[str] = None
    category: Optional[str] = None
    tags: list[str] = []
    description: Optional[str] = None
    mood_tags: list[str] = []
    motion_tags: list[str] = []
    tag_status: str
    tag_provider: Optional[str] = None
    tag_cost_usd: float = 0.0
    tag_error: Optional[str] = None
    discovered_at: datetime
    indexed_at: Optional[datetime] = None
    used_count: int = 0
    last_used_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class LibraryStats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    disk_bytes: int
    tagging_cost_usd: float


class LibraryListResponse(BaseModel):
    items: list[LibraryClipRead]
    total: int
    page: int
    page_size: int


class LibraryBackfillResponse(BaseModel):
    scanned_projects: int
    ingested_clips: int
    skipped: int


class LibraryRetryTagsResponse(BaseModel):
    reset_count: int


class LibraryImportFolderRequest(BaseModel):
    folder_path: str
    recursive: bool = True


class LibraryImportFolderResponse(BaseModel):
    scanned: int
    ingested: int
    skipped: int
    error: Optional[str] = None


class LibraryImportFileResponse(BaseModel):
    clip: Optional[LibraryClipRead] = None
    error: Optional[str] = None
