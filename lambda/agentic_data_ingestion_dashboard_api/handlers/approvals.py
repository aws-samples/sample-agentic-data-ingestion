#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approval management handlers for dashboard API"""

import json
import re
from datetime import UTC, datetime
from typing import Any, cast

import boto3
from boto3.dynamodb.conditions import ConditionBase, Key
from botocore.exceptions import ClientError

from audit_utils import extract_request_context, write_audit_entry
from dynamo_typing import item_list, item_str
from mapping_memory import MappingMemoryClient
from mapping_reuse import revoke_mapping_reuse
from sfn_token_errors import TOKEN_CONSUMED_ERROR_CODES
from step_names import from_any

from .common import approvals_table, create_response, handle_error, logger, require_env, state_table

_dynamodb = boto3.resource("dynamodb")
task_tokens_table = _dynamodb.Table(require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"))

# Step Functions error codes meaning the task token was already consumed or has
# expired — the execution is no longer waiting on this approval (P28 R2.3).
# Aliased to the shared single source (shared/sfn_token_errors.py, p28b R3 —
# the R18.2 single-source habit); behavior unchanged.
_TOKEN_CONSUMED_ERROR_CODES = TOKEN_CONSUMED_ERROR_CODES

# Approvals status GSI (data-stack.ts): partition `status`, sort `created_at`.
# The design doc's R17 name for this access pattern is the "status GSI"; the
# deployed index is status-created-index because approval rows carry
# `created_at` (written by the approval_handler), not `created_timestamp`.
_STATUS_INDEX = "status-created-index"

# Approvals pipeline GSI (data-stack.ts:126): partition `pipeline_id`, sort
# `status`, ProjectionType.ALL. Deployed since the table was created and read by
# NOTHING until p51 C1 — recorded here so the next reader can tell a deliberate
# first use from an oversight (p51 R5.4).
#
# It answers "the approvals for this pipeline" as ONE keyed Query, which is why
# p51 R1.3 forbids a filter walk for that question: the key condition *is* the
# question, so no FilterExpression and no client-side pipeline filter is involved.
#
# NOTE ON ORDERING, measured live rather than assumed (p51 task 1.1): a
# partition-only read here comes back ordered by the range key `status`
# (approved…, pending, rejected…), NOT by `created_at`. So `get_approvals`' sort
# below is load-bearing on this index, not incidental.
_PIPELINE_STATUS_INDEX = "pipeline-status-index"

# Closed set of approval lifecycle statuses (P28 R17.1). Writers: the
# approval_handler creates rows as "pending"; decisions move them to
# "approved" / "rejected" (dashboard + timeout handler); consumed/expired
# task tokens mark them "expired" (_mark_approval_expired below). The
# unfiltered listing enumerates this set with one Query per status instead
# of scanning the table.
_APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")

# P28 R8.3 (task 14.2): state rows are never keyed by approval_type anymore
# (those were phantom rows). The status echo targets the CANONICAL step row
# and only updates an existing row. test_failure gets NO echo — the test_code
# row's honest "failed" status (R4.3) must survive an approve-anyway decision.
# Keep this policy in sync with the approval_handler Lambda's
# state_echo_step_name.
_APPROVAL_TYPES_WITHOUT_STATE_ECHO = frozenset({"test_failure"})


def _authenticated_email(event: dict[str, Any] | None) -> str:
    """Return the verified ``email`` claim from the request, or ``""``.

    Read straight off ``requestContext.authorizer.claims`` rather than through
    ``extract_request_context``, whose return shape is exactly
    ``{"actor", "ip_address"}``: all ten of its call sites splat the result
    (``write_audit_entry(..., **ctx)``), so a third key would be a ``TypeError``
    at each one. That two-key shape is pinned by
    ``tests/unit/shared/test_audit_utils.py::test_returns_exactly_actor_and_ip_address``
    and by a property test over generated events, so the email is read here
    instead of widening the resolver.

    Why the email is wanted at all: this User Pool is configured
    ``UsernameAttributes: ["email"]``, so ``sub`` and ``cognito:username`` are
    byte-identical UUIDs and ``email`` is the only human-meaningful claim. The
    attributed identity stays the stable ``sub`` the resolver returns (R2.1);
    the email rides along as an audit *detail* so the trail stays legible to a
    human reading it.

    This is a claim from the authorizer, not client input — the Cognito User Pool
    authorizer validated the token before API Gateway populated it. A body or
    header value would be unverifiable and is never read here (R1.5).

    Never raises, for any input shape. A non-dict anywhere on the path, a missing
    key and a non-string value all yield ``""``: this feeds a detail field on a
    fire-and-forget audit write, so it must not be able to fail the decision it
    is describing.
    """
    if not isinstance(event, dict):
        return ""
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return ""
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, dict):
        return ""
    claims = authorizer.get("claims")
    if not isinstance(claims, dict):
        return ""
    email = claims.get("email")
    return email if isinstance(email, str) else ""


def _decision_audit_details(
    pipeline_id: str,
    event: dict[str, Any] | None,
    claimed_by: str,
) -> dict[str, Any]:
    """Build the ``details`` map for an approve/reject audit entry.

    Shared by ``approve_request`` and ``reject_request`` so the two decision
    paths cannot drift — a one-sided identity change is the failure mode
    ``LGR-c284fbb-7`` describes in another area: an inert change on one path
    while every test passes on the other.

    ``actor_email`` and ``claimed_by`` are both omitted when empty rather than
    written blank, so an audit row never asserts an identity it does not have.
    """
    details: dict[str, Any] = {"pipeline_id": pipeline_id}
    actor_email = _authenticated_email(event)
    if actor_email:
        details["actor_email"] = actor_email
    # R2.2: the body-supplied identity is retained for diagnostics only. It is
    # real — `useApprovalAction.ts:53-54` derives it from the ID token client-side
    # — but it is unverifiable server-side, so it is recorded as a *claim* beside
    # the verified actor and never as the attributed identity. Keeping it is what
    # makes a disagreement between claimed and verified identity detectable.
    if claimed_by:
        details["claimed_by"] = claimed_by
    return details


