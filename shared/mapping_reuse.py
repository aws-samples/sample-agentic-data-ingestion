# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which ``schema_mapping`` state rows a later run may reuse, and how a rejection revokes one.

Three readers ask whether an earlier run's mapping may stand in for a fresh one:

* ``schema_compare`` — the reuse candidate for an unchanged source schema;
* ``create_code/mapping_resolution`` — the freshest approved mapping for codegen;
* ``schema_mapping.find_reusable_rename_manifest`` — a rename manifest that
  exempts a run from a human decision.

``LGR-5d68eeb-2`` requires the three to share one definition of "approved", so
that a one-sided edit cannot re-fork them. :func:`approved_for_reuse_condition`
is that definition. A row qualifies when both hold:

1. ``status == "success"`` — approved, or approval was not required. A
   rejection rests the row's own status at ``rejected``, and a row still at the
   gate rests at ``waiting_approval``; neither qualifies.
2. it carries no :data:`REUSE_REVOKED_AT` — no later rejection has withdrawn it.

Why a second attribute instead of flipping ``status``
-----------------------------------------------------
A mapping is often rejected at a run that did not produce it: a reuse run
carries no ``schema_mapping`` row of its own, so the rejected mapping lives on
an earlier run's row. That earlier row is the history of the earlier run, which
may have been approved and loaded data. Rewriting its ``status`` to
``rejected`` would misstate what happened on that run, and would flip its
executions row to FAILED through the state-table stream. :func:`revoke_mapping_reuse`
therefore adds :data:`REUSE_REVOKED_AT` and leaves ``status`` alone.

Annotation writes
-----------------
``execution_updater`` re-derives a run's executions row from every write to one
of its step rows. Two writers touch a step row without that run making
progress: a user-triggered mapping regeneration (``POST /api/rerun-mapping``)
and a revocation. Both stamp :data:`ANNOTATED_AT`; ``execution_updater`` skips a
record whose latest write is an annotation (:func:`annotation_is_latest_write`).
Before this, regenerating a mapping on a finished run flipped that run back to
RUNNING.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from botocore.exceptions import ClientError

#: Set on a ``schema_mapping`` row when a later rejection withdraws its mapping
#: from reuse. ISO-8601 UTC.
REUSE_REVOKED_AT = "reuse_revoked_at"

#: The approval whose rejection withdrew the row, beside :data:`REUSE_REVOKED_AT`.
REUSE_REVOKED_BY_APPROVAL = "reuse_revoked_by_approval"

#: Stamped by a write that is not progress of the row's run. ISO-8601 UTC.
ANNOTATED_AT = "annotated_at"

#: The state-row step name every reader here filters on.
SCHEMA_MAPPING_STEP = "schema_mapping"

#: Page cap for :func:`revoke_mapping_reuse`. Equal to the readers' own caps
#: (``_MAPPING_LOOKUP_MAX_PAGES`` in ``create_code/mapping_resolution.py``), and
#: the walk is newest-first like theirs, so it covers every row they can reach.
REVOCATION_MAX_PAGES = 10

_STATUS_SUCCESS = "success"
_STATUS_WAITING_APPROVAL = "waiting_approval"
_STATUS_REJECTED = "rejected"


def approved_for_reuse_condition() -> ConditionBase:
    """The shared "approved for reuse" predicate, as a DynamoDB filter condition.

    Callers AND it with their own conjuncts (``step_name``, ``mapping_id``
    presence, schema hash). It never admits a row with an absent ``status``;
    see ``LGR-5d68eeb-2`` for why a permissive arm would point the wrong way.
    """
    return Attr("status").eq(_STATUS_SUCCESS) & Attr(REUSE_REVOKED_AT).not_exists()


def status_after_regeneration(existing: Mapping[str, Any] | None, mapping_id: str) -> str:
    """The ``status`` a regeneration writes over ``existing``, the run's current row.

    * No row yet: ``success``. A user-triggered regeneration needs no approval of
      its own; a later run that reuses it still passes the gate
      (``LGR-5d68eeb-1``).
    * ``waiting_approval``: kept. The run is still at the gate, and writing
      ``success`` would present an undecided mapping as approved.
    * ``rejected`` with the same ``mapping_id``: kept. ``mapping_id`` is a content
      hash, so this is the content a human already refused.
    * Anything else, including ``rejected`` with new content: ``success``.
    """
    if existing is None:
        return _STATUS_SUCCESS
    status = existing.get("status")
    if status == _STATUS_WAITING_APPROVAL:
        return _STATUS_WAITING_APPROVAL
    if status == _STATUS_REJECTED and existing.get("mapping_id") == mapping_id:
        return _STATUS_REJECTED
    return _STATUS_SUCCESS


