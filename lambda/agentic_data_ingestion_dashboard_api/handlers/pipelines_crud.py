#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pipeline lifecycle CRUD + provisioning/scheduling helpers for the dashboard API.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.

LIST-VIEW ENRICHMENT: BOUNDED, CORRECT, AND CACHED (p47 R8.2, task D6)
---------------------------------------------------------------------
``enrich_pipeline`` issues five reads per pipeline on the page. Two were already
cached through ``_get_cached`` / ``_set_cache`` (``pipelines_logs:318-333``);
R8.2 asks for the other three on the same mechanism. **Caching them first would
have cached two wrong answers**, so D6 fixed the values before caching them —
R8.2's own wording ("cached … or eliminated") permits it, and task D5 measured
both defects rather than inferring them.

**The mechanism is an in-process dict, not the DynamoDB cache R7.2 describes.**
``_get_cached`` / ``_set_cache`` is a module-level ``dict`` with a 300 s TTL,
per-container, with no size bound and no eviction beyond TTL. R8.2 names it and
design §2.4 names it, so this file extends it rather than reaching for
``metrics.py``'s rows; the listing therefore still issues **zero writes**, which
``test_pipelines_list_characterization.py::test_the_listing_writes_nothing``
asserts.

**Position on the unbounded growth, since D6 triples this file's use of it.** It
is acceptable at this scale and the reason is a bound the dict does not have
itself: entries live 300 s, so the working set is the number of distinct
pipelines one container serves inside a 5-minute window, not the number that
exist. D6's three keys per pipeline are small — an ``int``, a two-``int`` tuple,
and at most three ``{file_key, processed_at, status}`` dicts. The entry worth
watching is the *pre-existing* one: ``schema_history_{id}_{limit}`` caches up to
three versions each carrying ``extracted_schema`` verbatim, which is the only
key here whose size is a function of customer data. A size cap is deliberately
**not** added: it would need an eviction policy, the dict is shared with
``pipelines_logs``, and picking one on this task's evidence would be guessing.
The condition to revisit is a container serving hundreds of distinct pipelines
per window, or that schema-history entry growing — not the key count D6 added.

**The two defects, and why a bigger ``Limit`` was not the fix.** ``Limit`` is
applied *before* ``FilterExpression``, so a filtered ``Limit=50`` query is a
50-row **window**, not a bound on the answer. Both reads discarded
``LastEvaluatedKey``, so both reported "nothing found" when the answer sat at
position 51 — measured by D5 as ``mapping_count: 0`` for a pipeline whose
``schema_mapping`` row carries ``7``, and ``all_files: []`` beside
``file_count: 3`` **in the same response**. 50 rows is roughly six runs of one
pipeline, so every pipeline in routine use crosses it. The old comment at the
recent-files read said "we need a higher limit"; a higher limit is a bigger
window, which is the same defect further away.

**The bound and the correctness arrive together.** "Paginate until you find a
match" alone would reintroduce the unbounded read R8 exists to remove, so each
walk is bounded by a page cap **and** by a wall-clock deadline derived at call
time from the request's remaining budget — C23 / ``LGR-14d6dd3-2``, which chose
the deadline over a count cap because it makes the bound hold "by construction
and independently of per-read latency". Same shape as
``stats.py:_read_deadline`` (task D4) and ``schemas.py:_label_pass_deadline``;
each endpoint derives its own from its own budget, which is the house pattern
rather than a third invention. The deadline is anchored once at the top of
``get_pipelines`` and threaded into the fan-out, so **task D9 needs no new
helper** — it bounds the ``ThreadPoolExecutor`` with this same value.

**Pagination also fixed a second flaw in the mapping read.** It took
``Items[0]`` of its single window and called it "the latest schema mapping
step". The walk reads newest-first and stops at the first page carrying a match,
so ``Items[0]`` is now the newest matching row in the whole partition rather
than the newest inside an arbitrary window.

**``partial_fields`` — what a stopped walk says instead of ``0``.** When a bound
stops a walk before it can establish the answer, the value is a floor and
returning ``0`` would be the very defect being fixed: an unmarked
"could-not-check" reported as "checked and found none" (``LGR-b2f79ef-14``).
Each entry therefore carries a ``partial_fields`` list naming the keys whose
value is a floor or a prefix — ``[]`` when every read completed. This is D4's
``partial_counts`` shape (``stats.py``) and its rules, deliberately: a list so a
client can tell *which* value is short, **always present** so the key set is
assertable with ``==``, and a *derived* value listed when its inputs are — which
is why a failed executions read lists ``status`` beside ``running_count`` and
``approval_count``. It takes the entry from 21 keys to **22**, ratified in
``TestEnrichedEntryShape`` rather than arriving silently. Nothing renders it
yet: ``normalizePipeline`` (``use-pipelines.ts:64-99``) rebuilds the entry field
by field and drops unknown keys, so the marker is additive-safe on the wire and
invisible in the UI without a ``types.ts`` change R8.2 does not ask for — the
same named follow-up D4 left for ``partial_counts``.

**A partial value is never cached**, generalising D3's rule the way D4 did: the
cache write happens only on the completing branch of each walk, so everything in
the cache is a whole answer and a hit never needs marker logic. Re-serving a
floor for 300 s would turn a transient throttle into a sustained misreport.

THE SCHEMA-HISTORY WALK, AND WHAT A VERSION NUMBER MEANS (p47 R8.3, task D7)
---------------------------------------------------------------------------
Read #1 was already bounded — ``max_pages = 10`` — just bounded for the wrong
question. D5 measured **four round-trips to return three items** on twelve
oversized versions, running to *exhaustion* rather than to the cap, with no
``Limit`` on any page: the caller's ``limit=3`` bounded the output and not the
read. ``get_pipeline_schema_history_preview`` stops the walk at three distinct
hashes instead, so the same population costs **one** round-trip. The unbounded
walk stays exactly as it was for the other caller (R8.3, design §2.6).

**The cost of that cap is the version numbers, and this is the decision.**
``total_versions`` was derived from having walked the whole partition, so a walk
that stops early no longer knows it — D5 flagged this as DEFECT #3's sting and
wrote that "D7 must keep a count read or state that version numbers become
relative". A count read cannot keep it: a ``Select="COUNT"`` counts *rows* and
this read de-duplicates by ``schema_hash``, it is paginated by the same 1 MB
pre-filter ceiling so it costs the round-trips it was meant to save, and
projecting the payload away does not shrink a page either because
``PipelineIndex`` is ``ProjectionType.ALL``. All three were measured, and the
full argument with the figures lives on
``get_pipeline_schema_history_preview``.

So the numbers become relative **when the walk could not establish the total**,
and the entry says so via ``partial_fields`` — the same marker and the same rule
D6 landed, extended to two more keys. Two consequences worth stating plainly:

* **``schema_version`` is no longer a page length.** It was
  ``len(schema_history)`` after the slice to 3, so every pipeline past its third
  revision reported ``3`` for ever. It is now
  ``schema_history[-1]["version"]`` — the figure D5 pointed out was already
  correct in the same payload, sitting beside the wrong one. The two numbers now
  agree by construction rather than contradicting each other, and the common
  case is *fixed*, not merely marked: a pipeline whose schema-extract rows fit
  in one 1 MB page exhausts its partition on page one, so ``absolute`` is True
  and twelve versions report ``12``.
* **Only an absolute answer is cached**, on both entry points, which keeps D6's
  invariant that a cache hit never needs marker logic. It also keeps the
  pre-existing ``schema_history_{id}_{limit}`` key correct without a mode
  discriminator: since a cached answer is by definition one that read the whole
  partition, the bounded and unbounded walks produce the *same* value for the
  same ``limit``, so there is nothing for the key to tell apart. The one
  behaviour change on the unbounded path is that a walk stopped by the 10-page
  cap no longer writes to the cache — its value is unchanged, but pinning a
  floor into a 300 s per-container dict is what D6's rule exists to prevent.

**Known, out of scope: ``get_pipeline_file_count`` has the same latent
truncation.** It issues one ``Select="COUNT"`` Query with no ``Limit`` and does
not follow ``LastEvaluatedKey`` (``:97``), so a partition larger than one 1 MB
page silently undercounts — the read D5 used as the oracle that disproved
``all_files: []`` is itself only correct while a pipeline's partition fits in one
page. It is read #2, already cached, and outside D6's three; recorded here so
the next reader does not mistake its unbounded-but-unpaginated shape for the
answer.

THE PAGE READ: BOUNDED, AND HONEST ABOUT WHAT IT COULD NOT READ (R8.1, task D8)
------------------------------------------------------------------------------
R8.1 asks that page *N* not read pipelines belonging to other pages, and carves
out "if a filter or sort genuinely requires a full read, that case MUST be
identified explicitly". **This listing is that case, and the reason is
structural rather than a shortfall of effort:** the default order is
``updated_at`` descending and **no index in this account is keyed on
``updated_at``**. The pipelines table has two GSIs (``data-stack.ts:85-86``),
``status-created_at-index`` and ``target_schema_name-index``, and both sort on
``created_at``. So selecting page *N* by key is not available without changing
what the listing is ordered by, which is a product decision and not this task's
to take. See ``_scan_pipeline_page_set`` for the three separate reasons the
status GSI cannot back this read, each measured.

**What D8 therefore is.** Not "read less" — read *the same set, bounded, and say
when the bound bit*. The measurement that reframed the task: on a table whose
records total more than 1 MB, the old single ``scan`` did not merely cost too
much, it **lost records and asserted completeness**. Measured with thirteen
~300 KB records at ``page_size=3``: the response carried ``total_count: 3``,
``total_pages: 1``, ``has_more: False`` and three entries — ten pipelines
invisible, pages 2-5 unreachable, and nothing in the payload saying so. D5
pinned the same defect smaller (``total_count`` reading 3 of 4) and noted it was
"already R8.1's business". A page read that silently drops records is a worse
failure than a page read that costs too much, so it is fixed first.

**The bound.** The walk follows ``LastEvaluatedKey`` under a page cap
(``_MAX_PAGE_SCAN_PAGES``) **and** a wall-clock deadline (C23,
``LGR-14d6dd3-2``), the same shape as ``stats.py:_scan_all`` and D6's two
enrichment walks. The page cap here is its **own** constant rather than a share
of ``_MAX_ENRICHMENT_READ_PAGES``, because the two bound different resources: the
enrichment cap bounds *round-trips per pipeline* and is paid up to 100 times per
request, while this cap bounds *bytes accumulated in one list* before the sort.
They are the same number today for unrelated reasons, and sharing the constant
would let a later change to one silently move the other.

**The deadline is a share of the budget, not the whole of it.** D6 anchored
``request_started`` above the page scan precisely so a slow scan shrinks the
enrichment window. Now that the scan is itself deadline-bounded, giving it the
enrichment deadline would let one slow walk consume the entire budget and turn
every entry on the page partial. So the scan gets ``_PAGE_SCAN_TIME_SHARE_SECONDS``
and enrichment keeps the rest; both figures are stated on their constants.

**``partial_counts`` — the envelope's marker, and how it relates to
``partial_fields``.** When a bound stops the walk, the result set is a *prefix* of
the filtered table, so ``total_count`` is a floor, ``total_pages`` and
``has_more`` are derived from that floor, **and the page contents themselves are a
selection over a subset** — a pipeline that belongs on page 1 by ``updated_at``
can sit in the unread remainder. All four are therefore named in
``partial_counts``, D4's shape and D4's rules (``stats.py``): a list so a client
can tell *which* value is short, always present, ``[]`` when the walk completed,
and a derived value listed when its inputs are. The name is D4's deliberately,
kept for cross-endpoint consistency even though ``pipelines`` is not a count.

The two markers are **different scopes and disjoint key sets**, so a client
seeing both never has to guess which it is reading: ``partial_counts`` names keys
of the **envelope**, ``partial_fields`` names keys of one **entry**, and no key
name appears in both vocabularies. They are also independent — a complete page
read can carry entries with partial enrichment, and a truncated page read's
entries can each be fully enriched.

**Rejected: caching the scan and paginating from cache** (risk 5.6's named
fallback). Rejected on evidence rather than taste: the frontend treats this
endpoint as read-after-write, and **six call sites** invalidate
``pipelineKeys.all`` and immediately refetch the list — create and edit
(``use-pipeline-mutations.ts:69,80``), archive and unarchive
(``use-archive.ts:49,60``), bulk operations (``use-bulk-operations.ts:58,84``) and
approval actions (``useApprovalAction.ts:75``). A server-side cache of any TTS
would serve the pre-write list to every one of those, so "I created a pipeline
and it is not in the list". The only mechanism available here is
``_get_cached``/``_set_cache``, whose TTL is a fixed 300 s
(``pipelines_logs.py:320``), and the client already dedupes within its own
``staleTime: 30_000`` (``use-pipelines.ts:151``) — so the cache's remaining win
is the multi-page walk, which is the case where a human is browsing and cost is
least pressing. It would also make this listing *write* to DynamoDB if the
``metrics.py`` mechanism were used instead, which
``test_pipelines_list_characterization.py::test_the_listing_writes_nothing``
pins that it does not. The condition to revisit is a listing whose order is
index-backed, at which point the cache is unnecessary anyway.

**Which paths still read the full filtered set, named explicitly per R8.1.**
All of them, and the reason is one reason: the ordering.

* *The default listing* — ordered by ``updated_at``, unindexed. Every request
  reads the whole non-archived set.
* *Search* (``search=``) — a case-insensitive substring test over ``pipeline_id``
  **or** ``pipeline_name``, applied in memory. DynamoDB's ``contains`` is
  case-sensitive and has no ``lower``, and a two-attribute ``or`` would not
  reduce the read anyway because a ``FilterExpression`` is applied after the page
  is read. Pagination makes search strictly *better* than before — it can now
  find a match that used to sit beyond the first 1 MB — but it does not make it
  cheaper.
* *``status_filter``* — a single-partition Query on ``status-created_at-index``
  would be key-bounded, and is still not taken; the three reasons are on
  ``_scan_pipeline_page_set``.

THE FAN-OUT'S OWN BOUND, AND WHAT IT ACTUALLY BOUNDS (R8.1, task D9)
-------------------------------------------------------------------
D6 and D7 bounded each enrichment read; D8 bounded the page read and guaranteed
the fan-out a window of at least nine seconds. **What none of them bounded was
the executor.** ``as_completed`` without a timeout waits indefinitely, so every
individual read could honour its deadline while their *sum* over a 20-row page
did not — twenty pipelines each finishing just inside a 9 s window is still 9 s
of wall clock the listing has no say over, and a page whose reads collectively
overrun returns nothing at all through the gateway.

The bound is ``as_completed`` with a timeout on the **same** ``read_deadline``
(C23 / ``LGR-14d6dd3-2``). Not a second deadline: that record's property is that
the bound holds "by construction and independently of per-read latency", which
requires the instant to come from the request's remaining budget, and this
request already has exactly one such instant with named shares
(``_list_read_deadline``, ``_page_scan_deadline``). A third timer would be a
third thing to keep consistent with the Lambda's 25 s ceiling.

**One share had to be added, and it was forced by an ordering fact rather than
chosen.** A read stopped *by* the deadline checks the clock after a page and then
returns, so it delivers its floor a few milliseconds **after** the instant. A
fan-out that stopped waiting exactly there would abandon every pipeline whose
reads had just self-truncated — which makes D6's and D7's marking path
unreachable through this endpoint and replaces a marked entry with a stub that
says *less*. So the fan-out collects until
``read_deadline + _FAN_OUT_COLLECT_GRACE_SECONDS``, a slice of the reserve that
was already held back for "everything after the last enrichment read returns".
That constant carries the measurement; the last instant this handler can be
waiting is 17.5 s, inside the 20 s budget and 7.5 s inside the Lambda's 25 s.

**What is bounded is how long this handler waits — not how long the work runs.**
``shutdown(wait=False, cancel_futures=True)`` cancels only futures that have not
started; a worker already inside a DynamoDB call runs to completion, and on
Lambda the container freezes on return and takes the thread with it. So the
honest claim is that the *response* is not delayed past the deadline. Nothing
consumes a read that arrives after it, which is also why this is not the
``LGR-b46a5bb-1`` case: that invariant's subject is post-response work something
depends on, and ``find_thread_uses``
(``tests/unit/test_no_lambda_threads_prop.py``) matches ``threading.Thread``
bindings, which a ``ThreadPoolExecutor`` structurally is not — the same two-part
argument ``schemas.py:_resolve_display_labels`` makes for the same pattern.

**The trap, recorded because it is what a later reader will "clean up".** The
executor is deliberately **not** a context manager. ``Executor.__exit__`` calls
``shutdown(wait=True)``, which blocks until every submitted read finishes, so a
timeout followed by a ``with``-block exit would raise ``TimeoutError`` and then
return *after* the very overrun the deadline exists to prevent. ``try``/``finally``
with ``shutdown(wait=False, cancel_futures=True)`` is what actually stops waiting.

**``enrichment_abandoned`` — the third marker, and why it is not one of the two.**
A pipeline whose future was outstanding at the deadline ships as the same
six-key stub an enrichment *failure* produces, so the page keeps its length and
its position (``page_order`` covers a stub — it is keyed on the record's
``pipeline_id``, which the stub carries). What it needs is a signal, and neither
existing marker fits: ``partial_counts`` says the page set is a *prefix*, and it
is not — the page walk completed; ``partial_fields`` names keys **of an entry**
whose value is a floor, and an abandoned entry *omits* those keys rather than
carrying floors, so naming them would point a client at keys that are not there.
The condition is genuinely third — "this page member has no enrichment at all,
because the clock ran out" — so it gets its own envelope key: a list of
``pipeline_id``\\ s in page order, always present, ``[]`` when the deadline did
not bind. D4's rules, a third time: a list so a client can tell *which*, always
present so the key set stays assertable with ``==``.

That takes the envelope from seven keys to **eight**, ratified in
``TestPaginationEnvelope`` rather than arriving silently, and the three
vocabularies stay disjoint: ``enrichment_abandoned`` holds *ids*, where
``partial_counts`` and ``partial_fields`` hold *key names*, so no value in one
can be mistaken for a value in another. Nothing renders it yet — ``fetchPipelines``
(``use-pipelines.ts:120-126``) reads six keys and drops the rest — the same
additive-safe position and the same named frontend follow-up D6 and D8 left.