def _state_echo_step_name(approval_type: str) -> str | None:
    """Canonical state-table step name for an approval-status echo (or None)."""
    if not approval_type or approval_type in _APPROVAL_TYPES_WITHOUT_STATE_ECHO:
        return None
    try:
        return str(from_any(approval_type))
    except ValueError:
        logger.warning(f"No canonical step for approval_type {approval_type!r} — skipping state-row echo")
        return None


def _annotate_execution_row(pipeline_id: str, run_id: str, status: str, *, context: str) -> None:
    """Clear the pending-approval denormalization and set the row's status.

    ONE write path for both decision branches, so the guard below cannot be
    forgotten by a third caller.

    ``attribute_exists(run_id)`` — annotate an EXISTING row only. Without it
    ``update_item`` CREATES the row (p48 R9.2), and the row it would create is
    ``{pipeline_id, run_id, status, updated_at}`` with **no ``started_at``**.
    Neither decision branch knows a run's start time, and synthesising one
    would put a fabricated timestamp on ``RunsByTimeIndex``' sort key
    (p48 R1.4) — so the row is refused rather than invented. Same guard
    ``retry_run`` uses on this table (``pipelines_runs.py``) and
    ``cache_step_result`` uses one table over (``LGR-60c4985-4``).

    Deliberately NOT a state-row echo: each decision branch echoes onto the
    state row in its own step (``approve_request`` inline,
    ``reject_request`` through :func:`_echo_rejection_onto_state_row`), so a
    failure of one write can never skip the other.

    Bookkeeping ABOUT a decision, not part of it — it must never fail the
    request (``LGR-eee8ac8-4``).
    """
    from .common import executions_table

    try:
        executions_table.update_item(
            Key={"pipeline_id": pipeline_id, "run_id": run_id},
            UpdateExpression="REMOVE pending_approval_id, pending_approval_type SET #s = :status, updated_at = :now",
            ConditionExpression="attribute_exists(run_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":now": datetime.now(UTC).isoformat(),
            },
        )
        logger.info(f"Cleared pending_approval from execution: {pipeline_id}/{run_id} → {status}")
    except ClientError as exec_err:
        if exec_err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Row absent — there is nothing to annotate. Not a failure: the
            # decision itself already landed on the approvals row and (for the
            # approve path) the task token has already been sent.
            logger.info(
                f"No execution row for {pipeline_id}/{run_id} — no {context} status to annotate (the decision stands)"
            )
            return
        logger.warning(f"Failed to update execution record after {context}: {exec_err}")
    except Exception as exec_err:
        logger.warning(f"Failed to update execution record after {context}: {exec_err}")


def _echo_rejection_onto_state_row(run_id: str, approval_type: str) -> str:
    """Set the run's canonical step row to ``rejected``; return its ``mapping_id``.

    The rejection arm of ``approve_request``'s echo: same step mapping
    (:func:`_state_echo_step_name`, so ``test_failure`` gets none), same
    ``attribute_exists(run_id)`` guard so it can never create a row, and the same
    classification of a row-absent outcome as ordinary. ``execution_updater``
    derives FAILED from ``rejected``.

    Returns the row's ``mapping_id`` (``""`` when there is no row or none on it)
    so the caller can revoke that content from reuse. Never raises: this is
    bookkeeping about a decision that has already landed (``LGR-eee8ac8-4``).
    """
    echo_step = _state_echo_step_name(approval_type) if run_id else None
    if not run_id or echo_step is None:
        return ""
    try:
        response = state_table.update_item(
            Key={"run_id": run_id, "step_name": echo_step},
            UpdateExpression="SET #s = :status, updated_at = :now",
            ConditionExpression="attribute_exists(run_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "rejected",
                ":now": datetime.now(UTC).isoformat(),
            },
            ReturnValues="ALL_NEW",
        )
        logger.info(f"Updated state table: {run_id}/{echo_step} → rejected")
        attributes = response.get("Attributes") if isinstance(response, dict) else None
        return item_str(attributes, "mapping_id") if isinstance(attributes, dict) else ""
    except ClientError as state_err:
        if state_err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(f"No {run_id} state row to echo the rejection onto (the decision stands)")
        else:
            logger.warning(f"Failed to echo rejection onto the state row: {state_err}")
    except Exception as state_err:
        logger.warning(f"Failed to echo rejection onto the state row: {state_err}")
    return ""


_REUSED_MAPPING_ID_PATTERN = re.compile(r'"reused_mapping_id"\s*:\s*"([^"]+)"')


def _reused_mapping_id(approval_record: dict[str, Any]) -> str:
    """The mapping a reuse run's approval was about, or ``""``.

    ``approval_handler._reused_mapping_payload`` writes it into ``approval_data``
    on both of its reuse states. ``approval_data`` is a JSON string that the
    handler truncates past 50,000 characters, which breaks the JSON, so a
    pattern search over the raw string is the fallback.
    """
    raw = approval_record.get("approval_data")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        value = parsed.get("reused_mapping_id")
        return value if isinstance(value, str) else ""
    match = _REUSED_MAPPING_ID_PATTERN.search(raw)
    return match.group(1) if match else ""


def _revoke_rejected_mapping(
    pipeline_id: str, approval_id: str, approval_record: dict[str, Any], own_mapping_id: str
) -> None:
    """Withdraw every mapping this rejection refused from reuse by later runs.

    Two sources, because a schema-mapping approval is about one of two things:
    the mapping on the run's own row (a fresh mapping), or the earlier run's
    mapping a reuse run resolved to (``approval_data.reused_mapping_id``). A reuse
    run has no mapping row of its own, so without the second source a
    rejection there left the mapping reusable, and the next run proposed it again.

    Never raises: the decision has already landed. A failure here is logged as a
    warning; its cost is that the next run may propose the same mapping, which
    still has to pass the approval gate (``LGR-5d68eeb-1``).
    """
    mapping_ids = {own_mapping_id, _reused_mapping_id(approval_record)} - {""}
    for mapping_id in sorted(mapping_ids):
        try:
            revoked = revoke_mapping_reuse(state_table, pipeline_id, mapping_id, approval_id=approval_id)
            logger.info(f"Revoked {mapping_id} from reuse on {revoked} row(s) of {pipeline_id}")
        except Exception as revoke_err:
            logger.warning(f"Failed to revoke {mapping_id} from reuse for {pipeline_id}: {revoke_err}")


