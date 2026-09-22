"""Tests for the general subject-mismatch gate in literal_probe.

A video about one concrete subject must not keep footage whose tags name a
DIFFERENT member of the same concept group (the bug: wolf clips in a wild-boar
video). The rule is general, not animal-specific — it also covers dishes,
vehicles, etc. — and stays inert for abstract subjects.
"""
from backend.services.footage_service import FootageCandidate
from backend.services.literal_probe import (
    _subject_keys,
    _subject_mismatch,
    _subjects_in,
    filter_candidates,
)


def _cand(source_id, tags, url="https://example.com/v/x.mp4", source="pexels"):
    return FootageCandidate(
        source=source, source_id=source_id, source_url=url, source_tags=tags,
    )


# --- the rule itself (helper-level, no other gates in the way) ---

def test_subjects_in_collapses_aliases():
    canons = {c for _, c in _subjects_in("wolf, predator, gray wolf, wildlife")}
    assert "wolf" in canons
    assert {c for _, c in _subjects_in("wild boar pig swine hog")} == {"boar"}


def test_subjects_in_matches_on_token_boundary():
    # "ant" must NOT fire inside "plant"; real "ant" must.
    assert _subjects_in("plant growth time-lapse forest") == set()
    assert "ant" in {c for _, c in _subjects_in("ant colony macro")}


def test_mismatch_wolf_vs_boar():
    subj = _subjects_in("wild boar")
    assert _subject_mismatch("wolf, predator, gray wolf", subj)          # different animal -> reason
    assert _subject_mismatch("wild boar foraging forest", subj) == ""    # same subject -> ok
    assert _subject_mismatch("forest, trees, fog, nature", subj) == ""   # no animal -> ok


def test_general_rule_covers_food_and_vehicles():
    rice = _subjects_in("fried rice")
    assert _subject_mismatch("pizza, cheese, italian", rice)             # wrong dish -> reject
    assert _subject_mismatch("fried rice, wok, asian food", rice) == ""  # right dish -> ok

    car = _subjects_in("car")
    assert _subject_mismatch("airplane, jet, runway", car)              # wrong vehicle -> reject
    assert _subject_mismatch("car, highway, traffic", car) == ""        # right vehicle -> ok


def test_inert_for_abstract_subject():
    # Money/inflation is not a concrete group member -> no subject keys -> gate off.
    keys = _subject_keys({"long_video_visual_policy": {"topic_anchor": "inflation"}}, ["money"])
    assert keys == set()
    assert _subject_mismatch("wolf, predator", keys) == ""


# --- wired into filter_candidates (the v2 long-video path) ---

def test_filter_candidates_drops_wolf_for_boar_video():
    plan = {
        "long_video_visual_policy": {"topic_anchor": "wild boar"},
        "must_show": ["boar"],
        "sentence": "wild boar at the city edge in the forest",
        "queries": ["wild boar city edge habitat"],
    }
    cands = [
        _cand("wolf1", "wolf, predator, european wolf, gray wolf, wildlife"),
        _cand("boar1", "wild boar, pig, forest, wildlife, animal"),
    ]
    out = filter_candidates(cands, must_not=[], must_show=["boar"], chunk_plan=plan)
    ids = {c.source_id for c in out}
    assert "wolf1" not in ids
    assert "boar1" in ids
