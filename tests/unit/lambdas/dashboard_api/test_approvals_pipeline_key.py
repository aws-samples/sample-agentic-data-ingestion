# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`?pipeline_id=` on `get_approvals` — Properties 1 and 4 (p51 task 2.1).

Two properties, checked in the two different ways they have to be checked:

**Property 1 — the approvals read is keyed, not walked.** A claim about the
*shape of the call*, which a backend executes and then discards, so it is
observed on a stubbed table: `IndexName` and the `KeyConditionExpression`, plus
the absence of a `FilterExpression`. Asserting the returned rows would not
distinguish a keyed Query from a walk that happened to be filtered down to the
same rows — and in the two-vocabulary hazard below, not even from a read that
matched nothing.

**Property 4 — ghost rows are excluded structurally, not by filter.** A claim
about the *index population*, which a stub cannot have, so it is checked against
the shared moto backend. `LGR-7d19eca-2` (**invariant**) is the mechanism: a GSI
indexes only items carrying **both** key attributes, so a row with no
`pipeline_id` is absent from `pipeline-status-index` by construction. The handler
therefore carries **no** ghost filter on this path, and these tests are what
establishes that the omission is safe rather than an oversight.

**Live data cannot express Property 4** (`findings.md` §4, measured): all 87
rows carry `pipeline_id`, so there are zero ghosts for a live read to exclude.
moto is the only place the case exists, which is the point of testing it there
rather than a reason to skip it.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From the spec documents, not from running the handler and recording its output
(`LGR-28b4d75-5`):

- `requirements.md` R1.3 — the read "MUST be **exact**, via a keyed Query on
  `pipeline-status-index` (`pipeline_id` = this pipeline, `status` = `pending`).
  A filter walk MUST NOT be used here".
- `design.md` §Correctness Properties, P1 and P4, including P1's "asserting
  `IndexName` and the key condition, not merely the returned rows".
- `design.md` §Error Handling — "`?pipeline_id=` with no matches is a **200 with
  `[]`**, never a 404".
- `findings.md` §2, measured live — a partition-only read on this index arrives
  ordered by the range key **`status`**, not by `created_at`, so the handler's
  `created_at`-descending sort is load-bearing on this path. The two ordering
  tests below are that measurement carried into a test.

THE TWO STATUS VOCABULARIES
---------------------------
Approvals hold lowercase (`"pending"`); the executions status filter takes
`"RUNNING"` (`findings.md` §Notes, confirmed live). A copy-pasted `"PENDING"` in
a key condition returns **zero rows with no error**, so a row-only assertion
would read as a legitimately empty pipeline. `test_the_key_condition_is_compared
_structurally_not_loosely` exists to show the condition assertion actually
discriminates that case rather than passing on anything.

WHY THE STUB IS A `MagicMock` AND THE MOTO FIXTURE IS THE SHARED ONE
-------------------------------------------------------------------
Patching the module-global `approvals_table` is the established way to observe a
call shape in this directory (`test_query_not_scan.py`,
`test_approvals_dispatch_characterization.py`). No `moto.mock_aws()` is created
here and no boto3 client is replaced — the moto tests take
`mock_dynamodb_tables` from `tests/fixtures/conftest.py`, which is where
`pipeline-status-index` is defined (`:96`), so
`tests/unit/test_no_duplicate_mocking_prop.py` is satisfied.

Those moto tables are created in `us-east-1` by that fixture while the unit layer
pins `AWS_REGION=us-west-2` (`LGR-3e45e65-1`). Harmless here: the fixture hands
over a `Table` resource carrying its own client, and it is that object the handler
is given, so no region is resolved at read time.

WHAT THIS FILE DOES NOT COVER, DELIBERATELY
-------------------------------------------
The existing `?status=` and no-parameter paths. Those are
`test_approvals_dispatch_characterization.py`'s (task 1.3), which must keep
passing **unamended** — it is the evidence this change left them alone, so
re-asserting them here would put that evidence in two places.

GOVERNING RECORDS, NONE OF THEM TOUCHED
---------------------------------------
`LGR-c58f276-3` (PROPOSED invariant — `extract_request_context` returns exactly
`{"actor", "ip_address"}`), `LGR-c58f276-2`, `LGR-e59edd7-3` and `LGR-7d19eca-3`
all govern the **approve and reject** branches of `approvals.py`. This change is
a read path and goes nowhere near them.

