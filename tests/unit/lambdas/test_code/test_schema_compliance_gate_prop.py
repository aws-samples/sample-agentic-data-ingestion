# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 1 — the vacuous schema-compliance gate (p33 Bug Condition 1).

For any ``test_code`` run of a pipeline that HAS a target schema, where the
transform's output columns are not all present in that schema, the fixed code
SHALL report ``schema_compliance`` below 100, SHALL name the absent columns in
``missing_columns``, and SHALL emit a ``schema_compliance`` check whose
``passed`` is false, so ``test_status`` derives ``failed``.

Bug condition (design §Bug Details)::

    isBugCondition(input) =
        pipelineHasTargetSchema(input.pipeline_id)
        AND readTargetSchema(input) = {}
        AND reportedSchemaCompliance(input) = 100.0

**RED ON UNFIXED CODE — the failure is task 1's deliverable.** The handler reads
``target_schema`` from the schema-mappings record
(``test_code/lambda_function.py:976-985``); that table has never carried the
attribute, so the read yields ``{}``, ``_build_validation_code``'s ``expected``
list is empty, ``code_interpreter_runner.py:374`` short-circuits
``schema_compliance`` to ``100.0``, and ``aggregate_check_results`` emits no
``schema_compliance`` check at all.

Expectations here derive from requirements 2.1/2.2/2.6/2.7, NOT from current
output — current output is the defect.

Scoped to the live counterexample for reproducibility: run
``run-2026-08-05-14-29-05-cc73973d``, execution
``…:execution:agentic-data-ingestion-pipeline:file-p32ver_renC_csv-20260805T142902-51f5323dd3ac``,
whose transform renamed six columns to ``_out`` names that the target schema
does not declare — and which still produced a passing
``code_interpreter_execution`` and ``schema_compliance == 100.0``.

What is real here and what is simulated
---------------------------------------
Real: the target-schema read (driven through ``lambda_handler``), the pandas
converter, ``_build_test_data_code``, ``_build_validation_code``, the score
arithmetic (the generated validation code is *executed*), ``_parse_results``
and ``aggregate_check_results``.

Simulated: the interpreter's ``df_transformed``. ``pandas`` is not installed in
the unit environment, so the transform's output frame is supplied as a fixture
(the same ``_FakeDataFrame`` shim pattern
``test_code_interpreter_runner.py:_run_validation_code`` established) rather
than produced by running the converted transform. The frame's columns ARE the
property's input — what is under test is the read, the scoring and the gating,
not the pandas proxy.

