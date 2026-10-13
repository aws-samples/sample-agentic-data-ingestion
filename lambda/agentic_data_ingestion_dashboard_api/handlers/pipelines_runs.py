#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run / execution operations for the dashboard API.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.
"""

import contextlib
import json
import os
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from audit_utils import extract_request_context, write_audit_entry
from dynamo_typing import item_dict, item_int, item_list, item_str
from load_outcome import (
    FROM_DQ_REPORT_ROW_LOWER_BOUND,
    FROM_LOAD_METRICS_ROW,
    FROM_LOAD_METRICS_ROW_ABSENT,
    FROM_LOAD_METRICS_ROW_INCOMPLETE,
    OUTCOME_NOT_ESTABLISHED,
    derive_load_outcome,
)
from step_names import STEP_SEQUENCE, TASK_STEPS, StepName, from_any

from .common import (
    AWS_REGION,
    RUNS_BY_TIME_INDEX,
    STATE_TABLE_NAME,
    DecimalEncoder,
    approvals_table,
    create_response,
    dynamodb,
    executions_table,
    get_aws_account_id,
    handle_error,
    is_phantom_execution_row,
    logger,
    pipelines_table,
    require_env,
    state_table,
)
from .pipelines_artifacts import _read_s3_object
from .pipelines_logs import get_step_logs
from .run_cursor import InvalidCursorError, decode_cursor, encode_cursor


def _canonical_or_raw(step_name: str) -> str:
    """Canonical spelling for any known step-name variant; raw value otherwise.

    P28 R8.2 (task 14.2): new state rows carry canonical snake_case names
    while rows written before the rename persist with the legacy PascalCase
    variants ("CodeGeneration", "RunPipeline", …). Read paths compare in
    canonical space so both generations of rows are recognized; unknown
    names (agent records, sentinels) pass through unchanged.
    """
    try:
        return str(from_any(step_name))
    except ValueError:
        return step_name


# ── Load_Outcome (p37 R1.7, R1.9, R1.10) ─────────────────────────────────
# The ``step_name`` of the counts row the generated Glue job writes (p37 C1).
# It is a record, not a Step_Row: it is absent from ``step_names.STEP_SEQUENCE``,
# which is why ``execution_updater``'s allowlist routes it away from the step
# machinery (``_to_step_or_none``) without naming it. Spelled once here so this
# module's two readers — the run list below and the run detail's synthetic-step
# filter — cannot drift apart.
_LOAD_METRICS_STEP = "load_metrics"

# The DQ_Report_Row's ``step_name`` (p37 C5). Also a record and not a Step_Row —
# it is already in the run-detail synthetic-step denylist below, by that name,
# and has been since before p37. Read here only for the retroactive lower bound:
# runs that completed before C1 shipped carry no ``load_metrics`` row, and for
# inline-DQ ``s3_tables`` runs this row carries ``rows_passed``, which IS
# Rows_Written (``glue_job_s3_tables.py.j2:307``).
_DQ_REPORT_STEP = "data_quality"

# ── Chronological ordering (p48 C1) ──────────────────────────────────────
# ``RUNS_BY_TIME_INDEX`` — ``(pipeline_id, started_at)``, ``ProjectionType.ALL``,
# on the executions table — is imported from ``.common`` above and used unchanged
# throughout this module. Task 5.6 moved the literal there when
# ``handlers/data_preview.py`` became its second reader; ``.common`` is where
# ``executions_table`` lives and both modules already import it, so neither
# handler needs to import the other for one string. The reasoning, and the
# rejected alternatives, are recorded beside the constant itself.

# ── The exact total, and its bounds (p48 C3, R2.2–R2.5, task 3.3) ────────
# ``total_count`` was ``len(executions)`` — the current page's length after
# filtering — and ``total_pages`` was the literal ``1``, which contradicted
# ``has_next`` in the same envelope. Both now derive from a windowless
# ``Select="COUNT"`` Query over ``RUNS_BY_TIME_INDEX``, fully paginated under the
# two bounds below, with any bound that binds named in ``partial_counts``.
#
# **This is ``LGR-9a35b2d-3``'s shape reused, not a new mechanism** (R2.5): that
# record established windowless ``Select="COUNT"`` Queries, fully paginated,
# bounded by a page cap AND a wall-clock deadline, with an always-present
# ``partial_counts`` key, for the four stats counts. ``handlers/stats.py``'s
# ``_count_step_rows`` / ``_read_deadline`` are the implementation being matched
# here — same loop, same ``(count, complete)`` return, same "the first page is
# always read and the bound applies to *continuing*" property. Divergence in shape
# between two implementations of one mechanism is what ``LGR-99050e6-5`` fences
# against on the test side, and the argument transfers.
#
# ``get_pipeline_file_count`` (``pipelines_crud.py:97``) is the counter-example
# named in ``LGR-9a35b2d-2``'s scope and must NOT be imitated: one
# ``Select="COUNT"`` Query with no ``Limit`` that never follows
# ``LastEvaluatedKey``, so a partition larger than one 1 MB page silently
# undercounts. The walk below follows the continuation token; that is the point.

#: Page cap on the count walk. 100, following ``stats.py:_MAX_READ_PAGES`` and the
#: figure it borrows from ``cost_optimization_agent/main.py`` — and 100 rather than
#: ``pipelines_crud.py``'s 20 for the reason that file states: its cap bounds bytes
#: accumulated before an in-memory sort, while a ``Select="COUNT"`` walk accumulates
#: **nothing** but an int. The deadline is what makes the bound hold; this is the
#: cheap backstop against a pathological page.
_MAX_COUNT_PAGES = 100

#: This read's time budget. The dashboard API Lambda's timeout is 25 s and is
#: recorded as load-bearing (``compute-stack.ts:770-788``), so 20 s leaves 5 s in
#: hand — which is what makes an overrun a *reportable* outcome (a 200 carrying
#: ``partial_counts``) rather than a gateway timeout carrying no body. Same figure
#: and same reasoning as ``stats.py:_STATS_TIME_BUDGET_SECONDS`` and
#: ``pipelines_crud.py:_LIST_TIME_BUDGET_SECONDS``.
_RUNS_TIME_BUDGET_SECONDS = 20.0

#: Held back out of that budget for everything after the count returns:
#: ``create_response`` and the JSON encode of one page of enriched executions.
#: 2.0 — ``stats.py``'s reserve rather than ``pipelines_crud.py``'s 3.0, because
#: this payload is a page of at most ``page_size`` rows and not a 20-row page
#: carrying schema history. A test that needs the deadline to bind shortens the
#: budget rather than asserting a wall-clock duration.
_RUNS_READ_RESERVE_SECONDS = 2.0

#: The envelope keys derived from the count, in the order ``partial_counts`` emits
#: them, so the marker is deterministic and assertable with ``==``.
#:
#: **``has_next`` and ``next_cursor`` are deliberately absent, and this is the
#: reconciliation R2.3 asks for.** ``pipelines_crud.py``'s equivalent list names
#: ``has_more`` because there ``has_more = page < total_pages`` and so inherits the
#: count's floor. Here ``has_next`` is "a cursor was returned", minted from the page
#: query's own ``LastEvaluatedKey`` — a fact about the page read, independent of the
#: count walk. So navigation stays **exact** even when the count is a floor: a
#: truncated count cannot strand a reachable page. ``executions`` is absent for the
#: same kind of reason — ``Limit=page_size`` bounds the page server-side, so the
#: page is a correctly-ordered exact prefix whatever the count walk did. Ordering is
#: never truncated under the index, so ``partial_counts`` has nothing to say about
#: row order — only about counts (design §Data Models).
_COUNT_DERIVED_KEY_ORDER: tuple[str, ...] = ("total_count", "total_pages")

# ── The status filter, and its bounds (p48 C5, R4.2–R4.4, task 4.3) ──────
# ``status`` filters the **derived** status — the value after
# :func:`get_pipeline_executions` rewrites it to ``WAITING_APPROVAL`` for a row
# carrying ``pending_approval_id``. That is do-NOT 8 and R4.2, and it is the whole
# reason the filter sits where it does rather than on the query: a
# ``FilterExpression`` on the **stored** ``status`` returns NOTHING for
# ``WAITING_APPROVAL``, the one status an operator most wants to find, because no
# writer ever stores that string. ``CP3`` is the property that fails if the filter
# moves above the derivation.
#
# **A server-side ``FilterExpression`` is tempting here and is wrong twice over.**
# ``LGR-9a35b2d-2`` (**template / ratified**) measured the second reason one file
# over: ``Limit`` is applied by DynamoDB *before* ``FilterExpression``, so a
# filtered ``Limit=page_size`` is a page-sized **window** and not a bound on the
# answer — that record found ``mapping_count: 0`` for a pipeline whose row carried
# 7. Its fix is the shape used below: follow ``LastEvaluatedKey`` under a page cap
# and a wall-clock deadline, and name the floor. Note that record explicitly
# *exempts* this file's executions probe ("it wants the latest rows and takes them,
# with no ``FilterExpression`` for a ``Limit`` to be applied ahead of") — that
# exemption ends the moment a filter is introduced, which is now.
#
# ``LGR-eee8ac8-3`` reached the same conclusion independently on ``schema_compare``'s
# ``PipelineIndex`` reads: an all-filtered page "returns empty with a
# ``LastEvaluatedKey``". Reasoning precedent, not a fence on this file.

#: The status values ``?status=`` accepts, each with its producer, so the set is an
#: enumeration of what can actually be matched rather than a guess.
#:
#: * ``RUNNING`` / ``SUCCEEDED`` / ``FAILED`` — ``execution_updater
#:   ._derive_execution_status`` (``:643-652``), plus ``record_failure:472`` for
#:   ``FAILED`` and ``pipeline_trigger`` for the initial ``RUNNING``.
#: * ``SKIPPED`` — ``pipeline_trigger:230`` (contention) and ``:491`` (duplicate
#:   content). **First-class, because it is the case that opened this spec** (R4.4).
#: * ``CANCELLED`` — :func:`_mark_cancelled` (a run stopped at a Lambda step or an
#:   approval gate), and ``execution_updater._derive_execution_status`` from the
#:   ``cancelled`` run_pipeline row job_completion writes once a stopped Glue job
#:   has stopped.
#: * ``CANCELLING`` — :func:`_mark_cancelling`: the run's Glue job is being stopped.
#: * ``WAITING_APPROVAL`` — **no writer at all.** Derived on this read path, which is
#:   why the filter runs after the derivation and not on the query.
#: * ``IN_PROGRESS`` — also no writer, and kept anyway for the old-row tolerance
#:   ``LGR-5aad762-2`` argues for in this same file: "a live table can hold shapes no
#:   current producer writes". Two readers already tolerate it (``:1059`` here and
#:   ``handlers/health.py:66``), so a legacy row carrying it must be findable rather
#:   than answered with a 400. Task 5.5 may leave it out of the dropdown without any
#:   server change; the accepted set and the offered set are not required to match.
#:
#: ``QUEUED`` is deliberately absent and is refused with its own message — see
#: :data:`_QUEUED_STATUS_FILTER_MESSAGE`.
_ACCEPTED_STATUS_FILTERS: frozenset[str] = frozenset(
    {
        "RUNNING",
        "IN_PROGRESS",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "SKIPPED",
        "CANCELLED",
        "CANCELLING",
    }
)

#: p48 O2, resolved. ``QUEUED`` is refused rather than accepted-and-empty, and the
#: reason is **structural rather than incidental**: the ``QUEUED`` split runs BEFORE
#: the filter position, so by the time the filter sees the list every ``QUEUED`` row
#: has already been moved into ``queued_files``. ``status=QUEUED`` is therefore a
#: query over an array that provably cannot contain a match — not "no runs match",
#: which is a fact about the data, but "this question cannot be asked of this array",
#: which is a fact about the shape. An empty 200 would read as the former.
#:
#: *Rejected: synthesising the ``queued_files`` entries into ``executions``.* A
#: queued entry is four keys (``file_name``, ``source_key``, ``queued_at``,
#: ``status``) with no ``run_id`` and no ``started_at``, so both would have to be
#: invented — a second shape for a queued row beside the one ``queued_files``
#: already publishes, and ``get_queued_files`` reads that one (do-NOT 3).
#:
#: *Rejected: accepting it and returning an empty list.* That is precisely the
#: "control that returns an empty table" O2's own fallback warns against, and it
#: hides the misunderstanding instead of naming it.
#:
#: **Correction (p54 do-NOT 15).** Task 1.3's finding that *"no ``QUEUED`` row exists
#: anywhere in the live table"* was a **measurement at a moment**, not a standing
#: property: placeholders are deleted on processing by ``_remove_queued_file`` and
#: carry a 1-hour TTL, so the table is usually empty of them and was on the day it
#: was read. p54 task 7.1 re-measured and again found **zero** live ``QUEUED`` rows —
#: same result, same reason. That is not evidence the array cannot hold one, and p54
#: R9 makes rows of that shape matter, so the refusal above rests on the structural
#: argument alone. **The 400 itself is unaffected and stays** (R9.7, R8.12).
_QUEUED_STATUS_FILTER_MESSAGE = (
    "QUEUED files are returned in the queued_files array, not in executions; "
    "the status filter operates on the executions list"
)

#: Page cap on the status filter walk. 100, the same figure and the same reasoning as
#: :data:`_MAX_COUNT_PAGES`. It is also the loop's hard backstop against a page that
#: returns no rows while still carrying a ``LastEvaluatedKey``.
_MAX_FILTER_PAGES = 100

#: The filter walk's **share** of the request budget, carved out of the same
#: :data:`_RUNS_TIME_BUDGET_SECONDS` the count walk draws on.
#:
#: **Task 3.3 argued no carve-out was needed and was right for its own read; the
#: argument does not transfer to this one.** Its reasoning was positional — "the count
#: is the **last** read in the function, so it is the one that yields". The filter
#: walk is the **first**: it decides which rows get enriched, so without a share of
#: its own it could spend the whole window and starve all three reads behind it — the
#: schema-version map, the cost / ``load_outcome`` enrichment (every row degrading to
#: ``actual_cost: None`` and ``not_established``) and the count (degrading to a
#: floor). A page short by a bound and named in ``partial_counts`` is a far better
#: outcome than a full page of rows annotated with nothing.
#:
#: 8.0 of the 20 s budget, which is ``pipelines_crud.py:_PAGE_SCAN_TIME_SHARE_SECONDS``
#: exactly — the in-tree precedent for this same ordering problem, where "giving it
#: the enrichment deadline would let one slow walk consume the entire budget and turn
#: every entry on the page partial". That leaves at least 10 s for the reads behind it
#: after the 2 s reserve. A test that needs this to bind shortens it rather than
#: asserting a wall-clock duration.
_FILTER_WALK_TIME_SHARE_SECONDS = 8.0

#: The envelope key the filter walk names when a bound stopped it short of a full
#: page, emitted **before** :data:`_COUNT_DERIVED_KEY_ORDER` so the marker is
#: deterministic and assertable with ``==``.
#:
#: **This is the premise task 3.3 recorded, changing under a filter — deliberately.**
#: 3.3 left ``executions`` out of its list because "``Limit=page_size`` bounds the
#: page server-side, so the page is a correctly-ordered exact prefix whatever the
#: count walk did". That still holds unfiltered. Under a filter walk a bound can stop
#: the walk with matches still unread, so a page shorter than ``page_size`` no longer
#: means "the filtered list ended" — and a short page that reads as the end of the
#: list is exactly the quiet wrongness this spec opened on (R4.3).
#:
#: Narrower than ``pipelines_crud.py``'s reason for naming ``pipelines``, and the
#: difference is worth stating: there a truncated scan makes the page "a *selection*
#: over a subset — the entries can be the wrong entries". Here the walk reads
#: newest-first in index order from the cursor, so a truncated page is still a correct
#: **prefix** of the filtered set: fewer entries, never the wrong ones.
_FILTER_DERIVED_KEY_ORDER: tuple[str, ...] = ("executions",)

# ── The schema-version map, and its bounds (p48 C6, R6.1, task 6.1) ──────
# The enrichment queried ``PipelineIndex`` with
# ``FilterExpression=Attr("step_name").eq("schema_extract")``, ``ScanIndexForward=
# True``, **no ``Limit`` and no ``LastEvaluatedKey`` follow** — while its own comment
# claimed it read "ALL distinct schema hashes … to ensure consistent version numbering
# across pagination". A ``FilterExpression`` is applied *after* the 1 MB page read, so
# on a partition larger than one page the response was silently short and the version
# map incomplete.
#
# **This is ``LGR-9a35b2d-2``'s defect literally — same mechanism, not merely the same
# consequence class.** That record's subject is a ``FilterExpression`` whose page read
# is bounded ahead of it while ``LastEvaluatedKey`` is discarded, and it *exempted*
# this file's executions probe on the explicit ground that the probe carries "no
# ``FilterExpression`` for a ``Limit`` to be applied ahead of". This query has one. So
# unlike R2.4 — which is the same *consequence* under a different mechanism, ``Limit``
# ahead of an application-side filter — C6 is the record's own defect, in the same
# shape it measured (``mapping_count: 0`` for a pipeline whose row carried 7).
#
# ``LGR-eee8ac8-3`` reached the same conclusion independently on ``schema_compare``'s
# reads of **this same index**: an all-filtered page "returns empty with a
# ``LastEvaluatedKey``", which is why that read paginates under a bounded
# ``ExclusiveStartKey`` loop. Its scope is ``schema_compare/**``, so it is reasoning
# precedent here rather than a fence on this file — but it is precedent about
# ``PipelineIndex`` specifically, which is as close as precedent gets.
#
# **Truncation produces a MISSING version, never a wrong one, and that is a property
# of the read rather than a hope.** ``PipelineIndex`` is ``(pipeline_id, timestamp)``
# (``data-stack.ts:51-56``), the walk reads it ascending, and a version number is the
# ordinal of a hash's **first appearance**. A truncated ascending read is a *prefix* of
# the partition, and the first-appearance ordinal of any hash inside a prefix equals
# its ordinal over the whole partition — so every number the short map contains is the
# number the complete map would have contained. What a truncated map loses is entries:
# a hash whose first appearance sat past the cut is absent, and the guard below leaves
# that row with no ``schema_version`` at all.
#
# Two consequences follow, and they pull in opposite directions:
#
# * the numbers are never wrong, so this is a completeness defect and not a
#   correctness one — no operator is shown version 3 for what is really version 7;
# * but the loss is skewed onto exactly the rows being looked at. The map is built
#   **oldest-first** while the page is served **newest-first**, so the hashes a
#   truncated map drops are the newest ones, which are the hashes carried by the runs
#   on page 1.
#
# So the honest report is "the map may be short", which is what the marker below says.
#
# **The skew above is why the walk now takes the caller's wanted set** (see
# ``_schema_hash_versions``' ``wanted_hashes``). Reading the whole partition to number
# hashes nobody asked about is what made the second bullet bite: the bound was spent
# on the oldest history while the rows on screen carried the newest hashes. Stopping
# once every hash in hand has been resolved does not change a single ordinal — a
# version is fixed at first appearance — and it turns the common case (one schema,
# first appearance on page 1) from a full-partition walk into one page read.


#: Page cap on the schema-version walk. 100, the same figure and the same reasoning as
#: :data:`_MAX_COUNT_PAGES` and :data:`_MAX_FILTER_PAGES`. It is also the hard backstop
#: against ``LGR-eee8ac8-3``'s shape — an all-filtered page returning no rows while
#: still carrying a ``LastEvaluatedKey``, which is the case a loop keyed on "did this
#: page yield anything" would spin on.
_MAX_SCHEMA_VERSION_PAGES = 100

#: The instant the schema-version walk must stop by, as an offset from the **same**
#: ``started_at`` anchor :data:`_FILTER_WALK_TIME_SHARE_SECONDS` and
#: :func:`_runs_read_deadline` use. So the three figures are positions on one timeline
#: — 8.0, this, then ``20.0 - 2.0`` — rather than three independent durations, and this
#: walk's own worst-case share is this figure **minus** the filter walk's 8.0.
#:
#: **This read needs a carve-out for task 4.3's reason, not task 3.3's.** 3.3 declined
#: one because the count "is the **last** read in the function, so it is the one that
#: yields". This read is third of four and has two reads behind it: the cost /
#: ``load_outcome`` enrichment, which has **no deadline of its own at all** and so
#: cannot yield, and the count. A schema walk given the count's 18 s could therefore
#: spend the whole window and still leave the cost enrichment to issue its
#: ``batch_get_item`` afterwards — past the Lambda's 25 s ceiling, which is a gateway
#: timeout carrying no body rather than a 200 carrying ``partial_counts``. That is the
#: outcome the budget exists to prevent.
#:
#: 12.0 splits the 10 s that remain after the filter walk's share and the 2 s reserve
#: between this walk and the count (6 s worst case). A test that needs this to bind
#: shortens it rather than asserting a wall-clock duration.
#:
#: **Updated when the lost-file read was inserted ahead of it.** There are now four
#: reads on this timeline — 8.0, then
#: :data:`_LOST_FILE_READ_TIME_SHARE_SECONDS`' 10.0, then this, then ``20.0 - 2.0`` —
#: so this walk's worst case is **2 s** rather than the 4 s an earlier revision of
#: this comment stated, and up to 12 s when the two reads ahead of it were cheap. The
#: ceiling is unchanged; only the share behind a maximal predecessor moved. That is
#: affordable because the same change set made this walk stop as soon as every hash
#: on the page has been resolved (:func:`_schema_hash_versions`' ``wanted_hashes``),
#: so the work it has to fit into shrank at the same time as its worst-case window.
_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS = 12.0

#: The envelope key the schema-version walk names when the map it built may be short,
#: emitted **between** :data:`_FILTER_DERIVED_KEY_ORDER` and
#: :data:`_COUNT_DERIVED_KEY_ORDER` so the list mirrors the order the reads run in and
#: stays deterministic and assertable with ``==``.
#:
#: **``schema_version`` is a per-row field and not a count, so naming it here is a
#: deliberate widening of what ``partial_counts`` covers.** The alternative was a ninth
#: pagination key, and that was rejected: task 4.2's
#: ``test_executions_envelope_contract.py`` pins the block at **exactly eight** keys on
#: both arms against ``ExecutionsPagination``'s own declaration, and tasks 4.1 and 5.1
#: were both built against those eight. Extending a list that is already "always
#: present, ``[]`` when nothing is short" costs no key and no interface change.
#:
#: *Rejected: reporting nothing.* The existing degrade path already yields "no version"
#: for an absent hash, so an incomplete map is **indistinguishable** from a run whose
#: schema was never extracted — an unmarked "could-not-check" rendered as "checked and
#: found none", which is precisely what ``LGR-b2f79ef-14``'s reasoning forbids and what
#: ``LGR-9a35b2d-3`` applied when it brought ``stats.py``'s swallowed approvals failure
#: under its marker rather than marking one neighbour honestly.
#:
#: Named on **both** failure modes — a bound that stopped the walk, and a read that
#: raised — because a client cannot tell them apart from the response and the meaning
#: is the same: the version numbers present are right, and some may be missing.
_SCHEMA_VERSION_DERIVED_KEY_ORDER: tuple[str, ...] = ("schema_version",)


def _runs_read_deadline(started_at: float) -> float:
    """The monotonic instant the count walk must be finished by (p48 R2.5).

    Derived **at call time from the request's remaining budget** rather than being
    a fixed duration — ``LGR-14d6dd3-2`` as applied by ``LGR-9a35b2d-3``, which
    rejected a count cap on the ground that it "leaves wall clock a function of
    per-read latency; a throttled S3 turns a capped read count into a timeout
    anyway". Same argument, DynamoDB, same 25 s ceiling.

    ``started_at`` is anchored at the **top** of ``get_pipeline_executions``, before
    the cursor decode, so the window left for the count is
    ``budget - reserve - elapsed`` and shrinks when the work ahead of it was slow.
    That direction is deliberate and it is why no carve-out like
    ``pipelines_crud.py:_PAGE_SCAN_TIME_SHARE_SECONDS`` is needed here: the count is
    the **last** read in the function, so it is the one that yields, and the thing
    it yields to is the cost / ``load_outcome`` enrichment. A count degraded to a
    floor and named in ``partial_counts`` is a far better outcome than an enrichment
    read starved into ``actual_cost: None`` and ``not_established`` on every row.

    The budget comes from this module's two constants against the function's Lambda
    timeout, not from ``context.get_remaining_time_in_millis()``: ``router.py``
    calls this with path and query-string arguments only, and threading a Lambda
    context through would change every route's signature for a figure already known
    statically. ``stats.py:_read_deadline``, ``pipelines_crud.py:_list_read_deadline``
    and ``schemas.py:_label_pass_deadline`` all derive theirs the same way.

    A budget smaller than the reserve clamps to the anchor rather than going
    negative, so a misconfiguration degrades to "read one page and mark it".
    """
    return started_at + max(0.0, _RUNS_TIME_BUDGET_SECONDS - _RUNS_READ_RESERVE_SECONDS)


def _filter_walk_deadline(started_at: float) -> float:
    """The monotonic instant the status filter walk must stop by (p48 R4.3, task 4.3).

    Earlier than :func:`_runs_read_deadline`'s instant by construction, and that gap is
    the point — see :data:`_FILTER_WALK_TIME_SHARE_SECONDS`. Derived from the **same**
    ``started_at`` anchor as the count's deadline, so the two are shares of one budget
    rather than two independent timers; that is
    ``pipelines_crud.py:_page_scan_deadline``'s shape, which solves the same problem
    (a read that runs ahead of the enrichments and could otherwise spend everything).

    Clamped so a share larger than the whole budget cannot put the filter walk's
    deadline beyond the count's, which would invert the carve-out silently.
    """
    return started_at + max(0.0, min(_FILTER_WALK_TIME_SHARE_SECONDS, _RUNS_TIME_BUDGET_SECONDS))


def _schema_version_walk_deadline(started_at: float) -> float:
    """The monotonic instant the schema-version walk must stop by (p48 R6.1, task 6.1).

    Between :func:`_filter_walk_deadline`'s instant and :func:`_runs_read_deadline`'s,
    from the **same** ``started_at`` anchor, so all three are shares of one budget
    rather than independent timers — see
    :data:`_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS`.

    Clamped at **both** ends, and each clamp answers a different misconfiguration:

    * not later than the count's deadline, so a share larger than the budget cannot
      push this walk past the read that is supposed to yield to nothing;
    * not earlier than the filter walk's, because the filter walk may legitimately
      consume its whole share — and a schema deadline behind that instant would mean
      the walk reads its first page and always stops, silently reinstating the
      unpaginated behaviour C6 exists to remove.
    """
    ceiling = _RUNS_TIME_BUDGET_SECONDS - _RUNS_READ_RESERVE_SECONDS
    share = min(max(_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS, _FILTER_WALK_TIME_SHARE_SECONDS), ceiling)
    return started_at + max(0.0, share)


def _schema_hash_versions(
    pipeline_id: str, deadline: float, wanted_hashes: frozenset[str] | None = None
) -> tuple[dict[str, int], bool]:
    """Build the pipeline's hash → version map from ``PipelineIndex`` (p48 C6).

    Version numbers are ordinals of **first appearance** in ``timestamp`` ascending
    order, which is what makes them stable across pages of run history: the map is a
    property of the partition and not of which page is being served.

    ``wanted_hashes`` is the set of hashes the caller actually has to resolve, and it
    makes the walk **stop as soon as every one of them has been seen**. Both callers
    know their set before they call: the run list has the page's rows in hand, and
    ``get_run_details`` wants exactly one ordinal. ``None`` means "resolve everything",
    which is the pre-existing behaviour and reads the whole partition.

    **The numbers are unchanged by this, and that is a property rather than a hope.**
    A version is fixed at a hash's first appearance, so nothing later in an ascending
    read can alter one that has already been assigned. Every hash is still counted as
    it is met — the wanted set decides only when to *stop*, never what to number — so
    the ordinals a stopped walk produces are the ordinals the whole-partition walk
    would have produced. A short map still loses entries and never renumbers them.

    **What ``complete`` means, stated because the wanted set narrows it.** It is
    "no hash the caller asked about went unresolved because a bound stopped the walk".
    Unfiltered that is the same statement as "the whole partition was read", which is
    what it meant before. With a wanted set it is the same statement about the set
    that matters, so the client-visible contract is untouched: ``partial_counts``
    names ``schema_version`` exactly when some row's absent version might be a
    could-not-check rather than a genuine absence. The change can only remove false
    positives — a run whose hash resolved on page 1 of a 40-page partition is no
    longer reported as possibly-missing. A hash that is genuinely absent from the
    partition still runs the walk to the end and still reports ``True``: never
    finding it is the honest answer, not a bound.

    **Why this and not a cache.** An in-process cache of a complete map
    (``pipelines_logs``' ``_get_cached``/``_set_cache`` is the in-tree mechanism) was
    rejected: a schema version minted inside the TTL window would be missing from the
    cached map while ``complete`` still claimed the map whole — an unmarked
    could-not-check reported as checked-and-found-none, which is exactly what
    ``LGR-b2f79ef-14``'s reasoning forbids and what the marker on this walk exists to
    prevent. Stopping early cannot go stale.

    Following ``LastEvaluatedKey`` is the whole point (``LGR-9a35b2d-2``). The
    ``FilterExpression`` on ``step_name`` is applied by DynamoDB *after* the 1 MB page
    read, so a page can come back with no matching rows and a continuation token —
    ``LGR-eee8ac8-3``'s case, on this same index — and a single-page read reports "no
    such hash" for a hash that exists further in. The 1 MB is spent on *scanned* bytes
    rather than returned ones, and ``PipelineIndex`` projects ALL, so an index entry
    carries the whole state row including ``extracted_schema``. A ``schema_extract``
    partition therefore crosses a page boundary at a low number of rows, which is why
    this is a live defect and not a theoretical one.

    ``ProjectionExpression="schema_hash"`` is kept: it does not reduce the bytes the
    1 MB limit is measured against, but it keeps them off the wire and out of memory,
    and ``LastEvaluatedKey`` is returned regardless of what the projection names. It is
    NOT paired with ``Select``, which real DynamoDB rejects even though moto accepts it
    (``LGR-9a35b2d-3``).

    Returns:
        tuple: ``(versions, complete)``. ``complete`` is ``False`` when the page cap or
        the deadline stopped the walk with a wanted hash still unresolved, in which
        case the map may be **short of something the caller needs** — every number in
        it is still correct (see the module comment above
        :data:`_MAX_SCHEMA_VERSION_PAGES`). The caller then names
        ``_SCHEMA_VERSION_DERIVED_KEY_ORDER`` in ``partial_counts``.
    """
    query_kwargs: dict[str, Any] = {
        "IndexName": "PipelineIndex",
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        "FilterExpression": Attr("step_name").eq("schema_extract"),
        "ProjectionExpression": "schema_hash",
        "ScanIndexForward": True,  # oldest first for stable version numbering
    }
    # An empty wanted set is satisfied before the first read and must NOT be
    # confused with ``None`` (resolve everything). ``get_pipeline_executions`` reaches
    # here with an empty set whenever no row on the page carries a ``schema_hash`` —
    # a page of skips, say — and for that page the honest answer is an empty map
    # reported COMPLETE, because nothing was asked and nothing is missing.
    unresolved: set[str] | None = set(wanted_hashes) if wanted_hashes is not None else None
    if unresolved is not None and not unresolved:
        return {}, True

    versions: dict[str, int] = {}
    for _page in range(_MAX_SCHEMA_VERSION_PAGES):
        response = state_table.query(**query_kwargs)
        for item in response.get("Items", []):
            schema_hash = item_str(item, "schema_hash")
            if schema_hash and schema_hash not in versions:
                # Counted before the stop check, always: the ordinal of every hash
                # met on this page is part of the numbering whether the caller asked
                # about it or not. Discounting an unwanted hash would renumber the
                # wanted ones, which is the one thing this function may not do.
                versions[schema_hash] = len(versions) + 1
                if unresolved is not None:
                    unresolved.discard(schema_hash)
        if unresolved is not None and not unresolved:
            # Every hash the caller asked about has a final ordinal. Continuing would
            # read more of the partition and change none of them.
            return versions, True
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return versions, True
        # Checked AFTER establishing there is more to read, so the first page is always
        # read and the bound applies to *continuing* — the same property
        # ``_count_partition_rows`` and the filter walk hold, and the reason an empty
        # map is never reported as partial when the partition is genuinely empty.
        if time.monotonic() >= deadline:
            return versions, False
        query_kwargs["ExclusiveStartKey"] = last_key
    return versions, False


#: The stored ``status`` of a Queued_Placeholder / Lost_File row — the row shape both
#: ``pipeline_trigger._track_queued_file`` and ``queue_loss_recorder`` write, and the
#: one value that must NOT be a run.
#:
#: Spelled once because two readers have to **agree** rather than merely coexist:
#: :func:`_split_derive_and_filter` moves rows carrying it out of ``executions``, and
#: :func:`_count_partition_rows` excludes them from ``total_count``. A drift between
#: those two is not a degraded render — it is a total that cannot be reconciled with
#: the rows on screen, which is the defect that count exists to avoid.
#:
#: Deliberately NOT the same thing as the ``"QUEUED"`` refused by ``?status=``: that
#: one is a query-string value in :data:`_ACCEPTED_STATUS_FILTERS`' vocabulary, and it
#: is refused for a reason about array shapes (:data:`_QUEUED_STATUS_FILTER_MESSAGE`)
#: rather than about what a row stores. Same letters, different subject; keep them
#: apart.
_QUEUED_ROW_STATUS = "QUEUED"


def _count_partition_rows(pipeline_id: str, deadline: float) -> tuple[int, bool]:
    """Count one pipeline's ``RUNS_BY_TIME_INDEX`` rows that are candidates to render.

    **What this number counts, stated plainly, because R2.2 is that it must
    describe something real.** It is the count of index rows in the partition
    **excluding Queued_Placeholder rows** — that is, the rows that can reach the
    ``executions`` array. It still *includes* Phantom_Execution_Rows (which the
    phantom filter drops in Python and no server-side predicate can express), and
    still *excludes* rows carrying no ``started_at``, which are absent from a sparse
    index rather than filtered out of it (``LGR-7d19eca-2``, do-NOT 2 / R9.4 — the
    exclusion below is a test on a stored attribute, never on index membership).

    **The ``QUEUED`` exclusion is p54's 30-day Lost_File record, corrected.** An
    earlier revision of this function counted raw rows and argued the inclusion
    deliberately, on the ground that a ``QUEUED`` row occupies a page slot and so
    belongs in a number describing the page walk's cost. That argument was sound
    while a placeholder lived about an hour and was deleted on processing. The loss
    recorder writes a row of the same shape with a **30-day** TTL, so a pipeline that
    loses files accumulates page capacity that never renders and never expires — a
    total that grows away from anything an operator can see. A number nobody can
    reconcile with the screen is the class of quiet wrongness this file exists to
    remove, so the count now describes **the set being paged** rather than the reads
    it costs.

    **What that gives up, and why the trade is safe here.** ``total_pages`` is no
    longer the exact number of page reads a full walk costs: a partition whose rows
    are one third placeholders needs more page reads than
    ``ceil(total_count / page_size)``. The earlier revision called under-stating a
    *correctness* failure because "a pager that under-states hides pages that exist,
    making rows unreachable" — and that is true of a **numeric-offset** pager. This
    pager is keyed: ``has_next`` and ``next_cursor`` are minted from the page query's
    own ``LastEvaluatedKey`` and are independent of this walk, which is exactly what
    ``_COUNT_DERIVED_KEY_ORDER`` records ("a truncated count cannot strand a
    reachable page"), and ``CursorPagination`` navigates on ``hasNext``/``hasPrev``
    while rendering ``totalPages`` as a label. So no row becomes unreachable; the
    figure is a description of the set, and the set is what an operator counts.

    The phantom over-statement is left in place and the direction of that error is
    unchanged: it can still show a page that comes back short, which is a display
    imprecision on a correctly-ordered page. It is also transient and rare, where the
    placeholder over-statement was permanent and monotonic — which is the whole
    difference between the two.

    A ``Select="COUNT"`` Query returns ``Count`` and no ``Items``, so neither the
    split nor ``is_phantom_execution_row`` — a Python predicate over
    ``current_step`` and ``steps_status`` (``handlers/common.py``) — can be applied
    to it. ``status`` is a stored attribute, so *that* predicate is expressible
    server-side, and ``Count`` on a filtered COUNT Query is the post-filter figure.
    The filter is written as ``attribute_not_exists(status) OR status <> "QUEUED"``
    rather than a bare ``<>``: DynamoDB's comparison operators are false for a path
    that does not exist, so a bare ``<>`` would silently drop any row carrying no
    ``status`` at all — under-counting rows that do occupy page slots. The predicate
    that has to be exact here is "is a placeholder", not "is not a placeholder".

    Deliberately **windowless** — no ``Limit`` — and deliberately not combined with
    a ``ProjectionExpression``: real DynamoDB rejects that pairing even though moto
    accepts it (``LGR-9a35b2d-3``), and a COUNT needs none. Note what ``Select``
    does and does not buy, from the same record: it does **not** reduce read
    capacity, because DynamoDB reads the items to count them — and the
    ``FilterExpression`` does not either, for the same reason. Neither is a cost fix;
    both are about what the number means. The saving is that no item crosses the wire
    and nothing accumulates in memory, which is what makes a 100-page cap affordable
    here, and it is also why the walk must keep following ``LastEvaluatedKey``: a
    page whose rows are all dropped by the filter still returns a continuation token
    (``LGR-eee8ac8-3``'s shape).

    Returns:
        tuple: ``(count, complete)``. ``complete`` is ``False`` when the page cap or
        the deadline stopped the walk, in which case ``count`` is a **floor** and the
        caller must name ``_COUNT_DERIVED_KEY_ORDER`` in ``partial_counts``.
    """
    query_kwargs: dict[str, Any] = {
        "IndexName": RUNS_BY_TIME_INDEX,
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        "FilterExpression": Attr("status").not_exists() | Attr("status").ne(_QUEUED_ROW_STATUS),
        "Select": "COUNT",
    }

    count = 0
    for _page in range(_MAX_COUNT_PAGES):
        response = executions_table.query(**query_kwargs)
        # ``response["Count"]`` and not ``.get("Count", 0)``: a response with no
        # ``Count`` is not a COUNT response, and defaulting it to 0 would invent a
        # number. The ``KeyError`` lands in the caller's degrade arm, which marks
        # the count as a floor — the honest answer. Matches ``stats.py``.
        count += int(response["Count"])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return count, True
        # Checked AFTER establishing there is more to read, so the first page is
        # always read and the bound applies to *continuing*. A count of zero marked
        # partial says nothing useful.
        if time.monotonic() >= deadline:
            return count, False
        query_kwargs["ExclusiveStartKey"] = last_key
    return count, False


def _optional_row_count(row: Mapping[str, Any], key: str) -> int | None:
    """Read one row count off a ``load_metrics`` row as an ``int``, or ``None``.

    ``None`` means "this fact is not available in a usable form", which
    ``derive_load_outcome`` reports as ``not_established`` naming the key
    (p37 R1.6). It is deliberately **not** 0: 0 is a meaningful count in that
    vocabulary — it is what ``empty_source`` and ``no_rows_loaded`` are made of —
    so defaulting an absent count to 0 would manufacture a verdict.

    The conversion is mandatory, not cosmetic. ``batch_get_item`` returns the
    row's ints as ``Decimal``, and ``load_outcome._usable_count`` requires a real
    ``int`` — it rejects ``Decimal`` — so passing the raw attribute through would
    turn every seeded run into ``not_established``. ``item_int`` is the house
    converter for exactly this (``shared/dynamo_typing.py``, P22 Req 2): it
    coerces ``Decimal`` and rejects ``bool``, and it never raises.

    ``item_int`` answers "absent" and "0" with the same value, so it is asked
    twice with two different defaults; the two answers agree only when a value
    was actually read. That tells absence apart from a real 0 without reserving
    a sentinel out of the count domain.
    """
    as_zero = item_int(row, key, default=0)
    as_one = item_int(row, key, default=1)
    return as_zero if as_zero == as_one else None


def _load_outcome_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the Load_Outcome from a ``load_metrics`` row that was read.

    Provenance is the row's own state, in the three-value shape
    ``derive_quality_verdict`` established: both counts usable ⇒
    ``load_metrics_row``; the row is present but a count is absent or unusable ⇒
    ``load_metrics_row_incomplete``. The distinction is the point — a row that
    exists and says nothing must not read the same as no row at all
    (``LGR-b2f79ef-8``, **template / ratified**, cited as reasoning precedent
    rather than direct scope — its own subject is
    ``test_code.get_pipeline_metadata``. This read ``LGR-78ce0b7-8`` until the
    ledger recompile: the two commits carry the same decision set and only the
    ``b2f79ef`` twin resolves, so grep the twin, not the dead hash).
    """
    rows_read = _optional_row_count(row, "rows_read")
    rows_written = _optional_row_count(row, "rows_written")
    # Annotated because the flat Layer_Import_Fallback spelling ("load_outcome",
    # not "shared.load_outcome") is unresolvable to mypy under
    # ignore_missing_imports, so the deriver's return arrives as Any and
    # warn_return_any would flag the return.
    outcome: dict[str, Any] = derive_load_outcome(
        rows_read=rows_read,
        rows_written=rows_written,
        determined_from=(
            FROM_LOAD_METRICS_ROW
            if rows_read is not None and rows_written is not None
            else FROM_LOAD_METRICS_ROW_INCOMPLETE
        ),
    )
    return outcome


def _load_outcome_absent() -> dict[str, Any]:
    """The Load_Outcome for a run with no ``load_metrics`` row (p37 R1.6).

    Two cases reach this, and neither is an error: a run that completed before
    the counts were recorded at all, and a run whose best-effort counts write
    failed. Both are ``not_established`` — the honest answer, and a reachable
    steady state rather than a legacy one (design risk 3). Also the arm used
    when the enrichment read itself fails, mirroring how ``actual_cost``
    degrades to ``None`` for every row.
    """
    outcome: dict[str, Any] = derive_load_outcome(
        rows_read=None,
        rows_written=None,
        determined_from=FROM_LOAD_METRICS_ROW_ABSENT,
    )
    return outcome


def _load_outcome_lower_bound_from_dq_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Derive a lower-bound Load_Outcome from a DQ_Report_Row (p37 C5, R1.6).

    Returns ``None`` when the row establishes nothing, so a caller can fall
    through to :func:`_load_outcome_absent` rather than replace one
    ``not_established`` with another that reads as if a source had been consulted.

    **One combination establishes an outcome, and it is not a guess.**
    ``rows_passed == 0`` with ``rows_quarantined > 0``: the quarantined rows came
    from the source, so Rows_Read > 0 **follows**, and ``rows_passed`` **is**
    Rows_Written for the inline-DQ case (``glue_job_s3_tables.py.j2:307`` writes
    ``transformed_count`` under that name). That yields ``no_rows_loaded`` —
    exactly the defect this spec exists for, on the one template that could
    produce it before C1 shipped.

    **Rows_Read is not recoverable from this row, so ``complete_load`` is
    unreachable.** Both the transform filter and the quarantine reduce the frame
    and neither pre-transform count is written, so R1.4 and R1.5 cannot be told
    apart here. The quarantine count is passed as a **lower bound** and
    ``derive_load_outcome`` guards the arms structurally under
    ``FROM_DQ_REPORT_ROW_LOWER_BOUND``; this function does not rely on its own
    pre-filter to keep that promise.

    Both counts go through :func:`_optional_row_count` for the reason recorded
    there: ``batch_get_item`` returns the row's ints as ``Decimal`` and
    ``load_outcome._usable_count`` rejects those, so the conversion is what makes
    the lower bound reachable at all. A DQ row written by the **on-demand**
    data quality agent rather than the Glue job carries neither field
    (``agentcore/data_quality_agent/main.py`` writes ``overall_status`` and
    ``checks`` only), so it reads as establishing nothing — which is correct: it
    describes a check, not a load.
    """
    rows_written = _optional_row_count(row, "rows_passed")
    rows_quarantined = _optional_row_count(row, "rows_quarantined")
    if rows_written != 0 or rows_quarantined is None or rows_quarantined <= 0:
        return None
    outcome: dict[str, Any] = derive_load_outcome(
        rows_read=rows_quarantined,
        rows_written=rows_written,
        determined_from=FROM_DQ_REPORT_ROW_LOWER_BOUND,
    )
    return outcome


def _prefer_established(*candidates: dict[str, Any] | None) -> dict[str, Any]:
    """First candidate that established an outcome; else the first that exists.

    The ``load_metrics`` row is always offered first: it is the recorded source,
    and the DQ_Report_Row's lower bound is a fallback for runs that have no such
    row (p37 C5). A ``load_metrics`` row that exists but carries no usable counts
    establishes nothing, so the lower bound is allowed to answer for it — but its
    ``load_metrics_row_incomplete`` provenance is still preferred over
    ``load_metrics_row_absent`` when neither establishes anything, because "the
    row was there and said nothing" and "there was no row" must not collapse
    (``LGR-b2f79ef-8``, **template / ratified**).
    """
    for candidate in candidates:
        if candidate is not None and candidate.get("outcome") != OUTCOME_NOT_ESTABLISHED:
            return candidate
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return _load_outcome_absent()


#: The stored attribute that carries a Lost_File's outcome, and the value that means
#: lost (p54 R9.3, design §4.8 change 1 candidate 1).
#:
#: **Read through :func:`_is_lost_queued_row` and nowhere else** — by the
#: ``queued_files`` split and by :func:`_lost_files`, which is one predicate and two
#: call sites rather than two spellings. It rides on the Queued_Placeholder row
#: *beside* ``status``, which stays ``"QUEUED"`` — that is the whole point, and it is
#: ``LGR-21a1b5b-2`` (**template / ratified**) that forces it. Its reasoning: the
#: executions table's DynamoDB Stream drives the WebSocket broadcast Lambda, its
#: ``StatusIndex`` GSI is partitioned on ``status``, and *"the run-history read path
#: treats every non-``QUEUED`` row as a run and pages it with a server-side Limit"*,
#: so a non-run row *"would render as a phantom run unless three separate readers
#: learned to ignore it."* A fourth executions ``status`` is therefore refused
#: (p54 do-NOT 19); the lost fact is a separate attribute instead, and the split, the
#: phantom filter, the GSI, the Stream and the run-history reader all behave exactly
#: as they did before p54.
#:
#: **Its producer is ``agentic_data_ingestion_queue_loss_recorder``**, the Lambda on
#: ``PipelineTriggerDLQ``, which shipped in the same commit as this read. An earlier
#: revision of this comment said the attribute had no writer and that "the read below
#: is inert"; that was true only between two tasks of one spec and is false now. Both
#: spellings of the attribute and its value are asserted to agree by
#: ``tests/unit/lambdas/queue_loss_recorder/test_queue_loss_record.py``, which reads
#: this module's source — the two Lambda packages cannot import each other.
#:
#: **It writes a whole row with ``put_item``, and the ordering that made an
#: annotation look safe is reversed** (``LGR-e5e9edd-3``, **template / ratified**).
#: An earlier revision of this comment described ~25 min of Redelivery_Budget against
#: a 60-min ``QUEUED_RECORD_TTL_SECONDS``, leaving "~35 min of slack for the row to
#: still be there when the DLQ handler rewrites it". ``maxReceiveCount`` is now 1000,
#: so the budget is **8 h 20 min against a 1 h TTL**: by the time a message reaches
#: the DLQ the placeholder has normally expired, and an ``update_item`` against a
#: missing item CREATES it — minting a row with no ``status`` and no ``started_at``,
#: which invariant ``LGR-7d19eca-2`` makes unindexable by ``RunsByTimeIndex`` and
#: therefore invisible with no error. The recorder's IAM role is granted ``PutItem``
#: only, so that mistake is unmakeable rather than merely discouraged.
#:
#: The record's own lifetime is ``config.LOST_FILE_RECORD_TTL_SECONDS`` — a **fourth**
#: TTL constant, deliberately not coupled to ``QUEUED_RECORD_TTL_SECONDS`` (now
#: shorter than the redelivery budget), ``SKIPPED_RUN_RECORD_TTL_SECONDS`` or
#: ``PROCESSED_FILE_LEDGER_TTL_SECONDS`` (R8.13, do-NOT 16).
#:
#: **That 30-day lifetime is why one reader of this attribute is not enough.** The
#: row's ``started_at`` is the instant the loss was *recorded*, so it enters the
#: newest-first index at the top and is pushed off page 1 by newer runs within about
#: an hour, while the record itself lives 30 days. The split below therefore publishes
#: the loss only while the row happens to sit on page 1; :func:`_lost_files` is the
#: read that makes it durable, and it is keyed rather than page-scoped.
_QUEUE_OUTCOME_ATTRIBUTE = "queue_outcome"
_QUEUE_OUTCOME_LOST = "lost_to_dlq"

#: The two values the published ``status`` of a ``queued_files`` entry can take, and
#: the only two — ``ExecutionsPage.tsx:576`` is a binary ternary over
#: ``item.status === 'failed_dlq'``, so a third literal would render as "Queued"
#: regardless. Publishing exactly two keeps the producer honest about what the reader
#: can express.
#:
#: Absence of :data:`_QUEUE_OUTCOME_ATTRIBUTE` means ``"queued"`` — and that is
#: absence carrying meaning, not absence collapsed into a value (do-NOT 7): a file
#: with no recorded queue outcome *is* a Deferred_File, because the only thing that
#: writes an outcome is the loss path.
#:
#: An **unrecognised** stored value also publishes ``"queued"``, deliberately, and the
#: asymmetry is the point: only one producer writes this attribute, so a value it did
#: not write is not evidence of loss, and claiming loss on it would be the invented
#: fact R9.5's three-state discipline exists to prevent. It is not normalised or
#: case-folded either (``LGR-ad78946-1``: *"normalising removes the only signal that
#: the deployed value is not the intended one"*), and it is not logged — this is a
#: per-row read on a request path that already carves its 25 s budget five ways.
_QUEUED_STATUS_DEFERRED = "queued"
_QUEUED_STATUS_LOST = "failed_dlq"


def _is_lost_queued_row(row: Mapping[str, Any]) -> bool:
    """Does this executions row record a Lost_File?

    **The one spelling of "this row is lost", read by both consumers** — the
    ``queued_files`` split's step-1 comprehension and :func:`_lost_files`. Two
    inline copies of ``row.get(attr) == value`` would be the mirrored definition
    ``LGR-9e76249-9`` records as a source of drift, and the drift here is not a
    degraded render: the two surfaces would disagree about whether a file is gone.

    Exact match, not normalised and not case-folded, for the reason
    :data:`_QUEUED_STATUS_DEFERRED` states — ``LGR-ad78946-1``'s *"normalising
    removes the only signal that the deployed value is not the intended one"*. An
    unrecognised value answers ``False``: only one producer writes this attribute
    (``queue_loss_recorder``), so a value it did not write is not evidence of loss,
    and claiming loss on it would be an invented fact.
    """
    return row.get(_QUEUE_OUTCOME_ATTRIBUTE) == _QUEUE_OUTCOME_LOST


#: The ``run_id`` prefix every Queued_Placeholder and every Lost_File record carries.
#: Content-addressed as ``queued-{sha256(source_key)[:16]}`` by BOTH writers —
#: ``pipeline_trigger._track_queued_file`` (``:676-694``) and
#: ``queue_loss_recorder.queued_row_key`` — which is what makes it usable as a **key
#: condition** rather than as a filter. It is one of the six ``run_id`` prefixes the
#: p48 C1 comment enumerates, and it is exactly the placeholder namespace: no other
#: writer mints an id under it.
_QUEUED_ROW_ID_PREFIX = "queued-"

#: Page cap on the lost-file read. 100, the same figure and the same reasoning as
#: :data:`_MAX_COUNT_PAGES`, :data:`_MAX_FILTER_PAGES` and
#: :data:`_MAX_SCHEMA_VERSION_PAGES` — and it is the hard backstop against the shape
#: ``LGR-eee8ac8-3`` names, a page whose rows are all dropped by the
#: ``FilterExpression`` while a ``LastEvaluatedKey`` still comes back.
_MAX_LOST_FILE_PAGES = 100

#: The lost-file read's **ceiling** on the same one-budget timeline the other three
#: reads sit on — 8.0 (filter walk), this, 12.0 (schema walk), then ``20.0 - 2.0``
#: (count) — all measured from the SAME ``request_started`` anchor. A second time
#: budget would be a second authority for one figure, which ``LGR-9e76249-2`` and
#: ``LGR-28b4d75-1`` reject for derivable numbers.
#:
#: **10.0, and it is positioned rather than sized.** This read runs second, so its
#: worst case is 10.0 minus whatever the filter walk spent — 2.0 s after a maximal
#: filter walk, the whole 10.0 s when there was no filter at all. That is a ceiling
#: this read should never approach: it is a **keyed** Query (partition key equality
#: plus ``begins_with`` on the sort key), so DynamoDB reads only the placeholder
#: namespace rather than scanning the partition, and a pipeline holds single-digit
#: numbers of those. The bound exists because "cheap by construction" is an argument
#: about the expected case and a deadline is a property that holds in every case —
#: which is exactly ``LGR-9a35b2d-3``'s ground for rejecting a count cap alone, that
#: it "leaves wall clock a function of per-read latency".
#:
#: The cost of inserting it here is the schema walk's worst-case share, which drops
#: from 4.0 s to 2.0 s (its 12.0 s ceiling is unchanged). That is coherent with the
#: same change set rather than at odds with it: :func:`_schema_hash_versions` now
#: stops as soon as every hash it was asked about has been resolved, so the walk it
#: has to fit into is shorter than it was, not longer.
_LOST_FILE_READ_TIME_SHARE_SECONDS = 10.0

#: The envelope key the lost-file read names when a bound stopped it or the read
#: raised, emitted **after** :data:`_FILTER_DERIVED_KEY_ORDER` and **before**
#: :data:`_SCHEMA_VERSION_DERIVED_KEY_ORDER` so ``partial_counts`` mirrors the order
#: the reads run in and stays deterministic and assertable with ``==``.
#:
#: Named on both failure modes for :data:`_SCHEMA_VERSION_DERIVED_KEY_ORDER`'s
#: reason: a client cannot tell a bound from a raise, and the meaning is the same —
#: the lost files listed are real, and there may be more. An unmarked short list
#: would report "I could not look" as "I looked and found none", which is the defect
#: ``LGR-b2f79ef-14``'s reasoning forbids.
#:
#: Like ``schema_version`` this names something other than a count, which
#: :data:`_SCHEMA_VERSION_DERIVED_KEY_ORDER` already established as a widening of
#: this list rather than a new pagination key — the ``pagination`` block stays at
#: exactly eight keys, which ``test_executions_envelope_contract.py`` pins.
_LOST_FILES_DERIVED_KEY_ORDER: tuple[str, ...] = ("lost_files",)


def _lost_file_read_deadline(started_at: float) -> float:
    """The monotonic instant the lost-file read must stop by (p54 R10.3).

    Same anchor and same clamping discipline as :func:`_filter_walk_deadline` and
    :func:`_schema_version_walk_deadline`: derived from the request's own
    ``started_at`` so all four reads are shares of one budget, and clamped at both
    ends so a misconfiguration degrades rather than inverting the carve-out.

    * not later than the count's ceiling (``budget - reserve``), so a share larger
      than the budget cannot push this read past the read that yields to nothing;
    * not earlier than the filter walk's share, because the filter walk may
      legitimately consume all of its own — and a deadline behind that instant
      would mean this read fetches its first page and always stops, which for a
      pipeline whose losses span more than one page would silently reinstate the
      truncation this bound is supposed to report.
    """
    ceiling = _RUNS_TIME_BUDGET_SECONDS - _RUNS_READ_RESERVE_SECONDS
    share = min(max(_LOST_FILE_READ_TIME_SHARE_SECONDS, _FILTER_WALK_TIME_SHARE_SECONDS), ceiling)
    return started_at + max(0.0, share)


def _lost_files(pipeline_id: str, deadline: float) -> tuple[list[dict[str, Any]], bool]:
    """Every Lost_File recorded for one pipeline, independent of run-history paging.

    **Why this is a second read rather than more of ``queued_files`` (p54 R10.3).**
    ``queued_files`` comes from the FIRST page of ``RUNS_BY_TIME_INDEX`` only, and
    that scoping is deliberate — accumulating it across a filter walk "would make
    the array a function of how far the filter had to search", so the same request
    with two different ``?status=`` values would return different queued files. But
    a Lost_File record lives ``LOST_FILE_RECORD_TTL_SECONDS`` (30 days) while its
    ``started_at`` is *the instant the loss was recorded*, so on an active pipeline
    it drops off page 1 within an hour and then exists, correctly, where nothing
    reads it. The condition that creates a lost file — a busy pipeline holding an
    approval lock — is the same condition that buries the evidence.

    So the loss gets **its own array**, and the shape follows the precedent one file
    over: ``PipelineOverviewPage.tsx`` records that page-1 scoping cost approvals
    their Approve/Reject buttons, and the fix there was a second keyed read rather
    than a wider first one.

    *Rejected: widening the ``queued_files`` entry, or accumulating it across
    pages.* Its four keys are pinned by set equality in
    ``test_split_derive_and_filter_order_characterization.py`` (p54 do-NOT 3), and
    the page-1 comment above must stay true. Both are fences with reasons, not
    incidental shapes.

    *Rejected: a fourth ``executions`` status.* ``LGR-21a1b5b-2`` /
    ``LGR-e5e9edd-2`` (**both template / ratified**) refuse one, because several
    readers switch exhaustively on the existing set. The row's own ``status`` stays
    ``"QUEUED"`` and the loss stays on :data:`_QUEUE_OUTCOME_ATTRIBUTE`; nothing
    here changes that. ``LGR-e5e9edd-2``'s ``revisit_when`` is *"a reader needs to
    filter lost files server-side, which an attribute outside the key cannot do
    efficiently"* — that condition has now fired, and the answer below is not a new
    key but the **key that already exists**: see :data:`_QUEUED_ROW_ID_PREFIX`.

    **The read: a keyed Query on the BASE table, not on the sparse index.**
    ``(pipeline_id, run_id)`` is the table's own key schema, and every placeholder
    and loss record is minted under :data:`_QUEUED_ROW_ID_PREFIX`, so
    ``begins_with`` on the sort key narrows the read to the placeholder namespace
    server-side. Two things follow, and both matter:

    * it is cheap in a way a ``FilterExpression`` over the whole partition is not —
      DynamoDB reads the matching range rather than reading everything and
      discarding;
    * it does **not** consult ``RUNS_BY_TIME_INDEX``, so this array cannot inherit
      that index's sparseness. Invariant ``LGR-7d19eca-2`` makes a row with no
      ``started_at`` silently absent from the index, and do-NOT 2 / R9.4 forbid
      treating that absence as a filter. Reading the base table means a loss record
      is visible here on the strength of its key alone.

    The ``FilterExpression`` on :data:`_QUEUE_OUTCOME_ATTRIBUTE` is a cost
    reduction, not the decision: it keeps deferred placeholders off the wire, and
    ``LastEvaluatedKey`` is returned regardless of what a filter dropped, which is
    why the loop below follows it. What decides whether an entry is published is
    :func:`_is_lost_queued_row` — the same predicate the ``queued_files`` split
    uses — so the answer does not depend on the filter having been applied.
    ``ProjectionExpression`` is kept for ``_schema_hash_versions``' reason (bytes
    off the wire and out of memory) and is deliberately NOT paired with ``Select``,
    which real DynamoDB rejects even though moto accepts it (``LGR-9a35b2d-3``).

    **The published entry is four keys and carries no ``run_id``** — deliberately,
    and this is do-NOT 3 / do-NOT 15 / p48 O2 honoured rather than worked around.
    The record does have a real ``run_id``, but ``get_run_details`` on it 404s (a
    placeholder row has no state rows at all), so publishing it would hand the
    frontend something that looks navigable and is not. ``source_key`` is unique
    per file within a pipeline — the row's key is derived from it — so a consumer
    has a stable identity for a list without being handed a dead link.

    ``lost_at`` falls back to ``started_at``: the recorder writes the same instant
    to both (``queue_loss_recorder.record_lost_file``), so the fallback cannot
    disagree with the primary, and it keeps the timestamp present for any row that
    carries the outcome attribute without the loss-specific one.

    Newest first, sorted here rather than by the query: the sort key is a content
    hash, so index order carries no time information at all.

    Returns:
        tuple: ``(entries, complete)``. ``complete`` is ``False`` when the page cap
        or the deadline stopped the walk, in which case the list is a **prefix of
        what exists** — every entry in it is real — and the caller names
        :data:`_LOST_FILES_DERIVED_KEY_ORDER` in ``partial_counts``.
    """
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id) & Key("run_id").begins_with(_QUEUED_ROW_ID_PREFIX),
        "FilterExpression": Attr(_QUEUE_OUTCOME_ATTRIBUTE).eq(_QUEUE_OUTCOME_LOST),
        "ProjectionExpression": (
            f"file_name, source_key, lost_at, lost_reason, started_at, {_QUEUE_OUTCOME_ATTRIBUTE}"
        ),
    }

    entries: list[dict[str, Any]] = []
    complete = False
    for _page in range(_MAX_LOST_FILE_PAGES):
        response = executions_table.query(**query_kwargs)
        for row in response.get("Items", []):
            if not _is_lost_queued_row(row):
                continue
            entries.append(
                {
                    "file_name": item_str(row, "file_name"),
                    "source_key": item_str(row, "source_key"),
                    "lost_at": item_str(row, "lost_at") or item_str(row, "started_at"),
                    "lost_reason": item_str(row, "lost_reason"),
                }
            )
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            complete = True
            break
        # Checked AFTER establishing there is more to read, so the first page is always
        # read and the bound applies to *continuing* — the property
        # ``_count_partition_rows``, the filter walk and ``_schema_hash_versions`` all
        # hold, and the reason an empty list is never reported as partial when the
        # pipeline genuinely has no losses.
        if time.monotonic() >= deadline:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    entries.sort(key=lambda entry: item_str(entry, "lost_at"), reverse=True)
    return entries, complete


def _split_derive_and_filter(
    all_items: list[dict[str, Any]], status: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One raw index page → ``(executions, queued_files)``, in the one correct order.

    **This function exists so that the order of these four steps is spelled once.**
    Task 4.3's filter walk reads more than one index page, so every step had to run
    per page; duplicating them at a second site would have produced two divergent
    spellings of the phantom predicate and the ``WAITING_APPROVAL`` derivation — the
    drift ``LGR-9e76249-9`` records as the cost of a mirrored definition. Every
    position below is load-bearing and each is a do-NOT:

    1. **split ``QUEUED``** into ``queued_files`` (do-NOT 4's anchor);
    2. **phantom-row filter**, on the POST-split ``executions`` list (do-NOT 4);
    3. **derive ``WAITING_APPROVAL``** from ``pending_approval_id``;
    4. **status filter**, last — after 2 and after 3 (do-NOT 8, R4.2, CP3).

    Step 4 after step 3 is the one a reasonable implementer would "optimise" by
    pushing onto the query as a ``FilterExpression``. See
    :data:`_ACCEPTED_STATUS_FILTERS` for the two independent reasons that is wrong.

    ``status`` of ``None`` means no filter and the returned ``executions`` are exactly
    what this function returned before task 4.3 — the unfiltered read path is
    byte-equivalent, which is what keeps do-NOT 3 (``get_queued_files``, which calls
    with defaults) and CP6 untouched.
    """
    # Separate QUEUED placeholder items into their own array (merged response)
    #
    # p54 R9.3 — the published ``status`` is the state that is true, not a literal.
    # It was an unconditional ``"queued"``, which is the last thing an operator is
    # told about a file that is already in the DLQ and will never be processed. The
    # value now comes from :data:`_QUEUE_OUTCOME_ATTRIBUTE` (absent → ``"queued"``);
    # see those constants for where the attribute comes from, why the row's own
    # ``status`` stays ``"QUEUED"``, and why its producer is task 7.7's.
    #
    # **The edit is inside this comprehension on purpose.** It is the one place in
    # this function that cannot move a step boundary, so the four steps stay in the
    # order the docstring above fences (do-NOT 14, R9.8). The **key set is
    # unchanged** — four keys, and ``test_split_derive_and_filter_order_
    # characterization.py``'s set-equality assertion is the fence on that (do-NOT 3).
    executions = [e for e in all_items if e.get("status") != _QUEUED_ROW_STATUS]
    queued_files = [
        {
            "file_name": e.get("file_name", ""),
            "source_key": e.get("source_key", ""),
            "queued_at": e.get("started_at", ""),
            "status": (_QUEUED_STATUS_LOST if _is_lost_queued_row(e) else _QUEUED_STATUS_DEFERRED),
        }
        for e in all_items
        if e.get("status") == _QUEUED_ROW_STATUS
    ]
    queued_files.sort(key=lambda x: item_str(x, "queued_at"))

    # p38 R3.2 / R3.5: drop Phantom_Execution_Rows — rows no state-recording
    # writer has ever touched — so they neither inflate the running count that
    # PipelineOverviewPage.tsx:43 derives from this response nor consume a page
    # slot ahead of a real run (``LGR-21a1b5b-2``, **template / ratified**, whose
    # *decision* is to put the Processed_File_Ledger in its own table but whose
    # *reasoning* is what governs here: "the run-history read path treats every
    # non-QUEUED row as a run and pages it with a server-side Limit", so a non-run
    # row "would render as a phantom run unless three separate readers learned to
    # ignore it". Also an in-code fence at ``data-stack.ts:237-251``). It also
    # satisfies R3.5 structurally: a filtered row renders no dead link because it
    # never reaches the list.
    #
    # **The position stands: this runs on the POST-split ``executions`` list,
    # never on ``all_items``.** It is the defensive ordering — a filter applied
    # after the split cannot remove a row from ``queued_files``, whatever shape
    # the QUEUED placeholder turns out to have. That is why the placement is
    # unaffected by the correction below, and it is still the spelling to keep.
    #
    # The placeholder's writer IS located, and **design §5 delta D3 is CLOSED**
    # — resolved during implementation (task 6.2), though the design text still
    # lists it as unresolved, so a reader coming from there will expect
    # otherwise. The writer is ``_track_queued_file``
    # (``pipeline_trigger/lambda_function.py:676-694``), whose ``put_item``
    # (``:681-692``) writes ``"status": "QUEUED"`` at ``:685`` and
    # ``current_step="queued_trigger"`` at ``:689``; a grep for
    # ``"status": "QUEUED"`` across ``lambda/``, ``shared/`` and ``agentcore/``
    # returns exactly that one writer. So a QUEUED placeholder carries
    # ``current_step`` and **never matched this predicate at all** — the earlier
    # note here asserted the opposite, and D3's premise that these rows are
    # phantom-shaped was false. Applying the filter before the split would
    # therefore be equivalent today, and fragile: it would tie this filter's
    # correctness to the placeholder continuing to set ``current_step``.
    # ``get_queued_files`` delegates to the caller and reads ``queued_files``
    # only, so that path is unaffected.
    #
    # Deliberately NOT applied in ``handlers/metrics.py``: ``:112`` and ``:724``
    # read the same ``StatusIndex`` GSI, and p39-metrics-truth is rewriting those
    # computations. A copy there would be a second, divergent spelling of this
    # predicate in a file another spec is changing.
    executions = [e for e in executions if not is_phantom_execution_row(e)]

    # Enrich RUNNING executions with pending approval details from denormalized fields
    # pending_approval_id and pending_approval_type are written directly to execution
    # records by the approval handler (Task 17.2/17.3), so no extra queries needed.
    for exec_item in executions:
        if exec_item.get("pending_approval_id"):
            exec_item["status"] = "WAITING_APPROVAL"
            approval_type = exec_item.get("pending_approval_type", "")
            if approval_type:
                exec_item["current_step"] = approval_type

    # p48 C5 (R4.2, R4.4, task 4.3): the status filter, on the **derived** status —
    # so it must be the last step, below the loop above. A row stored ``RUNNING``
    # with ``pending_approval_id`` set now reads ``WAITING_APPROVAL`` and matches
    # ``status=WAITING_APPROVAL``; it does NOT match ``status=RUNNING``, its stored
    # value. That pair is CP3, and CP3 fails if this line moves above the derivation
    # — which is do-NOT 8 made mechanical rather than left as prose.
    #
    # Exact string comparison, not case-folded and not stripped. ``LGR-ad78946-1``
    # rejected normalising a configured value on the ground that "normalising removes
    # the only signal that the deployed value is not the intended one"; the same holds
    # for a query-string value, where a 400 naming the accepted set is a more useful
    # answer to ``?status=succeeded`` than a silent match would be. The value reaching
    # here has already been checked against :data:`_ACCEPTED_STATUS_FILTERS`, so this
    # comparison cannot be the thing that decides an unknown value's fate.
    if status is not None:
        executions = [e for e in executions if item_str(e, "status") == status]

    return executions, queued_files


def get_pipeline_executions(
    pipeline_id: str, page: int = 1, page_size: int = 10, cursor: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """Get execution history for a pipeline from the executions summary table.

    ``cursor`` is the keyed-pagination input (p48 C2, task 3.2, Arm A as chosen in
    3.1): the opaque token a previous response returned as
    ``pagination.next_cursor``. Absent, this reads the newest page.

    ``status`` is the derived-status filter (p48 C5, task 4.3, **Arm 1** — a bounded
    filter walk). Absent, every read below is byte-equivalent to what it was before
    4.3, which is what keeps do-NOT 3 and CP6 untouched. Present, the read becomes a
    walk: successive index pages until ``page_size`` matches are collected or a bound
    trips. Accepted values are :data:`_ACCEPTED_STATUS_FILTERS`; anything else is a
    **400**, and ``QUEUED`` a 400 of its own (O2, resolved at
    :data:`_QUEUED_STATUS_FILTER_MESSAGE`).

    **What a filter changes about the envelope, stated because three keys shift
    meaning and one does not.**

    * ``next_cursor`` stays **exact**. Each page of the walk asks for only the rows
      still needed (``Limit = page_size - len(matches)``), so the walk never reads
      past a row it returns and never discards a match it found. Resuming from the
      cursor therefore reads the next unread raw row: no duplicate, no omission —
      CP2 holds under the walk exactly as it does without one.
    * ``has_next`` becomes an **upper bound**. It means "more raw rows remain to
      walk", which under a filter is not the same as "more matching rows exist", so
      following the cursor can yield an empty page. ``has_next: False`` is still
      exact: the partition is exhausted, and there are no more matches. Knowing more
      than that would require reading ahead, which *is* the walk — and the walk is
      bounded, so the honest answer is the bound rather than a promise.
    * ``total_count`` counts **matches found**, not partition rows, and the
      ``Select="COUNT"`` walk is skipped entirely because it counts the wrong set.
      Reporting the partition's 61 rows beside a filtered view of 3 would be exactly
      the scope claim R4.3 forbids. It is exact only when the walk covered the whole
      partition — started with no cursor AND ended with no ``LastEvaluatedKey`` — and
      is a floor named in ``partial_counts`` otherwise.
    * A short page is **not** the end of the list. The invariant that makes this
      readable: a filtered page shorter than ``page_size`` with ``has_next: true``
      always carries ``executions`` in ``partial_counts``.

    An empty match is a 200 with ``executions: []`` and an accurate ``total_count``
    of 0 — never a 404 and never a 500 (design §Error Handling).

    **``page`` is no longer a seek input, and cannot be one.** DynamoDB has no
    offset addressing, so a page *number* cannot locate a page; only a key can.
    It stays in the signature — positionally, because ``router.py:152`` passes it
    positionally — and stays in the envelope, but as a **label**:

    * with a cursor, the label comes out of the cursor, so the number reported and
      the rows returned cannot disagree. The query-string ``page`` is ignored on
      that path by design; the cursor is the only thing that knows which page it
      addresses.
    * with no cursor, the label is forced to ``1`` whatever was asked for, because
      the first page is the only page reachable without a key. Echoing ``page: 7``
      over page 1's rows would be the *previous* defect wearing a different
      number, and ``ExecutionsPage`` would render it.

    ``total_count`` is the size of the set being paged — index rows in the pipeline's
    partition **excluding Queued_Placeholder / Lost_File rows**, which leave through
    ``queued_files`` and ``lost_files`` and can never appear in ``executions``. See
    :func:`_count_partition_rows` for the full statement, including what it still
    over-states and why the earlier "exact number of page reads a full walk costs"
    reading was given up: a 30-day loss record made the placeholder inclusion a
    permanent, growing gap between the total and the screen. ``total_pages`` derives
    from it, so it is a page-count *label* and not a read-count; navigation is
    ``has_next`` / ``next_cursor``, which come off the page query's own
    ``LastEvaluatedKey`` and are unaffected. Either number can be a **floor** rather
    than a total when the count walk's bound binds, in which case both are named in
    ``pagination.partial_counts`` (always present, ``[]`` when nothing is short).

    ``lost_files`` is a THIRD top-level array (p54 R10.3): every file whose upload was
    dead-lettered and will never be processed, for as long as its record exists —
    **independent of paging and of ``status``**, from a keyed read of the placeholder
    namespace rather than from the page in hand. ``queued_files`` publishes the same
    loss too, but only while the row happens to sit on page 1 of a newest-first index,
    which is about an hour on an active pipeline against a 30-day record. Always
    present, ``[]`` when nothing was lost, and named in ``partial_counts`` when a bound
    or a failure could have left it short. Entries carry no ``run_id``: there is a real
    one on the row, but ``get_run_details`` 404s on it, so publishing it would hand out
    a dead link. See :func:`_lost_files`.

    ``schema_version`` is enriched per row from a paginated walk of the pipeline's
    ``schema_extract`` history (task 6.1, :func:`_schema_hash_versions`), which is
    handed the hashes **this page** carries and stops once all of them are resolved. A
    row whose hash is not in that map carries **no** ``schema_version`` key, which is
    the same shape as a row with no hash at all — so when a bound or a failure could
    have left a wanted hash unresolved, ``schema_version`` is named in
    ``partial_counts`` and the absence reads as "may be missing" rather than "there is
    none". Numbers that *are* present are always right: the walk reads oldest-first and
    versions are first-appearance ordinals, so stopping early — or being stopped —
    loses entries and never renumbers them.
    """
    # The anchor the count's deadline is derived from. Taken at the very top, before
    # the cursor decode, so the window left for the count is what remains of the
    # budget rather than a fresh duration — see :func:`_runs_read_deadline`.
    # ``time.monotonic`` because a wall-clock adjustment mid-request must not make
    # the budget appear to run backwards.
    request_started = time.monotonic()

    # Decoded OUTSIDE the outer ``try`` on purpose. A malformed or foreign cursor is
    # a **bad request**, and the arm below turns every exception into 200-with-empty
    # lists — correct for a read failure, wrong here: it would render "this pipeline
    # has no runs" for a pipeline that has runs, which is the exact class of quiet
    # wrongness this spec exists to remove. Placing the decode above the ``try``
    # makes that swallow structurally impossible rather than merely avoided.
    #
    # *Rejected: falling back to page 1 on a bad cursor.* It makes a tampered cursor
    # indistinguishable from a legitimate first-page request, and would mask a real
    # client bug — a cursor truncated by a URL length limit, say — as "the history
    # restarted from the top".
    exclusive_start_key: dict[str, str] | None = None
    if cursor:
        try:
            exclusive_start_key, page = decode_cursor(cursor, pipeline_id=pipeline_id)
        except InvalidCursorError as bad_cursor:
            # ``str(bad_cursor)`` is one of ``run_cursor``'s fixed messages and never
            # echoes the submitted cursor, so neither the log line nor the body can
            # carry client-controlled content.
            logger.warning("Rejected run-history cursor for %s: %s", pipeline_id, bad_cursor)
            return create_response(400, {"error": "Invalid pagination cursor", "message": str(bad_cursor)})
    else:
        page = 1

    # Validated OUTSIDE the outer ``try`` for the same reason the cursor is, and
    # answered the same way: an unrecognised ``status`` is a **bad request**, not a
    # pipeline with no runs. Task 3.2 set this precedent for this function — a
    # malformed cursor is a 400 rather than an empty 200, "on the grounds that an
    # empty 200 reads as 'this pipeline has no runs'" — and the reasoning transfers
    # unchanged: ``?status=SUCEEDED`` returning ``executions: []`` would read as "this
    # pipeline has no successful runs", a false statement about the data caused by a
    # client typo.
    #
    # **``get_pipelines`` diverges and that is deliberate, not an oversight to
    # align.** ``pipelines_crud.py:1149-1150`` takes its own ``status_filter`` straight
    # into ``Attr("status").eq(...)`` and answers an unrecognised value with an empty
    # list. That is a different table, a different three-value vocabulary
    # (active/archived/error) and a server-side filter; it is also pre-existing and out
    # of this spec's scope. Do not make one match the other without revisiting both.
    #
    # An empty ``?status=`` reads as absent, matching the cursor's truthiness guard
    # (``router.py`` normalises it too, so a direct caller gets the same answer).
    # Neither arm echoes the submitted value back: the message names the accepted set,
    # which is both safer and more useful than repeating what the client sent.
    if not status:
        status = None
    elif status == "QUEUED":
        logger.warning("Rejected QUEUED status filter for %s", pipeline_id)
        return create_response(400, {"error": "Invalid status filter", "message": _QUEUED_STATUS_FILTER_MESSAGE})
    elif status not in _ACCEPTED_STATUS_FILTERS:
        logger.warning("Rejected unrecognised status filter for %s", pipeline_id)
        return create_response(
            400,
            {
                "error": "Invalid status filter",
                "message": f"status must be one of: {', '.join(sorted(_ACCEPTED_STATUS_FILTERS))}",
            },
        )

    try:
        from boto3.dynamodb.conditions import Key

        # p48 C1 (R1.1, R1.4, R1.5): order by the row's own event time. The base
        # table cannot — its sort key is ``run_id``, and SIX id prefixes share
        # it, so a descending scan groups BY KIND and is chronological only
        # within a group. Table-wide, descending, that order is:
        #
        #     unknown > skipped- > run- > retry- > queued- > file- > e2e-
        #
        # So every duplicate skip pinned above every run permanently — the
        # operator's report, and ``LGR-21a1b5b-4`` (**template / ratified**)
        # named this exact defect when it rejected the ``skipped-`` prefix for
        # the contention-skip id. A ``retry-`` row sorted BELOW every ``run-``
        # row, and the ``queued-`` placeholder — frequently the newest event on
        # the list — sorted below both and fell off page 1 once a pipeline had
        # ``page_size`` higher-sorting rows. Verified live (the validation account,
        # ``latesttesting-19ca826a``): ``skipped-42e54662…`` at 08-20 20:51 sat
        # at position 2 of 9 under ``run_id`` and sits at position 6 under this
        # index, below the 08-22 run it predates.
        #
        # ``started_at`` is the sort key and the ONLY ordering attribute
        # reachable: a GSI sorts on exactly one, so the
        # ``last_skipped_at → started_at → created_at`` fallback an in-memory
        # sort could have expressed is NOT available (R1.4). Verified against
        # live data — no row carries ``last_skipped_at``, and a duplicate skip's
        # ``started_at`` IS the real skip instant. A **contention** skip's is the
        # collapse-window start rather than the skip instant: a cost
        # ``LGR-21a1b5b-4`` accepted explicitly when it shaped that id like a
        # run id, so this inherits that cost rather than introducing one. That
        # record's ``revisit_when`` — "the run-history query stops paging by run
        # id" — is what this query satisfies; it is not superseded by it.
        #
        # The index is SPARSE: a row carrying no ``started_at`` is absent from
        # it, silently, and so invisible here (p48 R9.1, detected by
        # ``scripts/detect_unindexed_executions.py``). Do NOT read that as a
        # filter (do-NOT 2, R9.4). It overlaps the phantom-row filter below by
        # accident only, and the two predicates differ in BOTH directions —
        # ``test_executions_index_sparseness.TestSparsenessIsNotAFilter`` pins
        # that. The phantom filter keeps its position on the post-split list.
        #
        # ``Limit=page_size`` stays on the query, which is what keeps the
        # enrichment key count a function of page size alone (CP6) — the
        # rejected bounded-in-memory-walk arm would have broken it. A query
        # against a missing or still-backfilling index raises, and that lands in
        # the outer ``except`` below as a 200 with empty lists: a read failure
        # must never become a 500.
        #
        # p48 C2 (R1.3, R2.1, task 3.2): ONE page, positioned by
        # ``ExclusiveStartKey``. That preserves CP6 exactly — the walk-and-discard
        # arm rejected in 3.1 would have read ``page`` pages to serve page
        # ``page``, and a mechanism that reads more than one page before enriching
        # is what makes the key count a function of walk depth instead of page
        # size. There is deliberately no loop here: the key that positions this
        # single query came from the client, and the client asks again for the
        # next one.
        # p48 C5 (task 4.3): with no ``status`` this loop runs exactly once and issues
        # exactly the one page read C2 established. With a ``status`` it becomes the
        # **bounded filter walk** — C5 Arm 1.
        #
        # *Rejected: Arm 2, filtering within one page only.* Cheapest, and it renders a
        # filtered view that is simply wrong: an operator filtering ``SKIPPED`` on a
        # pipeline whose skips are older than one page sees "no runs match" when three
        # exist. That is the same class of quiet wrongness as the defect this spec
        # opened on.
        #
        # *Rejected: Arm 3, a second GSI on ``(pipeline_id, status)`` or reuse of
        # ``StatusIndex``.* ``StatusIndex`` is partitioned by status **globally**, so
        # filtering one pipeline still means filter-after-read; and a fourth index on
        # this table would be a second reversal of ``LGR-9a35b2d-3`` /
        # ``LGR-9a35b2d-5``'s declines for a narrower reason than R2's. Neither arm can
        # match ``WAITING_APPROVAL`` at all, since no writer stores it.
        #
        # **The accepted tradeoff, stated rather than buried:** a filtered request can
        # cost up to :data:`_MAX_FILTER_PAGES` page reads instead of one, and
        # ``has_next`` stops being an exact statement about matches (see the
        # docstring). Bought with: the enrichments still see at most ``page_size`` rows
        # (CP6, below), the walk gets its own share of the budget so it cannot starve
        # them (:data:`_FILTER_WALK_TIME_SHARE_SECONDS`), and any bound that binds is
        # named in ``partial_counts`` rather than rendered as a complete answer.
        query_kwargs: dict[str, Any] = {
            "IndexName": RUNS_BY_TIME_INDEX,
            "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
            # Newest first by ``started_at`` — the index's sort key, NOT the
            # table's ``run_id``. This comment was false until p48 R1.5.
            "ScanIndexForward": False,
        }
        filter_walk_deadline = _filter_walk_deadline(request_started)
        # Rebuilt by ``run_cursor.decode_cursor`` from an allowlist and checked
        # against ``pipeline_id`` before reaching here, so no client-chosen
        # attribute name and no foreign partition can land in this key.
        # ``dict[str, Any]`` rather than ``decode_cursor``'s narrower ``dict[str, str]``
        # because this name also holds a ``LastEvaluatedKey`` straight off a boto3
        # response, whose values are the DynamoDB attribute-value union. Both are only
        # ever handed to ``encode_cursor`` (``Mapping[str, Any]``) or back to
        # ``ExclusiveStartKey``, so nothing downstream needs the narrower type.
        next_start_key: dict[str, Any] | None = exclusive_start_key
        executions: list[dict[str, Any]] = []
        queued_files: list[dict[str, Any]] = []
        pages_read = 0
        filter_walk_truncated = False
        while True:
            page_kwargs = dict(query_kwargs)
            # **``Limit`` is what keeps CP6 true under a walk.** It asks for only the
            # rows still needed, so the collected set can never exceed ``page_size``
            # and the enrichments below stay a function of page size alone — 4 keys
            # per run, 40 for a default page, one ``batch_get_item``. It is also what
            # makes ``next_cursor`` exact: the walk stops on a page boundary it read
            # in full, so the final ``LastEvaluatedKey`` sits immediately after the
            # last raw row consumed and nothing between it and the returned rows is
            # skipped. On the first iteration this is ``page_size`` exactly, which is
            # the pre-4.3 call.
            page_kwargs["Limit"] = page_size - len(executions)
            if next_start_key is not None:
                page_kwargs["ExclusiveStartKey"] = next_start_key
            response = executions_table.query(**page_kwargs)
            pages_read += 1

            page_executions, page_queued = _split_derive_and_filter(response.get("Items", []), status)
            executions.extend(page_executions)
            if pages_read == 1:
                # ``queued_files`` comes from the FIRST page only, always, and must not
                # vary with ``status``. Accumulating it across a walk would make the
                # array a function of how far the filter had to search — the same
                # request with ``status=SKIPPED`` and ``status=SUCCEEDED`` would return
                # different queued files, which is incoherent. Page-scoped is also what
                # it has always been, so do-NOT 3 is preserved by construction.
                queued_files = page_queued

            next_start_key = response.get("LastEvaluatedKey")
            if status is None:
                # C2's single keyed page, unchanged: the key that positions this query
                # came from the client and the client asks again for the next one.
                break
            if next_start_key is None:
                # The partition is exhausted. That is *one* of the two conditions the
                # filtered count needs to be exact; the other is that the walk also
                # began at the top (no cursor in), which is checked where the count is
                # settled rather than assumed here.
                break
            if len(executions) >= page_size:
                break  # the page is full; there may be more matches behind the cursor
            # Checked AFTER establishing there is more to read, so the first page is
            # always read and the bound applies to *continuing* — the same property
            # ``_count_partition_rows`` and ``stats.py`` hold.
            if pages_read >= _MAX_FILTER_PAGES or time.monotonic() >= filter_walk_deadline:
                filter_walk_truncated = True
                break

        # The durable Lost_File read (p54 R10.3). Its own array, its own keyed read,
        # and deliberately independent of everything above it: not scoped to page 1
        # the way ``queued_files`` is, and not varying with ``status`` the way the
        # ``executions`` walk does. A loss record lives 30 days and the array must be
        # the same on every page and under every filter, because "is this file gone"
        # is not a question about the page being looked at.
        #
        # Its own ``try``, for the reason the count below has one: a read that raises
        # must degrade this array alone and must NOT reach the outer arm, which would
        # discard a page of correctly-ordered rows already in hand and render "this
        # pipeline has no runs". ``lost_files_complete`` starts False so a raise
        # before the read returns marks the list short rather than silently claiming
        # it whole — the same posture ``schema_version_complete`` takes below.
        lost_files: list[dict[str, Any]] = []
        lost_files_complete = False
        try:
            lost_files, lost_files_complete = _lost_files(pipeline_id, _lost_file_read_deadline(request_started))
        except Exception as lost_err:
            logger.warning(f"Failed to read lost files for {pipeline_id}: {lost_err}")

        # Enrich executions with schema version from denormalized schema_hash field
        # schema_hash is written directly to execution records by the execution updater (Task 17.1)
        #
        # p48 C6 (task 6.1): the map is built by a **paginated** walk of the whole
        # partition, bounded by a page cap and a deadline, and any bound that binds is
        # named in ``partial_counts``. See :func:`_schema_hash_versions`.
        #
        # The walk is handed the hashes THIS PAGE carries, so it stops as soon as all
        # of them are resolved instead of numbering history nobody is looking at. The
        # ordinals are identical either way — a version is fixed at a hash's first
        # appearance — and the map it returns is allowed to be short of hashes the
        # page never mentions, because no row here could have rendered one.
        #
        # Still inside this ``try``, and the position is load-bearing rather than
        # incidental: a schema-version read that raises must leave the rows with no
        # ``schema_version`` and let the rest of the response through. That is
        # ``LGR-eee8ac8-4``'s principle — bookkeeping about a run must never fail the
        # run — applied to a read instead of a write, and it is the same reason the
        # count below carries its own ``try`` rather than reaching the outer arm.
        # ``schema_version_complete`` starts False so a raise before the walk returns
        # marks the map short rather than silently claiming it whole.
        schema_version_complete = False
        try:
            page_hashes = frozenset(h for h in (item_str(e, "schema_hash") for e in executions) if h)
            global_hash_versions, schema_version_complete = _schema_hash_versions(
                pipeline_id, _schema_version_walk_deadline(request_started), page_hashes
            )

            # Assign version numbers using the global version map and denormalized schema_hash
            for exec_item in executions:
                schema_hash = item_str(exec_item, "schema_hash")
                if schema_hash and schema_hash in global_hash_versions:
                    exec_item["schema_version"] = global_hash_versions[schema_hash]
        except Exception as schema_err:
            logger.warning(f"Failed to enrich schema versions: {schema_err}")

        # Enrich executions with actual_cost from run_pipeline step records in
        # State_Table. P28 R8.2 (task 14.2): new rows carry the canonical
        # "run_pipeline" step name; rows from before the rename persist with
        # the legacy "RunPipeline" key — batch-get BOTH keys per run (each is
        # a direct key lookup; only one exists per run).
        #
        # p37 C3 (R1.7, R1.9, R1.10) rides along on this same read: one extra key
        # per run for the load_metrics counts row, and ZERO new DynamoDB
        # operations. LGR-7c18e81-1 (**backfilled** — the author's own words,
        # unconfirmed history) records that this file's read cost was deliberately
        # reduced (parallelised step-log fetching, fewer pipeline-list enrichment
        # queries); a second query or get_item here would spend that back.
        # R1.10 holds by construction: one deriver over one input source, and the
        # run-detail reader calls the same pure function.
        try:
            run_ids = [e["run_id"] for e in executions if e.get("run_id")]
            cost_map: dict[str, Any] = {}
            load_outcome_map: dict[str, dict[str, Any]] = {}
            lower_bound_map: dict[str, dict[str, Any]] = {}
            if run_ids:
                run_pipeline_variants = (str(StepName.RUN_PIPELINE), "RunPipeline")
                keys = [{"run_id": rid, "step_name": step} for rid in run_ids for step in run_pipeline_variants]
                keys += [{"run_id": rid, "step_name": _LOAD_METRICS_STEP} for rid in run_ids]
                # p37 C5's fourth key. Bought for retroactive display only: runs
                # that completed before C1 shipped have no load_metrics row, and
                # an inline-DQ s3_tables run's DQ report can still establish
                # no_rows_loaded from rows_passed == 0 with rows_quarantined > 0.
                keys += [{"run_id": rid, "step_name": _DQ_REPORT_STEP} for rid in run_ids]
                # DynamoDB batch_get_item supports max 100 keys per call.
                # Risk N3, as verified here rather than restated: the key count is
                # now 4 per run — two run_pipeline name variants, load_metrics and
                # data_quality — so one call covers 25 runs, down from 33 at three
                # keys and 50 at two. page_size defaults to 10 (:58) and the query
                # above passes Limit=page_size (:66), so the default page holds at
                # most 10 runs = 40 keys and no page crosses the boundary. It is
                # NOT unbounded, though: router.py:150 reads page_size straight off
                # the query string with no ceiling, so ?page_size=26 now crosses it
                # — where the chunking loop below splits correctly (the maps are
                # keyed by run_id, so a run's keys landing in different chunks costs
                # a second call and changes no result). The N+1 guard's 20 items =
                # 80 keys stay in one call, which is what keeps its operation-count
                # assertion true unamended.
                for i in range(0, len(keys), 100):
                    batch_keys = keys[i : i + 100]
                    batch_resp = dynamodb.batch_get_item(
                        RequestItems={
                            STATE_TABLE_NAME: {
                                "Keys": batch_keys,
                                "ProjectionExpression": (
                                    "run_id, step_name, output_data, rows_read, rows_written, "
                                    "rows_passed, rows_quarantined"
                                ),
                            }
                        }
                    )
                    for item in batch_resp.get("Responses", {}).get(STATE_TABLE_NAME, []):
                        rid = item_str(item, "run_id")
                        step = item_str(item, "step_name")
                        if step == _LOAD_METRICS_STEP:
                            load_outcome_map[rid] = _load_outcome_from_row(item)
                            continue
                        if step == _DQ_REPORT_STEP:
                            # None when the row establishes nothing, which is the
                            # common case: only an inline-DQ row that quarantined
                            # every row can settle an outcome (p37 C5).
                            lower_bound = _load_outcome_lower_bound_from_dq_row(item)
                            if lower_bound is not None:
                                lower_bound_map[rid] = lower_bound
                            continue
                        output_data = item_dict(item, "output_data")
                        actual_cost = output_data.get("actual_cost")
                        if actual_cost is not None:
                            # Canonical row wins if a run somehow has both variants.
                            if rid in cost_map and step != str(StepName.RUN_PIPELINE):
                                continue
                            cost_map[rid] = float(actual_cost)
            for exec_item in executions:
                rid = item_str(exec_item, "run_id")
                exec_item["actual_cost"] = cost_map.get(rid)
                # Recorded counts first, the DQ_Report_Row's lower bound second,
                # absence last. No row of either kind ⇒ not_established, naming
                # the absence as its provenance. Never omitted and never a guess
                # (R1.6).
                exec_item["load_outcome"] = _prefer_established(load_outcome_map.get(rid), lower_bound_map.get(rid))
        except Exception as cost_err:
            logger.warning(f"Failed to enrich execution costs: {cost_err}")
            # On failure, still set actual_cost to null on all executions — and
            # mirror that for load_outcome with not_established on every row. A
            # read failure degrades the annotation; it must never become a 500.
            for exec_item in executions:
                if "actual_cost" not in exec_item:
                    exec_item["actual_cost"] = None
                if "load_outcome" not in exec_item:
                    exec_item["load_outcome"] = _load_outcome_absent()

        # p48 C2: the token for the page after this one, or ``None`` when this was
        # the last page. Minted from the query's own ``LastEvaluatedKey`` — verified
        # live to be ``{pipeline_id, started_at, run_id}`` on this index — and bound
        # to ``pipeline_id`` at mint time as well as at redeem time.
        # ``next_start_key`` rather than ``response["LastEvaluatedKey"]`` because under
        # a filter walk the last page read is not necessarily the first: this is the
        # key the walk actually stopped at, which is the position a resumed request
        # must start from. Identical value on the unfiltered path, where the loop runs
        # once. Under a filter it is what keeps the cursor exact — the walk consumed
        # whole pages it read, so nothing sits between this key and the rows returned.
        next_cursor = encode_cursor(next_start_key, pipeline_id=pipeline_id, page=page + 1)

        # p48 C3 (R2.2, R2.3, R2.5): the exact total, or a floor that says so.
        #
        # Placed LAST, after the enrichments, on purpose — see
        # :func:`_runs_read_deadline`. Inside the outer ``try`` and additionally
        # wrapped in its own, so the two failure modes stay distinguishable:
        #
        # * a **bound** that binds is not an error. The walk returns what it counted
        #   and ``complete`` is False.
        # * a count query that **raises** must degrade too, and must NOT become a
        #   500 or a 400. It cannot be allowed to reach the outer arm either: that
        #   would discard a page of correctly-ordered rows already in hand and
        #   render "this pipeline has no runs", which is the class of quiet
        #   wrongness this spec opened on. So the count is the only thing that
        #   degrades, and the rest of the response survives.
        #
        # **Under a ``status`` filter the walk is skipped entirely, because it counts
        # the wrong set** (p48 C5, R4.3, task 4.3). ``_count_partition_rows`` counts
        # every non-placeholder index row; a filtered view's number has to be a count
        # of *matches*, and
        # reporting the partition's total beside three matching rows is exactly the
        # scope claim R4.3 forbids — it implies the whole history was searched and
        # found 61 skips. A filtered count over the whole partition is not available
        # cheaply either: the predicate is application-side over the **derived** status,
        # so ``Select="COUNT"`` cannot express it and a ``FilterExpression`` cannot
        # either (see :data:`_ACCEPTED_STATUS_FILTERS`). So the filtered answer is what
        # the walk actually established, marked when that is a floor.
        if status is None:
            try:
                counted, count_complete = _count_partition_rows(pipeline_id, _runs_read_deadline(request_started))
            except Exception as count_err:
                logger.warning(f"Failed to count run history for {pipeline_id}: {count_err}")
                counted, count_complete = 0, False
        else:
            # Exact only when the walk covered the WHOLE partition: it began at the top
            # (no cursor) and ended with nothing left to read. Any other shape — a
            # cursor'd page, a full page with rows behind it, a bound that bound — makes
            # this "at least this many match", which is what the marker says.
            counted = len(executions)
            count_complete = exclusive_start_key is None and next_start_key is None

        # ``max`` matters only on the degrade arm above, where ``counted`` is 0.
        # Reporting a total BELOW the number of rows in the same response body is not
        # a floor of anything — it is a contradiction visible in one payload, which
        # is exactly the defect R2.3 names in ``total_pages: 1`` beside ``has_next:
        # true``. ``len(executions)`` is a sound lower bound on the raw row count
        # (post-split, post-filter rows are a subset of this page's raw rows, which
        # are a subset of the partition), and on the complete path ``counted`` always
        # dominates it, so this never distorts an exact answer.
        total_count = max(counted, len(executions))
        # Ceiling division, the spelling ``pipelines_crud.py`` already uses. An empty
        # partition gives ``0``, not ``1`` — which is what reconciles the success
        # arm's old literal ``1`` with the error arm's ``0`` (both are now this one
        # expression's answer for an empty set) and matches the in-tree convention
        # that an empty listing reports zero pages.
        total_pages = (total_count + page_size - 1) // page_size
        # ``[]`` means both numbers above are totals. A named key means that number
        # is a floor. Derived from one flag rather than two so ``total_pages`` cannot
        # be reported as exact over a floor it was computed from — a derived value is
        # marked when its inputs are (``stats.py``'s rule for its rates).
        #
        # A floor ``total_pages`` can read BELOW the ``page`` label above, and that is
        # the marker doing its job rather than a bug: clamping it up to ``page``
        # would manufacture a number that merely *looks* consistent, which is what
        # the literal ``1`` did. ``LGR-b2f79ef-14``'s reasoning as
        # ``pipelines_crud.py`` and ``LGR-9a35b2d-2`` both apply it — an unmarked
        # "could-not-check" reported as "checked and found none" is the defect.
        #
        # Task 4.3 adds the filter walk's own entry, ``executions``, and puts it FIRST
        # so the list stays deterministic and assertable with ``==``. It is emitted only
        # when a bound stopped the walk short — see :data:`_FILTER_DERIVED_KEY_ORDER`
        # for why 3.3's reason for omitting it does not survive a filter. It can never
        # appear alone: a truncated walk always leaves a ``LastEvaluatedKey``, so the
        # count is a floor in the same breath. The invariant a client can rely on is
        # that a filtered page shorter than ``page_size`` carrying ``has_next: true``
        # always names ``executions`` here.
        #
        # Task 6.1 adds the schema-version walk's entry, ``schema_version``, between
        # the two so the list mirrors the order the reads run in. Unlike the other two
        # it names a **per-row field** rather than a count, which is a deliberate
        # widening of this list rather than a ninth pagination key —
        # :data:`_SCHEMA_VERSION_DERIVED_KEY_ORDER` records why. It CAN appear alone: a
        # single-page partition of runs whose ``schema_extract`` rows span several
        # pages is an exact count beside a short version map, which is the normal case
        # for a pipeline with many schema versions.
        #
        # p54 R10.3 adds the lost-file read's entry, ``lost_files``, second — the
        # position the read runs in, so the list keeps mirroring the read order and
        # stays deterministic and assertable with ``==``. Like ``schema_version`` it
        # names an array rather than a count, which
        # :data:`_SCHEMA_VERSION_DERIVED_KEY_ORDER` already established as a widening
        # of this list rather than a ninth pagination key. It CAN appear alone: the
        # lost read is keyed and independent of the page walk and the count, so a
        # single-page partition with an exact count can sit beside a lost list a bound
        # cut short.
        partial_counts = list(_FILTER_DERIVED_KEY_ORDER) if filter_walk_truncated else []
        if not lost_files_complete:
            partial_counts.extend(_LOST_FILES_DERIVED_KEY_ORDER)
        if not schema_version_complete:
            partial_counts.extend(_SCHEMA_VERSION_DERIVED_KEY_ORDER)
        if not count_complete:
            partial_counts.extend(_COUNT_DERIVED_KEY_ORDER)
        return create_response(
            200,
            {
                "executions": executions,
                "queued_files": queued_files,
                # p54 R10.3 — the durable record of files that will never be
                # processed. A THIRD top-level array rather than more of
                # ``queued_files``: see :func:`_lost_files` for why widening that one
                # was refused. Always present, ``[]`` when the pipeline has lost
                # nothing, and present on the degrade arm below too — the two arms
                # emit the same top-level keys so a client never branches on which one
                # answered, which ``test_executions_envelope_contract.py`` asserts.
                "lost_files": lost_files,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    # "a cursor was returned", which is C2 Arm A's definition. Derived
                    # from ``next_cursor`` rather than from ``"LastEvaluatedKey" in
                    # response`` so that "there is a next page" and "here is how to
                    # ask for it" are one fact and cannot drift. Same truth value
                    # today; the point is that it stays the same one.
                    "has_next": next_cursor is not None,
                    "has_prev": page > 1,
                    # ALWAYS present, ``None`` on the last page rather than omitted.
                    # ``LGR-9a35b2d-3``'s reasoning for ``partial_counts``, reused:
                    # "one answer is assertable with ``==``; two are not" — an
                    # optional key makes the envelope's key set unassertable, and 4.1
                    # has to declare this field in ``PaginatedExecutions`` either way.
                    "next_cursor": next_cursor,
                    # ALWAYS present, ``[]`` when nothing is short — design §P3 and
                    # ``LGR-9a35b2d-3``: "one answer is assertable with ``==``; two
                    # are not", which is why it is not omitted when empty. Task 4.3
                    # adds the status filter walk's own entry to this same list.
                    "partial_counts": partial_counts,
                },
            },
        )
    except Exception as e:
        logger.error(f"Error getting pipeline executions: {e!s}")
        return create_response(
            200,
            {
                "executions": [],
                "queued_files": [],
                # Present here too, empty, so the key set matches the success arm.
                # NOT named in ``partial_counts`` below, and that asymmetry is
                # deliberate: this arm is reached when the read failed *before* the
                # lost read could run, so there is no "the list may be short" to
                # report — the whole response is a floor, and the counts are what say
                # so. Naming an array whose read never happened would imply the read
                # happened and came back partial.
                "lost_files": [],
                "pagination": {
                    "page": 1,
                    "page_size": page_size,
                    "total_count": 0,
                    "total_pages": 0,
                    "has_next": False,
                    "has_prev": False,
                    # Present here too, so the key set is the same on both arms and a
                    # client never has to branch on which arm answered. ``None``
                    # because a failed read cannot promise where the next page is.
                    "next_cursor": None,
                    # **The pair task 3.3 was left to reconcile, reconciled.** The
                    # success arm's literal ``total_pages: 1`` is gone — it is now
                    # ``ceil(total_count / page_size)``, which is ``0`` for an empty
                    # set. So this arm's ``0`` is no longer a second, contradictory
                    # convention: it is the same expression's answer, and the two arms
                    # agree.
                    #
                    # What still differs is what the zeros MEAN, and that difference
                    # is now reported rather than hidden. On the success arm ``0`` is
                    # a total: the partition is empty. Here the read failed, so ``0``
                    # is a floor — "I could not look", which must not read as
                    # "I looked and found none" (``LGR-b2f79ef-14``'s reasoning; the
                    # same extension ``stats.py`` made when it brought its swallowed
                    # approvals failure under the marker rather than marking one
                    # neighbour honestly and inheriting the other's misreport).
                    #
                    # Scoped to the counts. ``executions`` and ``queued_files`` are
                    # not named: ordering is never truncated under the index, so
                    # ``partial_counts`` has nothing to say about rows (design
                    # §Data Models).
                    "partial_counts": list(_COUNT_DERIVED_KEY_ORDER),
                },
            },
        )


def get_queued_files(pipeline_id: str) -> dict[str, Any]:
    """Get files queued for a pipeline — backward compat redirect.

    Queued files are now returned as part of the executions response.
    This endpoint delegates to get_pipeline_executions and extracts the
    queued_files array for callers that still hit the old route.
    """
    try:
        import json as _json

        exec_resp = get_pipeline_executions(pipeline_id)
        body = _json.loads(exec_resp.get("body", "{}"))
        queued = body.get("queued_files", [])
        return create_response(200, {"queued_files": queued, "count": len(queued)})
    except Exception as e:
        logger.warning(f"Failed to get queued files for {pipeline_id}: {e}")
        return create_response(200, {"queued_files": [], "count": 0})


def get_run_details(run_id: str) -> dict[str, Any]:
    """Get detailed information about a specific pipeline run"""
    try:
        from boto3.dynamodb.conditions import Key

        # Anchored at the TOP, before any read, and used by exactly one thing: the
        # schema-version walk's deadline near the end of this function (p54 R4,
        # task 3.2). Same discipline and same reason as
        # ``get_pipeline_executions``' anchor — ``_runs_read_deadline``'s docstring
        # states it: the window a walk gets is ``share - elapsed``, so it "shrinks
        # when the work ahead of it was slow". Anchoring immediately before the walk
        # instead would hand it a fixed share ON TOP of everything already spent,
        # which is the direction that overruns the Lambda ceiling.
        _request_started = time.monotonic()

        logger.info(f"Fetching run details for: {run_id}")

        # Query state table by run_id — base table query (run_id is PK)
        response = state_table.query(KeyConditionExpression=Key("run_id").eq(run_id))

        items = response.get("Items", [])
        logger.info(f"Found {len(items)} items for run {run_id}")

        if not items:
            return create_response(404, {"error": f"Run not found: {run_id}"})

        # Build run details from items
        run_details: dict[str, Any] = {
            "run_id": run_id,
            "pipeline_id": "",
            "original_pipeline_id": "",
            # p48 task 6.4 — ``RunDetail`` declares this one **required**, so it
            # is initialised here rather than assigned only where a second
            # table's row happens to exist. See the derivation further down.
            "pipeline_key": "",
            "execution_id": "",
            "status": "unknown",
            "started_at": "",
            "file_key": "",
            "original_s3_uri": "",
            "json_s3_uri": "",
            "steps": [],
        }

        for item in items:
            # Update run-level info
            if not run_details["pipeline_id"]:
                run_details["pipeline_id"] = item_str(item, "pipeline_id")
            if not run_details["original_pipeline_id"]:
                run_details["original_pipeline_id"] = item_str(item, "original_pipeline_id")
            if not run_details["execution_id"]:
                run_details["execution_id"] = item_str(item, "execution_id")

            # Get file info
            json_s3_uri = item_str(item, "json_s3_uri")
            if json_s3_uri and not run_details["json_s3_uri"]:
                run_details["json_s3_uri"] = json_s3_uri
                file_key = json_s3_uri.split("/")[-1]
                run_details["file_key"] = file_key

            original_s3_uri = item_str(item, "original_s3_uri")
            if original_s3_uri and not run_details["original_s3_uri"]:
                run_details["original_s3_uri"] = original_s3_uri
                if not run_details["file_key"]:
                    run_details["file_key"] = original_s3_uri.split("/")[-1]

            # Get timestamp
            timestamp = item_str(item, "timestamp")
            if timestamp and (not run_details["started_at"] or timestamp < run_details["started_at"]):
                run_details["started_at"] = timestamp

            # Add step info
            step_name = item_str(item, "step_name")
            if step_name:
                step_info: dict[str, Any] = {
                    "step_name": step_name,
                    "step_sequence": item_int(item, "step_sequence", 0),
                    "status": item_str(item, "status", "unknown"),
                    "timestamp": timestamp,
                    "output_data": item_dict(item, "output_data"),
                    "error_message": item_str(item, "error_message"),
                }

                # Add step-specific data. Branch in canonical space (P28 R8.2,
                # task 14.2) so new snake_case rows AND legacy-variant rows
                # ("CodeGeneration"/"CodeTesting") both take their branch.
                canonical_step = _canonical_or_raw(step_name)
                if canonical_step == StepName.SCHEMA_EXTRACT:
                    step_info["schema_hash"] = item_str(item, "schema_hash")
                    step_info["extracted_schema"] = item.get("extracted_schema", "")
                elif canonical_step == StepName.SCHEMA_MAPPING:
                    step_info["mapping_id"] = item_str(item, "mapping_id")
                    step_info["mapping_count"] = item_int(item, "mapping_count", 0)
                    step_info["unmapped_source_count"] = item_int(item, "unmapped_source_count", 0)
                    step_info["unmapped_target_count"] = item_int(item, "unmapped_target_count", 0)
                elif canonical_step == StepName.CREATE_CODE:
                    # Extract code_version_id from output_data or approval_data
                    output_data = item_dict(item, "output_data")
                    approval_data_str = item.get("approval_data", "")
                    if approval_data_str:
                        try:
                            approval_data: Any = (
                                json.loads(approval_data_str)
                                if isinstance(approval_data_str, str)
                                else approval_data_str
                            )
                            step_info["code_version_id"] = approval_data.get("code_version_id", "")
                            step_info["generated_code_s3_uri"] = approval_data.get("generated_code_s3_uri", "")
                        except (json.JSONDecodeError, TypeError, AttributeError) as e:
                            logger.warning(f"Failed to parse approval_data: {e}")
                    if not step_info.get("code_version_id") and output_data:
                        step_info["code_version_id"] = output_data.get("code_version_id", "")
                elif canonical_step == StepName.TEST_CODE:
                    # Resolve the four test fields from the three places a row can
                    # carry them, in this precedence order:
                    #
                    #   1. ``approval_data`` — kept FIRST, and deliberately, even
                    #      though NO producer writes it onto a state row and none
                    #      ever has: the only writer of the name is
                    #      ``approval_handler:428``, onto the APPROVALS table, and
                    #      its state-table echo (``:457-471``) sets only ``status``
                    #      / ``updated_at`` / ``approval_id``. Verified with
                    #      ``git log -S'"approval_data":'`` over both step Lambdas —
                    #      no commit. Retained for the same old-row-tolerance reason
                    #      ``_synthetic_steps`` still filters ``code_generation`` /
                    #      ``infrastructure`` / ``first_run`` (``LGR-9e76249-9``,
                    #      template / ratified): a live table can hold shapes no
                    #      current producer writes. ``pipelines_crud.py:1500,1516``
                    #      reads the same attribute off state rows.
                    #   2. The row's TOP LEVEL — the path that works today.
                    #      ``test_code`` writes ``test_execution_id`` /
                    #      ``test_status`` / ``test_output_s3_uri`` here, in
                    #      ``state_row_attributes``
                    #      (``test_code/lambda_function.py:3064-3106``) through
                    #      ``build_state_row_update``. This branch used to read them
                    #      from ``output_data`` instead, which could never work:
                    #      ``shared/artifact_schema.create_output_data`` fixes
                    #      ``output_data`` to ``artifact_type`` / ``primary_id`` /
                    #      ``status`` / ``summary`` / ``s3_uris`` and validates them
                    #      (``LGR-60c4985-4``, template / ratified), so moving these
                    #      keys IN is not available either. Confirmed against the
                    #      live row for ``run-2026-08-18-19-45-13-a158b7e5``: 24
                    #      attributes, all three at top level, no ``approval_data``.
                    #   3. ``output_data["approval"]`` — attached at ``:3052`` AFTER
                    #      validation runs, so it is a sixth key that does not
                    #      contradict ``LGR-60c4985-4``. Carries
                    #      ``test_execution_id``, ``test_status`` and
                    #      ``code_version_id`` but NOT ``test_output_s3_uri``
                    #      (``:3012-3030``), and is the only place this row carries
                    #      ``code_version_id`` at all.
                    #
                    # Each field resolves INDEPENDENTLY, and only while still empty.
                    # The previous single ``if not step_info.get("test_execution_id")``
                    # gate let one present field suppress the lookup of the other
                    # three.
                    output_data = item_dict(item, "output_data")
                    approval_data_str = item.get("approval_data", "")
                    if approval_data_str:
                        try:
                            approval_data = (
                                json.loads(approval_data_str)
                                if isinstance(approval_data_str, str)
                                else approval_data_str
                            )
                            step_info["test_execution_id"] = approval_data.get("test_execution_id", "")
                            step_info["code_version_id"] = approval_data.get("code_version_id", "")
                            step_info["test_status"] = approval_data.get("test_status", "")
                            step_info["test_output_s3_uri"] = approval_data.get("test_output_s3_uri", "")
                        except (json.JSONDecodeError, TypeError, AttributeError) as e:
                            logger.warning(f"Failed to parse approval_data: {e}")
                    output_approval = item_dict(output_data, "approval")
                    for _field in ("test_execution_id", "code_version_id", "test_status", "test_output_s3_uri"):
                        if step_info.get(_field):
                            continue
                        step_info[_field] = item_str(item, _field) or item_str(output_approval, _field)

                run_details["steps"].append(step_info)

        # Sort steps by sequence first
        run_details["steps"].sort(key=lambda x: x.get("step_sequence", 0))

        # Inject the currently running step if it's not already in the steps list.
        # The state table only has entries for completed steps — the step that's
        # actively executing hasn't written its result yet.
        # P28 R8.2 (task 14.2): the order is the shared canonical task-step
        # sequence; all membership checks run in canonical space so rows and
        # current_step markers in either naming generation are recognized.
        PIPELINE_STEP_ORDER = [str(step) for step in TASK_STEPS]
        # Sequence number for each step (matches the Lambda step_sequence values)
        _STEP_SEQ = {name: STEP_SEQUENCE[from_any(name)] for name in PIPELINE_STEP_ORDER}
        # Canonical names of steps that already have state rows (dedup space)
        existing_step_names = {_canonical_or_raw(s["step_name"]) for s in run_details["steps"]}
        try:
            pid = run_details.get("original_pipeline_id") or run_details.get("pipeline_id", "")
            if pid:
                # ── The run's own executions row, read by key (p48 C8, R6.3) ──
                #
                # This was ``query(ScanIndexForward=False, Limit=5)`` followed by
                # ``if ex.get("run_id") == run_id`` in Python. The base table's sort
                # key is ``run_id``, so "the five newest" was really "the five
                # lexicographically highest ids", and **six** id prefixes share that
                # key — measured live, not assumed: ``run-`` 50, ``file-`` 4,
                # ``skipped-`` 3, ``e2e-`` 2, ``retry-`` 1, ``unknown`` 1. Their real
                # order, re-derived here rather than taken from p48 design §"Observed
                # run_id prefixes", which has it wrong for ``retry-``:
                #
                #   e2e- < file- < queued- < retry- < run- < skipped- < unknown
                #
                # so ``skipped-`` and the bare ``unknown`` outrank every ``run-``,
                # while ``retry-`` sorts *below* it ('e' < 'u' in retry/run — the
                # design text's "a retried run outranks every original" is false).
                # On ``latesttesting-19ca826a`` (2 skips) the run under investigation
                # sat at position 3 of the 5 returned, and three more duplicate skips
                # would have pushed it out of the window — at which point the
                # injected next step vanishes with no error at all. A duplicate-skip
                # id is deterministic in the content fingerprint, so such a row never
                # ages out and never re-sorts: the failure would have been permanent.
                #
                # The run id is a parameter and ``pid`` is already resolved, so both
                # key parts are in hand and the scan was never needed. One keyed
                # ``get_item`` is strictly cheaper than the five-row read it replaces,
                # cannot miss, and is correct at any depth in the partition.
                #
                # Same *consequence class* as ``LGR-9a35b2d-2`` (**template /
                # ratified**) — a ``Limit`` window plus a filter reporting "not found"
                # for an item that exists past the window — but deliberately NOT the
                # same mechanism, and the distinction matters because that record
                # **exempts** an executions probe of its own: its defect is ``Limit``
                # being applied before a server-side ``FilterExpression``, and the
                # ``pipelines_crud.py`` probe was left at ``_EXECUTIONS_PROBE_LIMIT =
                # 5`` precisely because it carries no ``FilterExpression``. The filter
                # here ran in **Python**, one step further out again, so the record is
                # a reasoning precedent for this read and not a fence on it. Nothing
                # in the ledger governs this lookup (p48 R7.3 — that is the gap).
                #
                # This is the last ``run_id``-ordered read in ``get_run_details``,
                # but NOT in this module — p48's task text says "in the file" and
                # that is wrong. ``cancel_run`` and ``retry_run`` each still do
                # ``query(ScanIndexForward=False, Limit=10/20)`` plus the same Python
                # ``run_id`` filter, both with ``pipeline_id`` already in hand. Both
                # are deliberately out of p48's scope: ``cancel_run`` is tracked as an
                # unguarded upsert in ``test_executions_started_at_invariant.py``'s
                # ``KNOWN_UNGUARDED``, and ``retry_run``'s read is load-bearing for
                # ``LGR-8e34f37-2``'s read-modify-write of ``attempts`` off the same
                # row. Fixing either is its own change.
                #
                # ``run_id-index`` is deliberately NOT the instrument. The in-code
                # reason it exists (``data-stack.ts``) is a *run-only* lookup —
                # ``cancel_run``'s recovery path, where the state table carries no
                # ``pipeline_id`` and there is no key-bounded access path without it.
                # Here ``pipeline_id`` is available, so the base table answers in one
                # read with no index hop and no GSI eventual-consistency window.
                #
                # Deliberately NOT merged with the ``step_logs`` / lineage
                # ``get_item`` further down this function, even though it reads the
                # same row by the same key: ``LGR-8e34f37-3`` (**template /
                # ratified**) pins the three lineage keys to be assigned *inside that
                # try*, so a read which raised leaves them off the body rather than
                # reporting zero attempts. One shared read would make a single
                # ``except`` decide two unrelated degradations — this block degrades
                # silently to "no injected step", that one omits keys.
                ex = executions_table.get_item(
                    Key={"pipeline_id": pid, "run_id": run_id},
                    # Only what the injection needs. ``status`` is a DynamoDB reserved
                    # word and needs the alias; ``current_step`` and ``updated_at``
                    # are not. Narrow on purpose — this read must not become a second
                    # source for anything else the response carries (do-NOT 9).
                    ProjectionExpression="#status, current_step, updated_at",
                    ExpressionAttributeNames={"#status": "status"},
                ).get("Item")
                if ex:
                    exec_status = item_str(ex, "status")
                    current_step = item_str(ex, "current_step")

                    # If execution is still active, figure out the next step
                    if exec_status in ("RUNNING", "IN_PROGRESS", "WAITING_APPROVAL"):
                        # Check for pending approvals first — if there's one, the pipeline is paused
                        has_pending_approval = False
                        try:
                            # Query the run_id GSI instead of scanning the table
                            # (P28 R17.1 — this was an N+1 scan per run).
                            appr = approvals_table.query(
                                IndexName="run_id-index",
                                KeyConditionExpression=Key("run_id").eq(run_id),
                                FilterExpression=Attr("status").eq("pending"),
                                ProjectionExpression="approval_type",
                            )
                            if appr.get("Items"):
                                has_pending_approval = True
                                approval_step = item_str(appr["Items"][0], "approval_type")
                                # Map approval_type to the CANONICAL step where the
                                # pipeline actually pauses (P28 R8.2/R8.3):
                                # code_generation approval fires AFTER test_code
                                # (reviews code + test results); test_failure is the
                                # forced test-review gate (R4.2), also at test_code.
                                approval_step_map = {
                                    "schema_mapping": str(StepName.SCHEMA_MAPPING),
                                    "code_generation": str(StepName.TEST_CODE),
                                    "code_testing": str(StepName.TEST_CODE),
                                    "test_failure": str(StepName.TEST_CODE),
                                    "infrastructure": str(StepName.CREATE_INFRASTRUCTURE),
                                    "first_run": str(StepName.CREATE_QUALITY_CHECKS),
                                }
                                related_step = approval_step_map.get(approval_step, approval_step)
                                # Mark the related step as waiting_approval if it exists
                                # (compare in canonical space — the run's rows may
                                # carry legacy-variant names).
                                for s in run_details["steps"]:
                                    if _canonical_or_raw(s["step_name"]) == related_step:
                                        # Only mark as waiting_approval if step hasn't already completed
                                        if s["status"] not in ("success", "completed", "SUCCEEDED"):
                                            s["status"] = "waiting_approval"
                                        break
                                else:
                                    # Step not in list yet — add it (canonical dedup)
                                    if related_step not in existing_step_names:
                                        run_details["steps"].append(
                                            {
                                                "step_name": related_step,
                                                "step_sequence": _STEP_SEQ.get(related_step, len(run_details["steps"])),
                                                "status": "waiting_approval",
                                                "timestamp": ex.get("updated_at", ""),
                                                "output_data": {},
                                                "error_message": "",
                                            }
                                        )
                        except Exception:
                            pass

                        # Only inject a "running" next step if there's NO pending approval
                        if not has_pending_approval:
                            next_step_name = None
                            canonical_current = _canonical_or_raw(current_step) if current_step else ""
                            if canonical_current in PIPELINE_STEP_ORDER:
                                idx = PIPELINE_STEP_ORDER.index(canonical_current)
                                if canonical_current in existing_step_names and idx + 1 < len(
                                    PIPELINE_STEP_ORDER
                                ):
                                    next_step_name = PIPELINE_STEP_ORDER[idx + 1]
                                elif canonical_current not in existing_step_names:
                                    next_step_name = canonical_current

                            # Canonical dedup prevents duplicates from naming-variant mismatches
                            if next_step_name and next_step_name not in existing_step_names:
                                run_details["steps"].append(
                                    {
                                        "step_name": next_step_name,
                                        "step_sequence": _STEP_SEQ.get(next_step_name, len(run_details["steps"])),
                                        "status": "running",
                                        "timestamp": ex.get("updated_at", ""),
                                        "output_data": {},
                                        "error_message": "",
                                    }
                                )
        except Exception as inject_err:
            logger.debug(f"Failed to inject running step: {inject_err}")

        # Re-sort after injection to ensure correct order
        run_details["steps"].sort(key=lambda x: x.get("step_sequence", 0))

        # Determine overall status from steps and approvals (canonical-keyed —
        # tolerates legacy "RunPipeline" rows alongside new "run_pipeline" ones)
        step_statuses = {_canonical_or_raw(s["step_name"]): s["status"] for s in run_details["steps"]}
        has_failed = any(s.get("status", "").lower() in ("error", "failed") for s in run_details["steps"])
        has_running = any(s.get("status", "").lower() in ("running", "in_progress") for s in run_details["steps"])
        has_waiting = any(s.get("status", "").lower() == "waiting_approval" for s in run_details["steps"])
        has_success = any(s.get("status", "").lower() == "success" for s in run_details["steps"])

        # Also check approvals table directly for pending approvals on this run.
        # Query the run_id GSI instead of scanning (P28 R17.1). No Limit: with a
        # FilterExpression, Limit bounds items examined BEFORE filtering — the
        # old scan+Limit=1 could miss a pending approval entirely. A run's
        # approval partition holds only a handful of rows, so reading it all
        # is cheap and correct.
        has_pending_approval_for_run = False
        try:
            _appr_check = approvals_table.query(
                IndexName="run_id-index",
                KeyConditionExpression=Key("run_id").eq(run_id),
                FilterExpression=Attr("status").eq("pending"),
                ProjectionExpression="approval_id",
            )
            has_pending_approval_for_run = bool(_appr_check.get("Items"))
        except Exception:
            pass

        if has_failed:
            run_details["status"] = "FAILED"
        elif has_waiting or has_pending_approval_for_run:
            run_details["status"] = "WAITING_APPROVAL"
        elif has_running:
            run_details["status"] = "RUNNING"
        elif step_statuses.get(str(StepName.RUN_PIPELINE), "").lower() == "success":
            run_details["status"] = "SUCCEEDED"
        elif has_success:
            run_details["status"] = "RUNNING"  # Still in progress
        else:
            run_details["status"] = "RUNNING"

        # Load_Outcome (p37 R1.8, R1.10) — what reached the target on this run.
        # Every state row is already in hand from the query above, so this costs no
        # read. It is derived HERE, before the synthetic-step filter below, because
        # that filter drops the ``load_metrics`` row from ``steps``: the row has to
        # be read for its counts before it stops being visible.
        #
        # Reuses the run list's helpers rather than repeating their logic. R1.10 —
        # one Load_Outcome per run, the same on both surfaces — holds *by
        # construction* only while both readers call the same code (design D2), and
        # the Decimal→int conversion inside ``_optional_row_count`` is load-bearing:
        # skipping it yields ``not_established`` for every run while looking healthy.
        #
        # This deliberately does NOT feed the Run_Status chain above, and takes no
        # input from it. Run_Status is derived independently here and in
        # ``execution_updater._derive_execution_status``, and those two already
        # disagree; R1.10 exists to keep the Load_Outcome out of that disagreement.
        # p37 C5: the DQ_Report_Row is on this same partition, so the retroactive
        # lower bound costs no read here either. It is applied on BOTH surfaces or
        # neither — R1.10 requires one Load_Outcome per run, and a fallback wired
        # into the run list alone would make the two disagree for exactly the
        # historical runs it exists to serve.
        _load_metrics_row = next((i for i in items if item_str(i, "step_name") == _LOAD_METRICS_STEP), None)
        _dq_report_row = next((i for i in items if item_str(i, "step_name") == _DQ_REPORT_STEP), None)
        run_details["load_outcome"] = _prefer_established(
            _load_outcome_from_row(_load_metrics_row) if _load_metrics_row is not None else None,
            _load_outcome_lower_bound_from_dq_row(_dq_report_row) if _dq_report_row is not None else None,
        )

        # Remove synthetic steps that aren't real pipeline stages:
        # - pipeline_failure: the Failure_Record sentinel row (R1.4)
        # - data_quality: DQ agent record, not a pipeline step
        # - load_metrics: the generated Glue job's row-counts record (p37 C1), read
        #   for ``load_outcome`` just above. Like data_quality it is a record and
        #   not a Step_Row, so it must not appear in the tree — without this key it
        #   renders as a trailing ⚙️ step, which is the defect R4 exists to remove.
        #   NOTE (p37 risk N4, recorded rather than fixed): the writer side of this
        #   same classification is an ALLOWLIST — ``execution_updater`` routes rows
        #   away by asking ``step_names`` what *is* a step — while this reader side
        #   is a DENYLIST. They agree in effect and disagree in kind, so a future
        #   record type has to be added here by hand. Unifying them means settling
        #   the legacy PascalCase key question that p37 scoped OUT with evidence,
        #   so the asymmetry is deliberate and left in place.
        # - code_generation / infrastructure / first_run: phantom approval-echo
        #   rows keyed by approval_type. P28 R8.3 (task 14.2) stopped WRITING
        #   them, but rows from pre-rename runs persist in live tables, so the
        #   read-side filter stays for old-run tolerance.
        # - shadow_verification: the shadow-mode pandas/Glue comparison record
        #   (p42 R4.3), written by ``test_code``'s
        #   ``build_shadow_verification_item``. This entry is the by-hand
        #   addition ``LGR-9e76249-9`` (**template / ratified**) said this
        #   denylist would need for every future record type, and the writer's
        #   own docstring names it "a **prerequisite for enabling shadow
        #   mode**". Nothing writes the row today — ``VERIFICATION_EXECUTOR``
        #   defaults to ``pandas`` — so this is inert until shadow mode is
        #   switched on, at which point its absence would render the row as a
        #   trailing ⚙️ step with an unknown status: the same live regression
        #   ``load_metrics`` caused before p37 added its key.
        _synthetic_steps = {
            "pipeline_failure",
            "code_generation",
            "infrastructure",
            "first_run",
            # A literal, deliberately, and NOT by the pattern below. The two
            # constants exist because each is spelled TWICE in this module — the
            # Load_Outcome read at the top of this block and this entry — so a
            # rename could leave the reader and the denylist disagreeing. This
            # row has no reader here and none anywhere in the repo (R4.3 is that
            # guarantee), so it is spelled once, like the four names above it.
            # The real drift risk is cross-Lambda and no constant on either side
            # can close it: the writer's ``SHADOW_VERIFICATION_STEP_NAME`` lives
            # in the ``test_code`` Lambda, a separate bundle this one cannot
            # import. It is covered instead by
            # ``tests/contracts/test_shadow_verification_step_name_cross_lambda.py``,
            # which asserts the two spellings by parsing the writer's source —
            # ``LGR-28b4d75-5`` (**template / ratified**) is that technique's
            # precedent.
            "shadow_verification",
            # Spelled by the constant now that p37 C5 reads this row for the
            # lower bound: the denylist entry and the reader must not drift to
            # two spellings of one step_name. Same value, same behaviour.
            _DQ_REPORT_STEP,
            _LOAD_METRICS_STEP,
        }
        run_details["steps"] = [s for s in run_details["steps"] if s["step_name"] not in _synthetic_steps]

        # Fetch cached step logs — and the retry lineage — from the execution record.
        #
        # p38 R5.1/R5.2 ride along on THIS keyed read: one ``get_item``, five
        # attributes since p48 task 6.2 added ``completed_at``, no new query and no
        # new GSI (p38 do-NOT 8). None of the five names is a DynamoDB reserved
        # word, so no ExpressionAttributeNames is needed.
        #
        # The other executions read in this function — the running-step injection
        # above — reads the SAME row by the SAME key since p48 task 6.3, so the
        # hazard this note used to describe is gone: it was a
        # ``ScanIndexForward=False, Limit=5`` query scanned in Python for a matching
        # ``run_id``, which found nothing for an older retried run and would have
        # made the lineage vanish with no error. The two reads are still kept
        # separate, and deliberately — ``LGR-8e34f37-3`` (**template / ratified**)
        # pins the three lineage keys to be assigned inside *this* ``try``, so the
        # two reads must be able to fail independently. Merging them would put one
        # ``except`` in charge of two different degradations. They also ask for
        # different attributes: that one projects ``status`` / ``current_step`` /
        # ``updated_at``, this one the five names below.
        #
        # The three lineage keys are set INSIDE the try, so a read that failed
        # leaves them off the body entirely rather than reporting a run with two
        # recorded attempts as having zero. ``LGR-99050e6-2`` (**template /
        # ratified**) is the shape: an absent counter "is reported as unknown
        # rather than as zero". A row that WAS read and carries no lineage is a
        # different case and does get the keys — empty there is knowledge, not a
        # default. The response stays a 200 either way; the existing ``except``
        # already logs at debug and continues.
        #
        # ── ``completed_at`` rides this read too (p48 C7, R8.1) ──────────────
        #
        # It is the fifth name in the projection, and this read is where it comes
        # from **rather than** the injection read above. Both were available and
        # the choice is not the obvious one, so it is recorded here.
        #
        # Reachability does not decide it, and the p48 task text's guess that it
        # might is wrong — measured, not reasoned from the docstring: the
        # injection ``get_item`` is unconditional inside its ``if pid:``, so it
        # fires for a ``SUCCEEDED`` run exactly as it does for a ``RUNNING`` one.
        # Only the *use* of its result is gated on the three active statuses. A
        # finished run therefore reads that row already, and a ``completed_at``
        # taken off it would be populated.
        #
        # Three things decide it instead. Note what is NOT among them: "the
        # injection read degrades to silent absence and this one does not". Both
        # ``try`` blocks omit on failure, so that distinction does not exist and
        # an earlier draft of this comment claiming it was wrong.
        #
        # 1. That read's ``ProjectionExpression`` carries an in-code fence saying
        #    it "must not become a second source for anything else the response
        #    carries (do-NOT 9)". Widening it is the one thing that comment
        #    forbids by name — and the fence is asserted, not merely written:
        #    ``test_run_details_keyed_executions_lookup.py``'s
        #    ``test_the_injection_read_asks_only_for_what_it_uses`` pins that
        #    projection to exactly its three names.
        # 2. **What the two ``try`` blocks contain.** The injection ``try`` wraps
        #    the whole next-step computation — the approvals query, the canonical
        #    dedup, the step-order arithmetic. A ``completed_at`` assigned in
        #    there has its presence made contingent on code that has nothing to
        #    do with reading it: raise anywhere in the injection *after* the
        #    assignment and the key survives, raise *before* and it does not.
        #    Its absence would then mean "something in the injection failed"
        #    rather than "the completion time could not be read". This ``try``
        #    holds one ``get_item`` and four sibling assignments off the one held
        #    ``Item``, so absence here means exactly one thing.
        # 3. **The no-row case, where the two reads genuinely differ.** The
        #    injection guards on ``if ex:``, so a run with no executions row at
        #    all would get the key *omitted*. This read uses
        #    ``cache_resp.get("Item", {})`` and assigns unconditionally, so the
        #    same run gets ``""`` — present-and-empty, which is the treatment its
        #    three neighbours already have and which the contract pins in
        #    ``test_all_three_keys_are_present_when_the_read_succeeds``: a
        #    ``get_item`` returning no ``Item`` is a *successful* read. Sourcing
        #    from the injection would have given ``completed_at`` a fourth,
        #    inconsistent semantics on one response body.
        #
        # ``LGR-8e34f37-3`` (**template / ratified**) is what makes the resulting
        # three states ratified here rather than incidental: absent means the read
        # did not complete, present-and-empty (``""``) means the row was read and
        # records no completion, populated means the run finished at that instant.
        #
        # R8.3 is satisfied either way at the UI — ``RunMetadataPanel.tsx:56``
        # tests truthiness, so absent / ``None`` / ``""`` all render ``—`` and an
        # in-flight run never shows an invented time. R8.3 holds *because that
        # component is unchanged*, not because of anything here. What this choice
        # buys is honesty on the API side, where the two absences are different
        # facts.
        #
        # Deliberately NOT the derived Run_Status's business: this is a timestamp
        # read straight off the row, and the derivation at the status block above
        # neither reads it nor is read by it (do-NOT 9, R8.2).
        #
        # ── Why the projection stops at five names (p48 task 6.4, design §O9) ──
        #
        # ``completed_at`` was found by comparing ONE row against ONE panel, so O9
        # asked whether that accident hid siblings. It did — but not the ones the
        # task text guessed. Task 6.2 measured one live ``run-`` row and reported
        # 14 attributes with 8 dropped. Enumerated over the whole **attribute
        # space** instead (full scan of the live executions table, the validation account,
        # 2026-08-25: 61 rows, 28 distinct attributes) the dropped set is **19**,
        # not 8. Every one is dropped for a reason, and the reasons fall into four
        # classes. Widening this projection is what each of them argues against.
        #
        # 1. STRUCTURALLY UNREACHABLE — 7 attributes, and this is the strongest
        #    class because it is not a judgement call. ``content_fingerprint``,
        #    ``previous_run_id``, ``skip_detail``, ``skip_reason``, ``source_key``,
        #    ``triggered_by``, ``ttl`` occur on ``skipped-`` rows and **only** on
        #    ``skipped-`` rows (3 of 3 each; 0 occurrences on any other prefix).
        #    A duplicate skip writes no state rows at all — verified here, not
        #    taken from R12.2: none of the 3 live ``skipped-`` run ids appears in
        #    the state table — so ``get_run_details`` returns 404 at the
        #    ``if not items`` guard before this read is ever reached. Forwarding
        #    them would add keys to a body that cannot exist.
        # 2. A SECOND STATUS AUTHORITY — ``pending_approval_id``,
        #    ``pending_approval_type``. Live on ``run-`` rows and reachable. The
        #    run list DOES read ``pending_approval_id``, to rewrite ``status`` to
        #    ``WAITING_APPROVAL``; this reader derives the same state from the
        #    approvals ``run_id-index`` query above instead. do-NOT 9 / R8.2 forbid
        #    adding a third opinion about status, so these stay withheld.
        # 3. WITHHELD BY AN EXISTING RECORD — ``trigger`` (p38 §5 delta D5:
        #    emitting it makes ``StepFlowVisualization``'s skipped-step display
        #    reachable, a display change no requirement asks for) and
        #    ``retried_by_run_id`` (``LGR-453f12d-5`` — a link renders only from an
        #    identifier that resolves, and under the continuation model there is no
        #    second run to point at; already pinned by
        #    ``test_the_body_carries_no_forward_identifier``).
        # 4. NO DECLARED CONSUMER — ``ai_model_used``, ``current_step``,
        #    ``execution_name``, ``file_name``, ``lineage``, ``schema_hash``,
        #    ``steps_status``, ``updated_at``. ``RunDetail`` declares none of them
        #    and no run-detail component reads them: every ``runDetail.*`` access
        #    in ``dashboard-react/src`` lands on one of the type's 12 fields.
        #    ``LGR-5aad762-1`` (**template / ratified**) is the rule for this file's
        #    sibling reader — a field with no consumer is dead weight, and that key
        #    list "is an allowlist that decides what a row publishes, not a
        #    convenience". Two are near-misses worth naming so they are not
        #    rediscovered: ``execution_name`` and ``steps_status`` ARE read, but
        #    per-attempt inside ``attempts`` nodes (``RunAttempt``), which
        #    ``LGR-8e34f37-3`` already forwards verbatim — not at top level.
        #    ``steps_status`` additionally waits on the legacy-``step_name``
        #    question ``LGR-9e76249-9`` names as its own revisit condition.
        #
        # Not in any class, because they are not dropped: ``pipeline_id``,
        # ``run_id``, ``status``, ``started_at``, ``execution_id`` reach the body
        # from the state rows; ``attempts`` / ``attempt_count`` /
        # ``retry_source_run_id`` / ``completed_at`` ride this projection;
        # ``step_logs`` is read into ``cached_step_logs`` and applied per step.
        #
        # THE DEFECT THIS ENUMERATION ACTUALLY FOUND was in the other direction —
        # a field ``RunDetail`` declares that the response did not always send —
        # and it is fixed at the ``pipeline_key`` derivation further down, not
        # here. That direction is where the silent-``undefined`` bugs live (R3.1),
        # and it is the reason the enumeration was worth doing over the type as
        # well as over the row.
        cached_step_logs: dict[str, Any] = {}
        pipeline_id_for_cache = run_details.get("original_pipeline_id") or run_details.get("pipeline_id", "")
        try:
            if pipeline_id_for_cache:
                cache_resp = executions_table.get_item(
                    Key={"pipeline_id": pipeline_id_for_cache, "run_id": run_id},
                    ProjectionExpression="step_logs, attempts, attempt_count, retry_source_run_id, completed_at",
                )
                # Held once: five reads off one Item, not five calls for the same Item.
                cache_item = cache_resp.get("Item", {})
                cached_step_logs = item_dict(cache_item, "step_logs")
                # R5.1 / R5.2 — the retry lineage reaches the response.
                # ``retry_source_run_id`` is degenerate under the continuation
                # model: it equals ``run_id``, which is why §4.4's frontend guard
                # lands with this reader. Emitted on its own it would turn today's
                # dead link into a self-link — a link that resolves, to the page
                # you are already on. ``trigger`` is deliberately NOT emitted
                # (§5 delta D5): it would make StepFlowVisualization's
                # skipped-step display reachable, a display change no requirement
                # asks for.
                run_details["attempts"] = item_list(cache_item, "attempts")
                run_details["attempt_count"] = item_int(cache_item, "attempt_count")
                run_details["retry_source_run_id"] = item_str(cache_item, "retry_source_run_id")
                # p48 R8.1 — the run's completion time, which the response used to
                # drop. Observed live on ``run-2026-08-24-13-48-26-4fccdf03``: the
                # row carries ``2026-08-24T13:55:19.496708+00:00`` and the panel
                # rendered ``Completed: —``. Assigned here, inside the try, for the
                # reasons in the block comment above.
                run_details["completed_at"] = item_str(cache_item, "completed_at")
        except Exception as cache_err:
            logger.debug(f"Failed to read step_logs cache: {cache_err}")

        # Calculate step durations by measuring time to next step
        from datetime import datetime

        for i, step in enumerate(run_details["steps"]):
            step["start_time"] = step.get("timestamp", "")

            # Calculate duration as time until next step starts
            if i < len(run_details["steps"]) - 1:
                try:
                    current_time = datetime.fromisoformat(step["timestamp"].replace("Z", "+00:00"))
                    next_time = datetime.fromisoformat(run_details["steps"][i + 1]["timestamp"].replace("Z", "+00:00"))
                    duration_seconds = (next_time - current_time).total_seconds()

                    logger.info(
                        f"Step {step['step_name']}: {step['timestamp']} -> {run_details['steps'][i + 1]['timestamp']} = {duration_seconds}s"
                    )

                    step["end_time"] = run_details["steps"][i + 1]["timestamp"]

                    if duration_seconds < 1:
                        step["duration"] = f"{int(duration_seconds * 1000)}ms" if duration_seconds > 0 else "< 1ms"
                    elif duration_seconds < 60:
                        step["duration"] = f"{duration_seconds:.1f}s"
                    else:
                        minutes = int(duration_seconds // 60)
                        seconds = int(duration_seconds % 60)
                        step["duration"] = f"{minutes}m {seconds}s"
                except Exception as e:
                    logger.warning(f"Error calculating duration for step {step['step_name']}: {e!s}")
                    step["duration"] = "N/A"
                    step["end_time"] = step["timestamp"]
            else:
                # Last step - no duration available
                step["end_time"] = step["timestamp"]
                step["duration"] = "N/A"

            # `throughput`, `mcp_latency` and `memory_usage` used to be assigned
            # the literal string "N/A" here, unconditionally, for every step of
            # every run. Nothing ever measured them, so the dashboard rendered
            # "Throughput: N/A/s" on every step card — a metric row that looked
            # like missing data but was a placeholder that had no data source at
            # all. They are removed rather than left in, following the same call
            # already recorded for the metrics endpoint
            # (`tests/contracts/api_responses/test_metrics_contract.py:136`:
            # "Removed (no real data source): mcp_fallback_rate, cost_savings,
            # mcp_latency").
            #
            # `StepFlowVisualization.tsx:143` gates its whole metrics row on
            # `duration_ms != null || throughput != null || mcp_latency_ms !=
            # null`, so dropping `throughput` makes the row disappear rather than
            # render empty. It stays correct if a real measurement is added later:
            # the UI reads `duration_ms` / `mcp_latency_ms`, which is NOT what the
            # two deleted keys were named — `mcp_latency` was never read by
            # anything, and `memory_usage` has no consumer in the frontend at all.
            #
            # This repo already refuses to call an unmeasured rate a throughput
            # elsewhere, with a test enforcing it
            # (`tests/unit/lambdas/dashboard_api/test_metrics_acceptance_labelling.py`,
            # `test_it_refuses_the_word_throughput`),
            # so emitting the word here was inconsistent as well as empty.
            #
            # `mcp_operations` stays: an empty list is a truthful "this step
            # recorded no MCP calls", and the frontend consumes it.
            step["mcp_operations"] = []

        # Fetch step logs in parallel — use cache for completed steps
        def _fetch_logs_for_step(step_info):
            sname = step_info.get("step_name", "")
            cached = cached_step_logs.get(sname)
            if cached is not None:
                return sname, cached, False
            log_lines = get_step_logs(sname, run_id)
            return sname, log_lines, True

        uncached_steps = [s for s in run_details["steps"] if s.get("step_name", "") not in cached_step_logs]
        cached_steps = [s for s in run_details["steps"] if s.get("step_name", "") in cached_step_logs]

        # Apply cached logs immediately
        for step in cached_steps:
            step["logs"] = cached_step_logs.get(step.get("step_name", ""), [])

        # Fetch uncached logs in parallel (max 5 concurrent to avoid throttling)
        if uncached_steps:
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(_fetch_logs_for_step, s): s for s in uncached_steps}
                for future in as_completed(futures):
                    try:
                        sname, log_lines, was_fetched = future.result()
                        step = futures[future]
                        step["logs"] = log_lines
                        # Cache logs for completed steps
                        if was_fetched and log_lines and pipeline_id_for_cache:
                            step_status = step.get("status", "")
                            if step_status in ("success", "failed", "completed", "SUCCEEDED", "FAILED"):
                                with contextlib.suppress(Exception):
                                    executions_table.update_item(
                                        Key={"pipeline_id": pipeline_id_for_cache, "run_id": run_id},
                                        UpdateExpression="SET step_logs.#step = :logs",
                                        ExpressionAttributeNames={"#step": sname},
                                        ExpressionAttributeValues={":logs": log_lines[:50]},
                                    )
                    except Exception:
                        futures[future]["logs"] = []

        # Calculate additional fields for UI
        run_details["total_steps"] = len(run_details["steps"])
        run_details["start_time"] = run_details["started_at"]

        # Calculate end time from latest step
        end_time = ""
        for step in run_details["steps"]:
            step_time = step.get("timestamp", "")
            if step_time and (not end_time or step_time > end_time):
                end_time = step_time
        run_details["end_time"] = end_time

        # ── ``pipeline_key`` comes off the run's OWN rows (p48 task 6.4, R8.1) ──
        #
        # It used to be assigned only inside the ``try`` below, and only when the
        # pipelines table still held a row for ``original_pipeline_id``. That made
        # a field ``RunDetail`` (``types.ts:105``) declares **required** into a
        # conditional one, and ``RunMetadataPanel.tsx:48`` renders it unguarded —
        # ``<Box>{runDetail.pipeline_key}</Box>``, with no ``'—'`` fallback, unlike
        # ``started_at`` and ``completed_at`` two lines below it, which both guard.
        # It does not guard because the type says it never has to. So the Pipeline
        # field rendered blank for exactly the runs whose pipeline had been
        # deleted. This is ``R3.1``'s class — the frontend declares a shape the
        # producer does not keep — one type over from where task 4.2 found it
        # (``PipelineExecution.execution_id``).
        #
        # Measured live, not reasoned about (the validation account, 2026-08-25): **10 of 31**
        # ``original_pipeline_id`` values referenced by state rows have no row in
        # the pipelines table, covering **13 runs**; and 2 further runs
        # (``e2e-cm-db947a9b``, ``e2e-dq-d63e01a8``) carry no
        # ``original_pipeline_id`` at all, so the ``get_item`` below is called with
        # an empty key attribute and raises before the assignment could happen.
        #
        # The read never established this value in the first place: it reads the
        # row AT key ``original_pipeline_id`` and took that row's ``pipeline_id``
        # — the partition key itself — so the answer was always the key it asked
        # with. Deriving it here is therefore not a *default*, which is what
        # ``LGR-7561acb-7`` and ``LGR-44d601e-1`` forbid (a key is removed rather
        # than defaulted when nothing established it). It is the same value by
        # construction, established from the run's own rows.
        #
        # The derivation is the one this repo already uses in three places rather
        # than a new one: ``handlers/files.py:37`` publishes
        # ``item_str(item, "original_pipeline_id") or item_str(item, "pipeline_id")``
        # under this very key name, and ``handlers/approvals.py:295-296`` / ``:320-321``
        # normalise ``pipeline_id`` → ``pipeline_key`` with the comment "DynamoDB
        # uses pipeline_id, frontend expects pipeline_key". ``get_run_details`` was
        # the only producer of ``pipeline_key`` that made it contingent on a second
        # table.
        #
        # ``pipeline_name`` is NOT given the same treatment and stays inside the
        # try: nothing but that row establishes a pipeline's display name, so its
        # absence is real not-established (``LGR-7561acb-7``). It is also not
        # declared on ``RunDetail`` at all, so no consumer is promised it.
        run_details["pipeline_key"] = run_details["original_pipeline_id"] or run_details["pipeline_id"]

        # Get pipeline name from pipelines table if available
        try:
            pipeline_response = pipelines_table.get_item(Key={"pipeline_id": run_details["original_pipeline_id"]})
            if "Item" in pipeline_response:
                run_details["pipeline_name"] = pipeline_response["Item"].get("pipeline_name", "")
        except Exception as e:
            logger.warning(f"Could not fetch pipeline name: {e!s}")

        # Fetch approvals for this run and parse approval_data into details.
        # Query the run_id GSI instead of scanning the table (P28 R17.1).
        try:
            approval_items = approvals_table.query(
                IndexName="run_id-index",
                KeyConditionExpression=Key("run_id").eq(run_id),
            ).get("Items", [])
            parsed = []
            for a in approval_items:
                details: dict[str, Any] = {}
                raw = a.get("approval_data")
                if raw:
                    try:
                        details = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except (json.JSONDecodeError, TypeError):
                        details = {}
                a["details"] = details
                a.setdefault("gate_name", a.get("approval_type"))
                a["decided_by"] = a.get("approved_by") or a.get("rejected_by")
                # p50 C3 / R2.2 — the human-meaningful half of the same identity,
                # synthesised the same way and for the same reason.
                # ``LGR-c58f276-2`` makes ``decided_by`` a READ-PATH synthesis over
                # arm-specific persisted names ("rows carry ``approved_by`` /
                # ``rejected_by``, and the ``decided_by`` the frontend reads is
                # synthesised on the read path"), so the email stays arm-specific
                # in storage too — task 3.1 writes ``approved_by_email`` /
                # ``rejected_by_email`` rather than a stored ``decided_by_email``,
                # and the collapse happens here. Same ``or``, no new mechanism.
                #
                # ADDITIVE (R3.3): ``decided_by`` above still carries the Cognito
                # ``sub`` and is untouched. This is a second field, never a
                # replacement — anything reading ``decided_by`` as a stable id
                # keeps working.
                #
                # Falsiness is load-bearing, not incidental. 3.1 omits its
                # attribute when the email is empty rather than writing it blank
                # (``LGR-c58f276-2``: "an audit row never asserts an identity it
                # does not have"), and **every row written before 3.1 carries
                # neither** — which is the dominant state. Both cases land here as
                # ``None``, which is what lets the UI fall through to the ``sub``
                # (R2.3, CP5) instead of rendering blank.
                a["decided_by_email"] = a.get("approved_by_email") or a.get("rejected_by_email")
                a["decided_at"] = a.get("approved_at") or a.get("rejected_at")
                a["comment"] = a.get("comments") or a.get("rejection_reason")
                parsed.append(a)
            run_details["approvals"] = parsed
        except Exception as e:
            logger.warning(f"Could not fetch approvals for run {run_id}: {e}")
            run_details["approvals"] = []

        # ── The reuse fact, derived on the READ PATH (p54 R2.1–R2.3, design 4.2) ──
        #
        # ``reused_mapping: true`` is produced at ``orchestration-stack.ts:445`` and
        # has zero readers repo-wide. It is NOT threaded through ASL to get here,
        # and that is a fence rather than a preference: the key exists on the
        # ``ReuseExistingMapping`` **Pass arm only** — the SchemaMapping Lambda's
        # response dict does not carry it — so a JSONPath reference to it would be
        # a ``States.Runtime`` failure on every non-reuse run. Invariant
        # ``LGR-5c539a6-2`` is exactly that rule ("every key an ASL path reads off
        # ``$.schema_extract_result`` exists on both arms"), and p54 do-NOT 1
        # restates it for this key.
        #
        # So it is derived here instead, which is ``LGR-9e76249-2``'s ratified
        # shape, chosen there for the reason that applies verbatim here:
        # ``get_run_details`` "already holds every state row for the run — so the
        # read is free and 'derived once' holds **by construction** rather than by
        # discipline". This block adds **zero** DynamoDB operations.
        #
        # ── THREE STATES, and the third one is OMISSION ──────────────────────
        #
        #   ``False``        a ``schema_mapping`` state row IS present. The run
        #                    generated its own mapping; that is knowledge and
        #                    ``False`` states it.
        #   ``True`` + id    no such row AND an id resolves.
        #   both keys off    no such row and no id resolves. Absence means NOT
        #                    ESTABLISHED.
        #
        # ``False`` is NEVER the unknown case. It is asserted only from the
        # positive evidence of a row being there, never as a default, because
        # ``False`` on a run whose reuse could not be established would assert
        # that the run generated its own mapping. That collapse is p54 do-NOT 7,
        # and ``LGR-b2f79ef-14`` is its strongest in-tree form — a named reference
        # to something absent is an inconsistent record, not a thing that never
        # existed. ``LGR-99050e6-2`` ("an absent counter is reported as unknown
        # rather than as zero") and ``LGR-44d601e-1`` (a value is cleared by
        # REMOVING the key, so no reader must distinguish ``""`` from absence) are
        # the same rule for the two-valued and string cases.
        #
        # ── ``items``, NOT ``run_details["steps"]``, and this is load-bearing ──
        #
        # The two disagree on precisely the run this requirement is about. The
        # running-step injection above APPENDS a synthetic ``schema_mapping``
        # entry to ``steps`` when a pending approval maps to that step
        # (``approval_step_map``, and the ``else`` clause of the for/else that
        # follows it) — no state row required. A Reuse_Run parked at the
        # schema-mapping gate is exactly that case, so deriving from ``steps``
        # would report ``reused_mapping: False`` for the canonical Reuse_Run.
        # ``items`` is the state table's own answer and cannot be synthesised.
        #
        # ── Canonical comparison, not the raw ``step_name`` ──────────────────
        #
        # The row-picking idiom is the one this function already uses for the
        # ``load_metrics`` / DQ rows above, but the comparison runs in canonical
        # space like the step-specific branches do (P28 R8.2), so a pre-rename
        # PascalCase ``SchemaMapping`` row IS recognised where a raw
        # ``== "schema_mapping"`` would miss it and report ``True`` for a run
        # that generated its own mapping. That is a false positive on the
        # consequential arm, on live rows.
        #
        # CORRECTION (p54 task 7.6; found by task 3.2, confirmed independently
        # by 3.3). This note used to give the wrong mechanism for that
        # tolerance: it said ``step_names._LEGACY_ALIASES`` maps
        # ``SchemaMapping``. **It does not.** ``_LEGACY_ALIASES``
        # (``shared/step_names.py:155-175``) holds exactly ``CodeGeneration``,
        # ``CodeTesting``, ``schema_mapping_approval``, ``code_approval``,
        # ``infrastructure`` and ``first_run`` — neither ``SchemaMapping`` nor
        # ``SchemaExtract`` is among them, and ``schema_mapping_approval`` maps
        # to ``CHECK_SCHEMA_MAPPING_APPROVAL``, the GATE rather than the step.
        # The tolerance is real but comes from elsewhere: ``_normalize``
        # (``shared/step_names.py:178-180``) strips ``_`` and ``-`` and
        # lowercases before lookup, so ``SchemaMapping`` and ``schema_mapping``
        # both normalise to ``schemamapping`` and resolve through the CANONICAL
        # name. Same effect, different mechanism — ``_canonical_or_raw`` stays
        # correct and only the stated reason was wrong. Worth stating because a
        # reader who checked the alias table would conclude the tolerance was
        # absent and "fix" a comparison that is already right.
        #
        # The two ``_LOAD_METRICS_STEP`` / ``_DQ_REPORT_STEP`` reads above
        # compare raw because those are record types with no legacy spelling.
        #
        # ── ONE id source, not two: the ``create_code`` route DOES NOT EXIST ──
        #
        # p54 task 3.1 and design 4.2 both name a second source — "the run's
        # ``create_code`` state row's ``mapping_id``
        # (``create_code/lambda_function.py:1255``)" — for historical Reuse_Runs.
        # Verified against the writer and it is a misattribution: ``:1255`` is
        # inside ``generated_code_table.put_item``, a row on the GENERATED-CODE
        # table keyed ``(PK=CODE#{code_version_id}, SK=VERSION)``. The
        # ``create_code`` STATE row is written at ``:1370`` and its attributes are
        # ``run_id`` / ``step_name`` / ``status`` / ``step_sequence`` /
        # ``timestamp`` / ``pipeline_id`` / ``original_pipeline_id`` /
        # ``regeneration_attempt`` / ``output_data`` — no ``mapping_id``, at top
        # level or inside ``output_data`` (``create_output_data`` is fixed to five
        # keys by ``LGR-60c4985-4``; the ``summary`` it builds carries three
        # counts; ``create_approval_payload`` carries no ``mapping_id`` either).
        #
        # Swept the whole space rather than that one file: the ONLY writer of
        # ``mapping_id`` onto a state row anywhere is
        # ``schema_mapping/lambda_function.py:1935`` — the ``schema_mapping`` row,
        # which is by definition the row a Reuse_Run does not have.
        # ``schema_compare`` runs on both arms and resolves the candidate id
        # (``:765``) but does not persist it: its state item (``:480-536``) has no
        # ``existing_mapping_id``, and the approvals row itself carries no
        # top-level ``mapping_id`` — only ``approval_data`` as JSON
        # (``approval_handler:828-857``).
        #
        # THE CONSEQUENCE, recorded rather than papered over: a Reuse_Run whose
        # approval predates p54 task 2.2 resolves NO id, so it takes the
        # **omission** state. That is the honest answer for it — nothing on the
        # read path establishes which mapping it reused — and it is why the third
        # state exists. Reaching those runs would need a read of a fourth table
        # (generated-code, by ``code_version_id``), which is a new call on a read
        # path this block deliberately adds none to; that is its own change with
        # its own budget argument.
        #
        # ── The one source, and why it costs nothing ─────────────────────────
        #
        # ``approval_data.reused_mapping_id``, appended by p54 task 2.2 on BOTH of
        # ``_reused_mapping_payload``'s reuse states — the resolved one
        # (``approval_handler:492``) and the unreadable one (``:438``) — so an
        # approval whose origin payload could not be chased still names the
        # mapping. Available from the moment the gate fires, which is when the
        # approver is looking.
        #
        # design's §Risks note flags this route as "named as the place the
        # projection attaches, not as verified-compatible". Verified: **no
        # projection needs attaching**. The approvals query feeding
        # ``run_details["approvals"]`` above carries no ``ProjectionExpression``
        # at all, so every attribute including ``approval_data`` is already in
        # hand, and the block above has already parsed it into ``details``. This
        # reads that parsed dict. The two NARROW approvals queries earlier in this
        # function are deliberately not used: one projects ``approval_type`` and
        # fires only for an active execution, the other projects ``approval_id``.
        #
        # ── Its own ``try``, following ``LGR-8e34f37-3`` ─────────────────────
        #
        # Both keys are assigned INSIDE this block, so anything that raises here
        # leaves them off the body while the response stays 200 — the omit-on-
        # failure pattern that record pins for this function's three lineage
        # keys, for the same three-state reason. Its own ``try`` rather than the
        # approvals one above, on ``LGR-67bf5d3-5`` ground (2): a key assigned
        # inside a wider ``try`` has its presence made contingent on code with
        # nothing to do with reading it. This block holds one row scan and one
        # list walk, so absence here means exactly one thing.
        try:
            _schema_mapping_row = next(
                (i for i in items if _canonical_or_raw(item_str(i, "step_name")) == StepName.SCHEMA_MAPPING),
                None,
            )
            if _schema_mapping_row is not None:
                run_details["reused_mapping"] = False
            else:
                _reused_mapping_id = ""
                for _approval in run_details.get("approvals", []):
                    _reused_mapping_id = item_str(item_dict(_approval, "details"), "reused_mapping_id")
                    if _reused_mapping_id:
                        break
                if _reused_mapping_id:
                    run_details["reused_mapping"] = True
                    run_details["reused_mapping_id"] = _reused_mapping_id
        except Exception as reuse_err:
            logger.debug(f"Failed to derive the reuse fact for run {run_id}: {reuse_err}")
            # Both keys off, never a default. See the three-state note above.
            run_details.pop("reused_mapping", None)
            run_details.pop("reused_mapping_id", None)

        # ── ``schema_version``, DERIVED — there is nothing to project (p54 R4) ──
        #
        # ``schema_version`` is **not a stored attribute with a missing
        # projection**. It is a read-path derivation with no producer at all, and
        # task 3.2 re-ran p54 C-2's negative search over the whole tree before
        # building this — including ``mcp/`` and the Glue templates, which the
        # original search did not cover. The result, so a later reader does not
        # re-run it:
        #
        #   ``mcp/``, ``pyspark_templates/``, ``shared/``, ``deploy-cdk/lib``,
        #   ``deploy-cdk/bin``  →  ZERO occurrences of ``schema_version``.
        #   ``lambda/``          →  four files, none a state-row writer:
        #     * ``execution_updater:346-357`` READS ``schema_version`` off the
        #       stream image and falls back to ``output_data.summary
        #       .schema_version``, then writes it onto the executions row with
        #       ``if_not_exists``. Both sources are producerless, so the guard at
        #       ``:355`` is never true and the executions row's stored
        #       ``schema_version`` is never populated either;
        #     * ``schema_extract`` — the row this run's version is ABOUT — writes
        #       ``schema_hash`` (``:1339``, ``:1353``) and a three-key ``summary``
        #       of ``field_count`` / ``schema_hash`` / ``is_new_schema``
        #       (``:1337-1341``). No version ordinal, at top level or in
        #       ``output_data``;
        #     * ``job_completion`` / ``schema_compare`` touch
        #       ``target_schema_version`` on the PIPELINES table — a different
        #       attribute on a different table, not this one;
        #     * ``dashboard_api``'s own occurrences are all read-path derivations
        #       (``pipelines_crud`` response bodies, ``glue_jobs:175``'s response
        #       entry, and the executions enrichment at ``:1146-1157``).
        #
        # So do-NOT 3 stands and the task did NOT get smaller: a projection off the
        # ``schema_extract`` row would return absent on every run while LOOKING like
        # a working read — a silent no-op. The only thing that produces the number
        # both run lists render is ``_schema_hash_versions``, and this is its second
        # caller. ``LGR-5d68eeb-3``'s class — a reader with no producer.
        #
        # ── R4.4 IS OMISSION, and every failure mode lands there ─────────────
        #
        # ``run_details["schema_version"]`` is set **only** when the run's hash is a
        # key of the returned map. Four distinct ways it is not, all one outcome:
        #
        #   1. the run has no ``schema_extract`` row (a Skipped_Run, a run that
        #      failed before extraction);
        #   2. the row carries no ``schema_hash``;
        #   3. the hash is genuinely unknown to the partition;
        #   4. the walk was TRUNCATED by ``_MAX_SCHEMA_VERSION_PAGES`` or by the
        #      deadline, so the map is short.
        #
        # In all four the key is **absent**. Never ``''``, never ``0``, never
        # ``None``. ``LGR-44d601e-1`` is the rule — a value is cleared by REMOVING
        # the key, "precisely so no reader has to distinguish ``\"\"`` from
        # absence" — and ``LGR-7561acb-7`` applies the identical reasoning to
        # ``nullable``, rejecting a third spelling of two facts outright. It is also
        # already how the executions read behaves at ``:1152-1155``, so R4.3's "one
        # key, both surfaces" holds for the ABSENT case as well as the present one.
        #
        # Case 4 deserves its own sentence because the executions read handles it
        # differently and that difference is deliberate. There, a short map is named
        # in ``pagination.partial_counts`` via
        # ``_SCHEMA_VERSION_DERIVED_KEY_ORDER``. **This response has no
        # ``pagination`` envelope and must not gain one** — ``RunDetail`` is a single
        # object, ``partial_counts`` is a property of a page, and inventing an
        # envelope key here would be the "third spelling" ``LGR-7561acb-7`` refuses.
        # ``_SCHEMA_VERSION_DERIVED_KEY_ORDER`` stays the executions envelope's
        # business and is not read here. The honest expression of a truncated walk
        # on this surface is the same omission as an unknown hash, and the loss is
        # bounded the same way: ``_schema_hash_versions``' numbers are ordinals of
        # first appearance over an ASCENDING read, so a truncated map is a prefix
        # and every number in it is the number the whole map would have carried.
        # Truncation loses entries, never corrupts one (the module comment above
        # ``_MAX_SCHEMA_VERSION_PAGES`` argues this at length).
        #
        # ── THE BUDGET: reuse ``_schema_version_walk_deadline``, own anchor ───
        #
        # Settled by reading both, not assumed. The function is written for
        # ``get_pipeline_executions`` and its 12.0 s share carries that handler's
        # arithmetic in its own docstring: "this read is third of four and has two
        # reads behind it", and 12.0 "splits the 10 s that remain after the filter
        # walk's share and the 2 s reserve". Neither the filter walk nor the count
        # exists here, so that carve-out reasoning does not transfer.
        #
        # It is reused anyway, and the anchor is what differs:
        #
        # * The CONSTANTS transfer even though the carve-out does not.
        #   ``_RUNS_TIME_BUDGET_SECONDS`` (20.0) and ``_RUNS_READ_RESERVE_SECONDS``
        #   (2.0) are shares of the SAME 25 s ceiling on the SAME Lambda — both
        #   handlers are routes into ``dashboard_api``. A second constant would be a
        #   second authority for one figure, which is what ``LGR-9e76249-2`` and
        #   ``LGR-28b4d75-1`` reject for derivable numbers, and it would drift the
        #   moment one was retuned.
        # * The 12.0 s is a CEILING here rather than a carve-out, and it is
        #   generous: ``_schema_hash_versions`` returns ``(versions, True)`` on the
        #   first page with no ``LastEvaluatedKey``, which is the common case.
        # * **This walk is the LAST read in this function**, verified by reading:
        #   the state query (``:1455``), the injection ``get_item`` (``:1698``), two
        #   narrow approvals queries (``:1718``, ``:1813``), the lineage
        #   ``get_item`` and its ``step_logs`` ``update_item`` (``:2078``,
        #   ``:2178``), the pipelines ``get_item`` (``:2245``) and the approvals
        #   query (``:2254``) are all above it; nothing reads or writes below. That
        #   is exactly the condition ``_runs_read_deadline``'s docstring names as
        #   the reason a walk needs no carve-out of its own — "the count is the
        #   **last** read in the function, so it is the one that yields".
        #
        # The COST PROFILE does differ and it is not hidden: this is a per-RUN read
        # where the executions enrichment is per-PAGE, so the same partition walk
        # buys one version number instead of up to ten. ``requirements.md`` calls
        # that "a poor shape for resolving one row" — true, and it would be the
        # argument against this design if a stored attribute existed. None does, so
        # the choice is this walk or not answering R4 at all (design 4.4).
        #
        # ── ``items``, canonical comparison, and a correction ────────────────
        #
        # ``items`` rather than ``run_details["steps"]``, for 3.1's reason: the
        # running-step injection APPENDS synthetic entries to ``steps``, so ``steps``
        # can name a step that wrote no row. ``items`` is the state table's own
        # answer.
        #
        # The comparison runs in canonical space via ``_canonical_or_raw``, like the
        # step-specific branches at ``:1523-1527`` (P28 R8.2), so a pre-rename
        # PascalCase ``SchemaExtract`` row is recognised. **Correction to 3.1's
        # in-code note beside it:** that block says
        # ``step_names._LEGACY_ALIASES`` maps ``SchemaMapping``. It does not —
        # ``_LEGACY_ALIASES`` (``shared/step_names.py:155-175``) holds exactly
        # ``CodeGeneration``, ``CodeTesting``, ``schema_mapping_approval``,
        # ``code_approval``, ``infrastructure`` and ``first_run``, and neither
        # ``SchemaMapping`` nor ``SchemaExtract`` is among them. The tolerance is
        # real but comes from elsewhere: ``_normalize`` (``:178-180``) strips ``_``
        # and lowercases before lookup, so ``SchemaExtract`` and ``schema_extract``
        # both normalise to ``schemaextract`` and resolve through the CANONICAL
        # name. Same effect, different mechanism — worth stating because a reader
        # who checked the alias table would conclude the tolerance was absent.
        #
        # ``run_details["pipeline_id"]`` is the right partition key by construction,
        # not by coincidence: it is read off this run's OWN state rows, and
        # ``PipelineIndex`` — the GSI ``_schema_hash_versions`` walks — is hash-keyed
        # on that same attribute of that same table.
        #
        # ── NOT widened: either executions ``get_item`` (p54 do-NOT 2) ───────
        #
        # The injection read's three-name ``ProjectionExpression`` (``:1704``) is
        # untouched. Its in-code comment forbids widening by name and the fence is
        # ASSERTED — ``test_run_details_keyed_executions_lookup.py::
        # test_the_injection_read_asks_only_for_what_it_uses`` pins it to exactly
        # those three. The lineage read's five-name projection (``:2080``) is
        # untouched too, and widening it would be pointless rather than merely
        # forbidden: the attribute has no writer, so it would project nothing.
        #
        # ── Its own ``try`` (``LGR-8e34f37-3``, ``LGR-67bf5d3-5`` ground 2) ──
        #
        # The key is assigned INSIDE, so anything raising here leaves it off the
        # body while the response stays 200 — the omit-on-failure pattern
        # ``LGR-8e34f37-3`` pins for this function's three lineage keys. Its own
        # ``try`` and not the reuse block's above, because a key assigned inside a
        # wider ``try`` has its presence made contingent on code with nothing to do
        # with reading it. Note the degrade is IDENTICAL to the four omission cases
        # listed earlier, which is why no marker distinguishes them: from the
        # response, "the walk could not run" and "the hash is not in the map" mean
        # the same thing — no version is established for this run.
        try:
            _schema_extract_row = next(
                (i for i in items if _canonical_or_raw(item_str(i, "step_name")) == StepName.SCHEMA_EXTRACT),
                None,
            )
            _run_schema_hash = item_str(_schema_extract_row, "schema_hash") if _schema_extract_row else ""
            _run_pipeline_id = run_details.get("pipeline_id") or ""
            if _run_schema_hash and _run_pipeline_id:
                # The wanted set is this ONE hash, which is the whole reason the
                # parameter exists. An ordinal is fixed at a hash's first appearance,
                # so the walk can stop the moment it meets this one and the number is
                # the number the whole-partition walk would have produced. For the
                # common case — a pipeline whose schema has not changed, so its hash
                # first appears in the oldest ``schema_extract`` row — that is ONE
                # page read instead of a walk of the entire partition on every
                # run-detail view. ``requirements.md`` called a whole-partition walk
                # "a poor shape for resolving one row"; this is the same read asked a
                # narrower question, not a different source.
                _hash_versions, _ = _schema_hash_versions(
                    _run_pipeline_id,
                    _schema_version_walk_deadline(_request_started),
                    frozenset({_run_schema_hash}),
                )
                # The ONLY assignment. A hash the map does not carry — unknown, or
                # cut short by the page cap or the deadline — leaves the key absent.
                if _run_schema_hash in _hash_versions:
                    run_details["schema_version"] = _hash_versions[_run_schema_hash]
        except Exception as schema_version_err:
            logger.debug(f"Failed to derive the schema version for run {run_id}: {schema_version_err}")
            # Absent, never a substitute. R4.4, ``LGR-44d601e-1``.
            run_details.pop("schema_version", None)

        logger.info(f"Built run details with {len(run_details['steps'])} steps")

        return create_response(200, run_details)

    except Exception as e:
        logger.error(f"Error getting run details: {e!s}")
        return handle_error(e, f"Error getting run details for {run_id}")


# ── Run cancellation ────────────────────────────────────────────────────
# A run is stopped in one of two ways, decided by whether its Glue job is still
# doing work.
#
# * **No Glue job running** (the run is at a Lambda step or an approval gate):
#   StopExecution, then the executions row goes straight to CANCELLED. 200.
# * **Glue job running**: the job is stopped and the row goes to CANCELLING. The
#   Step Functions execution is deliberately left WAITING on the Glue callback:
#   when Glue reports the job stopped, job_completion finds the cancel marker on
#   the run's Glue-callback token row, stops the execution itself and writes the
#   run_pipeline state row ``cancelled``, which execution_updater turns into
#   CANCELLED. 202.
#
# The deferral is what keeps the pipeline lock held until Glue has actually
# stopped. The lock is released by SfnTerminalLockRelease when the execution goes
# ABORTED; aborting it here, while a Glue stop can take minutes, would free the
# lock with the job still running, and the pipeline's next run would hit Glue's
# MaxConcurrentRuns of 1 and fail. The 24 h lock TTL stays the backstop.
#
# A cancelled run never reaches RecordFailure, because an aborted execution runs
# no Catch, so it publishes no error-rate datapoint and dispatches no self-healing.
#
# Rejected: waiting for the Glue stop inside this request (a Glue stop can take
# minutes; the API times out at about 25 s), and aborting the execution at once
# and releasing the lock from job_completion (it needs the lock key, which for an
# S3 trigger is a source-key prefix nothing downstream records, and a second
# release owner beside LockReleaseFunction).

#: Executions-row statuses a cancel acts on. Anything else is either already a
#: cancellation (answered idempotently) or finished (409).
_CANCELLABLE_RUN_STATUSES = ("RUNNING", "IN_PROGRESS", "WAITING_APPROVAL")

#: Glue JobRunState values for a job run that is still doing, or about to do, work.
_GLUE_ACTIVE_STATES = frozenset({"STARTING", "RUNNING", "WAITING", "STOPPING"})

#: The error StopExecution records on a cancelled execution.
_CANCEL_ERROR = "UserCancelled"

#: TTL on a cancel fence row the cancel created itself (the launcher had not
#: written the token row yet). The launcher only needs to see it within its
#: own invocation, so a day is generous; the approval-path token TTL is 7 d.
_CANCEL_FENCE_TTL_SECONDS = 24 * 60 * 60


def _execution_arn_from_ref(ref: str) -> str:
    """A Step Functions execution ARN from what a row stores.

    The state machine hands the step Lambdas ``$$.Execution.Name`` under the key
    ``execution_arn`` (orchestration-stack.ts), so ``execution_id`` holds a bare
    execution NAME, and so does the retry path's ``execution_name``. StopExecution
    and DescribeExecution take an ARN. Before 2026-10-11 this function's caller
    passed the name, got InvalidArn, swallowed it and reported the run cancelled.
    """
    if ref.startswith("arn:"):
        return ref
    state_machine_arn = require_env("STATE_MACHINE_ARN")
    return f"{state_machine_arn.replace(':stateMachine:', ':execution:', 1)}:{ref}"


def _client_error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", ""))
    return ""


def _find_execution_row(run_id: str) -> dict[str, Any] | None:
    """The run's executions row, read consistently.

    ``run_id`` is only the table's sort key, so the partition comes from the
    ``run_id-index`` GSI (P28 R17.1/R17.2 added it for this access pattern), and
    the row itself from a consistent GetItem, because the GSI image can lag the
    status this function is about to act on.
    """
    hits = executions_table.query(
        IndexName="run_id-index",
        KeyConditionExpression=Key("run_id").eq(run_id),
        ProjectionExpression="pipeline_id, run_id",
    ).get("Items", [])
    for hit in hits:
        pipeline_id = item_str(hit, "pipeline_id")
        if not pipeline_id:
            continue
        row = executions_table.get_item(Key={"pipeline_id": pipeline_id, "run_id": run_id}, ConsistentRead=True).get(
            "Item"
        )
        if row:
            return row
    return None


def _run_pipeline_state_row(run_id: str) -> dict[str, Any] | None:
    """The run's Glue step row: canonical ``run_pipeline``, else the legacy ``RunPipeline``."""
    for step_name in (str(StepName.RUN_PIPELINE), "RunPipeline"):
        item = state_table.get_item(Key={"run_id": run_id, "step_name": step_name}, ConsistentRead=True).get("Item")
        if item:
            return item
    return None


def _cancel_response(status_code: int, run_id: str, status: str, message: str) -> dict[str, Any]:
    return create_response(status_code, {"success": True, "run_id": run_id, "status": status, "message": message})


def _cancel_conflict(run_id: str, status: str, message: str) -> dict[str, Any]:
    return create_response(409, {"error": message, "run_id": run_id, "status": status})


def _answer_for_settled_status(run_id: str, status: str) -> dict[str, Any] | None:
    """The response for a run a cancel cannot act on, or None when it can."""
    if status == "CANCELLED":
        return _cancel_response(200, run_id, "CANCELLED", "Run is already cancelled")
    if status == "CANCELLING":
        return _cancel_response(
            202, run_id, "CANCELLING", "The Glue job is already stopping; the run becomes CANCELLED when it has stopped"
        )
    if status not in _CANCELLABLE_RUN_STATUSES:
        return _cancel_conflict(run_id, status, f"Run is not active (status {status or 'unknown'}); nothing to cancel")
    return None


def _answer_after_lost_race(run_id: str) -> dict[str, Any]:
    """A conditional cancel write lost to a concurrent status change: answer from the row as it is now."""
    row = _find_execution_row(run_id)
    status = item_str(row, "status") if row else ""
    settled = _answer_for_settled_status(run_id, status)
    return settled or _cancel_conflict(
        run_id, status, "Run changed state while it was being cancelled; refresh and retry"
    )


def _mark_cancelled(pipeline_id: str, run_id: str, now: str) -> None:
    """RUNNING/IN_PROGRESS/WAITING_APPROVAL → CANCELLED. Raises ConditionalCheckFailed if the run moved on.

    The pending-approval fields go too: the run list rewrites any row carrying
    ``pending_approval_id`` to WAITING_APPROVAL, which would hide the cancel.
    """
    executions_table.update_item(
        Key={"pipeline_id": pipeline_id, "run_id": run_id},
        UpdateExpression=(
            "SET #s = :cancelled, updated_at = :now, completed_at = :now, cancel_requested_at = :now "
            "REMOVE pending_approval_id, pending_approval_type"
        ),
        ConditionExpression="attribute_exists(run_id) AND #s IN (:running, :in_progress, :waiting)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":cancelled": "CANCELLED",
            ":now": now,
            ":running": "RUNNING",
            ":in_progress": "IN_PROGRESS",
            ":waiting": "WAITING_APPROVAL",
        },
    )


def _mark_cancelling(pipeline_id: str, run_id: str, now: str) -> None:
    """RUNNING/IN_PROGRESS/WAITING_APPROVAL → CANCELLING. Raises ConditionalCheckFailed if the run moved on."""
    executions_table.update_item(
        Key={"pipeline_id": pipeline_id, "run_id": run_id},
        UpdateExpression="SET #s = :cancelling, updated_at = :now, cancel_requested_at = :now",
        ConditionExpression="attribute_exists(run_id) AND #s IN (:running, :in_progress, :waiting)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":cancelling": "CANCELLING",
            ":now": now,
            ":running": "RUNNING",
            ":in_progress": "IN_PROGRESS",
            ":waiting": "WAITING_APPROVAL",
        },
    )


