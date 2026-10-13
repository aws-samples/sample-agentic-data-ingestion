# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A heal that ran is named in a notification (p30 task 2.5).

R14.3's second clause: "a heal that ran SHALL be indicated in the pipeline run
list and in a notification stating what was remediated". These tests pin the
message ``send_self_healing_applied_alert`` builds, and specifically the two
ways it could be wrong:

1. **It must state what was remediated.** A notification saying only
   "self-healing ran" satisfies the letter and none of the point.
2. **It must not claim the pipeline is fixed.** At send time the only thing
   known is that ``start_execution`` succeeded. ``rerun_status`` moves to
   ``success``/``failed`` later, when the re-run's own ``job_completion`` links
   the terminal result back. A message reading "healed" would reintroduce
   exactly the dishonesty the outcome record exists to prevent — and it is the
   kind of wording no assertion catches unless it is asserted directly.

The start-failed case is covered as its own outcome rather than an edge case:
it means no re-run exists at all, and for the OOM path a fix has already been
applied to the Glue job and not rolled back.

The declined-re-run sibling is tests/unit/shared/test_self_healing_declined_notification.py
(task 6.3) and is not re-asserted here. The agent-side wiring is
tests/unit/agents/self_healing_agent/test_heal_applied_notification.py.

**Validates: Requirements 14.3**
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Transcribed from the agent's own remediation descriptions
# (remediate_oom / remediate_by_rerun in agentcore/self_healing_agent/main.py)
# rather than imported, so a drift between the two sides shows up here.
_OOM_FIX = "Upgraded worker type from G.1X to G.2X"
_RERUN_FIX = "Re-running the pipeline regenerates and re-validates the code (schema_mismatch)."


def _send(**overrides) -> tuple[dict, dict]:
    """Call the alert with the notification service captured.

    Returns ``(kwargs the service was called with, the alert's return value)``.
    """
    from notification_service import send_self_healing_applied_alert

    kwargs = {
        "pipeline_id": "pipe-1",
        "run_id": "run-1",
        "error_classification": "oom_error",
        "remediation_action": "upgrade_worker_type",
        "remediation_applied": _OOM_FIX,
        "rerun_started": True,
        "rerun_id": "run-1-heal",
        **overrides,
    }
    with patch("notification_service.notification_service.send_notification") as send:
        send.return_value = {"notification_id": "n-1", "delivery_results": {}, "success": True}
        returned = send_self_healing_applied_alert(**kwargs)
    return send.call_args.kwargs, returned


class TestItStatesWhatWasRemediated:
    def test_the_remediation_description_appears_verbatim(self):
        sent, _ = _send()
        assert _OOM_FIX in sent["message"]

    def test_the_root_cause_and_action_are_named(self):
        sent, _ = _send()
        assert "oom_error" in sent["message"]
        assert "upgrade_worker_type" in sent["message"]

    def test_the_rerun_remediation_description_appears_verbatim(self):
        sent, _ = _send(
            error_classification="schema_mismatch",
            remediation_action="rerun_pipeline",
            remediation_applied=_RERUN_FIX,
        )
        assert _RERUN_FIX in sent["message"]

    def test_a_missing_description_is_reported_as_missing_not_omitted(self):
        sent, _ = _send(remediation_applied="")
        assert "no description recorded" in sent["message"]

    def test_the_pipeline_and_the_healed_run_are_both_named(self):
        sent, _ = _send()
        assert "pipe-1" in sent["message"]
        assert "run-1" in sent["message"]
        assert "pipe-1" in sent["title"]


class TestItDoesNotClaimTheRunIsFixed:
    def test_the_message_says_the_outcome_is_not_known_yet(self):
        sent, _ = _send()
        assert "NOT KNOWN YET" in sent["message"]

    def test_the_message_never_says_healed_or_succeeded(self):
        """The one wording that would make this notification a lie."""
        sent, _ = _send()
        lowered = sent["message"].lower()
        assert "healed" not in lowered
        assert "succeeded" not in lowered
        assert "success" not in lowered

    def test_the_title_claims_a_remediation_not_a_fixed_pipeline(self):
        sent, _ = _send()
        assert "Remediated" in sent["title"]
        assert "healed" not in sent["title"].lower()

    def test_the_lineage_id_is_labelled_as_not_the_reruns_run_id(self):
        """``{original}-heal`` is lineage; file_validation mints a fresh run id."""
        sent, _ = _send()
        assert "run-1-heal" in sent["message"]
        assert "not the re-run's own run id" in sent["message"]

    def test_no_lineage_clause_when_there_is_no_lineage_id(self):
        sent, _ = _send(rerun_id="")
        assert "Heal lineage id" not in sent["message"]


