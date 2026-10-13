# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 task 8.4 — WorkerType persistence: create_glue_pipeline merges heal
rerun ``job_config_overrides`` over the generated Glue job config instead of
clobbering the self-healing agent's fix back (R9.5).

Delivery path under test (mirrors 8.3's triggered_by persistence):

1. the heal rerun's SFN input carries ``job_config_overrides`` (rerun
   contract, task 8.1);
2. file_validation persists it on the run's identity row keyed by the FRESH
   run_id it generates (origin tests live in
   tests/unit/lambdas/file_validation/);
3. create_glue_pipeline's handler reads it back from that row and threads it
   to ``create_glue_job`` (end-to-end shape test below);
4. ``create_glue_job`` merges the overrides OVER the generated config as the
   last step before ``create_job``/``update_job`` (merge tests below).

**Validates: Requirements 9.5**
"""

import importlib.util
import os
import sys
import unittest.mock as mock
from decimal import Decimal

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)
sys.path.insert(0, LAMBDA_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")

# Heavy/optional dependencies mocked at import time (same set as
# test_source_path_resolution.py so module loading is hermetic).
_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock"),
    ),
}


def _load(mod_name: str, alias: str):
    """Load a create_glue_pipeline module standalone with mocked heavy deps."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(alias, mod_path, submodule_search_locations=[LAMBDA_DIR])
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
    return mod


_jb = _load("job_builders", "jco_job_builders")


# ── Glue-native auto-retry stays disabled (P28, task 17.2 live finding) ──


def test_job_builders_disable_glue_native_retries():
    """MaxRetries must be 0 at both job-definition sites: a Glue auto-retry
    is invisible to the state machine (job_completion's token is consumed by
    the FIRST attempt's event, so the retry's completion can never match)
    and its run holds the MaxConcurrentRuns=1 slot, blocking the
    self-healing rerun's start_job_run (ConcurrentRunsExceededException —
    observed live in task 17.2 verification). Retries are owned by the
    self-healing agent / p16 retry-from-step."""
    import inspect

    import job_builders
    import job_update_handler

    for module in (job_builders, job_update_handler):
        source = inspect.getsource(module)
        assert '"MaxRetries": 0' in source, f"{module.__name__} must set MaxRetries: 0"
        assert '"MaxRetries": 1' not in source, f"{module.__name__} re-enables Glue-native retries"


def test_job_builders_allow_one_run_at_a_time():
    """`ExecutionProperty.MaxConcurrentRuns` must stay 1 where the job is defined.

    It is the only thing keeping a JDBC incremental watermark moving forward.
    The template's conditional write compares completion times, not watermark
    values, so if two runs of one pipeline could overlap, an older run that
    finished later would write a lower watermark and the next run would re-read
    rows (`docs/JDBC_SOURCE.md`, "Read → write → watermark ordering").

    Read from the parsed module rather than its text, so a comment mentioning
    the setting cannot satisfy the check.
    """
    import ast
    import inspect

    import job_builders

    tree = ast.parse(inspect.getsource(job_builders))
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "ExecutionProperty" and isinstance(value, ast.Dict):
                for inner_key, inner_value in zip(value.keys, value.values, strict=True):
                    if isinstance(inner_key, ast.Constant) and inner_key.value == "MaxConcurrentRuns":
                        values.append(ast.literal_eval(inner_value))
    assert values, "job_builders no longer sets ExecutionProperty.MaxConcurrentRuns"
    assert values == [1] * len(values), f"MaxConcurrentRuns must be 1 everywhere it is set; found {values}"


# ── The merge itself (pure function) ─────────────────────────────────────


def _generated_config() -> dict:
    """A representative generated job_config as create_glue_job assembles it."""
    return {
        "Name": "agentic-data-ingestion-job-pipe-1",
        "Role": "arn:aws:iam::123456789012:role/exec",
        "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/code.py", "PythonVersion": "3"},
        "DefaultArguments": {"--pipeline-id": "pipe-1", "--output_path": "s3://out/"},
        "MaxRetries": 1,
        "Timeout": 60,
        "WorkerType": "G.1X",
        "NumberOfWorkers": 4,
        "GlueVersion": "5.0",
        "Tags": {"Project": "AgenticDataIngestion", "WorkerType": "G.1X", "NumberOfWorkers": "4"},
    }


