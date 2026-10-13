# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgenticDataIngestion Approval Timeout Handler

This Lambda function handles approval timeouts by:
1. Scanning for expired approval requests
2. Processing timeouts based on pipeline configuration
3. Sending timeout notifications and alerts
4. Recording timeout metrics for monitoring
"""

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add shared directory to path
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from eventbridge_utils import get_eventbridge_publishers

publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event = (
    get_eventbridge_publishers()
)

from env_utils import require_env
from sfn_token_errors import is_token_consumed_error

try:
    from metrics_facade import ApprovalMetrics, record_error, time_function
    from notification_service import (
        NotificationChannel,
        NotificationPriority,
        notification_service,
        send_approval_timeout_alert,
    )
except ImportError:
    # Fallback if shared modules not available
    class ApprovalMetrics:
        @staticmethod
        def record_approval_timeout(*args, **kwargs):
            pass

        @staticmethod
        def record_notification_sent(*args, **kwargs):
            pass

    def record_error(*args, **kwargs):
        pass

    def time_function(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def send_approval_timeout_alert(*args, **kwargs):
        return {"success": False}

    class NotificationPriority:
        CRITICAL = "critical"

    class NotificationChannel:
        SNS = "sns"
        DASHBOARD = "dashboard"

    class notification_service:
        @staticmethod
        def send_notification(*args, **kwargs):
            return {"success": False}


# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Environment variables
APPROVALS_TABLE = require_env("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
TASK_TOKENS_TABLE = require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
CLOUDWATCH_ALARM_TOPIC = os.environ.get("CLOUDWATCH_ALARM_TOPIC")

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
stepfunctions = boto3.client("stepfunctions")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")

approvals_table = dynamodb.Table(APPROVALS_TABLE)
task_tokens_table = dynamodb.Table(TASK_TOKENS_TABLE)


@time_function("ApprovalTimeoutHandler.ScanExpiredApprovals")
def scan_expired_approvals() -> list[dict[str, Any]]:
    """Scan for approval requests that have exceeded their timeout"""
    try:
        current_time = datetime.now(UTC).isoformat()

        # Scan for pending approvals that have timed out
        response = approvals_table.scan(
            FilterExpression="#status = :status AND timeout_at < :current_time",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "pending", ":current_time": current_time},
        )

        expired_approvals = response.get("Items", [])
        logger.info(f"Found {len(expired_approvals)} expired approvals")

        return expired_approvals

    except Exception as e:
        logger.error(f"Error scanning for expired approvals: {e}")
        record_error("ScanError", "scan_expired_approvals", str(e))
        return []


@time_function("ApprovalTimeoutHandler.ProcessTimeout")
def process_approval_timeout(approval_record: dict[str, Any]) -> bool:
    """Process a single approval timeout based on configuration"""
    try:
        approval_id = approval_record["approval_id"]
        pipeline_id = approval_record["pipeline_id"]
        approval_type = approval_record["approval_type"]
        timeout_behavior = approval_record.get("timeout_behavior", "manual")

        logger.info(f"Processing timeout for approval {approval_id} with behavior: {timeout_behavior}")

        # Record timeout metrics
        ApprovalMetrics.record_approval_timeout(
            approval_type=approval_type, pipeline_id=pipeline_id, timeout_behavior=timeout_behavior
        )

        # Process based on timeout behavior
        #
        # ── p47 R6 — RATIFIED ARM B: THE TWO AUTO BRANCHES ARE UNREACHED ─────
        # `manual` is the ONLY branch any deployment takes today, and the other
        # two are no longer advertised as configurable (see the p47 R6 note in
        # approval_handler/lambda_function.py's `get_pipeline_config`). This is
        # NOT dead code and MUST NOT be deleted as such — read on before
        # touching it.
        #
        # WHY THEY ARE UNREACHED — two independent blockers, either alone
        # sufficient:
        #   1. NO SWEEP CAN ARRIVE IN TIME. `LGR-69ca78b-2`'s revisit_when,
        #      verbatim: "Both are currently unreachable — Step Functions times
        #      out at exactly 72h while the sweeper only runs every 15 minutes,
        #      so it always arrives to find a consumed token. Making them
        #      reachable requires the advertised window strictly BELOW the gate,
        #      which reverses the equality recorded here." The window and the
        #      gate are equal by decision: APPROVAL_WINDOW_HOURS = 72
        #      (shared/config.py) mirrors APPROVAL_TIMEOUT_SECONDS = 259200
        #      (deploy-cdk/lib/orchestration-stack.ts), and
        #      tests/contracts/test_approval_window_cross_language.py asserts
        #      that equality by PARSING the TypeScript rather than restating it.
        #   2. NOTHING CAN SET `timeout_behavior`. Its sole writer is
        #      approval_handler/lambda_function.py:411, whose input chain
        #      terminates in a hardcoded "manual" literal or in a state-row
        #      `pipeline_config` attribute that has no producer. So no code path
        #      can put any other value on an approval record.
        #
        # WHY THIS CODE STAYS ANYWAY — the selector is a PERSISTED DynamoDB
        # attribute, not a code path, so the branch is UNREACHED-BY-DATA and not
        # unreachable-by-construction. That a value cannot be *written* is a
        # weaker claim than that a row cannot *hold* one, and the second was not
        # established. Deleting these branches would silently reroute any such
        # row to `manual_timeout_handling`, which leaves the approval pending
        # and alarms — for a token-carrying approval that means the execution
        # keeps waiting until the 72 h gate abandons it, rather than resuming.
        # The enforcement here is also complete and carries a ratified ordering
        # contract (P28b R3.2 Send_Before_Status, via `_send_timeout_callback`),
        # which is what distinguishes this from `LGR-eee8ac8-6`'s inert
        # `execution_config` keys: those were deleted because honouring them
        # "would mean building enforcement". Here the enforcement exists; only a
        # producer for its input is missing, which is the opposite shape.
        #
        # WHAT WOULD MAKE THEM REACHABLE: both blockers, together. A producer
        # for `timeout_behavior`, AND the advertised window moved strictly below
        # the gate by more than one 15-minute sweeper period with headroom. That
        # second half reverses `LGR-69ca78b-2` and so needs a superseding record
        # (`LGR-60c4985-6`: the reversal must stay legible as a reversal), not an
        # edit. Fixing only one blocker leaves the feature inert while making it
        # look wired.
        #
        # An unrecognised value falls to `manual` on purpose — manual is the
        # only branch that consumes no task token — and that is pinned by
        # tests/unit/lambdas/approval_timeout/test_timeout_sweep_dispatch.py
        # (absent, unrecognised, and all three declared values).
        if timeout_behavior == "auto_approve":
            return auto_approve_timeout(approval_record)
        elif timeout_behavior == "auto_reject":
            return auto_reject_timeout(approval_record)
        else:  # manual
            return manual_timeout_handling(approval_record)

    except Exception as e:
        logger.error(f"Error processing timeout for {approval_record.get('approval_id')}: {e}")
        record_error("TimeoutProcessingError", "process_approval_timeout", str(e))
        return False


def _mark_approval_expired(approval_id: str) -> None:
    """Mark an approval "expired" — never approved/rejected (P28b R3.5).

    Used when the task token was already consumed/expired, or when the token
    record is gone while the approval claims has_task_token: the execution is
    no longer waiting on this approval, so a timeout outcome must not be
    written.
    """
    approvals_table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #status = :status, expired_at = :expired_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "expired",
            ":expired_at": datetime.now(UTC).isoformat(),
        },
    )


def _send_timeout_callback(approval_record: dict[str, Any], decision: str) -> str:
    """Send the Step Functions task callback for a timed-out approval.

    Send_Before_Status ordering (P28b R3.2, mirroring p28 task 3.1 / the
    approval_handler): tokens live ONLY in the dedicated task-tokens table
    (the approval record carries just has_task_token — the old
    approval_record.get("task_token") read never found one, so this path
    never sent at all). Read token → send → delete token record; the caller
    writes the timeout outcome status only after "ok".

    Returns:
        "ok"      — callback delivered and token record deleted (or the
                    approval never had a token): write the outcome status.
        "expired" — already-processed (consumed/expired token, or token
                    record absent while has_task_token): approval marked
                    "expired"; never write approved/rejected (R3.5).
        "failed"  — transient send failure: nothing written, token record
                    retained; report the item failed so the next scheduled
                    sweep retries (timeout_at is still past) (R3.4).
    """
    approval_id = approval_record["approval_id"]

    if not approval_record.get("has_task_token"):
        return "ok"  # no token to consume — write the status directly

    token_item = task_tokens_table.get_item(Key={"approval_id": approval_id}).get("Item")
    if not token_item:
        # Token record TTL-reaped or already deleted: the callback can never
        # be delivered — already-processed, not a retry (R3.5).
        logger.warning(f"Task token record absent for approval {approval_id} (has_task_token) — marking expired")
        _mark_approval_expired(approval_id)
        return "expired"

    task_token = token_item["task_token"]
    try:
        if decision == "approved":
            stepfunctions.send_task_success(
                taskToken=task_token,
                output=json.dumps(
                    {
                        "approval_decision": "approved",
                        "approval_id": approval_id,
                        "timeout_auto_approved": True,
                        "processed_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        else:
            stepfunctions.send_task_failure(
                taskToken=task_token,
                error="ApprovalTimeout",
                cause=f"Approval {approval_id} timed out and was auto-rejected",
            )
    except ClientError as send_err:
        if is_token_consumed_error(send_err):
            # Consumed/expired token (P28 R2.3 set): the execution is no
            # longer waiting on this approval — clean up and mark "expired",
            # never approved/rejected. Already-processed, not a retry (R3.5).
            error_code = send_err.response.get("Error", {}).get("Code", "")
            logger.warning(f"Task token for approval {approval_id} already consumed or expired ({error_code})")
            task_tokens_table.delete_item(Key={"approval_id": approval_id})
            _mark_approval_expired(approval_id)
            return "expired"
        # Transient failure (throttle, service 5xx): keep the token record
        # and the pending status intact so the next scheduled sweep retries
        # this timeout (R3.4).
        logger.error(f"Transient failure sending task callback for approval {approval_id}: {send_err}")
        return "failed"

    # Send confirmed — the token is consumed; delete its record now.
    task_tokens_table.delete_item(Key={"approval_id": approval_id})
    return "ok"


def auto_approve_timeout(approval_record: dict[str, Any]) -> bool:
    """Automatically approve timed out approval.

    Send_Before_Status ordering (P28b R3.2): the task callback is sent
    BEFORE the timeout outcome status is written, so a failed send can never
    leave a record claiming a resume the execution never reached.
    """
    try:
        approval_id = approval_record["approval_id"]

        # Send the task callback BEFORE any write
        send_outcome = _send_timeout_callback(approval_record, "approved")
        if send_outcome == "expired":
            logger.info(f"Approval {approval_id} already processed (expired) — skipping auto-approve write")
            return True
        if send_outcome == "failed":
            # Status stays "pending"; the item is reported failed in the
            # processing result and the next scheduled sweep retries (R3.4).
            return False

        # Write the timeout outcome status (only after a successful send)
        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression="SET #status = :status, processed_at = :processed_at, processed_by = :processed_by, timeout_reason = :reason",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "approved",
                ":processed_at": datetime.now(UTC).isoformat(),
                ":processed_by": "system_timeout",
                ":reason": "auto_approved_on_timeout",
            },
        )

        # Send notification
        send_timeout_notification(approval_record, "auto_approved")

        # Publish event
        publish_approval_status_change(
            approval_id=approval_id,
            pipeline_id=approval_record["pipeline_id"],
            approval_type=approval_record["approval_type"],
            status="approved",
            run_id=approval_record["run_id"],
            execution_id=approval_record.get("execution_arn"),
            approver="system_timeout",
            decision_details={"timeout_auto_approved": True, "processed_at": datetime.now(UTC).isoformat()},
        )

        logger.info(f"Auto-approved timed out approval: {approval_id}")
        return True

    except Exception as e:
        logger.error(f"Error auto-approving timeout: {e}")
        return False


def auto_reject_timeout(approval_record: dict[str, Any]) -> bool:
    """Automatically reject timed out approval.

    Send_Before_Status ordering (P28b R3.2): the task callback is sent
    BEFORE the timeout outcome status is written, so a failed send can never
    leave a record claiming a rejection the execution never received.
    """
    try:
        approval_id = approval_record["approval_id"]

        # Send the task callback BEFORE any write
        send_outcome = _send_timeout_callback(approval_record, "rejected")
        if send_outcome == "expired":
            logger.info(f"Approval {approval_id} already processed (expired) — skipping auto-reject write")
            return True
        if send_outcome == "failed":
            # Status stays "pending"; the item is reported failed in the
            # processing result and the next scheduled sweep retries (R3.4).
            return False

        # Write the timeout outcome status (only after a successful send)
        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression="SET #status = :status, processed_at = :processed_at, processed_by = :processed_by, timeout_reason = :reason",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "rejected",
                ":processed_at": datetime.now(UTC).isoformat(),
                ":processed_by": "system_timeout",
                ":reason": "auto_rejected_on_timeout",
            },
        )

        # Send notification
        send_timeout_notification(approval_record, "auto_rejected")

        # Publish event
        publish_approval_status_change(
            approval_id=approval_id,
            pipeline_id=approval_record["pipeline_id"],
            approval_type=approval_record["approval_type"],
            status="rejected",
            run_id=approval_record["run_id"],
            execution_id=approval_record.get("execution_arn"),
            approver="system_timeout",
            decision_details={"timeout_auto_rejected": True, "processed_at": datetime.now(UTC).isoformat()},
        )

        logger.info(f"Auto-rejected timed out approval: {approval_id}")
        return True

    except Exception as e:
        logger.error(f"Error auto-rejecting timeout: {e}")
        return False


def manual_timeout_handling(approval_record: dict[str, Any]) -> bool:
    """Handle manual timeout - mark as timed out but keep pending"""
    try:
        approval_id = approval_record["approval_id"]

        # Update approval status to indicate timeout
        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression="SET timeout_occurred = :timeout_occurred, timeout_notified_at = :notified_at",
            ExpressionAttributeValues={":timeout_occurred": True, ":notified_at": datetime.now(UTC).isoformat()},
        )

        # Send escalation notification
        send_timeout_notification(approval_record, "manual_escalation")

        # Create CloudWatch alarm for manual intervention
        create_timeout_alarm(approval_record)

        logger.info(f"Marked approval as timed out for manual handling: {approval_id}")
        return True

    except Exception as e:
        logger.error(f"Error handling manual timeout: {e}")
        return False


def send_timeout_notification(approval_record: dict[str, Any], timeout_action: str):
    """Send timeout notification using unified notification service"""
    try:
        approval_id = approval_record["approval_id"]
        pipeline_id = approval_record["pipeline_id"]
        approval_type = approval_record["approval_type"]

        if timeout_action == "auto_approved":
            # Send informational notification for auto-approval
            notification_result = notification_service.send_notification(
                notification_type="approval_auto_approved",
                title=f"AgenticDataIngestion Approval Auto-Approved: {approval_type}",
                message=f"""
