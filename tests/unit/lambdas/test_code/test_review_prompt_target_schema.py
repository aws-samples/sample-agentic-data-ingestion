# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The review prompt renders the schema the run is measured against (p33 4.5).

Requirement 2.4: "WHEN the code-review prompt is assembled THEN the system
SHALL render the pipeline's actual target schema, so a Schema Compliance verdict
is formed against the schema the run is measured against."

Why this file exists at all
---------------------------
Task 4.5's effect on the RESOLVED path did land as a consequence of task 4.3 —
4.3 re-sourced the read and the prompt renders what it resolved. But before this
file, **nothing in `tests/` asserted anything about the review prompt's
target-schema rendering** (verified: no test referenced ``Target Schema`` or
``target_schema_for_run``). So the behaviour requirement 2.4 asks for was
unpinned in both directions: a later change could re-point the prompt at the
mappings record, or at an empty dict, and every test would stay green. These are
the fence.

The live counterexample this is built from
------------------------------------------
Run ``run-2026-08-05-14-29-05-cc73973d`` (execution
``…:execution:agentic-data-ingestion-pipeline:file-p32ver_renC_csv-20260805T142902-51f5323dd3ac``):
the reviewer raised a CRITICAL issue that six ``_out`` renames would mismatch the
target schema, while this same prompt showed it that schema as ``{}``. A verdict
formed against a schema the reviewer could not see is what 2.4 removes.

The schema-less half is the part that needed code
-------------------------------------------------
4.3 pointed the prompt at ``target_schema_for_run = target_schema or {}``, which
keeps a schema-less pipeline from rendering ``null``. That is more honest than
``null`` but still not honest: ``{}`` is a legitimate JSON Schema meaning "no
constraints", the system prompt (``test_analysis.j2:23``/``:38``) makes a
``"Schema Compliance"`` verdict MANDATORY with no not-applicable value, and the
run record on that same path now says something different in text (task 4.4's
``no_target_schema`` non-verification). So the prompt states the absence
explicitly, and ``test_a_schema_less_pipeline_says_so_in_text`` pins it.

What is real here and what is simulated
---------------------------------------
Real: ``lambda_handler``, ``get_pipeline_metadata``, ``resolve_target_schema``
(including its S3 read), and the prompt assembly. The prompt string asserted on
is the one the handler actually built and actually passed to the review agent.

Simulated: the review agent (a ``MagicMock`` returning a fixed verdict, which is
also how the prompt is captured), DynamoDB, S3 bodies, and the Code Interpreter
runner. None of them participate in the rendering under test.

Validates: Requirements 2.4, 2.6
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The resolver builds its OWN lazily-cached S3 client, so `patch.object(_mod,
# "s3_client", ...)` is not the seam for the target-schema read — `_tsr._s3_client`
# is. Imported HERE, before the `patch.dict("sys.modules", ...)` block below,
# deliberately: that context manager restores the dict on exit and would drop a
# module first imported inside it, so importing after the block would bind a
# SECOND instance whose `_s3_client` is not the one `lambda_function` calls.
# Same convention as `test_schema_compliance_gate_prop.py`.
import target_schema_resolution as _tsr

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


_RUN_ID = "run-1"
_PIPELINE_ID = "pipe-1"
_CODE_BUCKET = "code-bucket"
_CODE_KEY = "code.py"
_SCHEMA_BUCKET = "adi-target-schemas-123456789012-us-east-1"
_SCHEMA_KEY = "pipelines/pipe-1/target_schema.json"

_CODE_GEN_ITEM = {
    "run_id": _RUN_ID,
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": f"s3://{_CODE_BUCKET}/{_CODE_KEY}"},
}

#: A pipeline record naming the canonical pair — requirement 2.1's resolved case.
_PIPELINE_WITH_SCHEMA = {
    "pipeline_id": _PIPELINE_ID,
    "target_schema_bucket": _SCHEMA_BUCKET,
    "target_schema_s3_key": _SCHEMA_KEY,
}

#: A pipeline record carrying neither attribute — requirement 2.6's legitimate
#: no-schema state, NOT a lookup failure.
_PIPELINE_WITHOUT_SCHEMA = {"pipeline_id": _PIPELINE_ID, "pipeline_name": "no-target-schema"}

#: The six `_out` renames from the live counterexample, as a target schema that
#: declares the columns the transform was supposed to produce.
_TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_id_out": {"type": "string"},
        "order_id_out": {"type": "string"},
        "amount_out": {"type": "number"},
        "currency_out": {"type": "string"},
        "status_out": {"type": "string"},
        "created_at_out": {"type": "string"},
    },
}

_REVIEW_ALL_PASS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {
            "Correctness": "pass",
            "Schema Compliance": "pass",
            "Error Handling": "pass",
            "AWS Best Practices": "pass",
            "Security": "pass",
            "Performance": "pass",
        },
    }
)

_GENERATED_CODE = "# === AI-Generated Transform Block ===\ndf_transformed = df\n# === End ===\n"

_EXEC_OK: dict = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "output_records": 100,
    "stderr": "",
    "data_quality_results": {},
}


