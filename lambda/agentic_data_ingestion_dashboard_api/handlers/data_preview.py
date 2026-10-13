#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Data preview handler — read first N rows of a run's source file."""

import contextlib
import csv
import io
import json
import os
import tempfile
import time
from typing import Any

from boto3.dynamodb.conditions import Key

from .common import (
    RUNS_BY_TIME_INDEX,
    create_response,
    executions_table,
    handle_error,
    logger,
    s3_client,
    state_table,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class NotFoundError(Exception):
    """Raised when a requested resource is not found."""


class SourceNotPreviewableError(NotFoundError):
    """The run names a source, but not one this endpoint can read (p48 C12).

    A **subclass** so that :func:`get_data_preview`'s existing
    ``except NotFoundError`` catches it unchanged — same 404, same message, no
    behaviour change on the per-run route. What the subclass buys is the one
    distinction :func:`_resolve_previewable_run` needs and a message string cannot
    safely carry: "this row has no source recorded" is a fact about the **row**, so
    an older row may still have one and the walk should continue; "this source is
    not S3-backed" is a fact about the **pipeline's source type**, so every older
    row will say the same thing and walking is pure cost.

    Introduced rather than sniffing ``str(exc)``, because a message is display text
    — it gets reworded — and a control-flow decision that depends on its exact
    wording breaks silently when someone improves the wording.
    """


def _resolve_source_file(run_id: str) -> tuple[str, str]:
    """Return (s3_uri, format) for the run's source file.

    Queries the state table for original_s3_uri / json_s3_uri.
    Prefers original_s3_uri; falls back to json_s3_uri (always json format).
    """
    response = state_table.query(
        KeyConditionExpression=Key("run_id").eq(run_id),
        ProjectionExpression="original_s3_uri, json_s3_uri, #sn",
        ExpressionAttributeNames={"#sn": "step_name"},
    )
    items = response.get("Items", [])
    if not items:
        raise NotFoundError(f"Run not found: {run_id}")

    # Collect URIs from any step that has them
    original_uri = ""
    json_uri = ""
    for item in items:
        if item.get("original_s3_uri") and not original_uri:
            original_uri = item["original_s3_uri"]
        if item.get("json_s3_uri") and not json_uri:
            json_uri = item["json_s3_uri"]

    s3_uri = original_uri or json_uri
    if not s3_uri:
        raise NotFoundError(f"No source file found for run: {run_id}")

    # JDBC (and other non-S3) sources have no previewable file: the pipeline
    # records a pseudo-URI like "jdbc://mysql/appdb/customers" rather than an
    # s3:// object. Source preview only supports S3-backed sources, so signal
    # "no preview available" with a NotFoundError. The API maps this to 404,
    # which the dashboard's useDataPreview hook treats as "no data to preview"
    # (no retry, no error toast) — whereas the old path fell through to the
    # format check and returned a noisy 400 "Unsupported file format".
    #
    # p48 C12 raised this to ``SourceNotPreviewableError`` — a ``NotFoundError``
    # subclass, so every existing caller and every existing message is unchanged.
    # See that class for why the distinction cannot live in the message text.
    if not s3_uri.startswith("s3://"):
        raise SourceNotPreviewableError(f"Source preview is not available for this source type: {s3_uri}")

    # Determine format from extension
    ext = s3_uri.rsplit(".", 1)[-1].lower() if "." in s3_uri else ""
    format_map = {"csv": "csv", "json": "json", "parquet": "parquet"}
    file_format = format_map.get(ext, "")

    # If using json_s3_uri fallback, format is always json
    if not original_uri and json_uri:
        file_format = "json"

    return s3_uri, file_format


def _parse_limit(query_params: dict) -> int:
    """Validate and clamp the ``limit`` query parameter.

    Returns the effective limit (default 20, max 100).
    Raises ``ValueError`` on non-positive or non-integer values.
    """
    raw = query_params.get("limit")
    if raw is None:
        return 20

    try:
        value = int(raw)
    except (ValueError, TypeError) as err:
        raise ValueError(f"Invalid limit parameter: {raw!r} — must be a positive integer") from err

    if value <= 0:
        raise ValueError(f"Invalid limit parameter: {value} — must be a positive integer")

    return min(value, 100)


# ---------------------------------------------------------------------------
# Format-specific readers
# ---------------------------------------------------------------------------


def _read_csv_preview(bucket: str, key: str, limit: int) -> dict:
    """Read CSV rows via a bounded Range GET (first 256 KB).

    **This was two functions — an S3 Select reader and a Range-GET fallback it
    fell back to inside a bare ``except Exception``. Both the S3 Select arm and
    that silent except are gone.** Amazon S3 Select has been closed to new
    customers since 2024-07-25, so on an account created after that date the
    ``SelectObjectContent`` call returns ``MethodNotAllowed`` every time and the
    fallback served every preview. Verified against the primary dev account
    before removal. Two things made keeping it worse than removing it: the bare
    ``except`` swallowed the failure with no log line at all, so unlike
    ``schema_extract``'s equivalent arm this one left no evidence it was always
    failing; and a genuine read error — a missing object, a permission problem —
    was caught by the same ``except`` and retried as a Range GET that then failed
    for its own reason, reporting the second cause instead of the first.

    **The comma is correct here, and it is correct by a coupling worth knowing
    about.** Unlike ``schema_extract``'s CSV reader, this one does *not* ask
    :func:`shared.format_delimiters.delimiter_for_key` for the separator, because
    only a ``.csv`` extension can reach it: ``_resolve_source_file``'s
    ``format_map`` admits exactly ``csv``, ``json`` and ``parquet``, and anything
    else — ``.tsv``, ``.txt``, ``.avro``, ``.orc`` — resolves to ``""`` and is
    refused with a 400 by :func:`get_data_preview` before any reader runs. That
    narrow ``format_map`` is a **ratified, deliberately-unfixed gap**
    (``LGR-eb8f3c2-1``: "an Avro or ORC pipeline ingests fine and previews as
    empty today, which is a cosmetic gap no test covers"), so widening it is a
    decision, not a tidy-up. **If it is ever widened to admit a delimited format,
    this reader must take its delimiter from the key at the same time**, or a TSV
    preview will render one column named for the whole unsplit header — the exact
    defect p55 R4.2 removed from ``schema_extract`` (``LGR-bba16cc-2``).
    """
    resp = s3_client.get_object(Bucket=bucket, Key=key, Range="bytes=0-262143")
    chunk = resp["Body"].read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(chunk))
    rows = []
    for row in reader:
        rows.append(dict(row))
        if len(rows) >= limit:
            break
    columns = list(rows[0].keys()) if rows else []
    return {"columns": columns, "rows": rows}