class TestTheReRunNeverStartedIsItsOwnOutcome:
    def test_it_says_no_rerun_exists_and_the_run_is_still_failed(self):
        sent, _ = _send(rerun_started=False, rerun_start_error="ThrottlingException: Rate exceeded")
        assert "could NOT start the re-run" in sent["message"]
        assert "No re-run exists" in sent["message"]
        assert "still failed" in sent["message"]

    def test_it_names_the_reason_the_start_failed(self):
        sent, _ = _send(rerun_started=False, rerun_start_error="ThrottlingException: Rate exceeded")
        assert "ThrottlingException: Rate exceeded" in sent["message"]

    def test_a_missing_error_is_reported_as_missing(self):
        sent, _ = _send(rerun_started=False, rerun_start_error="")
        assert "no error recorded" in sent["message"]

    def test_the_worker_upgrade_is_reported_as_applied_and_not_rolled_back(self):
        """remediate_oom's update_job ran before the re-run was attempted."""
        sent, _ = _send(rerun_started=False, rerun_start_error="boom")
        assert "not been rolled back" in sent["message"]
        assert "job definition has changed" in sent["message"]

    def test_the_rerun_action_reports_no_applied_change(self):
        """For schema/type failures the remediation IS the re-run — nothing was applied."""
        sent, _ = _send(
            rerun_started=False,
            rerun_start_error="boom",
            error_classification="schema_mismatch",
            remediation_action="rerun_pipeline",
            remediation_applied=_RERUN_FIX,
        )
        assert "not been rolled back" not in sent["message"]
        assert "Intended remediation" in sent["message"]

    def test_a_failed_start_is_the_higher_priority_of_the_two(self):
        """Nobody is re-running the failed run, so it needs more attention."""
        started, _ = _send()
        not_started, _ = _send(rerun_started=False, rerun_start_error="boom")
        assert started["priority"].value == "medium"
        assert not_started["priority"].value == "high"


class TestDeliveryContract:
    def test_it_matches_the_declined_alerts_delivery_shape(self):
        """Same channels and the alert_type-in-metadata convention as its sibling."""
        sent, _ = _send()
        assert sent["channels"] == ["sns", "dashboard"]
        assert sent["metadata"]["alert_type"] == "self_healing_applied"
        assert sent["notification_type"] == "self_healing_applied"

    def test_the_two_outcomes_carry_distinct_alert_types(self):
        started, _ = _send()
        not_started, _ = _send(rerun_started=False)
        assert started["metadata"]["alert_type"] == "self_healing_applied"
        assert not_started["metadata"]["alert_type"] == "self_healing_rerun_start_failed"

    def test_metadata_carries_the_fields_a_consumer_would_filter_on(self):
        sent, _ = _send()
        meta = sent["metadata"]
        assert meta["pipeline_id"] == "pipe-1"
        assert meta["run_id"] == "run-1"
        assert meta["rerun_started"] is True
        assert meta["remediation_applied"] == _OOM_FIX
        assert meta["remediation_action"] == "upgrade_worker_type"
        assert meta["error_classification"] == "oom_error"

    def test_an_undelivered_result_is_returned_rather_than_swallowed(self):
        from notification_service import send_self_healing_applied_alert

        with patch("notification_service.notification_service.send_notification") as send:
            send.return_value = {"notification_id": "n-1", "delivery_results": {}, "success": False}
            result = send_self_healing_applied_alert(
                pipeline_id="pipe-1",
                run_id="run-1",
                error_classification="oom_error",
                remediation_action="upgrade_worker_type",
                remediation_applied=_OOM_FIX,
                rerun_started=True,
            )
        assert result["success"] is False