**The failure-isolation boundary did not move, and the distinction is worth
stating precisely.** R8.5 is about *reads*: ``get_pipeline_schema_history_preview``
is still the only enrichment read whose exception reaches the per-future catch,
because the other three keep their own ``except`` inside ``enrich_pipeline``.
What D9 adds is a **second, non-read condition** that reaches the *same stub
literal* — which is why that literal moved into ``_unenriched_stub`` rather than
being copied. A client sees one shape from two causes and can tell them apart
from the envelope.
"""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import boto3
from boto3.dynamodb.conditions import Attr, Key

from audit_utils import extract_request_context, write_audit_entry

# P28 R18.2: the schema-mapping confidence gate has one source of truth.
from config import CONFIDENCE_AUTO_APPROVE_THRESHOLD

# P28 R19.2: the single shared float→Decimal converter (six local copies were
# consolidated into it; do not add a seventh). Needed on the create path because
# p64 R10.1 persists a submitted approval_config verbatim, and boto3 refuses
# `float` — see `_submitted_approval_config`.
from decimal_utils import convert_floats_to_decimal
from dynamo_typing import item_dict, item_int, item_str

# P28 R12.1 / p64 R7.2 — the Identifier_Allowlist, applied to `dedup_columns`
# because those names are interpolated into the generated Glue job's
# `dropDuplicates` call. Used as a refusal, never as a sanitizer: do-NOT 2 forbids
# weakening the allowlist at a sink and do-NOT 3 forbids modifying the validator,
# and nothing here does either. The S3 bucket-name validator beside it (security
# finding S1) is applied the same way, to target S3 locations, in
# `_validate_s3_target_locations`.
from identifier_utils import (
    IdentifierValidationError,
    S3BucketNameError,
    bucket_from_s3_uri,
    parse_identifier_list,
    validate_identifier,
)

# p55 R8.4 — the ONE definition of the Selected_Worksheet attribute name, shared
# with `schema_extract`, which is the reader (LGR-eee8ac8-5). Reader and writer
# live in different Lambdas and are only ever compared through this string, so a
# second spelling here would not fail: it would make the selection silently never
# match, and a configured pipeline would keep refusing multi-sheet workbooks as if
# nothing had been chosen.
from pipeline_record_read import SELECTED_WORKSHEET_ATTRIBUTE

# p64 R7 / R10.4 (design C7) — the ONE definition of the Quality_Config attribute
# names, shared with `create_code`'s `code_assembly`, which is the reader
# (LGR-eee8ac8-5). Writer and reader live in different Lambdas and are only ever
# compared through these strings, so a second spelling here would not fail: it
# would make every opt-in silently never match, and an operator's toggle would
# appear saved while doing nothing. That is the Approval_Config casing gap, one
# attribute over.
from quality_verdict import (
    DATA_QUALITY_ENABLED_KEY,
    DEDUP_COLUMNS_KEY,
    DEDUP_ENABLED_KEY,
    NONPRINTABLE_CHECK_ENABLED_KEY,
    QUALITY_CONFIG_KEY,
)
from step_names import StepName, from_any

# One definition of where an uploaded target schema lives, shared with the
# mapping step that reads it back (LGR-eee8ac8-5).
from target_schema_resolution import locate_uploaded_schema

# Which write modes each target implements — one policy for create, edit and
# code generation.
from write_modes import write_mode_refusal

from .common import (
    AWS_REGION,
    approvals_table,
    create_response,
    dynamodb,
    executions_table,
    get_aws_account_id,
    handle_error,
    logger,
    mappings_table,
    pipelines_table,
    require_env,
    s3_client,
    state_table,
)
from .pipelines_jdbc import (
    _augment_glue_connection_config,
    _provision_jdbc_runtime_crawler,
    _provision_jdbc_source_connection,
    _provision_jdbc_target_connection,
    _reject_managed_glue_connection,
    _reject_plaintext_target_credentials,
    _validate_jdbc_source_config,
    _validate_jdbc_target_config,
)
from .pipelines_logs import _get_cached, _set_cache

# Allowed source types — kept as a small constant so validators and tests
# share a single source of truth.
_ALLOWED_SOURCE_TYPES: frozenset[str] = frozenset({"S3_UPLOAD", "JDBC"})


def _parse_schema_location(schema_location: str) -> tuple[str, str] | None:
    """Parse an ``s3://bucket/key`` schema location into a usable pair.

    Returns ``None`` — meaning "no address here" — for anything that cannot
    address an object, so the caller writes neither
    ``target_schema_bucket`` nor ``target_schema_s3_key`` rather than half a
    pair.

    ``s3://bucket/`` is rejected for exactly that reason. The old inline parse
    split it into ``["bucket", ""]``, length 2, and wrote the pair with an empty
    key — a record that CLAIMS an object it cannot name.
    ``target_schema_resolution.resolve_target_schema`` fails such a record
    (p33 requirement 2.7) and its docstring names this line as the writer that
    could produce it; refusing to write it closes that route at the source.
    """
    if not schema_location or not schema_location.startswith("s3://"):
        return None
    parts = schema_location[len("s3://") :].split("/", 1)
    if len(parts) != 2:
        return None
    bucket, key = parts[0].strip(), parts[1].strip()
    if not bucket or not key:
        logger.warning("Ignoring schema_location %r — it does not address an S3 object", schema_location)
        return None
    return bucket, key


def get_pipeline_file_count(pipeline_id: str) -> int:
    """Get total number of files processed for a pipeline using GSI (with caching)"""
    cache_key = f"file_count_{pipeline_id}"
    cached = _get_cached(cache_key)
    if cached is not None:
        logger.info(f"Returning cached file count for {pipeline_id}: {cached}")
        return cached

    try:
        # Query for file_validation steps with success status
        response = state_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
            FilterExpression=Attr("step_name").eq("file_validation") & Attr("status").eq("success"),
            Select="COUNT",  # Only count, don't return items
        )

        count = response.get("Count", 0)
        logger.info(f"Queried file count for {pipeline_id}: {count} (scanned: {response.get('ScannedCount', 0)})")
        _set_cache(cache_key, count)
        return count
    except Exception as e:
        logger.error(f"Error getting file count for {pipeline_id}: {e}")
        return 0


def _read_schema_history(
    pipeline_id: str,
    limit: int,
    *,
    stop_at_limit: bool,
    deadline: float | None,
) -> tuple[list[dict], bool]:
    """The schema-version walk, shared by both callers. Returns ``(versions, absolute)``.

    ``absolute`` is ``True`` only when the walk read the pipeline's partition to its
    **end**, which is the sole condition under which ``total_versions`` — and
    therefore every ``version`` number derived from it — is the real figure rather
    than a figure relative to what was read. Everything about D7's decision hangs on
    that flag; see ``get_pipeline_schema_history_preview``.

    ``stop_at_limit`` is the bound R8.3 asks for and is **off by default**, so the
    other caller's behaviour is byte-identical to before. ``deadline`` is D6's
    per-request instant (``_list_read_deadline``) or ``None`` for an unbounded-in-time
    walk; it is checked **after** the ``LastEvaluatedKey`` test, so a walk that
    finishes naturally counts as complete even past its deadline — D6's ordering,
    kept deliberately.

    Read errors propagate (P28 R17.3). The old broad ``except → []`` made a DynamoDB
    throttle indistinguishable from a pipeline with no schema history, and this is
    still the **only** enrichment read whose exception reaches ``get_pipelines``'s
    per-future catch, so the unenriched-stub path depends on it continuing to raise.
    """
    all_items = []
    query_kwargs: dict[str, Any] = {
        "IndexName": "PipelineIndex",
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        "FilterExpression": Attr("step_name").eq("schema_extract") & Attr("schema_hash").exists(),
        "ScanIndexForward": False,  # Newest first
    }

    max_pages = 10  # Safety limit: max 10 pages to prevent runaway queries
    page_count = 0
    absolute = False

    while page_count < max_pages:
        response = state_table.query(**query_kwargs)
        all_items.extend(response.get("Items", []))
        page_count += 1

        # Check for more pages. **This test comes before both bounds below**, which is
        # load-bearing rather than stylistic: a walk that reached the end of the
        # partition knows `total_versions` and must be reported absolute even if it
        # also satisfied `stop_at_limit` on the same page. That is the common case —
        # a pipeline whose schema-extract rows fit in one 1 MB page — and reversing
        # these two branches would mark every such pipeline relative for no reason.
        # Same ordering rule D6 states for its own deadline, applied to both bounds.
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            absolute = True
            break

        # R8.3's bound. The walk is newest-first, so the moment `limit` DISTINCT
        # hashes have been seen it holds the `limit` newest versions — the whole
        # answer to what the list view asked. What it does not hold is
        # `total_versions`, which is why this branch leaves `absolute` False.
        if stop_at_limit and limit > 0 and len({item_str(item, "schema_hash") for item in all_items} - {""}) >= limit:
            logger.info(
                f"Schema history for {pipeline_id}: {limit} versions found after {page_count} page(s); "
                f"stopping the walk, so version numbers are relative to what was read"
            )
            break

        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                f"Schema history for {pipeline_id} stopped by the read deadline after {page_count} pages; "
                f"version numbers are relative and schema_version is a floor"
            )
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    else:
        logger.warning(
            f"Schema history for {pipeline_id} hit the {max_pages}-page cap; "
            f"version numbers are relative and schema_version is a floor"
        )

    # Deduplicate by schema_hash (keep first occurrence = newest)
    seen_hashes = set()
    unique_schemas = []
    for item in all_items:
        schema_hash = item_str(item, "schema_hash")
        if schema_hash and schema_hash not in seen_hashes:
            seen_hashes.add(schema_hash)
            json_s3_uri = item_str(item, "json_s3_uri")
            file_name = json_s3_uri.split("/")[-1] if json_s3_uri else "unknown"
            unique_schemas.append(
                {
                    "hash": schema_hash,
                    "timestamp": item_str(item, "timestamp"),
                    "extracted_schema": item.get("extracted_schema", ""),
                    "file_name": file_name,
                    "json_s3_uri": json_s3_uri,
                }
            )

    # Sort by timestamp (oldest to newest) for proper version numbering
    unique_schemas.sort(key=lambda x: str(x["timestamp"]))

    # Get total count for version numbering
    total_versions = len(unique_schemas)

    # Get the last N versions (most recent)
    recent_schemas = unique_schemas[-limit:] if len(unique_schemas) > limit else unique_schemas

    # Assign correct version numbers (starting from where they actually are in the full
    # list). Absolute when the walk exhausted the partition; relative to the versions
    # actually read when a bound stopped it — which is what `absolute` reports and what
    # the caller must not present as a total.
    start_version = total_versions - len(recent_schemas) + 1
    for i, schema in enumerate(recent_schemas):
        schema["version"] = start_version + i

    return recent_schemas, absolute


def get_pipeline_schema_history(pipeline_id: str, limit: int = 10) -> list[dict]:
    """Get last N unique schema versions for a pipeline using GSI

    Fetches ALL schema_extract items for the pipeline, deduplicates by hash,
    then returns the most recent N unique versions.

    OPTIMIZED: Uses caching and timeout protection to prevent long-running queries.

    **Unchanged by D7 except in one respect**, and deliberately: the walk still runs
    to exhaustion or to ``max_pages = 10``, and the version numbers are still derived
    from the whole partition. R8.3 and design §2.6 both say to keep this bound for
    other callers, so the list view's tighter bound lives in
    ``get_pipeline_schema_history_preview`` rather than here.

    The one respect: a walk stopped by the 10-page cap is **no longer cached**. Its
    returned value is identical to before — the numbers were already relative to the
    ten pages it read — but pinning that answer into a 300 s per-container dict would
    re-serve a floor as a total, which is the rule D6 established for its own two
    walks and generalised from D3. A walk that reaches the end of the partition still
    caches, which is every call this function has today.
    """
    cache_key = f"schema_history_{pipeline_id}_{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    recent_schemas, absolute = _read_schema_history(pipeline_id, limit, stop_at_limit=False, deadline=None)
    if absolute:
        _set_cache(cache_key, recent_schemas)
    return recent_schemas


#: The listing's own time budget. The Lambda's timeout is 25 s
#: (``compute-stack.ts:741``), so 20 s leaves 5 s in hand — which is what makes an
#: overrun a *reportable* outcome (a 200 whose entries carry ``partial_fields``)
#: rather than a gateway timeout carrying no body at all. Same figure and same
#: reasoning as ``stats.py:_STATS_TIME_BUDGET_SECONDS`` and
#: ``schemas.py:_LISTING_TIME_BUDGET_SECONDS``.
_LIST_TIME_BUDGET_SECONDS = 20.0

#: Held back out of that budget for everything after the last enrichment read
#: returns: the fan-out's collection of those reads, the executor's own teardown,
#: the re-sort, ``create_response`` and the JSON encode of a 20-row page carrying
#: schema history. Larger than stats' 2 s reserve because this payload is the bigger
#: of the two. A test that needs the deadline to bind shortens the budget rather
#: than asserting a wall-clock duration.
_LIST_READ_RESERVE_SECONDS = 3.0

#: How long past the enrichment read deadline the fan-out keeps collecting before it
#: abandons what is still outstanding (task D9). A named share of the reserve above,
#: **not** a second deadline: the instant is ``_list_read_deadline``'s, and this is
#: the slack that makes reaching it *observable*.
#:
#: **The ordering fact that forces it, measured rather than reasoned about.** A read
#: stopped *by* the deadline checks the clock after a page and then returns — so it
#: delivers its floor a few milliseconds **after** the instant, by construction. A
#: fan-out that stopped waiting exactly at that instant would therefore abandon
#: every pipeline whose reads had just self-truncated, and D6's and D7's whole
#: marking path — ``mapping_count`` as a floor, relative ``schema_version``, the
#: ``partial_fields`` naming both — would be unreachable through this endpoint,
#: replaced by a six-key stub that says *less*: ``schema_version: 0`` for a pipeline
#: with versions, and no ``mapping_count`` key at all. Measured while D9 was written:
#: with no grace, ``TestPartialFields``' deadline cases returned stubs instead of
#: marked entries, and which one arrived was a race between the worker and
#: ``as_completed``.
#:
#: A marked entry beats a stub, so the fan-out waits this much longer than the
#: reads. The bound still holds by construction: the last instant this handler can
#: be waiting is ``budget - reserve + grace`` = 17.5 s, which is inside the 20 s
#: budget and 7.5 s inside the Lambda's 25 s (``compute-stack.ts:741``).
_FAN_OUT_COLLECT_GRACE_SECONDS = 0.5

#: Page cap on each paginating enrichment walk, per pipeline. The deadline is what
#: makes the bound hold (C23); this is the cheap backstop for a pathological page,
#: following ``stats.py:_MAX_READ_PAGES`` and
#: ``cost_optimization_agent/main.py:823``. Deliberately smaller than those 100:
#: this cap is paid *per pipeline on the page*, so 20 pages x 50 rows already
#: covers a 1,000-row partition per pipeline and a 20-row page could otherwise
#: authorise 20x more work than a single-shot endpoint.
_MAX_ENRICHMENT_READ_PAGES = 20

#: Rows each enrichment page may examine. This is the same ``Limit=50`` the two
#: defective reads passed — kept, because with pagination behind it a small window
#: is a *cheap first page* rather than a truncation: the common case (a recent
#: ``schema_mapping`` row, recent validated files) is answered by one page that
#: examines 50 rows instead of a 1 MB page that examines thousands.
_ENRICHMENT_PAGE_LIMIT = 50

#: How many recent files the list-view preview carries. Three — the value the
#: loop's ``break`` has always enforced, in spite of two comments saying five
#: (pinned by ``test_the_preview_stops_at_three_files_despite_asking_for_five``).
_RECENT_FILES_PREVIEW_LIMIT = 3

#: How many schema versions the list view renders. Three, unchanged — it was the
#: inline ``limit=3`` at the call site and is the figure R8.3 argues from. Named
#: because D7 made it the walk's stopping condition as well as the slice width, so
#: the two can no longer drift apart.
_SCHEMA_HISTORY_PREVIEW_LIMIT = 3

#: How many recent executions the live-status probe reads. Five, unchanged: this
#: read is correct as written — it wants the latest rows and takes them, with no
#: ``FilterExpression`` for a ``Limit`` to be applied ahead of.
_EXECUTIONS_PROBE_LIMIT = 5

#: Page cap on the page read's walk (task D8). Deliberately **not** shared with
#: ``_MAX_ENRICHMENT_READ_PAGES`` despite holding the same number: that cap bounds
#: round-trips *per pipeline* and is paid up to ``page_size`` times, while this one
#: bounds the bytes accumulated in a single list before the in-memory sort. At
#: DynamoDB's 1 MB page ceiling that is up to ~20 MB of raw items, which is the
#: figure that matters on a 512 MB function (``compute-stack.ts:742``) — which is
#: also why this is not ``stats.py``'s 100: that endpoint's capped walks are
#: ``Select="COUNT"`` reads accumulating nothing. The deadline below is what makes
#: the bound hold (C23); this is the cheap backstop for a pathological page.
_MAX_PAGE_SCAN_PAGES = 20

#: Wall-clock share of ``_LIST_TIME_BUDGET_SECONDS`` the page read may consume.
#: A share rather than the whole enrichment deadline, because the page read now
#: runs under a deadline of its own: handed ``_list_read_deadline``'s instant it
#: could spend the entire budget and leave the N+1 enrichment phase — the part
#: that must not be starved, since every entry would go partial — with nothing.
#: Eight of the twenty seconds, so enrichment keeps at least nine after the 3 s
#: reserve. A test that needs this to bind shortens it rather than asserting a
#: wall-clock duration.
_PAGE_SCAN_TIME_SHARE_SECONDS = 8.0

#: The envelope keys derived from the page read's result set, in the order
#: ``partial_counts`` emits them, so the marker is deterministic and assertable.
#: ``page`` and ``page_size`` are excluded: both are the request echoed back and
#: neither can be a floor. ``pipelines`` is included because a truncated read
#: makes the page a *selection over a subset* — the entries can be the wrong
#: entries, not merely fewer of them.
_ENVELOPE_DERIVED_KEY_ORDER: tuple[str, ...] = ("pipelines", "total_count", "total_pages", "has_more")


def _list_read_deadline(started_at: float) -> float:
    """The monotonic instant every enrichment read in this request must finish by.

    Derived **at call time from the request's remaining budget** rather than being
    a fixed duration — C23 / ``LGR-14d6dd3-2``, which rejected a count cap because
    it "leaves wall clock a function of per-read latency; a throttled S3 turns a
    capped read count into a timeout anyway". The same argument holds for a
    throttled DynamoDB under the same 25 s ceiling.

    ``started_at`` is anchored at the top of ``get_pipelines``, **before** the page
    scan, so the window left for enrichment is ``budget - reserve - elapsed`` and
    **shrinks when the work ahead of it was slow**. Since task D8 the scan can only
    shrink it so far: it has a deadline of its own
    (``_page_scan_deadline``/``_PAGE_SCAN_TIME_SHARE_SECONDS``) carved from this same
    budget, precisely so one slow walk cannot leave the fan-out with nothing and
    turn every entry on the page partial. The budget comes from this module's two constants
    against the function's Lambda timeout rather than from
    ``context.get_remaining_time_in_millis()``: the router calls ``get_pipelines``
    with query-string arguments only, and threading a Lambda context through it
    would change every route's signature for a figure already known statically.
    ``stats.py:_read_deadline`` and ``schemas.py:_label_pass_deadline`` derive
    theirs the same way and say the same thing.

    **Task D9 should reuse this value, not add its own.** The fan-out bound R8
    still wants belongs to D9; the deadline it needs is this one, already
    anchored per request and already threaded into ``enrich_pipeline``.
    """
    return started_at + max(0.0, _LIST_TIME_BUDGET_SECONDS - _LIST_READ_RESERVE_SECONDS)


def _page_scan_deadline(started_at: float) -> float:
    """The monotonic instant the page read must stop by (R8.1, task D8).

    Earlier than ``_list_read_deadline``'s instant by construction, and that gap is
    the point: the page read runs *before* the enrichment fan-out and would
    otherwise be able to spend the whole budget, leaving every entry on the page
    partial for want of time rather than for want of data. See
    ``_PAGE_SCAN_TIME_SHARE_SECONDS``.

    Derived from the same ``started_at`` anchor as the enrichment deadline, so the
    two are shares of one budget rather than two independent timers.
    """
    return started_at + max(0.0, min(_PAGE_SCAN_TIME_SHARE_SECONDS, _LIST_TIME_BUDGET_SECONDS))


def _unenriched_stub(pipeline_item: dict[str, Any]) -> dict[str, Any]:
    """The six-key entry a pipeline gets when its enrichment did not produce one.

    Lifted verbatim out of the fan-out's ``except`` clause by task D9 so that the
    **two** conditions which now reach it emit byte-identical shapes: an enrichment
    read that raised, and a future abandoned at the fan-out deadline. Two copies of
    this literal would be two shapes one refactor apart, and its key set is pinned
    with ``==`` (``test_pipelines_list_characterization.py``'s ``STUB_KEYS``, a
    strict subset of ``ENRICHED_KEYS``) precisely because a client parses both forms
    with one type.

    ``schema_version: 0`` and ``schema_history: []`` are **neutral rather than
    accurate** — a pipeline with versions reports none here. That is the documented
    cost of degrading, characterized by D5 and unchanged by D9; what D9 adds is that
    the deadline case says *why* it is a stub, through the envelope's
    ``enrichment_abandoned``. ``status`` means something different here than in an
    enriched entry: it is the record's own value, not the derived ``live_status``.
    """
    pipeline_id = item_str(pipeline_item, "pipeline_id")
    return {
        "pipeline_key": pipeline_id,
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_item.get("pipeline_name", pipeline_id),
        "status": item_str(pipeline_item, "status", "active"),
        "schema_version": 0,
        "schema_history": [],
    }


def _scan_pipeline_page_set(deadline: float, **scan_kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
    """The listing's page read: the filtered pipeline set, paginated under a bound.

    R8.1 / task D8. Replaces a single ``pipelines_table.scan`` that discarded
    ``LastEvaluatedKey``, which did not merely cost too much — it **lost records
    and reported completeness**. Measured on thirteen ~300 KB records at
    ``page_size=3``: ``total_count: 3``, ``total_pages: 1``, ``has_more: False``,
    three entries returned, ten pipelines invisible and pages 2-5 unreachable.

    **Why this is still a Scan, when the table has a ``status``-keyed GSI.**
    ``status-created_at-index`` (``data-stack.ts:85``) is a single-partition Query
    for a given status and would be genuinely key-bounded. It is not taken, for
    three independent reasons, each established by measurement rather than
    inference:

    1. **The sort key is the wrong attribute.** The index sorts on ``created_at``;
       this listing orders by ``updated_at``, which moves on every edit while
       ``created_at`` never does. A Query with ``ScanIndexForward=False`` returns a
       *different order*, so switching would silently re-order the page a human
       reads. That is a product decision, not an optimisation.
    2. **The default predicate is not a partition.** The default filter is
       ``Attr("status").ne("archived")``, and reproducing it on a ``status``-keyed
       index means enumerating the non-archived value space and merging its
       partitions — which reads them all, so the bound is lost, and which silently
       drops any status value not on the list.
    3. **The index is sparse in exactly the way that hides records.** A pipeline
       record with **no** ``status`` attribute is absent from a ``status``-keyed GSI
       altogether. This is the same trap ``stats.py:_scan_all`` records for
       ``active_pipelines``, arrived at there from a *different* predicate
       (``p.get("status", "active") == "active"``), so it was re-established here
       rather than assumed: measured against the shared moto fixture, a record with
       no ``status`` **does** match ``.ne("archived")`` and appear in the listing,
       while being invisible to a Query on the index. Moving to the GSI would make
       such a pipeline vanish from the dashboard.

       *One divergence worth flagging, because it is not this task's to settle.*
       That measurement is moto's: ``moto.dynamodb.comparisons.OpNotEqual``
       resolves a missing attribute to ``None`` and returns ``None != "archived"``,
       i.e. True. Real DynamoDB's documented comparator semantics do not state the
       missing-operand case, and the community pattern
       ``attribute_not_exists(a) OR a <> :v`` implies the plain comparison is
       False there. If so, production already omits status-less pipelines from this
       listing and the fixture over-reports. Either way the predicate is
       **unchanged** by D8, so this is a pre-existing property of the test suite
       rather than a risk this change takes on — recorded so the next reader does
       not mistake a fixture behaviour for a production one.

    Returns:
        tuple: ``(items, complete)``. ``complete`` is ``False`` when the page cap or
        the deadline stopped the walk, in which case ``items`` is a **prefix** of
        the filtered set and the caller must name every key in
        ``_ENVELOPE_DERIVED_KEY_ORDER`` in the response's ``partial_counts``
        (``LGR-b2f79ef-14``: a total that could not be established must not be
        reported as one).

    Read errors propagate. The outer handler turns them into a 5xx rather than an
    empty page, which is the asymmetry
    ``test_pipelines_list_characterization.py::test_a_pipelines_table_failure_is_a_5xx_and_carries_no_envelope_key``
    pins — a failed page read must not be indistinguishable from "no pipelines".
    """
    items: list[dict[str, Any]] = []
    for page in range(_MAX_PAGE_SCAN_PAGES):
        response = pipelines_table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items, True
        if time.monotonic() >= deadline:
            logger.warning(
                f"Pipeline page read stopped by the read deadline after {page + 1} page(s) "
                f"({len(items)} records read); total_count is a floor and the page is a selection "
                f"over a subset, both named in partial_counts"
            )
            return items, False
        scan_kwargs["ExclusiveStartKey"] = last_key

    logger.warning(
        f"Pipeline page read hit the {_MAX_PAGE_SCAN_PAGES}-page cap ({len(items)} records read); "
        f"total_count is a floor and the page is a selection over a subset, both named in partial_counts"
    )
    return items, False


def get_pipeline_schema_history_preview(
    pipeline_id: str, deadline: float, limit: int = _SCHEMA_HISTORY_PREVIEW_LIMIT
) -> tuple[list[dict], bool]:
    """The list view's schema-version read: bounded at ``limit`` versions, not 10 pages.

    R8.3 / task D7. The walk is newest-first, so it stops as soon as it has ``limit``
    DISTINCT hashes instead of reading the whole partition to number them. D5
    measured **four round-trips to return three items** on twelve oversized versions,
    running to exhaustion rather than to ``max_pages``; the same population now costs
    **one**. The saving is per pipeline on the page, so a 20-row page drops 60
    round-trips.

    The returned versions are the same three versions the unbounded walk would have
    returned. Reading strictly newest-first and de-duplicating on first occurrence
    means the first ``limit`` distinct hashes are exactly the ``limit`` hashes with
    the newest occurrences, which is what the full walk's "sort ascending, take the
    last ``limit``" selects. **Only the numbering differs.**

    Returns:
        tuple: ``(versions, absolute)``. ``absolute`` is ``True`` when the walk read
        the partition to its end and every ``version`` is therefore the real version
        number; ``False`` when a bound stopped it, in which case the numbers are
        **ordinals within what was read** and ``schema_version`` is a floor. The
        caller must then name ``schema_version`` and ``schema_history`` in the entry's
        ``partial_fields`` (``LGR-b2f79ef-14`` — presenting a relative number as an
        absolute one is the same "could not check reported as checked" failure as
        reporting a floor as a total). Only an absolute answer is cached.

    **Why there is no count read, which is the decision D5 left open.** D5's DEFECT #3
    observed that ``total_versions`` is derived from having walked the whole partition
    and warned that "D7 must keep a count read or state that version numbers become
    relative". A ``Select="COUNT"`` read cannot keep it, on three independent grounds,
    all measured against moto rather than reasoned about:

    1. **It counts rows, not versions.** This function de-duplicates by
       ``schema_hash``, and ``test_duplicate_hashes_collapse_to_one_version`` already
       pins that four extract rows across three hashes are three versions. A COUNT
       over four rows sharing one hash returns ``4`` where the version count is ``1``.
       DynamoDB has no distinct-count, so the number a COUNT returns is not the number
       the oracle needs.
    2. **It is itself paginated, by the same ceiling.** The 1 MB limit applies to the
       data *scanned*, before ``Select`` and before ``FilterExpression``. Measured on
       D5's own twelve-oversized-version population, a ``Select="COUNT"`` Query
       returns ``Count=3`` and a ``LastEvaluatedKey`` — the same four round-trips it
       was supposed to replace, plus the walk's.
    3. **Projecting the payload away does not help either**, for the same reason: the
       same population under a ``ProjectionExpression`` of ``schema_hash, timestamp``
       still returns three items and a ``LastEvaluatedKey``. ``PipelineIndex`` is
       ``ProjectionType.ALL`` (``data-stack.ts:56``), so ``extracted_schema`` is in
       the index and counts against the page whether or not it is returned. A
       KEYS_ONLY index would fix this and is out of scope twice over — it is
       infrastructure, and ``data-stack.ts:73-75`` records that DynamoDB allows only
       one GSI creation per table per stack update.

    So the honest resolution is D5's second arm, and ``schema_version`` is fixed
    rather than left wrong: the caller now reports ``schema_history[-1]["version"]``,
    which is the figure D5 identified as already correct in the payload, and marks it
    when this walk could not establish it.
    """
    cache_key = f"schema_history_{pipeline_id}_{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached, True

    recent_schemas, absolute = _read_schema_history(pipeline_id, limit, stop_at_limit=True, deadline=deadline)
    if absolute:
        _set_cache(cache_key, recent_schemas)
    return recent_schemas, absolute


def get_pipeline_mapping_count(pipeline_id: str, deadline: float) -> tuple[int, bool]:
    """Field count from the pipeline's newest ``schema_mapping`` state row.

    **The filter cannot be avoided, which is why this paginates.** ``PipelineIndex``
    is keyed ``(pipeline_id, timestamp)`` and ``step_name`` is in neither position,
    so no key condition can select the step; the sibling
    ``step_name-timestamp-index`` is keyed the other way round and would need a
    ``pipeline_id`` filter over every pipeline's rows, which is strictly worse. So
    the read stays a filtered Query over one pipeline's partition and gets
    correctness from following ``LastEvaluatedKey`` instead of from a wider window.

    The walk is newest-first (``ScanIndexForward=False``) and stops at the **first
    page carrying a match**, which fixes the second flaw D5 found alongside the
    truncation: ``Items[0]`` is now the newest matching row in the partition rather
    than the newest inside one arbitrary 50-row window.

    ``ProjectionExpression`` is kept, so a matching row still returns almost
    nothing per item. Note that it makes a ``schema_mapping`` row with no
    ``mapping_count`` attribute arrive as an empty map — truthy, counted as a
    found row, and read as ``0`` by ``item_int``'s default. That is the
    pre-existing reading of "there is a mapping step and it recorded no count",
    and it is preserved.

    Returns:
        tuple: ``(count, complete)``. ``complete`` is ``False`` when the page cap
        or the deadline stopped the walk, in which case ``count`` is ``0`` as a
        **floor** — "could not establish", not "none" — and the caller must name
        ``mapping_count`` in the entry's ``partial_fields`` (``LGR-b2f79ef-14``).
        Only a complete answer is cached.
    """
    cache_key = f"mapping_count_{pipeline_id}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached, True

    query_kwargs: dict[str, Any] = {
        "IndexName": "PipelineIndex",
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        "FilterExpression": Attr("step_name").eq("schema_mapping"),
        "ProjectionExpression": "mapping_count",
        "ScanIndexForward": False,  # Newest first
        "Limit": _ENRICHMENT_PAGE_LIMIT,
    }

    scanned = 0
    for page in range(_MAX_ENRICHMENT_READ_PAGES):
        response = state_table.query(**query_kwargs)
        scanned += response.get("ScannedCount", 0)
        items = response.get("Items", [])
        if items:
            count = item_int(items[0], "mapping_count", 0)
            _set_cache(cache_key, count)
            return count, True

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            # The whole partition was read and carries no schema_mapping row, so
            # 0 is the answer rather than a floor — this is the ONLY branch that
            # may report 0 as a total, and the only one that caches it.
            _set_cache(cache_key, 0)
            return 0, True
        if time.monotonic() >= deadline:
            logger.warning(
                f"Mapping count for {pipeline_id} stopped by the read deadline after {page + 1} pages "
                f"({scanned} rows examined); reporting 0 as a floor, not as a total"
            )
            return 0, False
        query_kwargs["ExclusiveStartKey"] = last_key

    logger.warning(
        f"Mapping count for {pipeline_id} hit the {_MAX_ENRICHMENT_READ_PAGES}-page cap "
        f"({scanned} rows examined); reporting 0 as a floor, not as a total"
    )
    return 0, False


def get_pipeline_recent_files(
    pipeline_id: str, deadline: float, limit: int = _RECENT_FILES_PREVIEW_LIMIT
) -> tuple[list[dict[str, Any]], bool]:
    """The newest ``limit`` successfully validated files, for the list-view preview.

    Same shape and same reason as ``get_pipeline_mapping_count``: the ``step_name``
    filter cannot become a key condition on ``PipelineIndex``, so the read
    paginates newest-first and stops as soon as it has ``limit`` matches. In the
    common case that is one page, so the *typical* cost is unchanged from the old
    ``Limit=50`` single read while the truncation is gone.

    A folder-marker row (a key ending in ``/``) is skipped without counting toward
    ``limit``, unchanged. That asymmetry against ``get_pipeline_file_count``, which
    has no such predicate, is **deliberate** and is not the truncation being fixed
    here — the two are distinguished so they are not reconciled by mistake.

    Returns:
        tuple: ``(files, complete)``. ``complete`` is ``True`` when the walk either
        collected ``limit`` matches or read the partition to its end — a short list
        from an exhausted partition is the whole answer. ``False`` means a bound
        stopped the walk, so the list is a **prefix** and the caller must name
        ``all_files`` in ``partial_fields``. Only a complete answer is cached.
    """
    cache_key = f"recent_files_{pipeline_id}_{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached, True

    query_kwargs: dict[str, Any] = {
        "IndexName": "PipelineIndex",
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        "FilterExpression": Attr("step_name").eq("file_validation") & Attr("status").eq("success"),
        "ScanIndexForward": False,  # Newest first
        "Limit": _ENRICHMENT_PAGE_LIMIT,
    }

    recent_files: list[dict[str, Any]] = []
    scanned = 0
    for page in range(_MAX_ENRICHMENT_READ_PAGES):
        response = state_table.query(**query_kwargs)
        scanned += response.get("ScannedCount", 0)

        for item in response.get("Items", []):
            original_s3_uri = item_str(item, "original_s3_uri")
            file_key = original_s3_uri.split("/")[-1] if original_s3_uri else ""
            if file_key and not file_key.endswith("/"):
                recent_files.append(
                    {"file_key": file_key, "processed_at": item_str(item, "timestamp"), "status": "success"}
                )
                if len(recent_files) >= limit:
                    logger.info(
                        f"Recent files for {pipeline_id}: scanned {scanned} over {page + 1} page(s), "
                        f"found {len(recent_files)} matching files"
                    )
                    _set_cache(cache_key, recent_files)
                    return recent_files, True

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            logger.info(
                f"Recent files for {pipeline_id}: scanned {scanned} over {page + 1} page(s) to the end of the "
                f"partition, found {len(recent_files)} matching files"
            )
            _set_cache(cache_key, recent_files)
            return recent_files, True
        if time.monotonic() >= deadline:
            logger.warning(
                f"Recent files for {pipeline_id} stopped by the read deadline after {page + 1} pages "
                f"({scanned} rows examined); {len(recent_files)} of {limit} found, so the preview is a prefix"
            )
            return recent_files, False
        query_kwargs["ExclusiveStartKey"] = last_key

    logger.warning(
        f"Recent files for {pipeline_id} hit the {_MAX_ENRICHMENT_READ_PAGES}-page cap "
        f"({scanned} rows examined); {len(recent_files)} of {limit} found, so the preview is a prefix"
    )
    return recent_files, False


def get_pipeline_execution_counts(pipeline_id: str, limit: int = _EXECUTIONS_PROBE_LIMIT) -> tuple[int, int]:
    """Running and awaiting-approval counts among the pipeline's newest executions.

    **Cached as-is — this read was already correct.** It wants the latest ``limit``
    rows of the executions base table and takes them; there is no
    ``FilterExpression`` for the ``Limit`` to be applied ahead of, so it neither
    truncates an answer nor needs a deadline, and it returns no completeness flag
    of its own. R8.2's caching is the whole change here.

    The two counts are cached rather than the raw items, because they are all the
    caller uses. ``live_status`` stays derived at the call site: it starts from the
    pipeline record's own ``status``, which is not part of this read and must not
    be baked into a per-pipeline cache entry shared across requests.

    Raises whatever the read raises. The caller swallows it, exactly as before —
    see the note on the failure-isolation boundary in ``get_pipelines``.
    """
    cache_key = f"execution_counts_{pipeline_id}_{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached[0], cached[1]

    exec_resp = executions_table.query(
        KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        ScanIndexForward=False,
        Limit=limit,
    )

    running_count = 0
    approval_count = 0
    for ex in exec_resp.get("Items", []):
        ex_status = item_str(ex, "status")
        if ex_status == "WAITING_APPROVAL" or ex.get("pending_approval_id"):
            approval_count += 1
        elif ex_status == "RUNNING":
            running_count += 1

    _set_cache(cache_key, (running_count, approval_count))
    return running_count, approval_count


def get_pipelines(page: int = 1, page_size: int = 20, search: str = "", status_filter: str = "") -> dict[str, Any]:
    """Get paginated pipeline metadata with search and filtering

    Args:
        page: Page number (1-indexed)
        page_size: Number of pipelines per page (max 100)
        search: Search term for pipeline name/ID
        status_filter: Filter by status (active/archived/error)

    Returns:
        {
            'pipelines': [...],
            'total_count': int,
            'page': int,
            'page_size': int,
            'total_pages': int,
            'has_more': bool,
            # The two envelope markers tasks D8 and D9 added. Both always present;
            # `[]` from each means "nothing here is short". See their construction
            # sites below for what each one says and why they are not spellings of
            # each other.
            'partial_counts': [...],
            'enrichment_abandoned': [...]
        }
    """
    # The anchor BOTH read deadlines are derived from, so they are shares of one
    # budget rather than two independent timers. Taken before the page scan, not
    # after it, so the window left for enrichment is what remains of the budget
    # rather than a fixed duration — a slow scan shrinks it, and since task D8 the
    # scan can no longer shrink it without bound because it has a deadline of its
    # own (`_page_scan_deadline`). `time.monotonic` because a wall-clock adjustment
    # mid-request must not make the budget appear to run backwards.
    request_started = time.monotonic()
    try:
        # Validate and sanitize inputs
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))

        # STEP 1: Read the pipeline set from the pipelines table. This said "get all
        # pipelines" until task D10, which task D8 had already made an overclaim: the
        # walk is bounded by a page cap and a deadline, so what `all_pipelines` holds
        # below may be a prefix of the filtered set rather than the whole of it. The
        # name is kept; `page_set_complete` is what says whether it earned it.
        scan_kwargs: dict[str, Any] = {}

        # Default: exclude archived pipelines unless explicitly requested
        if status_filter:
            scan_kwargs["FilterExpression"] = Attr("status").eq(status_filter)
        else:
            # Default behavior: show only active pipelines
            scan_kwargs["FilterExpression"] = Attr("status").ne("archived")

        # Paginated under a page cap and a deadline since task D8 — the old single
        # `scan` silently dropped everything past the first 1 MB and still reported
        # `has_more: False`. `page_set_complete` False means what came back is a
        # prefix, which every envelope value below is then marked as resting on.
        all_pipelines, page_set_complete = _scan_pipeline_page_set(_page_scan_deadline(request_started), **scan_kwargs)

        # STEP 2: Apply search filter
        if search:
            search_lower = search.lower()
            all_pipelines = [
                p
                for p in all_pipelines
                if search_lower in item_str(p, "pipeline_id").lower()
                or search_lower in item_str(p, "pipeline_name").lower()
            ]

        # STEP 3: Sort by updated_at (newest first)
        all_pipelines.sort(key=lambda x: item_str(x, "updated_at"), reverse=True)

        total_count = len(all_pipelines)
        total_pages = (total_count + page_size - 1) // page_size

        # STEP 4: Paginate
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_pipelines = all_pipelines[start_idx:end_idx]

        # The order STEP 3 just established, by pipeline_id. The re-sort after the
        # fan-out restores *this* rather than re-deriving the order from each
        # enriched entry's `updated_at` — see the comment at that sort for the
        # defect that fixes (task D8, D5's fourth finding).
        page_order = {item_str(item, "pipeline_id"): position for position, item in enumerate(page_pipelines)}

        logger.info(f"Page {page}/{total_pages}: Showing {len(page_pipelines)} of {total_count} pipelines")

        # Every enrichment read in this request shares one deadline, derived from
        # what is left of the budget now that the page scan has returned (C23,
        # LGR-14d6dd3-2). Task D9 bounds the fan-out below with this same value
        # rather than deriving a second one.
        read_deadline = _list_read_deadline(request_started)

        # STEP 5: Enrich each pipeline in parallel — schema-version preview
        # (`_SCHEMA_HISTORY_PREVIEW_LIMIT`, three, not the ten this comment claimed
        # until task D10), file count, mapping count, recent files, live status.
        def enrich_pipeline(pipeline_item: dict[str, Any]) -> dict[str, Any]:
            pipeline_id = item_str(pipeline_item, "pipeline_id")

            # Keys whose value below is a floor or a prefix rather than the whole
            # answer. Named per key so a client can tell which one is short; `[]`
            # means every read completed. See the module docstring for why `0` on
            # its own is not an acceptable answer here (LGR-b2f79ef-14).
            partial_fields: list[str] = []

            # Three versions for the list view, and — since D7 — three versions'
            # worth of reading rather than up to ten pages of it (R8.3).
            #
            # **Deliberately NOT wrapped in try/except**, unlike the three reads
            # below. This is the only enrichment read whose exception reaches the
            # per-future catch in the fan-out, so it is the only one that can produce
            # the unenriched stub R8.5 preserves. Wrapping it here would silently
            # retire that path and the test that pins it
            # (`test_read_path_errors.py::test_list_view_enrichment_degrades_per_pipeline_on_throttle`).
            schema_history, versions_absolute = get_pipeline_schema_history_preview(pipeline_id, read_deadline)
            if not versions_absolute:
                # The walk stopped before reading the whole partition, so it never
                # established how many versions exist. `schema_version` is then a
                # floor and each `version` number is an ordinal within what was read —
                # both marked, because an absolute-looking number that is relative is
                # the same failure as a floor reported as a total (`LGR-b2f79ef-14`).
                partial_fields.extend(("schema_version", "schema_history"))

            # OPTIMIZATION: Use COUNT query for file count (faster than fetching items)
            file_count = get_pipeline_file_count(pipeline_id)

            # Field count from the newest schema_mapping step. Bounded, paginated
            # and cached — see `get_pipeline_mapping_count`. The `except` stays
            # here rather than moving inside the helper so the failure-isolation
            # boundary does not move: a failed read still degrades this one field
            # instead of turning the whole entry into a stub.
            try:
                mapping_count, mapping_complete = get_pipeline_mapping_count(pipeline_id, read_deadline)
            except Exception as mapping_err:
                logger.error(f"Error fetching mapping count for {pipeline_id}: {mapping_err}")
                mapping_count, mapping_complete = 0, False
            if not mapping_complete:
                partial_fields.append("mapping_count")

            # Recent files preview, same shape and same reason as above.
            try:
                recent_files, files_complete = get_pipeline_recent_files(pipeline_id, read_deadline)
            except Exception as files_err:
                logger.error(f"Error fetching recent files for {pipeline_id}: {files_err}")
                recent_files, files_complete = [], False
            if not files_complete:
                partial_fields.append("all_files")

            # Derive live_status from latest executions
            live_status = item_str(pipeline_item, "status", "active")
            running_count = 0
            approval_count = 0
            try:
                running_count, approval_count = get_pipeline_execution_counts(pipeline_id)

                if approval_count > 0:
                    live_status = "approval_required"
                elif running_count > 0:
                    live_status = "running"
            except Exception as live_err:
                logger.debug(f"Failed to derive live_status for {pipeline_id}: {live_err}")
                # Both counts are floors, and `status` is *derived* from them — so
                # it is marked too, following D4's rule that a derived value is
                # listed when either of its inputs is (`stats.py`, on rates). It is
                # the one of the three the UI actually renders
                # (`PipelineListPage.tsx:52`), which is what would make leaving it
                # unmarked the dishonest half of an otherwise honest payload.
                partial_fields.extend(("running_count", "approval_count", "status"))

            return {
                "pipeline_key": pipeline_id,
                "pipeline_id": pipeline_id,
                "pipeline_name": pipeline_item.get("pipeline_name", pipeline_id),
                "target_schema_name": pipeline_item.get("target_schema_name", ""),
                "ai_model": pipeline_item.get("ai_model", ""),
                "pipeline_description": pipeline_item.get("pipeline_description", ""),
                "status": live_status,
                "pipeline_status": pipeline_item.get("status", "active"),
                "running_count": running_count,
                "approval_count": approval_count,
                "created_at": pipeline_item.get("created_at", ""),
                "updated_at": pipeline_item.get("updated_at", ""),
                "file_count": file_count,
                "mapping_count": mapping_count,
                "all_files": recent_files,
                # p47 R8.3 / D7. **Was `len(schema_history)`**, which is a page length
                # and not a version: sliced to three, it reported `3` forever for
                # every pipeline past its third revision (D5's DEFECT #3). The
                # newest entry's own `version` is the figure D5 identified as already
                # correct in the same payload, so the two numbers now agree instead of
                # contradicting each other. A floor when `versions_absolute` is False,
                # and named in `partial_fields` when it is.
                "schema_version": schema_history[-1]["version"] if schema_history else 0,
                "schema_history": schema_history,
                "current_schema_hash": schema_history[-1]["hash"] if schema_history else None,
                "glue_job_metadata": pipeline_item.get("glue_job_metadata", {}),
                # P17 Requirement 6.7: surface source_type and source_config.
                # Default to S3_UPLOAD / {} for pre-P17 records that don't have
                # these fields persisted (Requirement 19 — backward compat).
                "source_type": pipeline_item.get("source_type", "S3_UPLOAD"),
                "source_config": pipeline_item.get("source_config", {}),
                # p47 R8.2 / D6. Always present; `[]` means every value above is a
                # whole answer. A key named here is a floor or a prefix, because a
                # bound stopped the read that would have established it — never
                # reported as `0`/`[]` on its own (LGR-b2f79ef-14). Mirrors
                # `stats.py`'s `partial_counts`; nothing renders it yet.
                "partial_fields": partial_fields,
            }

        # Parallel enrichment: max 10 concurrent queries, bounded to keep DynamoDB
        # from throttling. Until task D10 this comment claimed a five-wide pool, over
        # a pool that has been `max_workers=10` throughout; R8.4 resolved the
        # disagreement in favour of **the code**, so the width is unchanged and the
        # comment now matches it. Ten is also the width `schemas.py`'s listing
        # fan-out runs at (`LGR-14d6dd3-2`, which records that no ledger record
        # governs or fences either pool's width). The old wording is deliberately
        # not quoted here: `test_the_worker_pool_is_ten_wide_and_its_comment_agrees`
        # pins its absence from this file, so a quotation would read as a relapse.
        enriched_pipelines = []

        # Pipelines whose enrichment was still outstanding when the fan-out
        # deadline fired, in page order. `[]` means every future was consumed.
        # See the module docstring for why this is a third marker rather than an
        # entry in either existing one.
        enrichment_abandoned: list[str] = []

        # **NOT a context manager, and this is the single most likely thing for a
        # later reader to "clean up".** `Executor.__exit__` calls
        # `shutdown(wait=True)`, which blocks until every submitted read finishes —
        # so a deadline followed by a `with`-block exit would return *after* the
        # very overrun the deadline exists to prevent, making it decorative.
        # `try`/`finally` with `shutdown(wait=False, cancel_futures=True)` is what
        # actually stops waiting. Same shape and same reason as
        # `schemas.py:_resolve_display_labels`, which C23 / LGR-14d6dd3-2 records.
        # `cancel_futures` needs Python 3.9+; the runtime is PYTHON_3_12
        # (`compute-stack.ts:143`).
        executor = ThreadPoolExecutor(max_workers=10)
        try:
            futures = {executor.submit(enrich_pipeline, p): p for p in page_pipelines}
            # Consumed as each future is yielded rather than `future.done()`-tested
            # after the timeout: a future can still complete in the instant between
            # the timeout firing and any such check, so a `done()` sweep would narrow
            # the window without closing it while making the outcome depend on a race.
            # The systematic case it would be trying to catch — a read that stopped
            # *at* the deadline and returned just after — is handled by giving the
            # collection a named grace instead, which closes it by construction.
            outstanding = dict(futures)
            try:
                # The bound task D9 adds, and it is a bound on **how long this
                # handler waits**, not on how long the threads run: `cancel_futures`
                # cannot cancel a future that has already started, so a worker
                # mid-DynamoDB-call runs to completion (or is frozen with the
                # container on return). Nothing consumes what it produces.
                #
                # The instant is `read_deadline` — the one already derived above from
                # this request's remaining budget and already threaded into every
                # enrichment read — not a second one. That is what makes the bound
                # hold "by construction and independently of per-read latency"
                # (C23, LGR-14d6dd3-2) while keeping one budget with named shares
                # rather than three independent timers.
                #
                # Plus `_FAN_OUT_COLLECT_GRACE_SECONDS`, which is a share of the
                # reserve and not a timer of its own: a read stopped *by* the deadline
                # returns just after it, so waiting exactly to the instant would throw
                # away every floor D6 and D7 mark and replace it with a stub that says
                # less. The constant carries the measurement.
                collect_until = read_deadline + _FAN_OUT_COLLECT_GRACE_SECONDS
                for future in as_completed(futures, timeout=max(0.0, collect_until - time.monotonic())):
                    pipeline = outstanding.pop(future)
                    try:
                        enriched_pipelines.append(future.result())
                    except Exception as e:
                        logger.error(f"Error enriching pipeline {item_str(pipeline, 'pipeline_id')}: {e}")
                        # Add pipeline without enrichment
                        enriched_pipelines.append(_unenriched_stub(pipeline))
            except TimeoutError:
                # `as_completed`'s own timeout. Spelled as the builtin because
                # `concurrent.futures.TimeoutError` has been an alias of it since
                # Python 3.11 and the runtime is 3.12 — one spelling, correct on
                # both.
                #
                # Every page member still ships, with the same stub an enrichment
                # failure produces (R8.5's property one level up: the page degrades
                # entries, never its length or its order). Which entry it was is
                # named on the envelope, so a stub-by-clock is distinguishable from
                # a stub-by-failed-read instead of the two being conflated.
                for pipeline in outstanding.values():
                    enrichment_abandoned.append(item_str(pipeline, "pipeline_id"))
                    enriched_pipelines.append(_unenriched_stub(pipeline))
                logger.warning(
                    f"Enrichment fan-out stopped waiting at the read deadline plus its "
                    f"{_FAN_OUT_COLLECT_GRACE_SECONDS}s collection grace with {len(enrichment_abandoned)} of "
                    f"{len(page_pipelines)} pipeline(s) outstanding; each ships as an unenriched entry named "
                    f"in enrichment_abandoned"
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Page order, so the marker is deterministic and assertable even though
        # which futures were outstanding is not. Same anchor the re-sort below uses.
        enrichment_abandoned.sort(key=lambda pipeline_id: page_order.get(pipeline_id, len(page_order)))

        # Restore the page order STEP 3 established. The fan-out completes out of
        # order, so a re-sort is needed; what changed in task D8 is what it sorts
        # *on*. It read `updated_at` off each enriched entry, and an unenriched stub
        # carries no `updated_at` (the six-key literal above omits it), so
        # `item_str` yielded "" and `reverse=True` sorted every stub last —
        # measured by D5 as the newest pipeline appearing at the bottom of the page
        # the moment one of its reads failed, under a comment reading "Sort to
        # maintain original order". Sorting by the recorded position makes that
        # comment true: the healthy order is byte-identical (it *is* STEP 3's
        # order) and a stub now keeps the position its record earned.
        #
        # This is D8's rather than D9's because it is part of how the page is
        # selected and ordered, not part of how the fan-out is bounded; D9 changes
        # neither the page order nor the stub literal.
        enriched_pipelines.sort(key=lambda x: page_order.get(item_str(x, "pipeline_id"), len(page_order)))

        # R8.1's marking, task D8. Every value here is derived from the page read's
        # result set, so a stopped walk makes all four unreliable together: the
        # count is a floor, the two figures derived from it inherit that, and the
        # page itself is a selection over a subset rather than page `page` of the
        # whole. `[]` means the walk read the filtered set to its end. Always
        # present, so the key set stays assertable with `==`. Mirrors D4's
        # `partial_counts` in `stats.py`; scoped to the envelope, where
        # `partial_fields` is scoped to one entry, and the two vocabularies share no
        # key name so a client never has to guess which it is reading.
        partial_counts = [] if page_set_complete else list(_ENVELOPE_DERIVED_KEY_ORDER)

        return create_response(
            200,
            {
                "pipelines": enriched_pipelines,
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_more": page < total_pages,
                "partial_counts": partial_counts,
                # Task D9's marker. A *third* condition, not a third spelling of one
                # of the two above: `partial_counts` says the page set is a prefix
                # (and it is not — the walk completed), `partial_fields` names keys
                # of an entry whose value is a floor (and an abandoned entry omits
                # those keys rather than carrying floors). What this says is "these
                # page members have no enrichment at all, because the clock ran out".
                # Ids rather than a count, so a client can tell *which*; always
                # present, `[]` when the deadline did not bind.
                "enrichment_abandoned": enrichment_abandoned,
            },
        )

    except Exception as e:
        logger.error(f"Error fetching pipelines: {e}")
        return handle_error(e, "fetching pipelines")


def _ensure_s3_eventbridge_enabled(bucket_name: str) -> bool:
    """
    Ensure S3 bucket has EventBridge notifications enabled.
    This is idempotent — safe to call multiple times.
    """
    try:
        try:
            current_config = s3_client.get_bucket_notification_configuration(Bucket=bucket_name)
        except Exception:
            current_config = {}

        if "EventBridgeConfiguration" in current_config:
            logger.info(f"EventBridge already enabled on bucket: {bucket_name}")
            return True

        new_config = {"EventBridgeConfiguration": {}}
        for key in ["LambdaFunctionConfigurations", "TopicConfigurations", "QueueConfigurations"]:
            if key in current_config:
                new_config[key] = current_config[key]

        s3_client.put_bucket_notification_configuration(
            Bucket=bucket_name,
            NotificationConfiguration=new_config,
        )
        logger.info(f"EventBridge notifications enabled on bucket: {bucket_name}")
        return True
    except Exception as e:
        logger.warning(f"Could not enable EventBridge on bucket {bucket_name}: {e}")
        return False


def _is_missing_rule_error(exc: BaseException) -> bool:
    """Whether an EventBridge call failed because the rule does not exist.

    Read from the AWS error code where there is one, and from the message as a
    fallback for a client that raises plain exceptions (the unit tests' stubs do).
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        return bool(response["Error"].get("Code") == "ResourceNotFoundException")
    return "ResourceNotFoundException" in str(exc)


def _raise_for_failed_target_entries(response: Any, what: str) -> None:
    """Raise if a PutTargets or RemoveTargets response reports a failed entry.

    Both calls return HTTP 200 with ``FailedEntryCount`` and ``FailedEntries``
    when one or more targets could not be put or removed, so the SDK raises
    nothing and the caller must look. A response that is not a dict (a stubbed
    client) is treated as having no failures.
    """
    failed_count = response.get("FailedEntryCount", 0) if isinstance(response, dict) else 0
    if not failed_count:
        return
    entries: list[Any] = list(response.get("FailedEntries") or [])
    detail = "; ".join(
        f"{entry.get('TargetId', '?')}: {entry.get('ErrorCode', '?')} {entry.get('ErrorMessage', '')}".strip()
        for entry in entries
        if isinstance(entry, dict)
    )
    raise RuntimeError(
        f"EventBridge could not {what} ({failed_count} failed target{'s' if failed_count != 1 else ''}): "
        f"{detail or 'no detail returned'}"
    )


def _schedule_target_input(pipeline_id: str, source_prefix: str, source_type: str) -> dict[str, str]:
    """The event a pipeline's schedule rule sends the pipeline trigger.

    The consumer is ``pipeline_trigger._handle_scheduled_trigger``, which routes
    on ``trigger == "scheduled"`` and then branches on ``source_type``, defaulting
    an absent one to ``S3_UPLOAD``. Until 2026-10-10 this payload carried no
    ``source_type``, so every JDBC schedule took the S3 branch, listed an upload
    prefix the pipeline never uses, and returned "No files to process" with no
    run started. ``tests/contracts/test_schedule_payload_cross_lambda.py`` feeds
    this function's output to the real trigger handler.
    """
    return {
        "trigger": "scheduled",
        "pipeline_id": pipeline_id,
        "source_type": source_type,
        "source_prefix": source_prefix,
    }


def _manage_schedule_rule(
    pipeline_id: str,
    schedule_expression: str | None,
    pipeline_trigger_arn: str,
    source_prefix: str,
    *,
    source_type: str | None = None,
) -> dict:
    """Create, update, or delete an EventBridge schedule rule for a pipeline.

    Args:
        pipeline_id: The pipeline identifier.
        schedule_expression: A valid EventBridge cron/rate expression, or None to delete.
        pipeline_trigger_arn: ARN of the Pipeline_Trigger_Lambda.
        source_prefix: The S3 prefix for the pipeline's source files.
        source_type: The pipeline's ``source_type``, required to create or update
            (it decides which branch the trigger runs). Ignored when deleting.

    Returns:
        dict with 'action' key indicating what was done.

    Raises:
        ValueError: EventBridge refused the schedule expression. Callers map this
            to a 400; nothing was created.
        RuntimeError: ``source_type`` is missing or unknown (a caller bug), or
            ``put_targets`` reported a failed entry. In the second case the rule
            exists, possibly with no target, and the caller must remove it.
    """
    import boto3 as _boto3

    events_client = _boto3.client("events")
    rule_name = f"agentic-data-ingestion-schedule-{pipeline_id}"

    if not schedule_expression:
        # Delete existing rule. A missing rule at any step is success; anything
        # else raises, so a caller can tell "gone" from "still there". Each call
        # is its own try, so a failure the helper raises itself (a failed
        # RemoveTargets entry) can never be mistaken for a missing rule.
        try:
            targets_resp = events_client.list_targets_by_rule(Rule=rule_name)
        except Exception as e:
            if _is_missing_rule_error(e):
                return {"action": "deleted", "rule_name": rule_name}
            raise
        target_ids = [t["Id"] for t in targets_resp.get("Targets", [])]
        if target_ids:
            try:
                remove_resp: Any = events_client.remove_targets(Rule=rule_name, Ids=target_ids)
            except Exception as e:
                if _is_missing_rule_error(e):
                    return {"action": "deleted", "rule_name": rule_name}
                raise
            # RemoveTargets reports a target it could not remove the same way
            # PutTargets does: in the body, with HTTP 200
            # (https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_RemoveTargets.html).
            _raise_for_failed_target_entries(remove_resp, f"remove the trigger from schedule rule {rule_name}")
        try:
            events_client.delete_rule(Name=rule_name)
        except Exception as e:
            if not _is_missing_rule_error(e):
                raise
        logger.info(f"Deleted schedule rule {rule_name}")
        return {"action": "deleted", "rule_name": rule_name}

    # Required, and checked before anything is created: a missing value is what
    # sent every JDBC schedule down the trigger's S3 branch.
    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise RuntimeError(
            f"Cannot create schedule rule {rule_name}: source_type must be one of "
            f"{sorted(_ALLOWED_SOURCE_TYPES)}, got {source_type!r}"
        )

    # Create or update the rule — EventBridge validates the expression
    try:
        events_client.put_rule(
            Name=rule_name,
            ScheduleExpression=schedule_expression,
            State="ENABLED",
            Description=f"Scheduled trigger for pipeline {pipeline_id}",
        )
    except events_client.exceptions.ValidationException as e:
        raise ValueError(f"Invalid schedule expression: {schedule_expression}") from e

    # Set the trigger Lambda as target with pipeline context
    put_targets_resp: Any = events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "pipeline-trigger",
                "Arn": pipeline_trigger_arn,
                "Input": json.dumps(_schedule_target_input(pipeline_id, source_prefix, source_type)),
            }
        ],
    )
    # PutTargets reports a per-target failure in the response body and still
    # returns HTTP 200, so no exception is raised for it
    # (https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutTargets.html).
    # Unchecked, a rule with no target was reported as "created" and the
    # schedule never fired.
    _raise_for_failed_target_entries(put_targets_resp, f"attach the trigger to schedule rule {rule_name}")
    logger.info(f"Created/updated schedule rule {rule_name} with expression: {schedule_expression}")
    return {"action": "created", "rule_name": rule_name}


