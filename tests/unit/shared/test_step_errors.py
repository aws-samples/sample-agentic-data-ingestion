# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/step_errors.py (P28 R1 error-propagation foundation).

Covers:
- StepFailure attributes and the stable str() JSON serialization contract
  consumed by the RecordFailure Lambda (step_name, error_type, message, details)
- TransientStepFailure subclass behavior (error_type="transient", SFN-matchable name)
- step_handler decorator: happy passthrough, StepFailure re-raise (no double-wrap),
  unexpected-exception conversion with cause preservation, never returning an
  error payload

Validates: Requirements 1.1
"""

import json
import logging
import os
import sys

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from botocore.exceptions import ClientError

from step_errors import (
    TRANSIENT_CLIENT_ERROR_CODES,
    StepFailure,
    TransientStepFailure,
    raise_for_read_client_error,
    step_handler,
)


class TestStepFailureConstruction:
    """StepFailure carries step_name, message, error_type, and details."""

    def test_defaults_to_permanent_with_empty_details(self):
        exc = StepFailure("schema_mapping", "mapping generation failed")
        assert exc.step_name == "schema_mapping"
        assert exc.message == "mapping generation failed"
        assert exc.error_type == "permanent"
        assert exc.details == {}

    def test_explicit_transient_error_type_via_kwarg(self):
        exc = StepFailure("run_pipeline", "throttled", error_type="transient")
        assert exc.error_type == "transient"

    def test_details_are_stored(self):
        details = {"missing_field": "customer_id", "mapping_id": "map-123"}
        exc = StepFailure("create_code", "missing field mapping", details=details)
        assert exc.details == details

    def test_invalid_error_type_raises_value_error(self):
        with pytest.raises(ValueError, match="error_type"):
            StepFailure("test_code", "boom", error_type="retryable")  # type: ignore[arg-type]

    def test_is_an_exception(self):
        assert issubclass(StepFailure, Exception)


class TestSerializationContract:
    """str(exc) is the stable JSON document parsed by RecordFailure (task 1.4)."""

    def test_str_is_json_with_exact_documented_keys(self):
        exc = StepFailure("file_validation", "file too large", details={"size": 42})
        parsed = json.loads(str(exc))
        assert set(parsed.keys()) == {"step_name", "error_type", "message", "details"}

    def test_str_json_carries_field_values(self):
        exc = StepFailure(
            "file_validation",
            "file too large",
            error_type="transient",
            details={"size": 42},
        )
        parsed = json.loads(str(exc))
        assert parsed["step_name"] == "file_validation"
        assert parsed["error_type"] == "transient"
        assert parsed["message"] == "file too large"
        assert parsed["details"] == {"size": 42}

    def test_to_dict_matches_str_serialization(self):
        exc = StepFailure("schema_mapping", "boom", details={"a": 1})
        assert exc.to_dict() == json.loads(str(exc))

    @pytest.mark.parametrize(
        "message",
        [
            "quotes \" and 'single'",
            "newlines\nand\ttabs",
            "unicode — ключ 键 🔥",
            "",
        ],
    )
    def test_str_roundtrips_tricky_messages(self, message):
        exc = StepFailure("test_code", message)
        assert json.loads(str(exc))["message"] == message

    def test_non_json_serializable_details_are_stringified(self):
        """default=str degrades non-JSON values instead of raising."""

        class Opaque:
            def __str__(self):
                return "opaque-object"

        exc = StepFailure("create_code", "boom", details={"obj": Opaque()})
        parsed = json.loads(str(exc))
        assert parsed["details"]["obj"] == "opaque-object"

    def test_circular_details_fall_back_to_minimal_document(self):
        """Constructing the exception never raises because of bad details."""
        circular: dict = {}
        circular["self"] = circular
        exc = StepFailure("create_code", "boom", details=circular)
        parsed = json.loads(str(exc))
        assert parsed["step_name"] == "create_code"
        assert parsed["message"] == "boom"
        assert parsed["details"] == {"serialization_error": "details were not JSON-serializable"}


class TestTransientStepFailure:
    """TransientStepFailure is the SFN-retryable variant."""

    def test_error_type_is_transient(self):
        exc = TransientStepFailure("run_pipeline", "throttled")
        assert exc.error_type == "transient"

    def test_is_a_step_failure(self):
        exc = TransientStepFailure("run_pipeline", "throttled")
        assert isinstance(exc, StepFailure)

    def test_details_and_serialization(self):
        exc = TransientStepFailure("job_completion", "record not yet visible", details={"run_id": "r-1"})
        parsed = json.loads(str(exc))
        assert parsed["error_type"] == "transient"
        assert parsed["details"] == {"run_id": "r-1"}

    def test_class_names_are_the_sfn_errorequals_contract(self):
        """SFN Retry/Catch ErrorEquals match the class NAME — renaming breaks ASL."""
        assert StepFailure.__name__ == "StepFailure"
        assert TransientStepFailure.__name__ == "TransientStepFailure"


class TestStepHandlerDecorator:
    """step_handler wraps Step_Lambda entrypoints: passthrough or raise, never an error payload."""

    def test_successful_return_passes_through_unchanged(self):
        @step_handler("file_validation")
        def handler(event, context):
            return {"statusCode": 200, "validated": True, "event": event}

        result = handler({"key": "data.csv"}, None)
        assert result == {"statusCode": 200, "validated": True, "event": {"key": "data.csv"}}

    def test_arguments_are_forwarded(self):
        @step_handler("schema_mapping")
        def handler(event, context):
            return (event, context)

        event, context = handler({"a": 1}, "ctx")
        assert event == {"a": 1}
        assert context == "ctx"

    def test_step_failure_is_reraised_not_double_wrapped(self):
        original = StepFailure("create_code", "missing mapping", details={"field": "id"})

        @step_handler("create_code")
        def handler(event, context):
            raise original

        with pytest.raises(StepFailure) as exc_info:
            handler({}, None)
        assert exc_info.value is original

    def test_transient_step_failure_type_is_preserved(self):
        """Re-raise must preserve the subclass so SFN Retry ErrorEquals still match."""

        @step_handler("run_pipeline")
        def handler(event, context):
            raise TransientStepFailure("run_pipeline", "throttled")

        with pytest.raises(TransientStepFailure):
            handler({}, None)

    def test_unexpected_exception_is_converted_to_step_failure(self):
        @step_handler("test_code")
        def handler(event, context):
            raise KeyError("source_bucket")

        with pytest.raises(StepFailure) as exc_info:
            handler({}, None)
        exc = exc_info.value
        assert type(exc) is StepFailure
        assert exc.step_name == "test_code"
        assert exc.error_type == "permanent"
        assert "KeyError" in exc.message
        assert "source_bucket" in exc.message
        assert exc.details["original_error_type"] == "KeyError"

    def test_converted_exception_preserves_original_as_cause(self):
        original = ValueError("bad config")

        @step_handler("create_infrastructure")
        def handler(event, context):
            raise original

        with pytest.raises(StepFailure) as exc_info:
            handler({}, None)
        assert exc_info.value.__cause__ is original

    def test_converted_exception_serializes_for_record_failure(self):
        @step_handler("schema_mapping")
        def handler(event, context):
            raise RuntimeError("bedrock unavailable")

        with pytest.raises(StepFailure) as exc_info:
            handler({}, None)
        parsed = json.loads(str(exc_info.value))
        assert parsed["step_name"] == "schema_mapping"
        assert parsed["error_type"] == "permanent"
        assert "bedrock unavailable" in parsed["message"]

    def test_base_exceptions_are_not_converted(self):
        """SystemExit/KeyboardInterrupt must not be swallowed into StepFailure."""

        @step_handler("file_validation")
        def handler(event, context):
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            handler({}, None)

    def test_wraps_preserves_function_metadata(self):
        @step_handler("file_validation")
        def lambda_handler(event, context):
            """Original docstring."""
            return {}

        assert lambda_handler.__name__ == "lambda_handler"
        assert lambda_handler.__doc__ == "Original docstring."

    def test_failure_is_logged(self, caplog):
        @step_handler("create_code")
        def handler(event, context):
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, logger="step_errors"), pytest.raises(StepFailure):
            handler({}, None)
        assert any("create_code" in record.message for record in caplog.records)

    def test_step_failure_reraise_is_logged(self, caplog):
        @step_handler("run_pipeline")
        def handler(event, context):
            raise TransientStepFailure("run_pipeline", "throttled")

        with caplog.at_level(logging.ERROR, logger="step_errors"), pytest.raises(TransientStepFailure):
            handler({}, None)
        assert any("run_pipeline" in record.message for record in caplog.records)


class TestRaiseForReadClientError:
    """P28 R17.3: read-path ClientError classification.

    Not-found codes return normally (caller keeps its not-found value);
    transient infra codes raise TransientStepFailure; everything else
    raises StepFailure.

    Validates: P28 Requirements 17.3
    """

    @staticmethod
    def _client_error(code: str, message: str = "simulated") -> "ClientError":
        return ClientError({"Error": {"Code": code, "Message": message}}, "GetItem")

    @pytest.mark.parametrize(
        "code",
        ["ResourceNotFoundException", "NoSuchKey", "NoSuchBucket", "404", "NotFound"],
    )
    def test_not_found_codes_return_normally(self, code):
        exc = self._client_error(code)
        # Returns None without raising — the caller then returns its not-found value.
        assert raise_for_read_client_error(exc, "schema_mapping", operation="get_source_schema") is None

    @pytest.mark.parametrize(
        "code",
        [
            "ProvisionedThroughputExceededException",
            "ThrottlingException",
            "RequestLimitExceeded",
            "ServiceUnavailable",
            "InternalServerError",
        ],
    )
    def test_transient_codes_raise_transient_step_failure(self, code):
        exc = self._client_error(code)
        with pytest.raises(TransientStepFailure) as exc_info:
            raise_for_read_client_error(exc, "schema_mapping", operation="get_source_schema")
        raised = exc_info.value
        assert raised.step_name == "schema_mapping"
        assert raised.error_type == "transient"
        assert raised.details["error_code"] == code
        assert raised.details["operation"] == "get_source_schema"
        assert raised.__cause__ is exc

    @pytest.mark.parametrize("code", ["AccessDeniedException", "AccessDenied", "ValidationException"])
    def test_permanent_codes_raise_step_failure(self, code):
        exc = self._client_error(code)
        with pytest.raises(StepFailure) as exc_info:
            raise_for_read_client_error(exc, "create_code", operation="get_pipeline_metadata")
        raised = exc_info.value
        assert not isinstance(raised, TransientStepFailure)
        assert raised.step_name == "create_code"
        assert raised.error_type == "permanent"
        assert raised.details["error_code"] == code
        assert raised.__cause__ is exc

    def test_unknown_code_defaults_to_permanent(self):
        exc = self._client_error("SomethingNovel")
        with pytest.raises(StepFailure) as exc_info:
            raise_for_read_client_error(exc, "test_code", operation="get_pipeline_metadata")
        assert not isinstance(exc_info.value, TransientStepFailure)

    def test_message_carries_operation_code_and_aws_message(self):
        exc = self._client_error("ThrottlingException", message="Rate exceeded")
        with pytest.raises(TransientStepFailure) as exc_info:
            raise_for_read_client_error(exc, "schema_compare", operation="get_target_schema_from_s3")
        msg = exc_info.value.message
        assert "get_target_schema_from_s3" in msg
        assert "ThrottlingException" in msg
        assert "Rate exceeded" in msg

    def test_transient_codes_shared_with_agentcore_proxy(self):
        """Single source of truth — the proxy classification cannot drift (P28 R17.3)."""
        import agentcore_proxy

        assert agentcore_proxy._TRANSIENT_CLIENT_ERROR_CODES == TRANSIENT_CLIENT_ERROR_CODES
