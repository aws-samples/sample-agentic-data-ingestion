# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/retry_utils.py.

Validates: should_skip_step returns correct boolean for steps before/at/after
retry point; get_cached_step_result returns cached output_data.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub env_utils before importing retry_utils
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
    from retry_utils import get_cached_step_result, should_skip_step
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _retry_utils_uses_the_stub(monkeypatch):
    """Point ``retry_utils``' own ``require_env`` at the stub for each test.

    ``retry_utils`` binds ``require_env`` when it is first imported, so the stub
    above only reaches it if nothing imported it earlier. ``test_retry_resume.py``
    does, against the real ``env_utils``, and outside local mode that raises on
    ``STATE_TABLE``. Patching the binding the function actually reads makes the
    result independent of collection order.
    """
    monkeypatch.setitem(get_cached_step_result.__globals__, "require_env", mock_env_utils.require_env)


STEP_ORDER = [
    "file_validation",
    "schema_extract",
    "schema_compare",
    "schema_mapping",
    "CodeGeneration",
    "CodeTesting",
    "CreateInfrastructure",
    "CreateGluePipeline",
    "CreateQualityChecks",
    "RunPipeline",
]


class TestShouldSkipStep:
    """Tests for should_skip_step utility function."""

    def test_skip_step_before_retry_point(self):
        """Steps before the retry_from_step should be skipped."""
        event = {"retry_from_step": "schema_compare"}
        assert should_skip_step(event, "file_validation", STEP_ORDER) is True
        assert should_skip_step(event, "schema_extract", STEP_ORDER) is True

    def test_no_skip_at_retry_point(self):
        """The retry_from_step itself should NOT be skipped."""
        event = {"retry_from_step": "schema_compare"}
        assert should_skip_step(event, "schema_compare", STEP_ORDER) is False

    def test_no_skip_after_retry_point(self):
        """Steps after the retry_from_step should NOT be skipped."""
        event = {"retry_from_step": "schema_compare"}
        assert should_skip_step(event, "schema_mapping", STEP_ORDER) is False
        assert should_skip_step(event, "CodeGeneration", STEP_ORDER) is False

    def test_no_skip_when_no_retry_from_step(self):
        """When retry_from_step is absent, no steps should be skipped."""
        event = {}
        assert should_skip_step(event, "file_validation", STEP_ORDER) is False

    def test_no_skip_when_retry_from_step_is_none(self):
        """When retry_from_step is None, no steps should be skipped."""
        event = {"retry_from_step": None}
        assert should_skip_step(event, "file_validation", STEP_ORDER) is False

    def test_no_skip_for_unknown_current_step(self):
        """Unknown current step should not be skipped."""
        event = {"retry_from_step": "schema_compare"}
        assert should_skip_step(event, "unknown_step", STEP_ORDER) is False

    def test_no_skip_when_retry_from_first_step(self):
        """When retrying from the first step, nothing should be skipped."""
        event = {"retry_from_step": "file_validation"}
        assert should_skip_step(event, "file_validation", STEP_ORDER) is False

    def test_skip_all_before_last_step(self):
        """When retrying from the last step, all prior steps should be skipped."""
        event = {"retry_from_step": "RunPipeline"}
        assert should_skip_step(event, "file_validation", STEP_ORDER) is True
        assert should_skip_step(event, "CreateQualityChecks", STEP_ORDER) is True
        assert should_skip_step(event, "RunPipeline", STEP_ORDER) is False


class TestGetCachedStepResult:
    """Tests for get_cached_step_result utility function."""

    @patch("retry_utils.boto3")
    def test_returns_output_data_for_success_step(self, mock_boto3):
        """Should return output_data when step status is success."""
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "step_name": "file_validation",
                "status": "success",
                "output_data": {"validated": True, "file_size": 1024},
            }
        }

        result = get_cached_step_result("run-123", "file_validation")

        assert result is not None
        assert result["validated"] is True
        assert result["file_size"] == 1024

    @patch("retry_utils.boto3")
    def test_returns_none_for_failed_step(self, mock_boto3):
        """Should return None when step status is not success."""
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "step_name": "schema_extract",
                "status": "error",
                "output_data": {},
            }
        }

        result = get_cached_step_result("run-123", "schema_extract")
        assert result is None

    @patch("retry_utils.boto3")
    def test_returns_none_for_missing_step(self, mock_boto3):
        """Should return None when step entry doesn't exist."""
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {}

        result = get_cached_step_result("run-123", "nonexistent_step")
        assert result is None

    @patch("retry_utils.boto3")
    def test_returns_none_on_dynamodb_error(self, mock_boto3):
        """Should return None gracefully when DynamoDB raises an error."""
        mock_boto3.resource.side_effect = Exception("DynamoDB error")

        result = get_cached_step_result("run-123", "file_validation")
        assert result is None


