# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add shared directory to path for EventBridge publisher
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from eventbridge_utils import get_eventbridge_publishers

publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event = (
    get_eventbridge_publishers()
)

# P28 R18.2: behavioral thresholds/TTLs come from the single config source.
from config import APPROVAL_RECORD_RETENTION_DAYS, TASK_TOKEN_TTL_SECONDS, default_timeout_hours
from decimal_utils import convert_decimals_to_float
from env_utils import require_env
from sfn_token_errors import is_token_consumed_error
from step_names import StepName, from_any

try:
    from metrics_facade import ApprovalMetrics, record_error, time_function
    from notification_service import send_approval_notification, send_approval_timeout_alert
except ImportError:
    # Fallback if shared module not available
    class ApprovalMetrics:
        @staticmethod
        def record_approval_created(*args, **kwargs):
            pass

        @staticmethod
        def record_approval_processed(*args, **kwargs):
            pass

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

    def send_approval_notification(*args, **kwargs):
        return {"success": False}

    def send_approval_timeout_alert(*args, **kwargs):
        return {"success": False}


# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# P28 R8.3 (task 14.2): the approvals flow no longer writes state rows keyed
# by approval_type ("code_generation", "infrastructure", "first_run", ... were
# phantom rows — no pipeline step writes those names). The status echo that
# the execution updater and the mapping-approval lifecycle depend on is
# written against the CANONICAL step name instead, and ONLY as an update to
# the step's existing row (attribute_exists condition — an echo can never
# create a row). test_failure gets NO state-row echo: the test_code row's
# honest "failed" status (R4.3) must survive the approval; the pending
# approval is carried by the approvals table + the executions-record
# denormalization.
_APPROVAL_TYPES_WITHOUT_STATE_ECHO = frozenset({"test_failure"})


def state_echo_step_name(approval_type: str) -> str | None:
    """Canonical state-table step name for an approval-status echo.

    Returns None when the approval type must not touch state rows (unknown
    types and the cataloged no-echo types).
    """
    if not approval_type or approval_type in _APPROVAL_TYPES_WITHOUT_STATE_ECHO:
        return None
    try:
        return str(from_any(approval_type))
    except ValueError:
        logger.warning(f"No canonical step for approval_type {approval_type!r} — skipping state-row echo")
        return None


