# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The declined-re-run notification names the decision (P30 task 6.3).

R15.3: when self-healing declines to re-run because the target may hold rows
from the failed run, the user-facing notification must SAY so rather than read
as a generic "flagged for review". These tests pin the two honesty constraints
in the message ``send_self_healing_declined_alert`` builds:

1. it never claims a partial write was DETECTED — no such signal exists at the
   agent's decision point, so the verdict's "may have written rows" /
   "non-SUCCEEDED" wording is reproduced verbatim;
2. it names the write mode that was graded AND the mode as persisted, because
   ``write_mode`` is applied at code-generation time only.

Plus the OOM case: a worker-type upgrade is applied BEFORE the re-run decision,
so a notification that said only "declined to re-run" would hide a live
mutation to the Glue job.

The guard predicate itself is covered by
tests/unit/agents/self_healing_agent/test_rerun_safety.py (task 6.2); the
handler-level guard behaviour is task 6.5.

**Validates: Requirements 15.3**
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# The reason text the agent's evaluate_rerun_safety produces for an unsafe
# (target, mode) pair — reproduced here rather than imported so this test pins
# the notification's treatment of it, not the agent's wording.
_UNSAFE_REASON = (
    "Re-run declined: a glue_catalog target with write_mode 'overwrite' duplicates or "
    "corrupts data on re-run, and the failed run may have written rows. No partial-write "
    "signal exists at this decision point, so any non-SUCCEEDED terminal state is treated "
    "as 'may have written rows'."
)

_VERDICT = {
    "safe": False,
    "target_type": "glue_catalog",
    "target_type_persisted": "GLUE_CATALOG",
    "write_mode": "overwrite",
    "write_mode_persisted": "overwrite",
    "reason": _UNSAFE_REASON,
}


def _send(**overrides) -> tuple[dict, dict]:
    """Call the alert with the notification service captured.

    Returns ``(kwargs the service was called with, the alert's return value)``.
    """
    from notification_service import send_self_healing_declined_alert

    kwargs = {
        "pipeline_id": "pipe-1",
        "run_id": "run-1",
        "rerun_safety": _VERDICT,
        "declined_action": "rerun_pipeline",
        **overrides,
    }
    with patch("notification_service.notification_service.send_notification") as send:
        send.return_value = {"notification_id": "n-1", "delivery_results": {}, "success": True}
        returned = send_self_healing_declined_alert(**kwargs)
    return send.call_args.kwargs, returned


class TestNamesTheDecision:
    """R15.3: not a generic "flagged for review"."""

    def test_reason_appears_verbatim_in_the_message(self):
        sent, _ = _send()
        assert _UNSAFE_REASON in sent["message"]

    def test_title_names_the_declined_rerun_and_the_pipeline(self):
        sent, _ = _send()
        assert "Declined Re-run" in sent["title"]
        assert "pipe-1" in sent["title"]

    def test_message_names_the_declined_remediation_and_the_run(self):
        sent, _ = _send(declined_action="upgrade_worker_type", applied_fix="x")
        assert "upgrade_worker_type" in sent["message"]
        assert "run-1" in sent["message"]

    def test_metadata_carries_the_guard_skip_reason_and_alert_type(self):
        sent, _ = _send()
        assert sent["metadata"]["skip_reason"] == "partial_write_guard"
        assert sent["metadata"]["alert_type"] == "self_healing_rerun_declined"

    def test_matches_the_pipeline_failure_alert_delivery_shape(self):
        """Same channels/priority as the existing failure alert it mirrors."""
        from notification_service import NotificationPriority

        sent, _ = _send()
        assert sent["channels"] == ["sns", "dashboard"]
        assert sent["priority"] is NotificationPriority.HIGH

    def test_returns_the_services_delivery_result(self):
        _, returned = _send()
        assert returned["success"] is True
        assert returned["notification_id"] == "n-1"


class TestNeverClaimsDetection:
    """Honesty constraint 1 — no partial-write signal exists."""

    def test_says_the_run_may_have_written_rows(self):
        sent, _ = _send()
        assert "may have written rows" in sent["message"]

    def test_names_the_no_signal_rule(self):
        sent, _ = _send()
        assert "non-SUCCEEDED" in sent["message"]

    def test_never_says_detected(self):
        sent, _ = _send(declined_action="upgrade_worker_type", applied_fix="Upgraded worker type")
        assert "detect" not in sent["message"].lower()
        assert "detect" not in sent["title"].lower()


class TestNamesTheModeThatWasGraded:
    """Honesty constraint 2 — write_mode is render-time only."""

    def test_names_both_the_graded_and_persisted_write_mode(self):
        verdict = dict(_VERDICT, write_mode="", write_mode_persisted="Overwrite")
        sent, _ = _send(rerun_safety=verdict)
        # The persisted spelling is what makes a mismatch diagnosable.
        assert "'Overwrite'" in sent["message"]
        assert sent["metadata"]["write_mode"] == ""
        assert sent["metadata"]["write_mode_persisted"] == "Overwrite"

    def test_names_both_the_graded_and_persisted_target_type(self):
        sent, _ = _send()
        assert "glue_catalog" in sent["message"]
        assert "'GLUE_CATALOG'" in sent["message"]

    def test_says_the_mode_is_applied_at_code_generation_time(self):
        sent, _ = _send()
        assert "generated" in sent["message"]

    def test_unrecognised_target_is_labelled_not_blank(self):
        verdict = dict(_VERDICT, target_type="", target_type_persisted="Some Prose")
        sent, _ = _send(rerun_safety=verdict)
        assert "unrecognised" in sent["message"]
        assert "'Some Prose'" in sent["message"]

    def test_missing_verdict_fields_do_not_raise(self):
        sent, _ = _send(rerun_safety={})
        assert isinstance(sent["message"], str)


class TestAlreadyAppliedFix:
    """The OOM path mutates the Glue job BEFORE the gate."""

    def test_applied_fix_is_reported_as_not_undone(self):
        sent, _ = _send(
            declined_action="upgrade_worker_type",
            applied_fix="Upgraded worker type from G.1X to G.2X",
        )
        assert "Upgraded worker type from G.1X to G.2X" in sent["message"]
        assert "ALREADY APPLIED" in sent["message"]
        assert "does not roll it back" in sent["message"]
        assert sent["metadata"]["applied_fix"] == "Upgraded worker type from G.1X to G.2X"

    def test_no_applied_fix_clause_when_the_remediation_was_the_rerun(self):
        sent, _ = _send(declined_action="rerun_pipeline", applied_fix="")
        assert "ALREADY APPLIED" not in sent["message"]
        assert sent["metadata"]["applied_fix"] == ""


class TestDeliveryFailureIsReported:
    def test_unsuccessful_delivery_is_returned_not_swallowed(self):
        from notification_service import send_self_healing_declined_alert

        with patch("notification_service.notification_service.send_notification") as send:
            send.return_value = {
                "notification_id": "n-2",
                "delivery_results": {"sns": {"success": False, "error": "no topic"}},
                "success": False,
            }
            result = send_self_healing_declined_alert(
                pipeline_id="pipe-1",
                run_id="run-1",
                rerun_safety=_VERDICT,
                declined_action="rerun_pipeline",
            )
        assert result["success"] is False
        assert result["delivery_results"]["sns"]["success"] is False

    def test_channel_registry_untouched(self):
        """The alert adds a caller, not a channel (task 3.12 owns channels)."""
        import notification_service as ns

        assert set(ns._CHANNEL_REGISTRY) == {"sns", "dashboard", "slack", "cloudwatch"}
        assert isinstance(ns.notification_service, ns.NotificationService)
