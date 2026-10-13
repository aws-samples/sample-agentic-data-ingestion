# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/step_names.py (P28 R8.2 — Canonical_Step_Names).

Validates: 1:1 correspondence with retry_utils.STEP_ORDER (completeness and
order), canonical→SFN→canonical round-trip, from_any on every cataloged
legacy variant, unknown-value errors naming the input, and step_sequence
consistency with the values the task-step Lambdas write.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Stub env_utils before importing retry_utils (same convention as
# test_retry_utils.py — retry_utils imports it at module level).
mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

# Add shared directory to path
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

try:
    from retry_utils import STEP_ORDER
    from step_names import (
        CANONICAL_ORDER,
        SFN_STATE_NAMES,
        SFN_STATE_ORDER,
        STEP_SEQUENCE,
        TASK_STEPS,
        StepName,
        from_any,
        to_sfn_state_name,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


class TestCompletenessVsRetryUtils:
    """The enum must correspond 1:1 with retry_utils.STEP_ORDER."""

    def test_every_step_order_entry_maps_to_exactly_one_member(self):
        """Each STEP_ORDER name parses to a distinct enum member."""
        mapped = [from_any(name) for name in STEP_ORDER]
        assert len(mapped) == len(set(mapped)), "STEP_ORDER entries collapsed onto the same member"
        assert set(mapped) == set(StepName), "enum members missing from or exceeding STEP_ORDER"

    def test_every_member_has_an_sfn_state_name_in_step_order(self):
        """Each member's SFN state id is present in STEP_ORDER."""
        for step in StepName:
            assert to_sfn_state_name(step) in STEP_ORDER, f"{step!r} has no STEP_ORDER counterpart"

    def test_sfn_state_names_covers_every_member(self):
        """The explicit SFN mapping has an entry for every enum member."""
        assert set(SFN_STATE_NAMES.keys()) == set(StepName)


class TestOrderConsistency:
    """CANONICAL_ORDER must match STEP_ORDER's ordering via the mapping."""

    def test_sfn_state_order_equals_retry_utils_step_order(self):
        """SFN_STATE_ORDER is the drop-in replacement for STEP_ORDER (14.2)."""
        assert list(SFN_STATE_ORDER) == STEP_ORDER

    def test_canonical_order_covers_every_member_once(self):
        assert len(CANONICAL_ORDER) == len(StepName)
        assert set(CANONICAL_ORDER) == set(StepName)

    def test_canonical_order_maps_elementwise_to_step_order(self):
        for step, sfn_name in zip(CANONICAL_ORDER, STEP_ORDER, strict=True):
            assert to_sfn_state_name(step) == sfn_name


class TestRoundTrip:
    """canonical → SFN state id → canonical is the identity."""

    @pytest.mark.parametrize("step", list(StepName), ids=[s.value for s in StepName])
    def test_round_trip(self, step):
        assert from_any(to_sfn_state_name(step)) is step

    @pytest.mark.parametrize("step", list(StepName), ids=[s.value for s in StepName])
    def test_canonical_value_parses_to_itself(self, step):
        assert from_any(step.value) is step


class TestFromAnyVariants:
    """from_any parses every variant cataloged in the codebase."""

    # (variant as found in the codebase, expected member, source)
    CATALOGED_VARIANTS = [
        # DynamoDB state-row step_name values written by the Lambdas today
        ("file_validation", StepName.FILE_VALIDATION, "file_validation put_item"),
        ("schema_extract", StepName.SCHEMA_EXTRACT, "schema_extract put_item"),
        ("schema_compare", StepName.SCHEMA_COMPARE, "schema_compare put_item"),
        ("schema_mapping", StepName.SCHEMA_MAPPING, "schema_mapping put_item"),
        ("CodeGeneration", StepName.CREATE_CODE, "create_code put_item (legacy)"),
        ("CodeTesting", StepName.TEST_CODE, "test_code put_item (legacy)"),
        ("CreateInfrastructure", StepName.CREATE_INFRASTRUCTURE, "create_infrastructure put_item"),
        ("CreateGluePipeline", StepName.CREATE_GLUE_PIPELINE, "create_glue_pipeline put_item"),
        ("CreateQualityChecks", StepName.CREATE_QUALITY_CHECKS, "create_quality_checks put_item"),
        ("RunPipeline", StepName.RUN_PIPELINE, "run_pipeline put_item"),
        # snake_case step_handler / Failure_Record names
        ("create_code", StepName.CREATE_CODE, "@step_handler"),
        ("test_code", StepName.TEST_CODE, "@step_handler"),
        ("create_infrastructure", StepName.CREATE_INFRASTRUCTURE, "@step_handler"),
        ("create_glue_pipeline", StepName.CREATE_GLUE_PIPELINE, "@step_handler"),
        ("create_quality_checks", StepName.CREATE_QUALITY_CHECKS, "@step_handler"),
        ("run_pipeline", StepName.RUN_PIPELINE, "@step_handler / costs.py"),
        # PascalCase SFN state ids (retry_utils STEP_ORDER / logger keys)
        ("FileValidation", StepName.FILE_VALIDATION, "STEP_ORDER"),
        ("SchemaExtract", StepName.SCHEMA_EXTRACT, "STEP_ORDER"),
        ("SchemaCompare", StepName.SCHEMA_COMPARE, "STEP_ORDER"),
        ("CheckSchemaAction", StepName.CHECK_SCHEMA_ACTION, "STEP_ORDER"),
        ("SchemaMapping", StepName.SCHEMA_MAPPING, "STEP_ORDER"),
        ("CheckSchemaMappingApproval", StepName.CHECK_SCHEMA_MAPPING_APPROVAL, "STEP_ORDER"),
        ("CreateCode", StepName.CREATE_CODE, "STEP_ORDER"),
        ("TestCode", StepName.TEST_CODE, "STEP_ORDER"),
        ("CheckCodeApproval", StepName.CHECK_CODE_APPROVAL, "STEP_ORDER"),
        # Legacy third variants
        ("code_generation", StepName.CREATE_CODE, "approval_type / phantom rows"),
        ("code_testing", StepName.TEST_CODE, "approval_type map / agent traces"),
        ("schema_mapping_approval", StepName.CHECK_SCHEMA_MAPPING_APPROVAL, "record_failure PROGRESS_ORDER"),
        ("code_approval", StepName.CHECK_CODE_APPROVAL, "record_failure PROGRESS_ORDER"),
        ("infrastructure", StepName.CREATE_INFRASTRUCTURE, "approval_type / phantom rows"),
        ("first_run", StepName.CREATE_QUALITY_CHECKS, "approval_type / phantom rows"),
    ]

    @pytest.mark.parametrize(
        "variant,expected",
        [(v, e) for v, e, _ in CATALOGED_VARIANTS],
        ids=[f"{v}->{e.value}" for v, e, _ in CATALOGED_VARIANTS],
    )
    def test_cataloged_variant_parses(self, variant, expected):
        assert from_any(variant) is expected

    @pytest.mark.parametrize(
        "variant,expected",
        [
            ("FILE_VALIDATION", StepName.FILE_VALIDATION),
            ("Run-Pipeline", StepName.RUN_PIPELINE),
            ("codegeneration", StepName.CREATE_CODE),
            ("checkcodeapproval", StepName.CHECK_CODE_APPROVAL),
        ],
    )
    def test_casing_and_separator_insensitive(self, variant, expected):
        """Matching tolerates case and _/- separator slop of known names."""
        assert from_any(variant) is expected

    def test_accepts_step_name_instance(self):
        assert from_any(StepName.TEST_CODE) is StepName.TEST_CODE


class TestFromAnyUnknown:
    """Unknown values raise ValueError naming the offending input."""

    @pytest.mark.parametrize("bogus", ["not_a_step", "pipeline_failure", "self_healing", "data_quality", "cache", ""])
    def test_unknown_value_raises_naming_input(self, bogus):
        with pytest.raises(ValueError, match=repr(bogus) if bogus else "''"):
            from_any(bogus)

    def test_error_message_names_the_value(self):
        with pytest.raises(ValueError) as excinfo:
            from_any("SchemaMappping")  # typo — three p's
        assert "SchemaMappping" in str(excinfo.value)


class TestStepSequence:
    """step_sequence values match what the task-step Lambdas write today."""

    # Cataloged from lambda/*/lambda_function.py put_item calls.
    EXPECTED_SEQUENCES = {
        StepName.FILE_VALIDATION: 1,
        StepName.SCHEMA_EXTRACT: 2,
        StepName.SCHEMA_COMPARE: 3,
        StepName.SCHEMA_MAPPING: 4,
        StepName.CREATE_CODE: 5,
        StepName.TEST_CODE: 6,
        StepName.CREATE_INFRASTRUCTURE: 7,
        StepName.CREATE_GLUE_PIPELINE: 8,
        StepName.CREATE_QUALITY_CHECKS: 9,
        StepName.RUN_PIPELINE: 10,
    }

    def test_sequence_values_match_lambda_catalog(self):
        assert dict(STEP_SEQUENCE) == self.EXPECTED_SEQUENCES

    def test_task_steps_are_canonical_order_minus_choice_gates(self):
        """TASK_STEPS = the 10 state-row writers, in execution order."""
        gates = {
            StepName.CHECK_SCHEMA_ACTION,
            StepName.CHECK_SCHEMA_MAPPING_APPROVAL,
            StepName.CHECK_CODE_APPROVAL,
        }
        assert tuple(s for s in CANONICAL_ORDER if s not in gates) == TASK_STEPS
        assert set(TASK_STEPS) == set(STEP_SEQUENCE.keys())

    def test_sequence_increases_monotonically_in_execution_order(self):
        seqs = [STEP_SEQUENCE[s] for s in TASK_STEPS]
        assert seqs == sorted(seqs)
        assert seqs == list(range(1, len(TASK_STEPS) + 1))


class TestStrBehavior:
    """Members are strings usable directly in keys and payloads."""

    def test_fstring_yields_canonical_value(self):
        assert f"{StepName.FILE_VALIDATION}" == "file_validation"

    def test_member_equals_its_value(self):
        assert StepName.RUN_PIPELINE == "run_pipeline"

    def test_json_serializes_to_value(self):
        import json

        assert json.dumps({"step_name": StepName.SCHEMA_MAPPING}) == '{"step_name": "schema_mapping"}'
