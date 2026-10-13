# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CP2 — walking every page neither duplicates nor omits a row (p48 C2, task 3.2).

THE PROPERTY, AND WHY IT IS A PROPERTY
--------------------------------------
Design §Correctness Properties, Property 2:

    For any set and any ``page_size``, the concatenation of all pages is a
    permutation of the filtered set.

Stated here in the stronger form the index makes available: the concatenation is
not merely a permutation but the **ordered** set, ``started_at`` descending. The
order is total in the generated data (every row draws a distinct timestamp), so a
page boundary has nothing to be ambiguous about — which is exactly what makes a
duplicate or an omission attributable to the cursor rather than to a tie.

It is a property rather than an example because the failure is a **boundary**
failure and boundaries are a function of ``page_size`` against set size. The
single most likely defect — echoing back the previous page's key, or the key of
the wrong row within the page — is invisible at one page size and visible at
another, and the interesting cases (set size one less than, equal to, and one more
than a multiple of the page size) are the ones a hand-written example set forgets.
Design §Testing Strategy names this as *"the property that catches a cursor echoed
back wrongly"*.

WHAT IS GENERATED, AND WHAT IS NOT
----------------------------------
* **Generated:** how many rows, each row's ``started_at``, each row's ``run_id``
  **prefix**, and the ``page_size`` the walk uses. The prefix mix is the point of
  the spec: ``queued-`` < ``run-`` < ``retry-`` < ``skipped-`` lexicographically,
  so under the old ``run_id`` sort key the id prefix *was* the ordering. Drawing it
  independently of the timestamp means any example where prefix order and time
  order disagree is a test of C1 as well as of C2.
* **Not generated: ``QUEUED``.** A ``QUEUED`` row is split into ``queued_files``
  (``pipelines_runs.py:296-307``), a different array, so it is not part of the set
  being paged and including it would make "the concatenation is the set" a
  statement about two arrays at once. R1.2's ordering and R3.3's split are asserted
  deterministically in ``test_run_history_ordering.py`` instead.