# Environment variables
APPROVALS_TABLE = require_env("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
STATE_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
# The ADVERTISED approval window: 72 h, mirroring the gate's
# APPROVAL_TIMEOUT_SECONDS (p34 R6) so `timeout_at` on the record — and the
# deadline the notification states — is the deadline the gate actually gives.
# Env var DEFAULT_TIMEOUT_HOURS still overrides, and a per-pipeline
# approvals.timeout_hours still beats both (see create_approval_request).
# NOTE this is NOT the task-token TTL: that stays at TASK_TOKEN_TTL_SECONDS
# (7 days) so a token outlives the window rather than racing it.
DEFAULT_TIMEOUT_HOURS = default_timeout_hours()

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
stepfunctions = boto3.client("stepfunctions")
sns = boto3.client("sns")
events = boto3.client("events")

approvals_table = dynamodb.Table(APPROVALS_TABLE)
state_table = dynamodb.Table(STATE_TABLE)
task_tokens_table = dynamodb.Table(require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"))
# p54 R1.1: the reuse arm of get_approval_data's schema_mapping branch reads this
# table to resolve a Reuse_Run's mapping. Spelled MAPPINGS_TABLE to match the
# one other reader of the same table (dashboard_api/handlers/common.py:36), and
# supplied by compute-stack.ts's ApprovalHandler environment — require_env raises
# outside local mode, so a missing key is a cold start failure, not a silent
# fallback.
mappings_table = dynamodb.Table(require_env("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"))


#: Cap on a FREE-TEXT string field. Bounds what an arbitrary caller can push
#: into an approval record and into the notification rendered from it, and
#: truncation is the right answer there: a comment or a summary clipped at 500
#: characters is still the same fact, just shorter.
_MAX_FREE_TEXT_LENGTH = 500

#: Keys exempt from the cap ENTIRELY. A Step Functions task token is ~600-700
#: characters and every one of them is load-bearing — a clipped token cannot be
#: sent back, so the gate it belongs to stalls for its full ``TimeoutSeconds``.
_UNTRUNCATED_FIELDS = frozenset({"task_token"})

#: Keys whose value is an IDENTIFIER and must round-trip exactly. These are
#: capped at ``_MAX_FREE_TEXT_LENGTH`` like free text, but an over-length value
#: RAISES instead of being clipped.
#:
#: WHY THE CAP STAYS AND ONLY THE FAILURE MODE CHANGES. Truncating free text
#: yields a shorter version of the same fact. Truncating an id yields a
#: DIFFERENT id — one that is well-formed, plausible-looking, and resolves to
#: nothing. ``mapping_id`` is the case that made this concrete (p54 R1.1): it
#: arrives on the reuse arm's event, is handed to ``_reused_mapping_payload``
#: via ``get_approval_data``, and a clipped value misses in the mappings table,
#: so the reuse arm renders an "unreadable" approval payload with no indication
#: that a lossy transform upstream is why. ``pipeline_id`` and ``run_id`` are in
#: the set for the same reason and a worse consequence: they are the approval
#: record's own key components, so a clipped one files the approval against a
#: run that does not exist.
#:
#: Raising here is NOT a new error contract. ``LGR-86698d4-2`` (template /
#: ratified) draws the raise-vs-return line by ORIGIN, not by exception type:
#: ``validate_approval_request`` sits deliberately OUTSIDE ``lambda_handler``'s
#: inner ``try`` so its ``ValueError`` stays a 400 for a MALFORMED EVENT (R3.5),
#: while anything escaping ``create_approval_request`` fails the invocation. An
#: id longer than 500 characters is a malformed event — no producer emits one;
#: ``schema_compare`` writes a UUID-shaped ``mapping_id`` — so it joins the two
#: cases already on this path (missing required field, invalid approval type)
#: rather than opening a third.
#:
#: Not reachable with today's id shapes. This is a latent-correctness fence: an
#: id that cannot round-trip must never be silently substituted for a valid one.
_EXACT_IDENTITY_FIELDS = frozenset({"pipeline_id", "run_id", "mapping_id"})


def validate_approval_request(event: dict[str, Any]) -> dict[str, Any]:
    """Validate and sanitize approval request data.

    Gates all four approval types, so the sanitization rules below apply to
    every create-mode event the state machine sends.
    """
    required_fields = ["approval_type", "pipeline_id", "run_id"]

    # Check required fields
    for field in required_fields:
        if field not in event:
            raise ValueError(f"Missing required field: {field}")

    # Validate approval type. "test_failure" is the forced test-review gate
    # (P28 R4.2, ForceTestReviewApproval state): created when the TestCode
    # step reports a failed structured result.
    valid_types = ["schema_mapping", "code_generation", "first_run", "test_failure"]
    if event["approval_type"] not in valid_types:
        raise ValueError(f"Invalid approval type: {event['approval_type']}. Must be one of {valid_types}")

    # Sanitize string fields. Three rules, one per set above; see the comments
    # on the constants for why an id is never clipped.
    sanitized_event = {}
    for key, value in event.items():
        if isinstance(value, str):
            stripped = value.strip()
            if key in _UNTRUNCATED_FIELDS:
                sanitized_event[key] = stripped
            elif key in _EXACT_IDENTITY_FIELDS:
                if len(stripped) > _MAX_FREE_TEXT_LENGTH:
                    raise ValueError(
                        f"Field {key!r} is {len(stripped)} characters, over the "
                        f"{_MAX_FREE_TEXT_LENGTH}-character limit. It is an identifier, so it is "
                        f"REFUSED rather than truncated: a clipped id is a different id that "
                        f"resolves to nothing, and substituting one would make the approval "
                        f"unresolvable without saying why."
                    )
                sanitized_event[key] = stripped
            else:
                sanitized_event[key] = stripped[:_MAX_FREE_TEXT_LENGTH]
        else:
            sanitized_event[key] = value

    return sanitized_event


def get_pipeline_config(pipeline_id: str, run_id: str) -> dict[str, Any]:
    """Get pipeline configuration including approval settings"""
    try:
        # Query state table for pipeline configuration — base table query by run_id
        response = state_table.query(
            KeyConditionExpression="run_id = :run_id", ExpressionAttributeValues={":run_id": run_id}
        )

        # Find the first step which should contain pipeline config
        for item in response.get("Items", []):
            if "pipeline_config" in item:
                return json.loads(item["pipeline_config"])

        # Default configuration if not found
        #
        # ── p47 R6 — RATIFIED ARM B: `timeout_behavior` ADVERTISES ONLY "manual" ──
        # This dict used to carry `# manual, auto_reject, auto_approve` here, and
        # that trailing comment was the config surface: it is what told a reader
        # the other two values were options they could set. They are not, and they
        # are unreachable TWICE OVER — the second reason is not in `LGR-69ca78b-2`
        # and was found at p47 C1:
        #
        #   1. NO SWEEP CAN ARRIVE IN TIME. `LGR-69ca78b-2`'s own revisit_when,
        #      verbatim: "Both are currently unreachable — Step Functions times
        #      out at exactly 72h while the sweeper only runs every 15 minutes, so
        #      it always arrives to find a consumed token. Making them reachable
        #      requires the advertised window strictly BELOW the gate, which
        #      reverses the equality recorded here."
        #   2. NOTHING CAN SET THE VALUE. `timeout_behavior` is read from THIS
        #      function's result — the `approvals` sub-map of a `pipeline_config`
        #      JSON string on a STATE-TABLE row (:164) — and consumed at :411.
        #      Every config surface that writes approval settings writes an
        #      `approval_config` MAP on the PIPELINES table instead
        #      (approval_config/lambda_function.py:85, pipelines_crud.py:903 and
        #      :1140-1159). Different table, different attribute, different
        #      nesting: the two never meet. The state-row `pipeline_config`
        #      attribute has NO PRODUCER AT ALL — nothing writes it except :431
        #      echoing back what this function just returned — so the branch below
        #      always returns this literal and the value is "manual" in every
        #      deployment. The same producerless store is why the per-pipeline
        #      `timeout_hours` override is unreachable, which an existing test
        #      already records: tests/unit/lambdas/approval_handler/
        #      test_serialization_preservation_prop.py:133.
        #
        # DO NOT re-add the option list without also fixing (2), and do not "fix"
        # (2) by pointing this read at the pipelines table — that would make an
        # advertised-but-unreachable setting silently live on existing pipeline
        # records, which is a governance change, not a plumbing fix. Making the
        # feature real needs a producer AND the window/gate change (1) describes,
        # and that change reverses a ratified decision.
        #
        # THE SWEEPER'S ENFORCEMENT CODE WAS DELIBERATELY LEFT IN PLACE — see the
        # comment at approval_timeout_handler/lambda_function.py's dispatch for why
        # un-advertising and deleting are different acts.
        return {
            "approvals": {
                "schema_mapping_enabled": True,
                "code_generation_enabled": True,
                "first_run_enabled": True,
                "timeout_behavior": "manual",
                "timeout_hours": DEFAULT_TIMEOUT_HOURS,
                "notification_channels": ["sns"],
            }
        }

    except Exception as e:
        logger.warning(f"Could not retrieve pipeline config for {pipeline_id}: {e}")
        # Return default config — "manual" is the only advertised
        # `timeout_behavior`; see the p47 R6 note on the success path above
        # before adding any other value here.
        return {
            "approvals": {
                "schema_mapping_enabled": True,
                "code_generation_enabled": True,
                "first_run_enabled": True,
                "timeout_behavior": "manual",
                "timeout_hours": DEFAULT_TIMEOUT_HOURS,
                "notification_channels": ["sns"],
            }
        }


def _schema_mapping_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Project a ``(run_id, "schema_mapping")`` state row into the approval payload.

    ONE DERIVATION, TWO READERS (p54 R1.1). This is the eleven-key projection p50
    C1 established at ten and p55 R10.1 extended by one (``rename_manifest``, see
    the comment at that key), lifted out of ``get_approval_data``'s branch body so the reuse arm
    can source it from a DIFFERENT row without spelling the shape a second time.
    ``LGR-28b4d75-1``, ``LGR-28b4d75-5`` and ``LGR-9e76249-2`` (all template /
    ratified) all reject a second derivation of one shape in a second place, and a
    duplicated ten-key literal in the reuse arm would have been exactly that.

    **Nothing about the addresses changed** — every key reads from the same place
    p50 C1 pointed it at, and ``test_schema_mapping_payload_read.py``'s exact
    ``list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS)`` assertion is the proof
    that the key set and its ORDER survived the move. The order is part of the
    serialized bytes.

    ``item`` is the row itself, so the caller decides WHICH row is projected: the
    run's own on the normal path, the mapping's producing run on the reuse path.
    """
    # p50 R1.2 (C1): read the path the PRODUCER actually writes.
    #
    # Seven of these nine keys used to be read off this row's TOP level,
    # and their producer writes none of them there: source_schema /
    # target_schema / proposed_mappings / confidence_scores /
    # unmapped_fields / mapping_summary / generated_by are built by
    # create_approval_payload (schema_mapping/lambda_function.py:
    # 1732-1780) and stored whole at output_data.approval (:1927).
    # Measured live on run run-2026-08-24-13-48-26-4fccdf03
    # (the validation account): approval_data was 237 characters with eight of the
    # nine keys literal null, so a human approved a mapping they had
    # been shown nothing about — the same defect, and the same
    # consequence, as the code_generation branch before p34 R3.6
    # fixed it. LGR-86698d4-1 (template / ratified) predicted this twin
    # by name and named this path.
    #
    # evidence and questions are NOT in that payload and are read from
    # the row's TOP level ON PURPOSE — that IS their correct address.
    # Their writer is a different one: the complex-mapping agent's
    # update_item at schema_mapping/lambda_function.py:2277-2291, which
    # SETs them at the top level. They were null on the observed run
    # because that agent did not run, not because the read is wrong.
    # Moving them under output_data.approval would break a working read.
    #
    # generated_by resolves in BOTH places (the producer also writes it
    # at the top level, :1946), so it is read from the payload first and
    # the row second. Same two-source shape as code_version_id, and for
    # the same reason: both sources are the same writer's own.
    mapping_output = item.get("output_data")
    if not isinstance(mapping_output, dict):
        mapping_output = {}
    mapping_approval = mapping_output.get("approval")
    if not isinstance(mapping_approval, dict):
        mapping_approval = {}

    proposed_mappings = mapping_approval.get("proposed_mappings")

    return {
        "source_schema": mapping_approval.get("source_schema"),
        "target_schema": mapping_approval.get("target_schema"),
        "proposed_mappings": proposed_mappings,
        "confidence_scores": mapping_approval.get("confidence_scores"),
        "unmapped_fields": mapping_approval.get("unmapped_fields"),
        "mapping_summary": mapping_approval.get("mapping_summary"),
        # Top level is the correct address for these two — see above.
        "evidence": item.get("evidence"),
        "questions": item.get("questions"),
        # P28 R3.5: provenance so approvers can see when they are
        # reviewing a fallback (substring-match) mapping.
        "generated_by": mapping_approval.get("generated_by") or item.get("generated_by"),
        # p50 R1.4/R1.5 (C2): DERIVED from the list above, THREE-STATE,
        # and deliberately NOT read from the row's own top-level
        # `mapping_count` attribute — which this row does carry
        # (schema_mapping/lambda_function.py:1938 writes it as
        # len(mappings), beside the payload).
        #
        # WHY DERIVE WHEN A STORED VALUE IS RIGHT THERE. The two are
        # equal at write time, so this is not about correcting a wrong
        # number — it is about which number DESCRIBES WHAT THE APPROVER
        # IS SHOWN. `proposed_mappings` is what the Mapping Preview
        # renders, so a count taken from it cannot disagree with it,
        # whereas a count taken from the row's top level is a second
        # authority that can: it survives an `output_data` this branch
        # cannot read, and would then put a confident "Mapping count: 4"
        # next to an empty preview — the exact class of quiet wrongness
        # p50 exists to remove. A fixture in this repo already holds the
        # two disagreeing (test_serialization_preservation_prop.py's row:
        # top-level 3, one proposed mapping), so the divergence is
        # demonstrable rather than hypothetical. `LGR-28b4d75-1` and
        # `LGR-9e76249-2` (both template / ratified) reject a second
        # authority for a derivable number on exactly this ground —
        # derive on the read path, never persist. do-NOT 8 forbids a NEW
        # stored field; this goes further and declines to consume the
        # existing one.
        #
        # THREE-STATE, AND `isinstance` IS LOAD-BEARING. Per
        # `LGR-4854b3d-1` (template / ratified) for `field_count`: a
        # positive number, `0`, or None — "every hand-written `?? 0`
        # collapses null into 'declares nothing'". None means NOT
        # ESTABLISHED and must never be reported as `0` (do-NOT 7): zero
        # mappings and an unreadable payload are different facts (R1.5).
        # The `isinstance(..., list)` test is not defensive noise —
        # `len()` accepts a str and a dict, so a bare len() would turn a
        # malformed `proposed_mappings` into a character or key count and
        # present it as a mapping count. `LGR-9a35b2d-2` (template /
        # ratified) measured that failure's cost on the sibling listing
        # surface: `mapping_count: 0` reported for a pipeline whose
        # schema_mapping row carried 7.
        #
        # A plain int, so the serialization boundary before json.dumps
        # leaves it alone and it serializes as 4 rather than 4.0 — reading
        # the stored Decimal("4") would have inherited `LGR-86698d4-1`'s
        # accepted int→float widening for no reason.
        #
        # p54 R1.2 holds BY CONSTRUCTION on the reuse path because this is
        # the same expression over the same list: whichever row is
        # projected, the count describes the mappings the payload carries.
        "mapping_count": len(proposed_mappings) if isinstance(proposed_mappings, list) else None,
        # p55 R10.1/R10.2 — the ELEVENTH key, appended so no existing position
        # moves (the order is part of the serialized bytes).
        #
        # WHY THIS PROJECTION HAD TO GROW, stated because task 6.2's own brief
        # says the manifest "reaches the client through the same
        # `output_data["approval"]` path `store_mapping_result` already writes".
        # That is where it is STORED; it is not how it reaches a client. This
        # function is an explicit key projection, and `get_run_details`
        # (`dashboard_api/handlers/pipelines_runs.py:2350-2357`) hands the
        # frontend `json.loads(approval_data)` verbatim — so a key the producer
        # writes and this projection omits is a key no dashboard surface can ever
        # render. Without this line R10.1 would be unsatisfiable no matter what
        # the frontend does.
        #
        # `.get`, so the key is present with a `None` value rather than absent,
        # on all three of the rows that carry no manifest: a pre-p55 mapping row,
        # a row whose `create_approval_payload` took its degraded `except` arm,
        # and an `output_data`/`output_data.approval` this function coerced to
        # `{}`. That is what keeps `list(payload) == SCHEMA_MAPPING_APPROVAL_KEYS`
        # exactly assertable on every row — which is the whole reason
        # `LGR-e5e9edd-6` (template / ratified) refused to grow this tuple for the
        # p54 reuse keys: those appear on ONE path, so growing the base tuple
        # could only have been repaired by loosening two exact-equality
        # assertions to subset checks, and a do-NOT forbids that loosening by
        # name. This key appears on EVERY path, so the repair is to state the
        # eleventh member — the tuple is extended, and not one assertion on it is
        # weakened. Same reasoning, opposite verdict, for a materially different
        # key.
        #
        # NOT derived here, unlike `mapping_count` above. The stored manifest is
        # the authority at code-generation time and is used verbatim or not at all
        # (design risk R5.7): a manifest recomputed on the read path could differ
        # from the one a human approved while both remained "valid" by
        # Schema_Hash, which is exactly the silent divergence R5.7 names.
        "rename_manifest": mapping_approval.get("rename_manifest"),
    }


def _reused_mapping_payload(mapping_id: str) -> dict[str, Any]:
    """Resolve a Reuse_Run's approval payload by chasing the mapping to its origin run.

    p54 R1.1 / R1.4 — ROUTE A′, THREE READS, ALL READS. NOTHING HERE WRITES.
    ------------------------------------------------------------------------
    A Reuse_Run takes the `ReuseExistingMapping` Pass instead of the schema_mapping
    Lambda. A Pass writes nothing to DynamoDB, so there is no
    `(run_id, "schema_mapping")` row for this run and the caller's `if "Item" in
    response:` guard is skipped — which is why `approval_data` was `{}` and the
    approver was shown nothing.

    1. `mappings_table.get_item(Key={"mapping_id": ...})` — the shape
       `dashboard_api/handlers/approvals.py` already uses for the memory-feedback
       path, borrowed rather than coined.
    2. `run_id` off that item — `schema_mapping/lambda_function.py:1858-1859`
       writes it, CONDITIONALLY (`if run_id:`), so its absence is a real state and
       not a theoretical one.
    3. the ORIGIN run's `(run_id, "schema_mapping")` row, projected by
       `_schema_mapping_payload` — the producer's own bytes, built by the one
       `create_approval_payload` call. So the payload the approver sees on a
       Reuse_Run is what the approver saw when the mapping was first proposed.

    WHY NOT READ THE MAPPINGS ITEM ITSELF. It carries `mapping_result` but **no
    `source_schema` and no `target_schema`** (`:1836-1859`) — the two schemas the
    approver is being asked to judge live only inside the approval payload. Plain
    route A therefore cannot satisfy R1.1.

    WHY NOT REBUILD THE PAYLOAD. `create_approval_payload` is pure and would port,
    but it lives in the `schema_mapping` Lambda package and is unreachable from
    this one, so "rebuild" means a second derivation of one shape in a second
    place — the defect `LGR-28b4d75-1`, `LGR-28b4d75-5` and `LGR-9e76249-2` all
    reject.

    WHY NOT ROUTE B (have the Pass write a real state row). Rejected on
    eligibility: a Pass-written row at `status: "success"` becomes the newest
    matching reuse candidate the moment it is written — while the run is still
    parked at the human gate — and `LGR-5d68eeb-2` narrowed
    `schema_compare`'s candidate query to `success` for exactly the purpose of
    keeping a not-yet-approved row out of that set. p54 do-NOT 9 / R8.9.

    THE UNREADABLE STATE IS A STATEMENT, NOT AN EMPTY PAYLOAD (do-NOT 7).
    -------------------------------------------------------------------
    An unresolvable reused mapping is a *named reference to something absent* —
    `LGR-b2f79ef-14` classes that as an inconsistent record rather than a thing
    that never existed — so it must never render as a mapping with no fields.
    `proposed_mappings` is `None`, so `mapping_count` stays `None`: never `0`,
    never `[]`, never `{}`. `{}` is reserved for a genuinely different fact (no
    row AND no `mapping_id`), which is the *no reuse* state and is not p54's to
    change. `LGR-b2f79ef-8` is the same rule one Lambda over: "no pipeline record"
    and "pipeline has no target schema" were made to stop collapsing into one
    value, because a run "reported as unverifiable when the real problem is a
    missing record" is the prohibited outcome.

    A READ THAT RAISED IS ALSO UNREADABLE, AND THAT IS DELIBERATE.
    ------------------------------------------------------------
    design's Error Handling enumerates five SHAPE failures. A sixth condition is
    handled here — a read that raised — because `get_approval_data`'s own outer
    `except Exception: return {}` would otherwise convert a transient DynamoDB
    failure into the *no reuse* payload, i.e. into the false claim that this run
    did not reuse a mapping. That is precisely the collapse do-NOT 7 forbids, so
    the broad catch here is what prevents it rather than an instance of it. The
    catch is broad on purpose: a narrow one would let a `TypeError` off a
    malformed item escape to that same outer handler and produce `{}`.

    `LGR-86698d4-3` (**PROPOSED, not ratified**) — *a token-carrying step must
    never report failure by returning* — is honoured, not declined. Its subject is
    the step's own work: creating the approval request and handing out the task
    token. That is untouched, and `LGR-86698d4-2`'s by-origin rule still makes any
    exception escaping `create_approval_request` fail the invocation. What is
    caught here is one enrichment read, and it is caught into a payload that
    STATES the failure to the approver — the opposite of reporting failure by
    returning silence.
    """
    unreadable: dict[str, Any] = {
        "reused_mapping": True,
        "reused_mapping_id": mapping_id,
        "mapping_unreadable": True,
        # do-NOT 7: None, so the count below stays None. Never [] and never 0.
        "proposed_mappings": None,
        # Present-and-None rather than absent, so R1.2's expression is the same
        # one on both states and the display reader's null branch (an em dash,
        # `utils/approval-mapping-count.ts`) is reached by a value it declares.
        "mapping_count": None,
    }

    try:
        mapping_response = mappings_table.get_item(Key={"mapping_id": mapping_id})
        mapping_item = mapping_response.get("Item")
        if not isinstance(mapping_item, dict):
            logger.warning(f"Reused mapping {mapping_id} has no mappings-table item — payload marked unreadable")
            return unreadable

        origin_run_id = mapping_item.get("run_id")
        if not isinstance(origin_run_id, str) or not origin_run_id:
            logger.warning(f"Reused mapping {mapping_id} carries no run_id — payload marked unreadable")
            return unreadable

        origin_response = state_table.get_item(Key={"run_id": origin_run_id, "step_name": "schema_mapping"})
        origin_item = origin_response.get("Item")
        if not isinstance(origin_item, dict):
            logger.warning(
                f"Reused mapping {mapping_id} names origin run {origin_run_id}, which has no "
                f"schema_mapping row — payload marked unreadable"
            )
            return unreadable

        payload = _schema_mapping_payload(origin_item)
    except Exception as e:
        # See the docstring: this catch exists so a raised read cannot fall to
        # get_approval_data's `return {}` and be reported as *no reuse*.
        logger.warning(f"Could not resolve reused mapping {mapping_id}: {e} — payload marked unreadable")
        return unreadable

    # `output_data` or `output_data.approval` not being a dict is the fourth and
    # fifth resolution failure. `_schema_mapping_payload` coerces both to `{}`, so
    # it returns an eleven-key payload of nulls — which is a mapping with no fields,
    # the one rendering do-NOT 7 forbids for this state. Detect it by the key the
    # approver is actually shown and fall to the unreadable statement instead.
    if payload.get("proposed_mappings") is None:
        logger.warning(
            f"Reused mapping {mapping_id} resolved to origin run {origin_run_id} but its approval "
            f"payload carries no proposed_mappings — payload marked unreadable"
        )
        return unreadable

    # THREE KEYS APPEND (do-NOT 6). They go after the existing eleven so no
    # existing position moves — the order is part of the serialized bytes, and
    # `SCHEMA_MAPPING_APPROVAL_KEYS_REUSED` in tests/fixtures/conftest.py pins it.
    payload["reused_mapping"] = True
    payload["reused_mapping_id"] = mapping_id
    payload["reused_from_run_id"] = origin_run_id
    return payload


def get_approval_data(approval_type: str, run_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retrieve relevant data for the approval based on type.

    ``event`` is the raw approval-request payload from Step Functions; the
    test_failure gate carries its report inline there (P28 R4.2), and a Reuse_Run's
    ``mapping_id`` arrives on it (p54 R1.1).
    """
    try:
        approval_data = {}

        if approval_type == "schema_mapping":
            # The row and its ten-key projection: see `_schema_mapping_payload`,
            # which holds p50 C1's addresses and the reason for each of them.
            response = state_table.get_item(Key={"run_id": run_id, "step_name": "schema_mapping"})

            if "Item" in response:
                approval_data = _schema_mapping_payload(response["Item"])
            else:
                # p54 R1.1 — A REUSE_RUN HAS NO ROW HERE, AND THAT IS THE DEFECT.
                #
                # `ReuseExistingMapping` is a `sfn.Pass` and a Pass writes nothing
                # to DynamoDB, so the guard above is skipped and `approval_data`
                # stayed `{}` — the gate fired and the approver was shown nothing.
                # `_reused_mapping_payload` resolves it by chasing the mapping to
                # the run that produced it (route A′; three reads, no writes).
                #
                # TRUTHINESS, NOT `in`. A null or absent id is the *no reuse*
                # state, which keeps `{}`; it is NOT an id to resolve.
                #
                # THE CHECK IS DEFENSIVE, AND ITS OLD JUSTIFICATION WAS WRONG.
                # This comment used to claim the event can "legitimately carry
                # `mapping_id: null`", reasoning that `CheckSchemaMappingApproval`'s
                # reuse conjuncts are both satisfiable by a null. That reads only
                # two of the three conjuncts. The router is
                # `orchestration-stack.ts:929-933`, an `and` of
                # `stringEquals($.schema_compare_result.action,
                # 'use_existing_pipeline')`, `isPresent(...existing_mapping_id)` and
                # `not(stringEquals(...existing_mapping_id, ''))` — and the FIRST
                # conjunct is the one that decides it. `action` is set to
                # `use_existing_pipeline` in exactly one place, the identical-schema
                # branch at `schema_compare/lambda_function.py:686`, and that same
                # branch flips it back to `create_new_pipeline` whenever
                # `existing_mapping_id` is falsy (`:775-780`, the
                # `if not existing_mapping_id:` guard `LGR-5d68eeb-2` added). So a
                # null id routes to `SchemaMapping`, which writes the state row the
                # guard above finds, and this arm is never entered.
                #
                # The reuse arm is therefore UNREACHABLE with an empty id, and this
                # check has no reachable null to absorb. It STAYS: it is one
                # comparison, and the alternative is passing a null into
                # `_reused_mapping_payload` if that routing ever changes. Only the
                # stated reason is corrected — do not read this as licence to
                # delete the guard.
                mapping_id = (event or {}).get("mapping_id")
                if mapping_id:
                    approval_data = _reused_mapping_payload(str(mapping_id))

        elif approval_type == "code_generation":
            # p34 R3.6: read the paths the PRODUCERS actually write.
            #
            # This branch used to read seven keys off the two state rows' TOP
            # level, and no producer writes any of them there:
            # generated_code_s3_uri / template_used / code_quality_metrics /
            # strands_agent_reasoning were read off the create_code row, which
            # stores its whole approval payload at output_data.approval
            # (create_code/lambda_function.py:839, built by
            # create_approval_payload at :259-301); test_results /
            # test_success_rate / performance_metrics were read off the
            # test_code row, which writes the structured result as
            # test_result — singular — at test_code/lambda_function.py:1722.
            # The observed consequence was an approval record with seven keys
            # and seven nulls, approved by a human who had been shown nothing
            # about the code.
            #
            # code_version_id is the load-bearing addition: ApprovalArtifact's
            # code_generation branch renders the real CodeViewer only when it
            # can read that field (ApprovalArtifact.tsx:443-451, via
            # detailStr at :52-60).
            code_response = state_table.get_item(Key={"run_id": run_id, "step_name": StepName.CREATE_CODE})
            code_output = code_response.get("Item", {}).get("output_data")
            if not isinstance(code_output, dict):
                code_output = {}
            code_approval = code_output.get("approval")
            if not isinstance(code_approval, dict):
                code_approval = {}
            template_selection = code_approval.get("template_selection")
            if not isinstance(template_selection, dict):
                template_selection = {}

            approval_data = {
                # Both sources are the same writer's own: create_code sets
                # output_data.code_version_id (:833) alongside the payload.
                "code_version_id": code_approval.get("code_version_id") or code_output.get("code_version_id"),
                "generated_code_s3_uri": code_approval.get("generated_code_s3_uri"),
                "generated_code_preview": code_approval.get("generated_code_preview"),
                "code_quality_metrics": code_approval.get("code_quality_metrics"),
                # P28 R11.2: the DQDL ruleset baked into the generated job.
                "dqdl_rules": code_approval.get("dqdl_rules"),
                "strands_agent_reasoning": code_approval.get("strands_agent_reasoning"),
                "template_used": template_selection.get("template_used"),
            }

            # The structured test result, on the same ??-chain the test_failure
            # branch uses below: the inline SFN payload first, then the
            # test_code row's own attribute. WaitForCodeApproval passes the
            # same key inline (orchestration-stack.ts:639), so the state read
            # is the fallback, not the primary source.
            test_result = (event or {}).get("test_result")
            if not isinstance(test_result, dict):
                test_response = state_table.get_item(Key={"run_id": run_id, "step_name": StepName.TEST_CODE})
                test_result = test_response.get("Item", {}).get("test_result")

            if isinstance(test_result, dict):
                approval_data.update(
                    {
                        "test_result": test_result,
                        "test_status": test_result.get("status"),
                        "report_s3_uri": test_result.get("report_s3_uri"),
                    }
                )
            else:
                approval_data.update({"test_result": None, "test_status": "unknown"})

        elif approval_type == "test_failure":
            # Forced test-review gate (P28 R4.2): the approval record carries
            # the full structured test report so the dashboard can render it.
            # Primary source is the SFN payload itself — ForceTestReviewApproval
            # passes test_result ({status, checks, report_s3_uri}) inline.
            test_result = (event or {}).get("test_result")
            if not isinstance(test_result, dict):
                # Fallback: the state row test_code writes under its
                # canonical step name (P28 R8.2, task 14.2).
                response = state_table.get_item(Key={"run_id": run_id, "step_name": StepName.TEST_CODE})
                test_result = response.get("Item", {}).get("test_result")

            if isinstance(test_result, dict):
                approval_data = {
                    "test_result": test_result,
                    "test_status": test_result.get("status"),
                    "failed_checks": [
                        c.get("name")
                        for c in test_result.get("checks", [])
                        if isinstance(c, dict) and not c.get("passed", False)
                    ],
                    "report_s3_uri": test_result.get("report_s3_uri"),
                }
            else:
                approval_data = {"test_result": None, "test_status": "unknown"}

            # Regeneration is p16 retry-from-step (resume from create_code),
            # not a state-machine loop — surface the hint to the dashboard.
            approval_data["remediation_hint"] = (event or {}).get("remediation_hint", "retry-from-step:create_code")

        elif approval_type == "first_run":
            # Get infrastructure and quality check data — direct base table lookups

            # Get infrastructure data
            infra_response = state_table.get_item(Key={"run_id": run_id, "step_name": "create_infrastructure"})

            # Get quality check data
            quality_response = state_table.get_item(Key={"run_id": run_id, "step_name": "create_quality_checks"})

            approval_data = {}
            if "Item" in infra_response:
                # ALL THREE OF THESE ARE WRONG-LEVEL READS TOO, AND THEY ARE NOT
                # IN p50 C5's SCOPE. Recorded, not fixed — the same way the note
                # C5 replaced recorded the two keys below before anyone fixed
                # those. R4.1 and design C5 name exactly two keys, both on the
                # create_quality_checks row, so correcting these here would be
                # scope this spec did not size or test for. Found while verifying
                # the writer for the block below, against
                # create_infrastructure/lambda_function.py:
                #
                # * infrastructure_status IS written, but at
                #   output_data.infrastructure_status (:958, via
                #   standardized_output -> "output_data" at :972) and as a DICT.
                #   Nothing writes it at the row's top level, so this read is
                #   null today. Note the consumer expects a string
                #   (ApprovalsPage.tsx:100 casts it `as string`), so correcting
                #   the address alone would put a dict where a string renders —
                #   which is why this needs its own decision rather than a
                #   one-line move. The same writer also puts it inside its own
                #   approval payload at output_data.approval.
                #   infrastructure_status (:643).
                # * cloudformation_stack_id occurs NOWHERE in that Lambda. There
                #   is no address to point at: this pipeline provisions S3 Table
                #   Buckets directly, not via CloudFormation, so the key looks
                #   like a leftover from an earlier provisioning model. It should
                #   probably be deleted rather than re-pointed.
                # * glue_job_name is likewise never written by this step. The Glue
                #   job is created by create_glue_pipeline, a DIFFERENT step and a
                #   different state row, so sourcing it means a third get_item —
                #   not a re-address.
                #
                # So the three are three different problems (wrong level / no
                # producer / wrong row) and only one is a move. Left for a
                # follow-up that can size them separately.
                infra_item = infra_response["Item"]
                approval_data.update(
                    {
                        "infrastructure_status": infra_item.get("infrastructure_status"),
                        "cloudformation_stack_id": infra_item.get("cloudformation_stack_id"),
                        "glue_job_name": infra_item.get("glue_job_name"),
                    }
                )

            if "Item" in quality_response:
                # p50 R4.1 (C5): read the paths the PRODUCER actually writes.
                #
                # quality_checks and data_quality_rules used to be read off this
                # row's TOP level, and create_quality_checks writes NEITHER key
                # there — nor anywhere under that spelling. The in-code note this
                # replaces said as much and called it "a pre-existing gap left
                # alone here"; C5 closes it. The third wrong-level read in this
                # branch, the create_infrastructure one above, is NOT in scope —
                # see the note there.
                #
                # Where the two facts actually live, verified against the writer
                # rather than inferred:
                #
                # * The step builds a first_run approval payload in
                #   combine_infrastructure_and_quality_approval_data
                #   (create_quality_checks/lambda_function.py:266, payload dict
                #   at :287-322) and stores it whole at
                #   output_data["approval"] (:433) — the same shape
                #   create_code and schema_mapping use, so this is the third
                #   branch reading a producer's own approval payload rather than
                #   a new mechanism.
                # * quality_checks is that payload's pre_run_validation.
                #   quality_checks (:307-312) — the writer's ONLY key of that
                #   name. It is a list of human-readable lines, which is what the
                #   consumer wants: ApprovalsPage.tsx:100-107 renders it under a
                #   "Quality checks" label beside "Infrastructure status".
                # * data_quality_rules is the generated rule list, at that
                #   payload's quality_check_configuration.quality_rules. It also
                #   resolves at output_data.quality_configuration.quality_rules
                #   (:420) and is read payload-first, row-second — the two are the
                #   SAME object at write time (quality_config is passed into the
                #   combiner by reference), so this is the two-source shape
                #   code_version_id uses at :381 and generated_by at :299, for the
                #   same stated reason: both sources are the same writer's own.
                #
                # KNOWN WEAKNESS IN THE PRODUCER, NOT FIXED HERE. Only the first
                # of pre_run_validation.quality_checks' four lines is derived from
                # anything ("N quality rules configured"); the other three are
                # unconditional string literals, so a run with zero rules still
                # tells its approver "Null value checks configured". That is a
                # producer-side defect and correcting it means changing what the
                # writer emits, which is outside a read correction — recorded here
                # so the next reader does not have to rediscover it, and left for
                # its own decision. Reading the producer's own key is still
                # strictly better than reading an address nothing writes: today
                # the approver is shown NOTHING, including the rule count.
                #
                # The isinstance guards are the code_generation branch's
                # template_selection shape at :374-376 — a nested sub-dict gets
                # the same treatment as output_data itself, so a malformed or
                # absent level degrades to absent keys instead of raising
                # (R1.3's requirement, applied to this branch).
                #
                # DECIMAL IS NOW REACHABLE ON THIS BRANCH TOO. The rule list
                # carries numerics — min_rows (:192), and min_value / max_value
                # off the target schema's minimum / maximum (:178-179) — and the
                # whole item goes through convert_floats_to_decimal on write
                # (:407), so boto3 hands them back as Decimal. They are converted
                # once at the serialization boundary (:659, LGR-86698d4-1,
                # template / ratified), which is the same condition R1.6 flagged
                # for schema_mapping: a read correction is what first makes the
                # conversion reachable.
                quality_item = quality_response["Item"]
                quality_output = quality_item.get("output_data")
                if not isinstance(quality_output, dict):
                    quality_output = {}
                quality_approval = quality_output.get("approval")
                if not isinstance(quality_approval, dict):
                    quality_approval = {}
                pre_run_validation = quality_approval.get("pre_run_validation")
                if not isinstance(pre_run_validation, dict):
                    pre_run_validation = {}
                approval_configuration = quality_approval.get("quality_check_configuration")
                if not isinstance(approval_configuration, dict):
                    approval_configuration = {}
                row_configuration = quality_output.get("quality_configuration")
                if not isinstance(row_configuration, dict):
                    row_configuration = {}
                approval_data.update(
                    {
                        "quality_checks": pre_run_validation.get("quality_checks"),
                        "data_quality_rules": (
                            approval_configuration.get("quality_rules") or row_configuration.get("quality_rules")
                        ),
                        # p30 R7.2: the Quality_Verdict mode for this run's
                        # generated code — whether anything withheld failing
                        # rows from the write, or the rule count above gates
                        # nothing. Read from output_data because that is where
                        # create_quality_checks writes it (:432). This read was
                        # already correct before C5 and is deliberately unchanged
                        # (p50 R4.3 / do-NOT 5) — it is the one key in this branch
                        # that never needed moving, and it is why the branch was
                        # only PARTLY wrong rather than wholly.
                        "quality_verdict": quality_output.get("quality_verdict"),
                    }
                )

        return approval_data

    except Exception as e:
        logger.error(f"Error retrieving approval data for {approval_type}: {e}")
        return {}


def store_task_token(approval_id: str, task_token: str):
    """Store task token in dedicated table with a TTL covering the approval window."""
    ttl = int(time.time()) + TASK_TOKEN_TTL_SECONDS  # outlives the approval wait window
    task_tokens_table.put_item(
        Item={
            "approval_id": approval_id,
            "task_token": task_token,
            "ttl": ttl,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    logger.info(f"Stored task token for approval {approval_id} (TTL: {TASK_TOKEN_TTL_SECONDS}s)")


@time_function("ApprovalHandler.CreateRequest")
def create_approval_request(event: dict[str, Any]) -> str:
    """Create and store approval request in DynamoDB"""
    try:
        # Generate unique approval ID
        timestamp = int(time.time())
        approval_id = f"{event['pipeline_id']}_{event['approval_type']}_{timestamp}"

        # Get pipeline configuration
        pipeline_config = get_pipeline_config(event["pipeline_id"], event["run_id"])
        approval_config = pipeline_config.get("approvals", {})

        # Calculate timeout
        timeout_hours = approval_config.get("timeout_hours", DEFAULT_TIMEOUT_HOURS)
        timeout_at = datetime.now(UTC) + timedelta(hours=timeout_hours)

        # Get approval-specific data (the event carries inline payloads for
        # some types, e.g. test_failure's structured test report)
        approval_data = get_approval_data(event["approval_type"], event["run_id"], event)

        # Create approval record
        approval_record = {
            "approval_id": approval_id,
            "pipeline_id": event["pipeline_id"],
            "run_id": event["run_id"],
            "approval_type": event["approval_type"],
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
            "timeout_at": timeout_at.isoformat(),
            "timeout_behavior": approval_config.get("timeout_behavior", "manual"),
            "has_task_token": bool(event.get("task_token")),
            "execution_arn": event.get("execution_arn"),
            # p34a R2.1/R2.4: convert Decimal at this serialization boundary, once,
            # rather than in each get_approval_data branch. boto3's resource
            # deserializer returns every DynamoDB `N` attribute as Decimal and
            # json.dumps has no encoder for it, so any numeric value a branch
            # surfaces from a state row arrives here as Decimal. A per-branch patch
            # would be branch-count-dependent and silently wrong the next time a
            # branch is added or a read is corrected — the schema_mapping branch is
            # one correct read away from crashing the same way. Uses the shared
            # converter (P28 R19.2 consolidated six local copies into
            # shared/decimal_utils); do not add a seventh, nor a `default=` here.
            # int→float widening is accepted: Decimal("335") serializes as 335.0,
            # and no consumer reads code_quality_metrics / lines_of_code /
            # imports_count / functions_generated (zero non-test matches in
            # dashboard-react/src). A consumer appearing is the trigger to revisit.
            "approval_data": json.dumps(convert_decimals_to_float(approval_data)),
            # pipeline_config needs no conversion: get_pipeline_config returns either
            # a json.loads result (float, never Decimal) or a hardcoded literal.
            "pipeline_config": json.dumps(pipeline_config),
            # Auto-cleanup retention (30 days) from the shared config source
            "ttl": int((datetime.now(UTC) + timedelta(days=APPROVAL_RECORD_RETENTION_DAYS)).timestamp()),
        }

        # Trim approval_data if too large to prevent DynamoDB item size issues
        approval_data_str = approval_record.get("approval_data", "{}")
        if len(approval_data_str) > 50000:
            approval_record["approval_data"] = approval_data_str[:50000] + "...(truncated)"
            logger.warning(f"Trimmed approval_data from {len(approval_data_str)} to 50000 chars")

        # Store task token in dedicated table (if present)
        task_token = event.get("task_token", "")
        if task_token:
            store_task_token(approval_id, task_token)

        # Store in DynamoDB
        approvals_table.put_item(Item=approval_record)

        # Update the state table so the execution updater picks up the WAITING_APPROVAL
        # status. P28 R8.3: the echo targets the CANONICAL step row (never a
        # phantom approval_type row) and only updates an existing row.
        echo_step = state_echo_step_name(event["approval_type"])
        if echo_step is not None:
            try:
                state_table_name = os.environ.get("STATE_TABLE", "agentic_data_ingestion_state")
                state_table = dynamodb.Table(state_table_name)
                state_table.update_item(
                    Key={
                        "run_id": event["run_id"],
                        "step_name": echo_step,
                    },
                    UpdateExpression="SET #s = :status, updated_at = :now, approval_id = :aid",
                    ConditionExpression="attribute_exists(run_id)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": "waiting_approval",
                        ":now": datetime.now(UTC).isoformat(),
                        ":aid": approval_id,
                    },
                )
                logger.info(f"Updated state table with waiting_approval for {event['run_id']}/{echo_step}")
            except Exception as state_err:
                logger.warning(f"Failed to update state table with waiting_approval: {state_err}")

        # Denormalize pending_approval_id + pending_approval_type onto execution record (Task 17.2)
        try:
            exec_table_name = os.environ.get("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
            exec_table = dynamodb.Table(exec_table_name)
            exec_table.update_item(
                Key={
                    "pipeline_id": event["pipeline_id"],
                    "run_id": event["run_id"],
                },
                UpdateExpression="SET pending_approval_id = :aid, pending_approval_type = :atype, #s = :status",
                # attribute_exists(run_id) — annotate an EXISTING row only.
                # Without it update_item CREATES the row (p48 R9.2), and the row
                # it would create is {pipeline_id, run_id, pending_*, status}
                # with no `started_at`, so `RunsByTimeIndex` would never index
                # it and run history would never show it. This path does not
                # know the run's start time and must not invent one — that would
                # put a fabricated value on the index's sort key (p48 R1.4).
                # Same guard the state echo three lines above already carries.
                ConditionExpression="attribute_exists(run_id)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":aid": approval_id,
                    ":atype": event["approval_type"],
                    ":status": "WAITING_APPROVAL",
                },
            )
            logger.info(f"Denormalized approval info onto execution record: {event['pipeline_id']}/{event['run_id']}")
        except ClientError as exec_err:
            if exec_err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Row absent — nothing to denormalize onto. Not a failure: the
                # approval record itself is already written and the dashboard
                # reads it directly.
                logger.info(
                    f"No execution row for {event['pipeline_id']}/{event['run_id']} — "
                    f"pending approval not denormalized (the approval stands)"
                )
            else:
                logger.warning(f"Failed to denormalize approval onto execution record: {exec_err}")
        except Exception as exec_err:
            logger.warning(f"Failed to denormalize approval onto execution record: {exec_err}")

        # Record metrics
        ApprovalMetrics.record_approval_created(approval_type=event["approval_type"], pipeline_id=event["pipeline_id"])

        logger.info(f"Created approval request: {approval_id}")
        return approval_id

    except Exception as e:
        logger.error(f"Error creating approval request: {e}")
        raise


def send_approval_notifications(approval_record: dict[str, Any]) -> None:
    """Send notifications for approval request using unified notification service"""
    try:
        # Calculate timeout hours for display
        timeout_at = datetime.fromisoformat(approval_record["timeout_at"].replace("Z", "+00:00"))
        created_at = datetime.fromisoformat(approval_record["created_at"].replace("Z", "+00:00"))
        timeout_hours = int((timeout_at - created_at).total_seconds() / 3600)

        # Generate dashboard URL
        dashboard_url = f"https://dashboard.agentic_data_ingestion.com/approvals/{approval_record['approval_id']}"

        # Send unified notification
        notification_result = send_approval_notification(
            approval_type=approval_record["approval_type"],
            pipeline_id=approval_record["pipeline_id"],
            approval_id=approval_record["approval_id"],
            dashboard_url=dashboard_url,
            timeout_hours=timeout_hours,
        )

        # Record notification metrics based on result
        ApprovalMetrics.record_notification_sent(
            approval_type=approval_record["approval_type"],
            channel="unified",
            success=notification_result.get("success", False),
        )

        # Send standardized EventBridge event for dashboard updates
        publish_approval_status_change(
            approval_id=approval_record["approval_id"],
            pipeline_id=approval_record["pipeline_id"],
            approval_type=approval_record["approval_type"],
            status="pending",
            run_id=approval_record["run_id"],
            execution_id=approval_record.get("execution_arn"),
        )

        logger.info(f"Sent unified notification for approval {approval_record['approval_id']}: {notification_result}")

    except Exception as e:
        logger.error(f"Error sending notifications for approval {approval_record['approval_id']}: {e}")
        # Record failed notification
        ApprovalMetrics.record_notification_sent(
            approval_type=approval_record["approval_type"], channel="unified", success=False
        )


@time_function("ApprovalHandler.ProcessDecision")
def process_approval_decision(approval_id: str, decision: str, comment: str = None, user: str = None) -> dict[str, Any]:
    """Process approval decision (approve/reject).

    Send_Before_Status ordering (P28b R3.1, mirroring p28 task 3.1's
    read token → send → delete pattern): for token-carrying approvals the
    Step Functions callback is sent BEFORE the decision status is written,
    so the record can never claim a state the execution never reached.

    Returns the processed-decision result, or an error-shaped dict
    ({"error": "TaskTokenNotFound" | "ApprovalExpired", ...}) for the two
    non-retryable token outcomes. Transient send failures raise so the
    caller lands in the 500 path and retries (R3.3).
    """
    start_time = time.time()
    try:
        # Get approval record
        response = approvals_table.get_item(Key={"approval_id": approval_id})
        if "Item" not in response:
            raise ValueError(f"Approval not found: {approval_id}")

        approval_record = response["Item"]

        # Validate decision
        if decision not in ["approved", "rejected"]:
            raise ValueError(f"Invalid decision: {decision}. Must be 'approved' or 'rejected'")

        # Check if already processed
        if approval_record["status"] != "pending":
            raise ValueError(f"Approval already processed with status: {approval_record['status']}")

        # P28b R3.1 (Send_Before_Status): the task-token callback is sent
        # BEFORE any write, so a failed send can never leave the record
        # claiming a state the execution never reached. Tokens live ONLY in
        # the dedicated task-tokens table (store_task_token) — the approval
        # record carries just has_task_token; the old
        # approval_record.get("task_token") read never found one, so this
        # path never sent at all.
        if approval_record.get("has_task_token"):
            token_response = task_tokens_table.get_item(Key={"approval_id": approval_id})
            token_item = token_response.get("Item")
            if not token_item:
                # Token record TTL-reaped or already deleted — the approval
                # record stays untouched (mirrors the dashboard's 410).
                logger.warning(f"Task token not found or expired for approval {approval_id}")
                return {
                    "error": "TaskTokenNotFound",
                    "approval_id": approval_id,
                    "detail": f"Task token not found or expired for approval {approval_id}",
                }
            task_token = token_item["task_token"]

            try:
                if decision == "approved":
                    stepfunctions.send_task_success(
                        taskToken=task_token,
                        output=json.dumps(
                            {
                                "approval_decision": decision,
                                "approval_id": approval_id,
                                "comment": comment,
                                "processed_by": user,
                                "processed_at": datetime.now(UTC).isoformat(),
                            }
                        ),
                    )
                else:
                    stepfunctions.send_task_failure(
                        taskToken=task_token, error="ApprovalRejected", cause=f"Approval rejected by {user}: {comment}"
                    )
            except ClientError as send_err:
                if is_token_consumed_error(send_err):
                    # Already-consumed/expired token (P28 R2.3 set): the
                    # execution is no longer waiting on this approval — clean
                    # up and mark "expired" (never approved/rejected). Not
                    # retryable (R3.5).
                    error_code = send_err.response.get("Error", {}).get("Code", "")
                    logger.warning(f"Task token for approval {approval_id} already consumed or expired ({error_code})")
                    task_tokens_table.delete_item(Key={"approval_id": approval_id})
                    approvals_table.update_item(
                        Key={"approval_id": approval_id},
                        UpdateExpression="SET #status = :status, expired_at = :expired_at",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":status": "expired",
                            ":expired_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    return {"error": "ApprovalExpired", "approval_id": approval_id, "status": "expired"}
                # Transient failure (throttle, service 5xx): re-raise into the
                # handler's 500 path — the approval stays "pending" and the
                # token record is retained so the caller can retry (R3.3).
                logger.error(f"Transient failure sending task callback for approval {approval_id}: {send_err}")
                raise

            # Send confirmed — the token is consumed; delete its record now.
            task_tokens_table.delete_item(Key={"approval_id": approval_id})

        # Write the decision status (only after a successful send)
        update_expression = "SET #status = :status, processed_at = :processed_at"
        expression_values = {":status": decision, ":processed_at": datetime.now(UTC).isoformat()}
        expression_names = {"#status": "status"}

        if comment:
            update_expression += ", #comment = :comment"
            expression_values[":comment"] = comment[:1000]  # Limit comment length
            expression_names["#comment"] = "comment"

        if user:
            update_expression += ", processed_by = :user"
            expression_values[":user"] = user

        approvals_table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
            ExpressionAttributeNames=expression_names,
        )

        # Log audit trail
        logger.info(f"Approval {approval_id} {decision} by {user}: {comment}")

        # Update the state table to reflect the approval decision
        # This changes the step status from 'waiting_approval' back to 'success' (approved) or 'rejected'.
        # P28 R8.3: the echo targets the CANONICAL step row (never a phantom
        # approval_type row) and only updates an existing row.
        # Hoisted out of the try below: the executions annotation that follows
        # reads both, so a state-echo failure must not leave them unbound.
        # Coerced to str because a DynamoDB item value is a wide union and both
        # go into a Key dict and a log message. `or ""` rather than a bare
        # str(): str(None) is "None", which is truthy and would send a
        # nonexistent key to DynamoDB.
        run_id = str(approval_record.get("run_id") or "")
        approval_type = str(approval_record.get("approval_type") or "")
        pipeline_id = str(approval_record.get("pipeline_id") or "")
        try:
            echo_step = state_echo_step_name(approval_type) if run_id else None
            if run_id and echo_step is not None:
                new_status = "success" if decision == "approved" else "rejected"
                state_table_name = os.environ.get("STATE_TABLE", "agentic_data_ingestion_state")
                state_tbl = dynamodb.Table(state_table_name)
                state_tbl.update_item(
                    Key={"run_id": run_id, "step_name": echo_step},
                    UpdateExpression="SET #s = :status, updated_at = :now",
                    ConditionExpression="attribute_exists(run_id)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": new_status,
                        ":now": datetime.now(UTC).isoformat(),
                    },
                )
                logger.info(f"Updated state table: {run_id}/{echo_step} → {new_status}")

        except Exception as state_err:
            logger.warning(f"Failed to update state table after approval: {state_err}")

        # Clear pending_approval_id from execution record (Task 17.3).
        # Its own try/except, not the state echo's: a row-absent outcome here is
        # ordinary and must not be reported as a state-table failure.
        if run_id and pipeline_id:
            new_exec_status = "RUNNING" if decision == "approved" else "FAILED"
            try:
                exec_table_name = os.environ.get("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
                exec_tbl = dynamodb.Table(exec_table_name)
                exec_tbl.update_item(
                    Key={"pipeline_id": pipeline_id, "run_id": run_id},
                    UpdateExpression="REMOVE pending_approval_id, pending_approval_type SET #s = :status, updated_at = :now",
                    # attribute_exists(run_id) — annotate an EXISTING row only.
                    # Without it update_item CREATES a row carrying no
                    # `started_at`, which `RunsByTimeIndex` cannot index, so the
                    # run would be invisible in run history with no error
                    # (p48 R9.2). Neither decision branch knows the run's start
                    # time and must not fabricate one onto the index's sort key
                    # (p48 R1.4). Same guard as the state echo above.
                    ConditionExpression="attribute_exists(run_id)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": new_exec_status,
                        ":now": datetime.now(UTC).isoformat(),
                    },
                )
                logger.info(f"Cleared pending_approval from execution: {pipeline_id}/{run_id} → {new_exec_status}")
            except ClientError as exec_err:
                if exec_err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    # Row absent — nothing to annotate. The decision itself has
                    # already landed on the approvals row; bookkeeping about a
                    # run must never fail it (`LGR-eee8ac8-4`).
                    logger.info(
                        f"No execution row for {pipeline_id}/{run_id} — no decision status to annotate "
                        f"(the decision stands)"
                    )
                else:
                    logger.warning(f"Failed to update execution record after decision: {exec_err}")
            except Exception as exec_err:
                logger.warning(f"Failed to update execution record after decision: {exec_err}")

        # Calculate response time and record metrics
        response_time_ms = (time.time() - start_time) * 1000
        ApprovalMetrics.record_approval_processed(
            approval_type=approval_record["approval_type"],
            decision=decision,
            response_time_ms=response_time_ms,
            pipeline_id=approval_record["pipeline_id"],
        )

        # Send standardized notification about decision
        publish_approval_status_change(
            approval_id=approval_id,
            pipeline_id=approval_record["pipeline_id"],
            approval_type=approval_record["approval_type"],
            status=decision,
            run_id=approval_record["run_id"],
            execution_id=approval_record.get("execution_arn"),
            approver=user,
            decision_details={"comment": comment, "processed_at": datetime.now(UTC).isoformat()},
        )

        return {
            "approval_id": approval_id,
            "decision": decision,
            "processed_at": datetime.now(UTC).isoformat(),
            "processed_by": user,
        }

    except Exception as e:
        logger.error(f"Error processing approval decision for {approval_id}: {e}")
        raise


def lambda_handler(event, context):
    """
    Lambda function to handle approval requests and decisions.

    Supports two modes:
    1. Create approval request (from Step Functions)
    2. Process approval decision (from API/Dashboard)

    The two modes have deliberately DIFFERENT failure contracts, because they
    have different callers. Create mode is invoked by a Step Functions
    `.waitForTaskToken` state, which needs a raise (p34a R2.3). Decision mode is
    invoked by the dashboard API, which consumes status codes and must never see
    a raise (p34a R3.4, P28b R3.3/R3.5). Same handler, two callers, two correct
    contracts.
    """
    # p34a R2.3/R3.5: the line between "report an error by returning" and "fail
    # the invocation" is drawn by ORIGIN, not by exception TYPE. Any exception
    # escaping `create_approval_request` — ValueError included — must reach Step
    # Functions as an invocation failure; only a MALFORMED EVENT still returns
    # 400. This flag records the origin so both requirements hold at once.
    #
    # Why not by type: `create_approval_request` re-raises whatever it caught
    # (:503-505), so under a type-based rule a ValueError originating INSIDE it
    # would land in the `except ValueError` clause below, be returned as 400, and
    # strand the execution exactly as it does today. Type cannot tell the two
    # sources apart; origin can. R2.3 says create mode raises "for any reason",
    # R3.5 scopes the 400 to a malformed event — the by-origin rule satisfies
    # both, a by-type rule satisfies only R3.5.
    #
    # The hazard being closed is the one LGR-60c4985-3 (template / ratified)
    # already records for waitForTaskToken steps: a return that skips the token
    # send makes the state "hang for its full TimeoutSeconds" — here 259200s /
    # 72h. A Lambda that RETURNS is a successful invocation, so Step Functions
    # records a healthy TaskSubmitted and the Catch on States.ALL never fires.
    # That record establishes the principle for the retry-resume path; this
    # applies it to the exception path it did not cover.
    #
    # Shape follows the narrowing pattern in LGR-78ce0b7-13 / LGR-b2f79ef-13
    # (template): keep the specific clause for the errors it exists for, stop the
    # blanket clause absorbing everything else.
    #
    # Consequence, stated plainly rather than papered over: after this change a
    # serialization fault fails the execution in ~50ms instead of stranding it
    # for 72h. The run still does not complete — the point is that someone finds
    # out. That is the intended outcome, not a regression.
    create_request_failed = False
    try:
        logger.info(f"Approval handler event: {json.dumps(event)}")

        logger.append_keys(
            run_id=event.get("run_id", ""), pipeline_id=event.get("pipeline_id", ""), step_name="ApprovalHandler"
        )

        # Determine operation mode
        if "approval_type" in event and "pipeline_id" in event:
            # Mode 1: Create approval request (from Step Functions)
            # `validate_approval_request` is deliberately OUTSIDE the inner try:
            # its ValueError is a malformed event, which stays a 400 (R3.5).
            validated_event = validate_approval_request(event)
            try:
                approval_id = create_approval_request(validated_event)
            except Exception:
                # Record the origin and re-raise UNCHANGED. Bare `raise` keeps
                # the exception identity and traceback intact, so the error name
                # an operator sees when the Catch fires is the real one rather
                # than a re-label.
                create_request_failed = True
                raise

            # Get the created approval record for notifications
            response = approvals_table.get_item(Key={"approval_id": approval_id})
            if "Item" in response:
                send_approval_notifications(response["Item"])

            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": "Approval request created successfully",
                        "approval_id": approval_id,
                        "status": "pending",
                    }
                ),
            }

        elif "approval_id" in event and "decision" in event:
            # Mode 2: Process approval decision (from API/Dashboard)
            result = process_approval_decision(
                approval_id=event["approval_id"],
                decision=event["decision"],
                comment=event.get("comment"),
                user=event.get("user", "system"),
            )

            if "error" in result:
                # Non-retryable outcomes (P28b R3.5): 409-style for a
                # consumed/expired token (approval marked "expired"),
                # 410-style for a TTL-reaped token record (approval
                # untouched). Transient send failures never get here — they
                # raise into the 500 path below so the caller retries (R3.3).
                status_code = 409 if result["error"] == "ApprovalExpired" else 410
                return {"statusCode": status_code, "body": json.dumps(result)}

            return {
                "statusCode": 200,
                "body": json.dumps({"message": f"Approval {result['decision']} successfully", "result": result}),
            }

        else:
            raise ValueError("Invalid event format. Must contain either approval request or decision data")

    except ValueError as e:
        if create_request_failed:
            # By ORIGIN: this ValueError came out of `create_approval_request`,
            # not from a malformed event, so it is a create-mode failure and must
            # fail the invocation (R2.3). Returning 400 here would strand the
            # .waitForTaskToken state for its full TimeoutSeconds.
            logger.error(f"Error in approval handler: {e}")
            raise
        # A malformed event: still 400 (R3.5). Reached from
        # `validate_approval_request`, from `process_approval_decision`'s own
        # three ValueErrors, and from the neither-mode `else` above. A validation
        # error is already correctly non-retryable and is not part of the
        # stranding mechanism.
        logger.error(f"Validation error: {e}")
        return {"statusCode": 400, "body": json.dumps({"error": "ValidationError", "message": str(e)})}

    except Exception as e:
        logger.error(f"Error in approval handler: {e}")
        if create_request_failed:
            # Create mode: raise so the .waitForTaskToken state fails through its
            # Catch on States.ALL immediately, instead of waiting out
            # TimeoutSeconds (R2.3, LGR-60c4985-3).
            raise
        # Everything else — decision mode above all — keeps the 500 return. Its
        # caller is the dashboard API, which reads the status code and retries a
        # transient failure (R3.4, P28b R3.3).
        return {"statusCode": 500, "body": json.dumps({"error": "InternalError", "message": str(e)})}