def _restore_after_failed_cancel(pipeline_id: str, run_id: str, prior_status: str) -> None:
    """Undo :func:`_mark_cancelling` when the Glue job could not be stopped. Best-effort.

    Conditional on CANCELLING, so a status execution_updater wrote meanwhile (the
    job finishing on its own) is never overwritten.
    """
    try:
        executions_table.update_item(
            Key={"pipeline_id": pipeline_id, "run_id": run_id},
            UpdateExpression="SET #s = :prior, updated_at = :now REMOVE cancel_requested_at",
            ConditionExpression="attribute_exists(run_id) AND #s = :cancelling",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":prior": prior_status,
                ":now": datetime.now(UTC).isoformat(),
                ":cancelling": "CANCELLING",
            },
        )
    except Exception as restore_err:
        logger.warning(f"Could not restore run {run_id} to {prior_status} after a failed cancel: {restore_err}")


#: Key prefix of the run's Glue-callback token row in the task-tokens table, the
#: same literal as GLUE_RUN_TOKEN_KEY_PREFIX in run_pipeline and job_completion.
#: The two writes below spell the key as a dict literal so the executions-writer
#: sweep (tests/unit/lambdas/test_executions_started_at_invariant.py) can see
#: they target another table.
_GLUE_CALLBACK_KEY_PREFIX = "glue_run#"


