"""Thin visual guardrail: reject wrong-world clips after the LLM accepts them."""
import importlib

from backend.lib import visual_guardrails as vg


ROMAN_SUBJECT = "ancient Rome Pantheon dome Pantheon Rome interior oculus Roman concrete"
MAYAN_SUBJECT = "Mayan ruins jungle pyramid Maya civilization Mesoamerican temple"


def test_mayan_clip_rejected_for_roman_video():
    reason = vg.asset_wrong_world(
        source_tags="Mayan ruins jungle temple aerial drone Mesoamerican pyramid",
        source_url="https://www.stock.com/video/stock/mayan-temple-jungle-123",
        subject_text=ROMAN_SUBJECT,
    )
    assert reason and reason.startswith("wrong_world:")


def test_pantheon_clip_allowed_for_roman_video():
    assert vg.asset_wrong_world(
        source_tags="Pantheon Rome interior dome wide angle Roman architecture",
        source_url="https://www.stock.com/video/stock/pantheon-rome-456",
        subject_text=ROMAN_SUBJECT,
    ) is None


def test_mayan_clip_allowed_for_mayan_video():
    # A genuinely Mayan video must NOT have its Mayan footage rejected.
    assert vg.asset_wrong_world(
        source_tags="Mayan ruins jungle temple aerial Mesoamerican pyramid",
        source_url="https://www.stock.com/video/stock/mayan-temple-789",
        subject_text=MAYAN_SUBJECT,
    ) is None


def test_generic_offsubject_ruin_rejected_soft():
    # A generic 'ancient ruins' clip sharing NO subject token with the Roman
    # video is rejected by the soft tier.
    reason = vg.asset_wrong_world(
        source_tags="ancient ruins desert archaeological site sunset",
        source_url="https://www.stock.com/video/stock/desert-ruins-001",
        subject_text="ancient Rome Pantheon dome oculus Roman concrete pozzolanic",
    )
    assert reason and reason.startswith("offsubject_ruin:")


def test_subject_overlap_survives_soft_tier():
    # Same 'ruins' token but the clip is clearly on-subject (mentions Pantheon)
    # → must NOT be rejected.
    assert vg.asset_wrong_world(
        source_tags="Pantheon Rome ancient ruins interior",
        source_url="https://www.stock.com/video/stock/pantheon-002",
        subject_text=ROMAN_SUBJECT,
    ) is None


def test_llm_forbidden_pack_rejected():
    reason = vg.asset_wrong_world(
        source_tags="modern city skyline drone night",
        source_url="https://www.stock.com/video/stock/city-003",
        subject_text=ROMAN_SUBJECT,
        forbidden_pack=["modern city", "skyscraper"],
    )
    assert reason and reason.startswith("llm_forbidden:")


def test_himalaya_not_false_positive_for_maya():
    # 'himalayan' must NOT trip the whole-word 'maya' rule.
    assert vg.asset_wrong_world(
        source_tags="himalayan mountain range snow aerial",
        source_url="https://example.com/video/himalaya-004",
        subject_text="mountain climbing expedition snow",
    ) is None


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_VISUAL_GUARDRAILS", "0")
    importlib.reload(vg)
    try:
        assert vg.asset_wrong_world(
            source_tags="Mayan ruins jungle temple",
            source_url="",
            subject_text=ROMAN_SUBJECT,
        ) is None
    finally:
        monkeypatch.delenv("MEDIA_BUDDY_VISUAL_GUARDRAILS", raising=False)
        importlib.reload(vg)
