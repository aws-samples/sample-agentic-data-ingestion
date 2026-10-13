# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R11.2 — DQDL Uniqueness inference: word-boundary + schema hints.

`_generate_dqdl_rules` must never add Uniqueness rules by substring match
(the old ``"key" in field_lower`` check gave "monkey" a Uniqueness rule).
Inference uses whole-name (re.fullmatch) matching against a key-like
allowlist, or schema hints (the explicitly configured merge_key from
target_config — the only primary-key-style metadata in the system).
The inferred rules are surfaced in the code approval payload so approvers
see the constraints the generated job will enforce.
"""

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


def _mappings(*target_fields: str) -> list[dict]:
    return [{"source_field": f"src_{name}", "target_field": name, "confidence": 0.9} for name in target_fields]


def _uniqueness_rules(dqdl: str) -> list[str]:
    return [line.strip().rstrip(",") for line in dqdl.splitlines() if "Uniqueness" in line]


# ── Word-boundary inference (no substring matching) ──────────────────


def test_monkey_does_not_get_uniqueness_rule():
    """The review's concrete bug: "monkey" contains "key" but is not a key."""
    dqdl = _mod._generate_dqdl_rules(_mappings("monkey"))
    assert 'Uniqueness "monkey"' not in dqdl
    # Completeness still applies to every mapped field.
    assert 'Completeness "monkey" >= 0.95' in dqdl


@pytest.mark.parametrize("name", ["customer_id", "id", "pk_customer", "email", "uuid", "guid"])
def test_key_like_names_get_uniqueness(name):
    dqdl = _mod._generate_dqdl_rules(_mappings(name))
    assert f'Uniqueness "{name}" >= 0.95' in dqdl


@pytest.mark.parametrize(
    "name",
    [
        "monkey",  # old bug: "key" substring
        "wide_identifier",  # old bug: "_id" substring
        "id_number",  # old bug: "id_" substring
        "email_address",  # old bug: "email" substring
        "key",  # bare "key" is not in the whole-name allowlist
        "video",  # contains "id" but is not an id
    ],
)
def test_substring_lookalikes_do_not_get_uniqueness(name):
    dqdl = _mod._generate_dqdl_rules(_mappings(name))
    assert "Uniqueness" not in dqdl, f"{name!r} must not be inferred as unique: {dqdl}"


def test_uniqueness_matching_is_case_insensitive():
    dqdl = _mod._generate_dqdl_rules(_mappings("Customer_ID"))
    assert 'Uniqueness "Customer_ID" >= 0.95' in dqdl


def test_empty_mappings_produce_empty_ruleset():
    assert _mod._generate_dqdl_rules([]) == ""


# ── Schema hints (merge_key) ──────────────────────────────────────────


def test_merge_key_hint_produces_uniqueness_regardless_of_name_shape():
    """A configured merge_key is the record identity — uniqueness by definition."""
    hints = _mod._uniqueness_hint_fields({"merge_key": "customer_number"})
    dqdl = _mod._generate_dqdl_rules(_mappings("customer_number", "monkey"), unique_hint_fields=hints)
    assert 'Uniqueness "customer_number" >= 0.95' in dqdl
    assert 'Uniqueness "monkey"' not in dqdl


def test_merge_key_hint_not_duplicated_when_name_also_matches():
    hints = _mod._uniqueness_hint_fields({"merge_key": "customer_id"})
    dqdl = _mod._generate_dqdl_rules(_mappings("customer_id"), unique_hint_fields=hints)
    assert _uniqueness_rules(dqdl) == ['Uniqueness "customer_id" >= 0.95']


def test_hint_for_unmapped_field_produces_no_rule():
    """A DQDL rule on a column absent from the data would error the evaluation."""
    hints = _mod._uniqueness_hint_fields({"merge_key": "not_a_mapped_field"})
    dqdl = _mod._generate_dqdl_rules(_mappings("monkey"), unique_hint_fields=hints)
    assert "Uniqueness" not in dqdl


def test_hint_matches_case_insensitively():
    hints = _mod._uniqueness_hint_fields({"merge_key": "Customer_Number"})
    dqdl = _mod._generate_dqdl_rules(_mappings("customer_number"), unique_hint_fields=hints)
    assert 'Uniqueness "customer_number" >= 0.95' in dqdl


@pytest.mark.parametrize(
    "target_config",
    [
        {},  # no merge_key configured
        {"merge_key": ""},  # empty
        {"merge_key": "   "},  # whitespace-only
        {"merge_key": None},  # explicit null
        None,  # no target_config at all
    ],
)
def test_absent_merge_key_yields_no_hints(target_config):
    """Only an EXPLICITLY configured merge_key counts — generate_code's
    fabricated template-var default ("id") must not manufacture a hint."""
    assert _mod._uniqueness_hint_fields(target_config) == set()


