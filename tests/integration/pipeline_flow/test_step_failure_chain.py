# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration tests: step failure chain (P28 R1) + p16 retry-from-step compatibility.

Chain under test (simulated at the Lambda level per design.md — moto cannot
execute real state machines with Lambda integrations):

    Step_Lambda raises StepFailure
      → SFN Catch (States.ALL, resultPath $.error)      [simulated exactly]
      → RecordFailure Lambda (real handler, moto DynamoDB)
      → Failure_Record lands in the state table
      → no downstream Step_Lambda ever ran (no downstream state rows)
      → p16 retry-from-step still resumes from the failed step

The raising Lambdas are the REAL handlers (file_validation, schema_mapping)
invoked against moto-mocked DynamoDB/S3 — no stubbed AI or AWS clients. The
SFN Catch error object is constructed exactly as the Lambda runtime + Step
Functions produce it: ``{"Error": <errorType>, "Cause": <runtime wrapper
JSON with errorMessage = str(exc)>}``.

Validates: Requirements 1.6
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_SHARED_DIR = _PROJECT_ROOT / "shared"
_DASHBOARD_API_DIR = _LAMBDA_ROOT / "agentic_data_ingestion_dashboard_api"

RUN_ID = "run-2026-01-01-00-00-00-itest001"
PIPELINE_ID = "customer-pipeline"
EXECUTION_NAME = "exec-itest-failure-chain"

# ---------------------------------------------------------------------------
# Module loaders — always called INSIDE a test (after mock_lambda_env has set
# the canonical table-name env vars and the moto context from the shared
# fixtures is active) so import-time env reads resolve correctly.
# ---------------------------------------------------------------------------


