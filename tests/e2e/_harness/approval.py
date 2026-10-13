# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approval_Driver — acts as the human approver for waitForTaskToken gates.

Releases the ``WaitForSchemaMappingApproval`` and ``WaitForCodeApproval``
gates in the AgenticDataIngestion pipeline state machine. Implements three release
paths in priority order:

1. **REST + SigV4 (primary):** ``GET /api/approvals/pending`` → match on
   ``pipeline_id`` + ``approval_type`` → ``POST /api/approvals/{id}/approve``
   or ``/reject``.
2. **Direct Lambda invoke (fallback):** invoke the ``dashboard_api`` Lambda
   with a synthetic API-Gateway proxy event for the same route.
3. **Raw Step Functions (last-resort):** read the token from
   ``agentic_data_ingestion_task_tokens`` by ``approval_id`` and call
   ``send_task_success`` / ``send_task_failure`` directly.

Usage (via the ``approval_driver`` fixture):

    def test_csv_run(approval_driver, provisioner, cost_budget):
        pipeline = provisioner.create(...)
        # ... trigger run ...
        approval_driver.release_gate(
            pipeline_id=pipeline.pipeline_id,
            approval_type="schema_mapping",
            decision="approve",
            timeout=cost_budget,
        )
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Table name for task tokens (matches deployed naming)
TASK_TOKENS_TABLE = "agentic_data_ingestion_task_tokens"


class ApprovalStallError(Exception):
    """Raised when an approval gate stalls past the Cost_Budget."""

    def __init__(self, gate: str, approval_id: str | None, timeout: int):
        self.gate = gate
        self.approval_id = approval_id
        self.timeout = timeout
        super().__init__(f"Approval gate '{gate}' stalled past {timeout}s timeout (approval_id={approval_id})")


@dataclass
class ApprovalResult:
    """Result of an approval action."""

    approval_id: str
    status: str  # "approved" or "rejected"
    method: str  # "rest", "lambda_invoke", or "raw_sfn"