class TestMergeJobConfigOverrides:
    """_merge_job_config_overrides: override keys win, generated keys survive.

    **Validates: Requirements 9.5**
    """

    def test_override_keys_win_generated_keys_intact(self):
        config = _generated_config()
        applied = _jb._merge_job_config_overrides(config, {"WorkerType": "G.2X"})

        # Overridden key wins (the OOM heal's upgrade persists).
        assert config["WorkerType"] == "G.2X"
        assert applied == {"WorkerType": "G.2X"}
        # Non-overridden generated keys are untouched.
        assert config["NumberOfWorkers"] == 4
        assert config["Timeout"] == 60
        assert config["DefaultArguments"] == {"--pipeline-id": "pipe-1", "--output_path": "s3://out/"}

    def test_no_overrides_leaves_generated_config_unchanged(self):
        config = _generated_config()
        baseline = _generated_config()

        assert _jb._merge_job_config_overrides(config, None) == {}
        assert config == baseline
        assert _jb._merge_job_config_overrides(config, {}) == {}
        assert config == baseline

    def test_decimal_values_normalized_for_glue_api(self):
        """DynamoDB reads return Decimal — the Glue API needs int."""
        config = _generated_config()
        applied = _jb._merge_job_config_overrides(config, {"NumberOfWorkers": Decimal("10")})

        assert config["NumberOfWorkers"] == 10
        assert isinstance(config["NumberOfWorkers"], int)
        assert applied == {"NumberOfWorkers": 10}

    def test_dict_valued_override_merges_into_generated_dict(self):
        """DefaultArguments override entries win; generated entries survive
        (wholesale replacement would drop --pipeline-id and target args)."""
        config = _generated_config()
        _jb._merge_job_config_overrides(config, {"DefaultArguments": {"--conf": "spark.sql.shuffle.partitions=64"}})

        assert config["DefaultArguments"]["--conf"] == "spark.sql.shuffle.partitions=64"
        assert config["DefaultArguments"]["--pipeline-id"] == "pipe-1"
        assert config["DefaultArguments"]["--output_path"] == "s3://out/"

    def test_name_is_protected(self):
        config = _generated_config()
        applied = _jb._merge_job_config_overrides(config, {"Name": "evil-other-job", "WorkerType": "G.2X"})

        assert config["Name"] == "agentic-data-ingestion-job-pipe-1"
        assert applied == {"WorkerType": "G.2X"}

    def test_tags_synced_with_overridden_worker_config(self):
        config = _generated_config()
        _jb._merge_job_config_overrides(config, {"WorkerType": "G.4X", "NumberOfWorkers": Decimal("8")})

        assert config["Tags"]["WorkerType"] == "G.4X"
        assert config["Tags"]["NumberOfWorkers"] == "8"

    def test_merge_logged_with_overridden_keys_and_values(self):
        config = _generated_config()
        with mock.patch.object(_jb, "logger") as mock_logger:
            _jb._merge_job_config_overrides(config, {"WorkerType": "G.2X"})

        logged = " ".join(str(c.args[0]) for c in mock_logger.info.call_args_list)
        assert "job_config_overrides" in logged
        assert "WorkerType" in logged
        assert "G.2X" in logged
        # Generated→override values are both visible for the audit trail.
        assert "G.1X" in logged


# ── create_glue_job: overrides reach the Glue API call ───────────────────


class _EntityNotFound(Exception):
    pass


@pytest.fixture
def glue_env(monkeypatch):
    """Patch create_glue_job's collaborators so only the Glue call matters."""
    mock_glue = mock.MagicMock()
    mock_glue.exceptions.EntityNotFoundException = _EntityNotFound
    # Default: job exists → update path.
    mock_glue.get_job.return_value = {"Job": {"Name": "agentic-data-ingestion-job-pipe-1"}}
    monkeypatch.setattr(_jb, "glue_client", mock_glue)

    mock_ddb = mock.MagicMock()
    mock_ddb.Table.return_value.get_item.return_value = {"Item": {}}
    monkeypatch.setattr(_jb, "dynamodb", mock_ddb)

    monkeypatch.setattr(_jb, "create_pipeline_specific_role", lambda *a, **k: "arn:aws:iam::123456789012:role/exec")
    monkeypatch.setattr(_jb, "PARAMETER_BUILDER_AVAILABLE", False)
    monkeypatch.setattr(_jb, "METRICS_AVAILABLE", False)
    return mock_glue