def _parse_partial_json(chunk: str) -> list[dict]:
    """Parse complete JSON records from a potentially truncated chunk.

    Handles JSON arrays and JSONL/NDJSON formats.
    """
    chunk = chunk.strip()
    records: list[dict] = []

    # Try JSONL first (line-delimited)
    if chunk and not chunk.startswith("["):
        for line in chunk.split("\n"):
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if records:
            return records

    # Try JSON array
    if chunk.startswith("["):
        chunk = chunk[1:]

    # Try to parse the whole thing first (small file case)
    try:
        data = json.loads("[" + chunk.rstrip(",").rstrip() + "]")
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Fallback: find complete JSON objects by brace matching
    depth = 0
    start = None
    for i, ch in enumerate(chunk):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    records.append(json.loads(chunk[start : i + 1]))
                start = None

    return records


def _read_json_preview(bucket: str, key: str, limit: int) -> dict:
    """Read JSON rows via Range GET (first 256KB)."""
    resp = s3_client.get_object(Bucket=bucket, Key=key, Range="bytes=0-262143")
    chunk = resp["Body"].read().decode("utf-8")
    records = _parse_partial_json(chunk)
    rows = records[:limit]
    columns = list(rows[0].keys()) if rows else []
    return {"columns": columns, "rows": rows}


MAX_PREVIEW_FILE_SIZE = 500 * 1024 * 1024  # 500MB


