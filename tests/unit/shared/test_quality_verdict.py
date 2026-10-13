# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit + property tests for shared/quality_verdict.py (p30 R7.2).

The dangerous failure mode of a Quality_Verdict mode is not a crash — it is a
confident wrong label. Two specific wrong labels are what these tests exist to
prevent:

1. **``enforced`` for a pipeline whose job runs no DQ.** Reachable two ways: by
   keying on target type alone (a JDBC source routes every target to the JDBC
   template, which runs no DQ), or by ignoring the generated DQDL ruleset (the
   one DQ-bearing template renders its block only when that ruleset is
   non-empty).
2. **``enforced`` read as "a quality failure stopped the run".** It never does:
   failing rows are quarantined and the job still succeeds. ``blocks_run`` must
   be False on every path.

The negative assertions below are the substance — a derivation can satisfy
R7.2's letter (two modes recorded) and still lie about both.

Validates: Requirements 7.2
"""

import pathlib
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from quality_verdict import (
    DQ_DEFAULT_ON_TEMPLATES,
    FROM_CREATE_CODE_ROW,
    FROM_MISSING_CREATE_CODE_ROW,
    FROM_UNREADABLE_CREATE_CODE_ROW,
    MODE_ADVISORY,
    MODE_ENFORCED,
    count_dqdl_rules,
    derive_quality_verdict,
)

# The exact string shape create_code's _generate_dqdl_rules emits, transcribed
# from that generator rather than produced by calling it (this module must not
# import a Lambda-local module).
DQDL_TWO_RULES = 'Rules = [\n    RowCount > 0,\n    Completeness "customer_id" >= 0.95\n]'
DQDL_ONE_RULE = "Rules = [\n    RowCount > 0\n]"

S3_TABLES_TEMPLATE = "glue_job_s3_tables.py.j2"
# The four templates that render no DQ block BY DEFAULT, plus the full-AI
# fallback path's marker (create_code sets template_used = "fallback_full_ai_v1"
# and generated_dqdl_rules = "" there).
#
# "by default" is p64 2b.4's amendment and the only change this module needed.
# Before p64 these four contained no ``EvaluateDataQuality`` block at all; R6.1
# put the block in all five via a shared partial, and R6.6 gated it on a
# per-pipeline opt-in whose default is the pre-p64 posture. So none of them
# evaluates anything for a pipeline that did not opt in, every assertion below
# still holds unchanged, and the ONE thing that moved is the name of the set the
# property test reads — ``DQ_BEARING_TEMPLATES`` became
# ``DQ_DEFAULT_ON_TEMPLATES``, because it now describes a default rather than
# which files contain the block. The opted-in arm is asserted in
# ``tests/unit/templates/test_data_quality_gate.py``, which can render the
# templates; this module stays pure.
NON_DQ_TEMPLATES = [
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc_source.py.j2",
    "fallback_full_ai_v1",
]


# ── count_dqdl_rules ─────────────────────────────────────────────────


class TestCountDqdlRules:
    def test_counts_comma_separated_entries(self):
        assert count_dqdl_rules(DQDL_TWO_RULES) == 2
        assert count_dqdl_rules(DQDL_ONE_RULE) == 1

    def test_empty_ruleset_is_zero(self):
        """The empty string is what _generate_dqdl_rules returns with no field
        mappings — the degradation case, not a missing value."""
        assert count_dqdl_rules("") == 0
        assert count_dqdl_rules(None) == 0

    def test_malformed_input_counts_zero_rather_than_raising(self):
        """A provenance annotation must never be the reason a step fails."""
        assert count_dqdl_rules("Rules = ") == 0
        assert count_dqdl_rules("]not[ a ruleset") == 0
        assert count_dqdl_rules("Rules = []") == 0

    def test_trailing_separators_do_not_inflate_the_count(self):
        assert count_dqdl_rules("Rules = [\n    RowCount > 0,\n]") == 1


# ── mode derivation ─────────────────────────────────────────────────


class TestEnforcedCombination:
    """The one combination of five that renders DQ: S3_UPLOAD → S3 Tables with
    a non-empty generated ruleset."""

    def _verdict(self):
        return derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules=DQDL_TWO_RULES,
            target_type="s3_tables",
        )

    def test_mode_is_enforced(self):
        assert self._verdict()["mode"] == MODE_ENFORCED

    def test_evaluation_happens_before_the_write_at_row_scope(self):
        verdict = self._verdict()
        assert verdict["evaluation_point"] == "before_write"
        assert verdict["enforcement_scope"] == "row_level"

    def test_enforced_does_not_claim_the_run_is_blocked(self):
        """The DQ block is wrapped in `except Exception` and the job reports
        success either way — `enforced` is about rows, not the run."""
        verdict = self._verdict()
        assert verdict["blocks_run"] is False
        reason = verdict["reason"].lower()
        assert "not blocked" in reason
        assert "fail the glue job" in reason

    def test_reason_names_the_dataset_level_caveat(self):
        """A dataset-level rule failing with no failing rows withholds
        nothing — the reason must not imply otherwise."""
        assert "withholds nothing" in self._verdict()["reason"]

    def test_dqdl_rule_count_comes_from_the_generated_ruleset(self):
        assert self._verdict()["dqdl_rule_count"] == 2

    def test_provenance_is_recorded(self):
        verdict = self._verdict()
        assert verdict["template_used"] == S3_TABLES_TEMPLATE
        assert verdict["target_type"] == "s3_tables"
        assert verdict["determined_from"] == FROM_CREATE_CODE_ROW


class TestAdvisoryCombinations:
    @pytest.mark.parametrize("template", NON_DQ_TEMPLATES)
    def test_templates_without_a_dq_block_are_advisory(self, template):
        verdict = derive_quality_verdict(template_used=template, dqdl_rules=DQDL_TWO_RULES)
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["evaluation_point"] == "none"
        assert verdict["enforcement_scope"] == "none"

    def test_a_non_empty_ruleset_does_not_make_a_non_dq_template_enforced(self):
        """create_code passes dqdl_rules to every template; four ignore it.
        A ruleset existing is not evidence that anything evaluates it."""
        verdict = derive_quality_verdict(template_used="glue_job_glue_catalog.py.j2", dqdl_rules=DQDL_TWO_RULES)
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["dqdl_rule_count"] == 2

    def test_jdbc_source_to_s3_tables_target_is_advisory(self):
        """The wrong answer this keying exists to prevent: a JDBC source routes
        to glue_job_jdbc_source.py.j2 for EVERY target, including S3 Tables."""
        verdict = derive_quality_verdict(
            template_used="glue_job_jdbc_source.py.j2",
            dqdl_rules=DQDL_TWO_RULES,
            target_type="s3_tables",
        )
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["target_type"] == "s3_tables"

    def test_advisory_reason_does_not_claim_quarantining(self):
        verdict = derive_quality_verdict(template_used="glue_job_s3_parquet.py.j2", dqdl_rules="")
        reason = verdict["reason"].lower()
        assert "quarantine" not in reason
        assert "withheld" not in reason

    def test_dq_bearing_template_with_an_empty_ruleset_is_advisory(self):
        """Condition 3: the template's DQ block is gated on a non-empty
        ruleset, so a mapping-less pipeline on this combination renders no DQ."""
        verdict = derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules="",
            target_type="s3_tables",
        )
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["evaluation_point"] == "none"
        assert verdict["dqdl_rule_count"] == 0
        assert "non-empty" in verdict["reason"]


class TestUndeterminedProvenance:
    @pytest.mark.parametrize(
        "determined_from",
        [FROM_MISSING_CREATE_CODE_ROW, FROM_UNREADABLE_CREATE_CODE_ROW],
    )
    def test_unavailable_facts_never_claim_an_evaluation_point(self, determined_from):
        verdict = derive_quality_verdict(
            template_used=None,
            dqdl_rules=None,
            determined_from=determined_from,
        )
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["evaluation_point"] == "unknown"
        assert verdict["determined_from"] == determined_from

    def test_undetermined_is_distinguishable_from_a_known_absence(self):
        """'We could not look' and 'we looked, there is no DQ' must not read
        the same — both are advisory, so the difference lives in the other
        fields."""
        unknown = derive_quality_verdict(
            template_used=None, dqdl_rules=None, determined_from=FROM_MISSING_CREATE_CODE_ROW
        )
        known_absence = derive_quality_verdict(template_used="glue_job_jdbc.py.j2", dqdl_rules="")
        assert unknown["mode"] == known_absence["mode"] == MODE_ADVISORY
        assert unknown["evaluation_point"] != known_absence["evaluation_point"]
        assert unknown["reason"] != known_absence["reason"]

    def test_a_dq_bearing_template_reported_as_unreadable_is_not_enforced(self):
        """Provenance wins over content: if the facts are not trustworthy, the
        safe direction is 'nothing was gated'."""
        verdict = derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules=DQDL_TWO_RULES,
            determined_from=FROM_UNREADABLE_CREATE_CODE_ROW,
        )
        assert verdict["mode"] == MODE_ADVISORY


# ── properties ──────────────────────────────────────────────────────

_templates = st.sampled_from([*NON_DQ_TEMPLATES, S3_TABLES_TEMPLATE, "", "glue_job_made_up.py.j2"])
_dqdl = st.sampled_from([DQDL_TWO_RULES, DQDL_ONE_RULE, "", "Rules = []", "garbage"])
_provenance = st.sampled_from([FROM_CREATE_CODE_ROW, FROM_MISSING_CREATE_CODE_ROW, FROM_UNREADABLE_CREATE_CODE_ROW])
_targets = st.sampled_from(["s3_tables", "glue_catalog", "jdbc", "rds_database", "s3_parquet", ""])


@given(template=_templates, dqdl=_dqdl, target=_targets, provenance=_provenance)
def test_enforced_requires_all_three_conditions(template, dqdl, target, provenance):
    """Property: ``enforced`` appears only when the rendered template is the
    DQ-bearing one, the generated ruleset is non-empty, AND the facts came from
    the create_code row. Nothing else can produce it — in particular no target
    type on its own.

    **Validates: Requirements 7.2**
    """
    verdict = derive_quality_verdict(
        template_used=template,
        dqdl_rules=dqdl,
        target_type=target,
        determined_from=provenance,
    )
    if verdict["mode"] == MODE_ENFORCED:
        assert template in DQ_DEFAULT_ON_TEMPLATES
        assert count_dqdl_rules(dqdl) > 0
        assert provenance == FROM_CREATE_CODE_ROW


@given(template=_templates, dqdl=_dqdl, target=_targets, provenance=_provenance)
def test_verdict_is_always_a_persistable_record_that_never_claims_a_blocked_run(template, dqdl, target, provenance):
    """Property: every derivation returns one of the two modes R7.2 allows,
    reports ``blocks_run: False`` (no combination fails the run on a quality
    failure), and carries only DynamoDB-safe scalars — no floats, so the row
    write needs no Decimal conversion of its own.

    **Validates: Requirements 7.2**
    """
    verdict = derive_quality_verdict(
        template_used=template,
        dqdl_rules=dqdl,
        target_type=target,
        determined_from=provenance,
    )
    assert verdict["mode"] in (MODE_ENFORCED, MODE_ADVISORY)
    assert verdict["blocks_run"] is False
    assert verdict["enforcement_scope"] in ("row_level", "none")
    assert verdict["evaluation_point"] in ("before_write", "none", "unknown")
    assert isinstance(verdict["dqdl_rule_count"], int)
    assert verdict["reason"]
    assert not any(isinstance(value, float) for value in verdict.values())
