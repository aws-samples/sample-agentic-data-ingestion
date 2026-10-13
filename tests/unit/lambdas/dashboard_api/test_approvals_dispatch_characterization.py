# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: `get_approvals`' dispatch (`handlers/approvals.py:278`).

p51-overview-count-scope task 1.3. This file pins today's **observable
behaviour** of `get_approvals(query_params)` *before* task 2.1 adds a
`?pipeline_id=` parameter to it. Four things, and no more:

1. **`?status=<s>` queries `status-created-index` for that status** — asserted on
   the `IndexName` and the key condition, not on the returned rows, because a
   Query against the wrong index can return the right rows in a stub and the
   wrong rows in production. Two vocabularies exist here (approvals lowercase,
   executions upper), so a copy-pasted `"PENDING"` would match nothing *without
   erroring* — the key-condition assertion is what catches that.
2. **No parameter enumerates `_APPROVAL_STATUSES` with one Query each** — the
   closed lifecycle set, in place of a table Scan.
3. **Both paths normalise `pipeline_id` → `pipeline_key`.**
4. **Both paths sort `created_at` descending**, and return a bare array.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From the spec documents, never from running the handler and recording its output
— that failure mode is `LGR-28b4d75-5`'s, where the producer's output is its own
oracle. Specifically:

- `requirements.md` §"The mechanism that makes the approval half exactly fixable":
  "`approvals.py:33` sets `_STATUS_INDEX = "status-created-index"`, partitioned by
  `status` … `get_approvals()` (`:278`) accepts only `?status=`."
- `design.md` §2: `get_approvals` "already branches on a query parameter,
  normalises `pipeline_id` → `pipeline_key`, sorts by `created_at` descending, and
  returns a bare array."
- `design.md` §Data Models: `_APPROVAL_STATUSES` is lowercase, and the case
  asymmetry with execution statuses is called out as the copy-paste hazard.
- `tasks.md` 2.1: "Response shape unchanged — a `created_at`-descending array —
  so the three existing contract suites keep applying."
- `findings.md` §2, measured live: a partition-only read on
  `pipeline-status-index` comes back ordered by the **range key `status`**, not by
  `created_at`. So the `created_at`-descending sort pinned below is *load-bearing*
  on the path 2.1 adds, not incidental to it.

WHY THIS OVERLAPS `test_query_not_scan.py` ON POINTS 1 AND 2, DELIBERATELY
-------------------------------------------------------------------------
`test_query_not_scan.py::TestApprovalsQueryNotScan` already asserts the filtered
and unfiltered Query shapes, as part of P28 R17.4 ("Query, never Scan"). That
file answers "does this path ever Scan"; this one answers "what is the whole
observable behaviour of the dispatch, immediately before a parameter is added to
it". The overlap is two cheap assertions and it is not redundancy to be tidied
away: de-duplicating by deleting them from here would leave the characterization
incomplete, and deleting them from there would weaken an R17.4 claim.

