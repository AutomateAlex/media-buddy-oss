"""Phase 2.11k — visual style presets endpoint.

Exposes the curated style presets so the Series UI can show a dropdown.
Read-only — presets ship hardcoded; users can't (yet) author their own.
A future paid tier may unlock custom-preset editing.
"""
from fastapi import APIRouter

from backend.lib.style_presets import list_presets

router = APIRouter()


@router.get("/")
def get_style_presets():
    """Return all available style presets for the Series-binding UI."""
    return {"presets": list_presets()}