def _query_all(table, **query_kwargs: Any) -> list[dict[str, Any]]:
    """Query a DynamoDB table/GSI, paginating through all pages (query returns max 1MB per call)."""
    items: list[dict[str, Any]] = []
    while True:
        response = table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def _query_approvals_by_status(status: str) -> list[dict[str, Any]]:
    """All approval rows with the given status, via the status GSI (P28 R17.1).

    Query, not Scan: the read is bounded to the requested status partition.
    """
    return _query_all(
        approvals_table,
        IndexName=_STATUS_INDEX,
        KeyConditionExpression=Key("status").eq(status),
    )


def _query_approvals_by_pipeline(pipeline_id: str, status: str | None = None) -> list[dict[str, Any]]:
    """Approval rows for ONE pipeline, via the pipeline GSI (p51 R1.3).

    Keyed, not walked, in both shapes:

    - `status` given  → `pipeline_id = :pid AND status = :st`, the index's full
      key. Not unique: a GSI key admits duplicates, and one live pipeline holds
      seven `approved` rows under it (p51 task 1.1).
    - `status` omitted → the partition alone, i.e. every approval of that
      pipeline whatever its status.

    Neither shape carries a `FilterExpression`: the key condition is the whole
    question, so `ScannedCount == Count` on both (verified live, p51 task 1.1).
    That exactness is what R1.3 requires and what a walk over `_STATUS_INDEX`
    plus a client-side `pipeline_id` filter would not give.

    The approvals status vocabulary is **lowercase** (`_APPROVAL_STATUSES`) while
    the executions table's is upper-case (`"RUNNING"`), and `status` reaches the
    key condition exactly as given — so a copy-pasted `"PENDING"` here returns
    zero rows with no error. That is why the tests assert the key condition itself
    and not merely the returned rows: an empty result is indistinguishable from a
    legitimately empty pipeline.

    A falsy `status` (`None`, or `?status=` with an empty value) means "not
    given" and takes the partition-only shape. That is the same truthiness test
    `get_approvals` already applies to `?status=`, so the two parameters treat an
    empty value identically. Neither is **validated**: an unknown status is one
    Query against an empty partition and a 200 with `[]`, not a 400. That stance
    is inherited from `?status=` on purpose (it is pinned by
    `test_approvals_dispatch_characterization.py`), so adding a pipeline key does
    not change what a caller gets for a value the table does not hold.
    """
    # Annotated as the base type because the two shapes are different classes
    # (`Equals`, then `And`); without it mypy infers `Equals` from the first
    # assignment and rejects the narrowing branch.
    key_condition: ConditionBase = Key("pipeline_id").eq(pipeline_id)
    if status:
        key_condition = key_condition & Key("status").eq(status)

    return _query_all(
        approvals_table,
        IndexName=_PIPELINE_STATUS_INDEX,
        KeyConditionExpression=key_condition,
    )


def _get_task_token(approval_id: str) -> str:
    """Read the task token from the dedicated table WITHOUT deleting it.

    The record is only deleted after Step Functions confirms the send
    (read token → send → delete, P28 R2.1), so a failed send leaves the
    approval retryable.

    Raises ValueError if token not found or expired.
    """
    response = task_tokens_table.get_item(Key={"approval_id": approval_id})
    item = response.get("Item")
    if not item:
        raise ValueError(f"Task token not found or expired for approval {approval_id}")

    return cast(str, item["task_token"])


def _delete_task_token(approval_id: str) -> None:
    """Delete the task-token record once the token has been consumed."""
    task_tokens_table.delete_item(Key={"approval_id": approval_id})
    logger.info(f"Deleted task token record for approval {approval_id}")


def _mark_approval_expired(approval_id: str) -> None:
    """Mark an approval as expired: its token was already consumed or timed out."""
    approvals_table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #status = :status, expired_at = :expired_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "expired",
            ":expired_at": datetime.now(UTC).isoformat(),
        },
    )


def _resolve_consumed_token(approval_id: str, error: ClientError) -> dict[str, Any] | None:
    """Classify a send_task_success/failure ClientError (P28 R2.2/R2.3).

    Already-consumed/expired token (TaskTimedOut, TaskDoesNotExist, InvalidToken):
    the execution is no longer waiting on this approval — delete the token record,
    mark the approval expired, and return an honest 409 response.

    Any other ClientError (throttling, service 5xx) is transient: return None so
    the caller re-raises — the token record stays intact and the approval stays
    pending, letting the user retry.
    """
    error_code = error.response.get("Error", {}).get("Code", "")
    if error_code not in _TOKEN_CONSUMED_ERROR_CODES:
        return None

    logger.warning(f"Task token for approval {approval_id} already consumed or expired ({error_code})")
    _delete_task_token(approval_id)
    _mark_approval_expired(approval_id)
    return create_response(
        409,
        {
            "error": "Approval already processed or expired",
            "detail": (
                f"Step Functions rejected the task token ({error_code}); "
                "the pipeline is no longer waiting on this approval"
            ),
            "approval_id": approval_id,
            "status": "expired",
        },
    )


