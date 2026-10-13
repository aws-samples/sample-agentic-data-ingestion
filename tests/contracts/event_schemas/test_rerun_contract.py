# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract tests for shared/rerun_contract.py — the pipeline trigger-input contract.

**Validates: Requirements 9.2** — the rerun input is a shared, tested
contract used by pipeline_trigger, retry-from-step, and the self-healing
agent — plus the R9.1 fix-enabler (heal-rerun input carries
``source_bucket``/``source_prefix`` so heal reruns no longer die at
file_validation) and the Heal_Guard key (``triggered_by="self_healing"``).

The cataloged key sets below mirror what
``lambda/agentic_data_ingestion_file_validation/lambda_function.py``
actually consumes:

* single-file mode: ``source_bucket`` + ``source_key`` (checked together
  before the Records conversion)
* prefix mode: hard ``event["source_bucket"]`` / ``event["source_prefix"]``
  subscripts, routed by ``trigger_mode == "prefix"``
* JDBC mode: ``pipeline_id`` (routed by ``source_type == "JDBC"``)
"""

import json
import os
import re
import sys
from decimal import Decimal

import pytest

# Shared modules import each other flat (Lambda-layer layout) — put shared/
# on the path and stub env_utils before retry_utils import, matching the
# tests/unit/shared/test_step_names.py convention.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from rerun_contract import (
    RERUN_TRIGGERS,
    SOURCE_TYPES,
    TRIGGER_VALUES,
    build_trigger_input,
)
from step_names import STEP_SEQUENCE, TASK_STEPS

ORCHESTRATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "orchestration-stack.ts"
)

# ── What file_validation consumes (cataloged from its source) ─────────────
FILE_VALIDATION_SINGLE_FILE_KEYS = {"source_bucket", "source_key", "pipeline_id"}
FILE_VALIDATION_PREFIX_KEYS = {"source_bucket", "source_prefix", "pipeline_id"}
FILE_VALIDATION_JDBC_KEYS = {"pipeline_id", "source_type"}

PIPELINE = {"pipeline_id": "sales-data", "source_type": "S3_UPLOAD", "source_bucket": "adi-source-files"}
JDBC_PIPELINE = {"pipeline_id": "orders-jdbc", "source_type": "JDBC"}

# ── Every distinct call shape the four producers build ────────────────────
# Transcribed from the call sites, not from the builder's output:
#   pipeline_trigger/lambda_function.py:847 (manual S3), :976 (JDBC),
#     :1151 (scheduled), :1316 (s3_upload)
#   dashboard_api/handlers/upload.py:174 (manual_upload)
#   dashboard_api/handlers/pipelines_runs.py:1042 (trigger_batch, prefix),
#     :1312 (retry_run)
#   agentcore/self_healing_agent/main.py:341 (self_healing)
# `build_trigger_input` has 54 callers across those four files, so any change
# to its signature is measured against all of these, not one of them.
PRODUCER_SHAPES: list[tuple[str, dict, dict]] = [
    ("pipeline_trigger:manual_s3", PIPELINE, {"triggered_by": "manual", "source_key": "sales-data/f.csv"}),
    ("pipeline_trigger:jdbc_manual", JDBC_PIPELINE, {"triggered_by": "manual"}),
    ("pipeline_trigger:jdbc_scheduled", JDBC_PIPELINE, {"triggered_by": "scheduled"}),
    ("pipeline_trigger:scheduled_s3", PIPELINE, {"triggered_by": "scheduled", "source_key": "sales-data/g.csv"}),
    ("pipeline_trigger:s3_upload", PIPELINE, {"triggered_by": "s3_upload", "source_key": "sales-data/h.csv"}),
    ("upload:manual_upload", PIPELINE, {"triggered_by": "manual_upload", "source_key": "sales-data/i.csv"}),
    ("pipelines_runs:trigger_batch", PIPELINE, {"triggered_by": "manual", "source_prefix": "sales-data/2026"}),
    (
        "pipelines_runs:retry_run",
        PIPELINE,
        {
            "triggered_by": "retry",
            "run_id": "run-1",
            "retry_from_step": "CreateCode",
            "cached_step_results": {"file_validation": {"run_id": "run-1"}},
        },
    ),
    (
        "self_healing_agent:heal_rerun",
        PIPELINE,
        {
            "triggered_by": "self_healing",
            "run_id": "run-1-heal",
            "original_run_id": "run-1",
            "job_config_overrides": {"WorkerType": "G.2X"},
        },
    ),
]


def _heal_input(**overrides):
    """A representative self-healing rerun input."""
    kwargs = {
        "triggered_by": "self_healing",
        "run_id": "run-abc-heal",
        "original_run_id": "run-abc",
    }
    kwargs.update(overrides)
    return build_trigger_input(PIPELINE, **kwargs)


class TestFileValidationKeyCoverage:
    """The builder output contains every key file_validation consumes."""

    def test_s3_single_file_input_carries_single_file_keys(self):
        out = build_trigger_input(PIPELINE, triggered_by="s3_upload", source_key="sales-data/f.csv")
        assert out.keys() >= FILE_VALIDATION_SINGLE_FILE_KEYS
        assert out["source_bucket"] == "adi-source-files"
        assert out["source_key"] == "sales-data/f.csv"

    def test_s3_prefix_input_carries_prefix_keys_and_routing_flag(self):
        """Prefix-only inputs MUST carry trigger_mode=prefix — without it
        file_validation falls through the single-file path and the
        source_prefix is never consumed."""
        out = build_trigger_input(PIPELINE, triggered_by="manual")
        assert out.keys() >= FILE_VALIDATION_PREFIX_KEYS
        assert out["trigger_mode"] == "prefix"

    def test_jdbc_input_carries_pipeline_id_and_source_type(self):
        out = build_trigger_input(JDBC_PIPELINE, triggered_by="scheduled")
        assert out.keys() >= FILE_VALIDATION_JDBC_KEYS
        assert out["source_type"] == "JDBC"

    def test_s3_input_always_carries_full_trigger_contract(self):
        """R9.2: every S3 flavor carries source_bucket AND source_prefix —
        the full trigger contract, not a per-producer subset."""
        for triggered_by, extra in [
            ("s3_upload", {"source_key": "sales-data/f.csv"}),
            ("manual", {}),
            ("scheduled", {"source_key": "sales-data/g.csv"}),
            ("retry", {"run_id": "run-1", "retry_from_step": "SchemaExtract"}),
            ("self_healing", {"run_id": "run-1-heal"}),
        ]:
            out = build_trigger_input(PIPELINE, triggered_by=triggered_by, **extra)
            assert {"source_bucket", "source_prefix", "pipeline_id", "source_type"} <= out.keys(), triggered_by


class TestSelfHealingRerunContract:
    """The R9.1 fix-enabler and the Heal_Guard key."""

    def test_heal_input_includes_source_bucket_and_source_prefix(self):
        """R9.1: heal reruns carry source_bucket + source_prefix sourced
        from the pipeline record, so they no longer die at file_validation."""
        out = _heal_input()
        assert out["source_bucket"] == "adi-source-files"
        assert out["source_prefix"] == "sales-data"

    def test_heal_input_carries_triggered_by_self_healing(self):
        """Heal_Guard: a self-healing rerun carries triggered_by="self_healing"
        in its execution input (requirements glossary — NOT the pre-contract
        "self_healing_agent" value)."""
        out = _heal_input()
        assert out["triggered_by"] == "self_healing"
        assert out["trigger"] == "self_healing"

    def test_heal_input_is_routed_to_a_handler_that_exists(self):
        """A heal rerun has no exact source_key — the contract routes it to
        file_validation's prefix mode."""
        out = _heal_input()
        assert out["trigger_mode"] == "prefix"
        assert "source_key" not in out

    def test_heal_input_carries_job_config_overrides(self):
        """R9.5 enabler: remediation config rides the rerun input so
        create_glue_pipeline can merge instead of clobber."""
        overrides = {"WorkerType": "G.2X", "NumberOfWorkers": 10}
        out = _heal_input(job_config_overrides=overrides)
        assert out["job_config_overrides"] == overrides

    def test_heal_input_links_the_original_run(self):
        out = _heal_input()
        assert out["run_id"] == "run-abc-heal"
        assert out["original_run_id"] == "run-abc"

    def test_heal_requires_run_id(self):
        with pytest.raises(ValueError, match="run_id"):
            build_trigger_input(PIPELINE, triggered_by="self_healing")