Approval request was automatically approved due to timeout.

Pipeline: {pipeline_id}
Approval Type: {approval_type}
Approval ID: {approval_id}
Time: {datetime.now(UTC).isoformat()}

The pipeline will continue execution automatically.
                """.strip(),
                priority=NotificationPriority.MEDIUM,
                channels=[NotificationChannel.SNS, NotificationChannel.DASHBOARD],
                metadata={
                    "approval_id": approval_id,
                    "pipeline_id": pipeline_id,
                    "approval_type": approval_type,
                    "timeout_action": timeout_action,
                },
            )

        elif timeout_action == "auto_rejected":
            # Send high priority notification for auto-rejection
            notification_result = notification_service.send_notification(
                notification_type="approval_auto_rejected",
                title=f"AgenticDataIngestion Approval Auto-Rejected: {approval_type}",
                message=f"""
Approval request was automatically rejected due to timeout.

Pipeline: {pipeline_id}
Approval Type: {approval_type}
Approval ID: {approval_id}
Time: {datetime.now(UTC).isoformat()}

The pipeline execution has been halted.
                """.strip(),
                priority=NotificationPriority.HIGH,
                channels=[NotificationChannel.SNS, NotificationChannel.DASHBOARD],
                metadata={
                    "approval_id": approval_id,
                    "pipeline_id": pipeline_id,
                    "approval_type": approval_type,
                    "timeout_action": timeout_action,
                },
            )

        else:  # manual_escalation
            # Send critical alert for manual escalation
            notification_result = send_approval_timeout_alert(
                approval_type=approval_type, pipeline_id=pipeline_id, approval_id=approval_id, timeout_behavior="manual"
            )

        # Record notification metrics
        ApprovalMetrics.record_notification_sent(
            approval_type=approval_type, channel="unified_timeout", success=notification_result.get("success", False)
        )

        logger.info(f"Sent unified timeout notification for {approval_id}: {timeout_action} - {notification_result}")

    except Exception as e:
        logger.error(f"Error sending timeout notification: {e}")
        ApprovalMetrics.record_notification_sent(
            approval_type=approval_record["approval_type"], channel="unified_timeout", success=False
        )


def create_timeout_alarm(approval_record: dict[str, Any]):
    """Create CloudWatch alarm for manual timeout escalation"""
    try:
        approval_id = approval_record["approval_id"]
        pipeline_id = approval_record["pipeline_id"]

        alarm_name = f"AgenticDataIngestion-Approval-Timeout-{approval_id}"

        # Create a custom metric for this specific timeout
        cloudwatch.put_metric_data(
            Namespace="AgenticDataIngestion/Approvals",
            MetricData=[
                {
                    "MetricName": "ManualTimeoutEscalation",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "ApprovalId", "Value": approval_id},
                        {"Name": "PipelineId", "Value": pipeline_id},
                    ],
                }
            ],
        )

        # Create alarm that triggers immediately
        cloudwatch.put_metric_alarm(
            AlarmName=alarm_name,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            EvaluationPeriods=1,
            MetricName="ManualTimeoutEscalation",
            Namespace="AgenticDataIngestion/Approvals",
            Period=60,
            Statistic="Sum",
            Threshold=1.0,
            ActionsEnabled=True,
            AlarmActions=[CLOUDWATCH_ALARM_TOPIC] if CLOUDWATCH_ALARM_TOPIC else [],
            AlarmDescription=f"Manual intervention required for timed out approval {approval_id}",
            Dimensions=[{"Name": "ApprovalId", "Value": approval_id}, {"Name": "PipelineId", "Value": pipeline_id}],
            Unit="Count",
        )

        logger.info(f"Created timeout alarm: {alarm_name}")

    except Exception as e:
        logger.error(f"Error creating timeout alarm: {e}")


@time_function("ApprovalTimeoutHandler.GenerateTimeoutReport")
def generate_timeout_report(processed_timeouts: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate summary report of timeout processing"""
    try:
        report = {
            "timestamp": datetime.now(UTC).isoformat(),
            "total_timeouts_processed": len(processed_timeouts),
            "auto_approved": 0,
            "auto_rejected": 0,
            "manual_escalations": 0,
            "processing_errors": 0,
            "timeout_details": [],
        }

        for timeout in processed_timeouts:
            if timeout.get("success"):
                action = timeout.get("action", "unknown")
                if action == "auto_approved":
                    report["auto_approved"] += 1
                elif action == "auto_rejected":
                    report["auto_rejected"] += 1
                elif action == "manual_escalation":
                    report["manual_escalations"] += 1
            else:
                report["processing_errors"] += 1

            report["timeout_details"].append(
                {
                    "approval_id": timeout.get("approval_id"),
                    "pipeline_id": timeout.get("pipeline_id"),
                    "approval_type": timeout.get("approval_type"),
                    "action": timeout.get("action"),
                    "success": timeout.get("success", False),
                }
            )

        return report

    except Exception as e:
        logger.error(f"Error generating timeout report: {e}")
        return {"error": str(e)}