def _audit_cancel(event: dict[str, Any] | None, run_id: str, details: dict[str, Any]) -> None:
    """Record the cancel. Never fails the request: the cancel has already taken effect."""
    try:
        write_audit_entry(
            action="run_cancelled",
            resource_type="run",
            resource_id=run_id,
            details=details,
            **extract_request_context(event or {}),
        )
    except Exception as audit_err:
        logger.warning(f"Failed to audit the cancel of run {run_id}: {audit_err}")


def cancel_run(run_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /runs/{runId}/cancel. See the "Run cancellation" block above for the two paths.

    Responses: 200 CANCELLED (stopped now, or already cancelled), 202 CANCELLING
    (the Glue job is stopping), 404 (no such run), 409 (finished, or a state a
    cancel cannot act on yet), 500 (an AWS call failed; nothing was marked).
    """
    try:
        logger.info(f"Cancelling run: {run_id}")

        row = _find_execution_row(run_id)
        if row is None:
            return create_response(404, {"error": f"Run not found: {run_id}", "run_id": run_id})
        pipeline_id = item_str(row, "pipeline_id")
        status = item_str(row, "status")
        settled = _answer_for_settled_status(run_id, status)
        if settled is not None:
            return settled

        # The execution to stop. A retry records its own execution's name on the
        # row (retry_run), and execution_updater copies the first execution's name
        # into ``execution_id`` with if_not_exists, so the retry's name wins.
        execution_ref = item_str(row, "execution_name") or item_str(row, "execution_id")
        if not execution_ref:
            fv_row = state_table.get_item(
                Key={"run_id": run_id, "step_name": str(StepName.FILE_VALIDATION)}, ConsistentRead=True
            ).get("Item")
            execution_ref = item_str(fv_row, "execution_id") if fv_row else ""
        if not execution_ref:
            return _cancel_conflict(
                run_id, status, "Run has no recorded Step Functions execution, so there is nothing to stop"
            )
        execution_arn = _execution_arn_from_ref(execution_ref)

        sfn_client = boto3.client("stepfunctions")
        try:
            execution_status = str(sfn_client.describe_execution(executionArn=execution_arn).get("status", ""))
        except ClientError as describe_err:
            if _client_error_code(describe_err) == "ExecutionDoesNotExist":
                return _cancel_conflict(
                    run_id, status, f"The run's Step Functions execution does not exist: {execution_arn}"
                )
            raise
        # ABORTED: something stopped the execution without marking the run (the
        # console, or a cancel from before 2026-10-11). The Glue job may still be
        # running, so it goes through the same check as a RUNNING execution.
        if execution_status not in ("RUNNING", "ABORTED"):
            return _cancel_conflict(
                run_id,
                status,
                f"The run's execution has already ended ({execution_status}); its status will update shortly",
            )

        rp_row = _run_pipeline_state_row(run_id)
        glue_job_name = item_str(rp_row, "glue_job_name") if rp_row else ""
        glue_job_run_id = item_str(rp_row, "glue_job_run_id") if rp_row else ""
        rp_status = item_str(rp_row, "status") if rp_row else ""
        if glue_job_name and not glue_job_run_id and rp_status.upper() == "RUNNING":
            # run_pipeline writes the row, then starts the job, then attaches its
            # id. Stopping the execution here would leave the job it is about to
            # start running unsupervised.
            return _cancel_conflict(run_id, status, "The Glue job is starting; try again in a few seconds")

        glue_client = boto3.client("glue")
        glue_state = ""
        if glue_job_name and glue_job_run_id:
            glue_state = str(
                glue_client.get_job_run(JobName=glue_job_name, RunId=glue_job_run_id)
                .get("JobRun", {})
                .get("JobRunState", "")
            )
        if glue_state == "SUCCEEDED" and execution_status == "RUNNING":
            # The load has landed and job_completion is about to close the run.
            return _cancel_conflict(run_id, status, "The Glue job has already succeeded; the run is finishing")

        now = datetime.now(UTC).isoformat()

        if glue_state not in _GLUE_ACTIVE_STATES:
            # The cancel fence, written BEFORE the execution is stopped. The
            # RunPipeline Lambda may already be running with no row written yet
            # (its invocation precedes its first write by up to a second), and
            # stopping the execution does not stop that Lambda. run_pipeline
            # makes both of its token-row writes conditional on this attribute
            # being absent, so once it is here the launcher either never starts
            # the job or stops the one it just started (assessment F1). The row
            # is created if the launcher has not written it yet and stamped if
            # it has; either way the launcher sees the fence on its next write.
            tokens_table = dynamodb.Table(require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"))
            tokens_table.update_item(
                Key={"approval_id": f"{_GLUE_CALLBACK_KEY_PREFIX}{run_id}"},
                UpdateExpression=(
                    "SET cancel_requested_at = :now, cancel_execution_arn = :arn, "
                    "run_id = if_not_exists(run_id, :run_id), #ttl = if_not_exists(#ttl, :ttl)"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":now": now,
                    ":arn": execution_arn,
                    ":run_id": run_id,
                    ":ttl": int(time.time()) + _CANCEL_FENCE_TTL_SECONDS,
                },
            )
            if execution_status == "RUNNING":
                sfn_client.stop_execution(
                    executionArn=execution_arn, error=_CANCEL_ERROR, cause="Cancelled from the dashboard"
                )
            try:
                _mark_cancelled(pipeline_id, run_id, now)
            except ClientError as write_err:
                if _client_error_code(write_err) == "ConditionalCheckFailedException":
                    return _answer_after_lost_race(run_id)
                raise
            _audit_cancel(event, run_id, {"pipeline_id": pipeline_id, "status": "CANCELLED"})
            return _cancel_response(200, run_id, "CANCELLED", "Run cancelled")

        # The Glue job is running: stop it and let job_completion finish the cancel.
        try:
            _mark_cancelling(pipeline_id, run_id, now)
        except ClientError as write_err:
            if _client_error_code(write_err) == "ConditionalCheckFailedException":
                return _answer_after_lost_race(run_id)
            raise

        # The marker job_completion reads when Glue reports the job finished. It
        # lives on the Glue-callback token row because job_completion already
        # reads that row consistently, and the table has no stream, so the write
        # cannot reach execution_updater. The token row exists from before the
        # job started until a callback is delivered, so a missing row means the
        # job's completion has already been handled.
        tokens_table = dynamodb.Table(require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"))
        try:
            tokens_table.update_item(
                Key={"approval_id": f"{_GLUE_CALLBACK_KEY_PREFIX}{run_id}"},
                UpdateExpression=(
                    "SET cancel_requested_at = :now, cancel_execution_arn = :arn, cancel_glue_job_run_id = :jrid"
                ),
                ConditionExpression="attribute_exists(approval_id)",
                ExpressionAttributeValues={":now": now, ":arn": execution_arn, ":jrid": glue_job_run_id},
            )
        except ClientError as marker_err:
            _restore_after_failed_cancel(pipeline_id, run_id, status)
            if _client_error_code(marker_err) == "ConditionalCheckFailedException":
                return _cancel_conflict(
                    run_id, status, "The Glue job finished before it could be stopped; the run is finishing"
                )
            raise

        if glue_state != "STOPPING":
            try:
                stop = glue_client.batch_stop_job_run(JobName=glue_job_name, JobRunIds=[glue_job_run_id])
            except Exception:
                _clear_cancel_marker(tokens_table, run_id)
                _restore_after_failed_cancel(pipeline_id, run_id, status)
                raise
            stop_errors = stop.get("Errors") or []
            if stop_errors:
                _clear_cancel_marker(tokens_table, run_id)
                _restore_after_failed_cancel(pipeline_id, run_id, status)
                detail = stop_errors[0].get("ErrorDetail", {})
                return _cancel_conflict(
                    run_id,
                    status,
                    f"The Glue job run could not be stopped: {detail.get('ErrorCode', '')}: "
                    f"{detail.get('ErrorMessage', '')}",
                )

        _audit_cancel(
            event,
            run_id,
            {"pipeline_id": pipeline_id, "status": "CANCELLING", "glue_job_run_id": glue_job_run_id},
        )
        return _cancel_response(
            202,
            run_id,
            "CANCELLING",
            "Stopping the Glue job; the run becomes CANCELLED when it has stopped",
        )

    except Exception as e:
        logger.error(f"Error cancelling run {run_id}: {e}")
        return handle_error(e, f"Error cancelling run {run_id}")


def _clear_cancel_marker(tokens_table: Any, run_id: str) -> None:
    """Remove the cancel marker when the Glue stop failed. Best-effort.

    Left behind, it would make a later FAILED completion of the same job read as
    a cancellation. A SUCCEEDED completion ignores it either way.
    """
    try:
        tokens_table.update_item(
            Key={"approval_id": f"{_GLUE_CALLBACK_KEY_PREFIX}{run_id}"},
            UpdateExpression="REMOVE cancel_requested_at, cancel_execution_arn, cancel_glue_job_run_id",
            ConditionExpression="attribute_exists(approval_id)",
        )
    except Exception as clear_err:
        logger.warning(f"Could not clear the cancel marker for run {run_id}: {clear_err}")


def get_run_artifacts(pipeline_id: str, run_id: str) -> dict[str, Any]:
    """
    Return all artifacts for a pipeline run in a single response.
    Queries state table for all steps of the run, extracts output_data from each.
    """
    try:
        logger.info(f"Fetching run artifacts for pipeline={pipeline_id}, run={run_id}")

        response = state_table.query(
            KeyConditionExpression=Key("run_id").eq(run_id),
        )

        items = response.get("Items", [])
        logger.info(f"Found {len(items)} step items for run {run_id}")

        if not items:
            return create_response(404, {"error": f"No steps found for run: {run_id}"})

        artifacts: dict[str, Any] = {}
        for item in items:
            step_name = item_str(item, "step_name")
            output_data = item_dict(item, "output_data")
            if step_name and output_data:
                artifacts[step_name] = output_data

        # Fetch code content from S3 from the create_code step's s3_uris.
        # P28 R8.2 (task 14.2): resolve the step in canonical space — new rows
        # are keyed "create_code", legacy rows "CodeGeneration", and pre-R8.3
        # phantom approval rows "code_generation". First row carrying a
        # code_file URI wins.
        code_s3_uri = None
        for step_name, output_data in artifacts.items():
            if _canonical_or_raw(step_name) == StepName.CREATE_CODE:
                code_s3_uri = item_dict(output_data, "s3_uris").get("code_file")
                if code_s3_uri:
                    break
        code_content = None
        if code_s3_uri:
            code_content = _read_s3_object(code_s3_uri)

        return create_response(
            200,
            {
                "pipeline_id": pipeline_id,
                "run_id": run_id,
                "artifacts": artifacts,
                "code_content": code_content,
            },
        )

    except Exception as e:
        return handle_error(e, f"Error fetching artifacts for run {run_id}")


#: Which of ``older`` / ``newer`` each ``ScanIndexForward`` value resolves. Written
#: out as data rather than as a ``True``/``False`` literal at two call sites,
#: because the mapping is the one thing in :func:`get_run_neighbours` that is easy
#: to invert and impossible to notice inverted: both arms return a plausible run id
#: and the buttons still work, they just go the wrong way.
#:
#: ``RUNS_BY_TIME_INDEX`` sorts ``started_at`` **ascending**, so reading forward
#: from a row walks toward *later* times and reading backward walks toward
#: *earlier* ones.
_NEIGHBOUR_SCAN_FORWARD: dict[str, bool] = {"older": False, "newer": True}


def get_run_neighbours(pipeline_id: str, run_id: str) -> dict[str, Any]:
    """The run either side of ``run_id`` in this pipeline's chronological history.

    Backs the run-detail **Previous Run / Next Run** buttons (p48 C10, R10.2). Those
    buttons used to be derived in the browser by ``findIndex`` into one page of
    ``get_pipeline_executions``' output (``RunDetailPage.tsx:264``, ``:356-364``),
    which made them dead at the edge of that page: the neighbour of the tenth run
    was on page 2, and nothing on page 1 could name it.

    **Direction convention, stated because "previous run" is ambiguous and getting
    it backwards is invisible.** The two words are anchored to *time*, not to a
    position in any list:

    * ``older`` — the run that started immediately **before** this one. This is the
      **Previous Run** button.
    * ``newer`` — the run that started immediately **after** this one. This is the
      **Next Run** button.

    That preserves what the array arithmetic did: over a newest-first list,
    ``runs[idx + 1]`` (the old ``prevRunId``) is the next row **down** the page,
    which is the next-older run. So "Previous" has always meant "earlier in time"
    on this surface and continues to.

    **Mechanism — three keyed reads, no walk.**

    1. One ``get_item`` on the base table's ``(pipeline_id, run_id)`` for this run's
       ``started_at``. Keyed, so it cannot miss — the property the
       ``Limit=5``-then-filter-in-Python read in ``get_run_details`` used to lack,
       until task 6.3 replaced it with a keyed ``get_item`` on the same two key
       parts (see the note at the end of this docstring).
    2. A ``Limit=1`` Query over ``RUNS_BY_TIME_INDEX`` with
       ``ScanIndexForward=False`` and this run's index key as ``ExclusiveStartKey``
       → the next row descending, i.e. the next-older.
    3. The same Query with ``ScanIndexForward=True`` → the next-newer.

    ``ExclusiveStartKey`` on a GSI carries the index key **and** the base-table key,
    so all three of ``pipeline_id`` / ``started_at`` / ``run_id`` are supplied. That
    is what makes the seek exact when two runs share a ``started_at`` — a GSI sort
    key is not unique, and the base key is the tiebreak DynamoDB uses.

    Three reads at ``Limit=1``, at any depth in the history. Verified against live
    data (the validation account, ``latesttesting-19ca826a``, 9 rows): seeking from index
    position 4 returns position 5 as ``older`` and position 3 as ``newer``, and both
    end cases return ``Count: 0``.

    **No cursor token, deliberately.** ``run_cursor``'s codec exists so a key can
    survive a round trip through an untrusted client; here the anchor is a ``run_id``
    that is already a path parameter, the ``ExclusiveStartKey`` is minted
    server-side from the ``get_item`` above, and nothing client-supplied ever
    reaches it. Encoding one would also mean fabricating a ``page`` label to satisfy
    ``_FIRST_CURSOR_PAGE``, which this read has no notion of.

    **No bound from tasks 3.3 / 4.3 applies.** ``_MAX_COUNT_PAGES`` /
    ``_MAX_FILTER_PAGES`` bound *walks*, and ``_runs_read_deadline`` /
    ``_filter_walk_deadline`` bound the wall clock a walk may consume. Every read
    here is a single keyed request with ``Limit=1``, so there is no loop to cap and
    no page count to exceed. Nothing is ever truncated, so there is no
    ``partial_counts`` either — the answer is exact or the read failed.

    **Non-run rows are legitimate neighbours and are NOT skipped.** A ``SKIPPED`` or
    ``QUEUED`` row is a real event in this pipeline's history and occupies a real
    position in it, so navigation steps onto it. Filtering them would make
    Previous/Next disagree with the list they navigate — and it would be a second
    ordering authority, which R10.4 forbids. (Task 5.6's previewable-run resolver
    *does* skip such rows, and that is a different question: it is choosing a run to
    render data from, not a run to navigate to.)

    **Absence is reported as ``None``, never omitted.** The newest run has no
    ``newer``; the oldest has no ``older``; a pipeline's only run has neither. The
    keys are always present so a client never has to tell "absent" from
    "not answered".

    A run absent from ``RUNS_BY_TIME_INDEX`` — no ``started_at``, the sparse-index
    cost R9 accepts — cannot be seeked from, so both neighbours come back ``None``
    rather than wrong. Same for a ``run_id`` that is not in this pipeline's
    partition at all: 404, because a neighbour of a run that is not here is not a
    question with an answer.

    Note on the shared-helper question this docstring used to leave open: task 6.3
    landed and **no** helper was extracted, on purpose. There are now three keyed
    reads of the executions row — this anchor read, ``get_run_details``' running-step
    read, and its ``step_logs`` / lineage read — and all three share only the key
    shape ``{"pipeline_id", "run_id"}``, which is two literal strings already pinned
    by ``tests/unit/lambdas/test_executions_started_at_invariant.py``'s AST sweep.
    What they do **not** share is the projection (whole row here, three attributes
    there, four in the third) or the failure semantics (404 here, silent degrade
    there, key omission in the third under ``LGR-8e34f37-3``). A helper would either
    have to drop the projections — making two governed reads wider for no gain — or
    take one as a parameter, at which point it is ``get_item`` with the key dict
    spelled once. Task 5.6's previewable-run walk reads the *index* rather than the
    base table, so it would not adopt one either. This call site therefore stays one
    plain ``get_item``.
    """
    try:
        logger.info("Fetching run neighbours for pipeline=%s, run=%s", pipeline_id, run_id)

        anchor = executions_table.get_item(Key={"pipeline_id": pipeline_id, "run_id": run_id}).get("Item")
        if not anchor:
            return create_response(404, {"error": f"Run not found in pipeline {pipeline_id}: {run_id}"})

        started_at = item_str(anchor, "started_at")
        if not started_at:
            # Present in the base table, absent from the sparse index (R9.1 measured
            # 10 of 61 live rows like this, all fixtures). There is no position to
            # seek from, so the honest answer is "no neighbours" — NOT an error, and
            # NOT a fallback to the old run_id ordering, which would reintroduce the
            # defect this spec removes on one row's behalf.
            logger.warning("Run %s carries no started_at; it is absent from %s", run_id, RUNS_BY_TIME_INDEX)
            return create_response(200, {"pipeline_id": pipeline_id, "run_id": run_id, "older": None, "newer": None})

        exclusive_start_key = {
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "started_at": started_at,
        }

        neighbours: dict[str, Any] = {}
        for direction, scan_forward in _NEIGHBOUR_SCAN_FORWARD.items():
            response = executions_table.query(
                IndexName=RUNS_BY_TIME_INDEX,
                KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
                ScanIndexForward=scan_forward,
                Limit=1,
                ExclusiveStartKey=exclusive_start_key,
                # Only what navigation needs. `run_id` is the destination;
                # `started_at` and `status` are here so a caller can label the
                # button without a second request. Deliberately NOT the whole row —
                # this is not a second source for anything `get_run_details` serves.
                ProjectionExpression="run_id, started_at, #status",
                ExpressionAttributeNames={"#status": "status"},
            )
            rows = response.get("Items", [])
            if not rows:
                neighbours[direction] = None
                continue
            row = rows[0]
            neighbours[direction] = {
                "run_id": item_str(row, "run_id"),
                "started_at": item_str(row, "started_at"),
                "status": item_str(row, "status"),
            }

        return create_response(
            200,
            {
                "pipeline_id": pipeline_id,
                "run_id": run_id,
                "older": neighbours["older"],
                "newer": neighbours["newer"],
            },
        )

    except Exception as e:
        return handle_error(e, f"Error fetching neighbours for run {run_id}")


def run_now(pipeline_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /api/pipelines/{pipeline_id}/run — Run Now endpoint (P17 Task 12.1).

    Looks up the pipeline's ``source_type`` and synchronously invokes the
    ``Pipeline_Trigger_Lambda`` (``agentic_data_ingestion_pipeline_trigger``) with
    a manual-trigger payload. The trigger Lambda enforces the global
    concurrency gate internally, so this handler simply propagates its
    response back to the caller.

    Implements Requirements 5.6, 5.7, 5.8 of the p17-jdbc-data-source spec.

    Args:
        pipeline_id: Pipeline identifier from the URL path.
        event: Optional API Gateway event used for audit context.

    Returns:
        - 200 with ``execution_arn`` and ``execution_name`` on success
        - 404 if the pipeline does not exist
        - 409 if the pipeline is archived (cannot run an archived pipeline)
        - 429 if the global concurrency limit is hit (forwarded from trigger)
        - 504 if the trigger Lambda invocation times out / errors out
        - 500 on any other unexpected failure
    """
    try:
        # ── 1. Look up the pipeline to get source_type ───────────────────
        pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
        pipeline = pipeline_resp.get("Item")

        if not pipeline:
            return create_response(404, {"error": f"Pipeline {pipeline_id} not found"})

        if pipeline.get("status") == "archived":
            return create_response(
                409,
                {"error": f"Pipeline {pipeline_id} is archived and cannot be run"},
            )

        # Default to S3_UPLOAD for legacy pipelines that pre-date the
        # source_type discriminator (P17).
        source_type = pipeline.get("source_type", "S3_UPLOAD")

        # ── 2. Invoke the Pipeline_Trigger_Lambda synchronously ──────────
        trigger_function_name = os.environ.get(
            "PIPELINE_TRIGGER_FUNCTION_NAME", "agentic_data_ingestion_pipeline_trigger"
        )

        payload = {
            "trigger": "manual",
            "pipeline_id": pipeline_id,
            "source_type": source_type,
        }

        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        try:
            invoke_response = lambda_client.invoke(
                FunctionName=trigger_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as invoke_err:
            # Network / SDK-level failure (e.g. ReadTimeoutError when the
            # trigger Lambda exceeds its 5s timeout). Surface as 504 so
            # the dashboard can show a "timed out" toast and let the user
            # retry.
            logger.error(
                "Pipeline_Trigger_Lambda invocation failed",
                extra={"pipeline_id": pipeline_id, "error": str(invoke_err)},
            )
            return create_response(
                504,
                {
                    "error": "Pipeline trigger Lambda invocation timed out",
                    "details": str(invoke_err),
                },
            )

        # ── 3. Detect Lambda runtime errors (timeout, unhandled) ─────────
        # When the Lambda exceeds its timeout boto3 returns success with
        # ``FunctionError == "Unhandled"`` and an error envelope in Payload.
        function_error = invoke_response.get("FunctionError")
        raw_payload = invoke_response.get("Payload")
        payload_bytes = raw_payload.read() if raw_payload is not None else b""

        if function_error:
            try:
                err_body = json.loads(payload_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                err_body = {"errorMessage": payload_bytes.decode("utf-8", errors="replace")}
            logger.error(
                "Pipeline_Trigger_Lambda returned FunctionError",
                extra={
                    "pipeline_id": pipeline_id,
                    "function_error": function_error,
                    "error_body": err_body,
                },
            )
            return create_response(
                504,
                {
                    "error": "Pipeline trigger Lambda timed out or errored",
                    "details": err_body.get("errorMessage", str(err_body)),
                },
            )

        # ── 4. Parse the trigger Lambda's structured response ────────────
        try:
            trigger_payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
        except (ValueError, UnicodeDecodeError) as parse_err:
            logger.error(
                "Failed to parse Pipeline_Trigger_Lambda response",
                extra={"pipeline_id": pipeline_id, "error": str(parse_err)},
            )
            return create_response(
                500,
                {"error": "Invalid response from pipeline trigger Lambda"},
            )

        status_code = trigger_payload.get("statusCode", 500)

        # 429 — concurrency limit hit (global or per-pipeline). Forward
        # the trigger Lambda's body verbatim so the UI can show the
        # specific reason (e.g. "Pipeline already has a running execution").
        if status_code == 429:
            return create_response(
                429,
                {
                    "error": "Concurrency limit hit",
                    "details": trigger_payload.get("body", ""),
                },
            )

        if status_code != 200:
            return create_response(
                status_code,
                {
                    "error": "Pipeline trigger Lambda returned non-success",
                    "details": trigger_payload.get("body", trigger_payload),
                },
            )

        # ── 5. Audit + return success ────────────────────────────────────
        ctx = extract_request_context(event or {})
        write_audit_entry(
            action="pipeline_run_now",
            resource_type="pipeline",
            resource_id=pipeline_id,
            details={
                "source_type": source_type,
                "execution_name": trigger_payload.get("execution_name"),
                "execution_arn": trigger_payload.get("execution_arn"),
            },
            **ctx,
        )

        return create_response(
            200,
            {
                "success": True,
                "pipeline_id": pipeline_id,
                "source_type": source_type,
                "execution_arn": trigger_payload.get("execution_arn"),
                "execution_name": trigger_payload.get("execution_name"),
            },
        )

    except Exception as e:
        return handle_error(e, f"Error running pipeline {pipeline_id}")


def trigger_batch(event: dict[str, Any]) -> dict[str, Any]:
    """Trigger a prefix-based batch ingestion via Step Functions.

    Accepts a pipeline_id (from URL path) and prefix (from request body),
    looks up the pipeline config for source_bucket, constructs the state
    machine ARN, and starts a Step Functions execution with trigger_mode='prefix'.

    Expected event structure:
        - pathParameters or URL path contains pipeline_id
        - body: { "prefix": "customer-data/2026-03-03/" }

    Returns 200 with execution ARN on success.
    """
    try:
        # Extract pipeline_id from the event (set by router)
        pipeline_id = event.get("pipeline_id")
        if not pipeline_id:
            return create_response(400, {"error": "pipeline_id is required"})

        body = json.loads(event.get("body", "{}"))
        prefix = body.get("prefix")
        if not prefix:
            return create_response(400, {"error": "prefix is required in request body"})

        # Look up pipeline to get source_bucket
        try:
            pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
            pipeline = pipeline_resp.get("Item")
        except Exception:
            pipeline = None

        # Historical resolution order preserved: pipeline_config.source_bucket
        # first, then top-level source_bucket, then the env/derived fallback.
        if pipeline:
            source_bucket = item_dict(pipeline, "pipeline_config").get("source_bucket") or pipeline.get("source_bucket")
        else:
            source_bucket = None
        if not source_bucket:
            source_bucket = _fallback_source_bucket()

        # Construct state machine ARN (same pattern as upload.py)
        region = os.environ.get("AWS_REGION", "us-west-2")
        account_id = get_aws_account_id()
        state_machine_name = os.environ.get("STATE_MACHINE_NAME", "agentic-data-ingestion-pipeline")
        state_machine_arn = f"arn:aws:states:{region}:{account_id}:stateMachine:{state_machine_name}"

        # Start Step Functions execution with prefix trigger mode via the
        # shared trigger contract (P28 R9.2) — a prefix-only S3 input always
        # carries trigger_mode="prefix" plus source_bucket/source_prefix.
        from rerun_contract import build_trigger_input

        sfn_client = boto3.client("stepfunctions")
        try:
            execution_input = build_trigger_input(
                {"pipeline_id": pipeline_id, "source_type": "S3_UPLOAD"},
                triggered_by="manual",
                source_bucket=source_bucket,
                source_prefix=prefix,
            )
        except ValueError as exc:
            return create_response(400, {"error": f"Invalid batch trigger input: {exc}"})

        execution_name = f"batch-{pipeline_id[:20]}-{int(time.time())}"

        response = sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(execution_input),
        )

        logger.info(f"Started batch execution: {execution_name} for pipeline {pipeline_id}")

        return create_response(
            200,
            {
                "success": True,
                "message": "Batch pipeline triggered successfully",
                "execution_arn": response["executionArn"],
                "execution_name": execution_name,
                "source_bucket": source_bucket,
                "source_prefix": prefix,
            },
        )

    except Exception as e:
        return handle_error(e, f"Error triggering batch for pipeline {event.get('pipeline_id')}")


def _record_source_bucket(record: dict[str, Any]) -> str | None:
    """Read the source bucket configured on a pipeline record, if any.

    Checks the same two attributes the rerun contract resolves
    (top-level ``source_bucket``, then ``pipeline_config.source_bucket``).
    """
    bucket = record.get("source_bucket")
    if isinstance(bucket, str) and bucket.strip():
        return bucket
    cfg = record.get("pipeline_config")
    if isinstance(cfg, dict):
        bucket = cfg.get("source_bucket")
        if isinstance(bucket, str) and bucket.strip():
            return bucket
    return None


def _fallback_source_bucket() -> str:
    """The source-bucket fallback chain for pipelines without a configured bucket.

    ``SOURCE_BUCKET`` env var, then the deterministic
    ``adi-source-files-{account}-{region}`` name (same chain trigger_batch
    has always used).
    """
    bucket = os.environ.get("SOURCE_BUCKET")
    if bucket:
        return bucket
    account_id = get_aws_account_id()
    region = os.environ.get("AWS_REGION", "us-west-2")
    return f"adi-source-files-{account_id}-{region}"


def retry_run(run_id: str, body: str = "{}") -> dict[str, Any]:
    """Retry a failed run from a specific step.

    Accepts an optional ``retry_from_step`` in the JSON *body*.  When provided
    the value is validated against the canonical ``STEP_ORDER`` and used as the
    starting point for the retry.  When omitted the auto-detected failed step
    is used (preserving the original behaviour).

    Queries the state table for all entries of the given run, identifies the
    failed step, constructs a new SFN execution input with completed step data,
    and starts a new execution that will skip already-completed steps.
    """
    try:
        import hashlib as _hashlib

        import boto3 as _boto3
        from boto3.dynamodb.conditions import Key as _Key

        logger.info(f"Retrying run: {run_id}")

        # --- Parse optional body for retry_from_step -----------------------
        from decimal_utils import convert_decimals_to_float
        from rerun_contract import build_trigger_input
        from retry_utils import STEP_ORDER

        try:
            parsed_body = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {"error": "Invalid JSON in request body"})

        user_retry_from_step: str | None = parsed_body.get("retry_from_step")

        # Validate when provided. P28 R8.2 (task 14.2): normalize via
        # from_any so the comparison can't miss on casing — the dashboard
        # naturally posts the snake_case failed_step from a Failure_Record,
        # while STEP_ORDER holds the PascalCase SFN state ids. Any known
        # variant is accepted; only genuinely unknown names are rejected.
        if user_retry_from_step is not None:
            try:
                from_any(user_retry_from_step)
            except ValueError:
                return create_response(
                    400,
                    {"error": "Invalid step name", "valid_steps": STEP_ORDER},
                )

        # 1. Query state table for all entries with this run_id (paginated —
        # the cached-result completeness of the retry input depends on seeing
        # every state row for the run)
        items: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {"KeyConditionExpression": _Key("run_id").eq(run_id)}
        while True:
            resp = state_table.query(**query_kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        if not items:
            return create_response(404, {"error": f"Run not found: {run_id}"})

        # 2. Determine overall status — reject if not FAILED
        has_failed_step = False
        failed_step = None
        pipeline_id = ""
        completed_steps: dict[str, Any] = {}

        for item in items:
            step_name = item_str(item, "step_name")
            step_status = item_str(item, "status").lower()

            if not pipeline_id:
                pipeline_id = item_str(item, "original_pipeline_id") or item_str(item, "pipeline_id")

            if step_status in ("error", "failed"):
                has_failed_step = True
                failed_step = item
            elif step_status == "success":
                completed_steps[step_name] = item

        # Check execution record status too. The row itself is held (not just
        # its status): p38 §4.2's attempt snapshot is a read-modify-write of
        # this same row's `attempts` list, and reusing this read is what keeps
        # the snapshot from adding a second call.
        exec_status = ""
        exec_row: dict[str, Any] = {}
        if pipeline_id:
            try:
                exec_resp = executions_table.query(
                    KeyConditionExpression=_Key("pipeline_id").eq(pipeline_id),
                    ScanIndexForward=False,
                    Limit=20,
                )
                for ex in exec_resp.get("Items", []):
                    if ex.get("run_id") == run_id:
                        exec_status = item_str(ex, "status").upper()
                        exec_row = ex if isinstance(ex, dict) else {}
                        break
            except Exception:
                pass

        # If no failed step in state entries and execution isn't FAILED, reject
        if not has_failed_step and exec_status != "FAILED":
            return create_response(
                409,
                {
                    "error": "Run is not in a failed state",
                    "run_id": run_id,
                    "current_status": exec_status or "unknown",
                },
            )

        # 3. Determine the retry starting step
        if user_retry_from_step:
            retry_step_name = user_retry_from_step
        else:
            # Fall back to auto-detected failed step (original behaviour)
            if not failed_step:
                return create_response(
                    400,
                    {
                        "error": "No failed step found in run state entries",
                        "run_id": run_id,
                    },
                )
            retry_step_name = item_str(failed_step, "step_name")
            if retry_step_name == "pipeline_failure":
                # The Failure_Record (R1.4) is a sentinel row, not a pipeline
                # step — its failed_step attribute carries the originating
                # step for precise resume.
                retry_step_name = item_str(failed_step, "failed_step")
            try:
                from_any(retry_step_name) if retry_step_name else None
            except ValueError:
                retry_step_name = ""
            if not retry_step_name:
                # Unknown/missing attribution → resume from the first step,
                # which skips nothing (the pre-contract full-rerun semantics).
                retry_step_name = STEP_ORDER[0]

        # 4. Get pipeline_id from state entries
        if not pipeline_id:
            return create_response(
                400,
                {"error": "Could not determine pipeline_id from run state entries"},
            )

        # 5. Check pipeline lock — reject if locked
        try:
            locks_table_name = require_env("LOCKS_TABLE", "agentic_data_ingestion_pipeline_locks")
            locks_tbl = dynamodb.Table(locks_table_name)
            lock_resp = locks_tbl.get_item(Key={"pipeline_prefix": pipeline_id})
            if "Item" in lock_resp:
                return create_response(
                    409,
                    {
                        "error": "Pipeline is currently locked by another execution",
                        "pipeline_id": pipeline_id,
                        "run_id": run_id,
                    },
                )
        except Exception as lock_err:
            logger.warning(f"Failed to check pipeline lock: {lock_err}")

        # 6. Build SFN input via the shared trigger contract (P28 R9.2).
        # Cached step outputs are keyed by whatever step_name the writer
        # Lambda used (snake_case, or the legacy PascalCase variants) — the
        # contract normalizes them to the canonical {step}_result keys the
        # state machine's resultPaths actually dereference. Non-step state
        # rows (self_healing / data_quality agent records, metrics cache) are
        # skipped: they are records, not cached pipeline-step results.
        cached_step_results: dict[str, Any] = {}
        for step_name, step_item in completed_steps.items():
            output_data = item_dict(step_item, "output_data")
            if not output_data:
                continue
            try:
                canonical = str(from_any(step_name))
            except ValueError:
                continue
            if canonical in cached_step_results:
                logger.warning(
                    f"Duplicate cached result for step {canonical} on run {run_id} (variant row {step_name!r} ignored)"
                )
                continue
            # DynamoDB reads carry Decimals; the contract requires JSON-safe
            # values (same float conversion DecimalEncoder used to apply).
            cached_step_results[canonical] = convert_decimals_to_float(output_data)

        # The retry input must satisfy the full trigger contract (R9.1): fetch
        # the pipeline record so JDBC pipelines get the JDBC shape and S3
        # pipelines get source_bucket/source_prefix from the record.
        pipeline_record: dict[str, Any] | None = None
        try:
            raw_record = pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item")
            if isinstance(raw_record, dict):
                pipeline_record = raw_record
        except Exception as rec_err:
            logger.warning(f"Failed to load pipeline record for retry of {pipeline_id}: {rec_err}")

        pipeline_map: dict[str, Any] = pipeline_record or {"pipeline_id": pipeline_id, "source_type": "S3_UPLOAD"}

        build_kwargs: dict[str, Any] = {}
        if pipeline_map.get("source_type", "S3_UPLOAD") == "S3_UPLOAD" and not _record_source_bucket(pipeline_map):
            build_kwargs["source_bucket"] = _fallback_source_bucket()

        try:
            sfn_input = build_trigger_input(
                pipeline_map,
                triggered_by="retry",
                run_id=run_id,
                retry_from_step=retry_step_name,
                cached_step_results=cached_step_results,
                **build_kwargs,
            )
        except ValueError as exc:
            return create_response(400, {"error": f"Invalid retry input: {exc}", "run_id": run_id})

        # The contract normalizes retry_from_step to the SFN state id — echo
        # the normalized value everywhere below.
        retry_step_name = sfn_input["retry_from_step"]

        # 7. Name the retry execution
        #
        # p38 §4.8: the EXECUTION NAME keeps its own 80-character truncation —
        # that bound is AWS's, on execution names only. The run id has no
        # external bound and no longer has a self-imposed one either: p38 §4.1
        # deletes the fabricated `retry-{run_id[:40]}-{ts_hash}` run id
        # entirely, so the retry executes AND reports under the Original_Run's
        # id (R1.1, R1.2). Nothing below builds an identifier by concatenation,
        # which is how R6.2 is satisfied. Do not unify the two identifiers.
        state_machine_arn = require_env("STATE_MACHINE_ARN")
        ts_hash = _hashlib.sha256(datetime.now(UTC).isoformat().encode()).hexdigest()[:8]
        execution_name = f"retry-{run_id}-{ts_hash}"
        # SFN execution names max 80 chars, must be alphanumeric + hyphens/underscores
        execution_name = execution_name[:80]

        sfn_client = _boto3.client("stepfunctions")

        # 8. Snapshot the displaced attempt onto the ORIGINAL executions row,
        #    then reset that row's live fields for the new attempt (p38 §4.2).
        #
        # Ordering is load-bearing: this runs BEFORE start_execution. The
        # `execution_updater` overwrites `status` and `current_step` on this row
        # from the retry's first reported step, and writes that step's
        # `steps_status` entry right after (execution_updater/lambda_function.py
        # :236-256 and :350-356, read end to end), so a snapshot taken after the
        # execution starts races the writer for the very values it is
        # preserving. Taken before, it races nothing.
        #
        # There is deliberately NO put_item here (R3.1, p38 do-NOT 2): a row
        # keyed on a fabricated run id is the Phantom_Execution_Row that three
        # readers would each have to learn to ignore (`LGR-21a1b5b-2`).
        now = datetime.now(UTC).isoformat()

        set_clauses = [
            "#status = :running",
            "#trigger = :retry",
            "retry_source_run_id = :run_id",
            "execution_name = :execution_name",
            "updated_at = :now",
        ]
        attr_values: dict[str, Any] = {
            ":running": "RUNNING",
            ":retry": "retry",
            ":run_id": run_id,
            ":execution_name": execution_name,
            ":now": now,
        }

        # The attempt list is a read-modify-write, so it is only rewritten from
        # a row this function actually read. When the pre-flight read did not
        # find the row, `attempts` and `attempt_count` are left out of the SET
        # rather than written from nothing — writing them would either clobber
        # a history this call cannot see or fabricate an Attempt_Record, and
        # `LGR-99050e6-2` (template / ratified) refuses both: absent is
        # reported as unknown, never defaulted.
        if exec_row:
            prior_attempts = [a for a in item_list(exec_row, "attempts") if isinstance(a, dict)]
            # Attempt numbering comes off `attempt_count`, not off
            # len(attempts): the list is capped, so a length-derived number
            # would stop increasing at the cap and break R2.4's per-attempt
            # attribution.
            displaced_attempt = max(item_int(exec_row, "attempt_count"), len(prior_attempts) + 1)

            # The displaced attempt's node. Every row-derived field is omitted
            # when the row does not carry it — which is why `completed_at` is
            # conditional rather than defaulted.
            attempt_node: dict[str, Any] = {"attempt": displaced_attempt, "failed_step": retry_step_name}
            for _node_key in ("status", "steps_status", "started_at", "completed_at", "execution_name"):
                _node_value = exec_row.get(_node_key)
                if _node_value:
                    attempt_node[_node_key] = _node_value

            # Cap: five attempts, newest kept, oldest dropped (p38 §4.2). The
            # bound is item size — this row also carries `step_logs`, which
            # grows without a bound of its own, and a 400 KB item is a hard
            # write failure that R2.5 forbids the snapshot from inflicting on
            # the retry.
            set_clauses += ["attempts = :attempts", "attempt_count = :attempt_count"]
            attr_values[":attempts"] = [*prior_attempts, attempt_node][-5:]
            attr_values[":attempt_count"] = displaced_attempt + 1

        try:
            executions_table.update_item(
                Key={"pipeline_id": pipeline_id, "run_id": run_id},
                # attribute_exists(run_id) — annotate an EXISTING row only.
                # Without it update_item CREATES the row, which is the phantom
                # this change exists to delete. Same guard `cache_step_result`
                # uses one table over (retry_utils.py:236-247, `LGR-60c4985-4`).
                ConditionExpression="attribute_exists(run_id)",
                UpdateExpression="SET " + ", ".join(set_clauses) + " REMOVE completed_at, retried_by_run_id",
                # `status` and `trigger` are both DynamoDB reserved words.
                ExpressionAttributeNames={"#status": "status", "#trigger": "trigger"},
                ExpressionAttributeValues=attr_values,
            )
        except Exception as snapshot_err:
            # R2.5: preserving the prior attempt is bookkeeping ABOUT a run,
            # not part of it, so it must never fail the run — log and fall
            # through to start_execution (`LGR-eee8ac8-4`). On a
            # ConditionalCheckFailedException the row is simply absent, which
            # is the branch R3.6's "or absent" exists to permit.
            #
            # It is deliberately synchronous, pre-response work in the request
            # path: `LGR-b46a5bb-1` is a standing invariant forbidding
            # threading.Thread for deferred work in lambda/**, so deferring
            # this snapshot is the one forbidden implementation of R2.5.
            logger.warning(
                f"Failed to snapshot the prior attempt of run {run_id} onto its execution row "
                f"(the retry proceeds regardless): {snapshot_err}"
            )

        # 9. Start the retry execution
        try:
            sfn_client.start_execution(
                stateMachineArn=state_machine_arn,
                name=execution_name,
                input=json.dumps(sfn_input, cls=DecimalEncoder),
            )
        except sfn_client.exceptions.ExecutionAlreadyExists:
            # p38 §4.8: this NARROWS, it does not widen. Without it a duplicate
            # execution name is unclassified and surfaces as `handle_error`'s
            # 500 (shared/response_utils.py:47-50), where every sibling refusal
            # in this function returns a named 4xx. do-NOT 10 forbids the
            # opposite move — re-labelling an already-classified exception.
            return create_response(
                409,
                {
                    "error": "A retry of this run was already started with the same execution name",
                    "run_id": run_id,
                    "execution_name": execution_name,
                },
            )

        # 10. Return success — the reported run id IS the executing run id
        return create_response(
            200,
            {
                "success": True,
                "run_id": run_id,
                "execution_name": execution_name,
                "pipeline_id": pipeline_id,
                "retry_from_step": retry_step_name,
                "retry_source_run_id": run_id,
            },
        )

    except Exception as e:
        return handle_error(e, f"Error retrying run {run_id}")