# ── generate_code integration: hint flows into the rendered ruleset ──


def _s3_tables_pipeline_metadata(**target_overrides) -> dict:
    target_config = {
        "type": "s3_tables",
        "bucket_name": "my-table-bucket",
        "namespace": "analytics",
        "table_name": "customers",
        "write_mode": "append",
        "source_format": "json",
    }
    target_config.update(target_overrides)
    return {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": target_config,
        "source_schema": {"properties": {}},
    }


def test_generate_code_bakes_merge_key_uniqueness_into_render():
    """merge_key hint reaches the dqdl_rules template var (shape unchanged: str)."""
    captured = {}
    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    mapping_result = {"field_mappings": _mappings("customer_number", "monkey")}
    with (
        patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
        patch.object(jinja2.Template, "render", _capture_render),
    ):
        _mod.generate_code(
            pipeline_metadata=_s3_tables_pipeline_metadata(merge_key="customer_number"),
            mapping_result=mapping_result,
        )

    dqdl = captured.get("dqdl_rules")
    assert isinstance(dqdl, str)
    assert 'Uniqueness "customer_number" >= 0.95' in dqdl
    assert 'Uniqueness "monkey"' not in dqdl


# ── Approval payload visibility ───────────────────────────────────────


def test_rules_appear_in_code_approval_payload():
    """Approvers reviewing generated code see the inferred DQDL constraints."""
    rules = _mod._generate_dqdl_rules(_mappings("customer_id", "monkey"))
    payload = _mod.create_approval_payload(
        code_version_id="cv-1",
        generated_code="print('job')",
        code_quality_metrics={"quality_score": 80},
        agent_reasoning="reasoning",
        template_used="glue_job_s3_tables.py.j2",
        code_s3_uri="s3://bucket/code.py",
        dqdl_rules=rules,
    )
    assert payload["approval_type"] == "code_generation"
    assert payload["dqdl_rules"] == rules
    assert 'Uniqueness "customer_id" >= 0.95' in payload["dqdl_rules"]
    assert 'Uniqueness "monkey"' not in payload["dqdl_rules"]


def test_approval_payload_defaults_to_empty_rules():
    """Fallback full-AI path carries no rules — the payload must not fabricate any."""
    payload = _mod.create_approval_payload(
        code_version_id="cv-1",
        generated_code="print('job')",
        code_quality_metrics={"quality_score": 80},
        agent_reasoning="reasoning",
        template_used="fallback_full_ai_v1",
        code_s3_uri="s3://bucket/code.py",
    )
    assert payload["dqdl_rules"] == ""


# ── p37 R2.1 / R2.2 — an Unsourced_Target_Field gets no rule ─────────
#
# C7. A mapping entry carrying a `target_field` and no usable `source_field`
# can only be satisfied by a constant: the AI transform sets the column to
# `lit(None)` because the prompt told it to produce the column. So
# `Completeness "<field>" >= 0.95` is a rule the SAME code generation makes
# impossible to satisfy — every row fails it and the inline DQ split
# quarantines the whole batch.
#
# Observed on pipeline newtest-ab2a08a0, run run-2026-08-09-15-38-19-5c5f5f8a:
# target field `phone` had no source, 10/11 rules passed, 5 rows quarantined,
# 0 rows written, and the run still reported SUCCEEDED.
#
# "Usable" is an explicit `isinstance(str)` + non-empty-after-`.strip()` test,
# NOT truthiness on an arbitrary object — a mapping record is parsed JSON, so
# the value can be absent, None, "", "   " or a non-string.
#
# The ordering constraint (R2.7 — a hostile `target_field` still raises even
# when unsourced) is asserted in `test_identifier_validation_sinks.py`, next to
# the other assertions about that sink.

# Every unusable `source_field` shape, as its own case so a regression names
# the shape it regressed on. Two non-string cases are included deliberately:
# a truthy one (`123`) is what a truthiness test would wave through.
UNUSABLE_SOURCE_FIELD_SHAPES = [
    pytest.param({}, id="source_field-absent"),
    pytest.param({"source_field": None}, id="source_field-None"),
    pytest.param({"source_field": ""}, id="source_field-empty-string"),
    pytest.param({"source_field": "   "}, id="source_field-whitespace-only"),
    pytest.param({"source_field": 123}, id="source_field-non-string-truthy"),
    pytest.param({"source_field": ["mobile"]}, id="source_field-non-string-list"),
]


@pytest.mark.parametrize("unsourced", UNUSABLE_SOURCE_FIELD_SHAPES)
def test_unsourced_target_field_gets_no_completeness_rule(unsourced):
    """R2.1: no Completeness rule for a target field with no usable source."""
    dqdl = _mod._generate_dqdl_rules([{"target_field": "phone", "confidence": 0.9, **unsourced}])
    assert 'Completeness "phone"' not in dqdl, dqdl


