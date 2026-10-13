# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dashboard read paths tolerate BOTH step-name generations (P28 R8.2, task 14.2).

Task 14.2 renamed the state-row step names to canonical snake_case
("create_code", "run_pipeline", …) while rows written before the rename
persist with the legacy PascalCase names ("CodeGeneration", "RunPipeline", …)
and the pre-R8.3 phantom approval rows ("code_generation", "infrastructure",
"first_run"). Every dashboard read path must recognize both generations:

- get_pipeline_executions: actual_cost batch-get covers both run_pipeline keys
- get_run_details: step-branch extraction, overall-status, synthetic filtering
- get_run_artifacts: code_content resolution from any create_code variant
- pipelines_logs: log-group + AgentCore agent resolution from any variant
- get_pipeline_details: code_version_id / test_status enrichment

Validates: P28 Requirements 8.2, 8.5
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path + environment setup (mirrors sibling dashboard_api tests)
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

RUN_ID = "run-14-2-tolerance"
PIPELINE_ID = "pipe-14-2"
STATE_TABLE_NAME = "agentic_data_ingestion_state"


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _reload_handlers(monkeypatch):
    """Fresh handler modules bound to real boto3 (test-ordering guard).

    The reload re-reads STATE_TABLE, and the assertions below key on
    STATE_TABLE_NAME. The module-scope setdefault above is a no-op once an
    earlier file has set STATE_TABLE to its own name, so pin it for the reload.
    This used to pass only because a leaked env_utils MagicMock ignored the env
    var and returned the default (LGR-c58f276-4).
    """
    import contextlib
    import importlib

    monkeypatch.setenv("STATE_TABLE", STATE_TABLE_NAME)
    for _name in ("handlers.common", "handlers.pipelines_runs", "handlers.pipelines_crud", "handlers.pipelines"):
        _mod = sys.modules.get(_name)
        if _mod is not None and "handlers" in sys.modules:
            with contextlib.suppress(ImportError):
                importlib.reload(_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runs_env(monkeypatch):
    """handlers.pipelines_runs with every table handle mocked out."""
    _reload_handlers(monkeypatch)
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mocks = {
        "state_table": MagicMock(),
        "approvals_table": MagicMock(),
        "executions_table": MagicMock(),
        "pipelines_table": MagicMock(),
        "dynamodb": MagicMock(),
    }
    for mod in (_common, _runs):
        for name, mock in mocks.items():
            monkeypatch.setattr(mod, name, mock)

    mocks["state_table"].query.return_value = {"Items": []}
    mocks["approvals_table"].query.return_value = {"Items": []}
    mocks["executions_table"].query.return_value = {"Items": []}
    mocks["executions_table"].get_item.return_value = {}
    mocks["pipelines_table"].get_item.return_value = {}
    mocks["dynamodb"].batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    return _runs, mocks


# ===========================================================================
# get_pipeline_executions — actual_cost batch-get covers both key variants
# ===========================================================================


class TestExecutionCostDualKeys:
    def _execution(self, run_id: str) -> dict:
        """An executions row shaped the way ``execution_updater`` writes one.

        ``current_step`` and ``steps_status`` are not decoration. p38 task 6.1
        filters ``get_pipeline_executions`` on ``is_phantom_execution_row``
        (``handlers/common.py:59``), which excludes rows carrying neither —
        ``execution_updater`` sets both on its first write for a run
        (``execution_updater/lambda_function.py:238-253``, issued at
        ``:340-345``), so a ``SUCCEEDED`` row without them is a shape no writer
        produces. Omitting them made this fixture unreachable from the reader and
        the dual-key assertions below vacuous — the fixture-side drift
        ``LGR-28b4d75-5`` (**template / ratified**) names. The subject of these
        tests is the ``run_pipeline`` / ``RunPipeline`` key pair (P28 R8.2), which
        is unchanged.
        """
        return {
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "status": "SUCCEEDED",
            "started_at": "2026-01-15T12:00:00+00:00",
            "current_step": "run_pipeline",
            "steps_status": {"run_pipeline": "SUCCEEDED"},
        }

    def test_batch_get_requests_both_run_pipeline_key_variants(self, runs_env):
        """Every run is batch-fetched under BOTH 'run_pipeline' and 'RunPipeline'."""
        _runs, mocks = runs_env
        mocks["executions_table"].query.return_value = {"Items": [self._execution("run-001")]}

        status, _body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        batch_calls = mocks["dynamodb"].batch_get_item.call_args_list
        assert batch_calls, "cost enrichment must batch-get run_pipeline rows"
        requested = [
            (k["run_id"], k["step_name"])
            for c in batch_calls
            for k in c.kwargs["RequestItems"][STATE_TABLE_NAME]["Keys"]
        ]
        assert ("run-001", "run_pipeline") in requested
        assert ("run-001", "RunPipeline") in requested

    @pytest.mark.parametrize("row_step_name", ["run_pipeline", "RunPipeline"])
    def test_cost_resolves_from_either_row_generation(self, runs_env, row_step_name):
        """actual_cost surfaces whether the run's row is canonical or legacy."""
        from decimal import Decimal

        _runs, mocks = runs_env
        mocks["executions_table"].query.return_value = {"Items": [self._execution("run-001")]}
        mocks["dynamodb"].batch_get_item.return_value = {
            "Responses": {
                STATE_TABLE_NAME: [
                    {
                        "run_id": "run-001",
                        "step_name": row_step_name,
                        "output_data": {"actual_cost": Decimal("2.5")},
                    }
                ]
            },
            "UnprocessedKeys": {},
        }

        _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))

        assert body["executions"][0]["actual_cost"] == 2.5


