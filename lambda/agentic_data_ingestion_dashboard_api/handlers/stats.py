#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Statistics handlers for dashboard API.

CACHING (p47 R7.1, R7.2, R7.3, R7.5)
------------------------------------
``get_stats`` is cache-first. On a hit it returns the stored body and issues no
scan at all — one ``GetItem`` and nothing else (R7.1). On a miss it computes as
before and writes the result back with a **60 s** TTL (R7.5).

**The mechanism is not this module's.** ``_get_cached_metrics`` /
``_cache_metrics`` live in ``handlers/metrics.py`` and are imported here,
because R7.2 forbids a second, divergent cache. This mirrors
``pipelines_crud.py:53``, which imports ``_get_cached`` / ``_set_cache`` from
``pipelines_logs`` for the same reason. Both cache-failure directions come with
that mechanism rather than being re-implemented here: a read failure returns
``None`` and a write failure logs — neither raises, so a broken cache degrades
to today's uncached behaviour instead of to a 500.

**Why 60 s and not the metrics helpers' 300 s.** R7.5 caps staleness at 60 s,
justified by the client's own ``staleTime: 60_000`` (``use-pipelines.ts:168-176``)
— a TTL at or below that is invisible to the user. The helpers hardcoded 300 s,
so R7.2 (one mechanism) and R7.5 (≤ 60 s) were in direct tension. Resolved by
giving the write helper a per-caller ``ttl_seconds``, defaulting to the metrics
value; see the block comment above ``_get_cached_metrics``.

**The cache row cannot corrupt the numbers it caches.** It is written into the
state table — the same table two of the four counting reads scan — keyed
``run_id="stats_cache#global"``, ``step_name="cache"``. Those two reads select
``step_name = "file_validation"`` and ``step_name = "schema_mapping"``, so the
row is *scanned and not matched*, exactly as the pre-existing
``metrics_cache#{period}`` row already is. It also carries no ``timestamp``, so
it stays out of the sparse ``step_name-timestamp-index``
(``data-stack.ts:66-69``) that D4's per-step Queries will use. Both properties
are asserted, not assumed — see ``TestStatsCache`` in
``tests/unit/lambdas/dashboard_api/test_stats_characterization.py``.

**``last_updated`` keeps meaning "when these numbers were computed".** A served
hit carries the stamp from the computation that filled the cache, so it can be
up to one TTL old. The alternative — stamping the serve time — was rejected: it
would make the field assert a freshness the numbers beside it do not have, which
is the same "a truncated total is indistinguishable from a real one" failure
family R7.4 exists to remove (``LGR-b2f79ef-14``). Keeping compute time is also
how R7.5's "staleness MUST be bounded **and stated**" is satisfied without
adding a response key: the age is readable from the payload, bounded by the TTL
above. D3 therefore changes no key in the eleven-key contract.

**A degraded payload is never cached (R7.3).** The approvals block swallows any
exception and reports ``0``/``0``, which is indistinguishable from genuinely none
pending. Uncached that misreport lasts one request; cached it would persist for
the whole TTL — "partial counts presented as complete", which is precisely what
R7.3 forbids. So when the approvals read fails the response is unchanged (still
a 200 with zeros, exactly as before) and the cache write is skipped. Marking
that degradation *in the response* is out of D3's scope and belongs with D4's
``partial_counts`` decision.