def _submitted_selected_worksheet(data: dict[str, Any]) -> str | None:
    """The Selected_Worksheet to persist, or ``None`` to write no attribute.

    p55 R8.4, the writer half of design §4.4 (decision W3-D5 arm (a)). The reader
    is ``schema_extract._configured_worksheet``.

    **Absent and empty are different facts, and this function is where the
    difference is enforced on the way in.** The reader tests
    ``SELECTED_WORKSHEET_ATTRIBUTE in record`` — a membership test, never a truth
    test — so:

    * attribute **absent** → no human has chosen. A single-sheet workbook proceeds
      (R8.1); a multi-sheet one refuses and names every Worksheet present (R8.4
      residual).
    * attribute **present**, *including* ``""`` → that value is a *selection*, and
      an empty one names no sheet, so the run refuses under R8.5 rather than
      falling back to the workbook's active sheet.

    So persisting ``""`` for "the user chose nothing" would break every
    single-sheet Excel pipeline created through the wizard. Returning ``None``
    rather than ``""`` is what keeps the two apart, and the caller writes nothing
    at all in that case. ``LGR-44d601e-1`` (**template / ratified**) settled the
    same question for the Display_Label in the same direction: clearing a value
    REMOVES the key, because "a stored ``\"\"`` would have to be distinguished
    from absence at every reader".

    **The gate trims and the stored value does not** — two different rules, both
    the producer's, and the same split ``_submitted_display_label`` already draws.
    A Worksheet name may legitimately carry padding, so trimming the value would
    make a sheet named ``" Data "`` permanently unselectable; but a
    whitespace-only submission is a user who typed nothing.

    A non-string submission (a number, a list, ``None``) is treated as no
    selection. It cannot have come from the wizard, and coercing it would record a
    selection the caller did not make. **It does NOT 400**: the wizard is the only
    producer and it validates client-side, and ``create_pipeline``'s permissive
    posture is governed by ``LGR-cad8032-2`` — an unverifiable claim is answered by
    writing no attribute, not by rejecting the request. Whether the named sheet
    exists is unknowable here in any case (no workbook exists at creation time),
    which is exactly why R8.5 checks it at run time and names both sides.
    """
    submitted = data.get(SELECTED_WORKSHEET_ATTRIBUTE)
    if not isinstance(submitted, str) or not submitted.strip():
        return None
    return submitted


class _WorksheetUnchanged:
    """The type of :data:`_WORKSHEET_UNCHANGED`.

    A named class rather than a bare ``object()`` so the third arm of
    :func:`_submitted_worksheet_edit`'s return type is expressible, and so a
    reader of that signature can see that there are exactly three arms.
    """


#: "The request said nothing about the Selected_Worksheet." Distinct from both
#: ``None`` (clear it) and ``""`` — see :func:`_submitted_worksheet_edit`.
_WORKSHEET_UNCHANGED: Final = _WorksheetUnchanged()


def _submitted_worksheet_edit(data: dict[str, Any]) -> str | None | _WorksheetUnchanged:
    """What an ``edit_pipeline`` body asks for the Selected_Worksheet.

    p55 R8.4's edit half. ``create_pipeline`` writes this attribute once and
    nothing read it back or updated it, so a pipeline created without a worksheet
    — the default, since the wizard field is optional and no workbook exists at
    creation time — had **no route to one at all**. The R8.4 refusal told the
    operator to set a worksheet the product gave them no way to set. This is the
    function that closes that.

    Three intents, and they must stay three, because the attribute's ABSENCE is
    load-bearing. The reader (``schema_extract._configured_worksheet``) tests
    ``SELECTED_WORKSHEET_ATTRIBUTE in record`` — a membership test, never a truth
    test — so:

    * key **omitted** from the body → :data:`_WORKSHEET_UNCHANGED`. Neither SET
      nor REMOVE, so an edit that only changes the description cannot silently
      clear a configured worksheet. This is the intent `edit_pipeline` expresses
      for every other field by omission, and it is why a two-state
      ``str | None`` return would have been wrong: ``None`` already means clear.
    * key present with a **non-blank string** → SET that string, **verbatim and
      untrimmed**. A Worksheet name may legitimately carry padding, so trimming
      would make a sheet named ``" Data "`` permanently unselectable. The gate
      below trims and the stored value does not — the same split
      ``_submitted_selected_worksheet`` draws and ``worksheet-selection.ts``
      mirrors on the client.
    * key present with a **blank string** (``""`` or whitespace-only), or
      ``null`` → ``None``, meaning **REMOVE the attribute**, taking the record
      back to "no human has chosen".

    **Clearing REMOVES; it does not store ``""``.** Those are different facts and
    collapsing them would be the defect, not a simplification: a stored ``""`` is
    a *recorded selection naming no sheet*, which refuses under R8.5 on every
    subsequent run, so "clear the worksheet" would turn a working single-sheet
    pipeline into one that can never run again. ``LGR-44d601e-1`` (**template /
    ratified**) settled exactly this for the Display_Label in exactly this
    direction — clearing REMOVES the key, because "a stored ``\"\"`` would have to
    be distinguished from absence at every reader". The consequence is that this
    path cannot *produce* ``""``; that is deliberate, and it means no in-tree
    writer can, which leaves ``schema_extract``'s R8.5 empty-selection branch as
    a guard against a hand-edited record rather than against us.

    The value rule is :func:`_submitted_selected_worksheet`, reused rather than
    restated, so the create and edit paths cannot disagree about what counts as a
    selection. The only thing added here is the membership question, which
    ``create_pipeline`` does not need to ask.

    A non-string (a number, a list) therefore reads as "not a selection" → clear,
    which is what the create path already rules a non-string to be. It does **not**
    400: a rejection posture here would make the two halves of this attribute
    disagree about the same input, and ``create_pipeline``'s permissive posture is
    governed by ``LGR-cad8032-2``. Nor is the name validated for length or
    forbidden characters — that rule lives once, in
    ``dashboard-react/src/utils/worksheet-selection.ts``, applied by the form that
    submits, and ``_submitted_selected_worksheet``'s docstring records why the
    server does not restate it. Whether the sheet *exists* is still unknowable
    here, which is why R8.5 checks it at run time and names both sides.
    """
    if SELECTED_WORKSHEET_ATTRIBUTE not in data:
        return _WORKSHEET_UNCHANGED
    return _submitted_selected_worksheet(data)


