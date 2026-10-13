# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Partial_Write_Guard exemption reasons (p30 task 6.4).

**Scope note.** This file covers ONLY the recorded rationale for the allow-list:
that every exemption has its own reason, that each reason survives the paraphrase
traps task 6.1 flagged, and that the two deliberate non-exemptions stay recorded
and stay out of the safe set. The predicate itself (normalisation, fail-closed
readings, case sensitivity) is task 6.2's ``test_rerun_safety.py``; the
handler-level behaviour (append target → no re-run / classified / reason
surfaced; overwrite target → unchanged; Heal_Guard one heal per run) is task
6.5's and is not touched here.

Each assertion below is a place where an exemption note would be FALSE if it
paraphrased the mechanism:

* ``merge`` attributed to snapshot isolation or to Iceberg being transactional —
  it is the MERGE statement that converges, and ``append`` is just as atomic yet
  deliberately not exempt.
* the two clobber exemptions read as non-destructive — the table is dropped /
  the path is deleted mid-run.
* the S3 Parquet clobber read as partition-scoped — every partition is replaced.

**Validates: Requirements 15.5**
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
    _spec = importlib.util.spec_from_file_location("sha_exemption_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_exemption_main"] = sha
    _spec.loader.exec_module(sha)


def _reason(target_type: str, write_mode: str) -> str:
    verdict = sha.evaluate_rerun_safety({"target_config": {"type": target_type, "write_mode": write_mode}})
    assert verdict["safe"] is True, f"({target_type}, {write_mode}) is not in the safe set"
    return verdict["reason"]


class TestEveryExemptionHasItsOwnReason:
    """R15.5: an exemption without a recorded reason is not auditable."""

    def test_reasons_cover_the_allow_list_exactly(self):
        """Pins the coupling: a new exemption cannot be added to the allow-list
        without stating why, and a reason cannot outlive its exemption."""
        assert set(sha._SAFE_RERUN_REASONS) == set(sha._SAFE_TO_RERUN)

    def test_each_exemption_reason_is_distinct(self):
        """A generic 'in the safe set' line for all four would satisfy the
        predicate and fail the requirement."""
        reasons = [_reason(t, m) for t, m in sorted(sha._SAFE_TO_RERUN)]
        assert len(set(reasons)) == len(reasons)

    def test_no_verdict_falls_back_to_the_unaudited_reason(self):
        for target_type, write_mode in sorted(sha._SAFE_TO_RERUN):
            assert "unaudited" not in _reason(target_type, write_mode)


class TestSnapshotIsolationIsNotCollapsed:
    """R15.5's own example is the easiest reason to state wrongly: snapshot
    isolation is why ``overwrite`` is safe and is NOT why ``merge`` is safe."""

    def test_merge_reason_credits_the_statement_not_the_table_format(self):
        reason = _reason("s3_tables", "merge").lower()
        assert "merge statement" in reason
        assert "idempotent" in reason
        # Both mechanisms are named only to be DENIED — the reason must not
        # credit either of them.
        assert "not of iceberg being transactional" in reason
        assert "not of snapshot isolation" in reason

    def test_merge_reason_names_append_as_equally_atomic_yet_not_exempt(self):
        """The distinction that makes the merge reason true: atomicity is not
        what earns the exemption, or ``append`` would have it too."""
        reason = _reason("s3_tables", "merge").lower()
        assert "append" in reason
        assert "not exempt" in reason

    def test_overwrite_reason_is_the_one_that_credits_snapshot_isolation(self):
        reason = _reason("s3_tables", "overwrite").lower()
        assert "snapshot isolation means a killed job leaves no visible rows" in reason
        assert "createorreplace" in reason
        assert "not why 'merge' is exempt" in reason


class TestClobberSemanticsAreSurfaced:
    """The two clobber exemptions converge on the right END state; they destroy
    the target on the way. An audit note that says only 'safe to re-run' is
    true but misleading."""

    @pytest.mark.parametrize("target_type", ["jdbc", "s3_parquet"])
    def test_clobber_reason_says_safe_means_convergence_not_preservation(self, target_type):
        reason = _reason(target_type, "overwrite").lower()
        assert "clobber" in reason
        assert "end state converges" in reason
        assert "not that nothing was destroyed" in reason

    def test_jdbc_reason_denies_transactionality_and_names_the_gap(self):
        reason = _reason("jdbc", "overwrite").lower()
        assert "not transactional" in reason
        assert "absent mid-run" in reason
        assert "truncate" in reason

    def test_s3_parquet_reason_names_the_blast_radius(self):
        """``partitionOverwriteMode`` is unset, so the re-run replaces every
        partition ever written, not just the ones this run touched."""
        reason = _reason("s3_parquet", "overwrite")
        assert "partitionOverwriteMode" in reason
        assert "EVERY partition" in reason
        assert "not only the ones this run touched" in reason

    @pytest.mark.parametrize("write_mode", ["merge", "overwrite"])
    def test_iceberg_reasons_do_not_carry_the_clobber_caveat(self, write_mode):
        """Adding it to all four would be the paraphrase error in the other
        direction: an Iceberg commit destroys nothing visible mid-run."""
        reason = _reason("s3_tables", write_mode).lower()
        assert "clobber" not in reason
        assert "destroyed" not in reason


class TestDeliberateNonExemptions:
    """A recorded near-miss is as useful as a recorded exemption — the next
    reader of the allow-list will ask about exactly these two."""

    def test_non_exemptions_are_disjoint_from_the_allow_list(self):
        """So neither can be quietly promoted while its 'not exempt' rationale
        still stands."""
        assert set(sha._NOT_EXEMPT_REASONS) & set(sha._SAFE_TO_RERUN) == set()

    def test_s3_tables_append_non_exemption_names_the_window(self):
        reason = sha._NOT_EXEMPT_REASONS[("s3_tables", "append")]
        lowered = reason.lower()
        assert "commit-then-fail" in lowered
        assert "no partial rows are possible" in lowered
        assert "watermark" in lowered
        assert "did not advance" in lowered
        assert "judgement" in lowered

    def test_glue_catalog_overwrite_non_exemption_names_the_o13_trap(self):
        reason = sha._NOT_EXEMPT_REASONS[("glue_catalog", "overwrite")]
        lowered = reason.lower()
        assert "ignores write_mode" in lowered
        assert "from_catalog" in lowered
        assert "o13" in lowered
        assert "unsafe direction" in lowered

    def test_non_exemption_rationale_is_not_spliced_into_the_decline_reason(self):
        """p30 task 6.3 reproduces the decline reason verbatim in the user-facing
        alert and pinned its wording; this rationale is the audit record for the
        omission, not the message about it."""
        for target_type, write_mode in sorted(sha._NOT_EXEMPT_REASONS):
            verdict = sha.evaluate_rerun_safety({"target_config": {"type": target_type, "write_mode": write_mode}})
            assert verdict["safe"] is False
            assert sha._NOT_EXEMPT_REASONS[(target_type, write_mode)] not in verdict["reason"]