BOUNDED, INDEX-BACKED COUNTS (p47 R7.4, task D4 implementing D2's plan)
----------------------------------------------------------------------
**Hybrid, not either/or.** R7.4 reads as two arms — index-backed reads *or* a
bounded read with the count marked. D4 does both, because they fix different
things. The index arm stops the two state-table counts being a function of the
whole table. It does **not** make them bounded: a Query over the
``file_validation`` partition is still ``O(rows in that partition)`` and that
partition grows with every file ever processed. So the bound is a backstop that
holds by construction, not a fallback for when the index is unavailable.

**The index.** ``step_name-timestamp-index`` (``data-stack.ts:72-77``), partition
key ``step_name``, sort key ``timestamp``, **projection ALL**. Because the
projection is ALL, ``status`` is in the index, so the success subset is a
server-side ``FilterExpression`` on the same bounded read — no table fetch, no
second read, and no ``ALL_PROJECTED_ATTRIBUTES``. Design risk 5.5 said this index
might not exist; it does, and 5.5 reasoned from ``PipelineIndex``, which is keyed
on ``pipeline_id`` and cannot answer a global per-step count. **No GSI was added**
— ``data-stack.ts:70-71`` permits only one GSI creation per table per stack
update, and this change needs none.

**No time window**, even though the sort key invites one. R7's counts are
all-time totals — ``total_files_processed`` means "runs that recorded a
``file_validation`` step, ever" — so a window would change what the number
*means* while keeping its name. That is a product decision for a human, not an
optimisation folded into a cost fix. The in-tree precedent points both ways and
each for its own reason: ``metrics.py:334-364`` windows because its metrics are
windowed; ``glue_jobs.py:30-55`` queries this same index **windowless** and
paginates fully because completeness matters. R7's counts are in the second
category.

**The sparseness, and what it now costs.** The index is sparse by design
(``data-stack.ts:66-69``): a row with no ``timestamp`` never enters it. D2
enumerated every writer that can produce a ``file_validation`` or
``schema_mapping`` row — ``file_validation:225`` and ``:455``,
``schema_mapping:1585`` and ``:2029`` all set ``timestamp`` unconditionally;
every ``update_item`` that could target those keys either carries
``ConditionExpression="attribute_exists(run_id)"`` or follows a ``put_item`` that
raises on failure; nothing ``REMOVE``s it — so **for rows written by current code
the Query counts the rows the scan counted**. The residual risk is *historical*
rows written before ``timestamp`` existed, which a backfilled GSI would not
contain. That is a real, accepted semantic change, and it is pinned as an
assertion rather than left implicit: see
``TestFilteredCounts.test_a_file_validation_row_with_no_timestamp_is_not_counted``.
``STATS_COUNT_SHADOW_COMPARE`` exists to close it by observation — see
``_shadow_compare_step_count``.

**The bound is a page cap and a wall-clock deadline**, applied to the four
Queries *and* to the two surviving scans. The deadline is the load-bearing half
(C23, ``LGR-14d6dd3-2``: it "makes the bound hold by construction and
independently of per-read latency"); the page cap is the cheap backstop for a
pathological page size, following ``_MAX_STATE_SCAN_PAGES = 100`` at
``cost_optimization_agent/main.py:823``.

**The marker is ``partial_counts``** — a list of the response keys whose value is
a **floor** rather than a total, empty when everything completed. A list rather
than a boolean because it says *which* numbers are floors, so a client cannot
mistake a partial ``successful_files`` for a partial ``total_pipelines``. The key
is **always present**, which is a deliberate narrowing of D2's "absent or
``[]``": the response contract is pinned with ``==`` on the key set, and one
answer is testable where two are not. This takes the contract from eleven keys to
**twelve** — ratified here rather than arriving silently. For up to one TTL after
deploy a cache row written by the previous version can be served without the key;
nothing consumes it yet (see below), so that is stated rather than defended
against.

**A rate is listed in ``partial_counts`` when either of its inputs is** — a
percentage of a floor over a floor is not a percentage of anything. The rate's
type and its ``0``-at-zero-denominator behaviour are **unchanged**: D1 pinned that
the ``0`` is an ``int`` precisely so this path could not quietly convert one to
the other. The arithmetic is also not clamped, so a marked rate can in principle
read above 100 if the total was cut shorter than the subset — the marker is what
says "do not read this as a percentage", and inventing a clamp would change a
pinned value.

**The approvals swallow is brought under the same marker**, which R7.4 does not
require and D2 §4.3 left open. Reported as ``0``/``0``, a failed approvals read is
already a floor — the real value is ``>= 0`` — so it fits ``partial_counts``
exactly, and the alternative was a response that marks six of its numbers
honestly and two dishonestly in the same payload. D3's rule that such a payload
is not cached is **unchanged and generalised**: nothing carrying any
``partial_counts`` entry is cached, so everything in the cache is complete and a
hit never needs marker logic of its own.

**Nothing renders this yet.** ``fetchStats`` (``use-pipelines.ts:134-145``)
normalises the body into a four-field ``DashboardStats`` and drops every other
key, so the marker is additive-safe on the wire and **invisible in the UI**
without a ``types.ts`` + hook change R7.4 does not ask for. Surfacing the caveat
to a human is a named frontend follow-up, not part of this change.
"""

import os
import time
from datetime import UTC, datetime
from typing import Any

from .common import approvals_table, create_response, handle_error, logger, pipelines_table, state_table
from .metrics import _cache_metrics, _get_cached_metrics

#: Cache lifetime for the ``/api/stats`` payload, in seconds. R7.5 caps this at
#: 60 s against the dashboard's own ``staleTime: 60_000``, so a hit served at the
#: TTL boundary is no staler than what the client would have reused anyway.
STATS_CACHE_TTL_SECONDS = 60

#: Key namespace for the stats cache row: ``stats_cache#global``. Deliberately
#: **not** ``metrics_cache``, so a metrics payload can never be read back as a
#: stats body (and vice versa) — the two shapes share nothing.
_STATS_CACHE_NAMESPACE = "stats_cache"

#: The variant token in the key. ``get_stats`` takes no arguments and has no
#: period dimension, so there is exactly one stats payload: the global one.
_STATS_CACHE_VARIANT = "global"

#: The sparse GSI the two per-step counts Query. Partition ``step_name``, sort
#: ``timestamp``, projection ALL (``data-stack.ts:72-77``). Spelled the same way
#: ``metrics.py:321`` and ``glue_jobs.py:38`` spell it — three readers, one index.
_STEP_TIMESTAMP_INDEX = "step_name-timestamp-index"

#: Page guard on every paginating read in this module. Mirrors
#: ``_MAX_STATE_SCAN_PAGES = 100`` (``cost_optimization_agent/main.py:823``). This
#: is the cheap backstop against a pathological page size; the deadline below is
#: what actually makes the bound hold.
_MAX_READ_PAGES = 100

#: This endpoint's own time budget. The Lambda's timeout is 25 s
#: (``compute-stack.ts:741``), so 20 s leaves 5 s in hand — which is what makes an
#: overrun a *reportable* outcome (a 200 carrying ``partial_counts``) rather than a
#: gateway timeout carrying no body at all. Same shape and same reasoning as
#: ``schemas.py:_LISTING_TIME_BUDGET_SECONDS``.
_STATS_TIME_BUDGET_SECONDS = 20.0

#: Held back out of that budget for everything after the last read returns — the
#: cache write, ``create_response``, JSON serialisation, and margin. A test that
#: needs the deadline to bind shortens the budget rather than asserting a
#: wall-clock duration.
_STATS_READ_RESERVE_SECONDS = 2.0

#: Env var gating the one-release shadow compare (D2 §1.4). Read at call time, not
#: import time, so it can be turned on per-account without a code change and so a
#: test can set it. Default **off**: the shadow read is the very scan this change
#: removes, so leaving it on would restore the cost being fixed.
_SHADOW_COMPARE_ENV_VAR = "STATS_COUNT_SHADOW_COMPARE"

#: The response's numeric keys in source order. ``partial_counts`` is emitted in
#: this order so the marker is deterministic and its ordering is assertable.
#: ``last_updated`` is excluded: it is not a count and can never be a floor.
_COUNT_KEY_ORDER: tuple[str, ...] = (
    "total_pipelines",
    "active_pipelines",
    "total_files_processed",
    "successful_files",
    "file_success_rate",
    "total_mappings",
    "successful_mappings",
    "mapping_success_rate",
    "pending_approvals",
    "total_approvals",
)


def _read_deadline(started_at: float) -> float:
    """The monotonic instant every read in this request must be finished by (R7.4).

    Derived **at call time from the request's remaining budget** rather than being
    a fixed duration — C23 / ``LGR-14d6dd3-2``, which chose exactly this over a
    count cap because a count cap "leaves wall clock a function of per-read
    latency; a throttled S3 turns a capped read count into a timeout anyway". Same
    argument, DynamoDB instead of S3, under the same 25 s ceiling.

    ``started_at`` is anchored at the top of ``get_stats``, **before** the cache
    read, so the window available to the counting reads is
    ``budget - reserve - elapsed`` and **shrinks when the work ahead of them was
    slow**. The remaining budget is therefore derived from this module's own two
    constants against the function's Lambda timeout, not from
    ``context.get_remaining_time_in_millis()``: the router's dispatch table calls
    ``get_stats()`` with no arguments (``router.py:599``) and threading a Lambda
    context through it would be a change to every route's signature, for a figure
    that is already known statically. ``schemas.py:_label_pass_deadline`` derives
    its deadline the same way and for the same reason.
    """
    return started_at + max(0.0, _STATS_TIME_BUDGET_SECONDS - _STATS_READ_RESERVE_SECONDS)


def _count_step_rows(step_name: str, deadline: float, *, success_only: bool = False) -> tuple[int, bool]:
    """Count state rows for one step via the ``step_name`` GSI.

    Query, not Scan (P28 R17.1): the key condition bounds the read to one step's
    partition of ``step_name-timestamp-index`` instead of the whole table. Modelled
    on ``glue_jobs.py:30-55`` — same index, **windowless**, fully paginated — plus
    ``Select="COUNT"``.

    ``Select="COUNT"`` returns ``Count`` and ``ScannedCount`` and no ``Items``. It
    does **not** reduce read capacity: DynamoDB reads the items to count them. The
    saving is the key condition, not the ``Select``. It is also deliberately not
    combined with a ``ProjectionExpression`` — real DynamoDB rejects that
    combination even though moto accepts it, and nothing here needs one.

    ``status`` is filterable in-index because the projection is ALL, so the success
    subset is a server-side filter on the same bounded read rather than a second
    read. ``Attr("status").eq("success")`` does not match a *missing* ``status``,
    which is identical to the ``.get("status") == "success"`` comprehension it
    replaces.

    Returns:
        tuple: ``(count, complete)``. ``complete`` is ``False`` when the page cap
        or the deadline stopped the walk, in which case ``count`` is a **floor**
        and the caller must name the affected keys in ``partial_counts`` (R7.4).
    """
    from boto3.dynamodb.conditions import Attr, Key

    query_kwargs: dict[str, Any] = {
        "IndexName": _STEP_TIMESTAMP_INDEX,
        "KeyConditionExpression": Key("step_name").eq(step_name),
        "Select": "COUNT",
    }
    if success_only:
        query_kwargs["FilterExpression"] = Attr("status").eq("success")

    count = 0
    for _page in range(_MAX_READ_PAGES):
        response = state_table.query(**query_kwargs)
        count += response["Count"]
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return count, True
        if time.monotonic() >= deadline:
            return count, False
        query_kwargs["ExclusiveStartKey"] = last_key
    return count, False


def _scan_all(table: Any, deadline: float, **scan_kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
    """Scan a table, paginating through its pages under a page cap and a deadline.

    The two unfiltered scans that survive D4 — pipelines (``total_pipelines`` /
    ``active_pipelines``) and approvals (``total_approvals`` /
    ``pending_approvals``) — both count *every* record, so no index helps: a Query
    needs a partition-key value and there is no value spanning either table.

    Neither can safely use the obvious index either, and both traps are the same
    trap. ``active_pipelines`` cannot use ``status-created_at-index``
    (``data-stack.ts:85``) because a record with **no** ``status`` attribute counts
    as active (``p.get("status", "active")``) and is not in a ``status``-keyed GSI
    at all. ``pending_approvals`` cannot safely use ``status-created-index``
    (``data-stack.ts:132``) because ``data-stack.ts:136-138`` records that an
    approval row can lack ``created_at``, so a Query there could undercount the
    number that gates human attention. ``LGR-acca05d-4`` is the precedent for
    stating an index's coverage gap and the completeness trade rather than silently
    taking the cheap Query.

    So they stay scans — and get the bound and the marker instead.

    Returns:
        tuple: ``(items, complete)``. ``complete`` is ``False`` when the page cap
        or the deadline stopped the walk, in which case ``items`` is a **prefix**.
    """
    items: list[dict[str, Any]] = []
    for _page in range(_MAX_READ_PAGES):
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items, True
        if time.monotonic() >= deadline:
            return items, False
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items, False


def _shadow_compare_step_count(step_name: str, indexed_count: int, indexed_complete: bool, deadline: float) -> None:
    """Log the Query count beside the scan count for one step, when explicitly enabled.

    D2 §1.4's mitigation for the one risk a writer-level argument cannot close:
    ``file_validation`` / ``schema_mapping`` rows written *before* ``timestamp``
    existed are absent from the backfilled GSI, so the index arm would undercount
    them silently. Every current writer sets ``timestamp`` — that was established
    by enumeration — but historical rows are out of reach of that argument, and no
    amount of reading the working tree settles it. Observation does.

    **This is a shadow compare, not a second read path**, and three things keep it
    from becoming one:

    - It is **off unless** ``STATS_COUNT_SHADOW_COMPARE`` is set. The shadow read
      is the exact scan D4 deletes; leaving it on would restore the cost.
    - It is called **after the response payload is complete**, so it cannot
      consume budget that a counted read needed and cannot change a single value
      or marker. Turning the flag on must be observable only in the logs.
    - It swallows everything. A failed comparison is a lost measurement, never a
      failed request.

    A disagreement is only meaningful when **both** sides walked to the end, so an
    incomplete side is logged as inconclusive rather than as a mismatch.
    """
    if os.environ.get(_SHADOW_COMPARE_ENV_VAR, "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        scanned, scan_complete = _scan_all(
            state_table,
            deadline,
            FilterExpression="step_name = :step",
            ExpressionAttributeValues={":step": step_name},
        )
        scan_count = len(scanned)
        if not (indexed_complete and scan_complete):
            logger.info(
                f"Stats shadow compare for {step_name!r} inconclusive: index={indexed_count} "
                f"(complete={indexed_complete}) scan={scan_count} (complete={scan_complete}) — "
                f"a bound stopped at least one side, so a difference proves nothing"
            )
        elif scan_count != indexed_count:
            logger.warning(
                f"Stats shadow compare MISMATCH for {step_name!r}: index={indexed_count} "
                f"scan={scan_count} (delta={scan_count - indexed_count}). The likely cause is "
                f"historical rows written before the state row carried a `timestamp`, which are "
                f"absent from the sparse {_STEP_TIMESTAMP_INDEX} (data-stack.ts:66-69)"
            )
        else:
            logger.info(f"Stats shadow compare for {step_name!r} agrees: {indexed_count} rows by both reads")
    except Exception as exc:
        logger.warning(f"Stats shadow compare for {step_name!r} failed (non-fatal): {exc}")


def get_stats() -> dict[str, Any]:
    """Get aggregated statistics for dashboard. Cache-first — see the module docstring."""
    # The anchor the read deadline is derived from. Taken before the cache read,
    # not after it, so the window available to the counting reads is what is left
    # of the budget rather than a fixed duration: a slow `GetItem` shrinks it.
    # `time.monotonic` because a wall-clock adjustment mid-request must not make
    # the budget appear to run backwards.
    request_started = time.monotonic()
    try:
        # R7.1: a hit returns here, so none of the six reads below runs.
        # `state_table` is read from this module's namespace on purpose, so a
        # test that patches `handlers.stats.state_table` redirects the cache too.
        cached = _get_cached_metrics(
            _STATS_CACHE_VARIANT,
            cache_namespace=_STATS_CACHE_NAMESPACE,
            table=state_table,
        )
        if cached is not None:
            return create_response(200, cached)

        deadline = _read_deadline(request_started)

        # Get basic pipeline counts. Still a scan — see `_scan_all` for why an
        # index cannot serve `active_pipelines` — but now a bounded one.
        pipeline_items, pipelines_complete = _scan_all(pipelines_table, deadline)
        total_pipelines = len(pipeline_items)
        active_pipelines = len([p for p in pipeline_items if p.get("status", "active") == "active"])

        # File processing and schema mapping stats: four `Select="COUNT"` Queries
        # on `step_name-timestamp-index` in place of two filtered table scans
        # (R7.4). Two per step, because the success subset is a filter on the same
        # bounded read rather than a second read of a materialised list.
        total_files, total_files_complete = _count_step_rows("file_validation", deadline)
        successful_files, successful_files_complete = _count_step_rows("file_validation", deadline, success_only=True)
        total_mappings, total_mappings_complete = _count_step_rows("schema_mapping", deadline)
        successful_mappings, successful_mappings_complete = _count_step_rows(
            "schema_mapping", deadline, success_only=True
        )

        # Get approval stats
        approvals_error: Exception | None = None
        approvals_complete = True
        try:
            approval_items, approvals_complete = _scan_all(approvals_table, deadline)
            pending_approvals = len([a for a in approval_items if a.get("status") == "pending"])
            total_approvals = len(approval_items)
        except Exception as exc:
            # Approvals table might not exist yet.
            # The zeros are unchanged behaviour — but they are a *floor*, not a
            # total, so D4 names both keys in `partial_counts` below rather than
            # letting "I could not look" read as "none pending". R7.4 does not
            # require this (it governs the two state-table counts); marking one
            # neighbour honestly while silently inheriting the other's misreport
            # is what it rules out in spirit, so the marker is extended here.
            approvals_error = exc
            approvals_complete = False
            pending_approvals = 0
            total_approvals = 0

        # Which numbers are floors. A rate is only as sound as its two inputs, so
        # it is marked when either is — a percentage of a floor over a floor is
        # not a percentage of anything.
        complete_by_key: dict[str, bool] = {
            "total_pipelines": pipelines_complete,
            "active_pipelines": pipelines_complete,
            "total_files_processed": total_files_complete,
            "successful_files": successful_files_complete,
            "file_success_rate": total_files_complete and successful_files_complete,
            "total_mappings": total_mappings_complete,
            "successful_mappings": successful_mappings_complete,
            "mapping_success_rate": total_mappings_complete and successful_mappings_complete,
            "pending_approvals": approvals_complete,
            "total_approvals": approvals_complete,
        }
        partial_counts = [key for key in _COUNT_KEY_ORDER if not complete_by_key[key]]

        stats = {
            "total_pipelines": total_pipelines,
            "active_pipelines": active_pipelines,
            "total_files_processed": total_files,
            "successful_files": successful_files,
            "file_success_rate": (successful_files / total_files * 100) if total_files > 0 else 0,
            "total_mappings": total_mappings,
            "successful_mappings": successful_mappings,
            "mapping_success_rate": (successful_mappings / total_mappings * 100) if total_mappings > 0 else 0,
            "pending_approvals": pending_approvals,
            "total_approvals": total_approvals,
            # R7.4's marking. Always present; `[]` means every count above is a
            # total. A named key here means that number is a floor.
            "partial_counts": partial_counts,
            # Compute time, not serve time — a cache hit re-serves this exact
            # string, so the stamp always describes the numbers beside it.
            "last_updated": datetime.now(UTC).isoformat(),
        }

        if not partial_counts:
            _cache_metrics(
                _STATS_CACHE_VARIANT,
                stats,
                cache_namespace=_STATS_CACHE_NAMESPACE,
                table=state_table,
                ttl_seconds=STATS_CACHE_TTL_SECONDS,
            )
        else:
            # D3's rule, generalised rather than reversed: only a complete payload
            # is cached. A marked-partial body would stay honest in the cache, but
            # a bound that binds is a transient condition (a throttle, a slow
            # page) and re-serving one floor for a whole TTL turns a one-request
            # degradation into a sustained one. Keeping the cache complete-only
            # also means a hit never needs marker logic of its own.
            logger.warning(
                f"Stats cache write skipped: {sorted(partial_counts)} are floors rather than "
                f"totals, so this payload is incomplete and must not be re-served for up to "
                f"{STATS_CACHE_TTL_SECONDS}s"
                + (f" (approvals read failed: {approvals_error})" if approvals_error is not None else "")
            )

        # Off by default, after the payload is final, and log-only. See
        # `_shadow_compare_step_count` for why all three of those matter.
        _shadow_compare_step_count("file_validation", total_files, total_files_complete, deadline)
        _shadow_compare_step_count("schema_mapping", total_mappings, total_mappings_complete, deadline)

        return create_response(200, stats)

    except Exception as e:
        return handle_error(e, "Error fetching statistics")