def lambda_handler(event, context):
    """
    Lambda function to handle approval timeouts.

    This function is typically triggered by CloudWatch Events on a schedule
    (e.g., every 15 minutes) to check for and process timed out approvals.
    """
    try:
        logger.info("Starting approval timeout processing")

        logger.append_keys(step_name="ApprovalTimeout")

        # Scan for expired approvals
        expired_approvals = scan_expired_approvals()

        if not expired_approvals:
            logger.info("No expired approvals found")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "No expired approvals found", "processed_count": 0}),
            }

        # Process each timeout
        processed_timeouts = []
        for approval in expired_approvals:
            try:
                success = process_approval_timeout(approval)
                processed_timeouts.append(
                    {
                        "approval_id": approval["approval_id"],
                        "pipeline_id": approval["pipeline_id"],
                        "approval_type": approval["approval_type"],
                        "action": approval.get("timeout_behavior", "manual"),
                        "success": success,
                    }
                )
            except Exception as e:
                logger.error(f"Error processing individual timeout: {e}")
                processed_timeouts.append(
                    {
                        "approval_id": approval.get("approval_id", "unknown"),
                        "pipeline_id": approval.get("pipeline_id", "unknown"),
                        "approval_type": approval.get("approval_type", "unknown"),
                        "action": "error",
                        "success": False,
                        "error": str(e),
                    }
                )

        # Generate summary report
        report = generate_timeout_report(processed_timeouts)

        logger.info(f"Processed {len(processed_timeouts)} timeout(s)")

        return {"statusCode": 200, "body": json.dumps({"message": "Timeout processing completed", "report": report})}

    except Exception as e:
        logger.error(f"Error in approval timeout handler: {e}")
        record_error("TimeoutHandlerError", "lambda_handler", str(e))

        return {"statusCode": 500, "body": json.dumps({"error": "InternalError", "message": str(e)})}
