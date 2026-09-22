"""Thin deterministic visual guardrails for stock-footage selection.

guardrails are the *quality inspector* of what actually came back. They NEVER
inject topic words — they only reject a chosen clip whose metadata points at the
WRONG visual world (e.g. a Mayan jungle-temple clip selected for a Roman
Pantheon video) so the selector falls through to the next candidate.

Kept deliberately small + high-precision: over-rejection would starve a chunk
into a worse fallback. Toggle with ``MEDIA_BUDDY_VISUAL_GUARDRAILS=0`` (A/B).
"""
from __future__ import annotations

import os
import re
from typing import Optional

# Cross-world tokens that are almost never correct unless the video is itself
# about that world. Single words are matched as whole tokens; phrases (with a
HARD_FORBIDDEN_WORLDS = (
    "mayan", "maya", "aztec", "aztecs", "mesoamerican", "angkor",
    "jungle temple",
)

# Lower-confidence "generic ancient ruin / jungle" tokens. Only reject when the
# clip ALSO shares NO subject token with the video (i.e. a generic off-subject
# ruin slipped through), never on its own.
SOFT_FORBIDDEN_WORLDS = (
    "pyramid", "ruins", "archaeological", "jungle", "rainforest",
)

# Generic tokens that don't distinguish a subject — ignored when measuring
# whether an off-subject clip shares any real subject vocabulary.
_GENERIC_TOKENS = frozenset({
    "ancient", "footage", "video", "stock", "clip", "scene", "shot", "close",
    "wide", "view", "background", "broll", "old", "historic", "historical",
    "the", "and", "of", "in", "on", "a", "an", "with", "for",
})

_WORD = re.compile(r"[a-z]+")


def guardrails_enabled() -> bool:
    return os.environ.get("MEDIA_BUDDY_VISUAL_GUARDRAILS", "1") != "0"


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _hit(tok: str, meta: str, meta_tokens: set[str]) -> bool:
    """Phrase tokens match as substrings; single words as whole tokens (so
    'maya' never fires on 'himalayan')."""
    return (tok in meta) if " " in tok else (tok in meta_tokens)


def asset_wrong_world(
    *,
    source_tags: str,
    source_url: str,
    subject_text: str,
    forbidden_pack: Optional[list] = None,
) -> Optional[str]:
    """Return a short rejection reason if the chosen clip is the wrong visual
    world for this video, else ``None``.

    - ``source_tags`` / ``source_url``: the chosen candidate's metadata.
    - ``subject_text``: the video's own subject vocabulary (topic anchor + its
      concrete queries + must_show). A forbidden token that ALSO appears here
      means the video really is about that world → never reject.
    - ``forbidden_pack``: the LLM's explicit forbidden visual worlds (hard).
    """
    if not guardrails_enabled():
        return None
    meta = f"{source_tags or ''} {source_url or ''}".lower()
    if not meta.strip():
        return None
    subj = (subject_text or "").lower()
    subj_tokens = _tokens(subject_text)
    meta_tokens = _tokens(meta)

    # 1) LLM-declared forbidden worlds (hard) — but never reject a token the
    #    video itself is about. Applies even to ruins/jungle videos.
    for raw in (forbidden_pack or []):
        tok = str(raw or "").strip().lower()
        if len(tok) >= 4 and _hit(tok, meta, meta_tokens) and tok not in subj:
            return f"llm_forbidden:{tok}"

    # If the video itself lives in the ancient-ruins / jungle world (its subject
    # names any of these worlds), the built-in cross-world filters must stand
    # down — a real Mayan/Egypt/ruins video legitimately uses such footage.
    subject_in_world = any(
        _hit(tok, subj, subj_tokens)
        for tok in HARD_FORBIDDEN_WORLDS + SOFT_FORBIDDEN_WORLDS
    )
    if subject_in_world:
        return None

    # 2) Built-in cross-world hard forbiddens (Mayan/Aztec/… in a non-ruins video).
    for tok in HARD_FORBIDDEN_WORLDS:
        if _hit(tok, meta, meta_tokens):
            return f"wrong_world:{tok}"

    # 3) Soft: a generic ruin/jungle clip that shares NO *distinctive* subject
    #    token with the video (off-subject) — conservative, reject only when the
    #    clip has none of the video's real vocabulary (generic words ignored).
    distinctive = subj_tokens - _GENERIC_TOKENS
    if distinctive and not (distinctive & meta_tokens):
        for tok in SOFT_FORBIDDEN_WORLDS:
            if _hit(tok, meta, meta_tokens):
                return f"offsubject_ruin:{tok}"
    return None
