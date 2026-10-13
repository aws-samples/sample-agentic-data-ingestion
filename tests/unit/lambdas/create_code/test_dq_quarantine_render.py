# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R11.1 — generate_code wires the DQ quarantine bucket into the render.

create_code must pass ``quarantine_bucket`` (the generated-code bucket) into
the Jinja render context so the s3_tables template's DQ block can persist
failing rows to ``s3://<bucket>/quarantine/<run_id>/`` instead of silently
dropping them. Without this variable the rendered job would refuse to filter
failing rows (RuntimeError) rather than lose them.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import jinja2
import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Stub heavy dependencies so the module imports cleanly in unit context.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _s3_tables_pipeline_metadata() -> dict:
    return {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": {
            "type": "s3_tables",
            "bucket_name": "my-table-bucket",
            "namespace": "analytics",
            "table_name": "customers",
            "write_mode": "append",
            "source_format": "json",
        },
        "source_schema": {"properties": {}},
    }


# Non-empty mappings → _generate_dqdl_rules produces a ruleset → DQ block renders.
_MAPPING_RESULT = {
    "field_mappings": [
        {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9},
    ]
}


def test_generate_code_renders_quarantine_with_generated_code_bucket():
    """The rendered s3_tables job quarantines to the generated-code bucket."""
    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        code = _mod.generate_code(
            pipeline_metadata=_s3_tables_pipeline_metadata(),
            mapping_result=_MAPPING_RESULT,
        )

    # The quarantine bucket baked into the job is the generated-code bucket.
    assert f'_dq_quarantine_bucket = "{_mod.GENERATED_CODE_BUCKET}"' in code
    # The quarantine prefix is keyed by run_id.
    assert 'f"s3://{_dq_quarantine_bucket}/quarantine/{_dq_quarantine_run_id}/"' in code
    # Counters are wired to metrics + state record.
    assert '"MetricName": "RowsQuarantined"' in code
    assert '"rows_quarantined": _dq_rows_quarantined,' in code


def test_generate_code_passes_quarantine_bucket_to_render_context():
    """quarantine_bucket is present in the Jinja render kwargs."""
    captured = {}
    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    with (
        patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
        patch.object(jinja2.Template, "render", _capture_render),
    ):
        _mod.generate_code(
            pipeline_metadata=_s3_tables_pipeline_metadata(),
            mapping_result=_MAPPING_RESULT,
        )

    assert captured.get("quarantine_bucket") == _mod.GENERATED_CODE_BUCKET
    assert captured.get("quarantine_bucket"), "quarantine_bucket must be non-empty"


# ── p37 R2.6 — the two call sites of _generate_dqdl_rules agree ──────
#
# `_generate_dqdl_rules` has exactly two real call sites:
#
#   1. `code_assembly.generate_code` — renders `dqdl_rules` into the Glue job
#      the run actually executes.
#   2. `create_code/lambda_function.py:622` — recomputes the ruleset so the
#      code approval payload "shows the exact ruleset the job enforces".
#
# R2.6 says those two counts must be equal. That is what makes p37 do-NOT 8
# mechanical: C7's skip had to land INSIDE the function, because a guard added
# at either call site would break this by construction. These assertions fail
# if a future change re-introduces such a guard.


def _rule_count(dqdl: str) -> int:
    """Number of rules in a DQDL ruleset string.

    Bracket-scan rather than a regex, matching the shape
    `shared/quality_verdict.py::count_dqdl_rules` uses.
    """
    if not dqdl:
        return 0
    inner = dqdl[dqdl.index("[") + 1 : dqdl.rindex("]")]
    return len([part for part in inner.split(",\n") if part.strip()])


def _rendered_dqdl_rules(pipeline_metadata: dict, mapping_result: dict) -> str:
    """Call site 1: the ruleset `generate_code` bakes into the rendered job."""
    captured = {}
    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    with (
        patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
        patch.object(jinja2.Template, "render", _capture_render),
    ):
        _mod.generate_code(pipeline_metadata=pipeline_metadata, mapping_result=mapping_result)

    return captured["dqdl_rules"]


def _approval_payload_dqdl_rules(pipeline_metadata: dict, mapping_result: dict) -> str:
    """Call site 2: the recomputation at `create_code/lambda_function.py:622`.

    Reproduced as the expression that site evaluates — same function, same two
    arguments — rather than by invoking `lambda_handler`, which would need the
    DynamoDB/Bedrock surroundings that have nothing to do with R2.6.
    """
    return _mod._generate_dqdl_rules(
        _mod._extract_field_mappings(mapping_result),
        unique_hint_fields=_mod._uniqueness_hint_fields(pipeline_metadata["target_config"]),
    )


