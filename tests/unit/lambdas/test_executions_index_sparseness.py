# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``RunsByTimeIndex`` sparseness, asserted rather than assumed (p48 R9.1, R9.3, CP5).

Two things this pins, and they are deliberately in one file because the second
only matters because of the first:

**R9.1 — the sparseness is documented behaviour, not a surprise.** A DynamoDB GSI
indexes only items carrying BOTH key attributes. ``RunsByTimeIndex`` is
``(pipeline_id, started_at)``, so a row with no ``started_at`` is absent from it
and therefore invisible in run history once the read moves to the index (p48 C1)
— silently, with no error. Every prose statement of that in the spec is worth one
executable assertion, which is what ``TestTheIndexIsSparse`` is.

**R9.3 / CP5 — the absence is detectable after the fact.** The honest guarantee
under a sparse index is *detection*, not inclusion:
``scripts/detect_unindexed_executions.find_unindexed_rows`` finds any base-table
row the index cannot see. Tested here against moto rather than live AWS, because
an e2e test is the wrong shape for a gate (``make test-e2e`` is never a gate and
e2e tests are excluded by default). The operational query itself lives in the
script, which is read-only.

**Do NOT read either of these as licence to filter on sparseness** — p48 do-NOT 2
and R9.4. The index incidentally excludes rows the phantom-row filter would also
drop, and that overlap is an accident. ``TestSparsenessIsNotAFilter`` asserts the
two sets are genuinely different, so the accident cannot be mistaken for a design.

The writer-side half of R9 is
``tests/unit/lambdas/test_executions_started_at_invariant.py``.