def _capture_prompt(pipeline_record: dict, target_schema_object: dict | None) -> str:
    """Run ``lambda_handler`` and return the prompt it passed to the review agent.

    The agent is the capture point rather than a separate seam: the handler
    builds the prompt inline and hands it straight to ``agent(prompt_text)``, so
    the recorded call argument IS the shipped prompt, with no reconstruction.
    """
    runs_table = MagicMock()

    def _runs_get_item(Key):
        if Key["step_name"] == "create_code":
            return {"Item": _CODE_GEN_ITEM}
        return {}

    runs_table.get_item.side_effect = _runs_get_item

    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}

    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(pipeline_record)} if pipeline_record else {}
    fake_dynamodb = MagicMock()
    fake_dynamodb.Table.return_value = pipelines_table

    s3 = MagicMock()

    def _get_object(Bucket, Key, **_kw):
        if (Bucket, Key) == (_CODE_BUCKET, _CODE_KEY):
            return {"Body": MagicMock(read=MagicMock(return_value=_GENERATED_CODE.encode()))}
        if (Bucket, Key) == (_SCHEMA_BUCKET, _SCHEMA_KEY) and target_schema_object is not None:
            return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(target_schema_object).encode()))}
        raise AssertionError(f"unexpected S3 read: s3://{Bucket}/{Key}")

    s3.get_object.side_effect = _get_object

    agent = MagicMock(return_value=_REVIEW_ALL_PASS)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = _EXEC_OK

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_tsr, "_s3_client", lambda: s3),
        patch.object(_mod, "dynamodb", fake_dynamodb),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        _mod.lambda_handler({"run_id": _RUN_ID, "pipeline_id": _PIPELINE_ID}, None)

    assert agent.call_args_list, "the review agent was never invoked, so no prompt was captured"
    # The FIRST invocation is the review; a second would be the reparse retry,
    # which is not what this file is about.
    return agent.call_args_list[0].args[0]


# ---------------------------------------------------------------------------
# Requirement 2.4 — the resolved case
# ---------------------------------------------------------------------------


def test_the_resolved_target_schema_reaches_the_reviewer():
    """The declared field names are IN the prompt, not a placeholder for them.

    This is the assertion the live counterexample would have failed: the
    reviewer was asked to judge Schema Compliance while the prompt carried
    ``{}``. Naming each field individually rather than comparing whole blobs
    keeps the test about "the reviewer can see the schema" rather than about
    ``json.dumps`` formatting.
    """
    prompt = _capture_prompt(_PIPELINE_WITH_SCHEMA, _TARGET_SCHEMA)

    for field in _TARGET_SCHEMA["properties"]:
        assert field in prompt, f"the reviewer cannot see declared target field {field!r}"


def test_the_rendered_schema_is_the_resolved_object_not_an_empty_one():
    """The exact resolved JSON is present, and the vacuous ``{}`` is not.

    The `{}` guard matters on its own: the defect requirement 2.4 removes was
    not "the schema was formatted oddly", it was "the schema rendered as an
    empty object while the reviewer was asked to judge against it".
    """
    prompt = _capture_prompt(_PIPELINE_WITH_SCHEMA, _TARGET_SCHEMA)

    assert f"Target Schema:\n{json.dumps(_TARGET_SCHEMA, indent=2)}" in prompt
    assert "Target Schema:\n{}" not in prompt
    assert "Target Schema:\nnull" not in prompt


# ---------------------------------------------------------------------------
# Requirement 2.6 — the schema-less case
# ---------------------------------------------------------------------------


def test_a_schema_less_pipeline_says_so_in_text():
    """No target schema is STATED, not rendered as ``{}`` or ``null``.

    ``{}`` would be indistinguishable from a schema that declares no columns —
    a legitimate JSON Schema meaning "no constraints" — and the system prompt
    (``test_analysis.j2:23``/``:38``) requires a ``"Schema Compliance"`` verdict
    with no not-applicable option, so the reviewer would have to guess and then
    assert the guess. The text also keeps the prompt coherent with the run
    record, which carries task 4.4's ``no_target_schema`` non-verification for
    this same input.
    """
    prompt = _capture_prompt(_PIPELINE_WITHOUT_SCHEMA, None)

    assert "Target Schema: NOT AVAILABLE" in prompt
    assert "no target schema on record" in prompt
    # The two renderings this replaces, both of which invite a fabricated verdict.
    assert "Target Schema:\n{}" not in prompt
    assert "Target Schema:\nnull" not in prompt


def test_the_schema_less_prompt_tells_the_reviewer_not_to_fabricate_the_finding():
    """The instruction is present, because the mandatory verdict needs one.

    Without it the reviewer still owes a pass/fail for a category it has no
    input for. This is the prompt-side counterpart of the run record's "This is
    not a passing score — nothing was checked."
    """
    prompt = _capture_prompt(_PIPELINE_WITHOUT_SCHEMA, None)

    assert "Do NOT report a Schema Compliance issue about a schema you were not given" in prompt


# ---------------------------------------------------------------------------
# Nothing else about the prompt moved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pipeline_record", "schema_object"),
    [
        pytest.param(_PIPELINE_WITH_SCHEMA, _TARGET_SCHEMA, id="schema-resolved"),
        pytest.param(_PIPELINE_WITHOUT_SCHEMA, None, id="schema-less"),
    ],
)
def test_the_rest_of_the_prompt_is_unchanged(pipeline_record, schema_object):
    """Every other element of the prompt survives, on BOTH schema states.

    Task 4.5 is a change to one section. P28 R13.1's JSON-verdict instruction,
    the fenced code block, the conciseness directives and the source-data URI
    are load-bearing for other requirements and are pinned here so a future
    edit to the target-schema section cannot take them with it.
    """
    prompt = _capture_prompt(pipeline_record, schema_object)

    assert "Return ONLY the JSON verdict object specified in the system prompt" in prompt
    assert "no markdown fences, no prose" in prompt
    assert "Be concise — 1-2 sentences per issue description" in prompt
    assert "No code blocks, no suggestions, no rewrites." in prompt
    assert f"Code:\n```python\n{_GENERATED_CODE}\n```" in prompt
    assert "Source Data URI:" in prompt
    assert prompt.endswith("Return ONLY the JSON object.")
