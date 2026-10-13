# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_quality_checks records a Quality_Verdict mode (p30 R7.2, R7.5).

Two things are pinned here.

**R7.2 — the mode is on the state row, keyed on what was actually generated.**
The handler derives it from this run's ``create_code`` row (the rendered
template + the generated DQDL ruleset), not from its own ``quality_rules``.
That distinction is the whole point: ``quality_rules`` is a different
vocabulary, built from the target schema, and it is non-empty (two dataset-level
rules) even when the generated job carries no DQ at all — so a mode inferred
from it would report ``enforced`` for every pipeline.

**R7.5 — the inert ``execution_config`` keys are gone from the persisted
configuration.** ``fail_on_error: True`` had no reader anywhere in the
repository and implied enforcement the system does not perform.

The success path of this handler had no test before this file, so the first
class characterizes the row shape that already existed (from the requirements'
description of it, not from a captured output) to keep the additions honest.

Validates: Requirements 7.2, 7.5
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_quality_checks",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _load_module():
    """Load the create_quality_checks lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_quality_checks_lf_p30_verdict",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["create_quality_checks_lf_p30_verdict"] = mod
    spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

RUN_ID = "run-verdict-1"
PIPELINE_ID = "pipe-verdict-1"

# The DQDL string shape create_code bakes into the generated job, transcribed
# from _generate_dqdl_rules rather than produced by importing it.
DQDL_TWO_RULES = 'Rules = [\n    RowCount > 0,\n    Completeness "customer_id" >= 0.95\n]'


def _create_code_row(template_used: str, dqdl_rules: str, target_type: str = "s3_tables") -> dict:
    """A create_code state row, in the shape that writer actually produces:
    template + target under output_data.summary (via create_output_data), the
    DQDL ruleset under output_data.approval (via create_approval_payload)."""
    return {
        "run_id": RUN_ID,
        "step_name": "create_code",
        "status": "success",
        "output_data": {
            "summary": {
                "line_count": 412,
                "template_used": template_used,
                "target_type": target_type,
            },
            "approval": {
                "approval_type": "code_generation",
                "dqdl_rules": dqdl_rules,
                "template_selection": {"template_used": template_used},
            },
        },
    }


def _runs_table(create_code_row: dict | None, infra_row: dict | None = None) -> mock.MagicMock:
    """State-table mock that answers the handler's two cross-step reads by key."""
    table = mock.MagicMock()

    def _get_item(Key):  # boto3 names the kwarg "Key"
        step = Key["step_name"]
        if step == "create_code" and create_code_row is not None:
            return {"Item": create_code_row}
        if step == "create_infrastructure" and infra_row is not None:
            return {"Item": infra_row}
        return {}

    table.get_item.side_effect = _get_item
    return table


def _invoke(table: mock.MagicMock) -> dict:
    with mock.patch.object(_lf, "pipeline_runs_table", table):
        return _lf.lambda_handler({"run_id": RUN_ID, "pipeline_id": PIPELINE_ID}, None)


def _written_row(table: mock.MagicMock) -> dict:
    return table.put_item.call_args.kwargs["Item"]


# ── characterization: the row shape that already existed ─────────────