Validates: p48 Requirements 9.1, 9.3, 9.4
"""

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.detect_unindexed_executions import find_unindexed_rows

EXECUTIONS = "agentic_data_ingestion_executions"
INDEX = "RunsByTimeIndex"

PIPELINE = "pipe-sparse-001"


def _row(run_id: str, *, started_at: str | None, **extra) -> dict:
    """An executions row. ``started_at=None`` omits the attribute entirely —
    which is the only way to be absent from the index, and is exactly what the
    four previously-unguarded approval-path upserts would have produced."""
    row: dict = {"pipeline_id": PIPELINE, "run_id": run_id, "status": "RUNNING"}
    if started_at is not None:
        row["started_at"] = started_at
    row.update(extra)
    return row


def _clear(table) -> None:
    """Empty the executions table. Needed only by the property test, where
    Hypothesis reuses one function-scoped fixture across every example."""
    for item in table.scan(ProjectionExpression="pipeline_id, run_id").get("Items", []):
        table.delete_item(Key={"pipeline_id": item["pipeline_id"], "run_id": item["run_id"]})


def _index_run_ids(table) -> set[str]:
    response = table.query(
        IndexName=INDEX,
        KeyConditionExpression="pipeline_id = :pid",
        ExpressionAttributeValues={":pid": PIPELINE},
    )
    return {item["run_id"] for item in response["Items"]}


# ---------------------------------------------------------------------------
# R9.1 — the sparseness itself
# ---------------------------------------------------------------------------


class TestTheIndexIsSparse:
    def test_a_row_without_started_at_is_absent_from_the_index(self, mock_dynamodb_tables):
        """R9.1: stated in an assertion so it is documented behaviour.

        This is the accepted cost of p48's chosen mechanism. The row is in the
        table, readable by key, and simply not in the index.
        """
        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(Item=_row("run-with-time", started_at="2026-08-24T13:04:04"))
        table.put_item(Item=_row("run-no-time", started_at=None))

        assert _index_run_ids(table) == {"run-with-time"}, (
            "a row carrying started_at must be indexed and a row without it must not be — "
            "if this fails, the sparseness premise the whole of R9 rests on is wrong"
        )

        # Still present in the base table, and readable by key. The row is not
        # lost; it is unreachable through the ordering index.
        assert "Item" in table.get_item(Key={"pipeline_id": PIPELINE, "run_id": "run-no-time"})

    def test_an_empty_started_at_is_refused_at_write_time_not_silently_unindexed(self, mock_dynamodb_tables):
        """Omission is the ONLY way to be invisible — an empty string is rejected.

        Measured, not assumed, and it narrows R9's failure mode usefully: a
        writer that sets ``started_at = ""`` does not produce a quietly
        unindexed row, it fails loudly with a ``ValidationException``, because a
        key attribute of a GSI cannot hold an empty string. So the only reachable
        way into the invisible set is ABSENCE of the attribute, which is exactly
        what the writer invariant enforces against.

        The error names ``StatusIndex`` rather than ``RunsByTimeIndex`` — both
        index ``started_at`` as their sort key and DynamoDB reports the first it
        validates, which is why the assertion is on the error type and the
        attribute, not on the index name.
        """
        from botocore.exceptions import ClientError

        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(Item=_row("run-real", started_at="2026-08-24T13:04:04"))

        with pytest.raises(ClientError) as excinfo:
            table.put_item(Item=_row("run-empty", started_at=""))

        assert excinfo.value.response["Error"]["Code"] == "ValidationException"
        assert "started_at" in str(excinfo.value)
        assert _index_run_ids(table) == {"run-real"}


# ---------------------------------------------------------------------------
# R9.3 / CP5 — detection
# ---------------------------------------------------------------------------


class TestDetectionFindsUnindexedRows:
    def test_finds_the_row_the_index_cannot_see(self, mock_dynamodb_tables):
        """R9.3: the failure mode is "a check found it", not "a run vanished"."""
        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(Item=_row("run-a", started_at="2026-08-24T10:00:00"))
        table.put_item(Item=_row("run-b", started_at="2026-08-24T11:00:00"))
        table.put_item(Item=_row("ghost", started_at=None, status="FAILED"))

        report = find_unindexed_rows(table, index_name=INDEX)

        assert report["base_count"] == 3
        assert report["index_count"] == 2
        assert report["missing_count"] == 1
        assert report["counts_agree"] is True
        assert [(r["pipeline_id"], r["run_id"]) for r in report["missing"]] == [(PIPELINE, "ghost")]
        assert report["missing"][0]["has_started_at"] is False
        # The status is carried through so the row is identifiable without a
        # second read — the live cases were `{pipeline_id, run_id, status:
        # FAILED, updated_at}` and nothing else.
        assert report["missing"][0]["status"] == "FAILED"

    def test_reports_clean_when_every_row_is_indexed(self, mock_dynamodb_tables):
        table = mock_dynamodb_tables[EXECUTIONS]
        for i in range(3):
            table.put_item(Item=_row(f"run-{i}", started_at=f"2026-08-24T1{i}:00:00"))

        report = find_unindexed_rows(table, index_name=INDEX)

        assert report["missing"] == []
        assert report["missing_count"] == 0
        assert report["base_count"] == report["index_count"] == 3

    def test_the_count_arithmetic_equals_the_enumeration(self, mock_dynamodb_tables):
        """``base_count - index_count`` is the invisible-row count, exactly.

        Pinned because that subtraction is what an operator will run against
        live data without enumerating anything (61 - 51 = 10 on the validation account), so
        the two must not be able to disagree.
        """
        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(Item=_row("seen-1", started_at="2026-08-24T10:00:00"))
        table.put_item(Item=_row("unseen-1", started_at=None))
        table.put_item(Item=_row("unseen-2", started_at=None))

        report = find_unindexed_rows(table, index_name=INDEX)

        assert report["missing_count"] == len(report["missing"]) == 2
        assert report["counts_agree"] is True

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(flags=st.lists(st.booleans(), min_size=1, max_size=8), example=st.uuids())
    def test_every_unindexed_row_is_found_whatever_the_mix(self, mock_dynamodb_tables, flags, example):
        """CP5: for ANY row absent from the index, the detection query finds it.

        Generated over the mix of rows-with and rows-without ``started_at``
        rather than example-tested, because the property that matters is "no
        invisible row escapes the check", and a single example passes with an
        off-by-one in the set difference.

        Hypothesis reuses the function-scoped moto fixture across examples, so
        each example clears the table first. Scoping by a generated ``run_id``
        prefix instead does NOT work: the uuid strategy is deterministic under
        replay and shrinking, so two examples can share a prefix and the second
        fails on the first's rows rather than on the property.

        **Validates: Requirements 9.1, 9.3**
        """
        table = mock_dynamodb_tables[EXECUTIONS]
        _clear(table)
        expected_missing = set()
        for i, has_time in enumerate(flags):
            run_id = f"gen-{i}"
            table.put_item(Item=_row(run_id, started_at=f"2026-08-24T10:00:0{i}" if has_time else None))
            if not has_time:
                expected_missing.add(run_id)

        report = find_unindexed_rows(table, index_name=INDEX)

        assert {r["run_id"] for r in report["missing"]} == expected_missing
        assert report["missing_count"] == len(expected_missing)
        assert all(r["has_started_at"] is False for r in report["missing"])


# ---------------------------------------------------------------------------
# R9.4 / do-NOT 2 — sparseness is NOT the phantom filter
# ---------------------------------------------------------------------------


class TestSparsenessIsNotAFilter:
    def test_the_two_sets_are_different_so_the_overlap_is_an_accident(self, mock_dynamodb_tables):
        """R9.4: index absence and phantom-ness are different predicates.

        The four upsert shapes that motivated this task happen to lack BOTH
        ``started_at`` and ``current_step``/``steps_status``, so they are
        excluded twice over — which is exactly the coincidence do-NOT 2 warns
        against reading as a design. These two rows separate the predicates:

        * ``real-but-unindexed`` is a genuine run the phantom filter KEEPS and
          the index DROPS, so leaning on sparseness would hide a real run.
        * ``phantom-but-indexed`` is a phantom the phantom filter DROPS and the
          index KEEPS, so sparseness cannot replace that filter either.

        Neither direction of substitution is safe, which is the point.
        """
        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(
            Item=_row("real-but-unindexed", started_at=None, current_step="run_pipeline", steps_status={"x": "OK"})
        )
        table.put_item(Item=_row("phantom-but-indexed", started_at="2026-08-24T12:00:00"))

        indexed = _index_run_ids(table)
        assert "real-but-unindexed" not in indexed, "a real run with no started_at is invisible — R9.1"
        assert "phantom-but-indexed" in indexed, (
            "a row the phantom filter would drop is still in the index, so index membership "
            "is not the phantom predicate (R9.4, do-NOT 2)"
        )

    def test_detection_reports_a_real_run_not_just_fixtures(self, mock_dynamodb_tables):
        """A row carrying step state and no ``started_at`` is still reported.

        This is the p48 R9.1 case that renders in run history TODAY (it passes
        the phantom filter) and would vanish the moment the read moves to the
        index. The detection query must not exempt it on the grounds that it
        looks like a real run.
        """
        table = mock_dynamodb_tables[EXECUTIONS]
        table.put_item(
            Item=_row("live-run", started_at=None, current_step="schema_extract", steps_status={"schema_extract": "OK"})
        )

        report = find_unindexed_rows(table, index_name=INDEX)

        assert [r["run_id"] for r in report["missing"]] == ["live-run"]


def test_the_index_exists_in_the_test_fixture():
    """Guards against the whole file passing vacuously.

    Every assertion above rests on ``RunsByTimeIndex`` existing on the moto
    table. If the fixture loses it, the queries raise rather than pass — but a
    future refactor could make them silently empty, so the index is asserted
    directly against the shared definition.
    """
    from tests.fixtures.conftest import TABLE_DEFINITIONS

    executions = next(t for t in TABLE_DEFINITIONS if t["TableName"] == EXECUTIONS)
    index = next((g for g in executions["gsis"] if g["IndexName"] == INDEX), None)
    assert index is not None, f"{INDEX} is missing from the shared moto table definitions"
    assert (index["pk"], index["sk"]) == ("pipeline_id", "started_at"), (
        "the fixture's index key schema must match data-stack.ts, or the sparseness these "
        "tests assert is not the deployed sparseness"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
