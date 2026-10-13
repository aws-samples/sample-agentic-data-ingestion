# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/rerun_contract.py (P28 R9.2 — typed trigger-input builder).

Covers the builder logic the contract tests don't exercise: default
resolution (source_prefix from pipeline_id, source_bucket from the pipeline
record), blank-string rejection, flavor gating (retry-only and rerun-only
arguments), step-name normalization for retry inputs, and override
emission rules.
"""

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from rerun_contract import RERUN_TRIGGERS, TRIGGER_VALUES, build_trigger_input
from step_names import StepName

PIPELINE = {"pipeline_id": "sales-data", "source_type": "S3_UPLOAD", "source_bucket": "bkt"}


class TestSourceResolution:
    def test_source_prefix_defaults_to_pipeline_id(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual")
        assert out["source_prefix"] == "sales-data"

    def test_explicit_source_prefix_wins(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual", source_prefix="sales-data/2026")
        assert out["source_prefix"] == "sales-data/2026"

    def test_explicit_source_bucket_wins_over_record(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual", source_bucket="other-bkt")
        assert out["source_bucket"] == "other-bkt"

    def test_source_bucket_from_record_top_level(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual")
        assert out["source_bucket"] == "bkt"

    def test_source_bucket_from_pipeline_config(self):
        pipeline = {"pipeline_id": "p", "pipeline_config": {"source_bucket": "cfg-bkt"}}
        out = build_trigger_input(pipeline, triggered_by="manual")
        assert out["source_bucket"] == "cfg-bkt"

    def test_source_type_defaults_to_s3_upload(self):
        out = build_trigger_input({"pipeline_id": "p", "source_bucket": "b"}, triggered_by="manual")
        assert out["source_type"] == "S3_UPLOAD"

    def test_single_file_mode_has_no_trigger_mode(self):
        out = build_trigger_input(PIPELINE, triggered_by="s3_upload", source_key="sales-data/f.csv")
        assert "trigger_mode" not in out

    def test_trigger_mirrors_triggered_by(self):
        out = build_trigger_input(PIPELINE, triggered_by="scheduled")
        assert out["trigger"] == out["triggered_by"] == "scheduled"


class TestBlankRejection:
    """Explicitly-passed blank strings are caller bugs, not defaults."""

    @pytest.mark.parametrize(
        ("field", "kwargs"),
        [
            ("source_key", {"source_key": ""}),
            ("source_prefix", {"source_prefix": "   "}),
            ("run_id", {"run_id": ""}),
            ("source_bucket", {"source_bucket": "  "}),
        ],
    )
    def test_blank_argument_raises_naming_field(self, field, kwargs):
        with pytest.raises(ValueError, match=field):
            build_trigger_input(PIPELINE, triggered_by="manual", **kwargs)

    def test_blank_original_run_id_raises(self):
        with pytest.raises(ValueError, match="original_run_id"):
            build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal", original_run_id=" ")


class TestJdbcStrictness:
    """JDBC pipelines have no S3 source location — passing one is drift."""

    JDBC = {"pipeline_id": "orders", "source_type": "JDBC"}

    @pytest.mark.parametrize("field", ["source_bucket", "source_key", "source_prefix"])
    def test_s3_source_argument_rejected_naming_field(self, field):
        with pytest.raises(ValueError, match=field):
            build_trigger_input(self.JDBC, triggered_by="manual", **{field: "x"})

    def test_jdbc_output_has_no_source_keys(self):
        out = build_trigger_input(self.JDBC, triggered_by="scheduled")
        assert not {"source_bucket", "source_key", "source_prefix", "trigger_mode"} & out.keys()

    def test_record_level_bucket_attribute_is_not_explicit_passing(self):
        # A pipeline record that happens to carry source_bucket must not
        # trip the JDBC rejection — only explicit kwargs count.
        jdbc_with_bucket = {"pipeline_id": "orders", "source_type": "JDBC", "source_bucket": "bkt"}
        out = build_trigger_input(jdbc_with_bucket, triggered_by="manual")
        assert "source_bucket" not in out


class TestFlavorGating:
    def test_retry_from_step_rejected_for_non_retry(self):
        with pytest.raises(ValueError, match="retry_from_step"):
            build_trigger_input(PIPELINE, triggered_by="manual", retry_from_step="CreateCode")

    def test_cached_results_rejected_for_non_retry(self):
        with pytest.raises(ValueError, match="cached_step_results"):
            build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal", cached_step_results={})

    def test_original_run_id_rejected_for_fresh_triggers(self):
        with pytest.raises(ValueError, match="original_run_id"):
            build_trigger_input(PIPELINE, triggered_by="s3_upload", original_run_id="run-1")

    def test_original_run_id_allowed_for_retry(self):
        out = build_trigger_input(
            PIPELINE,
            triggered_by="retry",
            run_id="run-1",
            original_run_id="run-1",
            retry_from_step="CreateCode",
        )
        assert out["original_run_id"] == "run-1"

    def test_fresh_trigger_may_carry_explicit_run_id(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual", run_id="run-x")
        assert out["run_id"] == "run-x"

    def test_fresh_trigger_omits_run_id_by_default(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual")
        assert "run_id" not in out


class TestRegenerationAttempt:
    """p32 Decision D1 — the counter is legal on the rerun flavors only.

    Gated exactly like ``original_run_id``: ``retry`` and ``self_healing``
    accept it, every other flavor raises ``ValueError`` naming the field.
    Rejected alternatives were *all flavors* (a field that may be present
    but must have no effect is what left the regeneration loop wired-but-
    dead) and *retry alone* (the self-healing agent is the other producer
    that re-runs after a failure).

    **Validates: Requirements 2.15; 3.18, 3.19**
    """

    def _rerun_kwargs(self, triggered_by):
        kwargs = {"triggered_by": triggered_by, "run_id": "run-1"}
        if triggered_by == "retry":
            kwargs["retry_from_step"] = "CreateCode"
        return kwargs

    @pytest.mark.parametrize("triggered_by", sorted(RERUN_TRIGGERS))
    def test_accepted_on_rerun_flavors(self, triggered_by):
        out = build_trigger_input(PIPELINE, **self._rerun_kwargs(triggered_by), regeneration_attempt=2)
        assert out["regeneration_attempt"] == 2

    @pytest.mark.parametrize("triggered_by", sorted(TRIGGER_VALUES - RERUN_TRIGGERS))
    def test_rejected_on_every_other_flavor_naming_the_field(self, triggered_by):
        with pytest.raises(ValueError, match="regeneration_attempt"):
            build_trigger_input(PIPELINE, triggered_by=triggered_by, regeneration_attempt=1)

    def test_zero_is_legal_and_emitted(self):
        # The initial value a producer may set explicitly — 0 means "original
        # generation", not "unset", so it must survive rather than be dropped.
        out = build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal", regeneration_attempt=0)
        assert out["regeneration_attempt"] == 0

    def test_omitted_when_not_passed(self):
        """3.18: adding the key must not change first-attempt behaviour —
        a run that does not carry it still does not carry it."""
        out = build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal")
        assert "regeneration_attempt" not in out

    @pytest.mark.parametrize(
        "bad",
        ["1", 1.5, Decimal("1"), True],
        ids=["str", "float", "dynamodb_decimal", "bool"],
    )
    def test_non_int_rejected_naming_the_field(self, bad):
        with pytest.raises(ValueError, match="regeneration_attempt"):
            build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal", regeneration_attempt=bad)

    def test_negative_rejected_naming_the_field(self):
        with pytest.raises(ValueError, match="regeneration_attempt"):
            build_trigger_input(
                PIPELINE, triggered_by="retry", run_id="run-1", retry_from_step="CreateCode", regeneration_attempt=-1
            )

    @pytest.mark.parametrize("triggered_by", sorted(RERUN_TRIGGERS))
    def test_counter_is_the_only_difference_from_the_same_input_without_it(self, triggered_by):
        """3.19: the field is added, no existing rejection is relaxed and no
        other emitted key moves."""
        kwargs = self._rerun_kwargs(triggered_by)
        without = build_trigger_input(PIPELINE, **kwargs)
        with_counter = build_trigger_input(PIPELINE, **kwargs, regeneration_attempt=1)
        assert with_counter == {**without, "regeneration_attempt": 1}


class TestRetryNormalization:
    def _retry(self, **kwargs):
        base = {"triggered_by": "retry", "run_id": "run-1", "retry_from_step": "CreateCode"}
        base.update(kwargs)
        return build_trigger_input(PIPELINE, **base)

    @pytest.mark.parametrize(
        ("variant", "expected"),
        [
            ("SchemaExtract", "SchemaExtract"),  # SFN id passes through
            ("schema_extract", "SchemaExtract"),  # canonical snake_case
            ("CodeGeneration", "CreateCode"),  # cataloged legacy variant
            (StepName.TEST_CODE, "TestCode"),  # enum member
        ],
    )
    def test_retry_from_step_variants_normalize(self, variant, expected):
        out = self._retry(retry_from_step=variant)
        assert out["retry_from_step"] == expected

    def test_cached_result_keys_normalize_to_canonical_snake_case(self):
        out = self._retry(
            cached_step_results={"FileValidation": {"a": 1}, "CodeGeneration": {"b": 2}},
        )
        assert out["file_validation_result"] == {"a": 1}
        assert out["create_code_result"] == {"b": 2}

    def test_duplicate_cached_step_variants_collide(self):
        with pytest.raises(ValueError, match="duplicate"):
            self._retry(cached_step_results={"create_code": {"a": 1}, "CodeGeneration": {"b": 2}})

    def test_unknown_cached_step_named(self):
        with pytest.raises(ValueError, match=r"cached_step_results.*bogus_step"):
            self._retry(cached_step_results={"bogus_step": {}})


class TestOverrides:
    def test_empty_overrides_omitted(self):
        out = build_trigger_input(PIPELINE, triggered_by="self_healing", run_id="r-heal", job_config_overrides={})
        assert "job_config_overrides" not in out

    def test_overrides_allowed_on_any_trigger(self):
        out = build_trigger_input(PIPELINE, triggered_by="manual", job_config_overrides={"WorkerType": "G.2X"})
        assert out["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_overrides_copied_not_aliased(self):
        overrides = {"WorkerType": "G.2X"}
        out = build_trigger_input(PIPELINE, triggered_by="manual", job_config_overrides=overrides)
        overrides["WorkerType"] = "G.4X"
        assert out["job_config_overrides"] == {"WorkerType": "G.2X"}
