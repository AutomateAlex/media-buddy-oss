"""

Pure-Python coverage; no llm/footage/db dependency.
"""
import logging
import threading

from backend.services.llm_call_counter import (
    LLMCallCounter,
    bump_active,
    get_active_counter,
    set_active_counter,
)


def test_bump_without_chunk_context_lands_in_shared():
    """Calls outside chunk() ctx attribute to 'shared', not per_chunk."""
    c = LLMCallCounter()
    c.bump("plan_chunks", "claude-sonnet-4-6")
    c.bump("plan_chunks", "claude-sonnet-4-6")
    summary = c.summary()
    assert summary["per_chunk"] == {}
    assert summary["shared"]["plan_chunks"]["calls"] == 2
    assert summary["shared"]["plan_chunks"]["model"] == "claude-sonnet-4-6"


def test_bump_inside_chunk_context_attributes_to_chunk():
    c = LLMCallCounter()
    with c.chunk(3):
        c.bump("verify", "claude-haiku-4-5")
        c.bump("verify", "claude-haiku-4-5")
        c.bump("blind_observe", "google/gemini-2.5-flash")
    s = c.summary()
    assert 3 in s["per_chunk"]
    chunk3 = s["per_chunk"][3]
    assert chunk3["verify"]["calls"] == 2
    assert chunk3["blind_observe"]["calls"] == 1
    assert chunk3["total"] == 3
    assert s["shared"] == {}


def test_chunk_context_restores_outer_binding_on_exit():
    """Nested chunk() ctx restores the outer binding (defensive — orchestrator
    does not nest today but future refactors might)."""
    c = LLMCallCounter()
    with c.chunk(1):
        c.bump("plan_chunks", "X")
        with c.chunk(2):
            c.bump("verify", "Y")
        # Back to chunk 1 context
        c.bump("plan_chunks", "X")
    s = c.summary()
    assert s["per_chunk"][1]["total"] == 2
    assert s["per_chunk"][2]["total"] == 1


def test_summary_aggregate_groups_by_purpose_and_model():
    """aggregate_by_purpose_and_model collapses per-chunk × shared into the
    flat 'how much would env-flip save' view."""
    c = LLMCallCounter()
    with c.chunk(0):
        c.bump("blind_observe", "google/gemini-2.5-flash")
    with c.chunk(1):
        c.bump("blind_observe", "google/gemini-2.5-flash")
        c.bump("verify", "claude-haiku-4-5")
    c.bump("plan_chunks", "claude-sonnet-4-6")  # shared
    agg = c.summary()["aggregate_by_purpose_and_model"]
    assert agg["blind_observe:google/gemini-2.5-flash"]["calls"] == 2
    assert agg["verify:claude-haiku-4-5"]["calls"] == 1
    assert agg["plan_chunks:claude-sonnet-4-6"]["calls"] == 1


def test_summary_grand_totals_match_bumps():
    c = LLMCallCounter()
    with c.chunk(0):
        for _ in range(5):
            c.bump("plan_chunks", "claude-sonnet-4-6")
    with c.chunk(1):
        for _ in range(3):
            c.bump("verify", "claude-haiku-4-5")
    c.bump("build_chunk_contracts", "claude-sonnet-4-6")  # shared
    s = c.summary()
    assert s["grand_total_calls"] == 9
    assert s["grand_total_est_cost_usd"] >= 0  # prices are not tracked in this build


def test_thread_local_isolation_no_cross_contamination():
    """Two threads each enter different chunk contexts; bumps stay attributed
    to the correct chunk. Validates the threading.local design choice."""
    c = LLMCallCounter()
    barrier = threading.Barrier(2)

    def worker(chunk_id: int, n_bumps: int):
        with c.chunk(chunk_id):
            barrier.wait()  # ensure both threads are inside their chunk ctx
            for _ in range(n_bumps):
                c.bump("verify", "claude-haiku-4-5")

    t1 = threading.Thread(target=worker, args=(10, 5))
    t2 = threading.Thread(target=worker, args=(20, 7))
    t1.start(); t2.start()
    t1.join(); t2.join()
    s = c.summary()
    assert s["per_chunk"][10]["verify"]["calls"] == 5
    assert s["per_chunk"][20]["verify"]["calls"] == 7


def test_warn_threshold_triggers_log(caplog):
    """Hitting WARN_THRESHOLD+1 in one chunk emits a logger.warning."""
    from backend.services import llm_call_counter as mod
    c = LLMCallCounter()
    with c.chunk(99), caplog.at_level(
        logging.WARNING, logger="backend.services.llm_call_counter"
    ):
        for _ in range(mod.WARN_THRESHOLD + 1):
            c.bump("verify", "claude-haiku-4-5")
    assert any(
        "cost_guardrail" in r.message and "warn threshold" in r.message
        for r in caplog.records
    ), f"warning missing — records: {[r.message for r in caplog.records]}"


def test_error_threshold_triggers_log_but_does_not_raise(caplog):
    """ERROR_THRESHOLD+1 emits logger.error but pipeline continues."""
    from backend.services import llm_call_counter as mod
    c = LLMCallCounter()
    with c.chunk(99), caplog.at_level(
        logging.ERROR, logger="backend.services.llm_call_counter"
    ):
        for _ in range(mod.ERROR_THRESHOLD + 1):
            c.bump("verify", "claude-haiku-4-5")
    # Did NOT raise — we reached here
    assert any(
        "cost_guardrail" in r.message and "error threshold" in r.message
        for r in caplog.records
    )


def test_guardrail_silent_env_kills_warning_emission(caplog, monkeypatch):
    """MEDIA_BUDDY_DISABLE_COST_GUARDRAIL=1 silences threshold logs even
    after surpassing both thresholds. summary() still works."""
    from backend.services import llm_call_counter as mod
    monkeypatch.setenv("MEDIA_BUDDY_DISABLE_COST_GUARDRAIL", "1")
    c = LLMCallCounter()  # re-read env in __init__
    with c.chunk(99), caplog.at_level(
        logging.WARNING, logger="backend.services.llm_call_counter"
    ):
        for _ in range(mod.ERROR_THRESHOLD + 1):
            c.bump("verify", "claude-haiku-4-5")
    threshold_logs = [
        r for r in caplog.records if "cost_guardrail" in r.message
    ]
    assert threshold_logs == [], (
        f"silent env should suppress threshold logs: {threshold_logs}"
    )
    # But counter still works
    s = c.summary()
    assert s["per_chunk"][99]["verify"]["calls"] == mod.ERROR_THRESHOLD + 1


def test_active_counter_set_and_get():
    c = LLMCallCounter()
    set_active_counter(c)
    assert get_active_counter() is c
    set_active_counter(None)
    assert get_active_counter() is None


def test_bump_active_no_op_when_no_counter():
    """bump_active() with no active counter must not raise."""
    set_active_counter(None)
    bump_active("plan_chunks", "claude-sonnet-4-6")  # should not throw


def test_bump_active_records_to_active():
    c = LLMCallCounter()
    set_active_counter(c)
    try:
        with c.chunk(0):
            bump_active("verify", "claude-haiku-4-5")
        s = c.summary()
        assert s["per_chunk"][0]["verify"]["calls"] == 1
    finally:
        set_active_counter(None)
