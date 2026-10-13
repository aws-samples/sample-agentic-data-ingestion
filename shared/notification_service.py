# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Notification Service for AgenticDataIngestion Pipeline System

Provides unified notification capabilities across multiple channels:
- SNS (email, SMS)
- Slack webhooks
- In-dashboard notifications
- CloudWatch alarms

Uses a pluggable channel architecture — channels are loaded from the
NOTIFICATION_CHANNELS environment variable (default: 'sns,dashboard').

Environment configuration
    NOTIFICATION_CHANNELS
        Comma-separated channel names to load. Default: ``sns,dashboard``.
    MAX_CRITICAL_ALERT_ALARMS
        Cap on how many DISTINCT critical-alert CloudWatch alarms may exist
        (P28 R19.4). Default: ``25`` (``DEFAULT_MAX_CRITICAL_ALERT_ALARMS``).
        When the cap is reached, notifications for NEW alert types still emit
        the CriticalAlert metric but skip alarm creation with a loud log.
        Misconfigured values raise ``ValueError`` — honest failure over a
        silent fallback (same policy as ``shared/model_config.py``).

Identifier policy (P28 R19.3): every ``notification_id`` embeds
``uuid4().hex`` — collision-free by construction. The previous 1-second-
resolution timestamps made two notifications in the same second collide
(DynamoDB ``put_item`` silently overwrites the first).
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

import boto3
import requests

# P28 R18.2: approval-timeout default comes from the single config source.
from config import default_timeout_hours

logger = logging.getLogger(__name__)

NOTIFICATION_CHANNELS = os.environ.get("NOTIFICATION_CHANNELS", "sns,dashboard")


