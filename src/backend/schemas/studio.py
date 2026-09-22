"""Phase 2.10a — Studio chat schemas."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str  # user | assistant | tool | system
    content: str
    ts: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_results: Optional[list[dict[str, Any]]] = None


class StudioSpec(BaseModel):
    """Slots the agent has filled. All optional — UI shows "—" for unfilled."""
    # Required (must fill before submission)
    video_type: Optional[str] = None         # "promo" | "tutorial" | "story" | "intro" | "testimonial"
    direction: Optional[str] = None          # platform-dependent: "finance" | "travel" | ...
    platform: Optional[str] = None           # "youtube_long" | "youtube_shorts" | "tiktok" | "reels"
    aspect_ratio: Optional[str] = None       # "16:9" | "9:16" | "1:1"
    duration_seconds: Optional[int] = None
    count: Optional[int] = None              # 1 = single, N = batch

    # Optional but recommended
    reference_video_url: Optional[str] = None
    reference_video_report: Optional[dict[str, Any]] = None
    own_assets_folder: Optional[str] = None
    series_id: Optional[str] = None
    voice: Optional[str] = None
    include_subtitles: bool = True

    # Final content (set when agent confirms with user)
    topics: Optional[list[str]] = None       # for batch: list of topic strings
    scripts: Optional[list[str]] = None      # final scripts ready to submit


class StudioSessionRead(BaseModel):
    id: str
    series_id: Optional[str] = None
    status: str
    messages: list[ChatMessage] = []
    spec: StudioSpec = StudioSpec()
    created_project_ids: list[str] = []
    created_batch_run_id: Optional[str] = None
    turn_count: int = 0
    created_at: datetime
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class StudioSessionCreate(BaseModel):
    """Start a new session. series_id is optional — passing it injects that
    Series's memory + locked settings into the agent's system prompt."""
    series_id: Optional[str] = None
    initial_message: Optional[str] = None  # if user already typed something


class StudioSendMessage(BaseModel):
    content: str


class StudioSendMessageResponse(BaseModel):
    """One agent turn. Includes the new assistant message + updated spec +
    any tool calls/results for transparency."""
    session: StudioSessionRead
    last_message: ChatMessage
    blocked_by_safety: bool = False
    safety_reason: Optional[str] = None