def revocation_kept_by_regeneration(existing: Mapping[str, Any] | None, mapping_id: str) -> dict[str, Any]:
    """The revocation attributes a regeneration must carry over, or ``{}``.

    A regeneration replaces the whole row. When it reproduces content that was
    revoked, dropping the revocation would make refused content reusable again.
    New content starts unrevoked.
    """
    if existing is None or existing.get("mapping_id") != mapping_id or REUSE_REVOKED_AT not in existing:
        return {}
    carried = {REUSE_REVOKED_AT: existing[REUSE_REVOKED_AT]}
    if REUSE_REVOKED_BY_APPROVAL in existing:
        carried[REUSE_REVOKED_BY_APPROVAL] = existing[REUSE_REVOKED_BY_APPROVAL]
    return carried


def _parse_instant(value: str) -> datetime | None:
    """An ISO-8601 string as an aware UTC datetime, or ``None`` if unparseable.

    A value without a zone suffix is read as UTC, so a naive writer and an aware
    one still compare by instant rather than raising ``TypeError``.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def annotation_is_latest_write(annotated_at: str, *progress_stamps: str) -> bool:
    """True when ``annotated_at`` is at least as recent as every progress stamp.

    ``progress_stamps`` are the row's run-progress timestamps (``timestamp``,
    ``updated_at``); empty strings mean "absent" and are ignored. A step that
    runs, or an approval decision that lands, after the annotation writes a newer
    stamp, so its own stream record is processed normally.

    Fails toward processing: an absent or unparseable ``annotated_at``, or any
    present progress stamp that cannot be parsed, answers ``False``, which keeps
    ``execution_updater``'s behaviour from before annotations existed.
    """
    annotated = _parse_instant(annotated_at) if annotated_at else None
    if annotated is None:
        return False
    for stamp in progress_stamps:
        if not stamp:
            continue
        progress = _parse_instant(stamp)
        if progress is None or progress > annotated:
            return False
    return True


def revoke_mapping_reuse(
    state_table: Any,
    pipeline_id: str,
    mapping_id: str,
    *,
    approval_id: str,
    now: str | None = None,
    max_pages: int = REVOCATION_MAX_PAGES,
) -> int:
    """Withdraw ``mapping_id`` from reuse on every approved row of ``pipeline_id``.

    Walks the ``PipelineIndex`` newest-first for ``schema_mapping`` rows that
    carry ``mapping_id`` and still satisfy :func:`approved_for_reuse_condition`,
    and stamps each with :data:`REUSE_REVOKED_AT`, :data:`REUSE_REVOKED_BY_APPROVAL`
    and :data:`ANNOTATED_AT`. ``status`` is not touched (see the module docstring).

    Each update is conditional on the row still holding ``mapping_id``, so a
    regeneration that replaced the content in the meantime keeps its new
    content reusable. A row that fails that condition is skipped, not an error.

    Returns the number of rows revoked. Raises on any other DynamoDB error; the
    caller decides whether that may fail its request.
    """
    stamp = now or datetime.now(UTC).isoformat()
    revoked = 0
    last_key: dict[str, Any] | None = None
    for _ in range(max_pages):
        query: dict[str, Any] = {
            "IndexName": "PipelineIndex",
            "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
            "FilterExpression": (
                Attr("step_name").eq(SCHEMA_MAPPING_STEP)
                & Attr("mapping_id").eq(mapping_id)
                & approved_for_reuse_condition()
            ),
            "ScanIndexForward": False,
        }
        if last_key:
            query["ExclusiveStartKey"] = last_key
        response = state_table.query(**query)
        for row in response.get("Items", []):
            run_id = row.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            try:
                state_table.update_item(
                    Key={"run_id": run_id, "step_name": SCHEMA_MAPPING_STEP},
                    UpdateExpression="SET #revoked_at = :now, #revoked_by = :approval_id, #annotated_at = :now",
                    ConditionExpression="attribute_exists(run_id) AND mapping_id = :mapping_id",
                    ExpressionAttributeNames={
                        "#revoked_at": REUSE_REVOKED_AT,
                        "#revoked_by": REUSE_REVOKED_BY_APPROVAL,
                        "#annotated_at": ANNOTATED_AT,
                    },
                    ExpressionAttributeValues={":now": stamp, ":approval_id": approval_id, ":mapping_id": mapping_id},
                )
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue
                raise
            revoked += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
    return revoked