class ApprovalDriver:
    """Drives approval gates in the AgenticDataIngestion pipeline state machine.

    Acts as the external approver for waitForTaskToken gates, supporting
    both the schema-mapping gate (always required with default config) and
    the optional code-generation gate.

    Args:
        rest_client: SigV4-signed REST client for the dashboard API.
        boto3_session: Active boto3 Session for Lambda/SFN/DDB fallbacks.
        cost_budget: Default timeout ceiling (overridable per call).
    """

    def __init__(
        self,
        rest_client: Any,
        boto3_session: Any,
        cost_budget: int = 420,
    ) -> None:
        self._rest_client = rest_client
        self._session = boto3_session
        self._cost_budget = cost_budget

    # ── Public API ────────────────────────────────────────────────────

    def wait_for_pending(
        self,
        *,
        pipeline_id: str,
        approval_type: str,
        timeout: int | None = None,
    ) -> str:
        """Poll for a pending approval matching pipeline_id + approval_type.

        Returns:
            The ``approval_id`` once found.

        Raises:
            ApprovalStallError: if no matching pending approval appears
                within the timeout.
        """
        effective_timeout = timeout or self._cost_budget
        deadline = time.time() + effective_timeout
        poll_interval = 5  # seconds between polls

        logger.info(
            "Waiting for pending approval: pipeline_id=%s, type=%s, timeout=%ds",
            pipeline_id,
            approval_type,
            effective_timeout,
        )

        while time.time() < deadline:
            approval_id = self._find_pending_approval(pipeline_id, approval_type)
            if approval_id:
                logger.info(
                    "Found pending approval: %s (pipeline=%s, type=%s)",
                    approval_id,
                    pipeline_id,
                    approval_type,
                )
                return approval_id
            time.sleep(poll_interval)

        raise ApprovalStallError(
            gate=approval_type,
            approval_id=None,
            timeout=effective_timeout,
        )

    def approve(self, approval_id: str, *, comments: str = "e2e auto-approve") -> ApprovalResult:
        """Approve a pending approval request.

        Tries each release path in priority order until one succeeds.

        Returns:
            ApprovalResult with the method used.

        Raises:
            RuntimeError: if all release paths fail.
        """
        return self._act(approval_id, decision="approve", comments=comments)

    def reject(self, approval_id: str, *, reason: str = "e2e reject") -> ApprovalResult:
        """Reject a pending approval request.

        Tries each release path in priority order until one succeeds.

        Returns:
            ApprovalResult with the method used.

        Raises:
            RuntimeError: if all release paths fail.
        """
        return self._act(approval_id, decision="reject", reason=reason)

    def release_gate(
        self,
        *,
        pipeline_id: str,
        approval_type: str,
        decision: str = "approve",
        timeout: int | None = None,
        optional: bool = False,
    ) -> ApprovalResult | None:
        """Discover a pending approval and act on it (approve or reject).

        This is the high-level convenience method that combines
        ``wait_for_pending`` + ``approve``/``reject``.

        Args:
            pipeline_id: The pipeline to look for.
            approval_type: Gate type (``"schema_mapping"`` or ``"code_generation"``).
            decision: ``"approve"`` or ``"reject"``.
            timeout: Override for the poll timeout.
            optional: If True, return None instead of raising when the gate
                never appears (used for code_generation which may not trigger).

        Returns:
            ApprovalResult on success, or None if optional and gate never appeared.

        Raises:
            ApprovalStallError: if the gate doesn't appear within timeout
                (and ``optional=False``).
        """
        effective_timeout = timeout or self._cost_budget

        try:
            approval_id = self.wait_for_pending(
                pipeline_id=pipeline_id,
                approval_type=approval_type,
                timeout=effective_timeout,
            )
        except ApprovalStallError:
            if optional:
                logger.info(
                    "Optional gate '%s' never appeared for pipeline %s — proceeding.",
                    approval_type,
                    pipeline_id,
                )
                return None
            raise

        if decision == "approve":
            return self.approve(approval_id)
        elif decision == "reject":
            return self.reject(approval_id)
        else:
            raise ValueError(f"Invalid decision: {decision!r} (expected 'approve' or 'reject')")

    # ── Private: discovery ────────────────────────────────────────────

    def _find_pending_approval(self, pipeline_id: str, approval_type: str) -> str | None:
        """Try to find a pending approval via REST API, then direct DDB scan.

        Returns the approval_id if found, else None.
        """
        # Path 1: REST API (works for full records)
        try:
            resp = self._rest_client.get("/api/approvals/pending")
            if resp.status_code == 200 and isinstance(resp.body, list):
                for item in resp.body:
                    # Full records have pipeline_id/pipeline_key
                    item_pipeline = item.get("pipeline_id") or item.get("pipeline_key", "")
                    item_type = item.get("approval_type", "")
                    item_status = item.get("status", "")
                    if item_pipeline == pipeline_id and item_type == approval_type and item_status == "pending":
                        return item.get("approval_id", "")

                    # Sparse records: check if approval_id contains our approval_type
                    # and query DDB to verify it belongs to our pipeline
                    if item_status == "pending" and not item_pipeline:
                        aid = item.get("approval_id", "")
                        if f"_{approval_type}_" in aid:
                            # Verify this approval belongs to our pipeline via DDB
                            if self._verify_approval_belongs_to_pipeline(aid, pipeline_id):
                                return aid
        except Exception as exc:
            logger.debug("REST pending-approval lookup failed: %s", exc)

        # Path 2: Direct DynamoDB scan (fallback for sparse records)
        try:
            return self._find_approval_in_ddb(pipeline_id, approval_type)
        except Exception as exc:
            logger.debug("DDB approval scan failed: %s", exc)

        return None

    def _verify_approval_belongs_to_pipeline(self, approval_id: str, pipeline_id: str) -> bool:
        """Check if an approval_id belongs to a specific pipeline by querying DDB."""
        try:
            table = self._session.resource("dynamodb").Table("agentic_data_ingestion_approvals")
            resp = table.get_item(Key={"approval_id": approval_id})
            item = resp.get("Item", {})
            return item.get("pipeline_id") == pipeline_id
        except Exception:
            return False

    def _find_approval_in_ddb(self, pipeline_id: str, approval_type: str) -> str | None:
        """Scan the approvals table directly for a pending approval matching our pipeline."""
        from boto3.dynamodb.conditions import Attr

        table = self._session.resource("dynamodb").Table("agentic_data_ingestion_approvals")

        # Scan with filter for pipeline_id + status + approval_type
        scan_kwargs = {
            "FilterExpression": (
                Attr("pipeline_id").eq(pipeline_id)
                & Attr("status").eq("pending")
                & Attr("approval_type").eq(approval_type)
            ),
        }

        resp = table.scan(**scan_kwargs)
        items = resp.get("Items", [])

        if items:
            # Return most recent
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return items[0].get("approval_id")

        return None

    # ── Private: action (three release paths) ─────────────────────────

    def _act(
        self,
        approval_id: str,
        *,
        decision: str,
        comments: str = "",
        reason: str = "",
    ) -> ApprovalResult:
        """Execute the approval/rejection via the three fallback paths."""
        errors: list[tuple[str, Exception]] = []

        # Path 1: REST + SigV4
        try:
            result = self._act_via_rest(approval_id, decision=decision, comments=comments, reason=reason)
            if result:
                return result
        except Exception as exc:
            errors.append(("rest", exc))
            logger.debug("REST %s failed for %s: %s", decision, approval_id, exc)

        # Path 2: Direct Lambda invoke
        try:
            result = self._act_via_lambda(approval_id, decision=decision, comments=comments, reason=reason)
            if result:
                return result
        except Exception as exc:
            errors.append(("lambda_invoke", exc))
            logger.debug("Lambda invoke %s failed for %s: %s", decision, approval_id, exc)

        # Path 3: Raw Step Functions
        try:
            result = self._act_via_sfn(approval_id, decision=decision, comments=comments, reason=reason)
            if result:
                return result
        except Exception as exc:
            errors.append(("raw_sfn", exc))
            logger.debug("Raw SFN %s failed for %s: %s", decision, approval_id, exc)

        # All paths failed
        error_details = "; ".join(f"{method}: {exc}" for method, exc in errors)
        raise RuntimeError(f"All release paths failed for {decision} on {approval_id}: {error_details}")

    def _act_via_rest(
        self,
        approval_id: str,
        *,
        decision: str,
        comments: str,
        reason: str,
    ) -> ApprovalResult | None:
        """Path 1: Approve/reject via the dashboard REST API."""
        if decision == "approve":
            path = f"/api/approvals/{approval_id}/approve"
            body = {"comments": comments, "approved_by": "e2e-harness"}
        else:
            path = f"/api/approvals/{approval_id}/reject"
            body = {"reason": reason, "rejected_by": "e2e-harness"}

        resp = self._rest_client.post(path, body)

        if resp.status_code == 200:
            logger.info("REST %s succeeded for %s", decision, approval_id)
            return ApprovalResult(
                approval_id=approval_id,
                status=decision + "d",  # "approved" or "rejected"
                method="rest",
            )

        # 410 = token expired/not found; 404 = approval not found — fall through
        if resp.status_code in (410, 404):
            logger.warning(
                "REST %s returned %d for %s: %s — falling through",
                decision,
                resp.status_code,
                approval_id,
                resp.body,
            )
            return None

        # Other errors — raise to trigger fallback
        raise RuntimeError(f"REST {decision} returned unexpected {resp.status_code}: {resp.body}")

    def _act_via_lambda(
        self,
        approval_id: str,
        *,
        decision: str,
        comments: str,
        reason: str,
    ) -> ApprovalResult | None:
        """Path 2: Invoke the dashboard_api Lambda directly with a synthetic event."""
        lambda_client = self._session.client("lambda")

        if decision == "approve":
            path = f"/api/approvals/{approval_id}/approve"
            http_method = "POST"
            body = json.dumps({"comments": comments, "approved_by": "e2e-harness"})
        else:
            path = f"/api/approvals/{approval_id}/reject"
            http_method = "POST"
            body = json.dumps({"reason": reason, "rejected_by": "e2e-harness"})

        # Synthetic API Gateway proxy event
        event = {
            "httpMethod": http_method,
            "path": path,
            "pathParameters": {"proxy": path.lstrip("/")},
            "queryStringParameters": None,
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "requestContext": {
                "identity": {"userArn": "arn:aws:iam::000000000000:user/e2e-harness"},
                "httpMethod": http_method,
                "path": path,
            },
            "isBase64Encoded": False,
        }

        # Find the dashboard_api Lambda function name
        func_name = self._find_dashboard_lambda()
        if not func_name:
            raise RuntimeError("Could not discover dashboard_api Lambda function name")

        resp = lambda_client.invoke(
            FunctionName=func_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode(),
        )

        payload = json.loads(resp["Payload"].read())
        status_code = payload.get("statusCode", 500)

        if status_code == 200:
            logger.info("Lambda invoke %s succeeded for %s", decision, approval_id)
            return ApprovalResult(
                approval_id=approval_id,
                status=decision + "d",
                method="lambda_invoke",
            )

        if status_code in (410, 404):
            logger.warning(
                "Lambda invoke %s returned %d for %s — falling through",
                decision,
                status_code,
                approval_id,
            )
            return None

        raise RuntimeError(f"Lambda invoke {decision} returned {status_code}: {payload.get('body', '')}")

    def _act_via_sfn(
        self,
        approval_id: str,
        *,
        decision: str,
        comments: str,
        reason: str,
    ) -> ApprovalResult | None:
        """Path 3: Read task token from DDB and call SFN directly."""
        dynamodb = self._session.resource("dynamodb")
        tokens_table = dynamodb.Table(TASK_TOKENS_TABLE)

        # Read the task token
        resp = tokens_table.get_item(Key={"approval_id": approval_id})
        item = resp.get("Item")
        if not item or not item.get("task_token"):
            raise RuntimeError(f"No task token found in {TASK_TOKENS_TABLE} for approval_id={approval_id}")

        task_token = item["task_token"]

        # Send the task result
        sfn_client = self._session.client("stepfunctions")

        if decision == "approve":
            sfn_client.send_task_success(
                taskToken=task_token,
                output=json.dumps(
                    {
                        "approval_id": approval_id,
                        "status": "approved",
                        "approved_by": "e2e-harness",
                        "comments": comments,
                    }
                ),
            )
        else:
            sfn_client.send_task_failure(
                taskToken=task_token,
                error="RejectedByE2E",
                cause=reason or "e2e reject",
            )

        # Delete the token (one-time use)
        try:
            tokens_table.delete_item(Key={"approval_id": approval_id})
        except Exception as exc:
            logger.debug("Token cleanup failed (non-fatal): %s", exc)

        logger.info("Raw SFN %s succeeded for %s", decision, approval_id)
        return ApprovalResult(
            approval_id=approval_id,
            status=decision + "d",
            method="raw_sfn",
        )

    # ── Private: helpers ──────────────────────────────────────────────

    def _find_dashboard_lambda(self) -> str | None:
        """Discover the dashboard_api Lambda function name."""
        try:
            lambda_client = self._session.client("lambda")
            paginator = lambda_client.get_paginator("list_functions")
            for page in paginator.paginate(MaxItems=200):
                for fn in page.get("Functions", []):
                    name = fn.get("FunctionName", "")
                    if "dashboard" in name.lower() and "api" in name.lower():
                        return name
        except Exception as exc:
            logger.debug("Lambda discovery failed: %s", exc)
        return None