def get_approvals(query_params: dict[str, str]) -> dict[str, Any]:
    """Get approval requests (general endpoint, not just pending)"""
    try:
        status_filter = query_params.get("status")
        pipeline_filter = query_params.get("pipeline_id")

        if pipeline_filter:
            # `?pipeline_id=` (p51 C1 / R1.1-R1.3): one keyed Query on the
            # pipeline GSI, narrowed further by `?status=` when both are given.
            # No matches is a 200 with `[]` — never a 404.
            #
            # NO GHOST FILTER HERE, deliberately. `get_pending_approvals` below
            # drops rows lacking `pipeline_id` (`:397`; `:316` before this change,
            # which is the line p51's spec cites) because it reads the *status*
            # partition, where such a row is present. On this index a row with no
            # `pipeline_id` is absent BY CONSTRUCTION: `LGR-7d19eca-2`
            # (**invariant**) — a GSI indexes only items carrying both key
            # attributes — so the equivalent filter here could never fire, and
            # writing one would suggest the exclusion depends on it. It does not;
            # it is structural. (This path already had no ghost filter before p51,
            # so the omission is consistency with it rather than a new asymmetry.)
            approvals = _query_approvals_by_pipeline(pipeline_filter, status_filter)
        elif status_filter:
            approvals = _query_approvals_by_status(status_filter)
        else:
            # No status filter: enumerate the closed status set with one Query
            # per status instead of scanning the table (P28 R17.1). Rows
            # outside the lifecycle set (or missing created_at, and therefore
            # absent from the sparse GSI) are ghost/partial records the
            # dashboard never renders meaningfully.
            approvals = [item for status in _APPROVAL_STATUSES for item in _query_approvals_by_status(status)]

        # Normalize field names: DynamoDB uses pipeline_id, frontend expects pipeline_key
        for approval in approvals:
            if "pipeline_id" in approval and "pipeline_key" not in approval:
                approval["pipeline_key"] = approval["pipeline_id"]

        # Sort by created_at timestamp (most recent first).
        #
        # LOAD-BEARING on the `?pipeline_id=` path, not incidental: that index's
        # sort key is `status`, so a partition-only read arrives ordered by status
        # (measured live, p51 task 1.1) and this is the only thing that makes the
        # response `created_at`-descending as the contract suites expect.
        #
        # An undated row sorts LAST, and on this path that is reachable for the
        # first time — `_STATUS_INDEX` is sparse on `created_at`, so a row without
        # one is absent from it (`LGR-7d19eca-2`), while `_PIPELINE_STATUS_INDEX`
        # does index it. Last place is kept deliberately: the order claims
        # recency, an undated row's recency is unknown, and first place would
        # assert it is the newest. It is not dropped either — that would cost the
        # row its Approve/Reject affordance, which is the defect p51 R1.1 exists
        # to fix.
        approvals.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        return create_response(200, approvals)

    except Exception as e:
        return handle_error(e, "Error fetching approvals")


def get_pending_approvals() -> dict[str, Any]:
    """Get all pending approval requests, normalizing field names for the frontend."""
    try:
        pending_approvals = _query_approvals_by_status("pending")

        # Drop "ghost" rows: partial records that only have approval_id/status/last_updated.
        # A real approval always carries pipeline_id (set by the approval_handler put_item),
        # so the absence of pipeline_id reliably identifies a ghost. These otherwise render
        # as blank Type / blank Pipeline / "NaNm" rows on the dashboard.
        pending_approvals = [a for a in pending_approvals if item_str(a, "pipeline_id")]

        # Normalize field names: DynamoDB uses pipeline_id, frontend expects pipeline_key
        for approval in pending_approvals:
            if "pipeline_id" in approval and "pipeline_key" not in approval:
                approval["pipeline_key"] = approval["pipeline_id"]

        # Sort by created_at timestamp (most recent first)
        pending_approvals.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        return create_response(200, pending_approvals)

    except Exception as e:
        return handle_error(e, "Error fetching pending approvals")


# ── The stored payload has THREE states, and they are three different facts ───
#
# `approval_data` is a JSON-string attribute on the approvals row, so reading it
# can end three ways and each says something different to an approver:
#
#   "parsed"     — the attribute was there and decoded to an object. That object
#                  is `details`. It may be `{}`, and it may be structurally
#                  complete with every value `null`; both are things the PRODUCER
#                  established, and the field readers already report them.
#   "absent"     — no attribute (or an empty one). The producer stored nothing.
#   "unreadable" — an attribute was stored and we could not interpret it.
#
# The distinction between the last two is the whole reason this is a named field
# rather than an empty dict. `LGR-4854b3d-1` (**template / ratified**) ruled the
# three-state shape for `field_count` and refused to collapse `null` onto `0`,
# because "every hand-written `?? 0` collapses null into 'declares nothing'".
# `{}` here would be that collapse one column over: it reads as *the producer
# stored nothing* when the truth is *we could not read what it stored*. p54
# already drew this same line for a single field —
# `REUSED_MAPPING_UNREADABLE_STATEMENT` exists so an unreadable reused mapping
# never renders as a mapping with no rows (`LGR-b2f79ef-14`: a named reference to
# something absent is not a thing with no fields) — and this is that reasoning
# applied to the whole payload rather than to one key inside it.
#
# WHY A SIBLING DISCRIMINATOR AND NOT A NULLABLE `details`. `details: null` was
# rejected: every existing reader spells its access `approval.details ?? {}`
# (`ApprovalArtifact.tsx:65`, `:130`, `:515`, `:617`), and `??` catches `null` and
# `undefined` alike — so a nullable `details` would be indistinguishable from an
# absent one at all four, which is exactly the collapse this field exists to
# prevent. Keeping `details` shaped as those readers already expect is also what
# lets every one of them be REUSED rather than re-implemented (`LGR-e59edd7-6`).
#
# `details` is OMITTED on the two non-parsed states rather than sent as `{}`, so a
# consumer that ignores this field cannot read "the producer stored an empty
# payload" out of a read that failed.
APPROVAL_DETAILS_PARSED = "parsed"
APPROVAL_DETAILS_ABSENT = "absent"
APPROVAL_DETAILS_UNREADABLE = "unreadable"


def _classify_approval_data(raw: Any) -> tuple[str, dict[str, Any] | None]:
    """Classify a stored ``approval_data`` value into one of the three states.

    Returns ``(status, details)`` where ``details`` is non-``None`` only for
    ``APPROVAL_DETAILS_PARSED``.

    The non-dict rule is ONE rule with no special cases: anything that decodes to
    something other than a JSON object is ``unreadable``, because a list, a
    number, a string or a literal ``null`` is not a details map and there is no
    honest way to read fields off it. This matters beyond tidiness —
    ``get_run_details``' parse (``pipelines_runs.py``) assigns whatever
    ``json.loads`` returns straight to ``details``, so a stored ``"[]"`` would
    reach the frontend typed ``Record<string, unknown>`` while actually being an
    array. Recorded rather than fixed there: that file is outside this change.

    A ``dict`` arriving unparsed is passed through — the attribute is declared a
    string, but ``get_run_details`` already tolerates a Map-typed value
    (``isinstance(raw, str)``) and diverging from that would be a second opinion
    about the same stored shape.
    """
    if not raw:
        # Absent attribute, empty string, or an empty Map. The producer recorded
        # no payload; there is nothing that failed to be read.
        return APPROVAL_DETAILS_ABSENT, None
    if isinstance(raw, dict):
        return APPROVAL_DETAILS_PARSED, raw
    if not isinstance(raw, str):
        return APPROVAL_DETAILS_UNREADABLE, None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return APPROVAL_DETAILS_UNREADABLE, None
    if not isinstance(decoded, dict):
        return APPROVAL_DETAILS_UNREADABLE, None
    return APPROVAL_DETAILS_PARSED, decoded