def _load_lambda_module(lambda_dir_name: str, alias: str):
    """Load a Lambda's lambda_function.py under a unique module alias."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))

    sys.modules.pop(alias, None)
    spec = importlib.util.spec_from_file_location(
        alias,
        str(_LAMBDA_ROOT / lambda_dir_name / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _load_pipelines_runs():
    """Fresh-import the dashboard API run handlers (retry_run lives here).

    Purges any cached ``handlers`` package modules first so the module-level
    table handles in ``handlers.common`` are created from THIS test's env
    (canonical table names) inside THIS test's moto context.
    """
    for mod_name in list(sys.modules):
        if (
            mod_name == "handlers"
            or mod_name.startswith("handlers.")
            or mod_name in ("router", "lambda_function")
            or "agentic_data_ingestion_dashboard_api" in mod_name
        ):
            sys.modules.pop(mod_name, None)

    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if str(_DASHBOARD_API_DIR) not in sys.path:
        sys.path.append(str(_DASHBOARD_API_DIR))

    import handlers.pipelines_runs as pipelines_runs

    return pipelines_runs


# ---------------------------------------------------------------------------
# SFN contract helpers
# ---------------------------------------------------------------------------


def _sfn_catch_error(exc: Exception) -> dict:
    """Build the Catch error object exactly as the Lambda runtime + SFN would.

    The Lambda runtime serializes an unhandled exception as
    ``{"errorMessage": str(exc), "errorType": type(exc).__name__, ...}``;
    Step Functions surfaces that JSON as the ``Cause`` and the errorType as
    the ``Error`` of the Catch payload (delivered at ``$.error`` per the
    orchestration stack's ``resultPath: '$.error'``).
    """
    return {
        "Error": type(exc).__name__,
        "Cause": json.dumps(
            {
                "errorMessage": str(exc),
                "errorType": type(exc).__name__,
                "requestId": "00000000-0000-0000-0000-000000000000",
                "stackTrace": ['  File "lambda_function.py", line 1, in lambda_handler\n'],
            }
        ),
    }


def _seed_completed_step(state_table, run_id: str, step_name: str, step_sequence: int) -> None:
    """Write a success state row exactly as a completed Step_Lambda would."""
    item = {
        "run_id": run_id,
        "step_name": step_name,
        "step_sequence": step_sequence,
        "status": "success",
        "timestamp": f"2026-01-01T00:0{step_sequence}:00+00:00",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "output_data": {f"{step_name}_data": True, "run_id": run_id},
    }
    if step_name == "schema_extract":
        # get_source_schema() reads extracted_schema; the mapping step also
        # resolves original_pipeline_id from this row.
        item["extracted_schema"] = json.dumps({"properties": {"customer_id": {"type": "string"}}})
    state_table.put_item(Item=item)


def _invoke_schema_mapping_and_capture_failure(mock_dynamodb_tables):
    """Run the real schema_mapping Lambda into its failure path.

    Seeds a run whose file_validation + schema_extract steps completed but
    whose pipeline record is missing (deleted mid-run) — the handler raises
    StepFailure("schema_mapping", ...). Returns the raised exception.
    """
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    _seed_completed_step(state_table, RUN_ID, "file_validation", 1)
    _seed_completed_step(state_table, RUN_ID, "schema_extract", 2)

    sm_module = _load_lambda_module("agentic_data_ingestion_schema_mapping", "itest_schema_mapping_lambda")

    with pytest.raises(sm_module.StepFailure) as exc_info:
        sm_module.lambda_handler(
            {
                "run_id": RUN_ID,
                "source_schema_hash": "hash-itest",
                "execution_arn": EXECUTION_NAME,
            },
            MagicMock(),
        )
    return exc_info.value


def _execution_state_at_schema_mapping_failure(error_object: dict) -> dict:
    """The full execution state as it exists when SchemaMapping's Catch fires.

    Mirrors the orchestration stack: trigger keys at the top level, completed
    steps' results at their resultPath keys, the Catch payload at $.error.
    """
    return {
        "source_bucket": "agentic-data-ingestion-source",
        "source_key": f"{PIPELINE_ID}/data.csv",
        "pipeline_id": PIPELINE_ID,
        "trigger": "s3_upload",
        "file_validation_result": {
            "statusCode": 200,
            "run_id": RUN_ID,
            "original_pipeline_id": PIPELINE_ID,
            "source_format": "csv",
        },
        "schema_extract_result": {"statusCode": 200, "run_id": RUN_ID},
        "schema_compare_result": {"statusCode": 200, "schema_change_action": "proceed"},
        "error": error_object,
    }


def _run_failure_chain(mock_dynamodb_tables) -> dict:
    """Execute the full simulated failure chain and return the Failure_Record.

    real schema_mapping raise → SFN Catch object → real RecordFailure handler
    → Failure_Record read back from the moto state table.
    """
    exc = _invoke_schema_mapping_and_capture_failure(mock_dynamodb_tables)
    error_object = _sfn_catch_error(exc)

    rf_module = _load_lambda_module("agentic_data_ingestion_record_failure", "itest_record_failure_lambda")
    result = rf_module.lambda_handler(
        {
            "state": _execution_state_at_schema_mapping_failure(error_object),
            "execution_name": EXECUTION_NAME,
        },
        MagicMock(),
    )
    assert result["recorded"] is True, "RecordFailure must persist the Failure_Record"

    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    response = state_table.get_item(Key={"run_id": RUN_ID, "step_name": "pipeline_failure"})
    assert "Item" in response, "Failure_Record (pipeline_failure row) must exist in the state table"
    return response["Item"]


def _create_sfn_state_machine() -> str:
    """Create a moto SFN state machine and return its ARN."""
    region = "us-east-1"
    iam = boto3.client("iam", region_name=region)
    role_arn = iam.create_role(
        RoleName="itest-sfn-role",
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}
                ],
            }
        ),
    )["Role"]["Arn"]
    sfn = boto3.client("stepfunctions", region_name=region)
    return sfn.create_state_machine(
        name="itest-pipeline-state-machine",
        definition=json.dumps({"StartAt": "Pass", "States": {"Pass": {"Type": "Pass", "End": True}}}),
        roleArn=role_arn,
    )["stateMachineArn"]


# ---------------------------------------------------------------------------
# 1. Simulated step raise → failed execution + Failure_Record + no downstream
# ---------------------------------------------------------------------------


class TestStepRaiseWritesFailureRecord:
    """A simulated step error produces a Failure_Record and no downstream state.

    Validates: Requirements 1.6
    """

    def test_step_lambda_raises_step_failure_with_stable_contract(
        self, mock_dynamodb_tables, mock_s3_buckets, mock_lambda_env
    ):
        """The real file_validation Lambda raises StepFailure (never returns 500).

        The raise is what makes the SFN task FAIL — an error payload return
        would be treated as task success and downstream states would run.
        str(exc) must be the stable JSON contract RecordFailure parses.
        """
        fv_module = _load_lambda_module("agentic_data_ingestion_file_validation", "itest_file_validation_lambda")

        with pytest.raises(fv_module.StepFailure) as exc_info:
            fv_module.lambda_handler(
                {
                    "source_bucket": "agentic-data-ingestion-source",
                    "source_key": f"{PIPELINE_ID}/does-not-exist.csv",
                    "pipeline_id": PIPELINE_ID,
                    "execution_arn": EXECUTION_NAME,
                },
                MagicMock(),
            )

        exc = exc_info.value
        assert exc.step_name == "file_validation"
        assert exc.error_type == "permanent"

        # Stable serialization contract (shared/step_errors.py) — this exact
        # JSON travels in the SFN Cause and is what RecordFailure parses.
        payload = json.loads(str(exc))
        assert set(payload.keys()) == {"step_name", "error_type", "message", "details"}
        assert payload["step_name"] == "file_validation"
        assert payload["error_type"] == "permanent"
        assert "File processing failed" in payload["message"]

    def test_failure_chain_writes_failure_record(self, mock_dynamodb_tables, mock_lambda_env):
        """schema_mapping raise → Catch → RecordFailure → Failure_Record in the state table.

        The Failure_Record carries the ORIGINATING step's name, error type,
        human-readable message, and run/pipeline identifiers (R1.4).
        """
        record = _run_failure_chain(mock_dynamodb_tables)

        # Key schema + legacy compatibility fields
        assert record["run_id"] == RUN_ID
        assert record["step_name"] == "pipeline_failure"
        assert record["status"] == "error"
        assert record["step_sequence"] == 99
        assert record["pipeline_id"] == f"{RUN_ID}.99"

        # Originating-step attribution from the StepFailure JSON (authoritative)
        assert record["failed_step"] == "schema_mapping"
        assert record["failed_step_source"] == "step_failure"
        assert record["error_type"] == "permanent"
        assert record["error_name"] == "StepFailure"
        assert "Pipeline metadata not found" in record["error_message"]

        # Run/pipeline identifiers recovered from the execution state
        assert record["original_pipeline_id"] == PIPELINE_ID
        assert record["execution_name"] == EXECUTION_NAME

    def test_no_downstream_state_executed(self, mock_dynamodb_tables, mock_lambda_env):
        """After the failure the state table has NO rows for downstream steps.

        The Catch chain routes to RecordFailure → ReleaseLockFailure →
        PipelineFailed (Fail): create_code, test_code, infrastructure and run
        steps are never invoked, so they never write state rows.
        """
        from boto3.dynamodb.conditions import Key

        _run_failure_chain(mock_dynamodb_tables)

        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        rows = state_table.query(KeyConditionExpression=Key("run_id").eq(RUN_ID))["Items"]
        rows_by_step = {row["step_name"]: row for row in rows}

        # The failed step itself never wrote a success row
        assert rows_by_step.get("schema_mapping", {}).get("status") != "success"

        # No state row exists for ANY step after the failed one
        downstream_steps = {
            "create_code",
            "test_code",
            "create_infrastructure",
            "create_glue_pipeline",
            "create_quality_checks",
            "run_pipeline",
        }
        assert not downstream_steps & set(rows_by_step), (
            f"Downstream steps must never run after a failure — found rows for: {downstream_steps & set(rows_by_step)}"
        )

        # What DOES exist: the completed steps and the Failure_Record
        assert rows_by_step["file_validation"]["status"] == "success"
        assert rows_by_step["schema_extract"]["status"] == "success"
        assert rows_by_step["pipeline_failure"]["status"] == "error"


# ---------------------------------------------------------------------------
# 2. Retry-from-step (p16) still resumes from the failed step
# ---------------------------------------------------------------------------


class TestRetryFromStepAfterFailure:
    """p16 retry-from-step works against the new Failure_Record shape.

    Validates: Requirements 1.6
    """

    def test_explicit_retry_from_failed_step_resumes_from_that_step(
        self, mock_dynamodb_tables, mock_lambda_env, monkeypatch
    ):
        """retry_run with retry_from_step resumes execution FROM the failed step.

        Seeds the failed run via the real failure chain, then drives the p16
        machinery end to end: failure detected (status='error' row), pipeline
        resolved via original_pipeline_id, new SFN execution started with the
        retry input, prior completed results cached, and the skip predicate
        skipping exactly the steps BEFORE the retry point.
        """
        record = _run_failure_chain(mock_dynamodb_tables)
        assert record["failed_step"] == "schema_mapping"

        # The failed run's own executions row, as pipeline_trigger writes it at
        # start and the execution updater leaves it on failure. retry_run
        # annotates THIS row and never creates one (p38 R3.1), so without it
        # the retry has nothing to link to.
        executions_table = mock_dynamodb_tables["agentic_data_ingestion_executions"]
        executions_table.put_item(
            Item={
                "pipeline_id": PIPELINE_ID,
                "run_id": RUN_ID,
                "status": "FAILED",
                "trigger": "s3_upload",
                "execution_name": EXECUTION_NAME,
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:04:00+00:00",
            }
        )

        sm_arn = _create_sfn_state_machine()
        monkeypatch.setenv("STATE_MACHINE_ARN", sm_arn)

        pipelines_runs = _load_pipelines_runs()

        # The Failure_Record's failed_step ("schema_mapping") maps to the
        # STEP_ORDER name "SchemaMapping" — the dashboard resumes from there.
        result = pipelines_runs.retry_run(RUN_ID, body=json.dumps({"retry_from_step": "SchemaMapping"}))
        body = json.loads(result["body"])

        assert result["statusCode"] == 200, f"retry_run failed: {body}"
        assert body["success"] is True
        assert body["retry_from_step"] == "SchemaMapping"
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["retry_source_run_id"] == RUN_ID

        # A new SFN execution was actually started (moto)
        sfn = boto3.client("stepfunctions", region_name="us-east-1")
        executions = sfn.list_executions(stateMachineArn=sm_arn)["executions"]
        assert len(executions) == 1
        sfn_input = json.loads(sfn.describe_execution(executionArn=executions[0]["executionArn"])["input"])

        assert sfn_input["run_id"] == RUN_ID
        assert sfn_input["pipeline_id"] == PIPELINE_ID
        assert sfn_input["retry_from_step"] == "SchemaMapping"
        assert sfn_input["trigger"] == "retry"

        # Completed step outputs travel at their resultPath keys so skipped
        # steps' data is available downstream
        assert sfn_input["file_validation_result"]["file_validation_data"] is True
        assert sfn_input["schema_extract_result"]["schema_extract_data"] is True

        # The resumed execution starts FROM the failed step: everything before
        # SchemaMapping is skipped, SchemaMapping and everything after runs.
        from retry_utils import STEP_ORDER, should_skip_step

        retry_idx = STEP_ORDER.index("SchemaMapping")
        for step in STEP_ORDER[:retry_idx]:
            assert should_skip_step(sfn_input, step, STEP_ORDER) is True, f"{step} should be skipped"
        for step in STEP_ORDER[retry_idx:]:
            assert should_skip_step(sfn_input, step, STEP_ORDER) is False, f"{step} should run"

        # The source run's own row now carries the retry, and no second row was
        # created: the retry runs under the same run id (p38 R3.1), so a new
        # row would be the phantom run the history readers would have to skip.
        # This assertion used to expect a separate `trigger="retry"` row, the
        # pre-p38 shape.
        from boto3.dynamodb.conditions import Key

        exec_rows = executions_table.query(KeyConditionExpression=Key("pipeline_id").eq(PIPELINE_ID))["Items"]
        assert [r["run_id"] for r in exec_rows] == [RUN_ID]
        row = exec_rows[0]
        assert row["trigger"] == "retry"
        assert row["retry_source_run_id"] == RUN_ID
        assert row["status"] == "RUNNING"
        assert "completed_at" not in row, "the live fields are reset for the new attempt"
        # The displaced attempt is kept on the row (p38 §4.2).
        assert [(a["status"], a["failed_step"]) for a in row["attempts"]] == [("FAILED", "SchemaMapping")]

    def test_auto_detected_retry_resumes_from_failure_record_attribution(
        self, mock_dynamodb_tables, mock_lambda_env, monkeypatch
    ):
        """retry_run with NO explicit step resumes from the Failure_Record's failed_step.

        P28 task 8.1: the rerun contract only accepts real pipeline steps, so
        the auto-detect path resolves the pipeline_failure sentinel row to its
        ``failed_step`` attribution ("schema_mapping" → SFN id
        "SchemaMapping") — precise resume instead of the old
        'pipeline_failure'-passthrough full rerun.
        """
        _run_failure_chain(mock_dynamodb_tables)

        sm_arn = _create_sfn_state_machine()
        monkeypatch.setenv("STATE_MACHINE_ARN", sm_arn)

        pipelines_runs = _load_pipelines_runs()

        result = pipelines_runs.retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200, f"retry_run failed: {body}"
        assert body["success"] is True
        # Auto-detect resolves the pipeline_failure record's failed_step
        # attribution ("schema_mapping") to the SFN state id
        assert body["retry_from_step"] == "SchemaMapping"

        sfn = boto3.client("stepfunctions", region_name="us-east-1")
        executions = sfn.list_executions(stateMachineArn=sm_arn)["executions"]
        assert len(executions) == 1
        sfn_input = json.loads(sfn.describe_execution(executionArn=executions[0]["executionArn"])["input"])
        assert sfn_input["retry_from_step"] == "SchemaMapping"

        # Precise resume: steps before SchemaMapping are skipped, SchemaMapping
        # and everything after runs
        from retry_utils import STEP_ORDER, should_skip_step

        retry_idx = STEP_ORDER.index("SchemaMapping")
        for step in STEP_ORDER[:retry_idx]:
            assert should_skip_step(sfn_input, step, STEP_ORDER) is True, f"{step} should be skipped"
        for step in STEP_ORDER[retry_idx:]:
            assert should_skip_step(sfn_input, step, STEP_ORDER) is False, f"{step} should run"

    def test_a_resumed_run_does_not_re_execute_the_completed_steps(
        self, mock_dynamodb_tables, mock_lambda_env, monkeypatch
    ):
        """p32 2.18: the skip decision now has a production actor.

        The two tests above assert that ``should_skip_step`` DECIDES correctly
        for the retry input the real dashboard handler produced. Until p32
        task 17 nothing acted on that decision: task 12 confirmed live that a
        retry re-entered ``FileValidation``, the step RAN, and its output
        overwrote the pre-seeded ``file_validation_result`` — so the cached
        results were not merely ignored, they were destroyed, and destroying
        them is what failed the run.

        This closes the loop in the same place the retry input is built:
        drive the completed steps through the REAL ``step_errors.step_handler``
        with that same input, and assert they neither run nor lose their
        cached results.

        **Validates: Requirements 2.18**
        """
        _run_failure_chain(mock_dynamodb_tables)
        sm_arn = _create_sfn_state_machine()
        monkeypatch.setenv("STATE_MACHINE_ARN", sm_arn)

        pipelines_runs = _load_pipelines_runs()
        retried = pipelines_runs.retry_run(RUN_ID, body=json.dumps({"retry_from_step": "SchemaMapping"}))
        assert retried["statusCode"] == 200, retried["body"]

        sfn = boto3.client("stepfunctions", region_name="us-east-1")
        executions = sfn.list_executions(stateMachineArn=sm_arn)["executions"]
        sfn_input = json.loads(sfn.describe_execution(executionArn=executions[0]["executionArn"])["input"])

        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        monkeypatch.setenv("STATE_TABLE", state_table.table_name)

        import retry_utils
        import step_errors

        # The cache the resume mechanism serves from: each completed step's
        # Step Functions RETURN value, which is NOT the five-key output_data
        # artifact on the same row (retry_utils module docstring, constraint 1).
        completed = ("file_validation", "schema_extract")
        original = {
            step: {"statusCode": 200, "run_id": RUN_ID, "original_pipeline_id": PIPELINE_ID, "produced_by": step}
            for step in completed
        }
        for step, result in original.items():
            assert retry_utils.cache_step_result(RUN_ID, step, result) is True

        executed: list[str] = []
        landed: dict[str, dict] = {}
        for step in completed:

            @step_errors.step_handler(step)
            def handler(event, context, _step=step):
                executed.append(_step)
                return {"statusCode": 500, "run_id": RUN_ID, "produced_by": f"reexecuted-{_step}"}

            landed[step] = handler(dict(sfn_input), None)

        assert executed == [], (
            f"2.18 UNMET — the resumed run re-executed {executed}, which completed before "
            f"retry_from_step={sfn_input['retry_from_step']!r}."
        )
        for step in completed:
            assert landed[step]["produced_by"] == step, (
                f"2.18 UNMET — {step}'s cached result was discarded: its resultPath carries "
                f"{landed[step]['produced_by']!r}. This is the overwrite task 12 observed live."
            )
            assert landed[step][retry_utils.RESUMED_FROM_CACHE_KEY] is True

    def test_failure_record_carries_failed_step_for_precise_resume(self, mock_dynamodb_tables, mock_lambda_env):
        """The new failed_step attribute names the exact step for precise resume.

        Consumers (dashboard error surface / retry UI) read failed_step from
        the Failure_Record and map it to the STEP_ORDER name to resume from
        the failed step instead of a full rerun.
        """
        record = _run_failure_chain(mock_dynamodb_tables)

        assert record["failed_step"] == "schema_mapping"
        assert record["failed_step_source"] == "step_failure"

        # The attribution is authoritative (from the StepFailure JSON), so a
        # consumer can resolve it to the SFN state name deterministically.
        snake_to_sfn = {"schema_mapping": "SchemaMapping"}
        from retry_utils import STEP_ORDER

        assert snake_to_sfn[record["failed_step"]] in STEP_ORDER