class TestExistingRowShapeUnchanged:
    def test_success_row_keeps_its_canonical_identity_and_rule_counts(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        _invoke(table)
        row = _written_row(table)
        assert row["run_id"] == RUN_ID
        assert row["step_name"] == "create_quality_checks"
        assert row["status"] == "success"
        assert row["approval_required"] is True
        # With no mapping_id the target schema is empty, so only the two
        # dataset-level rules generate_quality_check_rules always appends exist.
        assert row["output_data"]["total_rules"] == 2
        assert row["output_data"]["quality_configuration"]["rule_summary"]["total_rules"] == 2

    def test_response_still_carries_the_first_run_handoff(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        result = _invoke(table)
        assert result["statusCode"] == 200
        assert result["next_step"] == "first_run_approval"
        assert result["total_rules"] == 2


# ── R7.2: the verdict mode on the state row ──────────────────────────


class TestVerdictOnTheStateRow:
    def test_dq_bearing_combination_records_enforced(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        _invoke(table)
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["mode"] == "enforced"
        assert verdict["evaluation_point"] == "before_write"
        assert verdict["dqdl_rule_count"] == 2
        assert verdict["template_used"] == "glue_job_s3_tables.py.j2"
        assert verdict["determined_from"] == "create_code_state_row"

    @pytest.mark.parametrize(
        "template",
        [
            "glue_job_glue_catalog.py.j2",
            "glue_job_jdbc.py.j2",
            "glue_job_s3_parquet.py.j2",
            "glue_job_jdbc_source.py.j2",
            "fallback_full_ai_v1",
        ],
    )
    def test_templates_without_inline_dq_record_advisory(self, template):
        table = _runs_table(_create_code_row(template, DQDL_TWO_RULES))
        _invoke(table)
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["mode"] == "advisory"
        assert verdict["evaluation_point"] == "none"

    def test_jdbc_source_to_an_s3_tables_target_is_advisory(self):
        """The mislabel this keying prevents: the target type is s3_tables, but
        a JDBC source routes to the JDBC template, which runs no DQ."""
        table = _runs_table(_create_code_row("glue_job_jdbc_source.py.j2", DQDL_TWO_RULES, "s3_tables"))
        _invoke(table)
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["mode"] == "advisory"
        assert verdict["target_type"] == "s3_tables"

    def test_empty_generated_dqdl_is_advisory_even_on_the_dq_bearing_template(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", ""))
        _invoke(table)
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["mode"] == "advisory"
        assert verdict["dqdl_rule_count"] == 0

    def test_the_mode_is_not_inferred_from_this_steps_own_rule_count(self):
        """quality_rules is non-empty on every run (two dataset-level rules) —
        it must not be able to produce an `enforced` verdict."""
        table = _runs_table(_create_code_row("glue_job_glue_catalog.py.j2", ""))
        _invoke(table)
        row = _written_row(table)
        assert row["output_data"]["total_rules"] == 2
        assert row["output_data"]["quality_verdict"]["mode"] == "advisory"
        assert row["output_data"]["quality_verdict"]["dqdl_rule_count"] == 0

    def test_missing_create_code_row_records_an_undetermined_verdict(self):
        table = _runs_table(None)
        _invoke(table)
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["mode"] == "advisory"
        assert verdict["evaluation_point"] == "unknown"
        assert verdict["determined_from"] == "create_code_state_row_absent"

    def test_unreadable_create_code_row_does_not_fail_the_step(self):
        """A provenance read failing must degrade visibly, not raise: this step
        writes the row the first-run approval gate depends on."""
        from botocore.exceptions import ClientError

        table = mock.MagicMock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "GetItem"
        )
        result = _invoke(table)
        assert result["statusCode"] == 200
        verdict = _written_row(table)["output_data"]["quality_verdict"]
        assert verdict["determined_from"] == "create_code_state_row_unreadable"
        assert verdict["mode"] == "advisory"

    def test_verdict_never_claims_the_run_was_blocked(self):
        for template in ("glue_job_s3_tables.py.j2", "glue_job_jdbc.py.j2"):
            table = _runs_table(_create_code_row(template, DQDL_TWO_RULES))
            _invoke(table)
            assert _written_row(table)["output_data"]["quality_verdict"]["blocks_run"] is False


# ── R7.2: the verdict reaches the first_run approval payload ──────────


class TestVerdictOnTheApprovalPayload:
    def test_combined_approval_payload_carries_the_verdict(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        _invoke(table)
        approval = _written_row(table)["output_data"]["approval"]
        assert approval["approval_type"] == "first_run"
        assert approval["quality_verdict"]["mode"] == "enforced"

    def test_degraded_approval_payload_still_carries_the_verdict(self):
        """When the infrastructure read raises, the payload degrades — but an
        approver still needs to know whether anything gates the write."""
        table = _runs_table(_create_code_row("glue_job_jdbc.py.j2", ""))
        verdict = _lf.derive_quality_verdict(template_used="glue_job_jdbc.py.j2", dqdl_rules="", target_type="jdbc")
        with mock.patch.object(_lf, "pipeline_runs_table", table):
            table.get_item.side_effect = RuntimeError("infrastructure read exploded")
            payload = _lf.combine_infrastructure_and_quality_approval_data(RUN_ID, {}, verdict)
        assert "error" in payload
        assert payload["quality_verdict"]["mode"] == "advisory"


# ── R7.5: the inert execution_config keys are gone ───────────────────


class TestInertExecutionConfigRemoved:
    def test_persisted_configuration_has_no_execution_config(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        _invoke(table)
        config = _written_row(table)["output_data"]["quality_configuration"]
        assert "execution_config" not in config

    def test_no_inert_enforcement_key_survives_anywhere_in_the_row(self):
        """The keys were also carried into the approval payload nested inside
        this row — that copy must be gone too."""
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        _invoke(table)
        serialized = repr(_written_row(table))
        for inert_key in ("fail_on_error", "max_error_threshold", "fail_on_warning", "max_warning_threshold"):
            assert inert_key not in serialized

    def test_returned_configuration_has_no_execution_config(self):
        table = _runs_table(_create_code_row("glue_job_s3_tables.py.j2", DQDL_TWO_RULES))
        result = _invoke(table)
        assert "execution_config" not in result["quality_configuration"]

    def test_rule_summary_and_output_config_are_untouched(self):
        """Only the enforcement keys were inert-and-misleading; the rule counts
        are read by the approval payload and output_config is out of R7.5's
        scope."""
        config = _lf.create_quality_check_configuration([{"severity": "error"}, {"severity": "warning"}])
        assert config["rule_summary"] == {"total_rules": 2, "error_rules": 1, "warning_rules": 1}
        assert config["output_config"]["generate_report"] is True