class TestRetryFlavorContract:
    """Retry-from-step inputs — cached-result keys must match the state
    machine's resultPath names or they are dead keys (the pre-contract
    PascalCase ``FileValidation_result`` keys never matched)."""

    def _asl_result_paths(self) -> set[str]:
        with open(ORCHESTRATION_STACK_PATH) as f:
            source = f.read()
        # A quoted path ending exactly at `_result` is a ResultPath
        # declaration: `resultPath: '$.x_result'` on LambdaInvoke,
        # `ResultPath: '$.x_result'` on CustomStates, or the positional
        # ResultPath argument of the containerBackedTask helper that stamps
        # the four waitForTaskToken container steps (P28 R19.11). Payload
        # READS always drill deeper ('$.x_result.field') and don't match.
        return set(re.findall(r"'\$\.(\w+_result)'", source))

    def test_cached_result_keys_match_asl_result_paths(self):
        cached = {step.value: {"ok": True} for step in TASK_STEPS}
        out = build_trigger_input(
            PIPELINE,
            triggered_by="retry",
            run_id="run-1",
            retry_from_step="RunPipeline",
            cached_step_results=cached,
        )
        emitted = {k for k in out if k.endswith("_result")}
        assert emitted == {f"{step.value}_result" for step in TASK_STEPS}
        # Every emitted key is a resultPath the deployed state machine
        # actually dereferences.
        assert emitted <= self._asl_result_paths()

    def test_retry_from_step_is_normalized_to_sfn_state_id(self):
        """retry_run validates against retry_utils.STEP_ORDER (SFN state
        ids) — the contract emits that form regardless of input variant."""
        out = build_trigger_input(PIPELINE, triggered_by="retry", run_id="run-1", retry_from_step="schema_extract")
        assert out["retry_from_step"] == "SchemaExtract"

    def test_retry_carries_run_identity(self):
        out = build_trigger_input(PIPELINE, triggered_by="retry", run_id="run-1", retry_from_step="CreateCode")
        assert out["run_id"] == "run-1"
        assert out["trigger"] == "retry"