class NotificationPriority(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationChannelName(Enum):
    """Backward-compatible enum of known channel names."""

    SNS = "sns"
    SLACK = "slack"
    DASHBOARD = "dashboard"
    CLOUDWATCH = "cloudwatch"


# Backward-compatible alias so existing ``from notification_service import NotificationChannel``
# continues to work.
NotificationChannel = NotificationChannelName


class NotificationChannelProtocol(Protocol):
    """Protocol that all notification channels must implement."""

    def send(self, title: str, message: str, priority: str, metadata: dict) -> dict: ...


# ---------------------------------------------------------------------------
# Concrete channel implementations
# ---------------------------------------------------------------------------


class SNSChannel:
    """Send notifications via Amazon SNS."""

    def __init__(self):
        self.sns = boto3.client("sns")

    def send(self, title: str, message: str, priority: str, metadata: dict) -> dict:
        try:
            priority_enum = NotificationPriority(priority)
            topic_arn = self._get_sns_topic_for_priority(priority_enum)
            if not topic_arn:
                return {"success": False, "error": "No SNS topic configured for priority"}

            formatted_message = self._format_sns_message(title, message, priority_enum)

            response = self.sns.publish(
                TopicArn=topic_arn,
                Message=formatted_message,
                Subject=title,
            )

            return {
                "success": True,
                "message_id": response["MessageId"],
                "topic_arn": topic_arn,
            }
        except Exception as e:
            logger.error(f"SNS notification failed: {e}")
            return {"success": False, "error": str(e)}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _get_sns_topic_for_priority(priority: NotificationPriority) -> str | None:
        if priority == NotificationPriority.CRITICAL:
            return os.environ.get("CRITICAL_ALERTS_TOPIC_ARN")
        elif priority in (NotificationPriority.HIGH, NotificationPriority.MEDIUM):
            return os.environ.get("APPROVAL_NOTIFICATIONS_TOPIC_ARN")
        else:
            return os.environ.get("GENERAL_NOTIFICATIONS_TOPIC_ARN")

    @staticmethod
    def _format_sns_message(title: str, message: str, priority: NotificationPriority) -> str:
        priority_prefix = {
            NotificationPriority.CRITICAL: "🚨 CRITICAL",
            NotificationPriority.HIGH: "⚠️ HIGH",
            NotificationPriority.MEDIUM: "ℹ️ MEDIUM",
            NotificationPriority.LOW: "📝 LOW",
        }
        return f"""
{priority_prefix.get(priority, "")} - {title}

{message}

Time: {datetime.now(UTC).isoformat()}
System: AgenticDataIngestion Pipeline
        """.strip()


class DashboardChannel:
    """Store notifications for in-dashboard display."""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb")
        try:
            table_name = os.environ.get("NOTIFICATIONS_TABLE", "agentic_data_ingestion_notifications")
            self.notifications_table = self.dynamodb.Table(table_name)
        except Exception:
            logger.warning("Notifications table not available")
            self.notifications_table = None

    def send(self, title: str, message: str, priority: str, metadata: dict) -> dict:
        try:
            if not self.notifications_table:
                return {"success": False, "error": "Notifications table not available"}

            # P28 R19.3: uuid4 — two notifications in the same second must not
            # collide (put_item on a shared timestamp id silently overwrote).
            notification_id = f"dashboard_{uuid.uuid4().hex}"
            notification_record = {
                "notification_id": notification_id,
                "title": title,
                "message": message,
                "priority": priority,
                "created_at": datetime.now(UTC).isoformat(),
                "metadata": json.dumps(metadata or {}),
                "dashboard_visible": "true",
                "dashboard_read": "false",
                "ttl": int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
            }
            self.notifications_table.put_item(Item=notification_record)
            return {"success": True, "stored_in_dashboard": True}
        except Exception as e:
            logger.error(f"Dashboard notification failed: {e}")
            return {"success": False, "error": str(e)}

    # -- dashboard-specific query helpers ---------------------------------

    def get_dashboard_notifications(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        try:
            if not self.notifications_table:
                return []

            filter_expression = "dashboard_visible = :visible"
            expression_values: dict[str, Any] = {":visible": "true"}

            if unread_only:
                filter_expression += " AND dashboard_read = :read"
                expression_values[":read"] = "false"

            response = self.notifications_table.scan(
                FilterExpression=filter_expression,
                ExpressionAttributeValues=expression_values,
                Limit=limit,
            )

            notifications = response.get("Items", [])
            notifications.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return notifications
        except Exception as e:
            logger.error(f"Error getting dashboard notifications: {e}")
            return []

    def mark_notification_read(self, notification_id: str) -> bool:
        try:
            if not self.notifications_table:
                return False
            self.notifications_table.update_item(
                Key={"notification_id": notification_id},
                UpdateExpression="SET dashboard_read = :read, read_at = :read_at",
                ExpressionAttributeValues={
                    ":read": "true",
                    ":read_at": datetime.now(UTC).isoformat(),
                },
            )
            return True
        except Exception as e:
            logger.error(f"Error marking notification as read: {e}")
            return False


class SlackChannel:
    """Send notifications via Slack webhook."""

    def send(self, title: str, message: str, priority: str, metadata: dict) -> dict:
        try:
            webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
            if not webhook_url:
                return {"success": False, "error": "No Slack webhook configured"}

            priority_enum = NotificationPriority(priority)
            color = self._get_slack_color_for_priority(priority_enum)

            slack_payload: dict[str, Any] = {
                "text": title,
                "attachments": [
                    {
                        "color": color,
                        "title": title,
                        "text": message,
                        "fields": [],
                        "footer": "AgenticDataIngestion Pipeline",
                        "ts": int(datetime.now(UTC).timestamp()),
                    }
                ],
            }

            if metadata:
                for key, value in metadata.items():
                    slack_payload["attachments"][0]["fields"].append(
                        {
                            "title": key.replace("_", " ").title(),
                            "value": str(value),
                            "short": True,
                        }
                    )

            response = requests.post(webhook_url, json=slack_payload, timeout=10)
            response.raise_for_status()
            return {"success": True, "status_code": response.status_code}
        except Exception as e:
            logger.error(f"Slack notification failed: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _get_slack_color_for_priority(priority: NotificationPriority) -> str:
        colors = {
            NotificationPriority.CRITICAL: "danger",
            NotificationPriority.HIGH: "warning",
            NotificationPriority.MEDIUM: "good",
            NotificationPriority.LOW: "#36a64f",
        }
        return colors.get(priority, "good")


# ── Critical-alert alarm bounding (P28 R19.4) ──────────────────────────

#: Stable prefix for every critical-alert alarm. The full alarm name is
#: ``{prefix}{alert_type}`` — one alarm per alert TYPE, not per occurrence,
#: so repeated notifications update the same alarm (``put_metric_alarm`` on
#: an existing name is an update — naturally idempotent).
CRITICAL_ALARM_NAME_PREFIX = "AgenticDataIngestion-Critical-"

#: Default cap on how many DISTINCT critical-alert alarms may exist
#: (override with the ``MAX_CRITICAL_ALERT_ALARMS`` env var). The alert_type
#: vocabulary is small and code-defined (approval_timeout, pipeline_failure,
#: …), so 25 leaves generous headroom while guaranteeing a runaway caller can
#: never grow alarms without bound (CloudWatch account limits + cost + noise).
DEFAULT_MAX_CRITICAL_ALERT_ALARMS = 25


def _max_critical_alert_alarms() -> int:
    """Alarm-count cap from MAX_CRITICAL_ALERT_ALARMS (default 25).

    Misconfigured values raise ``ValueError`` — honest failure over a silent
    fallback default (same policy as ``shared/model_config.py``).
    """
    raw = os.environ.get("MAX_CRITICAL_ALERT_ALARMS", str(DEFAULT_MAX_CRITICAL_ALERT_ALARMS))
    try:
        cap = int(raw)
    except ValueError as exc:
        raise ValueError(f"MAX_CRITICAL_ALERT_ALARMS must be an integer, got {raw!r}") from exc
    if cap < 1:
        raise ValueError(f"MAX_CRITICAL_ALERT_ALARMS must be >= 1, got {cap}")
    return cap


class CloudWatchChannel:
    """Emit the CriticalAlert metric and maintain a BOUNDED set of alarms.

    P28 R19.4: alarm creation is idempotent (stable name keyed by alert_type)
    and bounded (a NEW alarm name is only created while the number of distinct
    critical-alert alarms is below the configured cap). The metric emission is
    unconditional — the metric is the signal; the alarm is the notifier.
    """

    def __init__(self) -> None:
        self.cloudwatch = boto3.client("cloudwatch")

    def send(self, title: str, message: str, priority: str, metadata: dict) -> dict:
        try:
            if priority != NotificationPriority.CRITICAL.value:
                return {"success": False, "error": "CloudWatch alarms only for critical notifications"}

            alert_type = (metadata or {}).get("alert_type", "Unknown")
            # P28 R19.4: stable, idempotent alarm name — the former
            # per-timestamp name created a new alarm on every notification.
            alarm_name = f"{CRITICAL_ALARM_NAME_PREFIX}{alert_type}"
            dimensions: Any = [
                {"Name": "AlertType", "Value": alert_type},
                {"Name": "Severity", "Value": "Critical"},
            ]

            # The metric is the signal — always emitted, even when the alarm
            # cap prevents creating a new alarm below.
            self.cloudwatch.put_metric_data(
                Namespace="AgenticDataIngestion/Alerts",
                MetricData=[
                    {
                        "MetricName": "CriticalAlert",
                        "Value": 1,
                        "Unit": "Count",
                        "Dimensions": dimensions,
                    }
                ],
            )

            # Bounded creation: updating an EXISTING alarm is always allowed
            # (it does not grow the set); creating a NEW name is subject to
            # the cap. If the describe fails we fail closed (no alarm) — the
            # bound cannot be verified, and the metric already went out.
            existing_alarms = self._existing_critical_alarm_names()
            cap = _max_critical_alert_alarms()
            if alarm_name not in existing_alarms and len(existing_alarms) >= cap:
                logger.error(
                    "Critical-alert alarm cap reached (%d existing >= cap %d): NOT creating alarm %r "
                    "for alert_type %r. The CriticalAlert metric was still emitted. Raise "
                    "MAX_CRITICAL_ALERT_ALARMS or delete stale %s* alarms to allow new alarm names.",
                    len(existing_alarms),
                    cap,
                    alarm_name,
                    alert_type,
                    CRITICAL_ALARM_NAME_PREFIX,
                )
                return {
                    "success": True,
                    "alarm_name": None,
                    "alarm_created": False,
                    "reason": "alarm_cap_reached",
                }

            self.cloudwatch.put_metric_alarm(
                AlarmName=alarm_name,
                ComparisonOperator="GreaterThanOrEqualToThreshold",
                EvaluationPeriods=1,
                MetricName="CriticalAlert",
                Namespace="AgenticDataIngestion/Alerts",
                Period=60,
                Statistic="Sum",
                Threshold=1.0,
                ActionsEnabled=True,
                AlarmDescription=f"{title}: {message}",
                Dimensions=dimensions,
                Unit="Count",
            )

            return {"success": True, "alarm_name": alarm_name, "alarm_created": True}
        except Exception as e:
            logger.error(f"CloudWatch alarm creation failed: {e}")
            return {"success": False, "error": str(e)}

    def _existing_critical_alarm_names(self) -> set[str]:
        """Names of all existing critical-alert alarms (paginated describe)."""
        names: set[str] = set()
        paginator = self.cloudwatch.get_paginator("describe_alarms")
        for page in paginator.paginate(AlarmNamePrefix=CRITICAL_ALARM_NAME_PREFIX):
            for alarm in page.get("MetricAlarms", []):
                names.add(alarm["AlarmName"])
        return names


# ---------------------------------------------------------------------------
# Channel registry
# ---------------------------------------------------------------------------

_CHANNEL_REGISTRY: dict[str, type] = {
    "sns": SNSChannel,
    "dashboard": DashboardChannel,
    "slack": SlackChannel,
    "cloudwatch": CloudWatchChannel,
}


# ---------------------------------------------------------------------------
# NotificationService — pluggable channel architecture
# ---------------------------------------------------------------------------


class NotificationService:
    """Centralized notification service for AgenticDataIngestion with pluggable channels."""

    def __init__(self):
        self._channels: dict[str, NotificationChannelProtocol] = {}
        self._load_configured_channels()

    def _load_configured_channels(self) -> None:
        """Load channels listed in NOTIFICATION_CHANNELS env var."""
        channel_names = [c.strip().lower() for c in NOTIFICATION_CHANNELS.split(",") if c.strip()]
        for name in channel_names:
            cls = _CHANNEL_REGISTRY.get(name)
            if cls is not None:
                try:
                    self._channels[name] = cls()
                except Exception as e:
                    logger.warning(f"Failed to initialise channel '{name}': {e}")
            else:
                logger.debug(f"Unknown notification channel '{name}' — skipping")

    def register_channel(self, name: str, channel: NotificationChannelProtocol) -> None:
        """Register an additional channel at runtime."""
        self._channels[name] = channel

    # -- core send --------------------------------------------------------

    def send_notification(
        self,
        notification_type: str,
        title: str,
        message: str,
        priority: NotificationPriority = NotificationPriority.MEDIUM,
        channels: list[str] = None,
        metadata: dict[str, Any] = None,
        recipients: list[str] = None,
    ) -> dict[str, Any]:
        """Send notification across configured (or explicitly requested) channels."""

        # If caller specifies channels, only use those that are actually loaded
        target_names = (
            list(self._channels.keys())
            if channels is None
            else [c if isinstance(c, str) else c.value for c in channels]
        )

        # P28 R19.3: uuid4 — collision-free notification ids (the former
        # 1-second-resolution timestamp collided across same-second sends).
        notification_id = f"{notification_type}_{uuid.uuid4().hex}"
        delivery_results: dict[str, Any] = {}

        for name in target_names:
            channel = self._channels.get(name)
            if channel is None:
                # Unconfigured channel — skip silently (Requirement 14.3)
                continue
            try:
                result = channel.send(
                    title=title,
                    message=message,
                    priority=priority.value,
                    metadata=metadata or {},
                )
                delivery_results[name] = result
            except Exception as e:
                logger.error(f"Error sending notification to {name}: {e}")
                delivery_results[name] = {"success": False, "error": str(e)}

        return {
            "notification_id": notification_id,
            "delivery_results": delivery_results,
            "success": any(r.get("success", False) for r in delivery_results.values()),
        }

    # -- dashboard helpers (delegate to DashboardChannel if loaded) -------

    def get_dashboard_notifications(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        dashboard = self._channels.get("dashboard")
        if isinstance(dashboard, DashboardChannel):
            return dashboard.get_dashboard_notifications(limit, unread_only)
        return []

    def mark_notification_read(self, notification_id: str) -> bool:
        dashboard = self._channels.get("dashboard")
        if isinstance(dashboard, DashboardChannel):
            return dashboard.mark_notification_read(notification_id)
        return False


# ---------------------------------------------------------------------------
# Global instance & convenience functions
# ---------------------------------------------------------------------------

notification_service = NotificationService()


def send_approval_notification(
    approval_type: str,
    pipeline_id: str,
    approval_id: str,
    dashboard_url: str,
    timeout_hours: int | None = None,
) -> dict[str, Any]:
    """Send approval required notification.

    ``timeout_hours`` defaults to the shared config source's approval
    timeout (P28 R18.2); callers normally pass the approval record's
    computed timeout explicitly.
    """
    if timeout_hours is None:
        timeout_hours = default_timeout_hours()
    title = f"AgenticDataIngestion Approval Required: {approval_type}"
    message = f"""
Pipeline {pipeline_id} requires approval for {approval_type}.

Approval ID: {approval_id}
Timeout: {timeout_hours} hours
Dashboard: {dashboard_url}

Please review and approve to continue pipeline execution.
    """.strip()

    return notification_service.send_notification(
        notification_type="approval_required",
        title=title,
        message=message,
        priority=NotificationPriority.HIGH,
        channels=["sns", "slack", "dashboard"],
        metadata={
            "approval_type": approval_type,
            "pipeline_id": pipeline_id,
            "approval_id": approval_id,
            "dashboard_url": dashboard_url,
        },
    )


def send_approval_timeout_alert(
    approval_type: str, pipeline_id: str, approval_id: str, timeout_behavior: str
) -> dict[str, Any]:
    """Send approval timeout alert"""
    title = f"AgenticDataIngestion Approval Timeout: {approval_type}"
    message = f"""
URGENT: Approval request has timed out and requires immediate attention.

Pipeline: {pipeline_id}
Approval Type: {approval_type}
Approval ID: {approval_id}
Timeout Behavior: {timeout_behavior}

This approval is blocking pipeline execution.
    """.strip()

    return notification_service.send_notification(
        notification_type="approval_timeout",
        title=title,
        message=message,
        priority=NotificationPriority.CRITICAL,
        channels=["sns", "slack", "dashboard", "cloudwatch"],
        metadata={
            "approval_type": approval_type,
            "pipeline_id": pipeline_id,
            "approval_id": approval_id,
            "timeout_behavior": timeout_behavior,
            "alert_type": "approval_timeout",
        },
    )


def send_pipeline_failure_alert(pipeline_id: str, stage_name: str, error_message: str) -> dict[str, Any]:
    """Send pipeline failure alert"""
    title = f"AgenticDataIngestion Pipeline Failure: {pipeline_id}"
    message = f"""
Pipeline execution has failed and requires attention.

Pipeline: {pipeline_id}
Failed Stage: {stage_name}
Error: {error_message}

Please investigate and resolve the issue.
    """.strip()

    return notification_service.send_notification(
        notification_type="pipeline_failure",
        title=title,
        message=message,
        priority=NotificationPriority.HIGH,
        channels=["sns", "dashboard"],
        metadata={
            "pipeline_id": pipeline_id,
            "stage_name": stage_name,
            "error_message": error_message,
            "alert_type": "pipeline_failure",
        },
    )


def send_self_healing_declined_alert(
    pipeline_id: str,
    run_id: str,
    rerun_safety: dict[str, Any],
    declined_action: str,
    applied_fix: str = "",
) -> dict[str, Any]:
    """Self-healing declined to re-run a failed pipeline (P30 R15.3).

    Companion to the outcome record written by the agent's
    ``build_outcome_record`` (``skip_reason="partial_write_guard"`` +
    ``skip_detail``): R15.3 requires the DECISION to be visible in the
    user-facing notification too, rather than reading as a generic "flagged
    for review". Same shape as :func:`send_pipeline_failure_alert` — HIGH
    priority, ``sns`` + ``dashboard``, ``alert_type`` in metadata for the
    critical-alarm keying convention.

    Two honesty constraints, which are the substance of R15.3:

    1. **It never claims a partial write was detected.** No partial-write
       signal exists at the agent's decision point, so the guard treats any
       non-``SUCCEEDED`` terminal state as "may have written rows". The
       ``reason`` produced by ``evaluate_rerun_safety`` says exactly that and
       is reproduced VERBATIM here — the wording is pinned by the agent's own
       tests, so re-phrasing it in this message would be the one way to
       reintroduce a false detection claim.
    2. **It names the write mode that was graded, and the mode as persisted.**
       ``write_mode`` is applied at code-generation time only, so the pipeline
       record's mode may not be what the deployed script did. Both spellings
       from the verdict are surfaced so a mismatch is diagnosable from the
       notification alone.

    ``applied_fix`` is the remediation that had ALREADY been applied when the
    re-run was declined (the OOM path calls ``update_job`` before the gate).
    Declining the re-run does not roll that back, so a notification that said
    only "declined to re-run" would hide a live mutation to the Glue job. Pass
    ``""`` when the declined remediation WAS the re-run, i.e. nothing was
    applied.
    """
    reason = str(rerun_safety.get("reason", ""))
    target = str(rerun_safety.get("target_type", "")) or "unrecognised"
    target_persisted = str(rerun_safety.get("target_type_persisted", ""))
    write_mode = str(rerun_safety.get("write_mode", ""))
    write_mode_persisted = str(rerun_safety.get("write_mode_persisted", ""))

    title = f"AgenticDataIngestion Self-Healing Declined Re-run: {pipeline_id}"

    lines = [
        f"Self-healing did NOT re-run pipeline {pipeline_id}. The failed run is flagged for human review instead.",
        "",
        f"Why: {reason}",
        "",
        f"Graded target type: {target} (recorded on the pipeline as {target_persisted!r})",
        f"Graded write mode: {write_mode!r} (recorded on the pipeline as {write_mode_persisted!r})",
        "The write mode is applied when the Glue script is generated, not at run time, so the "
        "deployed script's write behaviour is the one its last code generation produced. If the "
        "recorded mode changed after that, the graded mode is not what the failed run did.",
        "",
        f"Remediation that would have re-run: {declined_action or 'none'}",
    ]
    if applied_fix:
        lines += [
            "",
            f"ALREADY APPLIED, and not undone by this decision: {applied_fix}",
            "That change was made to the Glue job before the re-run decision. Declining the "
            "re-run does not roll it back — the job definition has changed.",
        ]
    lines += [
        "",
        f"Run: {run_id}",
        "",
        "Check the target for rows the failed run may have written before re-running this pipeline manually.",
    ]
    message = "\n".join(lines)

    return notification_service.send_notification(
        notification_type="self_healing_rerun_declined",
        title=title,
        message=message,
        priority=NotificationPriority.HIGH,
        channels=["sns", "dashboard"],
        metadata={
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "skip_reason": "partial_write_guard",
            "declined_action": declined_action,
            "target_type": target,
            "target_type_persisted": target_persisted,
            "write_mode": write_mode,
            "write_mode_persisted": write_mode_persisted,
            "applied_fix": applied_fix,
            "alert_type": "self_healing_rerun_declined",
        },
    )


def send_self_healing_applied_alert(
    pipeline_id: str,
    run_id: str,
    error_classification: str,
    remediation_action: str,
    remediation_applied: str,
    rerun_started: bool,
    rerun_id: str = "",
    rerun_start_error: str = "",
) -> dict[str, Any]:
    """Self-healing applied a remediation to a failed pipeline (P30 R14.3).

    Sibling of :func:`send_self_healing_declined_alert`, and the other half of
    R14.3: "a heal that ran SHALL be indicated in the pipeline run list and in
    a notification stating what was remediated". The declined alert covers the
    case where the Partial_Write_Guard refused to act; this covers the case
    where a remediation WAS applied. The two are mutually exclusive — the agent
    calls exactly one per heal — and they share the delivery contract
    (``sns`` + ``dashboard``, ``alert_type`` in metadata for the
    critical-alarm keying convention).

    **It must never read as "the pipeline is fixed".** At send time the only
    thing known is that a re-run was *started*: the agent's honesty contract is
    that ``rerun_started`` is True only when ``start_execution`` actually
    succeeded, and that ``rerun_status`` moves to ``success``/``failed`` only
    later, when the re-run's own ``job_completion`` links the terminal result
    back onto the outcome record. So the message states the outcome is not yet
    known, and no wording here implies otherwise.

    **``rerun_id`` is lineage, not a link.** It is ``{original}-heal``;
    ``file_validation`` mints a FRESH run_id for the re-run's state rows, so
    this id identifies the heal but is not the re-run's own id. The message
    says so rather than inviting the reader to look it up and find nothing.

    Priority differentiates the two outcomes because they need different
    attention: a started re-run is informational (``MEDIUM``), while a re-run
    that never started leaves a failed run with nobody re-running it
    (``HIGH``). Both map to the same SNS topic, so the distinction is carried
    by the dashboard notification rather than by routing.

    Args:
        error_classification: The agent's root-cause class (``oom_error``,
            ``schema_mismatch``, ``type_cast_error``).
        remediation_action: The action taken (``upgrade_worker_type``,
            ``rerun_pipeline``).
        remediation_applied: The human-readable description of the fix — the
            "what was remediated" R14.3 asks for. Reproduced verbatim.
        rerun_started: True only when ``start_execution`` succeeded.
        rerun_start_error: Present only when the start failed.
    """
    if rerun_started:
        title = f"AgenticDataIngestion Self-Healing Remediated: {pipeline_id}"
        lines = [
            f"Self-healing remediated a failed run of pipeline {pipeline_id} and started a re-run.",
            "",
            f"Remediated: {remediation_applied or '(no description recorded)'}",
            f"Root cause: {error_classification or 'unknown'}",
            f"Action: {remediation_action or 'unknown'}",
            "",
            "THE RE-RUN'S OUTCOME IS NOT KNOWN YET. This notification records only that the "
            "re-run was started; whether it succeeds is reported separately when it finishes.",
        ]
        if rerun_id:
            lines += [
                "",
                f"Heal lineage id: {rerun_id}",
                "That id links the heal to the run it was applied to. It is not the re-run's own "
                "run id — the re-run is assigned a fresh one when it starts.",
            ]
        priority = NotificationPriority.MEDIUM
        alert_type = "self_healing_applied"
    else:
        title = f"AgenticDataIngestion Self-Healing Could Not Re-run: {pipeline_id}"
        lines = [
            f"Self-healing diagnosed a failed run of pipeline {pipeline_id} but could NOT start the re-run. "
            f"No re-run exists and the run is still failed.",
            "",
            f"Intended remediation: {remediation_applied or '(no description recorded)'}",
            f"Root cause: {error_classification or 'unknown'}",
            f"Action: {remediation_action or 'unknown'}",
            "",
            f"Why the re-run did not start: {rerun_start_error or '(no error recorded)'}",
        ]
        if remediation_action == "upgrade_worker_type":
            lines += [
                "",
                "The worker-type upgrade was applied to the Glue job BEFORE the re-run was attempted, "
                "and it has not been rolled back — the job definition has changed.",
            ]
        priority = NotificationPriority.HIGH
        alert_type = "self_healing_rerun_start_failed"

    # "Failed run", not "healed run": the run that was healed is still the run
    # that failed until the re-run's own result says otherwise.
    lines += ["", f"Failed run: {run_id}"]
    message = "\n".join(lines)

    return notification_service.send_notification(
        notification_type=alert_type,
        title=title,
        message=message,
        priority=priority,
        channels=["sns", "dashboard"],
        metadata={
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "error_classification": error_classification,
            "remediation_action": remediation_action,
            "remediation_applied": remediation_applied,
            "rerun_started": rerun_started,
            "rerun_id": rerun_id,
            "rerun_start_error": rerun_start_error,
            "alert_type": alert_type,
        },
    )


def send_scheduled_run_skipped_alert(
    pipeline_id: str,
    run_id: str,
    skip_reason: str,
    skip_detail: str,
    *,
    window_seconds: int,
    running_count: int | None = None,
    running_max: int | None = None,
    running_count_is_floor: bool = False,
) -> dict[str, Any]:
    """A scheduled load did not happen because of contention (p30 R10.1).

    Companion to the Skipped_Run_Record ``pipeline_trigger`` writes on the same
    decision. R10.1 requires BOTH: the record makes the miss auditable later,
    the notification makes it noticeable now. The record alone satisfies nobody
    who is not already looking at the run list — which is exactly the person the
    requirement's user story is about ("a silent miss ... becomes a data gap I
    discover later").

    Same shape as :func:`send_pipeline_failure_alert` — ``sns`` + ``dashboard``,
    ``alert_type`` in metadata for the critical-alarm keying convention.

    **MEDIUM, not HIGH.** Every other alert in this module at HIGH describes
    something that went wrong and will stay wrong. A contention skip describes
    the system working as designed: the concurrency ceiling and the per-pipeline
    lock exist to refuse work, and refusing it is success for them. What makes
    it worth telling anyone is that the *pipeline owner's* data did not arrive —
    a real cost, but not a fault, and not an incident. Grading it HIGH alongside
    a pipeline failure would make the two indistinguishable in an inbox and
    would devalue the failure alert.

    **One notification per collapse window, not per skip.** The caller only
    reaches here when it created a NEW Skipped_Run_Record; a skip that folded
    into an existing window's record does not notify. Without that, a pipeline
    scheduled every five minutes against a saturated fleet would send 288
    notifications a day, all identical — noise that trains people to filter the
    channel, after which the alert protects nothing. ``window_seconds`` is
    stated IN the message so the reader knows the alert is a summary and knows
    not to expect one per occurrence.

    ``running_count``/``running_max`` apply to the global-concurrency reason
    only; a lock-contention skip has no fleet-wide number to report and passes
    them as ``None`` rather than as zeroes. ``running_count_is_floor`` travels
    WITH the count for the reason recorded on
    ``pipeline_trigger._running_execution_snapshot``: the count saturates at a
    page boundary, so reporting a lower bound as a total understates saturation
    in the reassuring direction. A floor renders as "at least N".
    """
    title = f"AgenticDataIngestion Scheduled Run Skipped: {pipeline_id}"

    window_minutes = max(1, window_seconds // 60)
    lines = [
        f"A scheduled load for pipeline {pipeline_id} did not run. No execution was started and "
        "nothing will retry it — the next attempt is the schedule's next fire.",
        "",
        f"Why: {skip_detail}",
        "",
        f"Reason code: {skip_reason}",
    ]
    if running_count is not None and running_max is not None:
        observed = f"at least {running_count}" if running_count_is_floor else str(running_count)
        lines += [
            f"Running executions when the trigger was checked: {observed} of a maximum {running_max}.",
        ]
        if running_count_is_floor:
            lines += [
                "That count is a LOWER BOUND, not a total — the running-execution listing was "
                "truncated, so the real number of running executions is higher.",
            ]
    lines += [
        "",
        f"Skipped_Run_Record: {run_id}",
        f"Further skips of this pipeline for this reason within {window_minutes} minute(s) are "
        "added to that record and do NOT send another notification, so treat this as a summary "
        "rather than a count of one.",
        "",
        "If the pipeline needs this load now, start it from the dashboard once the contention "
        "clears. If skips keep arriving, the schedule is firing faster than the pipeline can run.",
    ]
    message = "\n".join(lines)

    metadata: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "skip_reason": skip_reason,
        "skip_detail": skip_detail,
        "notify_window_seconds": window_seconds,
        "alert_type": "scheduled_run_skipped",
    }
    if running_count is not None:
        metadata["running_count"] = running_count
        metadata["running_count_is_floor"] = running_count_is_floor
    if running_max is not None:
        metadata["running_max"] = running_max

    return notification_service.send_notification(
        notification_type="scheduled_run_skipped",
        title=title,
        message=message,
        priority=NotificationPriority.MEDIUM,
        channels=["sns", "dashboard"],
        metadata=metadata,
    )


def send_performance_degradation_alert(
    metric_name: str, current_value: float, threshold: float, trend: str
) -> dict[str, Any]:
    """Send performance degradation alert"""
    title = f"AgenticDataIngestion Performance Alert: {metric_name}"
    message = f"""
Performance metric has degraded beyond acceptable threshold.

Metric: {metric_name}
Current Value: {current_value}
Threshold: {threshold}
Trend: {trend}

Please investigate system performance.
    """.strip()

    return notification_service.send_notification(
        notification_type="performance_degradation",
        title=title,
        message=message,
        priority=NotificationPriority.MEDIUM,
        channels=["sns", "dashboard"],
        metadata={
            "metric_name": metric_name,
            "current_value": current_value,
            "threshold": threshold,
            "trend": trend,
            "alert_type": "performance_degradation",
        },
    )