def _read_parquet_preview(bucket: str, key: str, limit: int) -> dict:
    """Read Parquet rows via PyArrow (first row group only).

    Downloads to /tmp and reads only the first row group.
    Rejects files > 500MB to avoid filling Lambda /tmp.
    """
    # Size guard — check BEFORE importing pyarrow
    head = s3_client.head_object(Bucket=bucket, Key=key)
    file_size = head["ContentLength"]
    if file_size > MAX_PREVIEW_FILE_SIZE:
        raise ValueError(
            f"File too large for preview ({file_size / (1024 * 1024):.0f}MB). "
            f"Maximum is {MAX_PREVIEW_FILE_SIZE / (1024 * 1024):.0f}MB."
        )

    import pyarrow.parquet as pq

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        s3_client.download_file(bucket, key, tmp_path)
        pf = pq.ParquetFile(tmp_path)
        table = pf.read_row_group(0).slice(0, limit)
        col_data = table.to_pydict()
        columns = list(col_data.keys())
        row_count = len(col_data[columns[0]]) if columns else 0
        rows = [{col: col_data[col][i] for col in columns} for i in range(row_count)]
        return {"columns": columns, "rows": rows}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# S3 URI parsing
# ---------------------------------------------------------------------------


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``."""
    without_scheme = s3_uri.replace("s3://", "", 1)
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def get_data_preview(pipeline_id: str, run_id: str, query_params: dict) -> dict[str, Any]:
    """Preview source data for a specific run."""
    try:
        limit = _parse_limit(query_params)
    except ValueError as exc:
        return create_response(400, {"error": str(exc)})

    try:
        s3_uri, file_format = _resolve_source_file(run_id)
    except NotFoundError as exc:
        return create_response(404, {"error": str(exc)})

    bucket, key = _parse_s3_uri(s3_uri)

    try:
        if file_format == "csv":
            result = _read_csv_preview(bucket, key, limit)
        elif file_format == "json":
            result = _read_json_preview(bucket, key, limit)
        elif file_format == "parquet":
            result = _read_parquet_preview(bucket, key, limit)
        else:
            return create_response(
                400,
                {
                    "error": f"Unsupported file format: {file_format or 'unknown'}. "
                    "Supported formats: csv, json, parquet."
                },
            )
    except ValueError as exc:
        # Size guard or other validation errors
        return create_response(400, {"error": str(exc)})
    except Exception as exc:
        logger.warning(f"Failed to read source file {s3_uri}: {exc}")
        return create_response(404, {"error": f"Source file not accessible: {s3_uri}"})

    return create_response(
        200,
        {
            "columns": result["columns"],
            "rows": result["rows"],
            "total_rows_returned": len(result["rows"]),
            "source_file": s3_uri,
        },
    )


# ---------------------------------------------------------------------------
# Resolving "the latest run" — the bounded previewable-run walk (p48 C12)
# ---------------------------------------------------------------------------
# ``get_latest_preview`` took the top row of a ``run_id``-ordered query. Two
# things were wrong with that and **the second is the load-bearing one**
# (design §3 do-NOT 10):
#
# 1. ``run_id`` is not a time. Six id prefixes share the executions table's sort
#    key, so a descending read groups BY KIND and ``skipped-`` sorts above
#    ``run-``. Fixed by reading ``RUNS_BY_TIME_INDEX``, exactly as C1 fixed run
#    history.
# 2. **The newest row is not necessarily previewable.** A duplicate-skip row has
#    **zero** state rows — verified live on ``latesttesting-19ca826a``
#    (``skipped-7412636fccb938719673179658c05773``, state-table ``Count: 0``),
#    because ``_record_duplicate_skip`` writes only to the executions table — so
#    ``_resolve_source_file`` raises and the endpoint 404s. A ``QUEUED``
#    placeholder is the same shape, and under R1.2 it can legitimately be the
#    newest row by ``started_at``, so **fixing the ordering creates that second
#    case rather than removing it**.
#
# Ordering alone would have made this pipeline work: by ``started_at`` its top row
# is a real run and the skip is at position 2 (confirmed against the live index).
# That is luck. do-NOT 10 names the risk precisely — "ordering by time makes the
# top row *usually* right, which is the worst kind of fix — it will look correct
# in testing and fail in production" — so the walk is the half that matters and
# ``test_data_preview_latest_walk.py`` pins it with a mutation test: delete the
# walk, keep the ordering, and a test must go red.
#
# **Why it never recovers on its own** (R12.3): the duplicate-skip id is
# deterministic in the content fingerprint
# (``pipeline_trigger/lambda_function.py:189``), so the row never ages out and
# never re-sorts. One duplicate skip permanently broke this panel for that
# pipeline.

#: How many index rows the walk will examine before giving up, and — because the
#: page ``Limit`` is 1 — also the number of index Queries it can issue. The two
#: coincide **by construction**, which is why there is one number here and not the
#: two (page cap, row cap) that ``pipelines_runs.py`` carries.
#:
#: **``Limit=1`` rather than a page-sized read, deliberately.** Task 4.3's filter
#: walk reads shrinking pages because it is *collecting* rows to fill a page; this
#: walk wants exactly one row, so a page read would charge the common case for rows
#: it never looks at. And the common case is the newest row: measured over all 24
#: indexed partitions in the validation account on 2026-08-24, **none** had a skip or a queued
#: row newest by ``started_at``, and the eight spot-checked all resolved a source.
#: With ``Limit=1`` that case costs exactly what the pre-C12 read cost and only the
#: pathological pipeline pays. Note what those same measurements mean for the walk
#: itself: no live pipeline exercises it today, which is precisely do-NOT 10's point
#: — this is the shape a test would ratify the half-fix against.
#:
#: 25 rather than ``pipelines_runs.py``'s 100 because **the dominant cost here is
#: per row, not per page**: each candidate costs one keyed ``state_table`` Query
#: inside :func:`_resolve_source_file`, so the row count *is* the request-count
#: multiplier. O14 asks for a bound on "a pipeline whose last twenty events were
#: all skips"; 25 clears that with headroom and caps the walk at ~25 index reads
#: plus ~25 state reads, well inside the share below.
_MAX_PREVIEW_CANDIDATES = 25

#: This request's time budget. Same figure and same reasoning as
#: ``pipelines_runs.py:_RUNS_TIME_BUDGET_SECONDS``, ``stats.py``'s and
#: ``pipelines_crud.py``'s: the dashboard API Lambda's timeout is 25 s and is
#: recorded as load-bearing (``compute-stack.ts:770-788``), so 20 s leaves 5 s in
#: hand.
#:
#: **Defined here rather than imported from ``pipelines_runs.py``, and the split is
#: the point.** The *index name* is shared because a mistyped literal is a
#: ``ValidationException`` — a correctness fence, so one copy. A *budget* is a
#: tuning figure scoped to one read's own work, so every module in this Lambda
#: carries its own (``stats.py``, ``pipelines_crud.py``, ``schemas.py``,
#: ``pipelines_runs.py`` — four copies of 20.0 already, deliberately). It is also
#: what keeps each module's budget monkeypatchable in its own namespace, which is
#: how tasks 3.3 and 4.3 make their bounds bind without sleeping.
_PREVIEW_TIME_BUDGET_SECONDS = 20.0

#: The walk's **share** of that budget. The walk runs *ahead* of the S3 read and
#: parse it exists to feed, and that read is the expensive one — up to a 500 MB
#: Parquet download (:data:`MAX_PREVIEW_FILE_SIZE`) plus a PyArrow row-group read.
#: Without a carve-out a long walk could spend the whole window and leave the
#: request timing out with no body at all, which is strictly worse than a 404 that
#: says "nothing previewable".
#:
#: 8.0 of 20 — ``pipelines_runs.py:_FILTER_WALK_TIME_SHARE_SECONDS`` and
#: ``pipelines_crud.py:_PAGE_SCAN_TIME_SHARE_SECONDS`` exactly, the two in-tree
#: precedents for this same ordering problem — leaving at least 12 s for the read
#: behind it. A test that needs this to bind shortens it rather than asserting a
#: wall-clock duration.
_PREVIEW_WALK_TIME_SHARE_SECONDS = 8.0

#: Returned when the partition holds rows but none of them can be previewed. **The
#: existing 404, not a new status code and not a new body shape** (O14): the only
#: thing that differs from the pre-C12 404s is the sentence, because the reason
#: differs. See :func:`get_latest_preview` for what task 5.7 needs to know about it.
_NO_PREVIEWABLE_RUN_MESSAGE = "No run with previewable source data found for pipeline: {pipeline_id}"


def _preview_walk_deadline(started_at: float) -> float:
    """The monotonic instant the previewable-run walk must stop by (p48 O14).

    Anchored at the top of the walk and clamped so a share larger than the whole
    budget cannot push the walk's deadline past the request's own — the same
    guard, for the same reason, as ``pipelines_runs.py:_filter_walk_deadline``.

    ``time.monotonic`` and not wall clock: a clock adjustment mid-request must not
    make the budget appear to run backwards.
    """
    return started_at + max(0.0, min(_PREVIEW_WALK_TIME_SHARE_SECONDS, _PREVIEW_TIME_BUDGET_SECONDS))


def _run_names_a_source(run_id: str) -> bool:
    """Whether this run is worth handing to :func:`get_data_preview` (p48 R12.2).

    **The predicate is capability, not status, and that choice is the substance of
    C12.** It asks the resolver the question the endpoint will actually ask —
    "can a source be resolved for this run?" — so the walk and the reader cannot
    disagree about what "previewable" means. There is no second definition to keep
    in sync.

    *Rejected: a status denylist* (skip rows whose ``status`` is ``SKIPPED`` or
    ``QUEUED``). Cheaper — no read per candidate — and wrong in both directions.
    It **misses** unpreviewable rows that carry neither status: task 1.4 found rows
    written by unguarded upserts, and R9.1 measured ten rows with no ``started_at``
    at all. And it can **exclude a previewable row**, because "``SKIPPED`` implies
    no state rows" is true only because of *which writer* produces skip records —
    an incidental overlap of exactly the kind do-NOT 2 / R9.4 forbid leaning on.
    A status vocabulary is also the wrong shape for this file: ``WAITING_APPROVAL``
    has no writer at all and ``LGR-5aad762-2`` argues in this same Lambda that "a
    live table can hold shapes no current producer writes".

    *Rejected: folding readability into the predicate* — treating an unsupported
    extension or an unreadable S3 object as "not previewable" and walking on. Those
    are properties of the **object**, and they can be transient; a temporarily
    unreadable newest run would then silently show an older run's data instead of
    saying so. R12.3's distinction between a permanent and a transient failure cuts
    the other way here: the permanent case is what the walk is for, and a transient
    one must surface as itself. So the predicate stops at resolution and
    :func:`get_data_preview` remains the sole author of 200 / 400 / 404.

    Returns ``True`` for a run whose source is recorded but **not** S3-backed
    (:class:`SourceNotPreviewableError`). That is not a previewable run, but it is a
    terminal answer: the source type belongs to the pipeline, so every older row
    says the same thing and walking past it would spend the whole bound to return
    the same 404 — while losing the specific "not available for this source type"
    message added for JDBC sources in ``5feb9b7647``. The caller therefore stops
    and lets ``get_data_preview`` produce that 404 verbatim.
    """
    try:
        _resolve_source_file(run_id)
    except SourceNotPreviewableError:
        return True
    except NotFoundError:
        # No state rows at all (the skip / queued placeholder case), or state rows
        # carrying no source URI (a run that failed before file_validation
        # recorded one). Both are facts about this row, so an older one may still
        # be previewable — keep walking.
        return False
    return True


def _resolve_previewable_run(pipeline_id: str) -> tuple[str | None, int]:
    """The newest run in this pipeline that :func:`get_data_preview` can answer for.

    Reads ``RUNS_BY_TIME_INDEX`` newest-first and returns the first row
    :func:`_run_names_a_source` accepts, together with **how many rows were
    examined** — which is what lets the caller tell "this pipeline has no indexed
    runs" from "it has runs, none previewable" without a second query.

    Bounded three ways, in the shape ``_count_partition_rows`` and the task-4.3
    filter walk already use: a hard iteration cap (:data:`_MAX_PREVIEW_CANDIDATES`,
    expressed as ``range`` so the loop cannot spin however the service behaves), a
    wall-clock deadline (:func:`_preview_walk_deadline`), and the partition running
    out. The deadline is checked **after** establishing there is more to read, so
    the first row is always examined and the bound applies to *continuing* — the
    same property ``stats.py`` and ``pipelines_runs.py`` hold.

    A bound that binds is **logged and not reported in the body**: O14 says to
    return the existing 404, and threading a ``partial_counts``-style marker into a
    404 error body would be the new error shape it forbids. The log line is what
    makes the case detectable after the fact.

    ``ProjectionExpression="run_id"`` because that is the only attribute used;
    ``LastEvaluatedKey`` still comes back complete, so paging is unaffected. (The
    pairing real DynamoDB rejects is ``Select="COUNT"`` with a projection —
    ``LGR-9a35b2d-3`` — which is not this query.)

    **No ``run_cursor`` codec here, for task 5.2's two reasons.** The key never
    leaves this process, so there is no client-supplied value for the allowlist and
    the pipeline binding to defend against; and ``decode_cursor`` requires a
    ``page`` of at least ``_FIRST_CURSOR_PAGE``, which this read has no notion of.

    A pipeline whose rows carry no ``started_at`` is absent from a sparse index and
    so returns ``(None, 0)`` — the cost R9 accepts, measured at ten rows in
    the validation account, all test or fixture rows. There is deliberately **no fallback to a
    base-table read**: that would reintroduce the ``run_id`` ordering this spec
    removes, which is the same refusal ``get_run_neighbours`` records for the same
    reason.
    """
    query_kwargs: dict[str, Any] = {
        "IndexName": RUNS_BY_TIME_INDEX,
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        # Newest first by ``started_at`` — the index's sort key, NOT the table's
        # ``run_id``. That substitution is change (1) of C12.
        "ScanIndexForward": False,
        "ProjectionExpression": "run_id",
        # One row per read: see :data:`_MAX_PREVIEW_CANDIDATES`.
        "Limit": 1,
    }
    deadline = _preview_walk_deadline(time.monotonic())
    examined = 0

    for _candidate in range(_MAX_PREVIEW_CANDIDATES):
        response = executions_table.query(**query_kwargs)
        for item in response.get("Items", []):
            examined += 1
            run_id = item.get("run_id")
            if isinstance(run_id, str) and run_id and _run_names_a_source(run_id):
                return run_id, examined
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return None, examined
        if time.monotonic() >= deadline:
            logger.warning(
                "Previewable-run walk hit its time budget for %s after %d rows; "
                "returning 404 rather than a partial answer",
                pipeline_id,
                examined,
            )
            return None, examined
        query_kwargs["ExclusiveStartKey"] = last_key

    logger.warning(
        "Previewable-run walk hit its %d-row cap for %s; returning 404 rather than a partial answer",
        _MAX_PREVIEW_CANDIDATES,
        pipeline_id,
    )
    return None, examined


def get_latest_preview(pipeline_id: str, query_params: dict) -> dict[str, Any]:
    """Preview source data for the pipeline's most recent **previewable** run.

    Two 404s, both the pre-existing status and body shape (O14), distinguished only
    by their sentence because their causes differ:

    * ``No runs found for pipeline: …`` — the index holds nothing for this
      pipeline. The message is unchanged from before C12.
    * :data:`_NO_PREVIEWABLE_RUN_MESSAGE` — rows exist and none of them names a
      source. New sentence, same shape.

    A third 404 still comes from :func:`get_data_preview` itself, for the run this
    function selected: an S3 object that is not readable, or the "not available for
    this source type" case.

    **What task 5.7 needs to know.** All of these are ``404`` with a
    ``{"error": <sentence>}`` body, so they are **not** distinguishable by status or
    by shape — only by wording. That is deliberate and 5.7 should not try to tell
    them apart: every one of them means "there is nothing to preview here", which is
    exactly the reading ``data_preview.py:63-66`` already states in words and
    ``use-data-preview.ts:26-31`` already implements by suppressing retry and the
    toast. So 5.7 should branch on the status alone and, if it wants to give the
    reason, render the ``error`` sentence verbatim — which also satisfies R12.5's
    "in words, not by colour" without any new contract.
    """
    try:
        run_id, examined = _resolve_previewable_run(pipeline_id)
        if run_id is None:
            if examined == 0:
                return create_response(404, {"error": f"No runs found for pipeline: {pipeline_id}"})
            return create_response(404, {"error": _NO_PREVIEWABLE_RUN_MESSAGE.format(pipeline_id=pipeline_id)})
        return get_data_preview(pipeline_id, run_id, query_params)
    except Exception as exc:
        return handle_error(exc, "Failed to fetch latest preview")