def get_approval(approval_id: str) -> dict[str, Any]:
    """One approval, with its stored ``approval_data`` parsed into ``details``.

    ``GET /api/approvals/{approval_id}``.

    WHY THIS ENDPOINT EXISTS. `ApprovalsPage`'s review modal reads
    ``approval.details``, and neither listing handler produced that key — so every
    reader on that surface correctly reported "not established" for the mapping
    count, the unsourced target fields, the confidence, the proposed renames and
    `MappingViewer`'s whole panel. The run-detail Approvals tab worked because
    ``get_run_details`` parses the payload, which is why the defect was
    surface-specific rather than global.

    WHY NOT PARSE IT IN THE LISTING INSTEAD. ``get_approvals`` unfiltered
    enumerates all four statuses and ``_query_all`` paginates every row with no
    cap, so parsing there would ship every approval's full ``proposed_mappings``
    on every page load in order to serve a modal opened for one row. This repo
    already settled that shape: ``LGR-14d6dd3-2`` (**template / ratified**) moved
    per-row schema field resolution OFF the listing and onto
    ``GET /api/schemas/{id}``, because a paginated listing that also resolves
    per-row payloads makes the endpoint's cost a function of an unmeasured
    per-row read. Same reasoning, same answer: the cost belongs on the request
    that needs the payload.

    A missing approval is a ``404`` naming the id, matching what ``approve_request``
    and ``reject_request`` already answer for the same input (``f"Approval not
    found: {approval_id}"``) — one sentence for one fact, so a client cannot get
    two spellings of "no such approval" from one resource.

    DELIBERATELY NOT SYNTHESISED HERE: ``decided_by`` / ``decided_by_email`` /
    ``decided_at`` / ``comment``. ``get_run_details`` collapses the arm-specific
    stored names into those four on ITS read path, and ``LGR-e59edd7-3`` /
    ``LGR-e59edd7-4`` (both **template / ratified**) make that collapse one
    authority precisely so two surfaces cannot disagree about a deciding
    identity. Adding a second collapse here would be that drift. The review modal
    reads none of the four; `ApprovalWorkflowSection` does, and it is fed by
    ``get_run_details``.

    ``approval_data`` itself is NOT echoed back. ``details`` supersedes it, no
    frontend file reads the raw attribute (verified by grep over
    ``dashboard-react/src/``), and a `schema_mapping` payload runs to ~6 KB — so
    returning both would double the response for no reader.
    """
    try:
        response = approvals_table.get_item(Key={"approval_id": approval_id})
        approval = response.get("Item")
        if not approval:
            return create_response(404, {"error": f"Approval not found: {approval_id}"})

        details_status, details = _classify_approval_data(approval.pop("approval_data", None))
        approval["details_status"] = details_status
        if details is not None:
            approval["details"] = details

        # Same normalization both listing handlers apply: DynamoDB stores
        # `pipeline_id`, the frontend reads `pipeline_key`. Kept identical rather
        # than tidied, so the modal's Pipeline field and its row link resolve off
        # this response exactly as they do off the listing.
        if "pipeline_id" in approval and "pipeline_key" not in approval:
            approval["pipeline_key"] = approval["pipeline_id"]

        if details_status != APPROVAL_DETAILS_PARSED:
            logger.info(f"Approval {approval_id} payload {details_status} — details omitted from the response")

        return create_response(200, approval)

    except Exception as e:
        return handle_error(e, f"Error fetching approval {approval_id}")


