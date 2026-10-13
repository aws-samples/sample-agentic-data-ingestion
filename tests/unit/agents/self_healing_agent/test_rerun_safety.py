# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Partial_Write_Guard predicate sanity check (p30 task 6.2).

**Scope note for p30 task 6.5:** this file covers ONLY the pure predicate
``evaluate_rerun_safety`` — the allow-list, the target-type normalisation, and
the two fail-closed readings 6.2 had to decide. It deliberately does NOT cover
the handler-level cases 6.5 owns (append target → no re-run / classified /
reason surfaced; overwrite target → behaviour unchanged; Heal_Guard still one
heal per run). Those need the handler, not the predicate.

The cases here exist because each one is a place where a plausible
implementation would have been wrong in the UNSAFE direction:

* ``glue_catalog`` + ``overwrite`` — design §5 O13's rejected fallback.
* the uppercase wizard spellings — a bare ``.lower()`` matches none of them.
* ``"Overwrite"`` — the templates compare ``write_mode`` case-sensitively.

**Validates: Requirements 15.2, 15.4**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: MagicMock()
_mock_boto3.resource.return_value = MagicMock()

_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("sha_safety_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_safety_main"] = sha
    _spec.loader.exec_module(sha)


def _record(target_type, write_mode):
    tc = {}
    if target_type is not None:
        tc["type"] = target_type
    if write_mode is not None:
        tc["write_mode"] = write_mode
    return {"pipeline_id": "pipe-1", "target_config": tc}


class TestSafeSet:
    """The four combinations task 6.1's matrix established as safe."""

    @pytest.mark.parametrize(
        "target_type,write_mode",
        [
            ("NEW_S3_TABLE_BUCKET", "overwrite"),
            ("EXISTING_S3_TABLE_BUCKET", "merge"),
            ("s3_tables", "overwrite"),
            ("JDBC", "overwrite"),
            ("RDS_DATABASE", "overwrite"),  # legacy alias folds into jdbc
            ("S3_PARQUET", "overwrite"),
        ],
    )
    def test_safe_combination_permits_rerun(self, target_type, write_mode):
        verdict = sha.evaluate_rerun_safety(_record(target_type, write_mode))
        assert verdict["safe"] is True
        assert verdict["write_mode"] == write_mode

    def test_every_wizard_persisted_spelling_normalises(self):
        """A bare ``.lower()`` of the persisted type matches none of the S3
        Tables spellings, which would fail closed on the DEFAULT target type
        and switch self-healing off for the common case."""
        for spelling in (
            "NEW_S3_TABLE_BUCKET",
            "EXISTING_S3_TABLE_BUCKET",
            "GLUE_CATALOG",
            "JDBC",
            "RDS_DATABASE",
            "S3_PARQUET",
        ):
            verdict = sha.evaluate_rerun_safety(_record(spelling, "overwrite"))
            assert verdict["target_type"], f"{spelling} did not normalise to a known target"