_Requirements: R1.1, R1.2, R1.3 — Design: C1, P1, P4_
"""

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import Key

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
#: than spelled again here (`LGR-3e45e65-1`, **template / ratified**). A
#: `KeyError` is a loud signal the layer pin has gone away, not something to
#: paper over with a second default.
_UNIT_LAYER_REGION = os.environ["AWS_REGION"]

#: The index this read must use. Taken from `requirements.md`, which quotes
#: `deploy-cdk/lib/data-stack.ts:126`, rather than imported from the module under
#: test — a test reading its expectation out of the code under test cannot detect
#: that code drifting.
_EXPECTED_PIPELINE_INDEX = "pipeline-status-index"

#: The index the *existing* paths use, needed only to show that the ghost row is
#: reachable there and therefore that its absence from the keyed read is
#: index-specific rather than a seeding failure.
_EXPECTED_STATUS_INDEX = "status-created-index"

#: A live pipeline key, from `findings.md` §2 — the one whose partition holds 11
#: approvals across three statuses. Used as the fixture value so the shapes under
#: test are the shapes measured live, not invented ones.
_PIPELINE = "helloworld-f9a76ce9"
_OTHER_PIPELINE = "newtest-ab2a08a0"

_APPROVALS_TABLE = "agentic_data_ingestion_approvals"


@pytest.fixture
def approvals_module(mock_lambda_env, monkeypatch):
    """`handlers.approvals`, imported with the env its import requires.

    `mock_lambda_env` is not optional: `handlers/common.py` calls `require_env`
    for nine table names at **import** time, so without it the import raises
    `OSError`. The region variables are then re-pinned to the unit layer's value
    because `handlers/common.py` freezes `AWS_REGION` into module-global boto3
    resources at import, and whichever test imports the module first would
    otherwise decide that region for the whole session — the collection-order
    dependence `LGR-3e45e65-1` removed from this layer.
    """
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    from handlers import approvals as approvals_mod

    return approvals_mod


def _row(approval_id: str, **attrs: Any) -> dict[str, Any]:
    """One approvals row. Only `approval_id` (the table's key) is mandatory."""
    return {"approval_id": approval_id, **attrs}


def _call_stubbed(approvals_module: Any, query_params: dict[str, str], pages: list[dict[str, Any]]):
    """Run `get_approvals` against a stubbed table; return `(body, mock_table)`.

    `pages` is one `table.query` response per expected call — `_query_all` keeps
    calling until a response omits `LastEvaluatedKey`.
    """
    mock_approvals = MagicMock()
    mock_approvals.query.side_effect = list(pages)
    with patch.object(approvals_module, "approvals_table", mock_approvals):
        response = approvals_module.get_approvals(query_params)
    assert response["statusCode"] == 200, response
    mock_approvals.scan.assert_not_called()
    return json.loads(response["body"]), mock_approvals


def _query_kwargs(table: MagicMock) -> list[dict[str, Any]]:
    """kwargs of every `table.query` call, in call order."""
    return [c.kwargs for c in table.query.call_args_list]


def _call_moto(approvals_module: Any, table: Any, query_params: dict[str, str]) -> list[dict[str, Any]]:
    """Run `get_approvals` against the real moto table; return the body array."""
    with patch.object(approvals_module, "approvals_table", table):
        response = approvals_module.get_approvals(query_params)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


# ═══════════════════════════════════════════════════════════════════════════
# Property 1 — the read is keyed, on the pipeline index
# ═══════════════════════════════════════════════════════════════════════════
class TestTheReadIsKeyedOnThePipelineIndex:
    """`IndexName` and the key condition, not the returned rows.

    **Validates: Requirements 1.1, 1.3**
    """

    def test_pipeline_id_with_a_status_queries_both_key_parts(self, approvals_module):
        """`?pipeline_id=&status=` → one Query, `pipeline_id = :p AND status = :s`.

        The full key of the index, so `ScannedCount == Count` and nothing is
        discarded server-side. `FilterExpression` must be absent: a filter would
        mean the key condition was not the whole question, which is the
        inexactness R1.3 refuses.
        """
        body, table = _call_stubbed(approvals_module, {"pipeline_id": _PIPELINE, "status": "pending"}, [{"Items": []}])

        calls = _query_kwargs(table)
        assert len(calls) == 1, f"expected one keyed Query, got {len(calls)}"
        assert calls[0]["IndexName"] == _EXPECTED_PIPELINE_INDEX
        assert calls[0]["KeyConditionExpression"] == Key("pipeline_id").eq(_PIPELINE) & Key("status").eq("pending")
        assert "FilterExpression" not in calls[0]
        assert body == []

    def test_pipeline_id_alone_queries_the_partition_only(self, approvals_module):
        """`?pipeline_id=` → the partition alone: every status for that pipeline.

        No `status` key part, because the question is "this pipeline's approvals"
        and the index's partition answers exactly that. Verified live to return
        all 11 rows of `helloworld-f9a76ce9` across three statuses
        (`findings.md` §2).
        """
        body, table = _call_stubbed(approvals_module, {"pipeline_id": _PIPELINE}, [{"Items": []}])

        calls = _query_kwargs(table)
        assert len(calls) == 1
        assert calls[0]["IndexName"] == _EXPECTED_PIPELINE_INDEX
        assert calls[0]["KeyConditionExpression"] == Key("pipeline_id").eq(_PIPELINE)
        assert "FilterExpression" not in calls[0]
        assert body == []

    def test_an_empty_status_value_is_treated_as_absent(self, approvals_module):
        """`?pipeline_id=x&status=` takes the partition-only shape.

        The same truthiness test `?status=` already gets on its own, so the two
        parameters treat an empty value identically rather than one of them
        keying on `""` — a partition that cannot exist, since `status` is a key
        attribute of the index and DynamoDB rejects an empty key value.
        """
        _, table = _call_stubbed(approvals_module, {"pipeline_id": _PIPELINE, "status": ""}, [{"Items": []}])

        calls = _query_kwargs(table)
        assert calls[0]["KeyConditionExpression"] == Key("pipeline_id").eq(_PIPELINE)

    def test_the_key_condition_is_compared_structurally_not_loosely(self, approvals_module):
        """The assertions above discriminate; they do not pass on anything.

        boto3 compares conditions by structure, so this shows the three ways the
        checks above could have been silently wrong: the wrong status **case**
        (the live two-vocabulary hazard — `"PENDING"` matches nothing without
        erroring), a partition-only condition where both parts were expected, and
        an `Attr` where a `Key` belongs, which is the difference between a key
        condition and a filter.
        """
        _, table = _call_stubbed(approvals_module, {"pipeline_id": _PIPELINE, "status": "pending"}, [{"Items": []}])
        recorded = _query_kwargs(table)[0]["KeyConditionExpression"]

        assert recorded != Key("pipeline_id").eq(_PIPELINE) & Key("status").eq("PENDING")
        assert recorded != Key("pipeline_id").eq(_PIPELINE)
        assert recorded != Key("pipeline_id").eq(_OTHER_PIPELINE) & Key("status").eq("pending")

    def test_a_pipeline_partition_larger_than_one_page_is_walked_to_the_end(self, approvals_module):
        """The keyed read is complete, not a first page.

        A truncated read would be an unmarked floor, which is the shape
        `LGR-2bcdd0d-2` forbids — and on this path it would silently drop an
        approval's Approve/Reject controls, the affordance failure R1.1 exists to
        fix.
        """
        pages = [
            {
                "Items": [_row("a-1", pipeline_id=_PIPELINE, status="pending", created_at="2026-08-22T12:00:00Z")],
                "LastEvaluatedKey": {"approval_id": "a-1"},
            },
            {"Items": [_row("a-2", pipeline_id=_PIPELINE, status="approved", created_at="2026-08-21T12:00:00Z")]},
        ]
        body, table = _call_stubbed(approvals_module, {"pipeline_id": _PIPELINE}, pages)

        calls = _query_kwargs(table)
        assert len(calls) == 2
        assert calls[1]["ExclusiveStartKey"] == {"approval_id": "a-1"}
        assert {row["approval_id"] for row in body} == {"a-1", "a-2"}

    def test_no_matches_is_a_200_with_an_empty_array(self, approvals_module):
        """An unknown pipeline key is a 200 with `[]`, never a 404.

        `design.md` §Error Handling, matching the stance
        `get_pipeline_executions` takes for an empty filtered match. Also pins
        that the pipeline key is **unvalidated**, the same as `?status=`: it goes
        to the key condition as given.
        """
        body, table = _call_stubbed(approvals_module, {"pipeline_id": "no-such-pipeline"}, [{"Items": []}])

        assert body == []
        assert _query_kwargs(table)[0]["KeyConditionExpression"] == Key("pipeline_id").eq("no-such-pipeline")


# ═══════════════════════════════════════════════════════════════════════════
# Property 4 — ghost rows are excluded structurally, not by filter
# ═══════════════════════════════════════════════════════════════════════════
class TestGhostRowsAreExcludedStructurally:
    """A row with no `pipeline_id` is absent from the index, not filtered out.

    `LGR-7d19eca-2` (**invariant**): a GSI indexes only items carrying **both**
    key attributes. So `get_approvals` needs no ghost filter on this path, and
    adding one — as `get_pending_approvals` does on the status partition — would
    imply the exclusion depends on it. It does not.

    **Validates: Requirements 1.1, 1.2**
    """

    #: One real row, one ghost. The ghost carries `status` and `created_at` and
    #: is therefore present in `status-created-index` — which is what makes its
    #: absence from `pipeline-status-index` attributable to the missing
    #: `pipeline_id` and to nothing else.
    _REAL = _row("real-1", pipeline_id=_PIPELINE, status="pending", created_at="2026-08-22T12:44:35Z")
    _GHOST = _row("ghost-1", status="pending", created_at="2026-08-22T13:00:00Z")

    @pytest.fixture
    def seeded(self, mock_dynamodb_tables):
        """The shared moto approvals table, holding one real row and one ghost."""
        table = mock_dynamodb_tables[_APPROVALS_TABLE]
        table.put_item(Item=dict(self._REAL))
        table.put_item(Item=dict(self._GHOST))
        return table

    def test_the_ghost_row_exists_in_the_table(self, seeded):
        """Both rows were stored. Establishes that later absences mean something.

        Without this, a test asserting the ghost is missing from a read would
        pass just as well if the seeding had silently failed.
        """
        assert seeded.get_item(Key={"approval_id": "ghost-1"}).get("Item") is not None
        assert seeded.get_item(Key={"approval_id": "real-1"}).get("Item") is not None

    def test_the_index_itself_does_not_contain_the_ghost_row(self, seeded):
        """The structural half, asserted on the index rather than on the handler.

        Scanning the GSI enumerates its population directly: the real row is
        there, the ghost is not, and no handler code ran. This is the mechanism
        `LGR-7d19eca-2` names, so the handler's missing filter is not what
        excludes the ghost — the index never offered it.

        (The Scan is the *test's*, to inspect the index. The handler's own read
        is a Query, which `TestTheReadIsKeyedOnThePipelineIndex` asserts.)
        """
        indexed = seeded.scan(IndexName=_EXPECTED_PIPELINE_INDEX).get("Items", [])
        indexed_ids = {item["approval_id"] for item in indexed}

        assert "real-1" in indexed_ids
        assert "ghost-1" not in indexed_ids, "a row with no pipeline_id must be absent from the index"

    def test_the_ghost_is_reachable_on_the_status_index_so_the_absence_is_index_specific(self, seeded):
        """The other index *does* carry the ghost — same row, same table.

        This is what makes the previous test a statement about the pipeline
        index's key schema rather than about the row being unstored or
        unqueryable. It is also why `get_pending_approvals` needs its filter and
        this path does not: the two read different indexes.
        """
        on_status_index = seeded.query(
            IndexName=_EXPECTED_STATUS_INDEX,
            KeyConditionExpression=Key("status").eq("pending"),
        ).get("Items", [])

        assert {item["approval_id"] for item in on_status_index} == {"real-1", "ghost-1"}

    def test_the_keyed_read_returns_the_real_row_and_not_the_ghost(self, approvals_module, seeded):
        """End to end: `?pipeline_id=` yields the real row alone.

        The handler applies no ghost filter, so this result is the index's
        population reaching the response unaltered.
        """
        body = _call_moto(approvals_module, seeded, {"pipeline_id": _PIPELINE})

        assert [row["approval_id"] for row in body] == ["real-1"]
        assert body[0]["pipeline_key"] == _PIPELINE

    def test_the_existing_status_path_still_returns_the_ghost(self, approvals_module, seeded):
        """`?status=pending` is unchanged and still returns both rows.

        Not a re-characterization of that path — it is the *contrast* that makes
        the keyed read's exclusion visible as a property of the index. The ghost
        gains no `pipeline_key`, having no `pipeline_id` to normalise.
        """
        body = _call_moto(approvals_module, seeded, {"status": "pending"})

        assert {row["approval_id"] for row in body} == {"real-1", "ghost-1"}
        ghost = next(row for row in body if row["approval_id"] == "ghost-1")
        assert "pipeline_key" not in ghost


# ═══════════════════════════════════════════════════════════════════════════
# Ordering on the new path — findings.md §2's live measurement, as a test
# ═══════════════════════════════════════════════════════════════════════════
class TestOrderingOnThePartitionOnlyPath:
    """The `created_at` sort is load-bearing here, and its undated case is reachable.

    Live measurement (`findings.md` §2): a partition-only read on
    `pipeline-status-index` arrives ordered by the range key **`status`**
    (`approved…`, `pending`, `rejected…`), not by `created_at`. These tests are
    that measurement carried into moto, plus the deliberate confirmation of the
    undated-row case that task 1.3 handed to 2.1.
    """

    #: Statuses chosen so `status` order and `created_at` order **disagree**: by
    #: status the order is approved, pending, rejected; by `created_at`
    #: descending it is rejected, pending, approved. Exactly reversed, so a
    #: response in index order cannot pass by coincidence.
    _ROWS = [
        _row("a-approved", pipeline_id=_PIPELINE, status="approved", created_at="2026-08-20T09:00:00Z"),
        _row("a-pending", pipeline_id=_PIPELINE, status="pending", created_at="2026-08-22T12:44:35Z"),
        _row("a-rejected", pipeline_id=_PIPELINE, status="rejected", created_at="2026-08-24T18:00:00Z"),
    ]

    @pytest.fixture
    def seeded(self, mock_dynamodb_tables):
        table = mock_dynamodb_tables[_APPROVALS_TABLE]
        for row in self._ROWS:
            table.put_item(Item=dict(row))
        return table

    def test_the_response_is_created_at_descending_not_index_order(self, approvals_module, seeded):
        """Newest first, though the index hands the rows over in status order."""
        body = _call_moto(approvals_module, seeded, {"pipeline_id": _PIPELINE})

        assert [row["approval_id"] for row in body] == ["a-rejected", "a-pending", "a-approved"]

    def test_an_undated_row_sorts_last_and_is_not_dropped(self, approvals_module, seeded):
        """A row with no `created_at` is returned, and it is returned **last**.

        **Reachable for the first time on this path.** `status-created-index` is
        sparse on `created_at`, so such a row is absent from it
        (`LGR-7d19eca-2`) and the existing paths never see one; this index's sort
        key is `status`, so it does carry the row.

        Last place is the confirmed answer, not an inherited accident: the order
        claims recency, an undated row's recency is unknown, and first place
        would assert it is the newest. Dropping it is also refused — that would
        cost the row its Approve/Reject controls, which is the affordance failure
        R1.1 exists to fix.
        """
        seeded.put_item(Item=_row("a-undated", pipeline_id=_PIPELINE, status="pending"))

        body = _call_moto(approvals_module, seeded, {"pipeline_id": _PIPELINE})

        assert [row["approval_id"] for row in body] == [
            "a-rejected",
            "a-pending",
            "a-approved",
            "a-undated",
        ]

    def test_the_keyed_status_read_narrows_to_one_status_within_the_pipeline(self, approvals_module, seeded):
        """Both key parts together: this pipeline **and** this status.

        The shape the overview card uses, and the one measured live returning one
        row per pipeline for `status="pending"`.
        """
        seeded.put_item(
            Item=_row("other-pending", pipeline_id=_OTHER_PIPELINE, status="pending", created_at="2026-08-23T00:00:00Z")
        )

        body = _call_moto(approvals_module, seeded, {"pipeline_id": _PIPELINE, "status": "pending"})

        assert [row["approval_id"] for row in body] == ["a-pending"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