Validates: Requirements 1.1, 1.2, 1.3, 1.7, 1.8
"""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The runner and the converter are the REAL modules — only the
# bedrock-agentcore client is stubbed, so the generated frame code, the
# generated validation code and the score it computes are all genuinely
# produced by the shipped implementation.
from code_interpreter_runner import CodeInterpreterTestRunner

# p42 task 11.3 (Property 19). The REAL Glue result builders, imported here —
# BEFORE the `patch.dict("sys.modules", _mods)` block below, for the same reason
# `target_schema_resolution` is: that context manager restores `sys.modules` on
# exit and would drop a module first imported inside it. `glue_session_runner`
# imports from the real `code_interpreter_runner` (already bound above) and
# builds no boto3 client at import time — its only `boto3.client("glue", …)` is
# inside `GlueSessionTestRunner.__init__` (`glue_session_runner.py:520`), which
# nothing here constructs.
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    NOT_VERIFIED_REASON_SESSION_CAPACITY,
    NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    FrameBuildError,
    FrameSource,
    GlueSessionError,
    StatementOutcome,
    frame_build_failure_results,
    frame_refusal_results,
    lifecycle_failure_results,
    not_executed_results,
    sample_read_failure_results,
    sample_read_format,
    session_capacity_results,
    session_creation_failure_results,
    statement_failure_results,
    success_results,
)

# The declared Test_Result_Contract (p42 task 1.2) — the hand-written oracle the
# Glue specimens below are held to. Deliberately NOT captured from a run:
# `LGR-28b4d75-5` (template / ratified) records that a test written from the
# producer's output cannot detect the producer drifting.
from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS, SUCCESS, shape_violations

# p33 task 4.3 moved the target-schema read into the shared resolver, which
# builds its OWN lazily-cached S3 client — so `patch.object(_mod, "s3_client",
# ...)` is no longer the seam for that read, and without this the handler makes
# a REAL `get_object` call from a unit test. Imported HERE, before the
# `patch.dict("sys.modules", ...)` block below, deliberately: that context
# manager restores the dict on exit and would drop a module first imported
# inside it, so importing after the block would bind a SECOND instance whose
# `_s3_client` is not the one `lambda_function` calls. Same convention as
# `tests/unit/lambdas/schema_mapping/test_get_target_schema_characterization.py`.
import target_schema_resolution as _tsr
from step_errors import StepFailure

# lambda_function pulls in strands/AWS at import time; stub those only.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# Minimal pandas stand-ins — enough surface for the generated validation code.
# Deliberately local rather than imported from test_code_interpreter_runner:
# importing one test module from another couples their sys.path setup.
# ---------------------------------------------------------------------------


class _FakeSeries:
    def __init__(self, values):
        self._values = list(values)
        self.dtype = "object"

    def isna(self):
        return _FakeSeries([v is None for v in self._values])

    def sum(self):
        return sum(1 for v in self._values if v is True)


class _FakeDataFrame:
    def __init__(self, data: dict):
        self._data = {k: list(v) for k, v in data.items()}
        self.columns = list(self._data.keys())

    def __len__(self):
        return len(next(iter(self._data.values()), []))

    def __getitem__(self, col):
        return _FakeSeries(self._data[col])

    def duplicated(self):
        rows = list(zip(*self._data.values(), strict=True))
        seen: set = set()
        flags = []
        for row in rows:
            flags.append(row in seen)
            seen.add(row)
        return _FakeSeries(flags)


_ROWS = 4


def _run_validation_code(code: str, produced_columns: list[str]) -> str:
    """Execute the REAL generated validation code against a fake frame.

    Returns the ``VALIDATION:`` stdout line the interpreter would have printed,
    so ``_parse_results`` sees a genuinely computed ``schema_compliance`` and
    ``missing_columns`` rather than a hand-written number.
    """
    printed: list[str] = []
    frame = _FakeDataFrame({c: [f"v{i}" for i in range(_ROWS)] for c in produced_columns})
    namespace = {
        "print": lambda *a: printed.append(" ".join(str(x) for x in a)),
        "df_transformed": frame,
    }
    exec(code, namespace)
    return next(line for line in printed if line.startswith("VALIDATION:"))


_FRAME_COLUMN_RE = re.compile(r'^\s+"([^"]+)": \[', re.MULTILINE)


def _frame_columns(frame_code: str) -> list[str]:
    """Columns the REAL ``_build_test_data_code`` output declares."""
    return _FRAME_COLUMN_RE.findall(frame_code)


# ---------------------------------------------------------------------------
# Handler harness — drives the target-schema read, which is the defect site.
# ---------------------------------------------------------------------------

_RUN_ID = "run-2026-08-05-14-29-05-cc73973d"
_PIPELINE_ID = "pipe-p32ver_renC_csv"
_MAPPING_ID = "map-1"
_CODE_BUCKET = "code-bucket"
_CODE_KEY = "generated/code.py"
_SCHEMA_BUCKET = "adi-target-schemas-123456789012-us-east-1"
_SCHEMA_KEY = f"{_PIPELINE_ID}/target-schema.json"

#: The schema-mappings record as the live table really shapes it: hashes only.
#: The absence of ``source_schema`` / ``target_schema`` here IS the bug — the
#: attribute the handler reads has never existed on this item.
_MAPPING_ITEM = {
    "mapping_id": _MAPPING_ID,
    "pipeline_id": _PIPELINE_ID,
    "source_schema_hash": "3f1a" * 16,
    "target_schema_hash": "9c2b" * 16,
    "mapping_config": {"field_mappings": []},
    "status": "approved",
}

_CODE_GEN_ITEM = {
    "run_id": _RUN_ID,
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": f"s3://{_CODE_BUCKET}/{_CODE_KEY}"},
}

#: Reviewer passes everything, so a schema-compliance shortfall is the ONLY
#: thing that can fail the run — the antecedent Property 1 needs.
_REVIEW_ALL_PASS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {"Correctness": "pass", "Security": "pass", "Schema Compliance": "pass"},
    }
)


def _schema_of(columns) -> dict:
    return {"type": "object", "properties": {c: {"type": "string"} for c in columns}}


def _generated_code(transform_block: str) -> str:
    return (
        "# Glue scaffolding\n"
        "# === AI-Generated Transform Block ===\n"
        f"{transform_block}\n"
        "# === End Transform Block ===\n"
        "# Iceberg write scaffolding\n"
    )


def _rename_transform(source_columns) -> tuple[str, list[str]]:
    """The live counterexample's shape: rename every source column to ``_out``.

    Returns the transform block and the columns the transformed frame carries.
    """
    lines = []
    src = "df"
    produced = []
    for col in source_columns:
        out = f"{col}_out"
        lines.append(f'df_transformed = {src}.withColumnRenamed("{col}", "{out}")')
        produced.append(out)
        src = "df_transformed"
    return "\n".join(lines), produced


def _stream(stdout: str, stderr: str = "", exit_code: int = 0) -> dict:
    return {
        "stream": [
            {
                "result": {
                    "content": [{"type": "text", "text": stdout}],
                    "structuredContent": {"stdout": stdout, "stderr": stderr, "exitCode": exit_code},
                }
            }
        ]
    }


def _make_ci_client(produced_columns: list[str], seen: dict) -> MagicMock:
    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-1"}
    client.stop_code_interpreter_session.return_value = {}

    def _invoke(**kwargs):
        code = kwargs["arguments"]["code"]
        if 'print("GENERATED:' in code:
            seen["frame_code"] = code
            payload = {"record_count": 100, "columns": _frame_columns(code)}
            return _stream(f"GENERATED:{json.dumps(payload)}")
        if "_df = df_transformed" in code:
            seen["validation_code"] = code
            return _stream(_run_validation_code(code, produced_columns))
        return _stream("transform executed")

    client.invoke_code_interpreter.side_effect = _invoke
    return client


def _run_handler(
    *,
    source_schema: dict,
    pipeline_record: dict,
    target_schema_object: dict | None,
    transform_block: str,
    produced_columns: list[str],
    mapping_item_extras: dict | None = None,
    event_extras: dict | None = None,
):
    """Run ``lambda_handler`` with the target schema in its canonical place.

    ``pipeline_record`` is what the pipelines table returns; ``target_schema_object``
    is what S3 holds at ``target_schema_bucket``/``target_schema_s3_key``. Both
    are ignored by the unfixed code, which is the point.

    ``mapping_item_extras`` and ``event_extras`` merge into the schema-mappings
    record and the invocation event respectively. Both default to nothing, so
    every other caller is unaffected; they exist so
    ``test_the_source_frame_comes_from_the_schema_extract_row_not_another_authority``
    can plant a DECOY schema on each of the two rival authorities the handler
    must not read (requirement 2.3).

    Returns ``(response, captured)`` where ``captured["execution_results"]`` is
    the runner's real structured result.
    """
    generated_code = _generated_code(transform_block)

    runs_table = MagicMock()

    def _runs_get_item(Key):
        if Key["step_name"] == "create_code":
            return {"Item": _CODE_GEN_ITEM}
        if Key["step_name"] == "schema_extract":
            # The authority create_code already uses (create_code:406-416).
            return {"Item": {**Key, "extracted_schema": json.dumps(source_schema)}}
        return {}

    runs_table.get_item.side_effect = _runs_get_item

    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}

    mappings_table = MagicMock()
    mappings_table.get_item.return_value = {"Item": {**_MAPPING_ITEM, **(mapping_item_extras or {})}}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(pipeline_record)} if pipeline_record else {}

    def _table(name):
        if "mapping" in name:
            return mappings_table
        if "pipeline" in name:
            return pipelines_table
        raise AssertionError(f"unexpected dynamodb.Table({name!r})")

    fake_dynamodb = MagicMock()
    fake_dynamodb.Table.side_effect = _table

    s3 = MagicMock()

    def _get_object(Bucket, Key, **_kw):
        if (Bucket, Key) == (_CODE_BUCKET, _CODE_KEY):
            return {"Body": MagicMock(read=MagicMock(return_value=generated_code.encode()))}
        if (Bucket, Key) == (_SCHEMA_BUCKET, _SCHEMA_KEY) and target_schema_object is not None:
            body = json.dumps(target_schema_object).encode()
            return {"Body": MagicMock(read=MagicMock(return_value=body))}
        raise AssertionError(f"unexpected S3 read: s3://{Bucket}/{Key}")

    s3.get_object.side_effect = _get_object

    agent = MagicMock(return_value=_REVIEW_ALL_PASS)
    agent.add_hook = MagicMock()

    seen: dict = {}
    ci_client = _make_ci_client(produced_columns, seen)
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = ci_client
        runner = CodeInterpreterTestRunner(region="us-east-1")
    assert runner.client is ci_client, "guard: a None client takes the _empty_results path instead"

    captured: dict = {"seen": seen}
    _real_run_test = runner.run_test

    def _run_test(transform, src_schema, tgt_schema):
        captured["source_schema_passed"] = src_schema
        captured["target_schema_passed"] = tgt_schema
        results = _real_run_test(transform, src_schema, tgt_schema)
        captured["execution_results"] = results
        return results

    runner.run_test = _run_test

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        # The resolver's own seam — the same mock, so the assertion in
        # `_get_object` that no unexpected object is read still covers the
        # target-schema read after task 4.3's delegation.
        patch.object(_tsr, "_s3_client", lambda: s3),
        patch.object(_mod, "dynamodb", fake_dynamodb),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler(
            {
                "run_id": _RUN_ID,
                "pipeline_id": _PIPELINE_ID,
                "mapping_id": _MAPPING_ID,
                **(event_extras or {}),
            },
            None,
        )
    return response, captured


def _checks(response) -> list[dict]:
    return response["test_result"]["checks"]


def _named(checks, name) -> list[dict]:
    return [c for c in checks if c["name"] == name]


# ---------------------------------------------------------------------------
# Generators — scoped to the live counterexample's shape (six `_out` renames)
# ---------------------------------------------------------------------------

_SOURCE_POOL = ["order_id", "customer", "state", "amount", "city", "sku", "qty", "region"]
_TARGET_POOL = ["order_key", "customer_name", "state_code", "total_amount", "city_name", "product_sku"]


@st.composite
def disjoint_target_schemas(draw):
    """A target schema of N columns and a transform producing none of them.

    ``_out``-suffixed rename targets can never collide with the target pool,
    so the produced set is disjoint by construction — the live shape.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    source = draw(st.lists(st.sampled_from(_SOURCE_POOL), min_size=n, max_size=n, unique=True))
    declared = draw(st.lists(st.sampled_from(_TARGET_POOL), min_size=n, max_size=n, unique=True))
    return source, declared


