# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the body of POST /runs/{runId}/cancel, against its consumer.

Every response ``cancel_run`` can give is driven through the real handler and
validated, and the success shape is held against the frontend's
``CancelRunResponse`` in ``dashboard-react/src/hooks/use-cancel-run.ts``, parsed
at test time by the shared reader in ``helpers.py``. The schema alone would not
do: it is a transcription, and a transcription cannot catch the producer and
the consumer drifting apart (``LGR-28b4d75-5``).

The pairing that matters to the consumer: HTTP 200 always carries
``CANCELLED`` and HTTP 202 always carries ``CANCELLING``. ``cancelOutcomeToast``
branches on ``status``, so a 202 that said CANCELLED would tell an operator the
run was over while its Glue job was still running.

DynamoDB is moto; Step Functions and Glue are MagicMocks.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import jsonschema
import pytest

from .helpers import load_interfaces, required_fields
from .schemas import CANCEL_RUN_REFUSAL_SCHEMA, CANCEL_RUN_RESPONSE_SCHEMA, CANCEL_RUN_STATUS_ENUM

REPO_DIR = Path(__file__).resolve().parents[3]
for _path in (REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api", REPO_DIR / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
FRONTEND_HOOK_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-cancel-run.ts"
RESPONSE_TYPE = "CancelRunResponse"

PIPELINE_ID = "orders-1a2b3c4d"
RUN_ID = "run-2026-10-11-09-00-00-cafe0001"
JOB_NAME = f"agentic-data-ingestion-job-{PIPELINE_ID}"
JOB_RUN_ID = "jr_0123abcd"


@pytest.fixture
def tables(mock_lambda_env, mock_dynamodb_tables):
    return mock_dynamodb_tables


def _seed(tables, status: str, *, glue_running: bool = False) -> None:
    tables["agentic_data_ingestion_executions"].put_item(
        Item={
            "pipeline_id": PIPELINE_ID,
            "run_id": RUN_ID,
            "status": status,
            "started_at": "2026-10-11T09:00:00+00:00",
            "execution_id": "file-orders_csv-20261011T090000-0123456789ab",
        }
    )
    if glue_running:
        tables["agentic_data_ingestion_state"].put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "run_pipeline",
                "status": "RUNNING",
                "glue_job_name": JOB_NAME,
                "glue_job_run_id": JOB_RUN_ID,
            }
        )
        tables["agentic_data_ingestion_task_tokens"].put_item(
            Item={"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-1"}
        )


def _cancel(tables, run_id: str = RUN_ID, *, execution_status: str = "RUNNING") -> tuple[int, dict]:
    from handlers import pipelines_runs as runs

    sfn = MagicMock(name="stepfunctions")
    sfn.describe_execution.return_value = {"status": execution_status}
    glue = MagicMock(name="glue")
    glue.get_job_run.return_value = {"JobRun": {"JobRunState": "RUNNING"}}
    glue.batch_stop_job_run.return_value = {"SuccessfulSubmissions": [{"JobRunId": JOB_RUN_ID}], "Errors": []}
    clients = {"stepfunctions": sfn, "glue": glue}
    with (
        patch.object(runs, "executions_table", tables["agentic_data_ingestion_executions"]),
        patch.object(runs, "state_table", tables["agentic_data_ingestion_state"]),
        patch.object(runs, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1")),
        patch.object(runs, "write_audit_entry", MagicMock()),
        patch.object(runs.boto3, "client", side_effect=lambda service, *a, **k: clients[service]),
    ):
        response = runs.cancel_run(run_id, event={})
    return response["statusCode"], json.loads(response["body"])


# Every accepted outcome, with the status code it must carry.
_ACCEPTED = [
    pytest.param({"status": "RUNNING"}, 200, "CANCELLED", id="stopped-at-a-lambda-step"),
    pytest.param({"status": "RUNNING", "glue_running": True}, 202, "CANCELLING", id="glue-job-stopping"),
    pytest.param({"status": "CANCELLED"}, 200, "CANCELLED", id="already-cancelled"),
    pytest.param({"status": "CANCELLING"}, 202, "CANCELLING", id="already-cancelling"),
]


class TestAcceptedCancels:
    @pytest.mark.parametrize(("seed", "expected_code", "expected_status"), _ACCEPTED)
    def test_the_body_validates_and_the_code_matches_the_status(self, tables, seed, expected_code, expected_status):
        _seed(tables, **seed)

        code, body = _cancel(tables)

        jsonschema.validate(body, CANCEL_RUN_RESPONSE_SCHEMA)
        assert (code, body["status"]) == (expected_code, expected_status)
        assert body["run_id"] == RUN_ID


class TestRefusedCancels:
    def test_an_unknown_run_is_a_404_with_a_reason(self, tables):
        code, body = _cancel(tables, "run-does-not-exist")

        assert code == 404
        jsonschema.validate(body, CANCEL_RUN_REFUSAL_SCHEMA)

    @pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "SKIPPED"])
    def test_a_finished_run_is_a_409_naming_its_status(self, tables, status):
        _seed(tables, status)

        code, body = _cancel(tables)

        assert code == 409
        jsonschema.validate(body, CANCEL_RUN_REFUSAL_SCHEMA)
        assert body["status"] == status


@pytest.fixture(scope="module")
def interface():
    """The frontend ``CancelRunResponse``, parsed at test time."""
    return load_interfaces(FRONTEND_HOOK_FILE, RESPONSE_TYPE)[RESPONSE_TYPE]


class TestAgainstTheFrontendType:
    def test_the_type_requires_exactly_the_fields_the_schema_requires(self, interface):
        assert required_fields(interface) == set(CANCEL_RUN_RESPONSE_SCHEMA["required"])
        assert set(interface) == set(CANCEL_RUN_RESPONSE_SCHEMA["properties"])

    def test_the_type_names_exactly_the_statuses_the_handler_answers_with(self, interface, tables):
        _, ts_type = interface["status"]
        declared = set(re.findall(r"'([A-Z_]+)'", ts_type))
        assert declared == set(CANCEL_RUN_STATUS_ENUM)

        produced = set()
        for seed, _, _ in (p.values for p in _ACCEPTED):
            for table_name in (
                "agentic_data_ingestion_executions",
                "agentic_data_ingestion_state",
                "agentic_data_ingestion_task_tokens",
            ):
                table = tables[table_name]
                key_names = [k["AttributeName"] for k in table.key_schema]
                for item in table.scan()["Items"]:
                    table.delete_item(Key={k: item[k] for k in key_names})
            _seed(tables, **seed)
            produced.add(_cancel(tables)[1]["status"])
        assert produced == declared