WHY THE TABLE IS A `MagicMock` AND NOT THE SHARED MOTO FIXTURE
-------------------------------------------------------------
Points 1 and 2 are claims about the **shape of the call**, which a real backend
executes and then discards. Patching the module-global `approvals_table` resource
is the established way to observe it in this directory
(`test_query_not_scan.py`, `test_metrics_rows_loaded_char.py`). No
`moto.mock_aws()` is created here and no boto3 client is replaced, so
`tests/unit/test_no_duplicate_mocking_prop.py` is satisfied — the shared moto
fixtures are not bypassed, they are simply not what answers this question.
(`tests/fixtures/conftest.py:96` does carry `pipeline-status-index` in moto, which
is what task 2.1's P1/P4 will want for the *rows*.)

The shared `mock_lambda_env` fixture *is* used, and is what makes the module
importable at all — see the `approvals_module` fixture. `AWS_REGION` is never
chosen here: it is read back from the unit layer, which pins it by assignment in
`tests/unit/conftest.py` (`LGR-3e45e65-1`, **template / ratified**).

GOVERNING RECORDS, AND WHY NONE OF THEM IS TOUCHED
--------------------------------------------------
Carried from `findings.md` §"Governing records" rather than re-derived:
`LGR-c58f276-3` (PROPOSED invariant — `extract_request_context` returns exactly
`{"actor", "ip_address"}`), `LGR-c58f276-2` and `LGR-e59edd7-3` (the audit actor
order and the arm-specific `approved_by_email` / `rejected_by_email` writes), and
`LGR-7d19eca-3` (the four approval-path executions writers guarded with
`attribute_exists(run_id)`). All four live in the **approve and reject** branches
of `approvals.py`. This file reads the listing path only and asserts nothing about
any of them.

**Expected result: green on the first run, by definition.** A red here means the
read behind the test was misdescribed and is a finding to report, not a test to
adjust — and this task adds tests only, so `approvals.py` is not modified either
way.

_Requirements: R3.3 — Design: C1, §Risks_
"""

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
# `conftest.py` in this directory already puts `shared/` and the dashboard API
# Lambda package on `sys.path` at collection start; repeated here for the reason
# that conftest gives — under `--dist=loadfile` any file may be the first
# imported on a worker.
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


#: The unit layer's region, read from the layer that owns the decision rather
#: than spelled again here. `tests/unit/conftest.py` assigns `AWS_REGION` and
#: `AWS_DEFAULT_REGION` for the whole unit layer (`LGR-3e45e65-1`) and runs before
#: this module is imported, so this is that value. A `KeyError` here is a loud
#: signal the layer pin has gone away — the failure that record exists to
#: prevent — not something to paper over with a second default.
_UNIT_LAYER_REGION = os.environ["AWS_REGION"]

#: The deployed index name, spelled here **once** as the expectation.
#: Taken from `requirements.md`, which quotes `approvals.py:33`, rather than
#: imported from the module under test — a test that reads its expectation out of
#: the code under test cannot detect that code drifting (`LGR-28b4d75-5`).
_EXPECTED_STATUS_INDEX = "status-created-index"

#: The closed lifecycle set, from `design.md` §Data Models. **Lowercase**, and
#: that is the point: the executions status filter takes `"RUNNING"` while the
#: approvals key condition takes `"pending"`, so a copy-pasted upper-case value
#: returns zero rows with no error.
_EXPECTED_APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")


@pytest.fixture
def approvals_module(mock_lambda_env, monkeypatch):
    """`handlers.approvals`, imported with the env its import requires.

    `mock_lambda_env` is the shared fixture from `tests/fixtures/conftest.py`. It
    is not optional: `handlers/common.py:35-44` calls `require_env` for nine table
    names at **import** time, and `require_env` only falls back to its defaults
    when `AGENTIC_DATA_INGESTION_ENV=local`, so without it the import raises
    `OSError`. This is also why the import is inside a fixture rather than at
    module scope — binding the module at collection would decide that env for
    every test in this directory.

    The two region variables are then re-pinned to the unit layer's value.
    `mock_lambda_env` sets `us-east-1` for its own callers, but
    `handlers/common.py:45-46` freezes `AWS_REGION` into module-global boto3
    resources at import, and whichever test imports the module first would decide
    that region for the whole session — the collection-order dependence
    `LGR-3e45e65-1` removed from this layer. Nothing in this file talks to a
    backend, so the region does not affect its own assertions; the re-pin is for
    the files that share the imported module.
    """
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    from handlers import approvals as approvals_mod

    return approvals_mod


# ---------------------------------------------------------------------------
# Condition-expression introspection
# ---------------------------------------------------------------------------
def _leaves(condition: Any) -> list[tuple[str, str, tuple]]:
    """Flatten a boto3 condition tree into `(operator, attribute, operands)` leaves.

    `Key("status").eq("pending")` → `[("=", "status", ("pending",))]`.

    A local copy, as in `test_metrics_rows_loaded_char.py:_leaves`, which carries
    the same reasoning: `test_query_not_scan.py` owns the other copy and must keep
    passing unamended, and there is no shared home for this helper. Two copies
    already exist; this is the third rather than a refactor of files this task is
    not authorised to touch.
    """
    expr = condition.get_expression()
    op = expr["operator"]
    values = expr["values"]
    if op in ("AND", "OR"):
        out: list[tuple[str, str, tuple]] = []
        for sub in values:
            out.extend(_leaves(sub))
        return out
    name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
    return [(op, name, tuple(values[1:]))]


def _query_calls(table: MagicMock) -> list[dict[str, Any]]:
    """kwargs of every `table.query` call, in call order."""
    return [c.kwargs for c in table.query.call_args_list]


def _row(approval_id: str, status: str, **extra: Any) -> dict[str, Any]:
    """One approvals row, carrying only what this file's assertions read."""
    row: dict[str, Any] = {"approval_id": approval_id, "status": status}
    row.update(extra)
    return row


def _call(approvals_module: Any, query_params: dict[str, str], pages: list[dict[str, Any]]):
    """Run `get_approvals(query_params)` against a stubbed approvals table.

    `pages` is the sequence of `table.query` responses, one per expected call —
    `_query_all` (`approvals.py:186`) keeps calling until a response omits
    `LastEvaluatedKey`, so a page without one ends that partition's walk.

    Returns `(response, body, mock_table)`.
    """
    mock_approvals = MagicMock()
    mock_approvals.query.side_effect = list(pages)
    with patch.object(approvals_module, "approvals_table", mock_approvals):
        response = approvals_module.get_approvals(query_params)
    assert response["statusCode"] == 200, response
    # Never a Scan on either path — the read is bounded to a status partition.
    mock_approvals.scan.assert_not_called()
    return response, json.loads(response["body"]), mock_approvals


# ═══════════════════════════════════════════════════════════════════════════
# 1 + 2 — which index, and which partitions
# ═══════════════════════════════════════════════════════════════════════════
class TestDispatchTargetsTheStatusIndex:
    """Both of today's paths read `status-created-index`, keyed on `status`."""

    @pytest.mark.parametrize("status", _EXPECTED_APPROVAL_STATUSES)
    def test_status_parameter_queries_the_status_index_for_that_status(self, approvals_module, status):
        """`?status=<s>`: exactly one partition Query, `IndexName` and key condition asserted.

        The key condition is asserted to be **only** the `status` equality — no
        second key part and no `FilterExpression` — because that is what makes the
        read exact for the requested status rather than a filtered walk.
        """
        _, body, table = _call(approvals_module, {"status": status}, [{"Items": []}])

        calls = _query_calls(table)
        assert len(calls) == 1, f"expected one Query for one status, got {len(calls)}"
        assert calls[0]["IndexName"] == _EXPECTED_STATUS_INDEX
        assert _leaves(calls[0]["KeyConditionExpression"]) == [("=", "status", (status,))]
        assert "FilterExpression" not in calls[0]
        assert body == []

    def test_no_parameter_enumerates_the_closed_status_set_one_query_each(self, approvals_module):
        """No parameter: one Query per lifecycle status, on the same index.

        Pins the vocabulary as well as the count. `design.md` §Data Models makes
        the lowercase spelling load-bearing, so an upper-case member here would be
        a silent zero-row read rather than an error.
        """
        pages = [{"Items": []} for _ in _EXPECTED_APPROVAL_STATUSES]
        _, body, table = _call(approvals_module, {}, pages)

        calls = _query_calls(table)
        assert len(calls) == len(_EXPECTED_APPROVAL_STATUSES)
        assert {c["IndexName"] for c in calls} == {_EXPECTED_STATUS_INDEX}

        queried = [_leaves(c["KeyConditionExpression"]) for c in calls]
        assert queried == [[("=", "status", (status,))] for status in _EXPECTED_APPROVAL_STATUSES]
        assert body == []

    def test_the_status_partition_walk_follows_last_evaluated_key(self, approvals_module):
        """A partition larger than one page is walked to the end, not truncated.

        `_query_all`'s pagination is what makes "all approvals with this status" a
        complete answer rather than a first page; a truncated read would be an
        unmarked floor, which is the shape `LGR-2bcdd0d-2` forbids.
        """
        pages = [
            {"Items": [_row("a-1", "pending")], "LastEvaluatedKey": {"approval_id": "a-1"}},
            {"Items": [_row("a-2", "pending")]},
        ]
        _, body, table = _call(approvals_module, {"status": "pending"}, pages)

        calls = _query_calls(table)
        assert len(calls) == 2
        assert calls[1]["ExclusiveStartKey"] == {"approval_id": "a-1"}
        assert {row["approval_id"] for row in body} == {"a-1", "a-2"}


# ═══════════════════════════════════════════════════════════════════════════
# 3 + 4 — the response shape both paths produce
# ═══════════════════════════════════════════════════════════════════════════
class TestResponseShape:
    """`pipeline_id` → `pipeline_key`, `created_at` descending, bare array."""

    #: The two of today's dispatch paths, as `(query_params, page_count)`. The
    #: unfiltered path issues one Query per status, so it needs four pages.
    _PATHS = [
        pytest.param({"status": "pending"}, 1, id="status-filtered"),
        pytest.param({}, len(_EXPECTED_APPROVAL_STATUSES), id="unfiltered"),
    ]

    @pytest.mark.parametrize(("query_params", "page_count"), _PATHS)
    def test_pipeline_id_is_normalised_to_pipeline_key(self, approvals_module, query_params, page_count):
        """Every returned row carries `pipeline_key` equal to its `pipeline_id`.

        `pipeline_id` is retained beside it — the normalisation adds the frontend's
        name, it does not rename the stored one.
        """
        rows = [_row("a-1", "pending", pipeline_id="pipe-1", created_at="2026-08-22T12:00:00Z")]
        pages = [{"Items": rows}] + [{"Items": []}] * (page_count - 1)
        _, body, _ = _call(approvals_module, query_params, pages)

        assert len(body) == 1
        assert body[0]["pipeline_key"] == "pipe-1"
        assert body[0]["pipeline_id"] == "pipe-1"

    @pytest.mark.parametrize(("query_params", "page_count"), _PATHS)
    def test_rows_are_returned_as_a_bare_array_sorted_created_at_descending(
        self, approvals_module, query_params, page_count
    ):
        """Newest first, and the body is the array itself — no envelope.

        Seeded out of order so the assertion is about the sort and not about the
        order the stub happened to hand over. On the unfiltered path the rows also
        arrive across four separate Queries, so this pins that the sort is applied
        to the *union* rather than per partition.
        """
        newest = _row("a-new", "pending", pipeline_id="pipe-1", created_at="2026-08-22T12:46:23Z")
        middle = _row("a-mid", "approved", pipeline_id="pipe-1", created_at="2026-08-22T12:44:35Z")
        oldest = _row("a-old", "rejected", pipeline_id="pipe-1", created_at="2026-08-20T09:00:00Z")

        if page_count == 1:
            pages: list[dict[str, Any]] = [{"Items": [middle, oldest, newest]}]
        else:
            # One row per status partition, in the handler's enumeration order:
            # pending, approved, rejected, expired.
            pages = [{"Items": [newest]}, {"Items": [middle]}, {"Items": [oldest]}, {"Items": []}]

        response, body, _ = _call(approvals_module, query_params, pages)

        assert isinstance(body, list), "the response body is the array itself, not an envelope"
        assert [row["approval_id"] for row in body] == ["a-new", "a-mid", "a-old"]
        assert response["headers"]["Content-Type"] == "application/json"


# ═══════════════════════════════════════════════════════════════════════════
# Incidental behaviour — pinned so a change to it is visible
# ═══════════════════════════════════════════════════════════════════════════
class TestIncidentalBehaviourTheSpecDoesNotSpecify:
    """Behaviour the requirements are **silent** on, derived by reading the code.

    These are not requirement-derived expectations and must not be read as
    ratified contracts. They are here because task 2.1 changes this function, and
    an unpinned incidental behaviour changes without anyone noticing. Each one is
    a decision 2.1 may legitimately make differently — but deliberately.
    """

    def test_an_existing_pipeline_key_is_left_alone_when_both_are_present(self, approvals_module):
        """The normalisation is guarded by `"pipeline_key" not in approval`.

        So a row that already carries a `pipeline_key` disagreeing with its
        `pipeline_id` keeps the stored `pipeline_key`. No live writer produces
        such a row; pinned because the guard is what makes the normalisation
        idempotent, and removing it would silently change which value wins.
        """
        rows = [
            _row(
                "a-1",
                "pending",
                pipeline_id="pipe-stored",
                pipeline_key="pipe-preexisting",
                created_at="2026-08-22T12:00:00Z",
            )
        ]
        _, body, _ = _call(approvals_module, {"status": "pending"}, [{"Items": rows}])

        assert body[0]["pipeline_key"] == "pipe-preexisting"
        assert body[0]["pipeline_id"] == "pipe-stored"

    def test_a_row_with_no_pipeline_id_is_returned_and_gains_no_pipeline_key(self, approvals_module):
        """`get_approvals` has **no** ghost filter, unlike `get_pending_approvals`.

        `get_pending_approvals` (`:307`) drops rows lacking `pipeline_id`
        (`:316`); this path does not, and returns them without a `pipeline_key`.
        Pinned because task 2.1 is instructed *not* to copy that filter onto the
        new keyed path — a row with no `pipeline_id` is absent from
        `pipeline-status-index` by construction (`LGR-7d19eca-2`, **invariant**),
        so a filter there could not fire. This test records that the two listing
        endpoints already differ here, so 2.1's omission is consistency rather
        than oversight.
        """
        rows = [_row("ghost-1", "pending", created_at="2026-08-22T12:00:00Z")]
        _, body, _ = _call(approvals_module, {"status": "pending"}, [{"Items": rows}])

        assert len(body) == 1, "the ghost row is not dropped on this path"
        assert body[0]["approval_id"] == "ghost-1"
        assert "pipeline_key" not in body[0]

    def test_a_row_with_no_created_at_sorts_last(self, approvals_module):
        """The sort key falls back to `""`, which is below every timestamp.

        Unreachable on today's paths: `status-created-index` is sparse on
        `created_at`, so a row without one is absent from it
        (`LGR-7d19eca-2`). It becomes **reachable for the first time** on the
        partition-only `pipeline-status-index` read task 2.1 adds, whose sort key
        is `status`. The requirements say only "sort `created_at` descending" and
        are silent about a row that has none — so this pins today's answer and
        flags it as 2.1's to confirm, not to inherit by accident.
        """
        dated = _row("a-dated", "pending", pipeline_id="pipe-1", created_at="2026-08-20T09:00:00Z")
        undated = _row("a-undated", "pending", pipeline_id="pipe-1")
        _, body, _ = _call(approvals_module, {"status": "pending"}, [{"Items": [undated, dated]}])

        assert [row["approval_id"] for row in body] == ["a-dated", "a-undated"]

    def test_an_unrecognised_status_is_queried_rather_than_refused(self, approvals_module):
        """`?status=bogus` is a 200 with `[]`, not a 400.

        The parameter is passed to the key condition unvalidated, so an unknown
        status is one Query against an empty partition. Pinned because 2.1 adds a
        second parameter to this same function: if validation is introduced for
        either, this is the behaviour that changes.
        """
        _, body, table = _call(approvals_module, {"status": "bogus"}, [{"Items": []}])

        calls = _query_calls(table)
        assert len(calls) == 1
        assert _leaves(calls[0]["KeyConditionExpression"]) == [("=", "status", ("bogus",))]
        assert body == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
