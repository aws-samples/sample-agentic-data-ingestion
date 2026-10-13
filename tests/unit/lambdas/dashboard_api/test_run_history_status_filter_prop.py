# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CP3 — the status filter matches the DERIVED status (p48 C5, R4.2, task 4.3).

THE PROPERTY, AND WHY IT IS A PROPERTY
--------------------------------------
Design §Correctness Properties, Property 3:

    For any row with ``pending_approval_id`` set, filtering
    ``status=WAITING_APPROVAL`` includes it and filtering ``status=RUNNING`` (its
    stored value) does not.

Stated below in both directions at once, over every stored status rather than
``RUNNING`` alone, because the one-sided form is satisfiable by an implementation
that is still wrong. A filter on the **stored** value returns nothing for
``WAITING_APPROVAL`` *and* returns the approval-pending rows under their stored
status; a test asserting only the first half passes for an implementation that
filters the stored value and happens to have no approval-pending rows in the
example.

It is a property rather than an example because the defect is **positional** —
whether the filter sits above or below the ``WAITING_APPROVAL`` derivation at
``handlers/pipelines_runs._split_derive_and_filter`` — and position is invisible in
any single example whose rows all agree about their stored and derived status. The
interesting examples are the mixed ones: a partition holding both a ``RUNNING`` row
with an approval pending and a ``RUNNING`` row without, where the two must be told
apart by a filter that can only see one of the two values.

**This is the test that must fail if the filter moves above the derivation.** That
was verified by moving it: see the mutation note in the task record.

WHAT IS GENERATED, AND WHAT IS NOT
----------------------------------
* **Generated:** how many rows, each row's ``started_at``, each row's **stored**
  status, and whether each row carries ``pending_approval_id``. The last is the
  point: it is what makes stored and derived status disagree.
* **Not generated: ``WAITING_APPROVAL`` as a stored status.** No writer stores that
  string — it exists only on the read path — so generating it would test a shape the
  table cannot hold and would blur the very distinction the property is about. A
  stored ``WAITING_APPROVAL`` row would legitimately match the filter, which is
  true and uninteresting.
* **Not generated: ``QUEUED``.** It is split into ``queued_files`` before the filter
  runs, so it is not part of the set being filtered at all — and the filter refuses
  it with a 400 for exactly that reason (p48 O2). ``test_run_history_ordering.py``
  pins that arm deterministically.
* **Not generated: phantom rows.** Every row carries ``current_step``, so the
  phantom filter removes nothing and a missing row is attributable to the status
  predicate rather than to a second filter. do-NOT 4's position is asserted in the
  ordering file.