def _with_merge_key(merge_key: str) -> dict:
    pm = _s3_tables_pipeline_metadata()
    pm["target_config"] = {**pm["target_config"], "write_mode": "merge", "merge_key": merge_key}
    return pm


_SOURCED = [
    {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9},
    {"source_field": "mail", "target_field": "email", "confidence": 0.9},
    {"source_field": "st", "target_field": "state", "confidence": 0.8},
]
_UNSOURCED_PHONE = {"target_field": "phone", "source_field": None, "confidence": 0.2}

CALL_SITE_CASES = [
    pytest.param(
        _s3_tables_pipeline_metadata(),
        {"field_mappings": _SOURCED},
        id="all-sourced-legacy-field_mappings",
    ),
    pytest.param(
        _s3_tables_pipeline_metadata(),
        {"field_mappings": [*_SOURCED, _UNSOURCED_PHONE]},
        id="one-unsourced-legacy-field_mappings",
    ),
    pytest.param(
        _s3_tables_pipeline_metadata(),
        {"mapping_config": {"field_mappings": [*_SOURCED, _UNSOURCED_PHONE]}},
        id="one-unsourced-nested-mapping_config",
    ),
    pytest.param(
        _s3_tables_pipeline_metadata(),
        {"mapping_result": json.dumps({"mappings": [*_SOURCED, _UNSOURCED_PHONE]})},
        id="one-unsourced-mapping_result-json-string",
    ),
    pytest.param(
        _with_merge_key("state"),
        {"field_mappings": [*_SOURCED, _UNSOURCED_PHONE]},
        id="merge-key-hint-plus-one-unsourced",
    ),
    pytest.param(
        _with_merge_key("phone"),
        {"field_mappings": [*_SOURCED, _UNSOURCED_PHONE]},
        id="merge-key-hint-names-the-unsourced-field",
    ),
    pytest.param(
        _s3_tables_pipeline_metadata(),
        {"field_mappings": [_UNSOURCED_PHONE, {"target_field": "fax", "source_field": "  "}]},
        id="every-entry-unsourced",
    ),
]


@pytest.mark.parametrize("pipeline_metadata,mapping_result", CALL_SITE_CASES)
def test_both_call_sites_produce_the_same_rule_count(pipeline_metadata, mapping_result):
    """R2.6: the approval payload's rule count equals the rendered job's."""
    rendered = _rendered_dqdl_rules(pipeline_metadata, mapping_result)
    payload = _approval_payload_dqdl_rules(pipeline_metadata, mapping_result)

    assert _rule_count(payload) == _rule_count(rendered)
    # Stronger than the requirement, and true because the function is
    # deterministic over these inputs: the rulesets are byte-identical.
    assert payload == rendered


@pytest.mark.parametrize("pipeline_metadata,mapping_result", CALL_SITE_CASES)
def test_neither_call_site_emits_a_rule_for_the_unsourced_field(pipeline_metadata, mapping_result):
    """R2.1/R2.2 hold at both sites — the skip is in the function, not a caller."""
    rendered = _rendered_dqdl_rules(pipeline_metadata, mapping_result)
    payload = _approval_payload_dqdl_rules(pipeline_metadata, mapping_result)

    for dqdl in (rendered, payload):
        assert 'Completeness "phone"' not in dqdl, dqdl
        assert 'Uniqueness "phone"' not in dqdl, dqdl
        assert 'Completeness "fax"' not in dqdl, dqdl


def test_the_unsourced_field_drops_exactly_one_rule_at_both_sites():
    """The count difference is attributable, not just equal on both sides."""
    pm = _s3_tables_pipeline_metadata()
    with_unsourced = {"field_mappings": [*_SOURCED, _UNSOURCED_PHONE]}
    sourced_only = {"field_mappings": _SOURCED}

    assert _rule_count(_rendered_dqdl_rules(pm, with_unsourced)) == _rule_count(_rendered_dqdl_rules(pm, sourced_only))
    assert _rule_count(_approval_payload_dqdl_rules(pm, with_unsourced)) == _rule_count(
        _approval_payload_dqdl_rules(pm, sourced_only)
    )
    # And sourcing `phone` adds its Completeness rule back at both sites.
    sourced_phone = {"field_mappings": [*_SOURCED, {**_UNSOURCED_PHONE, "source_field": "mobile"}]}
    assert (
        _rule_count(_rendered_dqdl_rules(pm, sourced_phone)) == _rule_count(_rendered_dqdl_rules(pm, sourced_only)) + 1
    )
    assert 'Completeness "phone" >= 0.95' in _approval_payload_dqdl_rules(pm, sourced_phone)
