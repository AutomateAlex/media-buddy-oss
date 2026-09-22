"""Unit tests for cultural_filter — the gate that stops Chinese street-food
clips from appearing in a French-themed video (media-buddy-rules.md §3)."""
from backend.lib.cultural_filter import (
    cultural_check,
    has_cultural_anchor,
    inject_cultural_anchor,
    normalize_culture,
)


# ---------- normalize_culture ----------

def test_normalize_culture_accepts_canonical():
    for v in ["France", "China", "Japan", "Universal"]:
        assert normalize_culture(v) == v


def test_normalize_culture_snaps_aliases():
    assert normalize_culture("french") == "France"
    assert normalize_culture("chinese") == "China"
    assert normalize_culture("english") == "Universal"  # unknown → Universal
    assert normalize_culture("british") == "UK"
    assert normalize_culture("european") == "Generic-Western"


def test_normalize_culture_empty_or_none():
    assert normalize_culture("") == "Universal"
    assert normalize_culture(None) == "Universal"


# ---------- cultural_check (the heart of the fix) ----------

def test_blocks_chinese_clip_in_french_script():
    """The exact 法国美食 failure case: video titled with 'Nanjing' /
    'Chinese temple' would have passed before, must reject now."""
    v = cultural_check(
        culture="France",
        title="Nanjing street food market in summer",
        description="",
    )
    assert v.blocked is True
    assert "nanjing" in v.matched_blacklist


def test_blocks_chinese_temple_in_french_script():
    v = cultural_check(
        culture="France",
        title="Chinese temple architecture at sunset",
    )
    assert v.blocked is True


def test_blocks_japanese_in_french_script():
    v = cultural_check(
        culture="France",
        title="Tokyo ramen shop close-up",
    )
    assert v.blocked is True


def test_blocks_via_chinese_characters_in_description():
    """中文招牌 in description triggers blacklist even if title is English."""
    v = cultural_check(
        culture="France",
        title="Outdoor food vendor",
        description="拍摄于中国南京老城区",
    )
    assert v.blocked is True


def test_passes_french_clip_with_anchor():
    v = cultural_check(
        culture="France",
        title="Parisian café terrace morning light",
    )
    assert v.blocked is False
    assert v.is_preferred is True


def test_passes_neutral_clip_no_culture_markers():
    """A clip with no obvious culture markers passes neutral (not preferred,
    not blocked) — orchestrator will let scoring decide."""
    v = cultural_check(
        culture="France",
        title="Soft golden sunlight on a wooden table",
    )
    assert v.blocked is False
    assert v.is_preferred is False


def test_universal_culture_never_blocks():
    """When the script has no culture (Universal), even a Chinese-titled
    clip passes the gate. Scoring still handles relevance."""
    v = cultural_check(
        culture="Universal",
        title="Beijing temple architecture",
    )
    assert v.blocked is False


def test_unknown_culture_falls_back_to_universal():
    v = cultural_check(
        culture="Atlantis",
        title="Tokyo neon sign",
    )
    assert v.blocked is False  # normalized to Universal


def test_no_text_metadata_passes_neutral():
    """Some Pexels rows have empty title/description — must not auto-block."""
    v = cultural_check(culture="France", title="", description="")
    assert v.blocked is False
    assert v.is_preferred is False


def test_tags_field_participates_in_match():
    """source_tags from Pexels API often carry the giveaway — make sure
    they're checked alongside title/description."""
    v = cultural_check(
        culture="France",
        title="Outdoor market scene",
        tags=["shanghai", "street", "food"],
    )
    assert v.blocked is True


# ---------- anchor injection ----------

def test_has_cultural_anchor_detects_french_word():
    assert has_cultural_anchor("French café morning coffee", "France")
    assert has_cultural_anchor("Paris street terrace", "France")
    assert not has_cultural_anchor("morning coffee", "France")


def test_inject_anchor_prepends_when_missing():
    out = inject_cultural_anchor("morning coffee table", "France")
    assert out.startswith("French ")


def test_inject_anchor_idempotent():
    """Calling inject twice doesn't double-prepend."""
    once = inject_cultural_anchor("morning coffee", "France")
    twice = inject_cultural_anchor(once, "France")
    assert once == twice


def test_inject_anchor_universal_is_noop():
    """Universal culture → no anchor word to inject, return as-is."""
    out = inject_cultural_anchor("morning coffee", "Universal")
    assert out == "morning coffee"


def test_inject_anchor_handles_empty_query():
    assert inject_cultural_anchor("", "France") == ""