* **Not generated: phantom rows.** Every row carries ``current_step``, so none is a
  Phantom_Execution_Row and the filter at ``:345`` removes nothing. That is
  deliberate: this property is about the paging mechanism, and a filter that
  silently shortened a page would make a real omission indistinguishable from an
  intended one. ``do-NOT 4``'s filter position is asserted in the ordering file.

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` from ``tests/fixtures/conftest.py``
(``tests/unit/test_no_duplicate_mocking_prop.py`` forbids a local ``mock_aws()``).
``HealthCheck.function_scoped_fixture`` is suppressed for the reason the repo's
other moto property tests suppress it, and the leak it guards against is closed
here by construction: **every example draws its own pipeline id**, so one example's
rows are in a partition no other example reads.

Validates: p48 Requirements 1.3, 2.1
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path and import-time env — must precede importing the handler, because
# ``handlers/common.py`` calls ``require_env`` at import time.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import common as common_handler
from handlers import pipelines_runs as runs_handler

EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
STATE_TABLE = "agentic_data_ingestion_state"
MOTO_REGION = "us-east-1"  # matches conftest._create_moto_dynamodb_tables

#: The six prefixes observed table-wide (design §Data Models), minus ``queued-``'s
#: status. ``retry-`` sorts above ``run-`` and ``skipped-`` above both, so a set
#: mixing these has an id order that disagrees with its time order in most examples.
_ID_PREFIXES = ("run-", "skipped-", "retry-", "file-", "e2e-", "unknown-")

#: Any status except ``QUEUED`` — see the module docstring for why that one is out.
_STATUSES = ("SUCCEEDED", "FAILED", "RUNNING", "SKIPPED", "CANCELLED")


@pytest.fixture
def runs_tables(mock_dynamodb_tables, monkeypatch) -> dict[str, Any]:
    """Bind moto handles onto the handler's namespace.

    ``handlers/pipelines_runs.py`` does ``from .common import executions_table, …``,
    copying the references into its own namespace, so patching ``common`` alone
    would patch names the handler never reads. Both are patched. ``dynamodb`` is
    rebuilt inside the fixture because moto intercepts at the botocore layer and the
    module-level resource was created before ``mock_aws()`` activated.
    """
    handles = {
        "executions_table": mock_dynamodb_tables[EXECUTIONS_TABLE],
        "state_table": mock_dynamodb_tables[STATE_TABLE],
    }
    for attribute, table in handles.items():
        monkeypatch.setattr(runs_handler, attribute, table)
        monkeypatch.setattr(common_handler, attribute, table)
    moto_dynamodb = boto3.resource("dynamodb", region_name=MOTO_REGION)
    monkeypatch.setattr(runs_handler, "dynamodb", moto_dynamodb)
    monkeypatch.setattr(common_handler, "dynamodb", moto_dynamodb)
    return handles


#: Partition ids come from a process-local counter, NOT from a drawn value.
#: ``mock_dynamodb_tables`` is function-scoped, so all examples of one test share one
#: moto table — and a *drawn* salt can repeat between examples during shrinking,
#: which would let a previous example's rows appear in this one's partition and fail
#: the property for a reason that is not the property. A counter cannot repeat.
_PARTITION_SEQUENCE = itertools.count()


def _started_at(offset: int) -> str:
    """An ISO-8601 instant, distinct per ``offset`` and lexicographically ordered.

    ``started_at`` is a **string** sort key, so the index compares these
    lexicographically. Fixed-width zero-padded output is what makes lexicographic
    and chronological order agree, which is the property the real writers get from
    ``datetime.isoformat()`` and the reason this uses it rather than hand-formatting.
    """
    return (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=offset)).isoformat()


#: A set of rows with pairwise-distinct ``started_at`` values. ``unique=True`` on the
#: offsets is load-bearing: CP1 asserts determinism across reads rather than a
#: tie-break rule for equal sort keys (DynamoDB documents none), so a generated tie
#: would make this property's expected order undefined rather than test anything.
_row_sets = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=200_000),
        st.sampled_from(_ID_PREFIXES),
        st.sampled_from(_STATUSES),
    ),
    min_size=0,
    max_size=13,
    unique_by=lambda row: row[0],
)


def _seed(table: Any, pipeline_id: str, rows: list[tuple[int, str, str]]) -> list[str]:
    """Write the rows and return their run ids in ``started_at``-descending order."""
    written: list[tuple[str, str]] = []
    for index, (offset, prefix, status) in enumerate(rows):
        run_id = f"{prefix}{index:04d}"
        started_at = _started_at(offset)
        table.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "run_id": run_id,
                "started_at": started_at,
                "status": status,
                # Never a Phantom_Execution_Row — see the module docstring.
                "current_step": "run_pipeline",
                "steps_status": {"run_pipeline": status.lower()},
            }
        )
        written.append((started_at, run_id))
    return [run_id for _, run_id in sorted(written, reverse=True)]


def _walk(pipeline_id: str, page_size: int, max_pages: int) -> tuple[list[str], int]:
    """Follow ``next_cursor`` to exhaustion; return the run ids and the page count.

    ``max_pages`` is a termination bound, not a paging bound: a cursor that never
    goes ``None`` is itself a defect, and without a bound it would hang the suite
    instead of failing it.
    """
    walked: list[str] = []
    cursor: str | None = None
    pages = 0
    while pages <= max_pages:
        response = runs_handler.get_pipeline_executions(pipeline_id, 1, page_size, cursor=cursor)
        assert response["statusCode"] == 200, response
        body = json.loads(response["body"])
        pages += 1
        walked.extend(execution["run_id"] for execution in body["executions"])
        cursor = body["pagination"]["next_cursor"]
        if cursor is None:
            assert body["pagination"]["has_next"] is False
            return walked, pages
        assert body["pagination"]["has_next"] is True
    raise AssertionError(f"the cursor walk did not terminate within {max_pages} pages")


@settings(
    max_examples=40,
    # moto writes and a handler call per page make each example tens of
    # milliseconds; the property asserts the rows returned, never a duration.
    deadline=None,
    # The moto fixture is function-scoped. Correct rather than merely tolerated
    # here: every example draws its own pipeline id below, so the partition one
    # example writes is one no other example reads, and a shared table leaks
    # nothing between them.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_row_sets, page_size=st.integers(min_value=1, max_value=6))
def test_walking_every_page_yields_the_ordered_set_exactly_once(runs_tables, rows, page_size):
    """CP2. **Validates: Requirements 1.3, 2.1**

    Three assertions, and each catches a different defect:

    * **equality with the expected order** catches an omission, a duplicate, and a
      mis-ordered page in one comparison;
    * **the explicit duplicate check** is redundant with the first but names the
      failure, because ``[a, a, b] != [a, b]`` reports a length difference and not
      "row ``a`` came back twice"; and
    * **the page count** catches a walk that terminated early — the one way to
      satisfy "no duplicates" while omitting everything, which the first assertion
      would also catch but only as a mysterious short list.
    """
    pipeline_id = f"prop-order-{next(_PARTITION_SEQUENCE):06d}"
    expected = _seed(runs_tables["executions_table"], pipeline_id, rows)

    walked, pages = _walk(pipeline_id, page_size, max_pages=len(rows) + 2)

    assert walked == expected, f"page_size={page_size} over {len(rows)} rows walked {walked}, expected {expected}"
    assert len(set(walked)) == len(walked), f"a row was returned more than once: {walked}"
    assert pages >= max(1, -(-len(rows) // page_size)), "the walk stopped before the set was exhausted"


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(rows=_row_sets, page_size=st.integers(min_value=1, max_value=6))
def test_no_page_exceeds_the_requested_size(runs_tables, rows, page_size):
    """CP6's half that keyed paging must not break. **Validates: Requirements 2.4**

    ``Limit=page_size`` stays on the query, so no page may return more rows than
    were asked for — which is what keeps the enrichment key count a function of page
    size alone (4 keys per run, one ``batch_get_item`` for a default page). A page
    may legitimately return *fewer*: ``Limit`` is applied before the ``QUEUED``
    split and the phantom filter (R2.4), and the last page is short by definition.
    Design is explicit that under the index this is a display imprecision on a
    correctly-ordered page, and that C3's exact count is what keeps the pager honest
    about it — so "fewer" is deliberately not asserted against here. Over-reading to
    fill a short page is the thing this test forbids.
    """
    pipeline_id = f"prop-limit-{next(_PARTITION_SEQUENCE):06d}"
    _seed(runs_tables["executions_table"], pipeline_id, rows)

    cursor: str | None = None
    for _ in range(len(rows) + 2):
        body = json.loads(runs_handler.get_pipeline_executions(pipeline_id, 1, page_size, cursor=cursor)["body"])
        assert len(body["executions"]) <= page_size
        cursor = body["pagination"]["next_cursor"]
        if cursor is None:
            return
    raise AssertionError("the cursor walk did not terminate")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