#: The Approval_Config's attribute name on the pipeline record, which is also the
#: request key the wizard submits it under. One constant so the create path's
#: three arms below cannot drift from each other. ``edit_pipeline`` still spells
#: it literally, because there it is interpolated into DynamoDB expression
#: strings alongside per-key name placeholders and a constant would not read any
#: clearer; that is a deliberate non-change, not an oversight.
APPROVAL_CONFIG_KEY: Final = "approval_config"


def _default_approval_config() -> dict[str, Any]:
    """The seeded Approval_Config, applied ONLY when the request omits the key.

    p64 R10.2. A **function returning a fresh dict**, not a module constant: the
    result is handed straight to ``put_item`` as a nested map, and a shared
    mutable default is the kind of thing a later caller edits in place.

    The confidence threshold mirrors the enforced gate in ``shared/config.py``
    (P28 R18.2) — ``schema_mapping`` applies the shared constant, so this
    per-pipeline copy is informational. It is a ``Decimal`` because DynamoDB
    rejects ``float``.

    **Every value here is also each reader's own fallback, which is why arm 3 of
    :func:`_submitted_approval_config` can safely write nothing.** Verified at
    ``schema_mapping/lambda_function.py:1669`` (``schema_mapping_enabled``,
    default ``True``) and ``:1657`` (``always_approve_schema_changes``, default
    ``True``), and at ``test_code.resolve_code_approval_setting``, where only an
    explicit boolean ``False`` disables the gate (``LGR-69ca78b-1``'s read-side
    counterpart). So an absent attribute and this dict select the same gates; the
    difference is only what the record *claims*.
    """
    return {
        "schema_mapping_enabled": True,
        "schema_mapping_confidence_threshold": Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD)),
        "code_generation_enabled": True,
        "always_approve_schema_changes": True,
    }


#: The three Approval_Config gates. Each MUST be a JSON boolean — see
#: :func:`_validate_approval_config` for why nothing weaker will do.
_APPROVAL_CONFIG_GATE_KEYS: Final = (
    "schema_mapping_enabled",
    "code_generation_enabled",
    "always_approve_schema_changes",
)

#: The one non-boolean setting, and its bounds. The unit is a **fraction**, set
#: by this attribute's only writer (:func:`_default_approval_config` seeds
#: ``Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))``). The wizard's slider is a
#: percent and ``buildApprovalConfigRequest`` divides by 100 on the way out, so a
#: value above 1 arriving here means an unconverted percent — which is why the
#: refusal below names the unit as well as the bound.
_APPROVAL_CONFIG_THRESHOLD_KEY: Final = "schema_mapping_confidence_threshold"
_APPROVAL_CONFIG_THRESHOLD_MIN: Final = 0
_APPROVAL_CONFIG_THRESHOLD_MAX: Final = 1


def _glue_schema_name_from_id(target_schema_id: str, target_config: dict[str, Any] | None) -> str:
    """Resolve ``glue_{database}_{table}`` to the ``database.table`` form the
    mapping step consumes.

    **The boundary cannot be recovered from the id by splitting.** Both halves may
    contain underscores, so the id is ambiguous on its own. The previous
    implementation did ``id.replace("glue_", "").split("_", 1)`` and joined with a
    dot, which splits on the FIRST underscore while the real boundary is elsewhere.
    For a single-token database and table that happens to be right, which is why it
    survived; for anything else it silently emits a different name. Worked example
    from the failure that prompted this, database
    ``agentic_data_ingestion_jdbc_discovery_dev`` and table
    ``agentic_data_ingestion_966e101e04e7_appdb_customers``:

        glue_agentic_data_ingestion_jdbc_discovery_dev_agentic_..._customers
          → split("_", 1) → ("agentic", "data_ingestion_jdbc_discovery_dev_agentic_...")
          → "agentic.data_ingestion_jdbc_discovery_dev_agentic_..._customers"

    ``schema_mapping.get_glue_catalog_schema`` then does ``split(".", 1)`` and asks
    Glue for database ``agentic``, which does not exist. When the pipeline's
    ``target_type`` also routes it away from the Glue branch, the same string is
    interpolated into S3 keys instead and the run dies six probes later with
    ``Could not find uploaded schema``.

    Two authoritative sources are tried, in order, and neither guesses:

    1. ``target_config['database']`` / ``['table']``. Required by the GLUE_CATALOG
       validation above, so present whenever the *write* target is Glue Catalog.
    2. The Glue catalog itself — find the database whose name the stripped id
       actually starts with. This is what covers a Glue-catalog target SCHEMA
       paired with a non-Glue write target (a JDBC target, say), where
       ``target_config`` carries connection fields and no database/table.

    Only if both are unavailable does it fall back to the old split, and then it
    logs an ERROR naming the consequence rather than emitting a plausible-looking
    name silently.

    Args:
        target_schema_id: The wizard's schema id, beginning ``glue_``.
        target_config: The request's target config, if any.

    Returns:
        ``"{database}.{table}"``, or the stripped id when it carries no underscore
        to split on at all.
    """
    stripped = target_schema_id.removeprefix("glue_")

    # 1. The structured fields, when the write target is Glue Catalog.
    cfg = target_config or {}
    database = str(cfg.get("database") or "").strip()
    table = str(cfg.get("table") or "").strip()
    if database and table and stripped == f"{database}_{table}":
        return f"{database}.{table}"

    # 2. Ask Glue which database this actually is. One paginated ListDatabases
    #    call; the longest matching prefix wins so that a database named as a
    #    prefix of another cannot claim the id.
    try:
        glue = boto3.client("glue", region_name=AWS_REGION)
        names: list[str] = []
        paginator = glue.get_paginator("get_databases")
        for page in paginator.paginate():
            names.extend(db["Name"] for db in page.get("DatabaseList", []))
        matches = [n for n in names if stripped.startswith(f"{n}_")]
        if matches:
            best = max(matches, key=len)
            return f"{best}.{stripped[len(best) + 1 :]}"
        logger.warning(
            "No Glue database matches schema id %r; falling back to the ambiguous split",
            target_schema_id,
        )
    # Broad deliberately: this is a best-effort disambiguation, and any failure
    # (denied ListDatabases, throttle, transient) must fall through to the
    # historical split rather than fail pipeline creation.
    except Exception as e:
        logger.warning("Could not list Glue databases to resolve %r: %s", target_schema_id, e)

    # 3. Neither source available. Preserve the historical shape, but say so.
    if "_" not in stripped:
        return stripped
    head, tail = stripped.split("_", 1)
    logger.error(
        "Falling back to the ambiguous first-underscore split for Glue schema id %r. "
        "The resulting target_schema_name %r is correct only if the database is a "
        "single token; if it is not, schema mapping will fail to find this table.",
        target_schema_id,
        f"{head}.{tail}",
    )
    return f"{head}.{tail}"


def _target_schema_source_for_id(target_schema_id: str) -> str:
    """Where the target SCHEMA is read from, independent of where data is written.

    These are two different questions that ``target_type`` was answering at once,
    and conflating them is what broke run
    ``run-2026-09-18-18-40-20-4be9f1a6``: that pipeline writes to JDBC and takes
    its target schema from a Glue Catalog table, so the single ``target_type``
    attribute could only describe one of the two. It described the write target
    (``JDBC``), so ``schema_mapping`` routed the Glue table name into the
    uploaded-JSON branch and probed six S3 keys that could never exist.

    Derived from the schema id, which is the only value that actually identifies
    the schema. Written alongside ``target_type`` rather than replacing it,
    because existing records carry only ``target_type`` and the reader falls back
    to it.
    """
    if target_schema_id.startswith("glue_"):
        return "glue_catalog"
    if target_schema_id.startswith("s3_table_"):
        return "s3_tables"
    return "uploaded"


def _validate_approval_config(data: dict[str, Any]) -> None:
    """Refuse a ``create_pipeline`` body whose ``approval_config`` cannot mean what it says.

    p64 R10.4. Raises :class:`ValueError`; the caller turns that into a 400, which
    is the ``_validate_jdbc_source_config`` idiom already used a few lines below.
    Returns ``None`` for a body with no ``approval_config`` at all — absence is
    R10.2's compatibility arm and is not a malformed value.

    **Why a 400 and not this handler's usual write-nothing posture.**
    ``LGR-cad8032-2`` (**template / ratified**) rules that an unresolvable schema
    id still returns 201 and writes no address attributes, and
    :func:`_submitted_approval_config` cites it for arm 3. That record does not
    reach here, and the distinction is the reason this function exists: it governs
    an **unverifiable claim about external state** — whether an S3 object exists —
    where the writer's duty is to make no claim it cannot support. A threshold of
    ``1.5``, or a gate spelled ``"yes"``, needs no external read to be wrong; it is
    self-evidently unsatisfiable at the boundary. Refusing it is not the stronger
    creation-time-existence obligation that record declined to take on. The create
    path already refuses a bad ``schema_source``, an unknown ``source_type`` and an
    incomplete JDBC ``target_config``, so a shape refusal here is this handler's
    existing posture for malformed input rather than a new one.

    **The gates: a non-boolean is a 400, and fail-closed at the reader is NOT
    enough.** ``test_code.resolve_code_approval_setting`` checks ``raw is True or
    raw is False`` and treats anything else as absent, so ``code_generation_enabled``
    alone would indeed fail closed (``LGR-69ca78b-1``'s read-side counterpart). The
    other two do **not**:

    * ``schema_mapping/lambda_function.py:1669`` reads
      ``approval_config.get("schema_mapping_enabled", True)`` and then tests
      ``if not schema_mapping_enabled:`` — **truthiness**.
    * ``:1657`` is ``if approval_config.get("always_approve_schema_changes", True):``
      — **truthiness** again.

    So a submitted ``0``, ``""``, ``[]`` or ``None`` under either key **disables a
    governance gate**, silently, having passed no boolean check anywhere. That is
    the hazard p64 task 2a.1b named when it refused to accept a second wire
    spelling ("a second way for a non-boolean falsy value to disable a governance
    gate"), and validation at the boundary is the only place it closes: the readers
    live several Lambdas away and two of them cannot tell ``0`` from ``False``.
    ``LGR-cd58706-2`` settled the same question the same way one handler over — only
    a real JSON boolean is honoured for ``confirm``, because that field decides
    whether a write happens — and these three decide whether a human reviews one.

    **The threshold: refused, never clamped.** ``.kiro/specs/p63-inert-controls-and-gate-integrity``
    R9.3 requires a 400 naming the floor and explicitly rejects clamping, because a
    control displaying 70% while the system enforces 85% manufactures a new inert
    control — the defect class p63 exists to remove. p64 task 2a.1b already declined
    to clamp on the hydrate path for that reason. ``LGR-4ea43f3-4`` is the same
    posture for a different value class: an identifier over the length cap is
    refused, never truncated, because a silently altered value is well-formed,
    plausible and wrong.

    **What is deliberately NOT checked here, and it is not an oversight.** The
    range is ``[0, 1]`` and nothing is compared against
    ``CONFIDENCE_AUTO_APPROVE_THRESHOLD``. p63 R9/D2 make the shared constant a
    **floor** — a per-pipeline value below it is refused — and that resolver is
    p63's to build, for a concrete reason: the wizard's own default slider position
    is 80% → ``0.8``, which sits **below** the 0.85 floor, so adding the comparison
    here would 400 every untouched wizard submission. p63 R9.4 is the other half of
    that change (raise the slider's minimum to the floor) and the two must land
    together. Harmless meanwhile because the attribute still has **no reader** in
    ``lambda/``, ``shared/`` or ``mcp/`` — ``schema_mapping:1777`` and ``:1902``
    read the constant directly.

    **Unrecognised keys are left alone.** Validating only the four keys that have
    a meaning, rather than rejecting everything else, keeps
    ``test_unknown_keys_are_persisted_rather_than_filtered`` true and keeps a newer
    client compatible with an older Lambda — deployment drift is a measured
    property of this project, not a hypothesis (``LGR-c88d326-4``). An allowlist
    would also have to be amended by p64 task 2a.3 the moment
    ``infrastructure_approval_enabled`` gains a producer, and a stale Lambda would
    then 400 a current wizard. Key filtering is a different obligation from value
    validation and R10.4 asks for the second.
    """
    if APPROVAL_CONFIG_KEY not in data:
        return

    submitted = data[APPROVAL_CONFIG_KEY]
    if not isinstance(submitted, dict):
        raise ValueError(
            f"{APPROVAL_CONFIG_KEY} must be an object with the approval settings as its keys; "
            f"got {type(submitted).__name__}"
        )

    for key in _APPROVAL_CONFIG_GATE_KEYS:
        if key not in submitted:
            continue
        value = submitted[key]
        # Mirrors `resolve_code_approval_setting`'s `raw is True or raw is False`.
        # `isinstance(1, bool)` is False, so `1` and `0` are refused too — which is
        # the point, since `0` is falsy and would read as "gate off" at both
        # truthiness sites.
        if not isinstance(value, bool):
            raise ValueError(
                f"{APPROVAL_CONFIG_KEY}.{key} must be true or false; got {value!r}. "
                "A non-boolean value would be read as a gate setting by truthiness, "
                "so it could disable an approval gate without ever saying so."
            )

    if _APPROVAL_CONFIG_THRESHOLD_KEY in submitted:
        threshold = submitted[_APPROVAL_CONFIG_THRESHOLD_KEY]
        # `bool` is an `int` subclass, so it has to be excluded before the numeric
        # test or `True` would pass as the number 1.
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError(
                f"{APPROVAL_CONFIG_KEY}.{_APPROVAL_CONFIG_THRESHOLD_KEY} must be a number "
                f"between {_APPROVAL_CONFIG_THRESHOLD_MIN} and {_APPROVAL_CONFIG_THRESHOLD_MAX}; "
                f"got {threshold!r}"
            )
        # NaN and infinity fail this comparison, which is what should happen to
        # them; there is no separate check for either.
        if not _APPROVAL_CONFIG_THRESHOLD_MIN <= threshold <= _APPROVAL_CONFIG_THRESHOLD_MAX:
            raise ValueError(
                f"{APPROVAL_CONFIG_KEY}.{_APPROVAL_CONFIG_THRESHOLD_KEY} must be between "
                f"{_APPROVAL_CONFIG_THRESHOLD_MIN} and {_APPROVAL_CONFIG_THRESHOLD_MAX}; "
                f"got {threshold!r}. The unit is a fraction, not a percent — 0.8, not 80. "
                "The value is refused rather than clamped, so what is displayed and what is "
                "enforced stay the same number."
            )


def _submitted_approval_config(data: dict[str, Any]) -> dict[str, Any] | None:
    """What a ``create_pipeline`` body asks for the Approval_Config.

    p64 R10.1 / R10.2. ``create_pipeline`` used to assign the seeded defaults
    **unconditionally**, so a wizard that had already spent a whole step
    collecting these four settings had them discarded on the way past. The
    wizard does send them — ``CreatePipelinePage.tsx:495``,
    ``approval_config: state.approvalConfig`` — which is what made this a
    silent discard rather than an unused code path.

    **Three arms, and they must stay three.** The distinction is *omitted* versus
    *explicitly empty*, not truthy versus falsy, and collapsing the two is the
    defect R10.1/R10.2 name:

    * key **omitted** from the body → :func:`_default_approval_config`. R10.2:
      a client that predates this field is unaffected.
    * key present and a **dict** → that dict, **verbatim**. Including ``{}`` and
      including a dict whose every value is ``False``. R10.1 is a statement about
      *persistence*, so an explicit "no overrides" is recorded as an explicit
      "no overrides" and is **not** re-defaulted. A truth test here — ``if
      data.get(APPROVAL_CONFIG_KEY):`` — would read ``{}`` as absence and
      silently upgrade it to the defaults, which is the ``${VAR:-default}``
      substitutes-on-empty class of bug (p64 task 1d.4 hit it one Lambda over).
    * key present and **not a dict** (a string, a list, ``None``, ``False``) →
      ``None``, meaning **write no attribute**. Two reasons, and the first is
      the load-bearing one. (a) Every reader does
      ``record.get("approval_config", {}).get(key, default)``, so a stored
      scalar makes the *reader* raise ``AttributeError`` — persisting garbage
      here breaks pipelines at run time, several Lambdas away. (b) Re-defaulting
      instead would record four settings the caller never asked for. Writing
      nothing does neither, and it is fail-closed: see
      :func:`_default_approval_config` on why absence selects exactly the gates
      the defaults do.

    **Arm 3 is now a backstop, and ``create_pipeline`` no longer reaches it.**
    p64 task 2a.2 / R10.4 built the boundary refusal this docstring used to defer
    to: :func:`_validate_approval_config` runs at the top of ``create_pipeline``
    and a non-dict submission is a **400** there, so on that path arm 3 cannot
    fire. It is kept rather than deleted for two reasons. Its own reason is
    unchanged and still load-bearing — a stored scalar makes every reader raise
    ``AttributeError`` several Lambdas away, so if this function is ever called
    without the validator in front of it, writing nothing is the right answer and
    the readers' fail-closed defaults apply. And it is the shape p64 tasks 2c/2d
    copy for ``quality_config``: interpretation here, refusal at the boundary, two
    functions rather than one doing both.

    This did **not** overturn ``LGR-cad8032-2``, which is why the record is still
    cited above. That record governs an unverifiable claim about *external state*;
    a malformed container is wrong on its face at the boundary. The argument is at
    :func:`_validate_approval_config`, and the do-NOT that goes with it: do not
    "restore consistency" by making the validator return 201 on a non-dict.

    **``convert_floats_to_decimal`` is serialisation, not validation.**
    ``json.loads`` yields ``float`` for a JSON number with a fractional part and
    boto3 refuses ``float``, so persisting verbatim without this raises
    ``TypeError`` on ``put_item``. That is reachable, not hypothetical: the
    wizard's confidence slider is typed ``number``
    (``wizard/types.ts:12``), and ``hydrateWizardStateFromPipeline``
    (``CreatePipelinePage.tsx:269``) reads a previously-stored
    ``Decimal("0.85")`` back into it when a pipeline is cloned. The shared
    converter is the single copy P28 R19.2 consolidated six locals into — do not
    add a seventh, and do not hand-roll a coercion here.

    **The shape p64 tasks 2c/2d copy for ``quality_config``.** Take all four
    parts: (1) the defaults are a function, (2) membership decides whether they
    apply — never truthiness, (3) any dict of the right container type persists
    verbatim, (4) a wrong container type writes nothing and leaves the refusal to
    the validation task. Note what is *not* copied: this returns ``dict | None``
    with two arms, because ``create_pipeline`` has no "unchanged" intent. An edit
    path needs a **third** state, and the in-tree pattern for that is
    :func:`_submitted_worksheet_edit`'s named ``_WORKSHEET_UNCHANGED`` sentinel —
    ``None`` there already means "clear it", so a two-state return would be wrong
    (``LGR-44d601e-1`` / ``LGR-4ea43f3-2``).

    **The casing gap is CLOSED, and it was closed on the client — so this
    function must stay a verbatim persist.** The wizard used to post its own
    camelCase state (``schemaMappingEnabled``, …) against snake_case readers, so
    persisting verbatim landed four keys nothing read and left the step-5 toggles
    inert. ``.kiro/specs/p63-inert-controls-and-gate-integrity`` R1.2 requires
    exactly **one** normalisation authority, and p64 placed it at the wizard's
    request boundary: ``buildApprovalConfigRequest``
    (``CreatePipelinePage.tsx``) emits the four snake_case keys the readers read,
    with the confidence threshold converted from the slider's percent to the
    fraction this attribute is stored in.

    **Do NOT add a server-side camelCase normaliser here.** It would make a second
    spelling legal on the wire permanently with nothing to arbitrate between them,
    which is what R1.2 forbids; and because ``schema_mapping:1669`` tests ``not
    schema_mapping_enabled`` by *truthiness* rather than by identity, accepting a
    second spelling would newly let a non-boolean falsy value disable a governance
    gate through a key that reaches no reader today. Choosing the client side added
    no accepted wire shape at all — ``PipelineFormModal.tsx:103-106`` already
    submitted these exact snake_case keys on the edit path.

    ``test_create_pipeline_approval_config.py``'s ``TestReconciledCasing`` (formerly
    ``TestKnownCasingGap``) pins the reconciled behaviour from this side: the
    readers' keys survive verbatim, and a camelCase payload is still persisted
    as-sent — unrecognised, not translated.
    """
    if APPROVAL_CONFIG_KEY not in data:
        return _default_approval_config()

    submitted = data[APPROVAL_CONFIG_KEY]
    if not isinstance(submitted, dict):
        return None

    converted: dict[str, Any] = convert_floats_to_decimal(submitted)
    return converted


def _default_quality_config() -> dict[str, Any]:
    """The seeded Quality_Config, applied ONLY when the request omits the key.

    p64 R7.1 / R7.3. A **function returning a fresh dict** for the same reason
    :func:`_default_approval_config` is one: the result is handed straight to
    ``put_item`` as a nested map, and a shared mutable default is the kind of thing
    a later caller edits in place.

    **``data_quality_enabled`` is deliberately ABSENT from this map, and that
    absence is load-bearing.** It is the one Quality_Config key whose default is
    *per template* rather than flat, and R6.6 requires "no existing pipeline's
    behaviour changes" in **both** directions. ``code_assembly``'s
    ``resolve_data_quality_enabled`` honours an explicit ``bool`` and otherwise
    falls back to ``DQ_DEFAULT_ON_TEMPLATES`` — which is on for
    ``glue_job_s3_tables.py.j2`` and off for the other four, i.e. exactly the
    posture each template had before p64. So seeding ``False`` here would write an
    explicit opt-OUT onto every newly created S3 Tables pipeline, silently removing
    the row-level split and the quarantine write that target type has had since P28
    and moving its verdict from ``enforced`` to ``advisory``. That is the same
    silent reclassification R6.6 forbids, running the other way; 2b.4's own
    docstring names it as the reason its default is per-template.

    **The rule this establishes for whoever adds the next key.** Seed a key here
    only if its default is unconditional — the same value for every pipeline, every
    target type, every template. ``dedup_enabled`` and ``dedup_columns`` qualify:
    dedup is off everywhere today, so stating it costs nothing and is true. A key
    whose default depends on anything about the pipeline belongs at its resolver,
    left absent here.

    Every value below is also each reader's own fallback —
    ``code_assembly.resolve_quality_config`` returns ``dedup_enabled=False`` and
    ``dedup_columns=[]`` for a record with no ``quality_config`` at all — so an
    absent attribute and this dict select the same behaviour. The difference is only
    what the record *claims*, which is what makes it worth writing: the wizard's
    edit path (p64 2c.2) reads the record back, and a record that says nothing gives
    it nothing to show.
    """
    return {
        DEDUP_ENABLED_KEY: False,
        DEDUP_COLUMNS_KEY: [],
    }


#: The Quality_Config keys that MUST be a JSON boolean when present. All three are
#: read by truthiness or by identity several Lambdas away, and a non-boolean under
#: any of them is a way to flip a behaviour without saying so — see
#: :func:`_validate_quality_config`.
#:
#: ``nonprintable_check_enabled`` was added by p64 task 2d.3. 2d.2 left it out and
#: recorded why: its resolver (``code_assembly.resolve_quality_config``) does
#: ``value if isinstance(value, bool) else False``, so a non-boolean fails **closed**
#: and cannot turn the check on by accident — a weaker case than
#: ``data_quality_enabled``, whose per-template fallback can resolve a malformed
#: value to **on**. Included anyway, for two reasons. Failing closed still means a
#: submitted ``"true"`` is an opt-in that silently does nothing, which is the defect
#: class this spec exists to remove, and it is the one direction the resolver cannot
#: report: the check is off, no error is raised anywhere, and the operator's evidence
#: that they asked for it is a stored string nobody reads. And three sibling keys in
#: one sub-object, two of which refuse a bad boolean and one of which accepts it, is
#: a trap for the next reader of this function more than for any caller.
_QUALITY_CONFIG_BOOLEAN_KEYS: Final = (
    DATA_QUALITY_ENABLED_KEY,
    DEDUP_ENABLED_KEY,
    NONPRINTABLE_CHECK_ENABLED_KEY,
)


def _validate_quality_config(data: dict[str, Any]) -> None:
    """Refuse a ``create_pipeline`` body whose ``quality_config`` cannot mean what it says.

    p64 R7.1 / R7.2 / R10.4. Raises :class:`ValueError`; the caller turns that into
    a 400. The sibling of :func:`_validate_approval_config`, deliberately built to
    the same shape — interpretation in one function, refusal in another — because
    that is the split ``_submitted_approval_config``'s docstring told 2c to copy.

    Returns ``None`` for a body with no ``quality_config`` at all: absence is the
    compatibility arm, not a malformed value.

    **Why a 400 rather than this handler's write-nothing posture.**
    ``LGR-cad8032-2`` (**template / ratified**) rules that an unresolvable schema id
    still returns 201 and writes no address attributes. That record governs an
    **unverifiable claim about external state** — whether an S3 object exists —
    where the writer's duty is to make no claim it cannot support. A gate spelled
    ``"yes"``, or a dedup column list that is a string, needs no external read to be
    wrong; it is self-evidently unsatisfiable at the boundary. The create path
    already refuses a bad ``schema_source``, an unknown ``source_type``, an
    incomplete JDBC ``target_config`` and (since 2a.2) a malformed
    ``approval_config``, so a shape refusal here is this handler's existing posture.

    **The booleans: a non-boolean is a 400, and fail-closed at the reader is NOT
    enough.** This is the item p64 2b.4 handed forward explicitly, having called its
    own resolver "a floor, not a substitute".
    ``code_assembly.resolve_data_quality_enabled`` does check ``isinstance(explicit,
    bool)`` and falls through to the per-template default otherwise, so
    ``data_quality_enabled: "false"`` would indeed not enable anything — but for an
    S3 Tables pipeline it would fall through to ``True``, i.e. an operator who
    submitted a string meaning "off" gets DQ **on** and no error anywhere. Refusing
    at the boundary is the only place that closes, because the resolver lives in
    another Lambda and cannot report a 400.

    **``dedup_columns``: refused, never filtered or sanitised.** Each entry must be
    a string and must pass the Identifier_Allowlist, because these names are
    interpolated into ``dropDuplicates([...])`` in the generated Glue job (p64 2c.3)
    — a generated-code sink, so R8.5's rule applies and do-NOT 2 forbids weakening
    the allowlist at any sink. Dropping the offending entries instead was rejected
    for ``LGR-4ea43f3-4``'s reason one value class over: a silently altered value is
    well-formed, plausible and wrong, and a dedup key list quietly missing a column
    deduplicates on the wrong identity. ``validate_identifier`` itself is untouched
    (do-NOT 3); it is only called.

    In practice this refuses nothing legitimate: p55 R11.3's rename projection means
    every target column name reaching the generated job has already passed the same
    allowlist, so a dedup column that fails here could not have been a target column
    anyway.

    **Unrecognised keys are left alone**, for the reason
    :func:`_validate_approval_config` gives at length: key filtering is a different
    obligation from value validation, R10.4 asks for the second, and deployment
    drift is a measured property of this project (``LGR-c88d326-4``) — an allowlist
    would make a stale Lambda 400 a current wizard.
    """
    if QUALITY_CONFIG_KEY not in data:
        return

    submitted = data[QUALITY_CONFIG_KEY]
    if not isinstance(submitted, dict):
        raise ValueError(
            f"{QUALITY_CONFIG_KEY} must be an object with the quality settings as its keys; "
            f"got {type(submitted).__name__}"
        )

    for key in _QUALITY_CONFIG_BOOLEAN_KEYS:
        if key not in submitted:
            continue
        value = submitted[key]
        # `isinstance(1, bool)` is False, so `1` and `0` are refused too. That is
        # the point: `0` is falsy and would read as "off" at a truthiness site,
        # while `1` would read as "on" — neither having passed a boolean check.
        if not isinstance(value, bool):
            raise ValueError(
                f"{QUALITY_CONFIG_KEY}.{key} must be true or false; got {value!r}. "
                "A non-boolean value is not honoured as an opt-in, so it would "
                "silently leave this setting at its default instead of what was asked for."
            )

    if DEDUP_COLUMNS_KEY in submitted:
        columns = submitted[DEDUP_COLUMNS_KEY]
        if not isinstance(columns, list):
            raise ValueError(
                f"{QUALITY_CONFIG_KEY}.{DEDUP_COLUMNS_KEY} must be a list of column names; "
                f"got {type(columns).__name__}. An empty list means every mapped target "
                "column, which is the default."
            )
        for index, column in enumerate(columns):
            try:
                validate_identifier(column, f"{QUALITY_CONFIG_KEY}.{DEDUP_COLUMNS_KEY}[{index}]")
            except IdentifierValidationError as exc:
                raise ValueError(
                    f"{QUALITY_CONFIG_KEY}.{DEDUP_COLUMNS_KEY}[{index}] is not a usable column "
                    f"name: {exc}. These names are interpolated into the generated Glue job's "
                    "dropDuplicates call, so the entry is refused rather than dropped — a dedup "
                    "key list quietly missing a column deduplicates on the wrong identity."
                ) from exc


