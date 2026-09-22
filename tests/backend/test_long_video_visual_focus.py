from backend.services.long_video.visual_focus import repair_long_video_plan
from backend.services.long_video.orchestrator import LongVideoOrchestrator
from backend.services.footage_service import FootageCandidate
from backend.services.literal_probe import (
    _queries,
    _similar_subject_queries,
    filter_candidates,
)






def test_long_query_failure_cache_disables_repeated_rejects():
    orch = LongVideoOrchestrator.__new__(LongVideoOrchestrator)
    orch._long_query_failure_cache = {}
    orch._long_query_failure_lock = __import__("threading").Lock()

    query = "wolf close up"
    assert not orch._long_query_is_disabled(query)
    for _ in range(5):
        orch._long_record_query_verdict(query, "reject")
    assert orch._long_query_is_disabled(query)


def test_long_static_bad_queries_are_disabled_without_spending_observer():
    orch = LongVideoOrchestrator.__new__(LongVideoOrchestrator)
    orch._long_query_failure_cache = {}
    orch._long_query_failure_lock = __import__("threading").Lock()

    assert orch._long_query_is_disabled("wolf pack hunting coordination")
    assert orch._long_query_is_disabled("truth close up")


def test_long_rescue_queries_expand_single_word_terms():
    queries = _queries(["wolf", "human", "animal", "snowy"])

    assert "wolf" not in queries
    assert "human" not in queries
    assert "animal" not in queries
    assert "snowy" not in queries
    assert "wolf wildlife" in queries
    assert "human activity" in queries
    assert "wild animal" in queries
    assert "snowy landscape" in queries
    assert _similar_subject_queries(["wolf"])[0] == "wolf wildlife"






def test_long_video_abstract_chunks_defer_to_pool_without_observer_search():
    plan = {
        "chunk_index": 12,
        "sentence": "The truth is only beginning to surface.",
        "visual_focus": "abstract_broll",
        "visual_anchor": False,
        "must_show": [],
        "queries": [],
        "metaphor_queries": [],
    }

    assert LongVideoOrchestrator._v2_should_defer_to_visual_pool(plan) is True


def test_long_video_title_gate_rejects_generic_human_and_kitchen_for_ecology():
    plan = {
        "sentence": "The wolf is not merely at the top of the food chain.",
        "visual_focus": "prey_or_livestock",
        "must_show": ["wild prey animals"],
        "queries": ["wild prey animals"],
    }
    city = FootageCandidate(
        source="pexels",
        source_id="city",
        source_url="",
        query="wild prey animals",
        source_tags="City park with people walking outdoors",
    )
    kitchen = FootageCandidate(
        source="pixabay_video",
        source_id="kitchen",
        source_url="",
        query="wild prey animals",
        source_tags="Cooking food preparation in kitchen pan",
    )
    deer = FootageCandidate(
        source="pexels",
        source_id="deer",
        source_url="",
        query="wild prey animals",
        source_tags="Wild deer herd in forest wildlife nature",
    )

    survivors = filter_candidates([city, kitchen, deer], [], ["wild prey animals"], chunk_plan=plan)

    assert [candidate.source_id for candidate in survivors] == ["deer"]


def test_long_video_title_gate_still_allows_real_food_show_footage():
    plan = {
        "sentence": "Today we cook a simple tomato pasta recipe in a home kitchen.",
        "visual_focus": "prey_or_livestock",
        "must_show": ["cooking ingredients"],
        "queries": ["cooking ingredients close up"],
    }
    kitchen = FootageCandidate(
        source="pexels",
        source_id="kitchen",
        source_url="",
        query="cooking ingredients close up",
        source_tags="Cooking food preparation in kitchen with fresh ingredients",
    )
    deer = FootageCandidate(
        source="pexels",
        source_id="deer",
        source_url="",
        query="cooking ingredients close up",
        source_tags="Wild deer herd in forest wildlife nature",
    )

    survivors = filter_candidates(
        [kitchen, deer], [], ["cooking ingredients"], chunk_plan=plan,
    )

    assert [candidate.source_id for candidate in survivors] == ["kitchen"]