TARGET_CONFIG = {"type": "S3_PARQUET", "output_path": "s3://out/"}


def _call_create_glue_job(job_config_overrides=None):
    return _jb.create_glue_job(
        "pipe-1",
        "S3_PARQUET",
        {"name": "tgt", "fields": []},
        "s3://b/code.py",
        s3_table_bucket=None,
        worker_recommendation=_jb.WorkerRecommendation(
            worker_type="G.1X",
            number_of_workers=4,
            timeout_minutes=60,
            estimated_cost_per_run=1.0,
            estimated_duration_minutes=30,
            rationale="test",
            confidence=0.9,
            mcp_tools_used=[],
        ),
        target_config=dict(TARGET_CONFIG),
        test_results=None,
        run_id="run-1",
        source_path="s3://src/pipe-1/data.csv",
        job_config_overrides=job_config_overrides,
    )


class TestCreateGlueJobAppliesOverrides:
    """create_glue_job merges overrides before update_job/create_job — the
    Glue API receives the upgraded WorkerType, and the returned job info
    reflects the ACTUAL config used.

    **Validates: Requirements 9.5**
    """

    def test_update_job_receives_overridden_worker_type(self, glue_env):
        result = _call_create_glue_job(job_config_overrides={"WorkerType": "G.2X"})

        job_update = glue_env.update_job.call_args.kwargs["JobUpdate"]
        assert job_update["WorkerType"] == "G.2X"  # override wins over generated G.1X
        assert job_update["NumberOfWorkers"] == 4  # generated value intact
        assert job_update["Timeout"] == 60  # generated value intact
        # The result (→ state row / pipelines metadata) tells the truth.
        assert result["worker_type"] == "G.2X"
        assert result["number_of_workers"] == 4
        assert result["job_config_overrides_applied"] == {"WorkerType": "G.2X"}

    def test_no_overrides_generated_config_unchanged(self, glue_env):
        result = _call_create_glue_job(job_config_overrides=None)

        job_update = glue_env.update_job.call_args.kwargs["JobUpdate"]
        assert job_update["WorkerType"] == "G.1X"
        assert job_update["NumberOfWorkers"] == 4
        assert result["worker_type"] == "G.1X"
        assert "job_config_overrides_applied" not in result

    def test_create_path_applies_overrides(self, glue_env):
        glue_env.get_job.side_effect = _EntityNotFound("no job")

        result = _call_create_glue_job(job_config_overrides={"WorkerType": "G.2X", "NumberOfWorkers": Decimal("10")})

        create_kwargs = glue_env.create_job.call_args.kwargs
        assert create_kwargs["WorkerType"] == "G.2X"
        assert create_kwargs["NumberOfWorkers"] == 10
        assert isinstance(create_kwargs["NumberOfWorkers"], int)
        assert create_kwargs["Tags"]["WorkerType"] == "G.2X"
        assert result["worker_type"] == "G.2X"
        assert result["number_of_workers"] == 10


# ── End-to-end shape: heal-rerun row → handler → create_glue_job ─────────