class TestValidationErrorsNameTheField:
    """Missing/empty required fields raise ValueError naming the field —
    the typed-builder drift protection."""

    def test_missing_pipeline_id(self):
        with pytest.raises(ValueError, match="pipeline_id"):
            build_trigger_input({}, triggered_by="manual")

    def test_empty_pipeline_id(self):
        with pytest.raises(ValueError, match="pipeline_id"):
            build_trigger_input({"pipeline_id": "  "}, triggered_by="manual")

    def test_unknown_triggered_by(self):
        with pytest.raises(ValueError, match="triggered_by"):
            build_trigger_input(PIPELINE, triggered_by="cron")

    def test_unknown_source_type(self):
        with pytest.raises(ValueError, match="source_type"):
            build_trigger_input({"pipeline_id": "p", "source_type": "FTP"}, triggered_by="manual")

    def test_missing_source_bucket_for_s3(self):
        with pytest.raises(ValueError, match="source_bucket"):
            build_trigger_input({"pipeline_id": "p", "source_type": "S3_UPLOAD"}, triggered_by="manual")

    def test_retry_missing_run_id(self):
        with pytest.raises(ValueError, match="run_id"):
            build_trigger_input(PIPELINE, triggered_by="retry", retry_from_step="CreateCode")

    def test_retry_missing_retry_from_step(self):
        with pytest.raises(ValueError, match="retry_from_step"):
            build_trigger_input(PIPELINE, triggered_by="retry", run_id="run-1")

    def test_unknown_retry_step_named(self):
        with pytest.raises(ValueError, match=r"retry_from_step.*NoSuchStep"):
            build_trigger_input(PIPELINE, triggered_by="retry", run_id="run-1", retry_from_step="NoSuchStep")