class TestStepOrderConstant:
    """Tests for the STEP_ORDER constant in retry_utils.

    Validates: Requirements 5.1
    """

    def test_step_order_has_exactly_13_steps(self):
        """STEP_ORDER must contain exactly 13 pipeline steps."""
        from retry_utils import STEP_ORDER as CANONICAL_STEP_ORDER

        assert len(CANONICAL_STEP_ORDER) == 13

    def test_step_order_first_step_is_file_validation(self):
        """First step must be FileValidation."""
        from retry_utils import STEP_ORDER as CANONICAL_STEP_ORDER

        assert CANONICAL_STEP_ORDER[0] == "FileValidation"

    def test_step_order_last_step_is_run_pipeline(self):
        """Last step must be RunPipeline."""
        from retry_utils import STEP_ORDER as CANONICAL_STEP_ORDER

        assert CANONICAL_STEP_ORDER[-1] == "RunPipeline"

    def test_step_order_matches_state_machine_definition(self):
        """STEP_ORDER must match the full state machine step sequence."""
        from retry_utils import STEP_ORDER as CANONICAL_STEP_ORDER

        expected = [
            "FileValidation",
            "SchemaExtract",
            "SchemaCompare",
            "CheckSchemaAction",
            "SchemaMapping",
            "CheckSchemaMappingApproval",
            "CreateCode",
            "TestCode",
            "CheckCodeApproval",
            "CreateInfrastructure",
            "CreateGluePipeline",
            "CreateQualityChecks",
            "RunPipeline",
        ]
        assert expected == CANONICAL_STEP_ORDER


class TestStepOrderAdoptsSharedStepNames:
    """P28 R8.2 (task 14.2): STEP_ORDER is the shared SFN_STATE_ORDER.

    Validates: Requirements 8.2
    """

    def test_step_order_is_shared_sfn_state_order(self):
        from retry_utils import STEP_ORDER as CANONICAL_STEP_ORDER
        from step_names import SFN_STATE_ORDER

        assert list(SFN_STATE_ORDER) == CANONICAL_STEP_ORDER


class TestShouldSkipStepNormalizesVariants:
    """P28 R8.2 (task 14.2): both sides of the retry comparison normalize
    via from_any, so a snake_case failed_step from a Failure_Record and the
    PascalCase SFN ids in STEP_ORDER can no longer silently miss.

    Validates: Requirements 8.2
    """

    def test_snake_case_current_step_matches_pascal_step_order(self):
        """The R8.2 gap: snake current_step vs Pascal STEP_ORDER used to
        yield current_idx=-1 (never skip). Now they meet on the canonical
        StepName."""
        from retry_utils import STEP_ORDER as PASCAL_ORDER

        event = {"retry_from_step": "CreateCode"}
        assert should_skip_step(event, "file_validation", PASCAL_ORDER) is True
        assert should_skip_step(event, "schema_mapping", PASCAL_ORDER) is True
        assert should_skip_step(event, "create_code", PASCAL_ORDER) is False
        assert should_skip_step(event, "test_code", PASCAL_ORDER) is False

    def test_snake_case_retry_from_step_matches_pascal_entries(self):
        """A Failure_Record's snake_case failed_step as retry_from_step."""
        from retry_utils import STEP_ORDER as PASCAL_ORDER

        event = {"retry_from_step": "create_code"}
        assert should_skip_step(event, "SchemaMapping", PASCAL_ORDER) is True
        assert should_skip_step(event, "CreateCode", PASCAL_ORDER) is False

    def test_legacy_variant_names_normalize_too(self):
        from retry_utils import STEP_ORDER as PASCAL_ORDER

        event = {"retry_from_step": "TestCode"}
        # Legacy "CodeGeneration" state rows canonicalize to create_code
        assert should_skip_step(event, "CodeGeneration", PASCAL_ORDER) is True
        # Legacy "CodeTesting" canonicalizes to test_code — the retry point
        assert should_skip_step(event, "CodeTesting", PASCAL_ORDER) is False

    def test_unknown_retry_from_step_never_skips(self):
        from retry_utils import STEP_ORDER as PASCAL_ORDER

        event = {"retry_from_step": "not_a_step"}
        assert should_skip_step(event, "file_validation", PASCAL_ORDER) is False