def approve_request(approval_id: str, body: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve a pending request and resume Step Functions execution"""
    try:
        data = json.loads(body) if body else {}
        # R2.1: the approver is the authenticated principal, resolved by the same
        # resolver every audit write uses. R2.5 is satisfied without a new
        # parameter — `event` was already threaded here for the audit write, and
        # this is that same single call, hoisted so the identity is in hand before
        # it is needed by the Step Functions payload and the approvals row below.
        # `extract_request_context` cannot raise, so hoisting it above the record
        # read cannot turn a 404 into a 500.
        ctx = extract_request_context(event or {})
        approved_by = ctx["actor"]
        # p50 C3 (R2.2): the human-meaningful identity, from the same verified
        # claim the audit detail already records. Written *beside* `approved_by`,
        # never instead of it (R3.3) — `approved_by` stays the stable `sub`
        # `LGR-c58f276-2` ratified as the attributed identity, so anything reading
        # it as an id keeps working. This closes that record's `revisit_when`:
        # until now the email reached only the audit_log row, and "Decided by"
        # reads the approvals row.
        #
        # Deliberately NOT taken from `ctx`: `extract_request_context` returns
        # exactly {"actor", "ip_address"} and all 7 production call sites splat it
        # into `write_audit_entry(..., **ctx)`, so a third key is a `TypeError` at
        # each — `LGR-c58f276-3`, a PROPOSED (not ratified) invariant.
        #
        # Resolved here rather than threaded out of `_decision_audit_details`:
        # `_authenticated_email` is pure over `event` and does no I/O, so the row
        # and the audit detail cannot disagree however many times it is called.
        approved_by_email = _authenticated_email(event)
        # R2.2: never let the body override the attributed identity; R2.3: the
        # fallback is the resolver's terminal "unknown", not "dashboard-user".
        claimed_by = data.get("approved_by") or data.get("user") or ""
        comments = data.get("comments", "")

        # Get the approval record
        approval_response = approvals_table.get_item(Key={"approval_id": approval_id})
        approval_record = approval_response.get("Item")
        if not approval_record:
            return create_response(404, {"error": f"Approval not found: {approval_id}"})

        # Get task token from dedicated table (if approval has one) — read only,
        # the record is deleted after the send succeeds (P28 R2.1)
        task_token = None
        has_token = approval_record.get("has_task_token", False)
        if has_token:
            try:
                task_token = _get_task_token(approval_id)
            except ValueError as e:
                logger.warning(f"Task token unavailable for {approval_id}: {e}")
                return create_response(
                    410,
                    {
                        "error": "Task token expired or not found",
                        "detail": str(e),
                        "approval_id": approval_id,
                    },
                )

        # Resume Step Functions execution BEFORE deleting the token or marking the
        # approval processed (read token → send → delete, P28 R2.1). A failed send
        # must leave the approval actionable.
        if task_token:
            sfn_client = boto3.client("stepfunctions")
            try:
                sfn_client.send_task_success(
                    taskToken=task_token,
                    output=json.dumps(
                        {
                            "approval_id": approval_id,
                            "status": "approved",
                            "approved_by": approved_by,
                            "comments": comments,
                        }
                    ),
                )
            except ClientError as send_err:
                consumed_response = _resolve_consumed_token(approval_id, send_err)
                if consumed_response is not None:
                    return consumed_response
                # Transient failure (throttle, service 5xx): keep the token record and
                # leave the approval pending so the user can retry (P28 R2.2).
                logger.error(f"Transient failure resuming Step Functions for approval {approval_id}: {send_err}")
                raise
            logger.info(f"Resumed Step Functions execution for approval {approval_id}")

            # Send succeeded — the token is consumed; delete its record now
            _delete_task_token(approval_id)

        # Update approval status (only after a successful send)
        update_expression = (
            "SET #status = :status, approved_by = :approved_by, approved_at = :approved_at, comments = :comments"
        )
        expression_values: dict[str, Any] = {
            ":status": "approved",
            ":approved_by": approved_by,
            ":approved_at": datetime.now(UTC).isoformat(),
            ":comments": comments,
        }
        # p50 C3: appended only when an email resolved. `LGR-c58f276-2`'s
        # convention — an identity detail is omitted when empty rather than
        # written blank, so a row never asserts an identity it does not have. A
        # blank attribute would also defeat task 3.2's read-path projection
        # (`approved_by_email or rejected_by_email`), which relies on falsiness to
        # fall through to the `sub`.
        if approved_by_email:
            update_expression += ", approved_by_email = :approved_by_email"
            expression_values[":approved_by_email"] = approved_by_email
        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=expression_values,
        )

        # The run's identifiers, read ONCE here because everything below wants
        # them: the executions-row clear immediately after, the Memory block's
        # `pipeline_id` and its mapping_id lookup, and the state echo further
        # down. `item_str` rather than a bare `.get` so each is a `str` — two of
        # them go into DynamoDB Key dicts, where a number or `None` is a request
        # error. A non-string `approval_type` therefore reads as `""`, which
        # takes the same branch a non-string took before (neither equals
        # `"schema_mapping"`), so collapsing the two reads into one changes no
        # branch.
        run_id = item_str(approval_record, "run_id")
        approval_type = item_str(approval_record, "approval_type")
        pipeline_id = item_str(approval_record, "pipeline_id")

        # Clear the pending-approval denormalization HERE — immediately after the
        # approvals row is marked approved, and BEFORE the Memory block below.
        #
        # `get_run_details` DERIVES `status = "WAITING_APPROVAL"` and
        # `current_step = pending_approval_type` from the mere presence of
        # `pending_approval_id` on the executions row (`pipelines_runs.py`);
        # nothing writes that status literal. So until this write lands, Recent
        # Runs keeps rendering "Awaiting Approval" for a run Step Functions
        # resumed at `send_task_success` above. The Memory block that used to sit
        # in between is 20+ serial AgentCore round trips for a 20-field schema —
        # `store_mapping_decision` does a `list_memory_records` and sometimes a
        # `batch_update_memory_records` per field (`shared/mapping_memory.py`) —
        # and that was the whole visible staleness window.
        #
        # Still AFTER the send, and that is load-bearing (P28 R2.1, and the
        # comment at the send above): a failed send must leave the approval
        # actionable, so no row may be marked RUNNING for a run that never
        # resumed. Do not hoist this above the send.
        if run_id and pipeline_id:
            _annotate_execution_row(
                pipeline_id,
                run_id,
                "RUNNING",
                context="approval",
            )

        # Store Memory feedback for schema mapping approvals
        if approval_type == "schema_mapping":
            try:
                memory_client = MappingMemoryClient()
                mapping_id = item_str(approval_record, "mapping_id")

                # Look up mapping_id from state table if not on approval record
                if not mapping_id and run_id:
                    try:
                        state_resp = state_table.get_item(Key={"run_id": run_id, "step_name": "schema_mapping"})
                        if "Item" in state_resp:
                            mapping_id = item_str(state_resp["Item"], "mapping_id")
                    except Exception as lookup_err:
                        logger.warning(f"Failed to look up mapping_id from state table: {lookup_err}")

                memory_client.store_approval_feedback(mapping_id or approval_id, approved=True)

                # Get proposed mappings — try approval record first, then fetch from mappings table
                proposed_mappings = item_list(approval_record, "proposed_mappings")
                if not proposed_mappings and mapping_id:
                    try:
                        from .common import mappings_table

                        mapping_resp = mappings_table.get_item(Key={"mapping_id": mapping_id})
                        if "Item" in mapping_resp:
                            mapping_result = json.loads(item_str(mapping_resp["Item"], "mapping_result", "{}"))
                            proposed_mappings = mapping_result.get("mappings", [])
                            logger.info(
                                f"Fetched {len(proposed_mappings)} mappings from mappings table for {mapping_id}"
                            )
                    except Exception as fetch_err:
                        logger.warning(f"Failed to fetch mappings from mappings table: {fetch_err}")

                # Also try approval_data JSON if proposed_mappings still empty
                if not proposed_mappings:
                    try:
                        approval_data = json.loads(item_str(approval_record, "approval_data", "{}"))
                        proposed_mappings = approval_data.get("proposed_mappings", [])
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Store each approved mapping decision in Memory
                for mapping in proposed_mappings:
                    memory_client.store_mapping_decision(
                        source_field=mapping.get("source_field", ""),
                        target_field=mapping.get("target_field", ""),
                        confidence=mapping.get("confidence", 0.5),
                        pipeline_id=pipeline_id,
                        approved=True,
                    )
                if proposed_mappings:
                    logger.info(f"Stored {len(proposed_mappings)} approved mapping decisions in Memory")
                else:
                    logger.warning(f"No proposed_mappings found for approval {approval_id}")

            except Exception as mem_err:
                logger.warning(f"Failed to store Memory approval feedback: {mem_err}")

        # Update state table: change step status from waiting_approval back to
        # success. P28 R8.3: the echo targets the CANONICAL step row (never a
        # phantom approval_type row) and only updates an existing row.
        #
        # This try wraps ONLY the echo. It used to also contain the
        # executions-row clear above, and that sharing was a defect, not a
        # convenience: `attribute_exists(run_id)` here raises
        # `ConditionalCheckFailedException` whenever the `{run_id, echo_step}`
        # state row does not exist, and `schema_mapping` is not in
        # `_APPROVAL_TYPES_WITHOUT_STATE_ECHO`, so that raise unwound past the
        # clear and the executions row kept its `pending_approval_id` forever —
        # a run stuck on "Awaiting Approval", reported only as
        # "Failed to update tables after approval". The sibling handler already
        # keeps the two apart for the same reason
        # (`approval_handler/lambda_function.py`: "Its own try/except, not the
        # state echo's: a row-absent outcome here is ordinary and must not be
        # reported as a state-table failure").
        #
        # Row-absent is logged at INFO, not WARNING: it is the guard doing its
        # job, exactly as `_annotate_execution_row` already classifies its own.
        # A genuine failure still warns.
        try:
            echo_step = _state_echo_step_name(approval_type) if run_id else None
            if run_id and echo_step is not None:
                state_table.update_item(
                    Key={"run_id": run_id, "step_name": echo_step},
                    UpdateExpression="SET #s = :status, updated_at = :now",
                    ConditionExpression="attribute_exists(run_id)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": "success",
                        ":now": datetime.now(UTC).isoformat(),
                    },
                )
                logger.info(f"Updated state table: {run_id}/{echo_step} → success")
        except ClientError as state_err:
            if state_err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.info(f"No {run_id} state row to echo the approval onto (the decision stands)")
            else:
                logger.warning(f"Failed to echo approval onto the state row: {state_err}")
        except Exception as state_err:
            logger.warning(f"Failed to echo approval onto the state row: {state_err}")

        # Audit: record approval decision. `ctx` is the same resolution that
        # produced `approved_by` at the top of this function, so the attributed
        # identity on the approvals row and on the audit row cannot disagree.
        pipeline_id_for_audit = cast(str, approval_record.get("pipeline_id", ""))
        write_audit_entry(
            action="approval_approved",
            resource_type="approval",
            resource_id=approval_id,
            details=_decision_audit_details(pipeline_id_for_audit, event, claimed_by),
            **ctx,
        )

        return create_response(
            200,
            {
                "success": True,
                "approval_id": approval_id,
                "status": "approved",
                "resumed_execution": bool(task_token),
                "message": "Request approved successfully",
            },
        )

    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        return handle_error(e, "Error approving request")


def reject_request(approval_id: str, body: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reject a pending request and fail Step Functions execution"""
    try:
        data = json.loads(body) if body else {}
        # R2.1: identical treatment to `approve_request` — same resolver, same
        # hoist, same body-is-a-claim handling. A one-sided change here would be
        # an inert fix on the rejection path that every test still passes.
        ctx = extract_request_context(event or {})
        rejected_by = ctx["actor"]
        # p50 C3 (R2.2): the rejection arm of the same additive write. R2.2 says
        # "after approve and after reject", and a one-sided landing here is the
        # failure mode the docstring above already names. The attribute pairs with
        # the persisted `rejected_by` rather than being a single `decided_by_email`
        # — see the approve branch for the full rationale.
        rejected_by_email = _authenticated_email(event)
        # R2.2 / R2.3, as above.
        claimed_by = data.get("rejected_by") or data.get("user") or ""
        reason = data.get("reason", "")

        # Get the approval record
        approval_response = approvals_table.get_item(Key={"approval_id": approval_id})
        approval_record = approval_response.get("Item")
        if not approval_record:
            return create_response(404, {"error": f"Approval not found: {approval_id}"})

        # Get task token from dedicated table (if approval has one) — read only,
        # the record is deleted after the send succeeds (P28 R2.1)
        task_token = None
        has_token = approval_record.get("has_task_token", False)
        if has_token:
            try:
                task_token = _get_task_token(approval_id)
            except ValueError as e:
                logger.warning(f"Task token unavailable for {approval_id}: {e}")
                return create_response(
                    410,
                    {
                        "error": "Task token expired or not found",
                        "detail": str(e),
                        "approval_id": approval_id,
                    },
                )

        # Fail Step Functions execution BEFORE deleting the token or marking the
        # approval processed (read token → send → delete, P28 R2.1). A failed send
        # must leave the rejection actionable.
        if task_token:
            sfn_client = boto3.client("stepfunctions")
            try:
                sfn_client.send_task_failure(
                    taskToken=task_token,
                    error="ApprovalRejected",
                    cause=json.dumps(
                        {
                            "approval_id": approval_id,
                            "rejected_by": rejected_by,
                            "reason": reason,
                        }
                    ),
                )
            except ClientError as send_err:
                consumed_response = _resolve_consumed_token(approval_id, send_err)
                if consumed_response is not None:
                    return consumed_response
                # Transient failure (throttle, service 5xx): keep the token record and
                # leave the approval pending so the user can retry (P28 R2.2).
                logger.error(f"Transient failure failing Step Functions for approval {approval_id}: {send_err}")
                raise
            logger.info(f"Failed Step Functions execution for rejected approval {approval_id}")

            # Send succeeded — the token is consumed; delete its record now
            _delete_task_token(approval_id)

        # Update approval status (only after a successful send)
        update_expression = (
            "SET #status = :status, rejected_by = :rejected_by, rejected_at = :rejected_at, rejection_reason = :reason"
        )
        expression_values: dict[str, Any] = {
            ":status": "rejected",
            ":rejected_by": rejected_by,
            ":rejected_at": datetime.now(UTC).isoformat(),
            ":reason": reason,
        }
        # p50 C3: omitted when empty, exactly as on the approve path.
        if rejected_by_email:
            update_expression += ", rejected_by_email = :rejected_by_email"
            expression_values[":rejected_by_email"] = rejected_by_email
        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=expression_values,
        )

        # Hoisted for the same reason as on the approve path — the clear below
        # needs them before the Memory block, and the Memory block reads
        # `pipeline_id` too. See the approve branch for why `item_str` here
        # changes no branch.
        run_id = item_str(approval_record, "run_id")
        approval_type = item_str(approval_record, "approval_type")
        pipeline_id = item_str(approval_record, "pipeline_id")

        # Clear the pending-approval denormalization and mark the run FAILED
        # HERE, before the Memory block — the rejection path carries the
        # identical latency and the identical stale "Awaiting Approval" render,
        # and a one-sided fix would be inert on this arm while every test on the
        # other one passed (the failure mode `_decision_audit_details`' docstring
        # names via `LGR-c284fbb-7`). Still after `send_task_failure` above
        # (P28 R2.1) — do not hoist it above the send.
        if run_id and pipeline_id:
            _annotate_execution_row(pipeline_id, run_id, "FAILED", context="rejection")

        # Store Memory feedback for schema mapping rejections
        if approval_type == "schema_mapping":
            try:
                memory_client = MappingMemoryClient()
                mapping_id = item_str(approval_record, "mapping_id", approval_id)
                memory_client.store_approval_feedback(mapping_id, approved=False, reviewer_notes=reason)

                # Update each field mapping's Memory entry with rejection status
                proposed_mappings = item_list(approval_record, "proposed_mappings")
                for mapping in proposed_mappings:
                    memory_client.store_mapping_decision(
                        source_field=mapping["source_field"],
                        target_field=mapping["target_field"],
                        confidence=mapping.get("confidence", 0.5),
                        pipeline_id=pipeline_id,
                        approved=False,
                    )
                logger.info(f"Stored Memory rejection feedback for {len(proposed_mappings)} field mappings")
            except Exception as mem_err:
                logger.warning(f"Failed to store Memory rejection feedback: {mem_err}")

        # State-row echo, the rejection arm of the approve path's. Without it a
        # dashboard-rejected mapping kept whatever status its row last held, and
        # a mapping regeneration during the wait had already rewritten that to
        # `success` — so the next run reused the mapping the human had just
        # refused. This closes `LGR-5d68eeb-2`'s recorded finding; the reuse
        # predicate itself is unchanged in meaning (`success` = approved).
        # Same guard and same error handling as the approve echo: it only
        # updates an existing row, and it never fails the decision.
        own_mapping_id = _echo_rejection_onto_state_row(run_id, approval_type)

        # A rejected mapping usually also lives on rows other than this run's
        # own: a reuse run has no mapping row at all, and identical content can
        # sit on several runs (`mapping_id` is a content hash). Withdraw it from
        # reuse on all of them, without rewriting their status — those rows are
        # other runs' history (`shared/mapping_reuse.py`).
        if approval_type == "schema_mapping" and pipeline_id:
            _revoke_rejected_mapping(pipeline_id, approval_id, approval_record, own_mapping_id)

        # Audit: record rejection decision. `ctx` is the same resolution that
        # produced `rejected_by` at the top of this function.
        pipeline_id_for_audit = cast(str, approval_record.get("pipeline_id", ""))
        write_audit_entry(
            action="approval_rejected",
            resource_type="approval",
            resource_id=approval_id,
            details=_decision_audit_details(pipeline_id_for_audit, event, claimed_by),
            **ctx,
        )

        return create_response(
            200,
            {
                "success": True,
                "approval_id": approval_id,
                "status": "rejected",
                "message": "Request rejected successfully",
            },
        )

    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        return handle_error(e, "Error rejecting approval request")