def _submitted_quality_config(data: dict[str, Any]) -> dict[str, Any] | None:
    """What a ``create_pipeline`` body asks for the Quality_Config.

    p64 R7.1 / R7.3. The sibling of :func:`_submitted_approval_config`, and a
    deliberate copy of the **four parts** that function's docstring names:

    1. the defaults are a **function** returning a fresh dict
       (:func:`_default_quality_config`), not a module constant, because the result
       goes into ``put_item`` as a nested map;
    2. **membership** decides whether the defaults apply — ``key not in data``,
       never ``if data.get(key):``, which would read ``{}`` as absence and silently
       upgrade an explicit "no overrides" to the seeded map. That is the
       ``${VAR:-default}`` substitutes-on-empty class of bug (p64 task 1d.4 hit it
       one Lambda over);
    3. any dict of the right container type persists **verbatim** — no merge, no
       top-up, no allowlist filtering;
    4. a wrong container type writes **no attribute**, and the refusal is the
       validator's job (:func:`_validate_quality_config`, a 400 at the boundary).

    R7.3 is why this exists at all: the setting must be settable **at pipeline
    creation**, not only by editing afterwards. The trap R7.3 names — that
    ``create_pipeline`` used to assign ``approval_config`` unconditionally and
    discard whatever the wizard sent — is **already fixed**, by p64 task 2a.1. So
    this copies the fixed pattern, not the broken one; there is no unconditional
    assignment left to inherit.

    **What is NOT copied.** ``_submitted_approval_config``'s ``dict | None`` return
    has two arms because ``create_pipeline`` has no "unchanged" intent — a create
    either states a value or does not. An **edit** path needs a third state, and the
    in-tree pattern for that is :func:`_submitted_worksheet_edit`'s named
    ``_WORKSHEET_UNCHANGED`` sentinel, because there ``None`` already means "clear
    it" (``LGR-44d601e-1`` / ``LGR-4ea43f3-2``). Nothing here touches an edit path;
    a future one must not reuse this two-state shape.

    **Arm 3 is a backstop this handler does not reach**, exactly as its sibling's is:
    :func:`_validate_quality_config` runs at the top of ``create_pipeline`` and 400s
    a non-dict first. It is kept because the function's contract is its own — every
    reader does ``record.get(QUALITY_CONFIG_KEY, {}).get(key, default)``, so a stored
    scalar makes the *reader* raise ``AttributeError`` several Lambdas away, and
    writing nothing is fail-closed (``resolve_quality_config`` and
    ``resolve_data_quality_enabled`` both treat absence as "nothing was asked for").

    **``convert_floats_to_decimal`` is serialisation, not validation.** No
    recognised key carries a number today, but arm 2 persists **verbatim**,
    including unrecognised keys, and ``json.loads`` yields ``float`` for any JSON
    number with a fractional part while boto3 refuses ``float``. Without this a
    forward-compatible client sending one key this Lambda does not know about would
    take down the whole create with a ``TypeError`` on ``put_item``. The shared
    converter is the single copy P28 R19.2 consolidated six locals into — do not add
    a seventh, and do not hand-roll a coercion here.

    **Casing: snake_case on the wire, normalised at the wizard, nothing translated
    here.** ``.kiro/specs/p63-inert-controls-and-gate-integrity`` R1.2 requires
    exactly **one** normalisation authority, and p64 2a.1b placed it at the wizard's
    request boundary for the Approval_Config. The Quality_Config follows the same
    convention: p64 2c.2's wizard serialiser emits these snake_case keys and this
    function persists them as sent. **Do NOT add a server-side camelCase normaliser
    here** — it would make a second spelling legal on the wire permanently with
    nothing to arbitrate between them, which is what R1.2 forbids and what 2a.1b
    superseded deliberately.
    """
    if QUALITY_CONFIG_KEY not in data:
        return _default_quality_config()

    submitted = data[QUALITY_CONFIG_KEY]
    if not isinstance(submitted, dict):
        return None

    converted: dict[str, Any] = convert_floats_to_decimal(submitted)
    return converted


def _provision_pipeline_resources(pipeline_id: str, pipeline_name: str) -> None:
    """Create S3 prefix and enable EventBridge on upload bucket."""
    upload_bucket = require_env("UPLOAD_BUCKET")

    # Create pipeline upload prefix
    s3_client.put_object(Bucket=upload_bucket, Key=f"{pipeline_id}/")
    logger.info(f"Created S3 prefix: s3://{upload_bucket}/{pipeline_id}/")

    # Enable EventBridge notifications on the bucket
    _ensure_s3_eventbridge_enabled(upload_bucket)


# ---------------------------------------------------------------------------
# Create-time failure: undo what this request made, and say so (2026-10-10)
# ---------------------------------------------------------------------------
#
# `create_pipeline` writes the pipeline row first and provisions after it. Two
# required stages used to log a failure and return 201 "Pipeline created
# successfully" anyway: the JDBC target's Glue connection (the first run then
# died with "A Glue connection is required for a JDBC target", which reads as a
# configuration mistake), and the requested schedule (the dashboard showed a
# schedule and a next run time for a rule that did not exist). The
# invalid-schedule 400 rolled back only the row, leaving any Glue connection and
# crawler the same request had created with no row to clean them up from.
#
# OWNERSHIP IS BY CONSTRUCTION, NEVER BY STORED NAME. Every name below is
# derived from this request's `pipeline_id`, which this request minted with a
# fresh uuid suffix, so nothing else can own it. A user-supplied
# `glue_connection_name` is never deleted (`LGR-19584a7-2`), and neither is
# `target_config.jdbc_connection_name`, which an API caller can set. A delete
# that finds nothing counts as done.

#: Machine-readable `reason` on a create-time provisioning failure, beside
#: `error` and `stage`, in the shape `jdbc_resources`' crawler classifier uses
#: (`LGR-b367259-1`).
_REASON_PROVISIONING_FAILED = "provisioning_failed"

#: AWS codes meaning "the request itself was malformed", which the caller can
#: fix. Everything else is a server-side failure, including AccessDenied: the
#: user is authorised, and what lacks a grant is this Lambda's role
#: (`LGR-b367259-1`).
_CALLER_FIXABLE_AWS_CODES = frozenset({"InvalidInputException", "ValidationException"})

#: AWS codes meaning "there was nothing to delete".
_ABSENT_AWS_CODES = frozenset({"EntityNotFoundException", "ResourceNotFoundException", "NoSuchKey"})


def _undo_pipeline_create(
    pipeline_id: str,
    source_type: str,
    source_config: dict[str, Any],
    target_config: dict[str, Any],
    *,
    folder_marker_written: bool,
    schedule_rule_may_exist: bool,
) -> dict[str, Any]:
    """Remove what this create request made, newest first; report what is left.

    Only resources this request could have created, by deterministic name. The
    pipeline row goes last, and only if everything else went: when something
    could not be removed, the row is set to ``archived`` instead, so the
    pipeline is not active and the dashboard's Delete (`delete_pipeline`, which
    removes the same names) can retry the cleanup. Never raises.

    Returns ``{"removed": [...], "residual": [...], "pipeline_record": ...}``,
    where ``pipeline_record`` is ``"deleted"``, ``"archived"`` or ``"remains"``.
    """
    from .jdbc_resources import _aws_error_code, _redact_aws_detail

    removed: list[str] = []
    residual: list[dict[str, str]] = []

    def attempt(label: str, delete: Any) -> None:
        try:
            delete()
            removed.append(label)
        except Exception as exc:
            if _aws_error_code(exc) in _ABSENT_AWS_CODES:
                return
            logger.error(f"Create rollback could not remove {label}: {exc}")
            residual.append({"resource": label, "error": _redact_aws_detail(exc)})

    if schedule_rule_may_exist:
        # Delete path of the same helper that created it; it already treats a
        # missing rule as success and raises anything else.
        attempt(
            f"schedule rule agentic-data-ingestion-schedule-{pipeline_id}",
            lambda: _manage_schedule_rule(pipeline_id, None, "", pipeline_id),
        )

    target_type = str(target_config.get("type") or "").upper()
    if target_type in ("JDBC", "RDS_DATABASE") and not target_config.get("glue_connection_name"):
        target_connection = f"agentic-data-ingestion-{pipeline_id}-rds"
        attempt(
            f"Glue connection {target_connection}",
            lambda: boto3.client("glue").delete_connection(ConnectionName=target_connection),
        )

    if source_type == "JDBC":
        crawler = f"agentic-data-ingestion-{pipeline_id}-source-discover"
        attempt(f"Glue crawler {crawler}", lambda: boto3.client("glue").delete_crawler(Name=crawler))
        if not source_config.get("glue_connection_name"):
            source_connection = f"agentic-data-ingestion-{pipeline_id}-source"
            attempt(
                f"Glue connection {source_connection}",
                lambda: boto3.client("glue").delete_connection(ConnectionName=source_connection),
            )

    # The zero-byte folder marker `_provision_pipeline_resources` writes, only
    # when it was written: that step is best-effort, so it may not have been.
    if folder_marker_written:
        attempt(
            f"upload folder marker {pipeline_id}/",
            lambda: s3_client.delete_object(Bucket=require_env("UPLOAD_BUCKET"), Key=f"{pipeline_id}/"),
        )

    if not residual:
        try:
            pipelines_table.delete_item(Key={"pipeline_id": pipeline_id})
            return {"removed": removed, "residual": residual, "pipeline_record": "deleted"}
        except Exception as exc:
            logger.error(f"Create rollback could not delete pipeline record {pipeline_id}: {exc}")
            residual.append({"resource": f"pipeline record {pipeline_id}", "error": _redact_aws_detail(exc)})
    try:
        # The same two attributes `archive_pipeline` sets.
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="SET #status = :archived, updated_at = :updated",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":archived": "archived", ":updated": datetime.now(UTC).isoformat()},
        )
        return {"removed": removed, "residual": residual, "pipeline_record": "archived"}
    except Exception as exc:
        logger.error(f"Create rollback could not archive pipeline record {pipeline_id}: {exc}")
        return {"removed": removed, "residual": residual, "pipeline_record": "remains"}


def _provisioning_failure_response(
    pipeline_id: str,
    stage: str,
    what_failed: str,
    exc: BaseException,
    undo: dict[str, Any],
) -> dict[str, Any]:
    """The create response for a required stage that failed after the row was written.

    400 when the caller can fix the request (a ``ValueError`` from our own
    validation, or an AWS input-validation code), 500 otherwise. The message
    says the pipeline was not created, what failed, and anything the rollback
    could not remove; the error detail is redacted (`LGR-b367259-1`).
    """
    from .jdbc_resources import _aws_error_code, _redact_aws_detail

    caller_fixable = isinstance(exc, ValueError) or _aws_error_code(exc) in _CALLER_FIXABLE_AWS_CODES
    message = f"Pipeline was not created: {what_failed}: {_redact_aws_detail(exc)}"
    if undo["residual"]:
        left = "; ".join(item["resource"] for item in undo["residual"])
        if undo["pipeline_record"] == "archived":
            message += (
                f". Could not remove: {left}. The pipeline was archived; delete it from the dashboard "
                "to retry the cleanup"
            )
        else:
            message += f". Could not remove: {left}"
    if undo["pipeline_record"] == "remains":
        message += f". The pipeline record {pipeline_id} could not be removed or archived"
    return create_response(
        400 if caller_fixable else 500,
        {
            "error": message,
            "reason": _REASON_PROVISIONING_FAILED,
            "stage": stage,
            "pipeline_id": pipeline_id,
            "cleanup": undo,
        },
    )