@st.composite
def partial_shortfall_schemas(draw):
    """N declared columns, of which the transform produces N-1."""
    n = draw(st.integers(min_value=2, max_value=6))
    declared = draw(st.lists(st.sampled_from(_TARGET_POOL), min_size=n, max_size=n, unique=True))
    absent = draw(st.sampled_from(declared))
    return declared, absent


_SETTINGS = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# Case 1 — the live counterexample, scoped
# ---------------------------------------------------------------------------


@_SETTINGS
@given(disjoint_target_schemas())
def test_absent_target_columns_fail_the_schema_compliance_gate(case):
    """Property 1. RED on unfixed code — that failure is task 1's deliverable.

    Validates: Requirements 1.1, 1.2, 2.1, 2.2
    """
    source, declared = case
    transform_block, produced = _rename_transform(source)
    assert not (set(produced) & set(declared)), "guard: the produced columns must be disjoint from the declared ones"

    response, captured = _run_handler(
        source_schema=_schema_of(source),
        pipeline_record={
            "pipeline_id": _PIPELINE_ID,
            "target_schema_bucket": _SCHEMA_BUCKET,
            "target_schema_s3_key": _SCHEMA_KEY,
        },
        target_schema_object=_schema_of(declared),
        transform_block=transform_block,
        produced_columns=produced,
    )

    # 2.2 — a shortfall is reported, and the absent columns are named. The
    # root cause is reported alongside it: an empty `target_schema_passed`
    # means the read is still aimed at the mappings record (2.1).
    quality = captured["execution_results"]["data_quality_results"]
    assert quality["schema_compliance"] < 100, (
        f"schema_compliance is {quality['schema_compliance']} for a transform producing "
        f"{produced} against a target schema declaring {declared} — none of the declared "
        "columns exist in the output. The target schema handed to run_test was "
        f"{captured['target_schema_passed']!r}"
        + (
            " — empty, so the read still points at the mappings record "
            "(test_code/lambda_function.py:976-985) and code_interpreter_runner.py:374 "
            "short-circuits the score to 100.0"
            if not captured["target_schema_passed"]
            else ""
        )
    )
    assert set(quality["missing_columns"]) == set(declared), (
        f"missing_columns is {quality['missing_columns']}, expected every declared column {declared}"
    )

    # Property 1 — the shortfall is an emitted, FAILING check…
    compliance = _named(_checks(response), "schema_compliance")
    assert len(compliance) == 1, (
        f"expected exactly one schema_compliance check, got {[c['name'] for c in _checks(response)]}"
    )
    assert compliance[0]["passed"] is False, f"schema_compliance check passed despite {declared} being absent"
    # …stated as text, not colour alone (C5 / requirement 3.13).
    for column in declared:
        assert column in compliance[0]["detail"], f"detail does not name the absent column {column!r}"

    # …and it fails the run, with no other check failing.
    assert response["test_status"] == "failed", (
        f"test_status is {response['test_status']!r}; a transform producing none of the "
        "declared target columns must fail the run"
    )
    assert _named(_checks(response), "code_interpreter_execution"), (
        "guard: the transform did execute — this case is a scoring failure, not an execution failure"
    )


# ---------------------------------------------------------------------------
# Case 2 — partial shortfall (N declared, N-1 produced)
# ---------------------------------------------------------------------------


@_SETTINGS
@given(partial_shortfall_schemas())
def test_partial_shortfall_names_only_the_absent_column(case):
    """One declared column absent → below 100, named, failing.

    Validates: Requirements 1.2, 2.2
    """
    declared, absent = case
    produced = [c for c in declared if c != absent]
    source = [f"src_{i}" for i in range(len(declared))]

    response, captured = _run_handler(
        source_schema=_schema_of(source),
        pipeline_record={
            "pipeline_id": _PIPELINE_ID,
            "target_schema_bucket": _SCHEMA_BUCKET,
            "target_schema_s3_key": _SCHEMA_KEY,
        },
        target_schema_object=_schema_of(declared),
        transform_block="\n".join(
            f'df_transformed = {"df" if i == 0 else "df_transformed"}.withColumnRenamed("src_{i}", "{c}")'
            for i, c in enumerate(produced)
        ),
        produced_columns=produced,
    )

    quality = captured["execution_results"]["data_quality_results"]
    assert quality["schema_compliance"] < 100, (
        f"schema_compliance is {quality['schema_compliance']} with {absent!r} absent from the output"
    )
    assert quality["missing_columns"] == [absent], (
        f"missing_columns is {quality['missing_columns']}, expected exactly [{absent!r}]"
    )

    compliance = _named(_checks(response), "schema_compliance")
    assert len(compliance) == 1, f"expected one schema_compliance check, got {[c['name'] for c in _checks(response)]}"
    assert compliance[0]["passed"] is False
    assert absent in compliance[0]["detail"], f"detail does not name the absent column {absent!r}"
    assert response["test_status"] == "failed"


# ---------------------------------------------------------------------------
# Case 3 — a genuinely schema-less pipeline is NOT a passing score
# ---------------------------------------------------------------------------


def test_schema_less_pipeline_reports_not_verified_not_a_pass():
    """Requirement 1.7 / 2.6 — "could not check" must not read as "checked and passed".

    ``passed`` is asserted as "not True" rather than "is False": D2 fixes the
    ``verified: False`` discriminator and the reason, and leaves the ``passed``
    spelling to the implementation. What the requirement forbids is the entry
    reading as a pass.

    Validates: Requirements 1.7, 2.6
    """
    source = ["order_id", "customer"]
    transform_block, produced = _rename_transform(source)

    response, captured = _run_handler(
        source_schema=_schema_of(source),
        # No target_schema_bucket, no target_schema_s3_key — never uploaded.
        pipeline_record={"pipeline_id": _PIPELINE_ID},
        target_schema_object=None,
        transform_block=transform_block,
        produced_columns=produced,
    )

    compliance = _named(_checks(response), "schema_compliance")
    assert len(compliance) == 1, (
        "a pipeline with no target schema emitted no schema_compliance entry, so "
        "'we could not check' is indistinguishable from 'we checked and it passed'; "
        f"checks were {[c['name'] for c in _checks(response)]}"
    )
    entry = compliance[0]
    assert entry.get("verified") is False, f"schema_compliance entry carries no verified:False discriminator: {entry}"
    assert entry.get("not_verified_reason") == "no_target_schema", (
        f"expected not_verified_reason 'no_target_schema', got {entry.get('not_verified_reason')!r}"
    )
    assert entry.get("passed") is not True, "an unverifiable schema-compliance check must not read as a pass"
    assert "no target schema" in entry.get("detail", "").lower(), (
        f"detail does not state in text that no target schema was available: {entry.get('detail')!r}"
    )

    # C4 — the entry stays in the list but is partitioned out of the derivation,
    # so an unverifiable check does not present as a failure (D2's fence).
    assert response["test_status"] == "passed", (
        f"test_status is {response['test_status']!r}; an unverified schema-compliance "
        "check must not fail a run whose every verified check passed"
    )
    assert captured["execution_results"]["execution_successful"] is True


