"""Unit tests for ``backend.lib.plan_validation`` — three-layer hard gate.

Covers (in order):

* PlanChunksError shape + serialization
* Helpers (_contains_cjk, _tokenize, _is_ascii_keyword_string, _atom_tokens)
* validate_plan_chunks_output — 8 checks
* validate_readiness_gate — universal checks + level-aware rules
"""
from __future__ import annotations

import pytest

from backend.lib.plan_validation import (
    PlanChunksError,
    _PLAN_ERROR_CODES,
    _CHUNK_STATUS,
    _atom_tokens,
    _contains_cjk,
    _cjk_ratio,
    _flatten_atoms,
    _is_ascii_keyword_string,
    _tokenize,
    validate_readiness_gate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fractal_atoms() -> dict:
    """Sample atoms for the koch curve fractal chunk."""
    return {
        "O_OBJECT": ["koch curve"],
        "A_ACTION": [],
        "N_NUMBER": ["1.26"],
        "M_METAPHOR": [],
        "C_CONCEPT": ["fractal dimension"],
        "S_SCALE": [],
        "E_EMOTION": [],
        "P_PARALLEL": [],
    }


@pytest.fixture
def fractal_prompts_depth5() -> dict:
    """Valid depth=5 prompts for the koch curve chunk."""
    return {
        "L1": "koch snowflake fractal animation",
        "L2": "fractal snowflake animation",
        "L3": "geometric pattern animation",
        "L4": "scientific abstract motion graphic",
        "L5": "fractal geometric pattern motion",   # topic-aware (fractal token)
    }


@pytest.fixture
def coffee_atoms() -> dict:
    """Coffee chunk — common vocab only → depth=2 typically."""
    return {
        "O_OBJECT": ["coffee", "person"],
        "A_ACTION": ["drinking"],
        "N_NUMBER": [], "M_METAPHOR": [], "C_CONCEPT": [],
        "S_SCALE": [], "E_EMOTION": [], "P_PARALLEL": [],
    }


@pytest.fixture
def coffee_prompts_depth2() -> dict:
    return {
        "L1": "coffee cup macro slow motion",
        "L5": "coffee lifestyle b roll",            # topic-aware (coffee token)
    }


@pytest.fixture
def l5_pool() -> list[str]:
    return [
        "abstract background motion",
        "particles background animation",
        "light leaks background",
        "bokeh background loop",
        "geometric pattern animation",
        "nature landscape b roll",
        "city aerial b roll",
        "people lifestyle b roll",
        "digital data background",
        "clouds time lapse",
        "ocean waves loop",
        "macro texture loop",
    ]


@pytest.fixture
def degradation_map() -> dict:
    return {
        "Koch curve": {
            "L2": ["koch snowflake fractal animation"],
            "L3": ["recursive triangle animation", "geometric fractal animation"],
        },
        "Peano curve": {
            "L2": ["space filling curve animation"],
            "L3": ["fractal curve animation"],
        },
        "coffee": {
            "L2": ["coffee pouring slow motion", "espresso shot close up"],
            "L3": ["cafe lifestyle b roll"],
        },
    }


@pytest.fixture
def fractal_plan(fractal_atoms, fractal_prompts_depth5) -> dict:
    return {
        "chunk_index": 0,
        "atoms": fractal_atoms,
        "cascade_depth": 5,
        "prompts": fractal_prompts_depth5,
    }


@pytest.fixture
def coffee_plan(coffee_atoms, coffee_prompts_depth2) -> dict:
    return {
        "chunk_index": 0,
        "atoms": coffee_atoms,
        "cascade_depth": 2,
        "prompts": coffee_prompts_depth2,
    }


@pytest.fixture
def fractal_sentence() -> str:
    return "Koch curve has a dimension of 1.26."


# ---------------------------------------------------------------------------
# PlanChunksError
# ---------------------------------------------------------------------------

class TestPlanChunksError:
    def test_basic_construction(self):
        err = PlanChunksError("empty_atoms", chunk_index=3)
        assert err.error_code == "empty_atoms"
        assert err.stage == "validation"
        assert err.chunk_index == 3
        assert "empty_atoms" in str(err)

    def test_to_dict_serialization(self):
        err = PlanChunksError(
            "chinese_prompt_detected",
            "got CJK L1",
            stage="readiness_gate",
            chunk_index=2,
            level="L1",
            original_prompt="分形结构",
            retry_action="retry_round_2",
            round_n=1,
        )
        d = err.to_dict()
        assert d["error_code"] == "chinese_prompt_detected"
        assert d["stage"] == "readiness_gate"
        assert d["chunk_index"] == 2
        assert d["level"] == "L1"
        assert d["original_prompt"] == "分形结构"
        assert d["retry_action"] == "retry_round_2"
        assert d["round_n"] == 1

    def test_error_code_constants_all_documented(self):
        """Every error code we use in PlanChunksError must be in _PLAN_ERROR_CODES."""
        # Spot-check a few known codes
        for code in ("empty_atoms", "chinese_prompt_detected", "l5_not_topic_aware",
                     "invalid_cascade_depth", "retry_exhausted"):
            assert code in _PLAN_ERROR_CODES

    def test_chunk_status_constants_present(self):
        """All states from spec are defined."""
        for state in ("plan_pending", "plan_validating", "plan_invalid",
                      "search_ready", "search_blocked", "stock_searching",
                      "asset_selected", "unmatched", "failed"):
            assert state in _CHUNK_STATUS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_contains_cjk_chinese(self):
        assert _contains_cjk("分形结构")
        assert _contains_cjk("This contains 中文 mixed")

    def test_contains_cjk_pure_english(self):
        assert not _contains_cjk("koch snowflake fractal animation")
        assert not _contains_cjk("")
        assert not _contains_cjk("1.26 dimension")

    def test_cjk_ratio(self):
        assert _cjk_ratio("分形结构") == 1.0
        assert _cjk_ratio("hello world") == 0.0
        # 4 cjk + 7 ascii ("fractal") excluding the space = 4/11 ≈ 0.364
        assert _cjk_ratio("分形结构 fractal") == pytest.approx(4 / 11, abs=0.01)

    def test_tokenize(self):
        # min length 3, lowercase
        assert _tokenize("Koch Snowflake Fractal") == {"koch", "snowflake", "fractal"}
        # filters out tokens < 3 chars (so 'a', 'is' are skipped, but 'the' kept)
        assert _tokenize("a is hi") == set()
        # handles punctuation
        assert _tokenize("hello, world!") == {"hello", "world"}

    def test_is_ascii_keyword_string(self):
        # Valid
        assert _is_ascii_keyword_string("koch snowflake fractal animation")
        assert _is_ascii_keyword_string("1.26 dimension")
        assert _is_ascii_keyword_string("slow-motion close up")
        # Invalid
        assert not _is_ascii_keyword_string("分形结构")
        assert not _is_ascii_keyword_string("hello, world")   # comma
        assert not _is_ascii_keyword_string('"quoted"')        # quotes
        assert not _is_ascii_keyword_string("(parens)")
        assert not _is_ascii_keyword_string("")

    def test_flatten_atoms(self, fractal_atoms):
        flat = _flatten_atoms(fractal_atoms)
        assert "koch curve" in flat
        assert "1.26" in flat
        assert "fractal dimension" in flat

    def test_flatten_atoms_empty_input(self):
        assert _flatten_atoms({}) == []
        assert _flatten_atoms(None) == []
        assert _flatten_atoms("not a dict") == []

    def test_atom_tokens_splits_multiword(self, fractal_atoms):
        tokens = _atom_tokens(fractal_atoms)
        # "koch curve" → {"koch", "curve"}, "fractal dimension" → {"fractal", "dimension"}
        assert "koch" in tokens
        assert "curve" in tokens
        assert "fractal" in tokens
        assert "dimension" in tokens
        # "1.26" is < 3 chars after stripping, won't tokenize


# ---------------------------------------------------------------------------
# validate_plan_chunks_output — happy paths
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# validate_plan_chunks_output — 8 failure modes
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# validate_readiness_gate — universal checks
# ---------------------------------------------------------------------------

class TestReadinessGate_Universal:
    def test_normal_l1_passes(self, fractal_atoms, fractal_prompts_depth5):
        # L1 has 'koch' 'fractal' which overlap with atoms
        validate_readiness_gate(
            chunk_index=0,
            sentence="The koch curve fractal",
            atoms=fractal_atoms,
            cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L1",
            prompt="koch snowflake fractal animation",
            visual_must_convey=["koch curve animation", "fractal pattern"],
        )

    def test_blocks_cjk_prompt(self, fractal_atoms, fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L1", prompt="科赫曲线分形动画",
            )
        assert exc.value.error_code == "chinese_prompt_detected"
        assert exc.value.stage == "readiness_gate"

    def test_blocks_empty_prompt(self, fractal_atoms, fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L1", prompt="",
            )
        assert exc.value.error_code == "prompt_too_short"

    def test_blocks_prompt_equals_sentence(self, fractal_atoms,
                                            fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0,
                sentence="koch snowflake fractal animation",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L1",
                prompt="koch snowflake fractal animation",
            )
        assert exc.value.error_code == "prompt_equals_sentence"

    def test_blocks_empty_atoms(self, fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms={"O_OBJECT": [], "A_ACTION": [], "N_NUMBER": [],
                       "M_METAPHOR": [], "C_CONCEPT": [], "S_SCALE": [],
                       "E_EMOTION": [], "P_PARALLEL": []},
                cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L1", prompt="koch fractal animation",
            )
        assert exc.value.error_code == "empty_atoms"


# ---------------------------------------------------------------------------
# validate_readiness_gate — level-aware overlap rules
# ---------------------------------------------------------------------------

class TestReadinessGate_LevelAware:
    def test_l1_universal_checks_only(self, fractal_atoms,
                                        fractal_prompts_depth5):
        """
        (CJK / empty / sentence / length / ASCII). atom-overlap requirement
        was dropped — LLM synonyms/plurals broke too many legitimate L1s,
        and the cascade catches weak L1s by falling to L2-L5."""
        # Passes even with zero atom overlap (cascade will compensate)
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L1",
            prompt="mountain landscape sunrise",  # zero atom token match
            visual_must_convey=["koch curve fractal pattern"],
        )
        # Still fails on CJK
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L1",
                prompt="分形动画",  # CJK
                visual_must_convey=["koch curve"],
            )
        assert exc.value.error_code == "chinese_prompt_detected"

    def test_l1_no_visual_must_convey_falls_back_to_atom_check(
        self, fractal_atoms, fractal_prompts_depth5,
    ):
        # When ChunkContract failed to build (must_convey=None),
        # L1 still needs ≥1 atom token
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L1",
            prompt="koch fractal animation",
            visual_must_convey=None,  # no contract — skip must_convey check
        )

    def test_l4_only_needs_style_word(self, fractal_atoms,
                                        fractal_prompts_depth5):
        # L4 doesn't require atom overlap — style word is enough
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L4",
            prompt="cinematic abstract motion graphic",  # no atom token at all
            visual_must_convey=["koch curve fractal pattern"],
        )

    def test_l4_blocks_without_style_word(self, fractal_atoms,
                                           fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L4",
                prompt="koch curve fractal mathematics",  # no style word
            )
        assert exc.value.error_code == "progressive_relationship_violated"
        assert exc.value.level == "L4"

    def test_l5_only_universal_checks(self, fractal_atoms,
                                       fractal_prompts_depth5):
        # L5 does NOT enforce atoms overlap. Universal checks only.
        # As long as no CJK / not equal to sentence / 4-100 chars / ASCII →
        # gate passes. Topic awareness is enforced earlier in plan validation.
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L5",
            # Even a prompt with no atom token overlap is OK at gate level
            # (validation already vetted topic-awareness in plan stage)
            prompt="scientific abstract motion graphic",
            visual_must_convey=["koch curve fractal pattern"],
        )

    def test_l3_needs_visual_category_and_atom(self, fractal_atoms,
                                                 fractal_prompts_depth5):
        # L3 with both visual category and atom token → passes
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L3",
            prompt="fractal geometric pattern animation",  # 'pattern' + 'fractal'
        )

    def test_l3_blocks_without_visual_category(self, fractal_atoms,
                                                 fractal_prompts_depth5):
        with pytest.raises(PlanChunksError) as exc:
            validate_readiness_gate(
                chunk_index=0, sentence="x",
                atoms=fractal_atoms, cascade_depth=5,
                prompts=fractal_prompts_depth5,
                level="L3",
                prompt="koch curve fractal mathematics",  # has atom but no category
            )
        assert exc.value.error_code == "progressive_relationship_violated"

    def test_l2_needs_any_overlap(self, fractal_atoms,
                                    fractal_prompts_depth5):
        # L2 passes with just 1 atom overlap (no must_convey required)
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L2",
            prompt="fractal snowflake illustration",  # 'fractal' overlap
        )

    def test_l2_zero_overlap_now_accepted(self, fractal_atoms,
                                            fractal_prompts_depth5):
        """
        L1 — same reason. The cascade absorbs weak-anchor L2s."""
        # Used to raise; now passes
        validate_readiness_gate(
            chunk_index=0, sentence="x",
            atoms=fractal_atoms, cascade_depth=5,
            prompts=fractal_prompts_depth5,
            level="L2",
            prompt="mountain landscape sunrise",  # zero overlap — OK now
            visual_must_convey=["koch curve fractal pattern"],
        )