class TestUnsafeSet:
    """Everything else declines, including the two look-safe traps."""

    @pytest.mark.parametrize(
        "target_type,write_mode",
        [
            ("GLUE_CATALOG", "overwrite"),  # write_mode is IGNORED by the template
            ("GLUE_CATALOG", "append"),
            ("GLUE_CATALOG", "merge"),
            ("NEW_S3_TABLE_BUCKET", "append"),  # atomic, but commit-then-fail
            ("JDBC", "append"),
            ("JDBC", "merge"),  # renders as append
            ("S3_PARQUET", "append"),
            ("S3_PARQUET", "merge"),  # renders as overwrite — destructive
        ],
    )
    def test_unsafe_combination_declines_rerun(self, target_type, write_mode):
        verdict = sha.evaluate_rerun_safety(_record(target_type, write_mode))
        assert verdict["safe"] is False
        assert verdict["reason"]

    def test_unrecognised_write_mode_fails_closed(self):
        """``edit_pipeline`` merges target_config keys with no validation, so
        any string can be persisted — including the vestigial ``upsert``, which
        reads as safe and renders as append on JDBC."""
        for mode in ("upsert", "APPEND", "", "sideways"):
            verdict = sha.evaluate_rerun_safety(_record("JDBC", mode))
            assert verdict["safe"] is False, f"write_mode {mode!r} was graded safe"

    def test_write_mode_is_compared_case_sensitively(self):
        """The templates test ``write_mode`` with exact-match Jinja2 equality,
        so ``"Overwrite"`` took the append branch at render time. Lower-casing
        it here would grade a duplicating write as a safe clobber."""
        verdict = sha.evaluate_rerun_safety(_record("JDBC", "Overwrite"))
        assert verdict["safe"] is False
        assert verdict["write_mode_persisted"] == "Overwrite"

    def test_unknown_target_type_fails_closed(self):
        verdict = sha.evaluate_rerun_safety(_record("DYNAMODB", "overwrite"))
        assert verdict["safe"] is False
        assert verdict["target_type"] == ""
        assert verdict["target_type_persisted"] == "DYNAMODB"

    @pytest.mark.parametrize("record", [{}, {"target_config": None}, {"target_config": {}}])
    def test_absent_target_config_fails_closed(self, record):
        verdict = sha.evaluate_rerun_safety(record)
        assert verdict["safe"] is False

    def test_absent_write_mode_fails_closed(self):
        """Not merely conservative: an absent write_mode renders as the
        ``append`` default, and append duplicates on all four targets."""
        verdict = sha.evaluate_rerun_safety(_record("NEW_S3_TABLE_BUCKET", None))
        assert verdict["safe"] is False
        assert verdict["write_mode"] == ""

    def test_non_string_write_mode_fails_closed_and_is_still_named(self):
        verdict = sha.evaluate_rerun_safety(_record("JDBC", 7))
        assert verdict["safe"] is False
        assert verdict["write_mode"] == ""
        assert verdict["write_mode_persisted"] == "7"


class TestVerdictHonesty:
    """R15.3's reason must not claim a partial write was DETECTED — no such
    signal reaches this decision point."""

    def test_decline_reason_says_may_have_written_rows(self):
        verdict = sha.evaluate_rerun_safety(_record("GLUE_CATALOG", "overwrite"))
        assert "may have written rows" in verdict["reason"]
        assert "non-SUCCEEDED" in verdict["reason"]

    def test_decline_reason_never_claims_detection(self):
        for target_type, write_mode in (("GLUE_CATALOG", "append"), ("JDBC", "append"), ("DYNAMODB", "overwrite")):
            reason = sha.evaluate_rerun_safety(_record(target_type, write_mode))["reason"]
            lowered = reason.lower()
            assert "detected" not in lowered
            assert "partial write was" not in lowered

    def test_verdict_names_the_mode_it_graded(self):
        """``write_mode`` is render-time only, so the record's mode may not be
        what the failed run did — the verdict must name what it graded."""
        verdict = sha.evaluate_rerun_safety(_record("JDBC", "append"))
        assert verdict["target_type"] == "jdbc"
        assert verdict["write_mode"] == "append"
        assert "'append'" in verdict["reason"]

    def test_source_type_does_not_change_the_verdict(self):
        """The gate is keyed on 2 fields, not 6.1's stated 3-tuple: both source
        types emit the same write statements, and the one source-dependent
        modifier bears on a combination already outside the allow-list."""
        for source_type in ("S3_UPLOAD", "JDBC"):
            record = _record("NEW_S3_TABLE_BUCKET", "append")
            record["source_type"] = source_type
            assert sha.evaluate_rerun_safety(record)["safe"] is False
        for source_type in ("S3_UPLOAD", "JDBC"):
            record = _record("NEW_S3_TABLE_BUCKET", "merge")
            record["source_type"] = source_type
            assert sha.evaluate_rerun_safety(record)["safe"] is True