# ---------------------------------------------------------------------------
# Case 4 — a half-written pipeline record is an error, not an absent schema
# ---------------------------------------------------------------------------


def test_key_without_bucket_fails_naming_both_attributes():
    """Requirement 2.7 / C1 — resolve the pair together, raise naming both.

    Validates: Requirement 2.7
    """
    source = ["order_id", "customer"]
    transform_block, produced = _rename_transform(source)

    with pytest.raises(StepFailure) as excinfo:
        _run_handler(
            source_schema=_schema_of(source),
            # target_schema_s3_key with no target_schema_bucket — the record is
            # incomplete, which is not the same as having no schema.
            pipeline_record={"pipeline_id": _PIPELINE_ID, "target_schema_s3_key": _SCHEMA_KEY},
            target_schema_object=None,
            transform_block=transform_block,
            produced_columns=produced,
        )

    message = str(excinfo.value)
    assert "target_schema_s3_key" in message, f"failure does not name target_schema_s3_key: {message}"
    assert "target_schema_bucket" in message, f"failure does not name target_schema_bucket: {message}"


# ---------------------------------------------------------------------------
# Case 5 — the SOURCE schema's authority (requirement 2.3)
#
# Added by p33 task 4.12. Task 4.11's coverage audit found requirement 2.3 (and
# its mirror, defect 1.3) asserted NOWHERE: this harness has always supplied a
# source schema through the run's `schema_extract` row and has always captured
# what reached `run_test` as `captured["source_schema_passed"]`, but nothing ever
# asserted on it.
#
# What the pre-existing coverage does and does not catch. A TOTAL regression —
# the source schema stops arriving at all — is caught incidentally: task 4.10's
# `_frame_refusal` returns `no_source_schema` for an empty schema
# (`code_interpreter_runner.py:292-298`), the run never starts a session, no
# `code_interpreter_execution` entry is emitted, and Cases 1-4 fail on their
# guards. But a WRONG-AUTHORITY read — one that went back to the mappings
# record's never-written `source_schema` attribute, or to the event, and happened
# to find something there — would satisfy every one of those assertions while
# exercising the transform against a frame shaped like the wrong input. That is
# precisely the defect class this spec exists to remove (the target-schema read
# failed the same way), so it gets an assertion rather than an inference.
#
# The two rival authorities are therefore planted with DECOYS here. Neither is
# read by the current implementation, which is the point: this test states
# where the schema must come from, so a future read from either place fails
# loudly instead of passing on a plausible-looking frame.
# ---------------------------------------------------------------------------

#: A schema that exists on no authority the handler is allowed to consult. Its
#: field names share no prefix with the real ones, so a mixed-up read shows up
#: in the generated frame code rather than in a subtle count.
_DECOY_SOURCE_SCHEMA = _schema_of(["decoy_from_the_wrong_authority", "decoy_second"])


def test_the_source_frame_comes_from_the_schema_extract_row_not_another_authority():
    """Requirement 2.3 — the frame derives from the run's ``schema_extract`` row.

    ``test_code/lambda_function.py:1253-1265`` reads ``extracted_schema`` off the
    ``(run_id, schema_extract)`` state row — the same authority ``create_code``
    already uses at ``create_code/lambda_function.py:406-416`` — and hands it to
    ``run_test`` unchanged (``:1474``). This asserts that identity, not merely
    that something non-empty arrived.

    The two places the old code looked, or could plausibly be made to look
    again, both carry a decoy: the schema-mappings record (whose
    ``source_schema`` attribute no writer has ever set — that read is defect
    1.3) and the invocation event. Reading either yields the decoy, and the
    decoy is asserted against.

    Validates: Requirements 2.3
    """
    row_source = ["order_id", "customer", "state"]
    row_schema = _schema_of(row_source)
    transform_block, produced = _rename_transform(row_source)

    assert not (set(row_schema["properties"]) & set(_DECOY_SOURCE_SCHEMA["properties"])), (
        "guard: the decoy must share no field name with the schema_extract row's schema, "
        "or a wrong-authority read could not be distinguished"
    )

    response, captured = _run_handler(
        source_schema=row_schema,
        pipeline_record={
            "pipeline_id": _PIPELINE_ID,
            "target_schema_bucket": _SCHEMA_BUCKET,
            "target_schema_s3_key": _SCHEMA_KEY,
        },
        # Declared == produced, so schema compliance passes and the only thing
        # this test can fail on is the source authority.
        target_schema_object=_schema_of(produced),
        transform_block=transform_block,
        produced_columns=produced,
        # Authority 1: the schema-mappings record — where the pre-fix code read
        # `source_schema` from (defect 1.3). No writer sets it, so a read from
        # here returns {} on the live path; the decoy makes a read from here
        # visible instead of silently degrading to the refusal path.
        mapping_item_extras={"source_schema": _DECOY_SOURCE_SCHEMA},
        # Authority 2: the event. Nothing reads it today (`:1191-1196` takes
        # run_id / pipeline_id / code_version_id / code_s3_uri / source_data_uri
        # / regeneration_attempt and no schema), and nothing should start.
        event_extras={"source_schema": _DECOY_SOURCE_SCHEMA},
    )

    passed_schema = captured["source_schema_passed"]
    assert passed_schema == row_schema, (
        "the schema handed to run_test is not the one the schema_extract state row carried. "
        f"Row carried {row_schema}, run_test received {passed_schema}"
        + (
            " — that is the DECOY planted on the schema-mappings record and the event, so the "
            "read has gone back to a non-authority (requirement 2.3)"
            if passed_schema == _DECOY_SOURCE_SCHEMA
            else ""
        )
    )

    # …and it is the schema the frame was actually BUILT from, not merely the
    # argument that reached the boundary.
    frame_columns = _frame_columns(captured["seen"]["frame_code"])
    assert frame_columns == row_source, (
        f"the generated frame declares {frame_columns}, expected one column per field the "
        f"schema_extract row declares, in order: {row_source}"
    )

    # Guard: the run really did execute against that frame, so the assertions
    # above are about a live path and not about a refused one.
    assert _named(_checks(response), "code_interpreter_execution"), (
        f"guard: the transform did not execute — checks were {[c['name'] for c in _checks(response)]}"
    )
    assert captured["execution_results"]["executed"] is True


