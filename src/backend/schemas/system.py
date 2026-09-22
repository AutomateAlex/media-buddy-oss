from pydantic import BaseModel

class ToolStatusItem(BaseModel):
    name: str
    status: str
    provider: str
    capability: str
    runtime: str
    install_instructions: str = ""

class SystemStatus(BaseModel):
    api_online: bool
    ffmpeg_available: bool
    # Phase 2.8 — replaces redis_available. True iff the in-process
    # BackgroundWorker daemon thread is alive.
    worker_running: bool
    tools: list[ToolStatusItem]
    configured_tts_providers: list[str]
    configured_stock_providers: list[str]