* **Not generated: ``page_size``.** It is fixed **above** the maximum set size, so
  the bounded filter walk always exhausts the partition and no example can be
  truncated by a bound. This property is about the predicate; the walk's bounds and
  their ``partial_counts`` markers are asserted deterministically in
  ``test_run_history_ordering.py::TestStatusFilter``. Drawing a page size here would
  make an omission caused by a bound indistinguishable from an omission caused by a
  wrong predicate.

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` from ``tests/fixtures/conftest.py``
(``tests/unit/test_no_duplicate_mocking_prop.py`` forbids a local ``mock_aws()``).
``HealthCheck.function_scoped_fixture`` is suppressed for the reason the repo's other
moto property tests suppress it, and the leak it guards against is closed by
construction: **every example draws its own pipeline id from a process-local
counter**, so one example's rows are in a partition no other example reads. A *drawn*
salt can repeat during shrinking; a counter cannot.

Validates: p48 Requirements 4.2, 4.4
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

#: Stored statuses a writer actually produces on an executions row, each named at
#: ``pipelines_runs._ACCEPTED_STATUS_FILTERS``. ``WAITING_APPROVAL`` and ``QUEUED`` are
#: both absent, for the two different reasons in the module docstring.
_STORED_STATUSES = ("SUCCEEDED", "FAILED", "RUNNING", "SKIPPED", "CANCELLED", "CANCELLING")

#: Bigger than ``_MAX_SET_SIZE`` so the walk always exhausts the partition — see the
#: module docstring on why ``page_size`` is not drawn.
_MAX_SET_SIZE = 12
_PAGE_SIZE = 40

_PARTITION_SEQUENCE = itertools.count()


@pytest.fixture
def runs_tables(mock_dynamodb_tables, monkeypatch) -> dict[str, Any]:
    """Bind moto handles onto the handler's namespace.

    ``handlers/pipelines_runs.py`` does ``from .common import executions_table, …``,
    copying the references into its own namespace, so patching ``common`` alone would
    patch names the handler never reads. Both are patched. ``dynamodb`` is rebuilt
    inside the fixture because moto intercepts at the botocore layer and the
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


def _started_at(offset: int) -> str:
    """An ISO-8601 instant, distinct per ``offset`` and lexicographically ordered."""
    return (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=offset)).isoformat()


#: ``(started_at offset, stored status, approval pending)``. Offsets are unique so the
#: index order is total and a missing row cannot be blamed on a sort-key tie.
_row_sets = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=200_000),
        st.sampled_from(_STORED_STATUSES),
        st.booleans(),
    ),
    min_size=0,
    max_size=_MAX_SET_SIZE,
    unique_by=lambda row: row[0],
)


def _seed(table: Any, pipeline_id: str, rows: list[tuple[int, str, bool]]) -> list[tuple[str, str, bool]]:
    """Write the rows; return ``(run_id, stored_status, approval_pending)`` per row."""
    written: list[tuple[str, str, bool]] = []
    for index, (offset, stored_status, approval_pending) in enumerate(rows):
        run_id = f"run-{index:04d}"
        item: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "started_at": _started_at(offset),
            "status": stored_status,
            # Never a Phantom_Execution_Row — see the module docstring.
            "current_step": "run_pipeline",
            "steps_status": {"run_pipeline": stored_status.lower()},
        }
        if approval_pending:
            # The denormalised pair the approval handler writes onto the execution
            # row, and the only input the derivation reads.
            item["pending_approval_id"] = f"appr-{index:04d}"
            item["pending_approval_type"] = "code_review"
        table.put_item(Item=item)
        written.append((run_id, stored_status, approval_pending))
    return written


def _filtered_run_ids(pipeline_id: str, status: str) -> list[str]:
    response = runs_handler.get_pipeline_executions(pipeline_id, 1, _PAGE_SIZE, status=status)
    assert response["statusCode"] == 200, response
    body = json.loads(response["body"])
    assert body["pagination"]["partial_counts"] == [], (
        "the page size is larger than any generated set, so nothing may be truncated — a marker here "
        "means this property is measuring a bound rather than the predicate"
    )
    return [execution["run_id"] for execution in body["executions"]]


@settings(
    max_examples=40,
    # moto writes plus one handler call per status make each example tens of
    # milliseconds; the property asserts the rows returned, never a duration.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_row_sets)
def test_the_filter_matches_the_derived_status_and_never_the_stored_one(runs_tables, rows):
    """CP3, in both directions. **Validates: Requirements 4.2**

    Two assertions, and neither alone is the property:

    * ``status=WAITING_APPROVAL`` returns **exactly** the approval-pending rows. A
      filter above the derivation returns ``[]`` here for every non-empty example.
    * for **every** stored status, filtering on it returns exactly the rows stored
      that way **without** an approval pending. A filter above the derivation returns
      the approval-pending rows too, so this half fails independently — which is what
      makes the pair a positional test rather than two restatements.

    Iterating every stored status rather than only ``RUNNING`` costs five handler
    calls per example and buys the case where the generated set has no ``RUNNING``
    row at all: the design's literal wording would be vacuously satisfied there, and
    the property is not.
    """
    pipeline_id = f"prop-derived-{next(_PARTITION_SEQUENCE):06d}"
    written = _seed(runs_tables["executions_table"], pipeline_id, rows)

    awaiting = [run_id for run_id, _stored, pending in written if pending]
    assert sorted(_filtered_run_ids(pipeline_id, "WAITING_APPROVAL")) == sorted(awaiting), (
        "every row with pending_approval_id must match the DERIVED status — a filter applied before "
        "the derivation at _split_derive_and_filter returns nothing here"
    )

    for stored_status in _STORED_STATUSES:
        expected = [run_id for run_id, stored, pending in written if stored == stored_status and not pending]
        assert sorted(_filtered_run_ids(pipeline_id, stored_status)) == sorted(expected), (
            f"{stored_status} must match only rows whose DERIVED status is still {stored_status}; a row "
            "with an approval pending has been derived away from it"
        )


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(rows=_row_sets)
def test_the_filtered_sets_partition_the_unfiltered_page(runs_tables, rows):
    """The completeness half: filtering loses nothing and invents nothing.

    **Validates: Requirements 4.2, 4.4**

    Every derived status's filtered set is disjoint from every other's, and their
    union is exactly the unfiltered page. That is what rules out the two failure
    modes a per-status assertion cannot see on its own: a row that matches **no**
    filter (invisible under every selection, so an operator can never find it — the
    ``SKIPPED`` complaint in a new form) and a row that matches **two** (double
    counted across views).

    Ordering is asserted as well, not just membership: each filtered set must be the
    unfiltered order restricted to its members, so the filter cannot re-sort. R10.4
    forbids a second ordering authority and a filter that reordered would be one.
    """
    pipeline_id = f"prop-partition-{next(_PARTITION_SEQUENCE):06d}"
    _seed(runs_tables["executions_table"], pipeline_id, rows)

    unfiltered = runs_handler.get_pipeline_executions(pipeline_id, 1, _PAGE_SIZE)
    page = [execution["run_id"] for execution in json.loads(unfiltered["body"])["executions"]]

    seen: list[str] = []
    for status in (*_STORED_STATUSES, "WAITING_APPROVAL"):
        filtered = _filtered_run_ids(pipeline_id, status)
        assert filtered == [run_id for run_id in page if run_id in set(filtered)], (
            f"{status}'s filtered page must be the unfiltered order restricted to its members"
        )
        seen.extend(filtered)

    assert sorted(seen) == sorted(page), "the filtered sets must partition the page — no row lost, none duplicated"
    assert len(set(seen)) == len(seen), f"a row matched more than one derived status: {seen}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