class TestHandlerDeliversOverridesFromFileValidationRow:
    """The handler reads job_config_overrides from the file_validation state
    row (where the heal rerun's file_validation persisted it, task 8.4
    delivery path) and threads it into create_glue_job.

    **Validates: Requirements 9.5**
    """

    @pytest.fixture
    def handler_env(self, monkeypatch):
        lf = _load("lambda_function", "jco_lambda_function")

        # file_validation row carries the heal remediation config, exactly as
        # file_validation persisted it (Decimal-laden from DynamoDB).
        fv_item = {
            "file_type": "csv",
            "job_config_overrides": {"WorkerType": "G.2X", "NumberOfWorkers": Decimal("10")},
        }
        mock_runs_table = mock.MagicMock()
        mock_runs_table.get_item.return_value = {"Item": fv_item}
        monkeypatch.setattr(lf, "pipeline_runs_table", mock_runs_table)

        monkeypatch.setattr(lf, "AgentTracePlugin", mock.MagicMock())
        monkeypatch.setattr(lf, "write_agent_audit_entry", None)
        monkeypatch.setattr(lf, "PipelineMetrics", None)
        monkeypatch.setattr(lf, "get_generated_code_from_pipeline_runs", lambda p, r: {"code_s3_uri": "s3://b/code.py"})
        monkeypatch.setattr(lf, "get_target_config_from_pipeline", lambda p: dict(TARGET_CONFIG))
        monkeypatch.setattr(lf, "get_source_path", lambda run_id, source_format="": "s3://src/pipe-1/data.csv")
        monkeypatch.setattr(
            lf,
            "validate_job_prerequisites",
            lambda **kw: {"valid": True, "errors": [], "warnings": [], "recommendations": []},
        )
        # MCP unavailable → rule-based fallback path calls create_glue_job directly.
        monkeypatch.setattr(lf, "initialize_strands_agent_with_mcp", lambda **kw: (None, None))
        monkeypatch.setattr(lf, "initialize_strands_agent_with_multi_mcp", lambda **kw: (None, None))
        monkeypatch.setattr(lf, "get_test_results_from_pipeline_runs", mock.MagicMock(side_effect=ValueError("none")))
        monkeypatch.setattr(lf, "get_data_characteristics", lambda p, r, e: {})

        recommendation = mock.MagicMock()
        recommendation.worker_type = "G.1X"
        recommendation.number_of_workers = 4
        recommendation.to_dict.return_value = {"worker_type": "G.1X", "number_of_workers": 4}
        monkeypatch.setattr(lf, "get_default_worker_recommendation", lambda dc: recommendation)

        monkeypatch.setattr(lf, "create_glue_database", lambda p: "db-pipe-1")
        create_glue_job_spy = mock.MagicMock(
            return_value={
                "job_name": "agentic-data-ingestion-job-pipe-1",
                "worker_type": "G.2X",
                "number_of_workers": 10,
                "timeout_minutes": 60,
                "job_config_overrides_applied": {"WorkerType": "G.2X", "NumberOfWorkers": 10},
            }
        )
        monkeypatch.setattr(lf, "create_glue_job", create_glue_job_spy)
        monkeypatch.setattr(lf, "estimate_job_cost", lambda cfg: {"estimated_cost_per_run": 1.0})
        monkeypatch.setattr(lf, "update_pipeline_metadata_with_glue_job", mock.MagicMock())
        monkeypatch.setenv("USE_AGENTCORE_RUNTIME", "false")
        return lf, create_glue_job_spy, mock_runs_table

    def test_heal_rerun_overrides_reach_create_glue_job(self, handler_env):
        lf, create_glue_job_spy, mock_runs_table = handler_env

        event = {"run_id": "run-heal-fresh", "pipeline_id": "pipe-1", "file_size_bytes": 1000, "file_count": 1}
        result = lf.lambda_handler(event, mock.MagicMock())

        # The handler read the file_validation row by run_id …
        fv_get = [
            c
            for c in mock_runs_table.get_item.call_args_list
            if c.kwargs.get("Key", {}).get("step_name") == "file_validation"
        ]
        assert fv_get, "handler never read the file_validation row"
        assert fv_get[0].kwargs["Key"]["run_id"] == "run-heal-fresh"
        assert "job_config_overrides" in fv_get[0].kwargs["ProjectionExpression"]

        # … and threaded the overrides into create_glue_job.
        assert create_glue_job_spy.called, "create_glue_job was never called"
        passed = create_glue_job_spy.call_args.kwargs["job_config_overrides"]
        assert passed == {"WorkerType": "G.2X", "NumberOfWorkers": Decimal("10")}

        # The step output reflects the actual (upgraded) config.
        assert result["glue_job"]["worker_type"] == "G.2X"

    def test_normal_run_passes_empty_overrides(self, handler_env):
        lf, create_glue_job_spy, mock_runs_table = handler_env
        mock_runs_table.get_item.return_value = {"Item": {"file_type": "csv"}}

        event = {"run_id": "run-normal", "pipeline_id": "pipe-1", "file_size_bytes": 1000, "file_count": 1}
        lf.lambda_handler(event, mock.MagicMock())

        assert create_glue_job_spy.call_args.kwargs["job_config_overrides"] == {}
