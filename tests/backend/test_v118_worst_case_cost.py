"""

from a real smoke run (commit 3a → smoke 15 dump → numbers). Until then
the slow integration test stays skipped — pure-unit pieces still run.

chunks ≈ 160 calls/chunk worst-case). These are the architectural red lines
not to cross without explicit justification in a commit message.
"""
import pytest


# Hard ceiling per single chunk — any single chunk consuming this many LLM
# calls indicates a cascade-control regression (e.g. fast-fail broke).
# Initial value 80 = WARN_THRESHOLD. The cost incident showed fail chunks
# hitting 160; A+B+C brought max to ~50. 80 buys headroom for legitimate
# growth (e.g. cascade extension) without normalizing the incident.
HARD_CEILING_PER_CHUNK = 80

# Total pipeline ceiling — to be filled by commit 3a smoke 15 dump.
# Procedure:
#   1. Ship commit 3a with EXPECTED_WORST_CASE_TOTAL = 0 (this scaffold)
#   2. Run smoke 15 against a worst-case script (14+ unsearchable chunks)
#   3. Read counter.summary()["grand_total_calls"] from the log
EXPECTED_WORST_CASE_TOTAL = 0


@pytest.mark.skip(
    reason="baseline pending — fill EXPECTED_WORST_CASE_TOTAL from smoke 15 dump"
)
@pytest.mark.slow
def test_worst_case_all_fail_cost_ceiling():
    """14 unsearchable chunks → counter.grand_total_calls must stay under
    EXPECTED_WORST_CASE_TOTAL and no single chunk > HARD_CEILING_PER_CHUNK.

    Will be implemented once smoke 15 baseline is captured.
    """
    raise NotImplementedError(
        "Real-LLM worst-case fixture pending. See module docstring."
    )


# ----------------------------------------------------------------------
# Pure-unit guard: just confirm the ceiling constant didn't get accidentally
# bumped without justification. Trip-wire test that's cheap to run.
# ----------------------------------------------------------------------
def test_hard_ceiling_constant_unchanged_without_justification():
    """If you raise HARD_CEILING_PER_CHUNK above 80, update this test and
    your commit message must explain why the cost incident's red line is
    moving. Don't ship the bump silently."""
    assert HARD_CEILING_PER_CHUNK == 80, (
        f"HARD_CEILING_PER_CHUNK changed to {HARD_CEILING_PER_CHUNK}. The "
        "2026-05-24 cost incident set this red line. Any change MUST be "
        "justified in the commit message AND this assertion updated."
    )