def cleanup_ghost_approvals() -> dict[str, Any]:
    """Delete orphaned 'pending' approval records whose Step Functions execution
    is no longer running (completed, failed, timed out, or aborted).

    These stale records accumulate when executions end without the approval
    handler cleaning up — e.g. SFN timeout, manual cancellation, or Lambda errors.
    """
    try:
        import boto3 as _boto3

        all_pending = _query_approvals_by_status("pending")
        deleted = 0
        sfn_client = _boto3.client("stepfunctions")

        for approval in all_pending:
            approval_id = item_str(approval, "approval_id")
            execution_arn = item_str(approval, "execution_arn")

            is_stale = False

            if not execution_arn:
                # No execution ARN — can't verify, treat as stale if old (> 24h)
                created_at = item_str(approval, "created_at")
                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
                        if age_hours > 24:
                            is_stale = True
                    except Exception:
                        is_stale = True
                else:
                    is_stale = True
            else:
                # Check if the SFN execution is still running
                try:
                    exec_resp = sfn_client.describe_execution(executionArn=execution_arn)
                    sfn_status = exec_resp.get("status", "")
                    if sfn_status != "RUNNING":
                        is_stale = True
                except Exception:
                    # Execution not found or access denied — treat as stale
                    is_stale = True

            if is_stale and approval_id:
                approvals_table.delete_item(Key={"approval_id": approval_id})
                try:
                    task_tokens_table.delete_item(Key={"approval_id": approval_id})
                except Exception as exc:
                    # Non-fatal: the task-token row may already be gone (one-time use);
                    # the stale approval is still deleted above so cleanup proceeds (P21 Req 6.2/6.5).
                    logger.warning(
                        f"Failed to delete task token for stale approval_id={approval_id}: {type(exc).__name__}: {exc}"
                    )
                deleted += 1

        logger.info(f"Cleaned up {deleted} stale approval records out of {len(all_pending)} pending")
        return create_response(
            200,
            {
                "message": f"Cleaned up {deleted} stale approval records",
                "deleted": deleted,
                "remaining": len(all_pending) - deleted,
            },
        )

    except Exception as e:
        return handle_error(e, "Error cleaning up stale approvals")