class TestJsonSerializability:
    """Step Functions input is JSON — every built input must serialize."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"triggered_by": "s3_upload", "source_key": "sales-data/f.csv"},
            {"triggered_by": "manual"},
            {"triggered_by": "scheduled", "source_key": "sales-data/g.csv"},
            {
                "triggered_by": "retry",
                "run_id": "run-1",
                "retry_from_step": "SchemaMapping",
                "cached_step_results": {"file_validation": {"run_id": "run-1", "file_count": 1}},
            },
            {
                "triggered_by": "self_healing",
                "run_id": "run-1-heal",
                "original_run_id": "run-1",
                "job_config_overrides": {"WorkerType": "G.2X"},
            },
        ],
        ids=["s3_upload", "prefix_manual", "scheduled", "retry", "self_healing"],
    )
    def test_output_round_trips_through_json(self, kwargs):
        out = build_trigger_input(PIPELINE, **kwargs)
        assert json.loads(json.dumps(out)) == out

    def test_jdbc_output_round_trips_through_json(self):
        out = build_trigger_input(JDBC_PIPELINE, triggered_by="manual")
        assert json.loads(json.dumps(out)) == out

    def test_decimal_in_overrides_rejected_naming_field(self):
        """DynamoDB Decimals are rejected loudly, not converted silently."""
        with pytest.raises(ValueError, match="job_config_overrides"):
            _heal_input(job_config_overrides={"NumberOfWorkers": Decimal("10")})

    def test_decimal_in_cached_results_rejected_naming_field(self):
        with pytest.raises(ValueError, match="cached_step_results"):
            build_trigger_input(
                PIPELINE,
                triggered_by="retry",
                run_id="run-1",
                retry_from_step="CreateCode",
                cached_step_results={"file_validation": {"file_size_bytes": Decimal("42")}},
            )


class TestRegenerationAttemptAcrossCallSites:
    """p32 2.15 / Decision D1 — the regeneration counter is part of this contract.

    ``build_trigger_input`` has 54 callers across four producers
    (``dashboard_api/handlers/pipelines_runs.py``,
    ``dashboard_api/handlers/upload.py``,
    ``agentcore/self_healing_agent/main.py``,
    ``pipeline_trigger/lambda_function.py``), so adding a field is not an
    additive change. :data:`PRODUCER_SHAPES` is every distinct call shape
    those producers build, transcribed from the call sites; the counter is
    exercised against each one.

    Decision D1: legal on the :data:`RERUN_TRIGGERS` flavors only. On any
    other flavor it raises ``ValueError`` naming the field — a fresh run must
    behave identically whether or not the key is present (3.18), so a field
    that may ride along with no effect is rejected rather than ignored.

    **Validates: Requirements 2.15; 3.18, 3.19**
    """

    def _shapes(self):
        return PRODUCER_SHAPES

    def test_every_rerun_call_site_shape_accepts_the_counter(self):
        for shape_id, pipeline, kwargs in self._shapes():
            if kwargs["triggered_by"] not in RERUN_TRIGGERS:
                continue
            out = build_trigger_input(pipeline, **kwargs, regeneration_attempt=1)
            assert out["regeneration_attempt"] == 1, shape_id

    def test_every_fresh_call_site_shape_rejects_the_counter_naming_the_field(self):
        for _shape_id, pipeline, kwargs in self._shapes():
            if kwargs["triggered_by"] in RERUN_TRIGGERS:
                continue
            with pytest.raises(ValueError, match="regeneration_attempt"):
                build_trigger_input(pipeline, **kwargs, regeneration_attempt=1)

    def test_no_call_site_shape_emits_the_counter_unasked(self):
        """3.18: first-attempt behaviour is unchanged — every existing
        producer keeps building an input with no counter in it, so every
        ``event.get("regeneration_attempt", 0)`` reader sees exactly what it
        saw before."""
        for shape_id, pipeline, kwargs in self._shapes():
            out = build_trigger_input(pipeline, **kwargs)
            assert "regeneration_attempt" not in out, shape_id

    def test_the_counter_relaxes_no_existing_rejection(self):
        """3.19: with the counter present, a rerun input still carries the
        full trigger contract and still round-trips through JSON — the counter
        is the only added key."""
        for shape_id, pipeline, kwargs in self._shapes():
            if kwargs["triggered_by"] not in RERUN_TRIGGERS:
                continue
            without = build_trigger_input(pipeline, **kwargs)
            with_counter = build_trigger_input(pipeline, **kwargs, regeneration_attempt=3)
            assert with_counter == {**without, "regeneration_attempt": 3}, shape_id
            if with_counter["source_type"] == "S3_UPLOAD":
                # The R9.1 full-contract keys and the prefix routing flag survive.
                assert with_counter.keys() >= FILE_VALIDATION_PREFIX_KEYS, shape_id
                assert with_counter["trigger_mode"] == "prefix", shape_id
            assert json.loads(json.dumps(with_counter)) == with_counter, shape_id

    def test_jdbc_rerun_carries_the_counter_without_gaining_s3_keys(self):
        """The JDBC rejection is untouched: a JDBC rerun may carry the counter
        and still emits no S3 source location."""
        out = build_trigger_input(JDBC_PIPELINE, triggered_by="self_healing", run_id="r-heal", regeneration_attempt=2)
        assert out["regeneration_attempt"] == 2
        assert not {"source_bucket", "source_key", "source_prefix", "trigger_mode"} & out.keys()

    def test_counter_must_be_a_non_negative_int(self):
        base = {"triggered_by": "self_healing", "run_id": "run-1-heal"}
        for bad in ["1", 1.5, Decimal("1"), -1, True]:
            with pytest.raises(ValueError, match="regeneration_attempt"):
                build_trigger_input(PIPELINE, **base, regeneration_attempt=bad)

    def test_rerun_still_requires_run_id_when_the_counter_is_present(self):
        """3.19: rerun triggers require ``run_id`` — the counter does not
        substitute for run identity."""
        with pytest.raises(ValueError, match="run_id"):
            build_trigger_input(PIPELINE, triggered_by="self_healing", regeneration_attempt=1)


class TestCatalogedValues:
    """The contract's vocabularies match the cataloged codebase reality."""

    def test_trigger_values_are_the_cataloged_set(self):
        assert {"s3_upload", "manual", "manual_upload", "scheduled", "retry", "self_healing"} == TRIGGER_VALUES

    def test_rerun_triggers(self):
        assert {"retry", "self_healing"} == RERUN_TRIGGERS

    def test_source_types(self):
        assert {"S3_UPLOAD", "JDBC"} == SOURCE_TYPES

    def test_task_steps_cover_all_state_row_writers(self):
        # Sanity: the cached-result key space is exactly the ten task steps.
        assert len(TASK_STEPS) == len(STEP_SEQUENCE) == 10