# ===========================================================================
# get_run_details — step branches + overall status in canonical space
# ===========================================================================


def _state_row(step_name: str, sequence: int, status: str = "success", **extra) -> dict:
    row = {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "status": status,
        "timestamp": f"2026-01-15T12:0{sequence}:00+00:00",
        "step_sequence": sequence,
    }
    row.update(extra)
    return row


class TestRunDetailsStepBranches:
    @pytest.mark.parametrize("code_step_name", ["create_code", "CodeGeneration"])
    def test_code_version_extracted_from_either_generation(self, runs_env, code_step_name):
        """The create_code branch fires for canonical AND legacy row names."""
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row(
                    code_step_name,
                    5,
                    approval_data=json.dumps({"code_version_id": "cv-42", "generated_code_s3_uri": "s3://b/c.py"}),
                )
            ]
        }

        status, body = _parse(_runs.get_run_details(RUN_ID))

        assert status == 200
        steps = {s["step_name"]: s for s in body["steps"]}
        assert steps[code_step_name]["code_version_id"] == "cv-42"

    @pytest.mark.parametrize("test_step_name", ["test_code", "CodeTesting"])
    def test_test_fields_extracted_from_either_generation(self, runs_env, test_step_name):
        """The ``approval_data`` arm fires for canonical AND legacy row names.

        **This shape is not one any producer writes, and the assertion is kept
        anyway.** ``approval_data`` is written only by ``approval_handler``
        (``lambda_function.py:428``) onto the APPROVALS table; its state-table
        echo (``:457-471``) sets ``status`` / ``updated_at`` / ``approval_id``
        and nothing else. ``git log -S'"approval_data":'`` over
        ``agentic_data_ingestion_test_code/`` and
        ``agentic_data_ingestion_create_code/`` returns no commit, so no step
        Lambda has ever written it onto a state row either.

        What it pins is therefore the reader's *tolerance* — the highest-priority
        arm, retained for the same reason ``_synthetic_steps`` still filters
        ``code_generation`` / ``infrastructure`` / ``first_run``
        (``LGR-9e76249-9``, **template / ratified**): a live table can hold
        shapes no current producer writes. It is amended rather than deleted
        under ``LGR-60c4985-6`` (citing ``LGR-5906ec6-2``) — an assertion that
        encoded a contract stays legible, with the reason recorded at the
        assertion. The amendment is the docstring plus the
        ``test_output_s3_uri`` line: this row supplies no URI in
        ``approval_data``, and the reader must not invent one.

        The path that actually runs in production is
        ``test_live_test_code_row_shape_surfaces_all_four_fields`` below.
        """
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row(
                    test_step_name,
                    6,
                    approval_data=json.dumps({"test_execution_id": "te-7", "test_status": "passed"}),
                )
            ]
        }

        _, body = _parse(_runs.get_run_details(RUN_ID))

        steps = {s["step_name"]: s for s in body["steps"]}
        assert steps[test_step_name]["test_execution_id"] == "te-7"
        assert steps[test_step_name]["test_status"] == "passed"
        # Absent from this row's approval_data and absent from the row itself,
        # so it reports empty rather than fabricated.
        assert steps[test_step_name]["test_output_s3_uri"] == ""

    @pytest.mark.parametrize("test_step_name", ["test_code", "CodeTesting"])
    def test_live_test_code_row_shape_surfaces_all_four_fields(self, runs_env, test_step_name):
        """The shape ``test_code`` actually writes: attributes at TOP LEVEL.

        Transcribed from ``state_row_attributes``
        (``test_code/lambda_function.py:3064-3106``, written through
        ``build_state_row_update``) and from the live state row for run
        ``run-2026-08-18-19-45-13-a158b7e5`` (account 777788889999, us-west-2):
        24 attributes, ``test_execution_id`` / ``test_status`` /
        ``test_output_s3_uri`` all at top level, and **no** ``approval_data``.

        ``output_data`` here is faithful too: the five keys
        ``shared/artifact_schema.create_output_data`` fixes and validates
        (``LGR-60c4985-4``, **template / ratified**) plus the ``approval``
        payload the producer attaches at ``:3052``, after validation has run.
        None of the three fields can live among the five — which is why the
        reader's old ``output_data.get(...)`` fallback returned empty on every
        real row, and why moving them into ``output_data`` is not the fix.

        ``code_version_id`` is asserted from ``output_data["approval"]``
        (``:3012-3030``): it is the only place this row carries it.
        """
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row(
                    test_step_name,
                    6,
                    test_execution_id="test-exec-a158b7e5",
                    test_status="passed",
                    test_output_s3_uri="s3://reports-bucket/tests/test-exec-a158b7e5/report.json",
                    test_report_s3_uri="s3://reports-bucket/tests/test-exec-a158b7e5/report.md",
                    validation_status="validated",
                    agent_session_id="sess-a158b7e5",
                    approval_required=False,
                    output_data={
                        "artifact_type": "test_results",
                        "primary_id": "test-exec-a158b7e5",
                        "status": "success",
                        "summary": {
                            "tests_passed": 4,
                            "tests_failed": 0,
                            "tests_total": 4,
                            "execution_verified": True,
                        },
                        "s3_uris": {
                            "full_report_json": "s3://reports-bucket/tests/test-exec-a158b7e5/report.json",
                        },
                        "approval": {
                            "code_version_id": "cv-a158b7e5",
                            "test_execution_id": "test-exec-a158b7e5",
                            "test_status": "passed",
                            "generated_code_s3_uri": "s3://code-bucket/scripts/job.py",
                        },
                    },
                )
            ]
        }

        _, body = _parse(_runs.get_run_details(RUN_ID))

        step = {s["step_name"]: s for s in body["steps"]}[test_step_name]
        assert step["test_execution_id"] == "test-exec-a158b7e5"
        assert step["test_status"] == "passed"
        assert step["test_output_s3_uri"] == "s3://reports-bucket/tests/test-exec-a158b7e5/report.json"
        assert step["code_version_id"] == "cv-a158b7e5"

    def test_approval_data_wins_but_does_not_suppress_the_other_fields(self, runs_env):
        """Per-field resolution: one field from ``approval_data`` no longer blocks the rest.

        The reader's fallback used to be gated on a single
        ``if not step_info.get("test_execution_id")``, so a row carrying the id
        in ``approval_data`` skipped the lookup of the other three even when the
        row itself supplied them. Precedence is unchanged — ``approval_data``
        still wins for the field it carries.
        """
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row(
                    "test_code",
                    6,
                    approval_data=json.dumps({"test_execution_id": "te-from-approval-data"}),
                    test_execution_id="te-from-top-level",
                    test_status="failed",
                    test_output_s3_uri="s3://reports-bucket/tests/te/report.json",
                )
            ]
        }

        _, body = _parse(_runs.get_run_details(RUN_ID))

        step = {s["step_name"]: s for s in body["steps"]}["test_code"]
        assert step["test_execution_id"] == "te-from-approval-data"
        assert step["test_status"] == "failed"
        assert step["test_output_s3_uri"] == "s3://reports-bucket/tests/te/report.json"

    @pytest.mark.parametrize("run_step_name", ["run_pipeline", "RunPipeline"])
    def test_overall_status_succeeded_from_either_generation(self, runs_env, run_step_name):
        """A successful final step yields SUCCEEDED for both row generations."""
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {"Items": [_state_row(run_step_name, 10)]}

        _, body = _parse(_runs.get_run_details(RUN_ID))

        assert body["status"] == "SUCCEEDED"

    def test_phantom_approval_rows_filtered_for_old_runs(self, runs_env):
        """Pre-R8.3 phantom rows (approval_type-keyed) never surface as steps."""
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row("CodeGeneration", 5),
                _state_row("code_generation", 0, status="approved"),
                _state_row("infrastructure", 0, status="approved"),
                _state_row("first_run", 0, status="approved"),
            ]
        }

        _, body = _parse(_runs.get_run_details(RUN_ID))

        step_names = [s["step_name"] for s in body["steps"]]
        assert "CodeGeneration" in step_names
        assert "code_generation" not in step_names
        assert "infrastructure" not in step_names
        assert "first_run" not in step_names