# ===========================================================================
# Property 19 — a schema-compliance score is reported EXACTLY when validation
# ran, and the claim has to survive a SECOND executor (p42 task 11.3).
#
# Everything above this line is p33's: it drives `lambda_handler` end to end and
# proves the gate works for the pandas proxy. This section is p42's regression
# guard on the same gate, and it asks a narrower question at a lower level.
#
# WHAT IS NEW HERE, AND WHY IT IS NOT COVERED ABOVE
# -------------------------------------------------
# The emission gate is one line — `if SCHEMA_COMPLIANCE_CHECK_NAME in
# quality_results` (`lambda_function.py:1343`), over
# `(execution_results or {}).get("data_quality_results") or {}`. p33 pinned it
# with worked examples for every shape the PANDAS runner can produce
# (`test_schema_compliance_emission.py`: `None`, a proxy refusal, an execution
# failure, a width refusal). The Glue runner produces four shapes that did not
# exist when those were written — a capacity refusal, a frame-build failure, a
# lifecycle failure, and a success whose validation stage produced no payload —
# and `aggregate_check_results` grew two new harness arms to render them
# (`:1222` capacity, `:1257` frame-unbuildable). None of that was asserted
# against the emission gate.
#
# "EXACTLY WHEN" IS A BICONDITIONAL, AND ONLY ONE HALF IS INTERESTING
# -------------------------------------------------------------------
# A test that only checked "validation ran → a score is reported" would pass
# against an implementation that reports a score unconditionally — which is
# precisely the defect p33 existed to remove, in the shape it had before the fix
# (`code_interpreter_runner.py` short-circuiting the score to 100.0 for an empty
# expected list, reported as a pass). So the absent direction carries the weight
# here: nine of the twelve enumerated Glue outcomes below produce NO payload, and
# :func:`test_the_absent_direction_covers_every_glue_shape_that_skips_validation`
# fences the enumeration so a later edit cannot quietly drop them and leave the
# property passing on the easy half alone.
#
# WHY THE RESULTS COME FROM THE SHIPPED BUILDERS
# -----------------------------------------------
# Every result below is produced by `glue_session_runner`'s real builders, not
# transcribed. `LGR-28b4d75-5` (**template / ratified**) gives the rule for the
# other direction — an oracle written from the producer's output cannot detect
# the producer drifting — and it applies to the ORACLE, which here is the
# hand-written table in `tests/fixtures/run_test_result_shapes.py`. The
# *specimens* should come from the producer, because what this property is about
# is whether the real pairing holds: a builder that stopped emitting
# `data_quality_results`, or started emitting a `schema_compliance` key on a
# path where nothing was validated, has to fail this test rather than pass it
# vacuously.
#
# In particular `success_results` runs the production `_parse_results`
# (`code_interpreter_runner.py:582-637`) over the stages' combined stdout, so
# whether the payload carries the score is decided by the shipped parser and not
# by this file. That matters for the two success sub-shapes below: `_parse_results`
# writes the seven `data_quality_results` keys — `schema_compliance` among them,
# defaulted to 0 — for ANY `VALIDATION:` payload without an `error` key, and
# leaves `data_quality_results` empty otherwise. So a Glue success is on both
# sides of the biconditional depending on what its validation stage printed,
# which is the pairing worth asserting and is unreachable from a hand-written
# dict.
#
# GOVERNING RECORDS
# -----------------
# `LGR-b2f79ef-1` (**template / ratified**) is the fence: `schema_compliance` is
# "emitted **only** when the execution produced validation output: on a proxy
# refusal, on `execution_results is None`, on an execution failure and on a
# harness non-verification there is no entry at all, because the transform never
# ran and there is nothing to compare." Property 19 is that sentence, generalised
# over the reason string so the two harness reasons p42 added are covered by the
# same statement rather than by two more examples.
#
# `LGR-b2f79ef-2` (**template / ratified**) is why the `target_schema is None`
# branch still EMITS an entry: p32's `verified: False` + `not_verified_reason`
# encoding, no third check state. So the biconditional is about the entry's
# PRESENCE, and R12.4's "no vacuous score" clause is a separate assertion about
# that entry's CONTENT —
# :func:`test_a_glue_run_with_no_target_schema_reports_a_non_verification_not_a_score`.
#
# `LGR-b2f79ef-6` (**template / ratified**) is why this is an extension rather
# than a rewrite: "an assertion that encoded a now-wrong contract is evidence of
# what the contract used to be". Nothing above is amended — p33's contract is not
# wrong, it is merely narrower than the executor set now is.
#
# NOT RE-ASSERTED HERE
# --------------------
# Full result-shape equivalence between the two executors is **Property 2**'s
# (task 8.2, `test_result_shape_equivalence_prop.py`). The only shape assertion
# below is the one this property depends on: that `data_quality_results` is
# present on every Glue result, so "the payload carries the score" is a statement
# about the payload's contents rather than about a missing key.
#
# Validates: Requirement 12.4
# ===========================================================================

#: A reviewer verdict that passes everything, so the emission gate is the only
#: thing under observation. It also has to be non-empty for a different reason:
#: `LGR-b2f79ef-2`'s revisit condition records that `test_status` derives
#: `failed` when NOTHING was verified, so a run whose only check carried
#: `verified: False` would fail for a reason unrelated to this property.
_PROP19_PARSED_PASS: dict = {"verdict": "pass", "issues": [], "test_categories": {}}

#: The reviewer's own Schema Compliance category — a deliberate near-miss.
#: `static_analysis:Schema Compliance` is a DIFFERENT check that p33 kept
#: distinct on purpose (`SCHEMA_COMPLIANCE_CHECK_NAME`'s comment,
#: `lambda_function.py:227-232`: "one is a column-set comparison over what the
#: transform actually produced, the other is an LLM opinion ... do not merge
#: them"). Drawn into `test_categories` on some examples so that "exactly one
#: `schema_compliance` entry" is asserted against a check list that also holds
#: the similarly-named one.
_REVIEWER_SCHEMA_CATEGORY = "Schema Compliance"

#: A declared target schema, for the `target_schema` arm that reports a score.
_PROP19_TARGET_SCHEMA = _schema_of(["order_key", "customer_name"])

#: The key INSIDE ``data_quality_results`` whose presence is the emission gate's
#: antecedent — written by ``_parse_results`` (``code_interpreter_runner.py:626``).
#: It happens to spell the same string as
#: ``lambda_function.SCHEMA_COMPLIANCE_CHECK_NAME``, which is the name of the
#: emitted CHECK, and the two are deliberately kept separate here: they are the
#: two sides of the biconditional, and conflating them would make the assertion
#: read as a tautology.
SCHEMA_COMPLIANCE_KEY = "schema_compliance"


def _outcome(
    stage: str,
    stdout: str = "",
    *,
    state: str = "AVAILABLE",
    output_status: str | None = "ok",
    statement: dict | None = None,
) -> StatementOutcome:
    """One observed Glue statement, in the shape the real mapper consumes."""
    return StatementOutcome(
        stage=stage,
        state=state,
        output_status=output_status,
        stdout=stdout,
        statement=statement or {},
    )


def _generated_marker(columns: list[str]) -> str:
    return f"GENERATED:{json.dumps({'record_count': 100, 'columns': columns})}"


def _validation_marker(payload: dict) -> str:
    return f"VALIDATION:{json.dumps(payload)}"


def _full_validation_payload(score: float, missing: list[str]) -> dict:
    """The nine-key payload the Spark validation statement emits (design §4.9)."""
    return {
        "record_count": 100,
        "completeness": 100.0,
        "uniqueness": 100.0,
        "schema_compliance": score,
        "overall_quality": 100.0,
        "null_counts": {},
        "dtypes": {},
        "duplicates": 0,
        "missing_columns": missing,
    }


def _glue_success(payload: dict | None) -> dict:
    """A Glue run whose stages all reported ``ok``, via the shipped builder.

    ``payload`` of ``None`` means the validation stage printed no ``VALIDATION:``
    marker at all. That is a real outcome rather than a contrivance:
    ``success_results``' own docstring records that it "cannot manufacture a
    success" — ``_parse_results`` sets ``execution_successful`` only where it
    found a payload — so a run routed here without one returns a result stating
    that nothing was validated, and therefore carries no score.
    """
    outcomes = [
        _outcome(STAGE_FRAME_BUILD, _generated_marker(["order_key", "customer_name"])),
        _outcome(STAGE_TRANSFORM),
        _outcome(STAGE_VALIDATION, _validation_marker(payload) if payload is not None else ""),
    ]
    return success_results(outcomes)