@pytest.mark.parametrize("unsourced", UNUSABLE_SOURCE_FIELD_SHAPES)
def test_unsourced_target_field_gets_no_uniqueness_rule(unsourced):
    """R2.2: not even for a name the key-like allowlist would otherwise match."""
    dqdl = _mod._generate_dqdl_rules([{"target_field": "customer_id", "confidence": 0.9, **unsourced}])
    assert "Uniqueness" not in dqdl, dqdl
    assert "Completeness" not in dqdl, dqdl


@pytest.mark.parametrize("unsourced", UNUSABLE_SOURCE_FIELD_SHAPES)
def test_unsourced_target_field_gets_no_uniqueness_rule_from_a_merge_key_hint(unsourced):
    """R2.2: the merge_key hint wins over name shape — but not over no source.

    ``_uniqueness_hint_fields`` makes a configured merge_key a uniqueness hint
    "regardless of its name shape". An unsourced field is the one case where it
    must not produce a rule: the hint says the column is the record identity,
    the missing source says the column will be a constant null.
    """
    hints = _mod._uniqueness_hint_fields({"merge_key": "customer_number"})
    dqdl = _mod._generate_dqdl_rules(
        [{"target_field": "customer_number", "confidence": 0.9, **unsourced}],
        unique_hint_fields=hints,
    )
    assert "Uniqueness" not in dqdl, dqdl
    assert "Completeness" not in dqdl, dqdl


def test_sourced_siblings_still_get_rules_when_one_entry_is_unsourced():
    """The skip is per-entry: it must not suppress the rest of the ruleset."""
    mappings = [
        *_mappings("cust_id", "email"),
        {"target_field": "phone", "source_field": None, "confidence": 0.3},
    ]
    dqdl = _mod._generate_dqdl_rules(mappings)
    assert 'Completeness "cust_id" >= 0.95' in dqdl
    assert 'Completeness "email" >= 0.95' in dqdl
    assert 'Uniqueness "cust_id" >= 0.95' in dqdl
    assert 'Uniqueness "email" >= 0.95' in dqdl
    assert "phone" not in dqdl, dqdl
    assert "RowCount > 0" in dqdl


def test_observed_run_ruleset_drops_the_unsatisfiable_rule_and_keeps_the_rest():
    """The live defect's exact mapping shape, and the exact rule arithmetic.

    Eight target fields, one of them (`phone`) unsourced. Before C7 this
    produced 1 RowCount + 8 Completeness + 2 Uniqueness = the 11 rules the run
    logged as "10/11 rules passed". After C7 it is 1 + 7 + 2 = 10, and the one
    rule that disappeared is the one no row could satisfy.
    """
    mappings = [
        *_mappings("cust_id", "fname", "lname", "email", "created_date", "state", "premium_member"),
        {"target_field": "phone", "source_field": "", "confidence": 0.2},
    ]
    dqdl = _mod._generate_dqdl_rules(mappings)

    completeness = [line for line in dqdl.splitlines() if "Completeness" in line]
    uniqueness = _uniqueness_rules(dqdl)
    assert len(completeness) == 7
    # cust_id matches `.*_id`, email matches `email`; the other five do not.
    assert len(uniqueness) == 2
    assert 'Completeness "phone" >= 0.95' not in dqdl
    assert "phone" not in dqdl, dqdl


def test_all_entries_unsourced_leaves_the_row_count_rule_only():
    """Pinned rather than assumed: this is NOT the empty-mappings case.

    ``_generate_dqdl_rules([])`` returns ``""`` and the s3_tables template then
    renders no DQ block at all. A non-empty mapping list whose every entry is
    unsourced still returns a ruleset — RowCount only — which stays satisfiable
    for any source that produced rows.
    """
    dqdl = _mod._generate_dqdl_rules(
        [
            {"target_field": "phone", "source_field": None},
            {"target_field": "fax", "source_field": "  "},
        ]
    )
    assert dqdl == "Rules = [\n    RowCount > 0\n]"


def test_generate_code_does_not_bake_an_unsourced_field_into_the_rendered_ruleset():
    """C7 reaches the rendered job, not just the helper (render call site)."""
    captured = {}
    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    mapping_result = {
        "field_mappings": [
            *_mappings("cust_id"),
            {"target_field": "phone", "source_field": None, "confidence": 0.2},
        ]
    }
    with (
        patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
        patch.object(jinja2.Template, "render", _capture_render),
    ):
        code = _mod.generate_code(
            pipeline_metadata=_s3_tables_pipeline_metadata(),
            mapping_result=mapping_result,
        )

    dqdl = captured.get("dqdl_rules")
    assert 'Completeness "cust_id" >= 0.95' in dqdl
    assert 'Completeness "phone"' not in dqdl
    assert 'Completeness "phone"' not in code
