"""Unit tests for the DRUG/CANNABIS gate in content_safety_filter.

Compliance-critical: our audience is primarily in China, where any illegal-drug /
cannabis / poppy imagery is a hard legal line. These tests lock in that:
  1. drug + cannabis + poppy (incl. borderline botanicals) are blocked in both a
     search QUERY (sanitize_query) and a candidate's TAGS (asset_is_unsafe);
  2. the gate is INDEPENDENT of the general content-safety A/B switch;
  3. everyday-ambiguous words (weed control / cooking pot / CBD skyline / Hindu
     Kush / reefer truck / poppy seed / medical syringe) are NOT over-blocked.
"""
import os

import pytest

from backend.lib.content_safety_filter import (
    _first_drug,
    asset_is_unsafe,
    sanitize_query,
)


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    """Both gates ON by default unless a test overrides."""
    monkeypatch.delenv("MEDIA_BUDDY_CONTENT_SAFETY", raising=False)
    monkeypatch.delenv("MEDIA_BUDDY_DRUG_SAFETY", raising=False)


# ---------- blocked: hard drug terms (EN + CN) ----------

@pytest.mark.parametrize("term", [
    "marijuana joint close up", "cannabis field", "cocaine powder",
    "heroin syringe", "crystal meth", "opium den", "a bong on a table",
    "大麻叶特写", "毒品交易", "冰毒结晶", "吸毒的人", "罂粟花田",
])
def test_drug_query_is_stripped(term):
    cleaned, changed = sanitize_query(term)
    assert changed is True
    # whatever survives must no longer contain any drug term
    assert _first_drug(cleaned) is None


@pytest.mark.parametrize("tags", [
    "cannabis, marijuana, weed leaf",
    "smoking a joint, rolling paper",
    "大麻, 毒品, 吸毒",
    "opium poppy field, red poppy",
])
def test_drug_tags_flag_asset_unsafe(tags):
    reason = asset_is_unsafe(tags)
    assert reason is not None and reason.startswith("unsafe:drug:")


# ---------- blocked: borderline botanicals the user said to cut ("擦边的肯定不生产") ----------

@pytest.mark.parametrize("term", [
    "cannabis leaf", "hemp leaf macro", "hemp field aerial",
    "poppy field", "poppy flower", "red poppies", "opium poppy",
])
def test_borderline_botanicals_blocked(term):
    assert _first_drug(term) is not None
    assert asset_is_unsafe(term) is not None


# ---------- independent gate: drugs blocked even with general safety OFF ----------

def test_drug_blocked_when_general_safety_off(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_CONTENT_SAFETY", "0")
    assert asset_is_unsafe("cannabis plant") is not None
    cleaned, changed = sanitize_query("marijuana grow room")
    assert changed is True and _first_drug(cleaned) is None


def test_drug_gate_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_DRUG_SAFETY", "0")
    # with the drug gate off AND general safety on, a pure-drug tag is no longer
    # flagged by the drug path (general banks don't carry drug words)
    assert asset_is_unsafe("cannabis plant") is None


# ---------- NOT over-blocked: everyday words that merely look drug-adjacent ----------

@pytest.mark.parametrize("safe", [
    "weed control in the garden", "pulling weeds lawn care",
    "cooking pot on stove", "boiling pot of soup",
    "central business district skyline", "cbd office towers at night",
    "hindu kush mountain range", "kingdom of kush nubia",
    "reefer container truck logistics", "refrigerated reefer ship",
    "poppy seed bagel bakery", "smoked salmon platter",
    "bbq smoke grill", "nurse holding a syringe vaccine",
    "hemp rope knot", "hemp fabric textile",
])
def test_safe_everyday_terms_pass(safe):
    assert _first_drug(safe) is None, f"false positive on: {safe}"
    assert asset_is_unsafe(safe) is None
    cleaned, changed = sanitize_query(safe)
    assert cleaned == safe and changed is False


def test_clean_query_unchanged():
    q = "aerial city sunrise timelapse"
    assert sanitize_query(q) == (q, False)
    assert asset_is_unsafe(q) is None