def _wide_source_schema() -> dict:
    """A source schema past ``_MAX_TEST_FRAME_COLUMNS``, for the width refusal."""
    return _schema_of([f"field_{i}" for i in range(513)])


#: A `sourceBucket` object URI of the shape `file_validation` writes —
#: `original_s3_uri = f"s3://{bucket}/{key}"` from the upload event's own bucket
#: (`file_validation/lambda_function.py:632`). p44 task 7.2's specimen reads from
#: it; nothing here opens it.
_SAMPLE_SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"


def _sample_frame_source() -> FrameSource:
    """A Sample_Frame decision, with the format resolved by production code.

    ``read_format`` comes from :func:`sample_read_format` rather than the literal
    ``"csv"``, so this fixture cannot describe a frame source the shipped
    resolver would never produce — and ``FrameSource.__post_init__`` refuses a
    ``sample`` mode without both a URI and a format, so a resolver that stopped
    recognising ``.csv`` fails here loudly rather than leaving a specimen that
    only looks like a sample. Same construction as
    ``test_sample_read_failure_builders.py``'s ``_sample_source``, for the reason
    that file records: ``LGR-28b4d75-5`` (**template / ratified**) — an
    expectation read off the thing under test cannot detect it drifting.
    """
    read_format = sample_read_format(_SAMPLE_SOURCE_URI)
    assert read_format is not None, f"{_SAMPLE_SOURCE_URI} must resolve a reader for this specimen to be a sample"
    return FrameSource(
        mode=FRAME_MODE_SAMPLE,
        requested_mode=FRAME_MODE_SAMPLE,
        source_uri=_SAMPLE_SOURCE_URI,
        read_format=read_format,
    )


def _denied_source_read() -> StatementOutcome:
    """The frame-build statement failing on the run's own object (p44 C11).

    ``AVAILABLE`` with an ``Output.Status`` other than ``ok`` — Spark raised,
    which is ``StatementOutcome.failed``'s first arm — on
    :data:`STAGE_FRAME_BUILD`, which on a Sample_Frame run **is** the source
    read. The ``ErrorValue`` carries no double-quoted span, so task 8.1's
    ``_truncate_quoted_literals`` has nothing to elide and the rendered text is
    the reported text.
    """
    return _outcome(
        STAGE_FRAME_BUILD,
        output_status="error",
        statement={
            "State": "AVAILABLE",
            "Output": {
                "Status": "error",
                "ErrorName": "AccessDeniedException",
                "ErrorValue": "An error occurred (403) when calling the HeadObject operation: Forbidden",
                "Traceback": ['  File "<stdin>", line 1, in <module>'],
            },
        },
    )


#: Every Glue-shaped result the runner can hand `aggregate_check_results`, paired
#: with whether its `data_quality_results` carries the score. Each entry is
#: `(label, builder, carries_score)`; the builder is called per example so no
#: dict is shared between them.
#:
#: Enumerated rather than generated because the SET is the interesting thing —
#: `test_the_absent_direction_covers_every_glue_shape_that_skips_validation`
#: reads this table directly, so a builder dropped from it fails a test instead
#: of silently shrinking the property's reach. Hypothesis draws over the table
#: and over the values inside each shape.
_GLUE_OUTCOMES: tuple[tuple[str, object, bool], ...] = (
    # ── validation RAN: the payload carries the score ───────────────────────
    (
        "success/full payload, full compliance",
        lambda: _glue_success(_full_validation_payload(100.0, [])),
        True,
    ),
    (
        "success/full payload, shortfall",
        lambda: _glue_success(_full_validation_payload(50.0, ["customer_name"])),
        True,
    ),
    (
        # `_parse_results` defaults the key to 0 rather than omitting it
        # (`code_interpreter_runner.py:626`), so a payload that forgot the score
        # still REPORTS one. Present in the table because the biconditional is
        # about the key, and a reader who assumed "no score in the payload → no
        # check" would be wrong about this one.
        "success/payload omitting schema_compliance",
        lambda: _glue_success({"record_count": 100, "completeness": 100.0}),
        True,
    ),
    # ── validation did NOT run: no payload, so no score ─────────────────────
    (
        "success/no VALIDATION marker printed",
        lambda: _glue_success(None),
        False,
    ),
    (
        # `_parse_results` returns early on an `error` key, leaving
        # `data_quality_results` empty (`:620-622`).
        "success/VALIDATION payload reporting an error",
        lambda: _glue_success({"error": "AnalysisException: cannot resolve 'order_key'"}),
        False,
    ),
    (
        "statement failure/Spark raised",
        lambda: statement_failure_results(
            _outcome(
                STAGE_TRANSFORM,
                output_status="error",
                statement={
                    "Output": {
                        "Status": "error",
                        "ErrorName": "AnalysisException",
                        "ErrorValue": "cannot resolve 'order_key'",
                        "Traceback": ['  File "<stdin>", line 1, in <module>'],
                    }
                },
            )
        ),
        False,
    ),
    (
        # R5.6 — a statement that reached a terminal state with no `Output`.
        "statement failure/statement cancelled",
        lambda: statement_failure_results(_outcome(STAGE_TRANSFORM, state="CANCELLED", output_status=None)),
        False,
    ),
    (
        # p42 R8.1, the THIRD harness reason. `aggregate_check_results:1222`.
        "harness/session capacity exhausted",
        lambda: session_capacity_results(
            ClientError(
                {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "too many sessions"}},
                "CreateSession",
            )
        ),
        False,
    ),
    (
        # p42 R7.3, the FOURTH harness reason. `aggregate_check_results:1257`.
        # The one the task names explicitly: it is not a width problem, so it
        # carries neither a field count nor a ceiling, and it renders its own
        # sentence — a shape the p33 examples above could not have exercised.
        "harness/test frame unbuildable",
        lambda: frame_build_failure_results(
            FrameBuildError("column 'created': declared type 'interval' has no Spark equivalent")
        ),
        False,
    ),
    (
        # p44 R2.1, the FIFTH harness reason — added by task 7.2 as an AMENDMENT
        # to the four-reason set below, not a relaxation of it (`LGR-60c4985-6`,
        # template / ratified). The first harness reason about the run's own
        # DATA: a session was created and billed, and what failed was reading the
        # object the frame was to be built from. Deliberately NOT a reuse of
        # `test_frame_unbuildable` above — different fact, different next step,
        # and its arm in `aggregate_check_results` names the object and the read
        # rather than the declared types.
        "harness/the run's source object could not be read",
        lambda: sample_read_failure_results(_denied_source_read(), _sample_frame_source()),
        False,
    ),
    (
        "harness/no source schema (width arm)",
        lambda: frame_refusal_results({}),
        False,
    ),
    (
        "harness/source schema past the 512-column ceiling (width arm)",
        lambda: frame_refusal_results(_wide_source_schema()),
        False,
    ),
    (
        "not executed/session creation failed",
        lambda: session_creation_failure_results(
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "CreateSession")
        ),
        False,
    ),
    (
        "not executed/lifecycle failure",
        lambda: lifecycle_failure_results(GlueSessionError("session reached state FAILED, from which READY is not")),
        False,
    ),
    (
        "not executed/client unavailable",
        lambda: not_executed_results("Glue client unavailable"),
        False,
    ),
)

