# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The retry resume mechanism — ``should_skip_step``'s production caller (p32 2.18).

``should_skip_step`` shipped in P16 (`LGR-2600abb-1`) with the deciding half
built and the acting half reverted ("Revert orchestration-stack.ts to original
linear chain (CDK skip states removed)"). Task 12 confirmed the consequence
live: a retry-from-step execution re-entered ``FileValidation``, the step RAN,
and its output **overwrote** the pre-seeded ``file_validation_result``, after
which ``$.file_validation_result.run_id`` could not be found.

``retry_utils.resolve_step_resume`` is the acting half. These tests pin the
four things that make it safe, each of which is a way the naive version fails:

1. A step before the resume point is served from cache — the wrapped handler
   never runs, and the value that lands at the step's ``resultPath`` is the
   value the ORIGINAL run put there. That is what makes the ``{step}_result``
   key survive the step it belongs to (task 12's design consequence).
2. With nothing cached to serve, the step RUNS. Skipping with nothing to
   serve writes ``null`` at the ``resultPath`` and breaks the next state —
   task 12's failure by another route.
3. The cache stores the step's Step Functions RETURN contract, not the
   ``output_data`` display artifact on the same row. ``create_output_data``
   fixes ``output_data`` to five keys that carry none of the fields
   downstream ASL dereferences, so serving it would fail identically.
4. A fresh (non-retry) run is untouched: no skip, and the same value the
   handler returned (3.18).

**Validates: Requirements 2.18, 3.18**
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import retry_utils
from artifact_schema import create_output_data
from retry_utils import (
    RESUME_MARKER_KEY,
    RESUMED_FROM_CACHE_KEY,
    SFN_RESULT_ATTRIBUTE,
    cache_step_result,
    get_cached_sfn_result,
    resolve_step_resume,
    resume_marker,
)
from step_errors import step_handler

STATE_TABLE = "agentic_data_ingestion_state"

# The value FileValidation actually returns to Step Functions
# (file_validation/lambda_function.py's success return) — the shape every
# downstream Payload dereferences.
FILE_VALIDATION_SFN_RESULT = {
    "statusCode": 200,
    "run_id": "run-2026-08-04-00-00-00-abcd1234",
    "original_pipeline_id": "customer-pipeline",
    "source_format": "csv",
    "file_size_bytes": 102,
    "file_count": 1,
    RESUME_MARKER_KEY: "",
}


@pytest.fixture
def state_table(mock_dynamodb_tables, monkeypatch):
    """The real moto state table, with retry_utils pointed at it.

    The shared fixture prefixes table names per session, so the env var must
    carry the ACTUAL name — not the logical one — or every read silently
    returns None and these tests pass for the wrong reason.
    """
    table = mock_dynamodb_tables[STATE_TABLE]
    monkeypatch.setenv("STATE_TABLE", table.table_name)
    # The shared fixture creates its moto tables in us-east-1 explicitly;
    # retry_utils builds an unpinned resource, so without this the lookups
    # land in the ambient region and every read returns None — which would
    # make these tests pass for the wrong reason.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return table


def _write_completed_step(table, run_id: str, step_name: str, sfn_result: dict | None) -> None:
    """Write the state row a completed step leaves behind.

    ``output_data`` is built by the real ``create_output_data`` so the
    distinction test 3 rests on is not a fiction invented here.
    """
    item = {
        "run_id": run_id,
        "step_name": step_name,
        "status": "success",
        "output_data": create_output_data(
            artifact_type="file_validation",
            primary_id=run_id,
            status="success",
            summary={"file_name": "e2e.csv", "file_size_bytes": 102},
            s3_uris={"source_file": "s3://bucket/e2e.csv"},
        ),
    }
    if sfn_result is not None:
        item[SFN_RESULT_ATTRIBUTE] = sfn_result
    table.put_item(Item=item)


class TestResumeMarker:
    """The marker is always a string, so an ASL Payload path always resolves."""

    def test_absent_marker_is_the_empty_string(self):
        assert resume_marker({}) == ""

    def test_non_string_marker_is_the_empty_string(self):
        # A malformed value must not become an absent JSONPath downstream.
        assert resume_marker({RESUME_MARKER_KEY: 7}) == ""

    def test_present_marker_is_returned_verbatim(self):
        assert resume_marker({RESUME_MARKER_KEY: "CreateCode"}) == "CreateCode"


class TestCacheRoundTrip:
    """The cache stores the SFN return contract and reads it back unchanged."""

    def test_a_completed_step_result_round_trips(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", None)

        assert cache_step_result(run_id, "file_validation", FILE_VALIDATION_SFN_RESULT) is True
        assert get_cached_sfn_result(run_id, "file_validation") == FILE_VALIDATION_SFN_RESULT

    def test_the_cache_never_creates_a_row(self, state_table):
        """attribute_exists(run_id) — no phantom state row for the dashboard.

        Same technique as `LGR-620555b-1`'s ghost-approval condition: a step
        that never wrote its own row must not get one written for it, because
        the dashboard's readers treat a state row as a step.
        """
        assert cache_step_result("run-never-existed", "file_validation", FILE_VALIDATION_SFN_RESULT) is False
        assert "Item" not in state_table.get_item(Key={"run_id": "run-never-existed", "step_name": "file_validation"})

    def test_a_failed_step_is_not_served(self, state_table):
        run_id = "run-failed"
        state_table.put_item(
            Item={
                "run_id": run_id,
                "step_name": "file_validation",
                "status": "error",
                SFN_RESULT_ATTRIBUTE: FILE_VALIDATION_SFN_RESULT,
            }
        )
        assert get_cached_sfn_result(run_id, "file_validation") is None

    def test_step_name_variants_meet_on_one_key(self, state_table):
        """A PascalCase writer and a snake_case reader must not miss (P28 R8.2)."""
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", None)

        assert cache_step_result(run_id, "FileValidation", FILE_VALIDATION_SFN_RESULT) is True
        assert get_cached_sfn_result(run_id, "file_validation") == FILE_VALIDATION_SFN_RESULT

    def test_a_non_dict_return_is_not_cached(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", None)
        assert cache_step_result(run_id, "file_validation", "not a payload") is False


class TestResolveStepResume:
    """The decision: serve the cached result, or execute the step."""

    def test_a_step_before_the_resume_point_is_served_from_cache(self, state_table):
        """2.18: not silently re-executed, and its cached result not discarded."""
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", FILE_VALIDATION_SFN_RESULT)

        served = resolve_step_resume(
            {"run_id": run_id, RESUME_MARKER_KEY: "CreateCode"},
            "file_validation",
        )

        assert served is not None
        # Every field a downstream Payload dereferences survives verbatim.
        for field in ("run_id", "original_pipeline_id", "source_format", "file_size_bytes", "file_count"):
            assert served[field] == FILE_VALIDATION_SFN_RESULT[field]
        assert served[RESUMED_FROM_CACHE_KEY] is True
        # The marker stays alive across a served step, so the steps after it
        # can still see where the run is resuming.
        assert served[RESUME_MARKER_KEY] == "CreateCode"

    def test_the_step_at_the_resume_point_runs(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "schema_extract", {"statusCode": 200})

        assert resolve_step_resume({"run_id": run_id, RESUME_MARKER_KEY: "SchemaExtract"}, "schema_extract") is None

    def test_a_step_after_the_resume_point_runs(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "schema_mapping", {"statusCode": 200})

        assert resolve_step_resume({"run_id": run_id, RESUME_MARKER_KEY: "SchemaExtract"}, "schema_mapping") is None

    def test_no_marker_means_no_skipping(self, state_table):
        """3.18: a fresh run is unaffected even with a warm cache."""
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", FILE_VALIDATION_SFN_RESULT)

        assert resolve_step_resume({"run_id": run_id}, "file_validation") is None
        assert resolve_step_resume({"run_id": run_id, RESUME_MARKER_KEY: ""}, "file_validation") is None

    def test_nothing_cached_means_the_step_runs(self, state_table):
        """Constraint 2: refuse to skip with nothing to serve.

        A retry of a run recorded before the cache existed lands here. Skipping
        would write null at the resultPath — task 12's failure by another route.
        """
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", None)

        assert resolve_step_resume({"run_id": run_id, RESUME_MARKER_KEY: "CreateCode"}, "file_validation") is None

    def test_the_output_data_artifact_is_never_served(self, state_table):
        """Constraint 1: output_data is not the resultPath contract.

        ``create_output_data`` fixes it to five keys — none of which is
        ``run_id`` / ``original_pipeline_id`` / ``source_format``. Serving it
        reproduces the exact JSONPath failure task 12 observed, so the row
        having an ``output_data`` and no ``sfn_result`` must NOT skip.
        """
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", None)
        row = state_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"})["Item"]

        assert "run_id" not in row["output_data"], "output_data would satisfy the downstream JSONPath — premise gone"
        assert resolve_step_resume({"run_id": run_id, RESUME_MARKER_KEY: "CreateCode"}, "file_validation") is None

    def test_no_run_identity_means_the_step_runs(self, state_table):
        assert resolve_step_resume({RESUME_MARKER_KEY: "CreateCode"}, "file_validation") is None

    @pytest.mark.parametrize("token_field", retry_utils.TASK_TOKEN_FIELDS)
    def test_a_token_holding_step_is_never_short_circuited(self, state_table, token_field):
        """Decision D3: a served result would never send the token.

        ``step_handler`` is the OUTERMOST decorator on the container steps, so
        a short-circuit there returns before ``@step_task_token`` sends the
        token and the waitForTaskToken state stalls for its full
        TimeoutSeconds — worse than the wasteful re-execution it replaces.
        """
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "create_code", {"statusCode": 200, "code_version_id": "v1"})

        event = {"run_id": run_id, RESUME_MARKER_KEY: "TestCode", token_field: "tok-1"}
        assert resolve_step_resume(event, "create_code") is None


class TestStepHandlerIsTheProductionCaller:
    """The seam: all ten @step_handler Lambdas get the behaviour from one place."""

    def test_a_served_step_does_not_execute_the_handler(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "file_validation", FILE_VALIDATION_SFN_RESULT)
        calls: list[dict] = []

        @step_handler("file_validation")
        def handler(event, context):
            calls.append(event)
            return {"statusCode": 500, "re_executed": True}

        result = handler({"run_id": run_id, RESUME_MARKER_KEY: "CreateCode"}, None)

        assert calls == [], "2.18 UNMET — the step was re-executed despite being before the resume point"
        assert result["run_id"] == run_id
        assert result["source_format"] == "csv"
        assert "re_executed" not in result, "the re-executed output reached the resultPath — the cache was discarded"

    def test_a_successful_step_caches_what_it_returned(self, state_table):
        run_id = FILE_VALIDATION_SFN_RESULT["run_id"]
        _write_completed_step(state_table, run_id, "schema_extract", None)

        @step_handler("schema_extract")
        def handler(event, context):
            return {"statusCode": 200, "run_id": run_id, "schema_hash": "abc123"}

        returned = handler({"run_id": run_id}, None)

        assert returned == {"statusCode": 200, "run_id": run_id, "schema_hash": "abc123"}
        assert get_cached_sfn_result(run_id, "schema_extract") == returned

    def test_a_fresh_run_is_unchanged(self, state_table):
        """3.18: no marker, so the handler runs and its value passes through."""
        run_id = "run-fresh-0000"
        _write_completed_step(state_table, run_id, "file_validation", FILE_VALIDATION_SFN_RESULT)
        calls: list[dict] = []

        @step_handler("file_validation")
        def handler(event, context):
            calls.append(event)
            return dict(FILE_VALIDATION_SFN_RESULT, run_id=run_id)

        result = handler({"source_bucket": "b", "source_key": "k.csv"}, None)

        assert len(calls) == 1
        assert result == dict(FILE_VALIDATION_SFN_RESULT, run_id=run_id)

    def test_a_cache_failure_does_not_fail_the_step(self, state_table, monkeypatch):
        """The write is best-effort: a cache miss costs a re-run, a raise costs the run."""
        monkeypatch.setenv("STATE_TABLE", "table-that-does-not-exist")

        @step_handler("schema_compare")
        def handler(event, context):
            return {"statusCode": 200, "run_id": "run-x", "action": "no_change"}

        assert handler({"run_id": "run-x"}, None) == {"statusCode": 200, "run_id": "run-x", "action": "no_change"}

    def test_a_raising_step_still_raises_a_step_failure(self, state_table):
        """P28 R1.1 is untouched: the resume path never converts a failure."""
        from step_errors import StepFailure

        @step_handler("schema_compare")
        def handler(event, context):
            raise ValueError("boom")

        with pytest.raises(StepFailure):
            handler({"run_id": "run-x"}, None)