def create_pipeline(body: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a new pipeline and provision its S3 resources."""
    try:
        data = json.loads(body) if body else {}
        pipeline_name = data.get("pipeline_name")
        target_schema_id = data.get("target_schema")
        ai_model = data.get("ai_model")
        pipeline_description = data.get("description", "")
        created_by = data.get("created_by", "dashboard-user")
        target_config = data.get("target_config", {})  # NEW: Extract target_config from request
        schema_location = data.get("schema_location", "")  # S3 URI from upload (e.g. s3://bucket/key)

        # P19 Req 8.4: persist `schema_source` for diagnostic display on the
        # Pipeline Overview page. Downstream Lambdas continue reading
        # `schema_fields` — this field is purely informational. Validated
        # against the documented set; the new `mirror_source` value is
        # accepted as of P19.
        schema_source = data.get("schema_source", "detected")
        if schema_source not in ("detected", "upload", "existing", "mirror_source"):
            return create_response(
                400,
                {"error": f"Invalid schema_source: {schema_source!r}"},
            )

        # P17: Source abstraction. Default to S3_UPLOAD so every existing
        # client that does not yet pass source_type continues to behave as
        # today (Requirement 6.6, 19).
        source_type = data.get("source_type", "S3_UPLOAD")
        source_config = data.get("source_config", {})

        if not pipeline_name:
            return create_response(400, {"error": "pipeline_name is required"})

        if not target_schema_id:
            return create_response(400, {"error": "target_schema is required"})

        if not ai_model:
            return create_response(400, {"error": "ai_model is required"})

        # p64 R10.4 — the Approval_Config's VALUES are checked here, at the
        # boundary, and a malformed one is refused before any work is done. It is
        # deliberately this early rather than beside the persist site below: the
        # only reason to read a schema from S3 or derive an account id for a
        # request that is going to be refused is habit. The three gates must be
        # JSON booleans and the threshold a fraction in [0, 1]; the argument for
        # each, and the floor comparison this deliberately does NOT make, are in
        # `_validate_approval_config`'s docstring. Read it before relaxing any of
        # this — two of the three gates are read by truthiness, so a non-boolean
        # is a live way to turn an approval gate off.
        try:
            _validate_approval_config(data)
        except ValueError as ve:
            logger.warning("approval_config validation rejected create: %s", ve)
            return create_response(400, {"error": str(ve)})

        # p64 R7.1 / R7.2 / R10.4 — the Quality_Config's VALUES, checked at the
        # boundary beside the Approval_Config's and for the same reason: refuse
        # before doing any work. The two opt-ins must be JSON booleans and every
        # `dedup_columns` entry must pass the Identifier_Allowlist, because those
        # names reach a generated-code sink. `_validate_quality_config`'s docstring
        # carries the argument for each, including why fail-closed at
        # `code_assembly.resolve_data_quality_enabled` is NOT sufficient — on an S3
        # Tables pipeline a non-boolean falls through to the per-template default of
        # ON, so a string meaning "off" would turn DQ on with no error anywhere.
        try:
            _validate_quality_config(data)
        except ValueError as ve:
            logger.warning("quality_config validation rejected create: %s", ve)
            return create_response(400, {"error": str(ve)})

        # Standing invariant `LGR-819980f-1` — a JDBC credential travels only as a
        # Secrets Manager reference. A `target_config` carrying `jdbc_user` /
        # `jdbc_password` is refused here, before any Glue call or write, for the
        # reason `_reject_plaintext_target_credentials`' docstring gives: the Glue
        # job builder no longer reads either key, so accepting them would only store
        # a plaintext credential in the pipelines table. The same function guards
        # `edit_pipeline`. The 400 and the log line name the keys, never a value.
        try:
            _reject_plaintext_target_credentials(target_config)
        except ValueError as ve:
            logger.warning("plaintext target credentials rejected create: %s", ve)
            return create_response(400, {"error": str(ve)})

        # A `glue_connection_name` must name a connection the user owns, never one
        # this system created for another pipeline or for Detect Schema: those are
        # deleted with their owner, which would break this pipeline silently.
        # `_reject_managed_glue_connection` carries the reasoning. Both sides,
        # before any Glue call or write.
        try:
            _reject_managed_glue_connection(source_config, "source_config")
            _reject_managed_glue_connection(target_config, "target_config")
        except ValueError as ve:
            logger.warning("managed Glue connection rejected create: %s", ve)
            return create_response(400, {"error": str(ve)})

        # Validate source_type and source_config (P17 Requirement 6.1-6.3)
        if source_type not in _ALLOWED_SOURCE_TYPES:
            return create_response(
                400,
                {"error": (f"source_type must be one of {sorted(_ALLOWED_SOURCE_TYPES)}; got {source_type!r}")},
            )

        if source_type == "JDBC":
            # In `glue_connection` mode the wizard does not surface
            # `secret_arn` / `engine` to the user (the Glue Connection
            # already carries both). Resolve them server-side so the
            # canonical pipeline_record shape is complete and the
            # validator below can rely on them being present.
            #
            # Diagnostic logging: JDBC validation 400s previously returned
            # silently, making wizard-create failures impossible to triage
            # from CloudWatch. Log the request shape (keys + connection
            # modes only — never secret VALUES) once at entry, and the
            # specific rejection reason on each 400 path below.
            logger.info(
                "JDBC create_pipeline validation: source keys=%s "
                "(_connection_mode=%s), target keys=%s (type=%s, "
                "_connection_mode=%s)",
                sorted(source_config.keys()) if isinstance(source_config, dict) else type(source_config).__name__,
                source_config.get("_connection_mode") if isinstance(source_config, dict) else None,
                sorted(target_config.keys()) if isinstance(target_config, dict) else type(target_config).__name__,
                target_config.get("type") if isinstance(target_config, dict) else None,
                target_config.get("_connection_mode") if isinstance(target_config, dict) else None,
            )
            try:
                _augment_glue_connection_config(source_config, "source_config")
            except ValueError as ve:
                logger.warning("JDBC source augment rejected create: %s", ve)
                return create_response(400, {"error": str(ve)})

            try:
                _validate_jdbc_source_config(source_config)
            except ValueError as ve:
                logger.warning("JDBC source validation rejected create: %s", ve)
                return create_response(400, {"error": str(ve)})

        # Validate target_config if provided
        if target_config:
            target_type = target_config.get("type", "").upper()
            if target_type == "NEW_S3_TABLE_BUCKET":
                if not target_config.get("bucket_name"):
                    return create_response(
                        400, {"error": "bucket_name is required in target_config for NEW_S3_TABLE_BUCKET"}
                    )
                if not target_config.get("namespace_name") and not target_config.get("namespace"):
                    return create_response(
                        400, {"error": "namespace is required in target_config for NEW_S3_TABLE_BUCKET"}
                    )
                if not target_config.get("table_name"):
                    return create_response(
                        400, {"error": "table_name is required in target_config for NEW_S3_TABLE_BUCKET"}
                    )
                # Normalize: accept 'namespace' as alias for 'namespace_name'
                if not target_config.get("namespace_name") and target_config.get("namespace"):
                    target_config["namespace_name"] = target_config["namespace"]
            elif target_type == "EXISTING_S3_TABLE_BUCKET" and (
                not target_config.get("bucket_name") or not target_config.get("table_name")
            ):
                return create_response(
                    400,
                    {"error": "bucket_name and table_name are required in target_config for EXISTING_S3_TABLE_BUCKET"},
                )
            elif target_type == "GLUE_CATALOG":
                # GLUE_CATALOG is no longer offered for NEW pipelines. It writes
                # into a catalog table the user must already own, under the
                # `agentic_data_ingestion_*` database-name and writable-bucket
                # rules the `detect_schema` pre-flight enforces, while an S3
                # Tables or S3 Parquet target creates its own destination and
                # carries neither rule. The wizard stopped listing it in the
                # same change (`WizardStepTarget.tsx`, `TARGET_TYPE_OPTIONS`).
                #
                # Only creation is refused. Existing GLUE_CATALOG pipelines keep
                # running: the Glue job template, `job_builders.py` and the IAM
                # grants are untouched, and `edit_pipeline` does not consult
                # this branch. Refused before any write, so no row is created.
                logger.warning("GLUE_CATALOG target rejected create: no longer offered for new pipelines")
                return create_response(
                    400,
                    {
                        "error": (
                            "GLUE_CATALOG is no longer offered as a target for new pipelines. "
                            "Choose an S3 Table Bucket (NEW_S3_TABLE_BUCKET or "
                            "EXISTING_S3_TABLE_BUCKET), S3_PARQUET or JDBC target instead. "
                            "Existing GLUE_CATALOG pipelines are not affected."
                        )
                    },
                )
            elif target_type == "JDBC":
                # P31 R9.2 — `JDBC` is the canonical target type; the
                # `RDS_DATABASE` branch below is the legacy alias the wizard
                # no longer emits. These are the same three fields that
                # branch has always required, in every connection mode; the
                # P17 type consolidation left `JDBC` with no branch of its
                # own, so a malformed JDBC target was accepted with HTTP 200
                # and only failed later inside the Glue job (that failure
                # class is recorded in LGR-2474614-1). Restoring the checks
                # here means all three connection modes are covered at the
                # API boundary, exactly as `RDS_DATABASE` already is.
                if not target_config.get("jdbc_url"):
                    return create_response(400, {"error": "jdbc_url is required in target_config for JDBC"})
                if not target_config.get("jdbc_driver"):
                    return create_response(400, {"error": "jdbc_driver is required in target_config for JDBC"})
                if not target_config.get("jdbc_table"):
                    return create_response(400, {"error": "jdbc_table is required in target_config for JDBC"})
            elif target_type == "RDS_DATABASE":
                if not target_config.get("jdbc_url"):
                    return create_response(400, {"error": "jdbc_url is required in target_config for RDS_DATABASE"})
                if not target_config.get("jdbc_driver"):
                    return create_response(400, {"error": "jdbc_driver is required in target_config for RDS_DATABASE"})
                if not target_config.get("jdbc_table"):
                    return create_response(400, {"error": "jdbc_table is required in target_config for RDS_DATABASE"})

            # Security finding S1 — the S3_PARQUET branch (and its `S3` /
            # `S3_BUCKET` aliases) that the chain above never had, plus every
            # target S3 location the Glue role is granted on, including a JDBC /
            # RDS_DATABASE target's `temp_dir`. An S3 output target must carry
            # `s3_path`, and every location present must name one valid bucket,
            # because its bucket becomes an IAM Resource and `s3://*/` granted
            # every object in the account. Before any Glue call or write.
            # `_validate_s3_target_locations` carries the argument; `edit_pipeline`
            # applies it to the merged config.
            try:
                _validate_s3_target_locations(target_config, require_s3_path=True)
            except ValueError as ve:
                logger.warning("S3 target location rejected create: %s", ve)
                return create_response(400, {"error": str(ve)})

            # P17/P20 — for `glue_connection` mode JDBC / RDS_DATABASE
            # targets the wizard does not surface `secret_arn` / `engine`
            # (the user's Glue Connection already carries both). Resolve
            # them server-side so the canonical target_config shape is
            # complete and downstream codegen has concrete values.
            if target_type in ("JDBC", "RDS_DATABASE"):
                try:
                    _augment_glue_connection_config(target_config, "target_config")
                except ValueError as ve:
                    logger.warning("JDBC target augment rejected create: %s", ve)
                    return create_response(400, {"error": str(ve)})

            # Per-mode target validation, for both `JDBC` and the legacy
            # `RDS_DATABASE` alias. The dispatcher inside
            # `_validate_jdbc_target_config` decides what each mode owes:
            # `manual` the networking fields it has the user type,
            # `select_rds` the credential reference it cannot derive, and
            # `glue_connection` nothing at all (P20 Req 6.5 / 7.5 / 7.6).
            #
            # This call used to be gated on `_connection_mode == "manual"`,
            # which made the dispatcher's own mode branching unreachable for
            # every other mode — so `select_rds`, the DEFAULT for any payload
            # with no `_connection_mode`, was never validated here at all.
            # That is how a JDBC target with no `secret_arn` reached HTTP 201:
            # `CreateConnection` then failed with Glue's generic "Validation
            # for connection properties failed", the failure was swallowed
            # below, and the run died three steps later claiming a Glue
            # connection was required. Gating the call defeats the dispatch;
            # let the dispatcher decide per mode.
            #
            # This is NOT the whole of the target-side validation: the
            # `jdbc_url` / `jdbc_driver` / `jdbc_table` presence checks in the
            # `JDBC` / `RDS_DATABASE` branches above run for all three modes
            # (P31 R9.2).
            if target_type in ("JDBC", "RDS_DATABASE"):
                try:
                    _validate_jdbc_target_config(target_config)
                except ValueError as ve:
                    logger.warning("JDBC target validation rejected create: %s", ve)
                    return create_response(400, {"error": str(ve)})

            # Validate write_mode if provided.
            # P19 Req 7.2: JDBC targets default to "overwrite" when the wizard
            # request body omits write_mode. Other target types preserve the
            # pre-P19 "append" default. The resolved value is written back to
            # target_config so downstream readers (DDB persistence and the
            # codegen Lambda) see the same value verbatim.
            target_type_upper = (target_config.get("type") or "").upper()
            default_write_mode = "overwrite" if target_type_upper == "JDBC" else "append"
            write_mode = target_config.get("write_mode", default_write_mode)
            target_config["write_mode"] = write_mode  # persist resolved value verbatim for downstream readers
            # Per target type, from the one policy edit and code generation also
            # use: `merge` only for S3 Tables. This used to accept `merge` for
            # every target, and the JDBC, S3 Parquet and Glue Catalog writers ran
            # it as append, overwrite and a plain write respectively, each
            # reporting success. See `shared/write_modes.py`.
            write_mode_error = write_mode_refusal(target_config.get("type"), write_mode)
            if write_mode_error:
                return create_response(400, {"error": write_mode_error})
            if write_mode == "merge" and not target_config.get("merge_key"):
                return create_response(400, {"error": "merge_key is required when write_mode is 'merge'"})
            if "partition_columns" in target_config:
                try:
                    target_config["partition_columns"] = _normalised_partition_columns(
                        target_config["partition_columns"]
                    )
                except ValueError as ve:
                    return create_response(400, {"error": str(ve)})

        # Generate pipeline ID from name with UUID suffix (matching provision_pipeline behavior)
        base_pipeline_id = pipeline_name.lower().replace(" ", "-").replace("_", "-")
        base_pipeline_id = "".join(c for c in base_pipeline_id if c.isalnum() or c == "-")
        pipeline_id = f"{base_pipeline_id}-{str(uuid.uuid4())[:8]}"

        # Detect target type and load schema details
        target_schema_details = None
        target_schema_name = None
        target_type = "New S3 Bucket"  # Default for uploaded JSON schemas

        # The (bucket, key) pair that gets written onto the record, resolved
        # exactly once. `schema_location` — the S3 URI the wizard echoes back
        # from an inline `/schemas/upload` — is authoritative when present
        # (`LGR-724f88c-1`, backfilled). It is absent whenever an EXISTING schema
        # was selected, because the wizard only uploads when none was
        # (`CreatePipelinePage.tsx:309-313`), which is why the uploaded-schema
        # branch below probes for the address instead of leaving the record
        # without one.
        target_schema_location: tuple[str, str] | None = _parse_schema_location(schema_location)

        # Detect target type based on schema ID format
        if target_schema_id.startswith("glue_"):
            # Glue Data Catalog. The id is `glue_{database}_{table}` — see
            # `handlers/schemas.py`, which mints the id and its display label
            # side by side from the same two values.
            target_type = "Glue Data Catalog"
            target_schema_name = _glue_schema_name_from_id(target_schema_id, target_config)
            logger.info(f"Detected Glue Data Catalog target: {target_schema_name}")

        elif target_schema_id.startswith("s3_table_"):
            # S3 Table Bucket: format is "s3_table_namespace_table".
            #
            # `removeprefix`, not `replace`: `replace` is unanchored and strips
            # EVERY occurrence, so a namespace or table containing the prefix
            # lost it from the middle of the name too.
            #
            # The `_`→`.` conversion below carries the same ambiguity this
            # function's Glue branch exists to avoid — it replaces every
            # underscore, so only a single-token namespace AND table survive it.
            # Left as-is deliberately: unlike the Glue branch there is no
            # authoritative source to disambiguate against here, and no observed
            # failure. It is the next thing to fix if a multi-token S3 Tables
            # namespace ever reaches this path.
            target_type = "S3 Table Bucket"
            target_schema_name = target_schema_id.removeprefix("s3_table_").replace("_", ".")
            logger.info(f"Detected S3 Table Bucket target: {target_schema_name}")

        else:
            # Uploaded JSON Schema (default)
            target_type = "New S3 Bucket"

            # Try to load schema details from S3
            try:
                s3_client = boto3.client("s3", region_name=AWS_REGION)
                account_id = get_aws_account_id()
                bucket_name = f"adi-target-schemas-{account_id}-{AWS_REGION}"

                # The schema id IS the object basename for both shapes that can
                # get here — ``upload_schema`` returns ``{name}_{timestamp}`` for
                # a key of ``shared/{name}_{timestamp}.json``
                # (``handlers/schemas.py:322-334``) and ``get_schemas`` derives an
                # existing schema's id by stripping exactly ``shared/`` and
                # ``.json`` (``:81-92``). The three prefix-guessed keys that used
                # to be derived here (``schemas/uploaded/{stripped}.json``,
                # ``uploaded_schemas/target_{id}.json``, ``schemas/{id}.json``)
                # matched neither, so this read always raised and was swallowed
                # below — leaving ``target_schema_details`` None on every request.
                #
                # ``locate_uploaded_schema`` probes the SAME six candidates the
                # mapping step reads back through, so the address written on the
                # record and the address the reader finds are one definition
                # (``LGR-eee8ac8-5``; see the module docstring in
                # ``shared/target_schema_resolution.py``).
                schema_name = target_schema_id
                if target_schema_location is None:
                    located = locate_uploaded_schema(
                        bucket_name,
                        target_schema_id,
                        s3_client=s3_client,
                        step_name="create_pipeline",
                    )
                    if located:
                        target_schema_location = (bucket_name, located)

                if target_schema_location is None:
                    # Nothing to read, and nothing to record. Logged at ERROR
                    # rather than 400-ing the request — see the comment on the
                    # `target_schema_location` write below for why.
                    raise FileNotFoundError(
                        f"no uploaded target schema object for id {target_schema_id!r} in {bucket_name}"
                    )

                schema_bucket, schema_key = target_schema_location

                # Load schema from S3
                schema_response = s3_client.get_object(Bucket=schema_bucket, Key=schema_key)
                schema_content = json.loads(schema_response["Body"].read())

                # Load metadata if available
                metadata = {}
                try:
                    metadata_key = f"schemas/uploaded/{schema_name}-metadata.json"
                    metadata_response = s3_client.get_object(Bucket=bucket_name, Key=metadata_key)
                    metadata = json.loads(metadata_response["Body"].read())
                except Exception as e:
                    logger.warning(f"Failed to load schema metadata from S3: {e}")

                target_schema_details = {"name": schema_name, "source": "uploaded_json", "fields": []}

                # Extract fields from JSON schema
                if "properties" in schema_content:
                    for field_name, field_def in schema_content["properties"].items():
                        field = {
                            "name": field_name,
                            "type": field_def.get("type", "string"),
                            "nullable": field_name not in schema_content.get("required", []),
                            "context": metadata.get("field_contexts", {}).get(field_name, ""),
                        }
                        target_schema_details["fields"].append(field)

                target_schema_name = schema_name

            except Exception as e:
                # ERROR, not warning: a pipeline that reaches this line has no
                # resolvable target schema, so p33's `schema_compliance` check
                # will report `verified: False` / `no_target_schema` on every one
                # of its runs. That was invisible for months at warning level.
                logger.error(
                    "Could not resolve target schema %r for pipeline %s: %s — the pipeline record will carry "
                    "no %s/%s, so schema compliance cannot be verified for its runs.",
                    target_schema_id,
                    pipeline_id,
                    e,
                    "target_schema_bucket",
                    "target_schema_s3_key",
                )
                # Continue with just the ID. The id IS the object basename (see
                # the resolution comment above), so it is also the right
                # `target_schema_name` for the mapping step's by-name fallback.
                # The old code stripped a leading `uploaded_` here, which put the
                # name and the key one prefix apart — live record
                # `testing-39eddb17` carries
                # `target_schema_name=schema_1785844048926_20260804_114729`
                # against
                # `target_schema_s3_key=shared/uploaded_schema_1785844048926_20260804_114729.json`,
                # so its by-name fallback could never find the object the same
                # record already addressed correctly.
                target_schema_name = target_schema_id

        # Create pipeline entry in agentic_data_ingestion_pipelines table
        pipeline_item = {
            "pipeline_id": pipeline_id,
            "pipeline_name": pipeline_name,
            "target_schema_name": target_schema_name or target_schema_id,
            "ai_model": ai_model,
            "pipeline_description": pipeline_description,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "created_by": created_by,
        }

        # Store the resolved S3 location for the target schema. Both attributes
        # or neither — `LGR-724f88c-1` established the pairing and p33 D14
        # (`LGR-78ce0b7-14`, template/ratified) turned it into a read contract:
        # a key the record NAMES is a claim that an object exists, so
        # `resolve_target_schema` RAISES on a key pointing at nothing rather than
        # reporting "no target schema".
        #
        # That is why an unresolvable id leaves BOTH attributes absent rather
        # than writing a guess, and also why it does NOT 400 the request: the
        # writer's obligation under D14 is to make no claim it has not verified,
        # and writing neither attribute discharges it. Requiring the referenced
        # object to exist at creation time is a stronger, separate obligation
        # that D14 did not establish — see the module note in the MR for the
        # rejected alternative and its blast radius.
        if target_schema_location:
            pipeline_item["target_schema_bucket"] = target_schema_location[0]
            pipeline_item["target_schema_s3_key"] = target_schema_location[1]

        # Where the target SCHEMA is read from. Derived from the schema id, which
        # is the only value that identifies the schema, and written unconditionally
        # so every new record carries it.
        #
        # This exists because `target_type` below answers a DIFFERENT question —
        # where data is WRITTEN — and the two are independent. A pipeline may write
        # to JDBC while taking its target schema from a Glue Catalog table; one
        # attribute cannot describe both, and when it described the write target,
        # `schema_mapping` routed a Glue table name into the uploaded-JSON branch.
        # See `_target_schema_source_for_id`.
        pipeline_item["target_schema_source"] = _target_schema_source_for_id(target_schema_id)

        # Store target_config if provided
        if target_config:
            pipeline_item["target_config"] = target_config
            # Deliberately the WRITE target, and deliberately `target_config`'s
            # value over the schema-derived `target_type` above: everything
            # downstream that provisions or writes reads this. Schema *reading*
            # now uses `target_schema_source` instead, so this no longer has to
            # serve both purposes.
            pipeline_item["target_type"] = target_config.get("type", target_type)

        # P19 Req 8.4: persist schema_source as a top-level field on the
        # pipeline record. Diagnostic only — downstream Lambdas continue
        # reading the resolved schema from `schema_fields` regardless of
        # which source mode produced it.
        pipeline_item["schema_source"] = schema_source

        # Store source_type and source_config as TOP-LEVEL fields (P17
        # Requirement 6.1, 6.2). source_type is always written so existing
        # query / list endpoints can rely on it being present; source_config
        # is only written when non-empty (matches target_config behaviour).
        pipeline_item["source_type"] = source_type
        if source_config:
            pipeline_item["source_config"] = source_config

        # p55 R8.4 / task 5.2 — the Selected_Worksheet, written TOP-LEVEL and only
        # when the wizard actually sent one. `_submitted_selected_worksheet`
        # carries the reasoning; both halves matter and are tested separately.
        selected_worksheet = _submitted_selected_worksheet(data)
        if selected_worksheet is not None:
            pipeline_item[SELECTED_WORKSHEET_ATTRIBUTE] = selected_worksheet

        # P17 Task 5.1 / Requirement 11.1, 11.2: pre-compute the source-side
        # Glue Connection name and persist it on the pipeline record so
        # downstream Tasks 5.2 / 5.4 can reference it without re-deriving
        # the connection mode. The name is deterministic in both modes:
        #   * select_rds  → agentic-data-ingestion-{pipeline_id}-source (will be created
        #     below, after the DDB write, by _provision_jdbc_source_connection)
        #   * glue_connection → source_config["glue_connection_name"] (the
        #     user's existing connection — never created or modified by
        #     AgenticDataIngestion)
        if source_type == "JDBC":
            existing_connection = source_config.get("glue_connection_name")
            if existing_connection:
                pipeline_item["source_connection_name"] = existing_connection
            else:
                pipeline_item["source_connection_name"] = f"agentic-data-ingestion-{pipeline_id}-source"

        # p64 R10.1 / R10.2 — the Approval_Config is the REQUEST's when it sent a
        # dict, and the seeded default only when the key is ABSENT. This used to
        # be an unconditional assignment, which discarded the four settings the
        # wizard's step 5 exists to collect. Three arms, and the omitted-versus-
        # explicitly-empty distinction between them, are in
        # `_submitted_approval_config`'s docstring — read it before changing this,
        # and note that `None` here means "write no attribute", which is
        # deliberate and fail-closed rather than an oversight.
        #
        # p64 R10.4 — by this line the submission has already passed
        # `_validate_approval_config` above, so the `None` arm is a backstop that
        # this path does not reach: a non-dict was refused with a 400 several
        # hundred lines ago. The check stays because the function's contract is
        # its own, not this call site's.
        approval_config = _submitted_approval_config(data)
        if approval_config is not None:
            pipeline_item[APPROVAL_CONFIG_KEY] = approval_config

        # p64 R7.1 / R7.3 (design C7) — the Quality_Config, written as a TOP-LEVEL
        # PEER of the Approval_Config above. Deliberately NOT folded into
        # `target_config`: that would make it a template variable for free (
        # `code_assembly` splats source_config and target_config wholesale into the
        # render context) at the cost of hiding an unvalidated boolean in a free-form
        # `Record<string, string>`, and a data-quality setting is not target
        # configuration. C7 takes the more expensive route on purpose.
        #
        # Same three arms as the Approval_Config, same omitted-versus-explicitly-
        # empty distinction, and `None` means "write no attribute" — see
        # `_submitted_quality_config`. Note one thing that is NOT symmetric: the
        # seeded map deliberately carries no `data_quality_enabled`, because that
        # key's default is per-template and seeding `False` would opt every new S3
        # Tables pipeline OUT of a DQ block it has had since P28 (R6.6, both
        # directions). `_default_quality_config` carries that argument.
        quality_config = _submitted_quality_config(data)
        if quality_config is not None:
            pipeline_item[QUALITY_CONFIG_KEY] = quality_config

        # Store schedule if provided
        schedule_expression = data.get("schedule")
        if schedule_expression:
            pipeline_item["schedule"] = schedule_expression

        # Store in DynamoDB
        pipelines_table.put_item(Item=pipeline_item)

        # Inline provisioning: create S3 prefix and enable EventBridge.
        # Best-effort, deliberately, unlike the target connection and schedule
        # below: uploads are matched by one static rule created at deploy time on
        # a bucket CDK already EventBridge-enables, and S3 prefixes need no
        # marker, so a failure here does not stop the pipeline from working.
        folder_marker_written = False
        try:
            _provision_pipeline_resources(pipeline_id, pipeline_name)
            folder_marker_written = True
            logger.info(f"Pipeline provisioning completed for {pipeline_id}")
        except Exception as e:
            logger.error(f"Error provisioning pipeline resources: {e}")
            # Continue anyway - pipeline is created in table

        # P17 Task 5.1 / Requirement 11.1, 11.2: provision the source-side
        # Glue JDBC Connection for JDBC pipelines. In select_rds mode this
        # creates ``agentic-data-ingestion-{pipeline_id}-source`` with
        # PhysicalConnectionRequirements derived from the wizard's RDS
        # selection so Glue routes the runtime crawler and job through the
        # customer's VPC. In glue_connection mode the user's existing
        # connection is referenced as-is — no Glue API calls are made.
        #
        # P17 Task 5.3 / Requirement 11.5: roll-back semantics. The DDB
        # pipeline record and the Glue resources must stay in lock-step —
        # a half-provisioned pipeline cannot be usefully resumed and
        # would confuse ``delete_pipeline``'s cleanup. On any failure we
        # delete the just-written DDB row and return HTTP 400, mirroring
        # the rollback-on-invalid-schedule pattern below. When the
        # crawler step fails after the connection step succeeded, we
        # also delete the just-created Glue Connection — but ONLY in
        # ``select_rds`` mode (where AgenticDataIngestion owns the connection
        # lifecycle). In ``glue_connection`` mode the connection is
        # user-supplied and must never be deleted.
        if source_type == "JDBC":
            # Ownership signal: ``glue_connection_name`` set ⇒ user
            # supplied an existing connection (glue_connection mode);
            # absent ⇒ we created ``agentic-data-ingestion-{pipeline_id}-source``
            # in select_rds mode and own its lifecycle.
            source_owned_by_adi = not source_config.get("glue_connection_name")

            try:
                _provision_jdbc_source_connection(pipeline_id, source_config)
            except Exception as e:
                logger.error(f"Error provisioning source Glue JDBC connection for {pipeline_id}: {e}")
                # No partial connection to clean up — the create call
                # failed. Roll back the DDB record and surface HTTP 400.
                pipelines_table.delete_item(Key={"pipeline_id": pipeline_id})
                return create_response(
                    400,
                    {"error": f"Failed to provision JDBC source connection: {e}"},
                )

            # P17 Task 5.2 / Requirement 11.8: provision the per-pipeline
            # runtime Glue Crawler. The schema_extract Lambda triggers
            # this crawler on every run to refresh the source schema in
            # the shared discovery database. Created here at pipeline-
            # creation time so connection / network errors surface to
            # the wizard rather than to the first execution. Re-use the
            # connection name pre-computed onto pipeline_item by Task
            # 5.1; defensive fallback to deriving from mode keeps this
            # robust against a malformed pipeline_item shape.
            source_connection_name = pipeline_item.get("source_connection_name") or (
                source_config.get("glue_connection_name") or f"agentic-data-ingestion-{pipeline_id}-source"
            )
            try:
                _provision_jdbc_runtime_crawler(pipeline_id, source_config, source_connection_name)
            except Exception as e:
                logger.error(f"Error provisioning runtime Glue crawler for {pipeline_id}: {e}")
                # Connection (5.1) succeeded but crawler (5.2) failed.
                # Clean up the partially-created connection in select_rds
                # mode only. NEVER delete a user-supplied connection.
                if source_owned_by_adi:
                    glue_client = boto3.client("glue")
                    try:
                        glue_client.delete_connection(ConnectionName=source_connection_name)
                        logger.info(
                            "Rolled back partially-created source connection %s",
                            source_connection_name,
                        )
                    except glue_client.exceptions.EntityNotFoundException:
                        # Already absent — rollback is idempotent.
                        pass
                    except Exception as cleanup_err:
                        # Log and continue so the original crawler error
                        # is what the user sees in the HTTP 400.
                        logger.warning(
                            "Failed to clean up partially-created connection %s during rollback: %s",
                            source_connection_name,
                            cleanup_err,
                        )
                pipelines_table.delete_item(Key={"pipeline_id": pipeline_id})
                return create_response(
                    400,
                    {"error": f"Failed to provision JDBC runtime crawler: {e}"},
                )

        # Auto-create Glue JDBC connection for RDS_DATABASE / JDBC targets.
        # P14 covered Select-RDS-Instance mode for `RDS_DATABASE`; P20
        # extends this to manual-mode JDBC targets so the manual flow
        # produces the same `agentic-data-ingestion-{pipeline_id}-rds` connection
        # shape (Req 8.2, 8.6). `glue_connection` mode short-circuits
        # inside `_provision_jdbc_target_connection` (early-return on
        # `target_config.glue_connection_name`), so it is safe to call
        # for any JDBC-flavoured target.
        target_type_upper = (target_config.get("type") or "").upper() if target_config else ""
        if target_config and target_type_upper in ("RDS_DATABASE", "JDBC"):
            try:
                conn_name = _provision_jdbc_target_connection(pipeline_id, target_config)
                if conn_name:
                    logger.info(f"JDBC target Glue connection ready: {conn_name}")
                    # Persist the resolved connection name onto the stored
                    # target_config so the run-time CreateGluePipeline step
                    # (job_arguments.build_jdbc_arguments / job_builders /
                    # iam_roles) can attach it to the Glue job. glue_connection
                    # mode already carries glue_connection_name (set by the
                    # wizard), but the select_rds / manual auto-create path
                    # produces agentic-data-ingestion-{pipeline_id}-rds and MUST write it
                    # back here — otherwise the run fails with "A Glue
                    # connection is required for a JDBC target". Written only
                    # after a successful create so the name never points at a
                    # connection that does not exist. The pipeline record was
                    # already persisted above, so update target_config in place.
                    if not target_config.get("glue_connection_name") and not target_config.get("jdbc_connection_name"):
                        target_config["jdbc_connection_name"] = conn_name
                        pipelines_table.update_item(
                            Key={"pipeline_id": pipeline_id},
                            UpdateExpression="SET target_config = :tc",
                            ExpressionAttributeValues={":tc": target_config},
                        )
                        logger.info(
                            "Persisted jdbc_connection_name=%s onto pipeline %s target_config",
                            conn_name,
                            pipeline_id,
                        )
            except Exception as e:
                # A required stage, not a best-effort one. This used to log and
                # continue ("connection can be created manually later"), on the
                # premise that the Glue job would "surface its own clear
                # connection error". It does not: the first run fails in
                # CreateGluePipeline with "A Glue connection is required for a
                # JDBC target", which reads as a configuration mistake, after a
                # 201 that said the pipeline was created. The update_item above
                # is inside this try too, so a connection created but not
                # recorded is rolled back rather than orphaned.
                logger.error(f"Error auto-creating Glue connection for JDBC target: {e}")
                undo = _undo_pipeline_create(
                    pipeline_id,
                    source_type,
                    source_config,
                    target_config,
                    folder_marker_written=folder_marker_written,
                    schedule_rule_may_exist=False,
                )
                return _provisioning_failure_response(
                    pipeline_id,
                    "target_connection",
                    f"could not create the Glue connection agentic-data-ingestion-{pipeline_id}-rds for the JDBC target",
                    e,
                    undo,
                )

        # Create schedule rule if schedule was provided. Required when asked for:
        # a schedule the record shows and EventBridge does not have is a pipeline
        # that silently never runs, and the dashboard renders a next run time for
        # it from the record.
        if schedule_expression:
            trigger_arn = require_env("PIPELINE_TRIGGER_ARN", "")
            try:
                if not trigger_arn:
                    # A deployment fault, not the caller's: the Compute stack sets
                    # this on the dashboard API. It used to skip silently.
                    raise RuntimeError("PIPELINE_TRIGGER_ARN is not set on the dashboard API")
                _manage_schedule_rule(
                    pipeline_id, schedule_expression, trigger_arn, pipeline_id, source_type=source_type
                )
                logger.info(f"Schedule rule created for pipeline {pipeline_id}")
            except Exception as e:
                # A ValueError is `put_rule` refusing the expression, so no rule
                # exists. Anything later (`put_targets`) may have left an ENABLED
                # rule with no target, so that case removes the rule too. Either
                # way every resource this request made goes with it: the
                # invalid-schedule path used to delete only the row, leaking the
                # JDBC connections and crawler created above.
                logger.warning(f"Failed to create schedule rule: {e}")
                undo = _undo_pipeline_create(
                    pipeline_id,
                    source_type,
                    source_config,
                    target_config,
                    folder_marker_written=folder_marker_written,
                    schedule_rule_may_exist=bool(trigger_arn) and not isinstance(e, ValueError),
                )
                return _provisioning_failure_response(pipeline_id, "schedule", "could not create the schedule", e, undo)

        # Audit: record pipeline creation
        ctx = extract_request_context(event or {})
        write_audit_entry(
            action="pipeline_created",
            resource_type="pipeline",
            resource_id=pipeline_id,
            details={"name": pipeline_name},
            **ctx,
        )

        return create_response(
            201, {"pipeline_id": pipeline_id, "status": "created", "message": "Pipeline created successfully"}
        )

    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        return handle_error(e, "Error creating pipeline")


def _normalised_partition_columns(value: object) -> str:
    """``target_config.partition_columns`` as stored: validated names joined by commas.

    The generated Glue job emits these names into ``partitionBy(...)``. Until
    2026-10-10 neither create nor edit read the value, so the edit endpoint's
    per-key merge could store any text there, and code generation then emitted
    it as Python. Accepts a list or a comma-separated string and stores the
    string, because ``job_builders`` passes the stored value as the
    ``--partition_columns`` Glue job argument, which must be a string. Code
    generation validates it again (``code_assembly._partition_columns_argument``)
    for records written before this check.

    Raises:
        ValueError: naming the refused entry.
    """
    try:
        return ",".join(parse_identifier_list(value, "target_config.partition_columns"))
    except IdentifierValidationError as exc:
        raise ValueError(
            f"{exc}. Partition column names are written into the generated Glue job, so each must be a "
            "plain column name: letters, digits and underscores, not starting with a digit."
        ) from exc


#: Target types whose Glue job role is granted read/write on the bucket named by
#: the target's S3 output path. A copy of `iam_roles.S3_OUTPUT_TARGET_TYPES` in
#: the create_glue_pipeline Lambda, which this Lambda cannot import. `S3_PARQUET`
#: is the spelling the wizard sends (`CreatePipelinePage.tsx`); `S3` and
#: `S3_BUCKET` are older spellings the role builder still honours, so a create
#: or edit under either gets the same check.
_S3_OUTPUT_TARGET_TYPES: Final[tuple[str, ...]] = ("S3_PARQUET", "S3", "S3_BUCKET")

#: Where an S3 output target's location is read, first non-empty wins
#: (`iam_roles.S3_OUTPUT_PATH_KEYS`, `job_arguments.build_s3_parquet_arguments`).
_S3_OUTPUT_PATH_KEYS: Final[tuple[str, ...]] = ("s3_path", "output_path", "target_path")

#: Every `target_config` key whose bucket the role builder can grant on: the
#: output path keys, and a Redshift JDBC target's `temp_dir`.
_S3_LOCATION_KEYS: Final[tuple[str, ...]] = (*_S3_OUTPUT_PATH_KEYS, "temp_dir")


def _validate_s3_target_locations(target_config: dict[str, Any], *, require_s3_path: bool) -> None:
    """Refuse a target S3 location that does not name exactly one valid bucket (security finding S1).

    The create_glue_pipeline role builder interpolates the bucket of these
    locations into the pipeline's Glue role as ``arn:aws:s3:::{bucket}/*`` with
    Get/Put/Delete, and ``arn:aws:s3:::{bucket}`` with ListBucket. IAM reads
    ``*`` and ``?`` there as wildcards, so ``s3_path: "s3://*/"`` granted every
    object in the account, and the permissions boundary cannot catch it because
    its own S3 statement is already ``*``. Neither endpoint looked at these keys
    before, and the edit endpoint stores a submitted key verbatim.

    Two rules, applied to the config as it will be stored:

    * every key in ``_S3_LOCATION_KEYS`` that is present and non-empty must be an
      ``s3://bucket/prefix`` URI whose bucket passes
      ``identifier_utils.bucket_from_s3_uri``, whatever the target type. The role
      builder reads ``temp_dir`` only for a Redshift JDBC target and the output
      keys only for an S3 output target, but an edit can change ``type`` later,
      so a value is checked when it is stored rather than when it starts to
      matter;
    * an S3 output target (``_S3_OUTPUT_TARGET_TYPES``) must have a location.
      On create (``require_s3_path``) that is ``s3_path``, the key the wizard
      sends. On edit it is any of ``_S3_OUTPUT_PATH_KEYS``, the order the role
      builder and the job arguments read, so a pipeline stored under a legacy
      ``output_path`` can still be saved.

    Refused, never repaired: a value is not trimmed, lowered or narrowed
    (``LGR-b993b94-2``, ``LGR-4ea43f3-4``). The role builder refuses the same
    values again for records written before this check.

    Raises:
        ValueError: naming the key and the value, for a 400.
    """
    for key in _S3_LOCATION_KEYS:
        value = target_config.get(key)
        # Truthiness, not `is None`: the role builder skips a falsy value the
        # same way, so the two agree on what "absent" means.
        if not value:
            continue
        try:
            bucket_from_s3_uri(value, f"target_config.{key}")
        except S3BucketNameError as exc:
            raise ValueError(
                f"{exc}. The bucket of a target S3 location is granted to the pipeline's Glue job role, so it "
                "must be an s3://bucket/prefix URI naming one bucket: 3-63 lowercase letters, digits, dots and "
                "hyphens, beginning and ending with a letter or digit."
            ) from exc

    target_type = str(target_config.get("type") or "").upper()
    if target_type not in _S3_OUTPUT_TARGET_TYPES:
        return
    if require_s3_path:
        if not target_config.get("s3_path"):
            raise ValueError(f"s3_path is required in target_config for {target_type}: an s3://bucket/prefix URI")
    elif not any(target_config.get(key) for key in _S3_OUTPUT_PATH_KEYS):
        raise ValueError(
            f"target_config for {target_type} needs an output location: s3_path, an s3://bucket/prefix URI"
        )


#: The condition on every write `edit_pipeline` makes. UpdateItem creates the
#: item when the key is absent, so without it an edit to a stale or deleted id
#: wrote a partial record (a name and `updated_at`, with no source, target or
#: status) and returned 200. Checked on the write itself, not by a separate
#: read, so an edit racing a delete cannot recreate the row.
_PIPELINE_EXISTS = "attribute_exists(pipeline_id)"


def _pipeline_not_found(pipeline_id: str) -> dict[str, Any]:
    return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})


def edit_pipeline(pipeline_id: str, body: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update pipeline metadata in agentic_data_ingestion_pipelines table"""
    try:
        data = json.loads(body) if body else {}

        # p64 R7.4 — the SAME boundary refusal `create_pipeline` applies, from the
        # SAME function. Not a second validator: `_validate_quality_config` is
        # reused verbatim so the two endpoints cannot drift into two policies for
        # one wire shape, which is the "second spelling with nothing to arbitrate
        # between them" class `LGR-b993b94-1` rejected for the casing gap.
        #
        # 400 at the boundary rather than fail-closed at the reader, and NOT
        # clamping — task 2a.2's ratified position (`LGR-b993b94-2`, and
        # `LGR-4ea43f3-4` for why a silently-altered value is worse than a refused
        # one). It matters more here than on create: an edit is applied to a
        # pipeline that already works, so a value that cannot mean what it says
        # must not be allowed to half-replace one that could.
        #
        # This runs BEFORE any write, including the `if_not_exists` map-seeding
        # call below, so a refused body leaves the record untouched rather than
        # creating an empty `quality_config` map on its way to a 400.
        try:
            _validate_quality_config(data)
        except ValueError as ve:
            logger.warning("quality_config validation rejected edit: %s", ve)
            return create_response(400, {"error": str(ve)})

        # The same treatment for `approval_config`, and for a stronger reason than
        # symmetry. p64 shipped `_validate_quality_config` on both endpoints but
        # `_validate_approval_config` on create only, so this one endpoint refused a
        # malformed `quality_config` and accepted a malformed `approval_config` —
        # two policies for two wire shapes at one boundary, which is the asymmetry
        # the sibling comment above exists to prevent.
        #
        # **What the asymmetry actually let through.** Only ONE of the three gates
        # is fail-closed at its reader: `test_code.resolve_code_approval_setting`
        # checks `raw is True or raw is False`. The other two are read by
        # truthiness — `schema_mapping/lambda_function.py:1669` does
        # `if not approval_config.get("schema_mapping_enabled", True):` and `:1657`
        # does `if approval_config.get("always_approve_schema_changes", True):` — so
        # an edit submitting `0`, `""`, `[]` or `None` under either key **disabled a
        # human review gate** and returned 200. `_validate_approval_config`'s
        # docstring argues this at length; read it before relaxing anything here.
        #
        # Reused verbatim, not reimplemented, for the reason the sibling gives: two
        # validators for one wire shape is the "second spelling with nothing to
        # arbitrate between them" class `LGR-b993b94-1` rejected.
        #
        # 400 at the boundary, never clamped — task 2a.2's ratified position
        # (`LGR-4ea43f3-4`: a silently-altered value is well-formed, plausible and
        # wrong). Placed beside its sibling so BOTH refusals precede every write,
        # including the `if_not_exists` seeding calls below.
        #
        # **This supersedes an explicit do-NOT, deliberately.**
        # `tests/unit/lambdas/dashboard_api/test_edit_pipeline_approval_config_boolean.py`
        # (p34 R7.3) pinned the unvalidated pass-through and instructed that the
        # module "must not grow an assertion that `pipelines_crud` rejects a
        # non-boolean", on the premise that the malformed case is resolved on the
        # read side. That premise holds for `code_generation_enabled`, the key p34
        # tested, and fails for the other two gates — which is why the position
        # moved. That module's guard was retargeted rather than deleted; see the
        # Decision block in the commit body.
        try:
            _validate_approval_config(data)
        except ValueError as ve:
            logger.warning("approval_config validation rejected edit: %s", ve)
            return create_response(400, {"error": str(ve)})

        # The create path's plaintext-credential refusal, from the SAME function
        # (`LGR-819980f-1`). It matters at least as much here: the `target_config`
        # branch below merges per key, so without it an edit could add
        # `jdbc_password` to an existing JDBC pipeline's stored config. Placed with
        # the two refusals above so it, too, precedes every write, including the
        # `if_not_exists` seeding call. Keyed on presence, not on `type`, because a
        # per-key edit need not carry `type` at all.
        try:
            _reject_plaintext_target_credentials(data.get("target_config"))
        except ValueError as ve:
            logger.warning("plaintext target credentials rejected edit: %s", ve)
            return create_response(400, {"error": str(ve)})

        # The create path's managed-connection refusal, for the one side an edit
        # can change: `target_config` merges per key below, so an edit could point
        # an existing pipeline at another pipeline's connection. (`source_config`
        # is not editable here.) Refused only when the submitted name DIFFERS from
        # the stored one, so re-saving a pipeline created before this guard keeps
        # working; the stored value is read only on that rare path.
        try:
            _reject_managed_glue_connection(data.get("target_config"), "target_config")
        except ValueError as ve:
            submitted = data["target_config"].get("glue_connection_name")
            stored = item_dict(
                pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item") or {},
                "target_config",
            ).get("glue_connection_name")
            if submitted != stored:
                logger.warning("managed Glue connection rejected edit: %s", ve)
                return create_response(400, {"error": str(ve)})

        # The create path's write-mode policy, from the same function
        # (`shared/write_modes.py`). Until 2026-10-10 this endpoint checked no
        # write mode at all, and the Edit Pipeline dialog offered "Merge" for
        # every target, so a JDBC pipeline could be switched to a merge that
        # ran as append, and an S3 Parquet one to a merge that ran as overwrite.
        #
        # Checked against the EFFECTIVE pair: `target_config` merges per key
        # below, so an edit can carry `write_mode` without `type`, or the
        # reverse, and what gets stored is the submitted keys over the stored
        # ones. The record is read only when the edit touches one of the three
        # keys, and the check precedes every write, so a refused edit leaves the
        # record untouched. A stored legacy `merge` on a JDBC pipeline therefore
        # refuses any edit that re-submits it, which is the correction path: the
        # dialog no longer offers it, and the user picks append or overwrite.
        submitted_target = data.get("target_config")
        if isinstance(submitted_target, dict) and any(
            key in submitted_target for key in ("type", "write_mode", "merge_key")
        ):
            stored_target = item_dict(
                pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item") or {},
                "target_config",
            )
            effective_target = {**stored_target, **submitted_target}
            if "write_mode" in effective_target:
                effective_mode = effective_target["write_mode"]
                write_mode_error = write_mode_refusal(effective_target.get("type"), effective_mode)
                if write_mode_error is None and effective_mode == "merge" and not effective_target.get("merge_key"):
                    write_mode_error = "merge_key is required when write_mode is 'merge'"
                if write_mode_error:
                    logger.warning("write_mode rejected edit: %s", write_mode_error)
                    return create_response(400, {"error": write_mode_error})

        # The create path's partition-column check. The per-key merge below
        # stores a submitted key verbatim, which is how an edit could put any
        # text into a value the generated job emits as code. Normalised in
        # place, so the stored value is the validated string.
        if isinstance(submitted_target, dict) and "partition_columns" in submitted_target:
            try:
                submitted_target["partition_columns"] = _normalised_partition_columns(
                    submitted_target["partition_columns"]
                )
            except ValueError as ve:
                logger.warning("partition_columns rejected edit: %s", ve)
                return create_response(400, {"error": str(ve)})

        # Security finding S1 — the create path's S3-location check, against the
        # EFFECTIVE target config, as the write-mode check above does: the per-key
        # merge below can set `s3_path` or `temp_dir` without `type`, or change
        # `type` under a stored location, and what is stored is the submitted keys
        # over the stored ones. Each of those keys' buckets becomes an IAM Resource
        # on the pipeline's Glue role. The record is read only when the edit
        # touches `type` or a location key; the Edit Pipeline dialog re-submits the
        # whole stored target_config, so a stored hostile location refuses every
        # save until the user corrects it. Before every write.
        if isinstance(submitted_target, dict) and any(key in submitted_target for key in ("type", *_S3_LOCATION_KEYS)):
            stored_target = item_dict(
                pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item") or {},
                "target_config",
            )
            try:
                _validate_s3_target_locations({**stored_target, **submitted_target}, require_s3_path=False)
            except ValueError as ve:
                logger.warning("S3 target location rejected edit: %s", ve)
                return create_response(400, {"error": str(ve)})

        # Build update expression
        update_expr = "SET "
        expr_values = {}
        expr_names = {}

        if "pipeline_description" in data or "description" in data:
            description = data.get("pipeline_description") or data.get("description")
            update_expr += "#desc = :desc, "
            expr_values[":desc"] = description
            expr_names["#desc"] = "pipeline_description"

        if "status" in data:
            update_expr += "#status = :status, "
            expr_values[":status"] = data["status"]
            expr_names["#status"] = "status"

        if "target_schema_name" in data or "target_schema" in data:
            schema = data.get("target_schema_name") or data.get("target_schema")
            update_expr += "target_schema_name = :schema, "
            expr_values[":schema"] = schema

        if "ai_model" in data:
            update_expr += "ai_model = :model, "
            expr_values[":model"] = data["ai_model"]

        if "pipeline_name" in data:
            update_expr += "pipeline_name = :name, "
            expr_values[":name"] = data["pipeline_name"]

        if "target_config" in data:
            tc = data["target_config"]
            if isinstance(tc, dict):
                # Ensure the top-level map exists first (separate call to avoid overlapping paths)
                pipelines_table.update_item(
                    Key={"pipeline_id": pipeline_id},
                    UpdateExpression="SET target_config = if_not_exists(target_config, :empty_map)",
                    ExpressionAttributeValues={":empty_map": {}},
                    ConditionExpression=_PIPELINE_EXISTS,
                )
                for key, value in tc.items():
                    safe_key = key.replace("-", "_")
                    placeholder = f":tc_{safe_key}"
                    name_placeholder = f"#tc_{safe_key}"
                    # Alias the nested key via ExpressionAttributeNames so DynamoDB
                    # reserved words (e.g. "type") are legal as document-path segments.
                    update_expr += f"target_config.{name_placeholder} = {placeholder}, "
                    expr_names[name_placeholder] = key
                    expr_values[placeholder] = value
            else:
                update_expr += "target_config = :tc, "
                expr_values[":tc"] = tc

        if "approval_config" in data:
            ac = data["approval_config"]
            if isinstance(ac, dict):
                # Ensure the top-level map exists first (separate call to avoid overlapping paths)
                pipelines_table.update_item(
                    Key={"pipeline_id": pipeline_id},
                    UpdateExpression="SET approval_config = if_not_exists(approval_config, :empty_map)",
                    ExpressionAttributeValues={":empty_map": {}},
                    ConditionExpression=_PIPELINE_EXISTS,
                )
                for key, value in ac.items():
                    safe_key = key.replace("-", "_")
                    placeholder = f":ac_{safe_key}"
                    name_placeholder = f"#ac_{safe_key}"
                    # Alias the nested key via ExpressionAttributeNames so DynamoDB
                    # reserved words are legal as document-path segments.
                    update_expr += f"approval_config.{name_placeholder} = {placeholder}, "
                    expr_names[name_placeholder] = key
                    expr_values[placeholder] = value
            else:
                update_expr += "approval_config = :ac, "
                expr_values[":ac"] = ac

        # p64 R7.4 — the Quality_Config's edit path. p64 shipped this sub-object
        # create-only: `create_pipeline` persisted it (2c.1) and the wizard learned
        # to read it back (2c.2), but no task owned the write path here, so a
        # per-pipeline setting could be chosen once and never changed.
        # `PipelineFormModal.tsx`'s `isEdit` branch records the consequence and the
        # order to fix it in — "Add the handler branch first, then the control" —
        # because a toggle over a handler with no branch renders, accepts a click,
        # submits, and changes nothing.
        #
        # **Per key, exactly as `approval_config` above.** The `if_not_exists`
        # seeds the map in its own call (overlapping document paths are illegal in
        # one expression), then each submitted key is set at its own path. A
        # whole-map `SET quality_config = :qc` would make every submission a
        # replacement, so a client sending only `dedup_enabled` would erase
        # `dedup_columns` — and, worse, erase an explicit `data_quality_enabled`,
        # putting the pipeline back on its template default. That is the silent
        # reclassification R6.6 forbids, arriving through the edit path.
        #
        # **A key the body omits is not written, and that is the mechanism absence
        # depends on.** `_default_quality_config` deliberately seeds no
        # `data_quality_enabled` because absence means "use the template's own
        # default" — on for `glue_job_s3_tables.py.j2`, off for the other four
        # (`LGR-b993b94-7`, template/ratified; `LGR-b993b94-5` for the resolver).
        # A per-key merge writes only the keys present in the submission, so an
        # edit that does not mention the key leaves the record's posture alone,
        # whether that posture is an explicit boolean or a load-bearing absence.
        #
        # **What this deliberately does NOT do: it cannot REMOVE a key.** Once
        # `data_quality_enabled` is stored, no request through this branch reverts
        # the pipeline to its template default — `null` is refused by the validator
        # above, on both endpoints, rather than being given a second meaning here
        # (see the Decision block in the commit body). A revert verb is a product
        # question — whether the UI offers "use the template default" at all — and
        # inventing one silently would put a third state on the wire that
        # `create_pipeline` does not accept.
        #
        # The attribute name comes from `QUALITY_CONFIG_KEY`, never a literal: the
        # reader is `code_assembly` in another Lambda and the two agree only through
        # that string (`LGR-eee8ac8-5`). A second spelling would not fail — it would
        # write an attribute nothing reads, so the operator's toggle would look
        # saved and do nothing.
        if QUALITY_CONFIG_KEY in data:
            qc = data[QUALITY_CONFIG_KEY]
            # A non-dict is already a 400 from `_validate_quality_config`, so this
            # is a backstop. Unlike `approval_config` above it has NO else arm that
            # persists the scalar: every reader does
            # `record.get(QUALITY_CONFIG_KEY, {}).get(key, default)`, so a stored
            # scalar makes the *reader* raise `AttributeError` several Lambdas away.
            # Writing nothing is fail-closed — `resolve_quality_config` and
            # `resolve_data_quality_enabled` both read absence as "nothing was
            # asked for" — which is `_submitted_quality_config`'s arm 3, kept for
            # its own reason rather than for symmetry with its sibling.
            if isinstance(qc, dict):
                # Ensure the top-level map exists first (separate call to avoid overlapping paths)
                pipelines_table.update_item(
                    Key={"pipeline_id": pipeline_id},
                    UpdateExpression=f"SET {QUALITY_CONFIG_KEY} = if_not_exists({QUALITY_CONFIG_KEY}, :empty_map)",
                    ExpressionAttributeValues={":empty_map": {}},
                    ConditionExpression=_PIPELINE_EXISTS,
                )
                # `convert_floats_to_decimal` is serialisation, not validation: no
                # recognised key carries a number today, but unrecognised keys are
                # persisted verbatim and boto3 refuses `float`, so a
                # forward-compatible client sending one would otherwise take the
                # whole edit down with a `TypeError` on `update_item`. The shared
                # converter is the single copy P28 R19.2 consolidated six locals
                # into — do not add a seventh.
                for key, value in convert_floats_to_decimal(qc).items():
                    safe_key = key.replace("-", "_")
                    placeholder = f":qc_{safe_key}"
                    name_placeholder = f"#qc_{safe_key}"
                    # Alias the nested key via ExpressionAttributeNames so DynamoDB
                    # reserved words are legal as document-path segments.
                    update_expr += f"{QUALITY_CONFIG_KEY}.{name_placeholder} = {placeholder}, "
                    expr_names[name_placeholder] = key
                    expr_values[placeholder] = value

        # Handle schedule field — store in record and manage EventBridge rule
        schedule_expression = data.get("schedule")  # can be string or None/""
        has_schedule_field = "schedule" in data
        if has_schedule_field:
            if schedule_expression:
                update_expr += "schedule = :schedule, "
                expr_values[":schedule"] = schedule_expression
            else:
                # Remove schedule from record
                update_expr += "schedule = :schedule, "
                expr_values[":schedule"] = ""

        # Attributes to REMOVE rather than SET. A list because there are now two
        # clearable attributes; it renders to the same `REMOVE mapping_context`
        # this was a bare string for when `mapping_context` was the only one.
        remove_names: list[str] = []

        # Handle mapping_context field
        if "mapping_context" in data:
            mc = data["mapping_context"]
            if len(mc) > 2000:
                return create_response(400, {"error": "mapping_context exceeds 2000 character limit"})
            if mc:
                update_expr += "mapping_context = :mc, "
                expr_values[":mc"] = mc
            else:
                # Remove attribute when cleared
                remove_names.append("mapping_context")

        # p55 R8.4 — the Selected_Worksheet's edit path. Three intents, decided by
        # `_submitted_worksheet_edit`, whose docstring carries the reasoning:
        # omitted → leave alone, a name → SET verbatim, blank/null → REMOVE.
        #
        # The attribute name comes from `SELECTED_WORKSHEET_ATTRIBUTE`, never a
        # literal, because the reader lives in another Lambda and the two only ever
        # agree through that string (LGR-eee8ac8-5). A second spelling here would
        # not fail — it would write an attribute nothing reads, so the operator
        # would see the refusal persist after apparently fixing it.
        worksheet_edit = _submitted_worksheet_edit(data)
        if not isinstance(worksheet_edit, _WorksheetUnchanged):
            if worksheet_edit is None:
                # Clearing REMOVES the key. It must not store `""`, which the
                # reader would treat as a selection naming no sheet and refuse on
                # every run (R8.5) — see `_submitted_worksheet_edit`.
                remove_names.append(SELECTED_WORKSHEET_ATTRIBUTE)
            else:
                update_expr += f"{SELECTED_WORKSHEET_ATTRIBUTE} = :sw, "
                expr_values[":sw"] = worksheet_edit

        update_expr += "updated_at = :updated"
        expr_values[":updated"] = datetime.now(UTC).isoformat()

        if not expr_values:
            return create_response(400, {"error": "No valid fields to update"})

        # Validate schedule expression before persisting. The rule's payload
        # carries the pipeline's `source_type`, which an edit cannot change
        # (`source_config` is not editable here), so it is read from the record.
        # A record with none predates P17 and is an S3 upload pipeline, the same
        # default the trigger applies.
        if has_schedule_field and schedule_expression:
            stored_item = pipelines_table.get_item(
                Key={"pipeline_id": pipeline_id}, ProjectionExpression="pipeline_id, source_type"
            ).get("Item")
            if stored_item is None:
                # Refused here, before the rule exists: the record write below
                # would refuse too, but only after an EventBridge rule had been
                # created for a pipeline that is not there.
                return _pipeline_not_found(pipeline_id)
            stored_source_type = item_str(stored_item, "source_type") or "S3_UPLOAD"
            try:
                trigger_arn = require_env("PIPELINE_TRIGGER_ARN", "")
                if not trigger_arn:
                    # The same deployment fault create_pipeline refuses; it used
                    # to skip silently here and store a schedule nothing runs.
                    raise RuntimeError("PIPELINE_TRIGGER_ARN is not set on the dashboard API")
                _manage_schedule_rule(
                    pipeline_id, schedule_expression, trigger_arn, pipeline_id, source_type=stored_source_type
                )
            except ValueError as ve:
                return create_response(400, {"error": str(ve)})
            except Exception as e:
                # Nothing on the record has been written yet. The rule itself may
                # now carry the new expression with the old target, or no target,
                # so the message says the schedule was not saved and to retry.
                logger.error(f"Schedule rule update failed for {pipeline_id}: {e}")
                from .jdbc_resources import _redact_aws_detail

                return create_response(
                    500,
                    {
                        "error": (
                            f"Schedule was not saved: {_redact_aws_detail(e)}. The pipeline record is "
                            "unchanged; save again to retry."
                        ),
                        "reason": _REASON_PROVISIONING_FAILED,
                        "stage": "schedule",
                        "pipeline_id": pipeline_id,
                    },
                )

        # Clearing a schedule removes the rule FIRST and writes the record only
        # once removal is confirmed. It used to write the empty schedule, then
        # try the delete and log any failure, so the API returned 200 "Pipeline
        # updated successfully" while the rule kept firing and the dashboard
        # showed no schedule. The delete also used to be skipped outright when
        # PIPELINE_TRIGGER_ARN was unset, although deleting never uses it.
        # `_manage_schedule_rule`'s delete branch raises on anything but a rule
        # that is already gone, including a RemoveTargets failed entry.
        if has_schedule_field and not schedule_expression:
            try:
                _manage_schedule_rule(pipeline_id, None, require_env("PIPELINE_TRIGGER_ARN", ""), pipeline_id)
            except Exception as e:
                logger.error(f"Schedule rule removal failed for {pipeline_id}: {e}")
                from .jdbc_resources import _redact_aws_detail

                return create_response(
                    500,
                    {
                        "error": (
                            f"Schedule was not cleared: {_redact_aws_detail(e)}. The pipeline record still "
                            "shows the schedule and its rule may still run; save again to retry."
                        ),
                        "reason": _REASON_PROVISIONING_FAILED,
                        "stage": "schedule",
                        "pipeline_id": pipeline_id,
                    },
                )

        update_kwargs: dict[str, Any] = {
            "Key": {"pipeline_id": pipeline_id},
            "UpdateExpression": update_expr + (f" REMOVE {', '.join(remove_names)}" if remove_names else ""),
            "ExpressionAttributeValues": expr_values,
            # UpdateItem creates an item when the key is absent, so without this
            # an edit to a missing or just-deleted id wrote a partial record and
            # returned 200. See `_PIPELINE_EXISTS`.
            "ConditionExpression": _PIPELINE_EXISTS,
        }
        if expr_names:
            update_kwargs["ExpressionAttributeNames"] = expr_names

        pipelines_table.update_item(**update_kwargs)

        # Audit: record pipeline edit
        ctx = extract_request_context(event or {})
        write_audit_entry(
            action="pipeline_edited",
            resource_type="pipeline",
            resource_id=pipeline_id,
            details={"changes": list(data.keys())},
            **ctx,
        )

        return create_response(
            200, {"success": True, "pipeline_id": pipeline_id, "message": "Pipeline updated successfully"}
        )

    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        from .jdbc_resources import _aws_error_code

        if _aws_error_code(e) == "ConditionalCheckFailedException":
            # Every write in this function carries `_PIPELINE_EXISTS`, so a
            # failed condition here means the id names no pipeline. The first
            # write to fail is the first write attempted, so nothing was stored.
            return _pipeline_not_found(pipeline_id)
        return handle_error(e, f"Error editing pipeline {pipeline_id}")


def get_pipeline_details(pipeline_key: str, schema_page: int = 1, schema_page_size: int = 50) -> dict[str, Any]:
    """Get detailed information for a specific pipeline with paginated schema history

    Args:
        pipeline_key: Pipeline ID
        schema_page: Page number for schema history (1-indexed)
        schema_page_size: Number of schema versions per page
    """
    try:
        logger.info(f"Fetching details for pipeline: {pipeline_key} (schema page {schema_page})")

        # Use GSI to query all items for this pipeline efficiently
        items = []
        query_kwargs: dict[str, Any] = {
            "IndexName": "PipelineIndex",
            "KeyConditionExpression": Key("pipeline_id").eq(pipeline_key),
        }

        # Handle pagination to get ALL data for this pipeline
        while True:
            response = state_table.query(**query_kwargs)
            items.extend(response.get("Items", []))

            # Check if there are more pages
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        logger.info(f"Found {len(items)} state items for pipeline {pipeline_key} via GSI")

        # If no state data, check if pipeline exists in pipelines table.
        # P28 R17.3: only a genuinely-missing item is a 404 — read errors
        # (throttles, access denied) propagate to the outer handler and
        # surface as 5xx instead of masquerading as "Pipeline not found".
        if not items:
            logger.info(f"No state data found, checking pipelines table for {pipeline_key}")
            pipeline_response = pipelines_table.get_item(Key={"pipeline_id": pipeline_key})
            if "Item" not in pipeline_response:
                logger.warning(f"Pipeline {pipeline_key} not found in pipelines table")
                return create_response(404, {"error": f"Pipeline not found: {pipeline_key}"})

            # Return basic pipeline info with no execution data
            pipeline_item = pipeline_response["Item"]
            logger.info(f"Returning basic info for pipeline {pipeline_key} (no execution data yet)")
            return create_response(
                200,
                {
                    "pipeline_key": pipeline_key,
                    "pipeline_id": pipeline_key,
                    "pipeline_name": pipeline_item.get("pipeline_name", pipeline_key),
                    "target_schema_name": pipeline_item.get("target_schema_name", ""),
                    "ai_model": pipeline_item.get("ai_model", ""),
                    "pipeline_description": pipeline_item.get("pipeline_description", ""),
                    "status": pipeline_item.get("status", "active"),
                    "created_at": pipeline_item.get("created_at", ""),
                    "updated_at": pipeline_item.get("updated_at", ""),
                    "file_count": 0,
                    "schema_version": 0,
                    "current_schema_hash": None,
                    "schema_history": [],
                    "files": [],
                    "all_files": [],
                    "has_mapping": False,
                    "mapping_count": 0,
                    "unmapped_source_count": 0,
                    "unmapped_target_count": 0,
                    # P17 Requirement 6.7: always surface source_type and
                    # source_config, defaulting to S3_UPLOAD / {} for
                    # pre-P17 records (Requirement 19).
                    "source_type": pipeline_item.get("source_type", "S3_UPLOAD"),
                    "source_config": pipeline_item.get("source_config", {}),
                    **({"schedule": pipeline_item["schedule"]} if pipeline_item.get("schedule") else {}),
                    **(
                        {"mapping_context": pipeline_item["mapping_context"]}
                        if pipeline_item.get("mapping_context")
                        else {}
                    ),
                    **({"target_config": pipeline_item["target_config"]} if pipeline_item.get("target_config") else {}),
                    **({"target_type": pipeline_item["target_type"]} if pipeline_item.get("target_type") else {}),
                    # p55 R8.4 — the Selected_Worksheet, so an operator can SEE what
                    # is configured. `in`, NOT `.get()`: every sibling above is a
                    # truth test, and copying that idiom here would collapse a
                    # recorded-but-empty selection into "nothing configured". Those
                    # are different facts on this attribute — the reader
                    # (`schema_extract._configured_worksheet`) makes an empty
                    # selection refuse under R8.5 where an absent one lets a
                    # single-sheet workbook proceed under R8.1 — so the response
                    # mirrors the record's membership exactly: key present (even as
                    # `""`) or key absent, never `""` standing in for absent.
                    **(
                        {SELECTED_WORKSHEET_ATTRIBUTE: pipeline_item[SELECTED_WORKSHEET_ATTRIBUTE]}
                        if SELECTED_WORKSHEET_ATTRIBUTE in pipeline_item
                        else {}
                    ),
                    # p64 R7.4 — the two governance sub-objects, so the wizard's
                    # edit hydration has something to read. Neither was projected
                    # before: `hydrateWizardStateFromPipeline` (2c.2) implements the
                    # `quality_config` round trip correctly and received nothing
                    # live, and `Pipeline.approval_config` has carried a declaration
                    # in `types.ts` against a response that never sent the field.
                    #
                    # **This arm matters more than the enriched one.** It is the arm
                    # a pipeline with no execution history takes — i.e. one created
                    # and not yet run, which is exactly the pipeline most likely to
                    # be edited. Projecting only the enriched arm below would leave
                    # the wizard hydrating from nothing on the common case and
                    # working on the rare one, which is worse than not projecting at
                    # all because the failure would look intermittent.
                    #
                    # `in`, NOT `.get()`, for the Selected_Worksheet's reason above
                    # and one of its own: within a present `quality_config`, an
                    # ABSENT `data_quality_enabled` means "use the template's own
                    # default" (`LGR-b993b94-7`), and the client's three-state reader
                    # (`utils/quality-config.ts`, `LGR-b993b94-9`) distinguishes it
                    # from `false`. A truth test would drop a stored `{}` — a
                    # recorded "no overrides" — and collapse it into "nothing
                    # configured", which are different facts to that reader.
                    **(
                        {APPROVAL_CONFIG_KEY: pipeline_item[APPROVAL_CONFIG_KEY]}
                        if APPROVAL_CONFIG_KEY in pipeline_item
                        else {}
                    ),
                    **(
                        {QUALITY_CONFIG_KEY: pipeline_item[QUALITY_CONFIG_KEY]}
                        if QUALITY_CONFIG_KEY in pipeline_item
                        else {}
                    ),
                },
            )

        # Build pipeline details
        pipeline: dict[str, Any] = {
            "pipeline_key": pipeline_key,
            "status": "active",
            "file_count": 0,
            "schema_version": 0,
            "created_at": None,
            "updated_at": None,
            "current_schema_hash": None,
            "schema_history": [],
            "files": [],
            "all_files": [],
            "has_mapping": False,
            "mapping_count": 0,
            "unmapped_source_count": 0,
            "unmapped_target_count": 0,
            "mapping_id": "",
            "mapping_details": None,
            "mapping_confidence": 0,
            "ai_model_used": "",
        }

        # Collect mapping information
        mapping_info: dict[str, Any] = {}

        for item in items:
            step_name = item_str(item, "step_name")
            timestamp = item_str(item, "timestamp")

            # Track timestamps
            if not pipeline["created_at"] or (timestamp and timestamp < pipeline["created_at"]):
                pipeline["created_at"] = timestamp
            if not pipeline["updated_at"] or (timestamp and timestamp > pipeline["updated_at"]):
                pipeline["updated_at"] = timestamp
            # Count files and collect file info
            if step_name == "file_validation":
                original_s3_uri = item_str(item, "original_s3_uri")
                file_key = original_s3_uri.split("/")[-1] if original_s3_uri else ""

                if item.get("status") == "success" and file_key and file_key != "":
                    pipeline["file_count"] += 1

                if not file_key or file_key == "":
                    continue

                file_info = {
                    "file_key": file_key,
                    "processed_at": timestamp,
                    "status": item_str(item, "status", "unknown"),
                    "file_type": item_str(item, "file_type", "unknown"),
                    "original_s3_uri": original_s3_uri,
                    "json_s3_uri": item_str(item, "json_s3_uri"),
                }
                pipeline["files"].append(file_info)
                pipeline["all_files"].append(file_info)

            # Get schema information
            if step_name == "schema_extract" and "schema_hash" in item:
                if not pipeline["updated_at"] or timestamp > pipeline["updated_at"]:
                    pipeline["current_schema_hash"] = item["schema_hash"]

                json_s3_uri = item_str(item, "json_s3_uri")
                file_name = json_s3_uri.split("/")[-1] if json_s3_uri else "unknown"

                current_hash = item["schema_hash"]
                run_id = item_str(item, "run_id")

                # Check if this schema hash already exists (deduplicate)
                existing_entry = next((s for s in pipeline["schema_history"] if s["hash"] == current_hash), None)

                if existing_entry:
                    # Schema already exists — add this run to its runs list
                    if run_id and not any(r["run_id"] == run_id for r in existing_entry.get("runs", [])):
                        existing_entry["runs"].append(
                            {
                                "run_id": run_id,
                                "timestamp": timestamp,
                                "file_name": file_name,
                                "code_version_id": None,
                                "test_status": None,
                            }
                        )
                        # Keep runs sorted newest-first
                        existing_entry["runs"].sort(key=lambda r: r["timestamp"], reverse=True)
                else:
                    # New unique schema — create entry with runs array
                    schema_entry = {
                        "version": len(pipeline["schema_history"]) + 1,
                        "hash": item["schema_hash"],
                        "created": timestamp,
                        "extracted_schema": item.get("extracted_schema", ""),
                        "file_name": file_name,
                        "json_s3_uri": item_str(item, "json_s3_uri"),
                        "has_mapping": False,
                        "mapping_details": None,
                        "runs": [
                            {
                                "run_id": run_id,
                                "timestamp": timestamp,
                                "file_name": file_name,
                                "code_version_id": None,
                                "test_status": None,
                            }
                        ]
                        if run_id
                        else [],
                    }

                    pipeline["schema_history"].append(schema_entry)

                    # Sort and renumber
                    pipeline["schema_history"].sort(key=lambda x: x["created"])
                    for i, schema in enumerate(pipeline["schema_history"]):
                        schema["version"] = i + 1
                        if i > 0:
                            schema["is_schema_change"] = schema["hash"] != pipeline["schema_history"][i - 1]["hash"]
                        else:
                            schema["is_schema_change"] = True
                    pipeline["schema_version"] = len(pipeline["schema_history"])
            # Get schema mapping information
            if step_name == "schema_mapping":
                source_schema_hash = item_str(item, "source_schema_hash")
                mapping_id = item_str(item, "mapping_id")

                if mapping_id and source_schema_hash:
                    try:
                        mapping_response = mappings_table.get_item(Key={"mapping_id": mapping_id})
                        if "Item" in mapping_response:
                            mapping_item = mapping_response["Item"]
                            mapping_details = json.loads(item_str(mapping_item, "mapping_result", "{}"))
                            mapping_confidence = mapping_item.get("confidence_score", 0)
                            ai_model_used = item_str(mapping_item, "ai_model_used")

                            mapping_info[source_schema_hash] = {
                                "mapping_details": mapping_details,
                                "mapping_confidence": mapping_confidence,
                                "ai_model_used": ai_model_used,
                                "mapping_count": item_int(item, "mapping_count", 0),
                                "unmapped_source_count": item_int(item, "unmapped_source_count", 0),
                                "unmapped_target_count": item_int(item, "unmapped_target_count", 0),
                            }

                            # Update pipeline-level mapping info with latest
                            if not pipeline.get("mapping_timestamp") or timestamp > pipeline.get("mapping_timestamp"):
                                pipeline["has_mapping"] = True
                                pipeline["mapping_timestamp"] = timestamp
                                pipeline["mapping_details"] = mapping_details
                                pipeline["mapping_confidence"] = mapping_confidence
                                pipeline["ai_model_used"] = ai_model_used
                                pipeline["mapping_count"] = item_int(item, "mapping_count", 0)
                                pipeline["unmapped_source_count"] = item_int(item, "unmapped_source_count", 0)
                                pipeline["unmapped_target_count"] = item_int(item, "unmapped_target_count", 0)
                                pipeline["mapping_id"] = mapping_id
                    except Exception as e:
                        logger.error(f"Error fetching mapping details: {e}")

        # Enrich runs with code_version_id and test_status from the create_code
        # and test_code steps. P28 R8.2 (task 14.2): match in canonical space —
        # new rows carry canonical snake_case names, rows written before the
        # rename persist with the legacy "CodeGeneration"/"CodeTesting" names.
        def _canonical_or_raw(name: str) -> str:
            try:
                return str(from_any(name))
            except ValueError:
                return name

        run_code_map = {}  # run_id -> code_version_id
        run_test_map = {}  # run_id -> test_status
        for item in items:
            step_name = _canonical_or_raw(item_str(item, "step_name"))
            run_id = item_str(item, "run_id")
            if not run_id:
                continue
            if step_name == StepName.CREATE_CODE:
                code_vid = None
                output_data = item_dict(item, "output_data")
                approval_data_str = item.get("approval_data", "")
                if approval_data_str:
                    try:
                        ad: Any = (
                            json.loads(approval_data_str) if isinstance(approval_data_str, str) else approval_data_str
                        )
                        code_vid = ad.get("code_version_id")
                    except Exception:
                        pass
                if not code_vid and isinstance(output_data, dict):
                    code_vid = output_data.get("code_version_id")
                if code_vid:
                    run_code_map[run_id] = code_vid
            elif step_name == StepName.TEST_CODE:
                test_status = None
                output_data = item_dict(item, "output_data")
                approval_data_str = item.get("approval_data", "")
                if approval_data_str:
                    try:
                        ad = json.loads(approval_data_str) if isinstance(approval_data_str, str) else approval_data_str
                        test_status = ad.get("test_status")
                    except Exception:
                        pass
                if not test_status and isinstance(output_data, dict):
                    test_status = output_data.get("test_status")
                if test_status:
                    run_test_map[run_id] = test_status

        # Backfill code_version_id and test_status into schema_history runs
        for schema_entry in pipeline["schema_history"]:
            for run in schema_entry.get("runs", []):
                rid = run.get("run_id", "")
                if rid in run_code_map:
                    run["code_version_id"] = run_code_map[rid]
                if rid in run_test_map:
                    run["test_status"] = run_test_map[rid]

        # Associate mapping information with schemas
        for source_schema_hash, mapping_data in mapping_info.items():
            for schema_entry in pipeline["schema_history"]:
                if str(schema_entry["hash"]) == str(source_schema_hash):
                    schema_entry["has_mapping"] = True
                    schema_entry.update(mapping_data)

        # Fetch pipeline metadata
        try:
            pipeline_response = pipelines_table.get_item(Key={"pipeline_id": pipeline_key})
            if "Item" in pipeline_response:
                pipeline_item = pipeline_response["Item"]
                pipeline["pipeline_name"] = pipeline_item.get("pipeline_name", pipeline_key)
                pipeline["target_schema_name"] = pipeline_item.get("target_schema_name", "")
                pipeline["ai_model"] = pipeline_item.get("ai_model", "")
                pipeline["pipeline_description"] = pipeline_item.get("pipeline_description", "")
                # Expose schedule configuration (Req 3, AC 11)
                if pipeline_item.get("schedule"):
                    pipeline["schedule"] = pipeline_item["schedule"]
                # Expose mapping_context for the Schema page
                if pipeline_item.get("mapping_context"):
                    pipeline["mapping_context"] = pipeline_item["mapping_context"]
                # Expose target_config for the Overview page
                if pipeline_item.get("target_config"):
                    pipeline["target_config"] = pipeline_item["target_config"]
                if pipeline_item.get("target_type"):
                    pipeline["target_type"] = pipeline_item["target_type"]
                # P17 Requirement 6.7: surface source_type and source_config
                # alongside target_type / target_config. Defaults to
                # S3_UPLOAD / {} so pre-P17 records (Requirement 19) still
                # report a valid source_type to the frontend.
                pipeline["source_type"] = pipeline_item.get("source_type", "S3_UPLOAD")
                pipeline["source_config"] = pipeline_item.get("source_config", {})
                # p55 R8.4 — the Selected_Worksheet. Same membership test, same
                # reason, as the no-state-data arm above: `in`, not `.get()`, and
                # no default, because absent and `""` are different facts to the
                # reader and a default would manufacture one of them. This is the
                # arm a pipeline that has actually run takes, so it is the one an
                # operator reading a refusal is looking at.
                if SELECTED_WORKSHEET_ATTRIBUTE in pipeline_item:
                    pipeline[SELECTED_WORKSHEET_ATTRIBUTE] = pipeline_item[SELECTED_WORKSHEET_ATTRIBUTE]
                # p64 R7.4 — the same two sub-objects as the no-state-data arm
                # above, with the same membership test for the same reasons. Both
                # arms must project them or the wizard's edit hydration would work
                # only for a pipeline that has run.
                if APPROVAL_CONFIG_KEY in pipeline_item:
                    pipeline[APPROVAL_CONFIG_KEY] = pipeline_item[APPROVAL_CONFIG_KEY]
                if QUALITY_CONFIG_KEY in pipeline_item:
                    pipeline[QUALITY_CONFIG_KEY] = pipeline_item[QUALITY_CONFIG_KEY]
            else:
                pipeline["pipeline_name"] = pipeline_key
        except Exception as e:
            logger.error(f"Error fetching pipeline metadata: {e}")
            pipeline["pipeline_name"] = pipeline_key

        return create_response(200, pipeline)

    except Exception as e:
        return handle_error(e, "Error fetching pipeline details")


def get_archived_pipelines() -> dict[str, Any]:
    """Get all archived pipelines"""
    try:
        response = pipelines_table.scan(
            FilterExpression="#status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "archived"},
        )

        pipelines = response.get("Items", [])

        # Sort by updated_at descending
        pipelines.sort(key=lambda x: item_str(x, "updated_at"), reverse=True)

        return create_response(200, pipelines)

    except Exception as e:
        return handle_error(e, "Error fetching archived pipelines")


def archive_pipeline(pipeline_id: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Archive a pipeline by setting status to 'archived'"""
    try:
        from datetime import datetime

        # Update pipeline status to archived
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="SET #status = :status, updated_at = :updated",
            ExpressionAttributeValues={":status": "archived", ":updated": datetime.now(UTC).isoformat()},
            ExpressionAttributeNames={"#status": "status"},
        )

        # Disable EventBridge rules (both S3 trigger and schedule)
        try:
            import boto3 as _boto3

            events_client = _boto3.client("events")
            for rule_name in [
                f"agentic-data-ingestion-{pipeline_id}-trigger",
                f"agentic-data-ingestion-schedule-{pipeline_id}",
            ]:
                try:
                    events_client.disable_rule(Name=rule_name)
                    logger.info(f"Disabled EventBridge rule: {rule_name}")
                except Exception as e:
                    if "ResourceNotFoundException" in str(e):
                        pass
                    else:
                        logger.warning(f"Could not disable EventBridge rule {rule_name}: {e}")
        except Exception as e:
            logger.warning(f"Could not disable EventBridge rules: {e}")

        # Audit: record pipeline archival
        ctx = extract_request_context(event or {})
        write_audit_entry(
            action="pipeline_archived",
            resource_type="pipeline",
            resource_id=pipeline_id,
            **ctx,
        )

        return create_response(200, {"success": True, "pipeline_id": pipeline_id, "status": "archived"})

    except Exception as e:
        return handle_error(e, "Error archiving pipeline")


def restore_pipeline(pipeline_id: str) -> dict[str, Any]:
    """Restore an archived pipeline by setting status to 'active'"""
    try:
        from datetime import datetime

        # Fetch pipeline to check for schedule
        pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
        pipeline_item = pipeline_resp.get("Item", {})

        # Update pipeline status to active
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="SET #status = :status, updated_at = :updated",
            ExpressionAttributeValues={":status": "active", ":updated": datetime.now(UTC).isoformat()},
            ExpressionAttributeNames={"#status": "status"},
        )

        # Re-enable EventBridge rules
        try:
            import boto3 as _boto3

            events_client = _boto3.client("events")
            # Always try to re-enable the S3 trigger rule
            try:
                events_client.enable_rule(Name=f"agentic-data-ingestion-{pipeline_id}-trigger")
                logger.info(f"Enabled EventBridge rule: agentic-data-ingestion-{pipeline_id}-trigger")
            except Exception as e:
                if "ResourceNotFoundException" in str(e):
                    pass
                else:
                    logger.warning(f"Could not enable S3 trigger rule: {e}")

            # Re-enable schedule rule only if pipeline has a schedule
            if pipeline_item.get("schedule"):
                try:
                    events_client.enable_rule(Name=f"agentic-data-ingestion-schedule-{pipeline_id}")
                    logger.info(f"Enabled schedule rule: agentic-data-ingestion-schedule-{pipeline_id}")
                except Exception as e:
                    if "ResourceNotFoundException" in str(e):
                        pass
                    else:
                        logger.warning(f"Could not enable schedule rule: {e}")
        except Exception as e:
            logger.warning(f"Could not enable EventBridge rules: {e}")

        return create_response(200, {"success": True, "pipeline_id": pipeline_id, "status": "active"})

    except Exception as e:
        return handle_error(e, "Error restoring pipeline")


def _other_pipelines_using_connection(connection_name: str, pipeline_id: str) -> list[str]:
    """Ids of every OTHER pipeline whose record names ``connection_name``.

    A connection this system created for pipeline A can be referenced by
    pipeline B in "Use Glue Connection" mode. B treats it as user-owned and
    will never delete it, so deleting A must not delete it either while B
    exists. ``_reject_managed_glue_connection`` stops new references of that
    kind at create and edit time; this protects the ones that already exist.

    Every place a record can name a connection is read: the top-level
    ``source_connection_name`` and ``glue_connection_name`` on either side, plus
    ``target_config.jdbc_connection_name``. A full scan, projected to those
    fields and paginated; the pipelines table holds one row per pipeline, and
    this runs once per delete, which already scans the approvals table.

    Raises:
        Exception: Whatever the scan raises. The caller keeps the connection
            when it cannot tell, because a leaked connection is cheaper than a
            broken pipeline.
    """
    users: list[str] = []
    kwargs: dict[str, Any] = {
        "ProjectionExpression": (
            "pipeline_id, source_connection_name, #sc.glue_connection_name, "
            "#tc.glue_connection_name, #tc.jdbc_connection_name"
        ),
        "ExpressionAttributeNames": {"#sc": "source_config", "#tc": "target_config"},
    }
    while True:
        resp = pipelines_table.scan(**kwargs)
        for item in resp.get("Items", []):
            other_id = item_str(item, "pipeline_id")
            if other_id == pipeline_id:
                continue
            source_config = item_dict(item, "source_config")
            target_config = item_dict(item, "target_config")
            named = {
                item_str(item, "source_connection_name"),
                source_config.get("glue_connection_name"),
                target_config.get("glue_connection_name"),
                target_config.get("jdbc_connection_name"),
            }
            if connection_name in named:
                users.append(other_id)
        # A real LastEvaluatedKey is a non-empty key map. Anything else ends the
        # walk, so a malformed page cannot spin this loop forever.
        last_key = resp.get("LastEvaluatedKey")
        if not isinstance(last_key, dict) or not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return users


def _delete_owned_connection(
    glue_client: Any,
    connection_name: str,
    pipeline_id: str,
    result_key: str,
    cleanup_results: dict[str, Any],
    cleanup_failures: list[dict[str, Any]],
) -> None:
    """Delete one Glue connection this pipeline owns, unless another pipeline uses it.

    Records the outcome the way the other ``delete_pipeline`` cleanup blocks do:
    ``cleanup_results[result_key]`` is ``True`` when deleted and ``False`` when
    it was already absent (``EntityNotFoundException`` is success). When another
    pipeline still references the connection it is kept, and
    ``cleanup_results[f"{result_key}_kept_for"]`` lists those pipeline ids. When
    the reference check itself fails, the connection is kept and the failure is
    recorded in ``cleanup_failures``.
    """
    try:
        users = _other_pipelines_using_connection(connection_name, pipeline_id)
    except Exception as e:
        logger.error(f"Not deleting {connection_name}: could not check whether another pipeline uses it: {e}")
        cleanup_failures.append(
            {
                "resource": result_key,
                "error": f"kept {connection_name}: could not check whether another pipeline uses it: {e}",
            }
        )
        return
    if users:
        logger.warning(f"Keeping {connection_name}: still referenced by pipeline(s) {users}")
        cleanup_results[f"{result_key}_kept_for"] = users
        return
    try:
        try:
            glue_client.delete_connection(ConnectionName=connection_name)
            cleanup_results[result_key] = True
        except glue_client.exceptions.EntityNotFoundException:
            cleanup_results[result_key] = False
    except Exception as e:
        logger.error(f"Failed to delete {result_key} {connection_name}: {e}")
        cleanup_failures.append({"resource": result_key, "error": str(e)})


def delete_pipeline(pipeline_id: str) -> dict[str, Any]:
    """Permanently delete a pipeline and all associated resources.

    Follows a best-effort cleanup pattern: each resource type is cleaned up
    independently, failures are logged and collected, and the pipeline record
    is deleted last.

    Requires the pipeline to be in 'archived' status before deletion.
    """
    try:
        import boto3 as _boto3
        from boto3.dynamodb.conditions import Attr as _Attr
        from boto3.dynamodb.conditions import Key as _Key

        logger.info(f"Starting permanent deletion of pipeline: {pipeline_id}")

        # 1. Fetch pipeline record — 404 if not found
        pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
        pipeline_item = pipeline_resp.get("Item")
        if not pipeline_item:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        # 2. Reject if status != "archived" (409)
        current_status = pipeline_item.get("status", "")
        if current_status != "archived":
            return create_response(
                409,
                {
                    "error": "Pipeline must be archived before deletion",
                    "current_status": current_status,
                    "pipeline_id": pipeline_id,
                },
            )

        # 3. Check for running executions — stop them if found
        stopped_executions = []
        try:
            exec_resp = executions_table.query(
                KeyConditionExpression=_Key("pipeline_id").eq(pipeline_id),
            )
            for ex in exec_resp.get("Items", []):
                if ex.get("status") == "RUNNING":
                    execution_arn = item_str(ex, "execution_id")
                    if execution_arn:
                        try:
                            sfn_client = _boto3.client("stepfunctions")
                            sfn_client.stop_execution(
                                executionArn=execution_arn,
                                error="PipelineDeleted",
                                cause="Pipeline is being permanently deleted",
                            )
                            stopped_executions.append(item_str(ex, "run_id"))
                        except Exception as sfn_err:
                            logger.warning(f"Failed to stop execution {execution_arn}: {sfn_err}")
        except Exception as exec_err:
            logger.warning(f"Failed to check running executions: {exec_err}")

        # 4. Set status to "deleting"
        try:
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_id},
                UpdateExpression="SET #status = :status, updated_at = :updated",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": "deleting",
                    ":updated": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as status_err:
            logger.warning(f"Failed to set deleting status: {status_err}")

        # 5. Best-effort cleanup loop
        cleanup_results: dict[str, Any] = {}
        cleanup_failures: list[dict[str, Any]] = []

        # JDBC pipelines own a per-pipeline runtime crawler, discovery-DB
        # tables, and (in select_rds mode only) a source-side Glue
        # Connection. Capture the discriminator early so the JDBC blocks
        # below stay no-ops for S3_UPLOAD pipelines. Mirrors the ownership
        # logic from create_pipeline (Task 5.1): if the user supplied an
        # existing ``glue_connection_name`` we MUST NOT delete it; if not,
        # AgenticDataIngestion created ``agentic-data-ingestion-{pipeline_id}-source`` and owns
        # its lifecycle.
        is_jdbc = pipeline_item.get("source_type") == "JDBC"
        source_owned_by_adi = is_jdbc and not item_dict(pipeline_item, "source_config").get("glue_connection_name")

        # 5a. Query & batch-delete state table entries (GSI PipelineIndex)
        try:
            deleted_state = 0
            query_kwargs: dict[str, Any] = {
                "IndexName": "PipelineIndex",
                "KeyConditionExpression": _Key("pipeline_id").eq(pipeline_id),
                "ProjectionExpression": "run_id, step_name",
            }
            while True:
                resp = state_table.query(**query_kwargs)
                items = resp.get("Items", [])
                if items:
                    with state_table.batch_writer() as batch:
                        for item in items:
                            batch.delete_item(Key={"run_id": item["run_id"], "step_name": item["step_name"]})
                            deleted_state += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                query_kwargs["ExclusiveStartKey"] = last_key
            cleanup_results["state_entries"] = deleted_state
        except Exception as e:
            logger.error(f"Failed to clean up state table: {e}")
            cleanup_failures.append({"resource": "state_entries", "error": str(e)})

        # 5b. Query & batch-delete executions table entries (PK = pipeline_id)
        try:
            deleted_exec = 0
            query_kwargs = {
                "KeyConditionExpression": _Key("pipeline_id").eq(pipeline_id),
                "ProjectionExpression": "pipeline_id, run_id",
            }
            while True:
                resp = executions_table.query(**query_kwargs)
                items = resp.get("Items", [])
                if items:
                    with executions_table.batch_writer() as batch:
                        for item in items:
                            batch.delete_item(Key={"pipeline_id": item["pipeline_id"], "run_id": item["run_id"]})
                            deleted_exec += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                query_kwargs["ExclusiveStartKey"] = last_key
            cleanup_results["executions"] = deleted_exec
        except Exception as e:
            logger.error(f"Failed to clean up executions table: {e}")
            cleanup_failures.append({"resource": "executions", "error": str(e)})

        # 5c. Scan & delete approvals (FilterExpression pipeline_id)
        # Also collect approval_ids for task token cleanup
        approval_ids = []
        try:
            deleted_approvals = 0
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": _Attr("pipeline_id").eq(pipeline_id),
                "ProjectionExpression": "approval_id",
            }
            while True:
                resp = approvals_table.scan(**scan_kwargs)
                items = resp.get("Items", [])
                for item in items:
                    approval_ids.append(item["approval_id"])
                    approvals_table.delete_item(Key={"approval_id": item["approval_id"]})
                    deleted_approvals += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            cleanup_results["approvals"] = deleted_approvals
        except Exception as e:
            logger.error(f"Failed to clean up approvals table: {e}")
            cleanup_failures.append({"resource": "approvals", "error": str(e)})

        # 5d. Delete associated task tokens
        try:
            deleted_tokens = 0
            task_tokens_table_name = require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
            task_tokens_tbl = dynamodb.Table(task_tokens_table_name)
            for approval_id in approval_ids:
                try:
                    task_tokens_tbl.delete_item(Key={"approval_id": approval_id})
                    deleted_tokens += 1
                except Exception:
                    pass
            cleanup_results["task_tokens"] = deleted_tokens
        except Exception as e:
            logger.error(f"Failed to clean up task tokens: {e}")
            cleanup_failures.append({"resource": "task_tokens", "error": str(e)})

        # 5e. Delete pipeline lock record
        try:
            locks_table_name = require_env("LOCKS_TABLE", "agentic_data_ingestion_pipeline_locks")
            locks_tbl = dynamodb.Table(locks_table_name)
            locks_tbl.delete_item(Key={"pipeline_prefix": pipeline_id})
            cleanup_results["locks"] = 1
        except Exception as e:
            logger.error(f"Failed to clean up lock record: {e}")
            cleanup_failures.append({"resource": "locks", "error": str(e)})

        # 5f. List & delete S3 objects under pipeline prefix
        try:
            upload_bucket = require_env("UPLOAD_BUCKET", "")
            deleted_objects = 0
            if upload_bucket:
                paginator = s3_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=upload_bucket, Prefix=f"{pipeline_id}/"):
                    objects = page.get("Contents", [])
                    if objects:
                        delete_keys = [{"Key": obj["Key"]} for obj in objects]
                        s3_client.delete_objects(
                            Bucket=upload_bucket,
                            Delete={"Objects": delete_keys},
                        )
                        deleted_objects += len(objects)
            cleanup_results["s3_objects"] = deleted_objects
        except Exception as e:
            logger.error(f"Failed to clean up S3 objects: {e}")
            cleanup_failures.append({"resource": "s3_objects", "error": str(e)})

        # 5g. Delete Glue job
        try:
            glue_client = _boto3.client("glue")
            glue_client.delete_job(JobName=f"agentic-data-ingestion-job-{pipeline_id}")
            cleanup_results["glue_job"] = True
        except Exception as e:
            error_str = str(e)
            if "EntityNotFoundException" in error_str:
                cleanup_results["glue_job"] = False  # Job didn't exist
            else:
                logger.error(f"Failed to delete Glue job: {e}")
                cleanup_failures.append({"resource": "glue_job", "error": error_str})

        # 5h. Delete EventBridge rules (both S3 trigger and schedule rules)
        try:
            events_client = _boto3.client("events")
            deleted_rules = []
            rule_names = [
                f"agentic-data-ingestion-{pipeline_id}-trigger",
                f"agentic-data-ingestion-schedule-{pipeline_id}",
            ]
            for rule_name in rule_names:
                try:
                    # Remove targets first
                    targets_resp = events_client.list_targets_by_rule(Rule=rule_name)
                    target_ids = [t["Id"] for t in targets_resp.get("Targets", [])]
                    if target_ids:
                        events_client.remove_targets(Rule=rule_name, Ids=target_ids)
                    events_client.delete_rule(Name=rule_name)
                    deleted_rules.append(rule_name)
                except Exception as rule_err:
                    error_str = str(rule_err)
                    if "ResourceNotFoundException" in error_str:
                        pass  # Rule didn't exist
                    else:
                        logger.warning(f"Failed to delete EventBridge rule {rule_name}: {rule_err}")
            cleanup_results["eventbridge_rules"] = deleted_rules
        except Exception as e:
            logger.error(f"Failed to clean up EventBridge rules: {e}")
            cleanup_failures.append({"resource": "eventbridge_rules", "error": str(e)})

        # 5i / 5j / 5k. JDBC-specific cleanup (Requirement 11.8).
        # Skipped silently for S3_UPLOAD pipelines so the existing
        # cleanup behaviour is unchanged for those.
        if is_jdbc:
            # Lazy-import DISCOVERY_DB so the import-time cost stays
            # off the cold-start path for non-JDBC pipelines (matches
            # Task 5.2's pattern).
            from .jdbc_resources import DISCOVERY_DB

            jdbc_glue_client = _boto3.client("glue")

            # 5i. Delete the per-pipeline runtime crawler created at
            # pipeline-creation time (Task 5.2). EntityNotFoundException
            # means it was already cleaned up — that's still success.
            try:
                runtime_crawler_name = f"agentic-data-ingestion-{pipeline_id}-source-discover"
                try:
                    jdbc_glue_client.delete_crawler(Name=runtime_crawler_name)
                    cleanup_results["runtime_crawler"] = True
                except jdbc_glue_client.exceptions.EntityNotFoundException:
                    cleanup_results["runtime_crawler"] = False
            except Exception as e:
                logger.error(f"Failed to delete runtime crawler {runtime_crawler_name}: {e}")
                cleanup_failures.append({"resource": "runtime_crawler", "error": str(e)})

            # 5j. Delete discovered tables matching the per-pipeline
            # prefix in the discovery database. Uses the prefix-based
            # glue:GetTables expression so we don't depend on persisted
            # table names.
            try:
                table_prefix = f"agentic_data_ingestion_{pipeline_id}_"
                deleted_jdbc_tables = 0
                try:
                    tables_resp = jdbc_glue_client.get_tables(
                        DatabaseName=DISCOVERY_DB,
                        Expression=f"{table_prefix}*",
                    )
                    for table in tables_resp.get("TableList", []) or []:
                        table_name = table.get("Name")
                        if not table_name:
                            continue
                        try:
                            jdbc_glue_client.delete_table(
                                DatabaseName=DISCOVERY_DB,
                                Name=table_name,
                            )
                            deleted_jdbc_tables += 1
                        except jdbc_glue_client.exceptions.EntityNotFoundException:
                            # Concurrent cleanup — fine.
                            pass
                except jdbc_glue_client.exceptions.EntityNotFoundException:
                    # Discovery database itself missing — nothing to do.
                    pass
                cleanup_results["jdbc_tables"] = deleted_jdbc_tables
            except Exception as e:
                logger.error(f"Failed to delete JDBC discovery tables: {e}")
                cleanup_failures.append({"resource": "jdbc_tables", "error": str(e)})

            # 5k. Delete the source-side Glue Connection — ONLY when
            # AgenticDataIngestion created it (select_rds mode). For glue_connection
            # mode the user supplied an existing connection that we must
            # NOT delete. Kept, too, while another pipeline still references it
            # (`_delete_owned_connection`).
            if source_owned_by_adi:
                _delete_owned_connection(
                    jdbc_glue_client,
                    item_str(pipeline_item, "source_connection_name") or f"agentic-data-ingestion-{pipeline_id}-source",
                    pipeline_id,
                    "source_connection",
                    cleanup_results,
                    cleanup_failures,
                )

        # 5l. Delete the target-side Glue Connection for a JDBC target — ONLY when
        # AgenticDataIngestion created it. `create_pipeline` provisions
        # `agentic-data-ingestion-{pipeline_id}-rds` for every JDBC / RDS_DATABASE
        # target that does not name its own `glue_connection_name`, and nothing
        # used to delete it, so every such pipeline leaked one connection.
        # Independent of the SOURCE type: an S3-upload pipeline can write to JDBC.
        #
        # Always the deterministic name, never `target_config.jdbc_connection_name`.
        # The create path writes the deterministic name there, but a direct API
        # caller can set that key itself, and deleting whatever it names could
        # remove a connection this system does not own. The deterministic name is
        # unique to this pipeline id, so it can only be ours.
        target_config_item = item_dict(pipeline_item, "target_config")
        target_type_item = str(target_config_item.get("type") or "").upper()
        if target_type_item in ("JDBC", "RDS_DATABASE") and not target_config_item.get("glue_connection_name"):
            _delete_owned_connection(
                _boto3.client("glue"),
                f"agentic-data-ingestion-{pipeline_id}-rds",
                pipeline_id,
                "target_connection",
                cleanup_results,
                cleanup_failures,
            )

        # 6. Delete the pipeline record itself
        try:
            pipelines_table.delete_item(Key={"pipeline_id": pipeline_id})
            cleanup_results["pipeline_record"] = True
        except Exception as e:
            logger.error(f"Failed to delete pipeline record: {e}")
            cleanup_failures.append({"resource": "pipeline_record", "error": str(e)})

        # 7. Return summary
        logger.info(f"Pipeline {pipeline_id} deletion complete. Results: {cleanup_results}")
        return create_response(
            200,
            {
                "success": True,
                "pipeline_id": pipeline_id,
                "message": "Pipeline permanently deleted",
                "cleanup_results": cleanup_results,
                "cleanup_failures": cleanup_failures,
                "stopped_executions": stopped_executions,
            },
        )

    except Exception as e:
        return handle_error(e, f"Error deleting pipeline {pipeline_id}")