#: The three `target_schema` states `aggregate_check_results` distinguishes
#: (`lambda_function.py:1063-1078`). All three are drawn, because the emission
#: gate is upstream of the branch between them and the biconditional must hold
#: for each: a dict and `TARGET_SCHEMA_UNSPECIFIED` report the computed score,
#: `None` replaces it with the non-verification `LGR-b2f79ef-2` specifies — but
#: all three emit exactly ONE entry, and only when validation ran.
_TARGET_SCHEMA_STATES: tuple[tuple[str, object], ...] = (
    ("a declared target schema", _PROP19_TARGET_SCHEMA),
    ("an empty declared target schema", {}),
    ("no target schema (requirement 2.6)", None),
    ("target schema unspecified", _mod.TARGET_SCHEMA_UNSPECIFIED),
)

#: Cheap compared with the handler-driven cases above — `aggregate_check_results`
#: is a pure derivation over its arguments and reaches no client — so the example
#: budget is raised rather than left at `_SETTINGS`' 20. 15 shapes × 4 target
#: schema states × the reviewer near-miss is 120 combinations (14 shapes until
#: p44 task 7.2 added the source-read specimen).
_PROP19_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _prop19_parsed_results(include_reviewer_schema_category: bool) -> dict:
    parsed = dict(_PROP19_PARSED_PASS)
    if include_reviewer_schema_category:
        parsed["test_categories"] = {_REVIEWER_SCHEMA_CATEGORY: {"passed": True}}
    return parsed


@_PROP19_SETTINGS
@given(
    outcome=st.sampled_from(_GLUE_OUTCOMES),
    target_schema_state=st.sampled_from(_TARGET_SCHEMA_STATES),
    include_reviewer_schema_category=st.booleans(),
)
def test_a_glue_schema_compliance_check_is_emitted_exactly_when_validation_ran(
    outcome, target_schema_state, include_reviewer_schema_category
):
    """Property 19, over Glue-shaped results. BOTH directions.

    A ``schema_compliance`` check is emitted **if and only if** the result's
    ``data_quality_results`` carries the score. Asserted as an equivalence rather
    than as two independent implications, so neither half can be satisfied by an
    implementation that ignores its antecedent.

    Validates: Requirement 12.4
    """
    label, build, carries_score = outcome
    schema_label, target_schema = target_schema_state
    execution_results = build()

    # Guard, not the property: `data_quality_results` must be PRESENT on every
    # Glue result, or "the payload carries the score" would be a claim about a
    # key that is not there and the gate would be reading `{}` for the wrong
    # reason. Full shape equivalence is Property 2's (task 8.2).
    assert set(execution_results) >= BASE_CONTRACT_KEYS, (
        f"{label}: the Glue result omits contract key(s) {sorted(BASE_CONTRACT_KEYS - set(execution_results))}"
    )

    quality = execution_results["data_quality_results"]
    payload_carries_score = SCHEMA_COMPLIANCE_KEY in quality
    assert payload_carries_score is carries_score, (
        f"{label}: the table says carries_score={carries_score}, but the shipped builder "
        f"produced data_quality_results={quality!r}. Either the builder changed or the "
        "table is now wrong — fix whichever is, rather than relaxing this"
    )

    _test_status, checks = _mod.aggregate_check_results(
        _prop19_parsed_results(include_reviewer_schema_category),
        execution_results,
        target_schema,
    )
    emitted = _named(checks, _mod.SCHEMA_COMPLIANCE_CHECK_NAME)

    # ── The biconditional ───────────────────────────────────────────────────
    assert (len(emitted) == 1) is payload_carries_score, (
        f"{label} with {schema_label}: data_quality_results "
        f"{'carries' if payload_carries_score else 'does not carry'} "
        f"{SCHEMA_COMPLIANCE_KEY!r}, but {len(emitted)} schema_compliance check(s) were emitted. "
        f"Checks were {[c['name'] for c in checks]}. "
        + (
            "A score reported for a run that produced no validation output is the vacuous "
            "score requirement 12.4 forbids (LGR-b2f79ef-1)."
            if not payload_carries_score
            else "Validation ran, so its result must be reported rather than dropped."
        )
    )
    # NOT implied by the biconditional above, and this is the gap: written as
    # `(len == 1) is carries`, the false side is satisfied by len 0 **or** len 2+.
    # So a duplicate emission on a payload-less result would pass the equivalence
    # and fail here. The reviewer's own `static_analysis:Schema Compliance` is
    # drawn on half the examples, which is the most likely source of such a
    # duplicate — it must be neither counted as this check nor suppress it.
    assert len(emitted) <= 1, (
        f"{label}: {len(emitted)} schema_compliance entries, expected at most one. "
        f"Checks were {[c['name'] for c in checks]}"
    )
    if include_reviewer_schema_category:
        assert _named(checks, f"static_analysis:{_REVIEWER_SCHEMA_CATEGORY}"), (
            "guard: the reviewer's own Schema Compliance category was not emitted, so this "
            "example did not actually exercise the near-miss"
        )


@_PROP19_SETTINGS
@given(
    outcome=st.sampled_from([entry for entry in _GLUE_OUTCOMES if not entry[2]]),
    target_schema_state=st.sampled_from(_TARGET_SCHEMA_STATES),
)
def test_no_glue_outcome_that_skipped_validation_reports_a_score(outcome, target_schema_state):
    """The absent half, isolated — including the two harness reasons p42 added.

    Restated on its own because it is the half that carries the weight, and
    because the failure it guards against is specific: a score reported for a run
    that never validated anything reads as a measurement. `LGR-b2f79ef-1`
    enumerates the paths — proxy refusal, ``execution_results is None``, execution
    failure, harness non-verification — and p42 adds a capacity refusal, a
    frame-build failure, a lifecycle failure and a validation-less success to the
    last two categories.

    Generalised over the reason string rather than enumerating reasons in
    assertions, so the fifth harness reason is covered by this test on the day it
    is added — the same posture task 3.7's Property 9 takes.

    Validates: Requirement 12.4
    """
    label, build, _carries_score = outcome
    schema_label, target_schema = target_schema_state

    _test_status, checks = _mod.aggregate_check_results(_PROP19_PARSED_PASS, build(), target_schema)

    assert not _named(checks, _mod.SCHEMA_COMPLIANCE_CHECK_NAME), (
        f"{label} with {schema_label} produced a schema_compliance entry although nothing "
        f"was validated. Checks were {[c['name'] for c in checks]}"
    )
    # …and the run still says something: silence about the schema is not silence
    # about the run. Every one of these shapes emits either an execution check or
    # a non-verification in its place (`lambda_function.py`'s total if/elif/else).
    assert len(checks) >= 2, (
        f"{label}: only {[c['name'] for c in checks]} emitted — dropping the score must not "
        "also drop the account of what happened"
    )