# ===========================================================================
# get_run_artifacts — code_content from any create_code variant
# ===========================================================================


class TestRunArtifactsCodeLookup:
    @pytest.mark.parametrize("code_step_name", ["create_code", "CodeGeneration", "code_generation"])
    def test_code_content_resolved_from_any_variant(self, runs_env, code_step_name, monkeypatch):
        _runs, mocks = runs_env
        mocks["state_table"].query.return_value = {
            "Items": [
                _state_row(
                    code_step_name,
                    5,
                    output_data={"s3_uris": {"code_file": "s3://code-bucket/scripts/job.py"}},
                )
            ]
        }
        monkeypatch.setattr(_runs, "_read_s3_object", lambda uri: f"# code from {uri}")

        status, body = _parse(_runs.get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["code_content"] == "# code from s3://code-bucket/scripts/job.py"
        # artifacts stay keyed by the RAW step name (API shape unchanged)
        assert code_step_name in body["artifacts"]


# ===========================================================================
# pipelines_logs — log-group and agent resolution from any variant
# ===========================================================================


class TestStepLogGroupResolution:
    @pytest.mark.parametrize(
        "variant,expected_log_group",
        [
            ("create_code", "/aws/lambda/agentic_data_ingestion_create_code"),
            ("CodeGeneration", "/aws/lambda/agentic_data_ingestion_create_code"),
            ("test_code", "/aws/lambda/agentic_data_ingestion_test_code"),
            ("CodeTesting", "/aws/lambda/agentic_data_ingestion_test_code"),
            ("run_pipeline", "/aws/lambda/agentic_data_ingestion_run_pipeline"),
            ("RunPipeline", "/aws/lambda/agentic_data_ingestion_run_pipeline"),
            ("create_infrastructure", "/aws/lambda/agentic_data_ingestion_create_infrastructure"),
            ("CreateInfrastructure", "/aws/lambda/agentic_data_ingestion_create_infrastructure"),
            ("JobCompletion", "/aws/lambda/agentic_data_ingestion_job_completion"),
        ],
    )
    def test_lambda_log_group_resolves_for_variant(self, monkeypatch, variant, expected_log_group):
        from handlers import pipelines_logs as _logs

        mock_logs_client = MagicMock()
        mock_logs_client.start_query.return_value = {"queryId": "q-1"}
        mock_logs_client.get_query_results.return_value = {"status": "Complete", "results": []}
        mock_logs_client.describe_log_groups.return_value = {"logGroups": []}
        monkeypatch.setattr(_logs, "logs_client", mock_logs_client)
        monkeypatch.setattr(_logs.time, "sleep", lambda *_: None, raising=False)

        _logs.get_step_logs(variant, RUN_ID, limit=5)

        queried_groups = [c.kwargs["logGroupName"] for c in mock_logs_client.start_query.call_args_list]
        assert expected_log_group in queried_groups

    def test_unknown_step_returns_empty_without_queries(self, monkeypatch):
        from handlers import pipelines_logs as _logs

        mock_logs_client = MagicMock()
        monkeypatch.setattr(_logs, "logs_client", mock_logs_client)

        assert _logs.get_step_logs("pipeline_failure", RUN_ID) == []
        mock_logs_client.start_query.assert_not_called()

    @pytest.mark.parametrize(
        "variant,expected_agent",
        [
            ("create_code", "create_code"),
            ("CodeGeneration", "create_code"),
            ("CodeTesting", "test_code"),
            ("create_glue_pipeline", "create_pipeline"),
            ("CreateGluePipeline", "create_pipeline"),
            ("CreateInfrastructure", "create_infra"),
            ("create_infrastructure", "create_infra"),
        ],
    )
    def test_agentcore_agent_resolves_for_variant(self, monkeypatch, variant, expected_agent):
        from handlers import pipelines_logs as _logs

        mock_logs_client = MagicMock()
        mock_logs_client.describe_log_groups.return_value = {"logGroups": []}
        monkeypatch.setattr(_logs, "logs_client", mock_logs_client)

        _logs._get_agentcore_step_logs(variant, RUN_ID)

        prefix = mock_logs_client.describe_log_groups.call_args.kwargs["logGroupNamePrefix"]
        assert prefix.endswith(expected_agent)

    def test_non_agent_step_skips_agentcore_lookup(self, monkeypatch):
        from handlers import pipelines_logs as _logs

        mock_logs_client = MagicMock()
        monkeypatch.setattr(_logs, "logs_client", mock_logs_client)

        assert _logs._get_agentcore_step_logs("run_pipeline", RUN_ID) == []
        mock_logs_client.describe_log_groups.assert_not_called()


# ===========================================================================
# get_pipeline_details — code/test enrichment from any variant
# ===========================================================================


class TestPipelineDetailsEnrichment:
    @pytest.mark.parametrize(
        "code_step_name,test_step_name",
        [("create_code", "test_code"), ("CodeGeneration", "CodeTesting")],
    )
    def test_schema_history_runs_enriched_from_either_generation(self, monkeypatch, code_step_name, test_step_name):
        _reload_handlers(monkeypatch)
        from handlers import pipelines_crud as _crud

        mock_state = MagicMock()
        mock_pipelines = MagicMock()
        mock_mappings = MagicMock()
        monkeypatch.setattr(_crud, "state_table", mock_state)
        monkeypatch.setattr(_crud, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(_crud, "mappings_table", mock_mappings)

        mock_state.query.return_value = {
            "Items": [
                {
                    "run_id": RUN_ID,
                    "step_name": "schema_extract",
                    "schema_hash": "hash-1",
                    "json_s3_uri": "s3://b/f.json",
                    "status": "success",
                    "timestamp": "2026-01-15T12:02:00+00:00",
                },
                _state_row(code_step_name, 5, output_data={"code_version_id": "cv-99"}),
                _state_row(test_step_name, 6, output_data={"test_status": "passed"}),
            ]
        }
        mock_pipelines.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_ID, "pipeline_name": "P"}}

        status, body = _parse(_crud.get_pipeline_details(PIPELINE_ID))

        assert status == 200
        runs = body["schema_history"][0]["runs"]
        assert runs[0]["code_version_id"] == "cv-99"
        assert runs[0]["test_status"] == "passed"
