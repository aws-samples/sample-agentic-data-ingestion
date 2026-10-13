#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Detect executions rows that ``RunsByTimeIndex`` cannot see (p48 R9.3).

``RunsByTimeIndex`` is ``(pipeline_id, started_at)`` on
``agentic_data_ingestion_executions`` (``deploy-cdk/lib/data-stack.ts``). A
DynamoDB GSI indexes only items carrying **both** key attributes, so a row with
no ``started_at`` is **absent from the index entirely** — and once run history
reads through the index (p48 C1), such a row is invisible with no error, no
warning and no metric.

R9.3 exists so that the failure mode is *"a check found it"* rather than *"a run
vanished"*. This is that check. It is read-only: ``Scan`` on the base table,
``Query`` or ``Scan`` on the index, no writes of any kind.

Usage
-----
    python scripts/detect_unindexed_executions.py --profile my-aws-profile
    python scripts/detect_unindexed_executions.py --profile my-aws-profile --region us-west-2
    python scripts/detect_unindexed_executions.py --profile my-aws-profile --json

Exit codes
----------
    0  every base-table row is present in the index
    1  at least one row is invisible (the rows are listed)
    2  the table or index could not be read

**Read the count arithmetic, not just the list.** The invisible-row count is
exactly ``base-table count − index count``; both are reported so the two agree
or the discrepancy is itself a finding. Measured at 61 − 51 = 10 on the validation account
(2026-08-24); all ten were test or fixture rows, and p48 R9.1 records them.

**Do NOT read a non-empty result as a licence to filter on sparseness.** p48
do-NOT 2 and R9.4 forbid using index absence as a substitute for the phantom-row
filter in ``dashboard_api/handlers/common.py``: the two sets overlap by accident,
and a missing ``started_at`` is a defect to fix at the writer (enforced by
``tests/unit/lambdas/test_executions_started_at_invariant.py``), not a feature to
depend on.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

DEFAULT_TABLE = "agentic_data_ingestion_executions"
DEFAULT_INDEX = "RunsByTimeIndex"

# The index's sort key. A row lacking it is not indexed — that is the whole
# mechanism, so the name is a constant rather than a literal in three places.
INDEX_SORT_KEY = "started_at"


def scan_all(table: Any, **scan_kwargs: Any) -> list[dict[str, Any]]:
    """Every item from a Scan, following ``LastEvaluatedKey``.

    Paginated deliberately: a single ``Scan`` returns at most 1 MB, so a
    one-shot read would under-report the base table and manufacture false
    "invisible" rows on a table larger than one page.
    """
    items: list[dict[str, Any]] = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key


def find_unindexed_rows(
    table: Any,
    index_name: str = DEFAULT_INDEX,
) -> dict[str, Any]:
    """Rows present in the base table and absent from ``index_name``.

    Returns a report dict:

    * ``base_count``    — rows in the base table
    * ``index_count``   — rows the index can see
    * ``missing_count`` — ``base_count - index_count``
    * ``missing``       — ``[{pipeline_id, run_id, status, has_started_at}, ...]``
      for each invisible row, so the row can be identified without a second read
    * ``counts_agree``  — whether ``len(missing) == missing_count``. A False here
      means the two reads disagree about the table and the report should not be
      trusted; the most likely cause is a concurrent write, the next most likely
      an index still backfilling (p48 O5).

    Membership is computed on the ``(pipeline_id, run_id)`` key pair rather than
    on the presence of ``started_at``, because the index is the authority on what
    the index contains. An attribute check would answer a different question and
    would report clean while a backfill was still in progress (p48 O5) — the one
    case where a row carries ``started_at`` and is genuinely not yet visible.

    Note that an EMPTY ``started_at`` is not a case this has to handle: DynamoDB
    rejects an empty string for a GSI key attribute at write time with a
    ``ValidationException``, so absence of the attribute is the only reachable
    way for a row to be unindexed. Verified in
    ``tests/unit/lambdas/test_executions_index_sparseness.py``.
    """
    base_rows = scan_all(
        table, ProjectionExpression="pipeline_id, run_id, #s, started_at", ExpressionAttributeNames={"#s": "status"}
    )
    index_rows = scan_all(table, IndexName=index_name, ProjectionExpression="pipeline_id, run_id")

    indexed_keys = {(row.get("pipeline_id"), row.get("run_id")) for row in index_rows}
    missing = [
        {
            "pipeline_id": row.get("pipeline_id"),
            "run_id": row.get("run_id"),
            "status": row.get("status"),
            "has_started_at": bool(row.get(INDEX_SORT_KEY)),
        }
        for row in base_rows
        if (row.get("pipeline_id"), row.get("run_id")) not in indexed_keys
    ]

    return {
        "index_name": index_name,
        "base_count": len(base_rows),
        "index_count": len(index_rows),
        "missing_count": len(base_rows) - len(index_rows),
        "missing": missing,
        "counts_agree": len(missing) == len(base_rows) - len(index_rows),
    }


def format_report(report: dict[str, Any], table_name: str) -> str:
    lines = [
        f"table:  {table_name}",
        f"index:  {report['index_name']}",
        f"base-table rows:   {report['base_count']}",
        f"indexed rows:      {report['index_count']}",
        f"invisible rows:    {report['missing_count']}",
    ]
    if not report["counts_agree"]:
        lines.append(
            "  WARNING: the enumerated rows and the count arithmetic disagree — a concurrent "
            "write, or an index still backfilling (check IndexStatus == ACTIVE and "
            "Backfilling == false)."
        )
    if not report["missing"]:
        lines.append("\nEvery base-table row is present in the index.")
        return "\n".join(lines)
    lines.append("\nRows the index cannot see (invisible in run history):")
    for row in report["missing"]:
        note = "" if row["has_started_at"] else f"  <- no {INDEX_SORT_KEY}"
        lines.append(f"  {row['pipeline_id']}/{row['run_id']}  status={row['status']}{note}")
    lines.append(
        f"\n{len(report['missing'])} row(s) will not appear in run history. Fix the WRITER that "
        f"produced them — do not filter on index absence (p48 R9.4)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="AWS profile name (passed as a flag, never an env prefix)")
    parser.add_argument("--region", default=None, help="AWS region (defaults to the profile's)")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)

    try:
        report = find_unindexed_rows(table, index_name=args.index)
    except Exception as err:
        print(f"Failed to read {args.table}/{args.index}: {err}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report, args.table))
    return 1 if report["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
