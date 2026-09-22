"""System status endpoint — reports tool/runtime availability.

Phase 2.11f — fully decoupled from OpenMontage. Status now reflects our
own native components (Edge TTS, OpenAI TTS, ElevenLabs TTS, in-house
VideoCompose, in-house stock adapters) without going through any OM
registry.
"""
import os
import shutil

from fastapi import APIRouter

from backend.schemas.system import SystemStatus, ToolStatusItem

router = APIRouter()


def _tool_item(name: str, available: bool, provider: str, capability: str,
               install_instructions: str = "") -> ToolStatusItem:
    return ToolStatusItem(
        name=name,
        status="available" if available else "unavailable",
        provider=provider,
        capability=capability,
        runtime="python",
        install_instructions=install_instructions,
    )


@router.get("/status", response_model=SystemStatus)
def get_system_status():
    # Native TTS providers — no OM registry
    edge_available = True  # always — pip dep, no key
    elevenlabs_available = bool(os.environ.get("ELEVENLABS_API_KEY"))
    tool_items: list[ToolStatusItem] = [
        _tool_item("elevenlabs_tts", elevenlabs_available, "ElevenLabs", "tts",
                    install_instructions="Set ELEVENLABS_API_KEY in Settings."),
        _tool_item("video_compose", bool(shutil.which("ffmpeg")), "Media Buddy",
                    "video_compose",
                    install_instructions="Bundles ffmpeg (locator handles PATH)."),
    ]

    # Phase 2.8 — in-process worker (replaced redis/celery)
    try:
        from backend.workers.background_worker import get_background_worker
        worker_running = get_background_worker().is_running()
    except Exception:
        worker_running = False

    configured_tts = [
        n for n, ok in (
            ("elevenlabs_tts", elevenlabs_available),
        ) if ok
    ]
    # Multi-source footage: report which in-house adapters are actually live
    try:
        from backend.services.footage_service import FootageService
        configured_stock = FootageService().source_names
    except Exception:
        configured_stock = []

    return SystemStatus(
        api_online=True,
        ffmpeg_available=bool(shutil.which("ffmpeg")),
        worker_running=worker_running,
        tools=tool_items,
        configured_tts_providers=configured_tts,
        configured_stock_providers=configured_stock,
    )