def test_the_absent_direction_covers_every_glue_shape_that_skips_validation():
    """The enumeration fence — without it the biconditional could hollow out.

    A property whose generator drew only score-carrying results would pass
    against an implementation that always reports a score, which is the defect
    this file was created for. Both tests above draw from
    :data:`_GLUE_OUTCOMES`, so this asserts what that table must contain:

    * both sides of the biconditional are represented at all;
    * every one of the FIVE harness reasons is present — the two width reasons
      p33 covered, plus ``session_capacity_exhausted`` (p42 R8.1),
      ``test_frame_unbuildable`` (p42 R7.3) and ``source_read_failed``
      (p44 R2.1), each of which has its own arm in ``aggregate_check_results``
      and its own copy;
    * a Glue success appears on BOTH sides, since that is the pairing a
      hand-written specimen could not produce.

    Deterministic rather than a property: what is being checked is the table, and
    a hypothesis draw over 300 examples still does not guarantee that all
    fifteen entries were seen.

    **p44 task 7.2 amended the reason set from four to five rather than relaxing
    it**, which is a code change with a reason attached rather than test upkeep:
    ``LGR-60c4985-6`` (**template / ratified**) is explicit that this repo keeps
    an assertion that encoded an earlier world and amends it, so the reversal
    stays legible as a reversal instead of reading as a regression, and
    ``LGR-5906ec6-2`` (**template / ratified**) is where the hold-as-assertion
    convention comes from. What the amendment did NOT do is weaken the shape: the
    equality is still an equality, because a subset form would stay green if a
    reason were later deleted.

    **This file's reach and its limit, stated so the next reader does not
    overread it.** Adding the specimen puts the fifth reason into
    ``st.sampled_from(_GLUE_OUTCOMES)`` for both properties above, so the SCORE
    emission gate — no ``schema_compliance`` entry, and still at least two checks
    — is now asserted for it. What is not asserted here is the TEXT of its
    ``aggregate_check_results`` arm: that arm executes on every draw, but a
    regression that dropped it would fall to the width ``else`` and render "the
    source schema declares None field(s)…" without failing anything in this file.
    ``test_not_verified_reason_labels_cross_language.py`` and the report tests own
    that sentence.
    """
    carrying = [label for label, _build, carries in _GLUE_OUTCOMES if carries]
    absent = [label for label, _build, carries in _GLUE_OUTCOMES if not carries]
    assert carrying, "no score-carrying Glue outcome is enumerated"
    assert len(absent) >= 9, (
        f"only {len(absent)} Glue outcomes skip validation; the absent direction is the half "
        "that can detect an unconditional score, so shrinking it weakens the property"
    )

    observed_reasons = set()
    for _label, build, _carries in _GLUE_OUTCOMES:
        result = build()
        if result.get("not_verified_by_harness"):
            observed_reasons.add(result.get("not_verified_reason"))

    expected_reasons = {
        "no_source_schema",
        "test_frame_truncated",
        NOT_VERIFIED_REASON_SESSION_CAPACITY,
        NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
        # p44 R2.1 (task 7.2). The set grew from four to FIVE; it was AMENDED and
        # the equality below was kept exact. Relaxing to a subset or a `>=` was
        # refused: a subset form stays green if a reason is later DELETED, which
        # is one of the two things this assertion exists to notice
        # (`LGR-60c4985-6`, template / ratified — amend the assertion, never
        # delete it, and keep the reversal legible as a reversal;
        # `LGR-5906ec6-2` is where the hold-as-assertion convention comes from).
        NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    }
    assert observed_reasons == expected_reasons, (
        f"harness reasons exercised: {sorted(observed_reasons)}; expected {sorted(expected_reasons)}. "
        "A reason with no specimen here is a reason whose emission gate is unasserted"
    )

    success_labels = [label for label, _build, _carries in _GLUE_OUTCOMES if label.startswith("success/")]
    success_carrying = [label for label, _build, carries in _GLUE_OUTCOMES if carries and label.startswith("success/")]
    success_absent = [
        label for label, _build, carries in _GLUE_OUTCOMES if not carries and label.startswith("success/")
    ]
    assert success_carrying and success_absent, (
        f"a Glue success must appear on both sides of the biconditional; got {success_labels}"
    )


@_PROP19_SETTINGS
@given(
    outcome=st.sampled_from([entry for entry in _GLUE_OUTCOMES if entry[2]]),
)
def test_a_glue_run_with_no_target_schema_reports_a_non_verification_not_a_score(outcome):
    """R12.4's rationale clause: validation ran, but there was nothing to compare to.

    The one case where an entry IS emitted and yet no score is reported. A Glue
    run against a pipeline with no target schema computes the vacuous 100 — an
    empty declared set has nothing to be short of — and
    ``aggregate_check_results`` replaces it with the ``verified: False`` +
    ``not_verified_reason`` encoding ``LGR-b2f79ef-2`` (**template / ratified**)
    settled, rather than reporting the number.

    Asserted for Glue shapes because the replacement is what stops "we could not
    check" reading as "we checked and it passed", and an executor change is the
    most plausible way to lose it by accident. ``is_not_verified_check`` is
    exercised as the discriminator so the partition stays the existing one — a
    reason, not a third state (C3).

    Validates: Requirement 12.4
    """
    label, build, _carries_score = outcome

    _test_status, checks = _mod.aggregate_check_results(_PROP19_PARSED_PASS, build(), None)
    emitted = _named(checks, _mod.SCHEMA_COMPLIANCE_CHECK_NAME)
    # Asserted rather than indexed: dropping the entry entirely is a DIFFERENT
    # failure from reporting the vacuous score, and `[0]` on an empty list would
    # report it as an IndexError with nothing about which one happened.
    assert len(emitted) == 1, (
        f"{label}: validation ran, so exactly one schema_compliance entry is due even with no "
        f"target schema — 'we could not check' must not be silence (LGR-b2f79ef-2). "
        f"Got {len(emitted)}; checks were {[c['name'] for c in checks]}"
    )
    entry = emitted[0]

    assert entry.get("verified") is False, f"{label}: no verified:False discriminator on {entry}"
    assert entry.get("not_verified_reason") == _mod.NO_TARGET_SCHEMA_REASON, (
        f"{label}: expected reason {_mod.NO_TARGET_SCHEMA_REASON!r}, got {entry.get('not_verified_reason')!r}"
    )
    assert entry.get("passed") is not True, f"{label}: an unverifiable schema-compliance check reads as a pass"
    assert _mod.is_not_verified_check(entry), (
        f"{label}: is_not_verified_check does not recognise the entry, so it would reach the "
        "verified-check partition and could fail the run (C3/C4)"
    )
    # No score in the text, either — the vacuous 100 must not be rendered
    # alongside the words that say nothing was checked.
    assert "100" not in entry.get("detail", ""), (
        f"{label}: the detail renders the vacuous score: {entry.get('detail')!r}"
    )


def test_a_glue_success_result_is_contract_shaped_so_the_payload_claim_is_about_contents():
    """The score-carrying Glue success really is a Test_Result_Contract success.

    Without this, "the payload carries the score" could be true of a malformed
    dict and the biconditional above would be measuring the wrong thing.

    ``conversion_report`` is excluded by SUBSTRING — the same three lines as
    ``test_result_shape_equivalence_prop.py:259`` and
    ``test_shadow_comparison.py:83``, for the same reason: the Glue path returns
    ``None`` for that key on **every** outcome (task 8.1's decision — the key
    means "None when conversion failed or never ran", and this runner runs no
    conversion by design), while ``SUCCESS`` declares it ``dict``. The exclusion
    is wider than that sentence, because it also drops a ``missing key`` report
    for the same key; the key's PRESENCE is asserted separately by
    :data:`BASE_CONTRACT_KEYS` above, which is where that guarantee lives.
    """
    result = _glue_success(_full_validation_payload(100.0, []))
    violations = [v for v in shape_violations(result, SUCCESS) if "conversion_report" not in v]
    assert not violations, f"Glue success departs from the declared SUCCESS shape: {violations}"
    assert result["conversion_report"] is None, (
        "the documented asymmetry is that the Glue path returns conversion_report=None on every "
        f"outcome; got {result['conversion_report']!r}"
    )
