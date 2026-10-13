# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test for the run-details API response shape.

Verifies that the response from get_run_details includes properly parsed
approvals with `details`, `gate_name`, `decided_by`, `decided_at`, and
`comment` — matching the Approval TypeScript interface expected by the
React dashboard's ApprovalWorkflowSection component.

Requirements: 8.1, 8.2, 8.3, 8.4, 12.2

p37 task 6.3 extends this file with two additive contracts on the same body:
``load_outcome`` is present on the run-detail response (R1.8), and neither
``load_metrics`` nor ``data_quality`` appears in ``steps`` (R4.4). The second is
asserted rather than assumed on purpose — that filter was already correct before
p37, and the point of pinning it is to prove it *stayed* correct while a second
record type was added to the same denylist. Nothing that was asserted here before
has been weakened.

p38 task 5.5 extends it with the retry-lineage contract: ``attempts``,
``attempt_count`` and ``retry_source_run_id`` (R5.1, R5.2). Those three are
**conditionally present**, and modelling that correctly is the whole point of
pinning them — see ``TestRunDetailsRetryLineageContract``. This file is the p37/p38
rebase point named in both designs (p38 §4.11); the two field sets are additive on
one body and ``test_load_outcome_and_the_lineage_keys_coexist`` is the assertion
that keeps them so.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUN_ID = "run-contract-001"
PIPELINE_ID = "pipe-contract-001"

STATE_ITEM = {
    "run_id": RUN_ID,
    "step_name": "schema_extract",
    "pipeline_id": PIPELINE_ID,
    "original_pipeline_id": PIPELINE_ID,
    "execution_id": "exec-001",
    "json_s3_uri": "s3://bucket/data.json",
    "status": "success",
    "timestamp": "2025-01-15T12:00:00+00:00",
    "step_sequence": 1,
}

# Approval fields required by the React dashboard Approval interface
APPROVAL_REQUIRED_FIELDS = {"details", "gate_name", "decided_by", "decided_at", "comment"}

# Keys ``derive_load_outcome`` returns in EVERY arm (shared/load_outcome.py).
# The run detail and the run list read the same pure deriver, so this set is the
# same one test_executions_contract.py pins — stated in both places because the
# two response bodies are two separate contracts.
LOAD_OUTCOME_REQUIRED_FIELDS = {
    "outcome",
    "rows_read",
    "rows_written",
    "rows_not_written",
    "blocks_run",
    "missing",
    "determined_from",
    "reason",
}

# p38 R5.1 / R5.2 — the three retry-lineage keys the reader emits from the
# executions row. Unlike ``load_outcome`` these are NOT unconditional, and the
# condition is the contract: see TestRunDetailsRetryLineageContract.
LINEAGE_KEYS = {"attempts", "attempt_count", "retry_source_run_id"}

# The exact projection the reader must ask for. Pinned as a string because a
# MagicMock honours no ProjectionExpression — it returns whatever ``Item`` the
# test seeded regardless — so every value assertion below would still pass if the
# projection were narrowed back to ``"step_logs"`` and lineage silently went
# empty against a real table. That is ``LGR-28b4d75-5``'s (**template /
# ratified**) failure mode in its mock-side form: a contract whose oracle is the
# producer's own output cannot detect the producer drifting.
#
# ``completed_at`` is the fifth name, added by p48 task 6.2 (``R8.1``). It rides
# this read rather than the injection read above it for two recorded reasons: the
# injection read's projection carries an in-code fence against becoming a second
# source for response fields (do-NOT 9), and this read's ``try`` gives the
# omit-on-failure semantics ``LGR-8e34f37-3`` (**template / ratified**) pins for
# its neighbours — so a failed read leaves ``completed_at`` off the body instead
# of reporting an unfinished-looking empty for a run that may well have finished.
LINEAGE_PROJECTION = "step_logs, attempts, attempt_count, retry_source_run_id, completed_at"


def _parse_response(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _step_item(step_name: str, *, status: str = "success", sequence: int = 1) -> dict:
    """A genuine Step_Row — the kind that belongs in ``steps``."""
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-001",
        "status": status,
        "timestamp": f"2025-01-15T12:{sequence:02d}:00+00:00",
        "step_sequence": sequence,
    }


def _load_metrics_item(**counts) -> dict:
    """The generated Glue job's ``(run_id, "load_metrics")`` counts row.

    No ``status`` attribute, matching the real writer: it is a record, not a
    Step_Row. Counts are ``Decimal`` because that is what DynamoDB returns for a
    number attribute, and ``load_outcome._usable_count`` rejects ``Decimal`` —
    so a reader that skips the ``item_int`` conversion answers
    ``not_established`` for every run while looking healthy. Seeding ``int``
    would never exercise that conversion.
    """
    row = {
        "run_id": RUN_ID,
        "step_name": "load_metrics",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "source": "glue_job",
        "timestamp": "2025-01-15T12:09:00+00:00",
    }
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


def _dq_report_item() -> dict:
    """A DQ_Report_Row: ``overall_status`` and no ``status`` (R4.4's subject)."""
    return {
        "run_id": RUN_ID,
        "step_name": "data_quality",
        "pipeline_id": PIPELINE_ID,
        "overall_status": "PASSED",
        "source": "glue_inline_dq",
        "timestamp": "2025-01-15T12:08:00+00:00",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    mock_state.query.return_value = {"Items": [STATE_ITEM]}
    mock_executions.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.approvals = mock_approvals
    # Additive (p37 task 6.3): the load_outcome and synthetic-step contracts
    # below seed the run's state rows, which the existing approval tests do not
    # need to vary.
    _patch_tables.state = mock_state
    # Additive (p38 task 5.5): the lineage contract varies
    # ``executions_table.get_item`` in get_run_details — a successful read, and a
    # read that raises. ``get_item.return_value = {}`` above is a *successful*
    # read of a row with no lineage, which is why the keys are already present on
    # every other test's body and were merely unasserted before this task.
    #
    # There are TWO such reads since p48 task 6.3 — the running-step injection and
    # this lineage read, the same key with different projections — and this mock
    # serves one ``return_value`` to both, because a MagicMock honours no
    # ``ProjectionExpression``. That is fine for a *contract* file: what is pinned
    # here is the shape on the wire, and ``LINEAGE_PROJECTION`` below pins which
    # read asks for what. Telling the two reads apart by projection needs the
    # dispatching fixture in
    # ``tests/unit/lambdas/dashboard_api/test_run_details_lineage.py``, which is
    # where the assertion that ``completed_at`` comes from the lineage read and
    # not from the injection read lives (p48 task 6.2).
    _patch_tables.executions = mock_executions
    yield


def _set_approvals(items):
    # P28 R17.1: run-details approvals are fetched via the run_id GSI Query
    _patch_tables.approvals.query.return_value = {"Items": items}


def _set_state_items(items):
    """Replace the run's state rows. ``get_run_details`` holds them all already."""
    _patch_tables.state.query.return_value = {"Items": items}


def _set_executions_row(**attributes):
    """A *successful* keyed read of the run's executions row.

    ``attributes`` are the row's attributes as DynamoDB returns them. Called with
    nothing it is the fixture's own default — a row that was read and carries no
    lineage, which is a different case from a read that failed.
    """
    _patch_tables.executions.get_item.side_effect = None
    _patch_tables.executions.get_item.return_value = {"Item": dict(attributes)} if attributes else {}


def _set_executions_read_failure(error: Exception):
    """The keyed read raises. ``get_run_details`` logs at debug and continues."""
    _patch_tables.executions.get_item.side_effect = error


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.pipelines_runs import get_run_details

# ===========================================================================
# Contract tests — Run details approvals expose `details`
# ===========================================================================


class TestRunDetailsApprovalContract:
    """Run-details response approvals expose details + decision fields."""

    def test_approvals_array_present_in_response(self):
        """Response body always includes an 'approvals' array."""
        _set_approvals([])

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert "approvals" in body
        assert isinstance(body["approvals"], list)

    def test_approval_has_required_fields(self):
        """Each approval in the response has all dashboard-required fields."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-c1",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": "admin@example.com",
                    "approved_at": "2025-01-15T13:00:00Z",
                    "comments": "Approved",
                    "approval_data": json.dumps(
                        {
                            "source_schema": {"id": "int"},
                            "evidence": {"id": {"reasoning": "exact name match"}},
                            "questions": [],
                        }
                    ),
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        approval = body["approvals"][0]
        missing = APPROVAL_REQUIRED_FIELDS - set(approval.keys())
        assert not missing, f"Approval missing fields: {missing}"

    def test_details_is_always_dict(self):
        """details field is always a dict, even when approval_data is missing."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-c2",
                    "run_id": RUN_ID,
                    "approval_type": "code_generation",
                    "status": "pending",
                    # No approval_data
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        details = body["approvals"][0]["details"]
        assert isinstance(details, dict)

    def test_details_exposes_evidence_and_questions(self):
        """When approval_data contains evidence/questions, details exposes them."""
        evidence = {"field_a": {"reasoning": "high confidence", "confidence_after": 0.95}}
        questions = [{"field": "field_b", "question": "Confirm mapping?"}]

        _set_approvals(
            [
                {
                    "approval_id": "appr-c3",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": "user@example.com",
                    "approved_at": "2025-01-15T14:00:00Z",
                    "comments": "Good",
                    "approval_data": json.dumps(
                        {
                            "evidence": evidence,
                            "questions": questions,
                        }
                    ),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        details = body["approvals"][0]["details"]
        assert "evidence" in details
        assert "questions" in details
        assert details["evidence"] == evidence
        assert details["questions"] == questions

    def test_gate_name_defaults_to_approval_type(self):
        """gate_name falls back to approval_type when not explicitly set."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-c4",
                    "run_id": RUN_ID,
                    "approval_type": "infrastructure",
                    "status": "pending",
                    "approval_data": "{}",
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        assert body["approvals"][0]["gate_name"] == "infrastructure"

    def test_unparseable_approval_data_never_fails_request(self):
        """Broken JSON in approval_data still returns 200 with empty details."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-c5",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "approval_data": "<<< not JSON >>>",
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == {}


# ===========================================================================
# Contract tests — the run-detail body carries load_outcome (p37 R1.8)
# ===========================================================================


class TestRunDetailsLoadOutcomeContract:
    """``load_outcome`` is on the run-detail body, in the run list's shape."""

    def test_load_outcome_present_on_the_body(self):
        """Never omitted. A run with no counts row carries ``not_established``
        rather than nothing, so the frontend never has to tell an absent field
        apart from absent counts."""
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10)])

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert "load_outcome" in body
        assert isinstance(body["load_outcome"], dict)

    def test_load_outcome_has_the_full_key_set(self):
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=5, rows_written=0)])

        _, body = _parse_response(get_run_details(RUN_ID))

        missing = LOAD_OUTCOME_REQUIRED_FIELDS - set(body["load_outcome"].keys())
        assert not missing, f"load_outcome missing fields: {missing}"

    def test_load_outcome_states_both_counts_and_the_withheld_count(self):
        """R1.8 — the run detail page has to be able to state Rows_Read and
        Rows_Written, so both have to survive to the response. Seeded as
        ``Decimal``: without the conversion in the reader this reads
        ``not_established`` and the page states nothing."""
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=5, rows_written=0)])

        _, body = _parse_response(get_run_details(RUN_ID))

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "no_rows_loaded"
        assert outcome["determined_from"] == "load_metrics_row"
        assert outcome["rows_read"] == 5
        assert outcome["rows_written"] == 0
        assert outcome["rows_not_written"] == 5
        assert outcome["missing"] == []

    def test_blocks_run_is_false(self):
        """p37 R1.12 and do-NOT 4 as a field, pinned on this surface too. A
        ``no_rows_loaded`` verdict reads as if it stops something; it does not,
        and by the time it is knowable the task token is already consumed.
        ``is False`` rather than ``== False`` — a falsy stand-in would pass
        equality and defeat the point."""
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=5, rows_written=0)])

        _, body = _parse_response(get_run_details(RUN_ID))

        assert body["load_outcome"]["blocks_run"] is False

    def test_no_counts_row_is_not_established_naming_the_absence(self):
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10)])

        _, body = _parse_response(get_run_details(RUN_ID))

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_absent"
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None
        assert outcome["blocks_run"] is False

    def test_an_incomplete_row_is_a_different_provenance_from_an_absent_one(self):
        """Both are ``not_established`` and the provenance is the only thing
        that separates them. ``LGR-78ce0b7-8`` (**template / ratified**) is why
        they must stay separate: a record that exists and says nothing must not
        report the same as no record at all."""
        _set_approvals([])

        _set_state_items([_step_item("run_pipeline", sequence=10)])
        absent = _parse_response(get_run_details(RUN_ID))[1]["load_outcome"]

        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=7)])
        incomplete = _parse_response(get_run_details(RUN_ID))[1]["load_outcome"]

        assert absent["determined_from"] == "load_metrics_row_absent"
        assert absent["missing"] == ["rows_read", "rows_written"]
        assert incomplete["determined_from"] == "load_metrics_row_incomplete"
        assert incomplete["missing"] == ["rows_written"]
        assert absent["outcome"] == incomplete["outcome"] == "not_established"
        assert absent["determined_from"] != incomplete["determined_from"]


class TestRunDetailsSyntheticStepsContract:
    """R4.4 — records on the run's partition are not steps in the tree."""

    def test_load_metrics_is_absent_from_steps(self):
        """p37 C1 added a second record type to this run's partition. Without
        ``load_metrics`` in the reader's synthetic-step set it renders as a
        trailing step with an unknown status — the same defect Requirement 4
        exists to remove, on a new row."""
        _set_approvals([])
        _set_state_items(
            [
                _step_item("schema_extract", sequence=1),
                _step_item("run_pipeline", sequence=10),
                _load_metrics_item(rows_read=5, rows_written=0),
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert "load_metrics" not in [s["step_name"] for s in body["steps"]]

    def test_data_quality_is_still_absent_from_steps(self):
        """Asserted, not assumed. This filter was already correct before p37 —
        the point of the assertion is that it stayed correct while
        ``load_metrics`` was added to the same set."""
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _dq_report_item()])

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert "data_quality" not in [s["step_name"] for s in body["steps"]]

    def test_both_records_are_absent_from_steps_on_one_run(self):
        """The shape of a real ``s3_tables`` run: both records on the partition,
        and only the genuine steps in the tree."""
        _set_approvals([])
        _set_state_items(
            [
                _step_item("schema_extract", sequence=1),
                _step_item("run_pipeline", sequence=10),
                _dq_report_item(),
                _load_metrics_item(rows_read=5, rows_written=0),
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        step_names = [s["step_name"] for s in body["steps"]]
        assert step_names == ["schema_extract", "run_pipeline"]
        assert "load_metrics" not in step_names
        assert "data_quality" not in step_names

    def test_the_filtered_row_still_supplies_the_counts(self):
        """The two halves together: the row is dropped from ``steps`` and its
        counts still reach ``load_outcome``, because the derivation runs before
        the filter."""
        _set_approvals([])
        _set_state_items(
            [
                _step_item("run_pipeline", sequence=10),
                _dq_report_item(),
                _load_metrics_item(rows_read=1000, rows_written=998),
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        assert [s["step_name"] for s in body["steps"]] == ["run_pipeline"]
        assert body["load_outcome"]["outcome"] == "partial_load"
        assert body["load_outcome"]["rows_not_written"] == 2


# ===========================================================================
# Contract tests — the run-detail body carries retry lineage (p38 R5.1, R5.2)
# ===========================================================================


class TestRunDetailsRetryLineageContract:
    """``attempts``, ``attempt_count`` and ``retry_source_run_id``, and the
    condition under which each is present.

    **The condition is the contract, not an implementation detail.** The reader
    sets all three *inside* the ``try`` around the one keyed
    ``executions_table.get_item`` (``pipelines_runs.py:796-814``), so:

    * a row that **was read** gets all three keys — empty values there are
      knowledge (the row genuinely records no attempts), not defaults;
    * a read that **raised** gets **none** of the three, and the response is
      still ``200``.

    ``LGR-99050e6-2`` (**template / ratified**) is why it has to be that way
    round: an absent counter "is reported as unknown rather than as zero". So
    ``attempt_count: 0`` on a run whose row could not be read is the prohibited
    shape — it asserts zero attempts where nothing is known. A test asserting
    ``body["attempts"] == []`` on the failure path would pin exactly that shape;
    the correct assertion is ``"attempts" not in body``.

    Unlike ``load_outcome``, which is never omitted because its deriver is total
    over rows the reader already holds, lineage lives on a *second* read that can
    fail on its own. That is the whole reason the two field sets are pinned
    differently on one body.
    """

    def test_all_three_keys_are_present_when_the_read_succeeds(self):
        """The fixture default — ``get_item`` returning no ``Item`` at all — is
        still a *successful* read, so all three keys are on the body. That is the
        state every other test in this file has been in all along, unasserted,
        which is why this contract was green before task 5.5 amended it."""
        _set_approvals([])
        _set_executions_row()

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        missing = LINEAGE_KEYS - set(body.keys())
        assert not missing, f"run detail missing lineage keys: {missing}"

    def test_a_read_row_with_no_lineage_reports_empty_rather_than_omitting(self):
        """Present-and-empty is the answer for a row that was read. Types are
        pinned; the values are not, because what the frontend has to be able to
        tell apart is present from absent, and pinning ``0`` here would make a
        later, more honest ``None`` for "unknown" read as a regression."""
        _set_approvals([])
        _set_executions_row(step_logs={})

        _, body = _parse_response(get_run_details(RUN_ID))

        assert isinstance(body["attempts"], list)
        assert isinstance(body["attempt_count"], int)
        assert isinstance(body["retry_source_run_id"], str)

    def test_recorded_lineage_reaches_the_response_verbatim(self):
        """R5.1 / R5.2 — the attempt list and both identifiers survive the read.

        The attempt node carries the disagreeing pair R2.3 is about: the row's
        ``status`` says ``FAILED`` while its own ``steps_status`` says a later
        step succeeded. Both survive into one node rather than one being
        reconciled away.
        """
        _set_approvals([])
        attempts = [
            {
                "attempt": 1,
                "status": "FAILED",
                "steps_status": {"file_validation": "completed", "test_code": "failed"},
                "started_at": "2025-01-15T12:00:00Z",
                "completed_at": "2025-01-15T12:30:00Z",
                "failed_step": "test_code",
                "execution_name": "run-contract-001-aaaaaaaa",
            }
        ]
        _set_executions_row(
            attempts=attempts,
            attempt_count=Decimal("1"),
            retry_source_run_id=RUN_ID,
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["attempts"] == attempts
        assert body["attempt_count"] == 1
        assert body["retry_source_run_id"] == RUN_ID

    def test_attempt_count_survives_as_an_int_not_a_decimal(self):
        """DynamoDB returns a number attribute as ``Decimal``, which is not JSON
        serialisable by the stdlib encoder. Seeded as ``Decimal`` deliberately,
        for the same reason the ``load_metrics`` fixtures above are: an ``int``
        would never exercise the conversion."""
        _set_approvals([])
        _set_executions_row(attempt_count=Decimal("4"))

        _, body = _parse_response(get_run_details(RUN_ID))

        assert body["attempt_count"] == 4
        assert isinstance(body["attempt_count"], int)

    def test_a_capped_attempt_list_reaches_the_response_whole(self):
        """The cap is five (p38 §4.2) and the reader does not re-trim. Pinned so
        a reader-side cap can never be introduced as a second, divergent copy of
        the writer's."""
        _set_approvals([])
        attempts = [{"attempt": n, "status": "FAILED", "steps_status": {}} for n in range(2, 7)]
        _set_executions_row(attempts=attempts, attempt_count=Decimal("6"))

        _, body = _parse_response(get_run_details(RUN_ID))

        assert [node["attempt"] for node in body["attempts"]] == [2, 3, 4, 5, 6]
        assert body["attempt_count"] == 6

    def test_the_reader_asks_for_every_name_in_one_projection(self):
        """do-NOT 8 — no new query and no new GSI. The lineage arrives on the
        ``step_logs`` read that was already there, widened.

        Renamed from ``..._all_four_names...`` by p48 task 6.2, which made it
        five: ``completed_at`` joined this projection rather than getting a read
        of its own, so do-NOT 8's guarantee is unchanged and only the count moved.

        Asserted because a mock honours no ``ProjectionExpression``: every value
        assertion above would still pass with the projection narrowed back to
        ``"step_logs"``, while a real table returned an ``Item`` with no lineage
        on it. This is the one assertion in the class that a hand-made fixture
        cannot fake past (``LGR-28b4d75-5``).
        """
        _set_approvals([])
        _set_executions_row()

        get_run_details(RUN_ID)

        kwargs = _patch_tables.executions.get_item.call_args.kwargs
        assert kwargs["ProjectionExpression"] == LINEAGE_PROJECTION
        assert kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}

    def test_a_failed_read_omits_all_three_and_still_returns_200(self):
        """``LGR-99050e6-2`` — absent is reported as unknown, never defaulted.

        ``"attempts" not in body`` rather than ``body["attempts"] == []``: an
        empty list here would claim the row records no attempts, which is a claim
        the failed read does not support.
        """
        _set_approvals([])
        _set_executions_read_failure(Exception("DynamoDB unavailable"))

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        for key in LINEAGE_KEYS:
            assert key not in body, f"{key} must be absent when the executions read fails, not defaulted"

    def test_a_read_row_with_no_lineage_is_a_different_body_from_a_failed_read(self):
        """The two cases the conditional presence exists to separate, asserted
        against each other rather than one at a time — the shape the
        ``load_metrics`` provenance contract above uses for the same reason.

        Row read, no lineage ⇒ the keys are there and empty. Read failed ⇒ the
        keys are gone. Collapsing the two is what would make ``attempt_count``
        unreadable: a caller could not tell "this run has no recorded attempts"
        from "we could not find out".
        """
        _set_approvals([])

        _set_executions_row(step_logs={})
        read_ok = _parse_response(get_run_details(RUN_ID))[1]

        _set_executions_read_failure(Exception("DynamoDB unavailable"))
        read_failed = _parse_response(get_run_details(RUN_ID))[1]

        assert set(read_ok.keys()) >= LINEAGE_KEYS
        assert LINEAGE_KEYS.isdisjoint(read_failed.keys())

    def test_the_body_carries_no_forward_identifier(self):
        """``retried_by_run_id`` is not on this response and must not appear.

        p38 deletes the forward identifier rather than making it resolvable: under
        the continuation model a retry runs under the original id, so there is no
        second run to point at. ``LGR-453f12d-5`` (**template / ratified**) is the
        rule — a link renders only from an identifier that resolves — and the
        frontend field is removed in the same change as the banner rebuild.
        """
        _set_approvals([])
        _set_executions_row(retried_by_run_id="run-contract-001", attempt_count=Decimal("1"))

        _, body = _parse_response(get_run_details(RUN_ID))

        assert "retried_by_run_id" not in body

    def test_load_outcome_and_the_lineage_keys_coexist(self):
        """The p37/p38 rebase point, asserted rather than assumed (§4.11).

        Both specs add fields to the same ``run_details`` dict. The collision was
        textual, not logical — p37 writes ``load_outcome`` as its own assignment
        and p38 writes three more — so one body carries all four. Pinned here so
        a future rebase of this file cannot drop one set while keeping the other
        green.
        """
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=5, rows_written=0)])
        _set_executions_row(attempts=[{"attempt": 1, "status": "FAILED"}], attempt_count=Decimal("1"))

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"
        assert not LOAD_OUTCOME_REQUIRED_FIELDS - set(body["load_outcome"].keys())
        assert not LINEAGE_KEYS - set(body.keys())
        assert body["attempt_count"] == 1

    def test_a_failed_lineage_read_does_not_cost_load_outcome(self):
        """The two field sets fail independently. ``load_outcome`` is derived from
        state rows the reader already holds, so the executions read failing must
        not take it with them — otherwise the lineage read becomes a single point
        of failure for p37's contract."""
        _set_approvals([])
        _set_state_items([_step_item("run_pipeline", sequence=10), _load_metrics_item(rows_read=5, rows_written=0)])
        _set_executions_read_failure(Exception("DynamoDB unavailable"))

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"
        assert body["load_outcome"]["rows_not_written"] == 5
        assert LINEAGE_KEYS.isdisjoint(body.keys())


# ===========================================================================
# Contract tests — the body satisfies every field ``RunDetail`` declares
# required (p48 task 6.4, R3.1's class / R3.2 / design §O9)
# ===========================================================================


class TestRunDetailsRequiredFieldContract:
    """The **frontend interface** is the oracle here, parsed at test time.

    Everything above pins fields this file already knew to name. That is exactly
    the blind spot ``LGR-28b4d75-5`` (**template / ratified**) describes: a
    contract written from the producer's output cannot detect the producer
    drifting, because the output is its own oracle. A hand-written key list has
    the same defect one step out — it can only catch what its author thought of.

    So this class does not name fields. It reads ``RunDetail`` out of
    ``dashboard-react/src/types.ts`` with ``helpers.read_interface`` and asserts
    the body carries every field the interface declares **without** ``?``. That
    is what ``R3.2`` asks for in words, and it is what found this task's defect:
    ``pipeline_key`` is declared ``pipeline_key: string`` — required — and
    ``get_run_details`` assigned it only inside the ``try`` around the
    pipelines-table read, so a run whose pipeline had been deleted got a body with
    no such key while ``RunMetadataPanel.tsx:48`` rendered it unguarded.

    Note what the fixture means for this class: ``mock_pipelines.get_item`` returns
    no ``Item``, so every test in this file has always been running the
    deleted-pipeline case. The assertion was missing, not the coverage.
    """

    #: Named interfaces reachable from ``RunDetail``'s field types. Passed to
    #: ``json_types`` so a bare identifier is never guessed at as "some object" —
    #: a renamed type has to fail here rather than be silently accepted.
    OBJECT_TYPES = frozenset({"StepDetail", "Approval", "RunAttempt", "LoadOutcome"})

    def _interface(self):
        from .helpers import load_interfaces

        types_path = Path(__file__).resolve().parents[3] / "dashboard-react" / "src" / "types.ts"
        return load_interfaces(types_path, "RunDetail")["RunDetail"]

    def test_every_required_field_of_the_frontend_interface_is_present(self):
        from .helpers import required_fields

        _set_approvals([])
        _set_executions_row()

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        missing = required_fields(self._interface()) - set(body)
        assert not missing, (
            f"RunDetail declares {sorted(missing)} required and the response does not send them. "
            f"Either the producer emits them or the interface marks them optional — a required "
            f"field the API omits reads as `undefined` in a component that has no reason to guard it."
        )

    def test_every_field_the_body_does_send_matches_its_declared_type(self):
        """The other half of the same oracle. A key that is present but of the
        wrong JSON type is the same silent failure as an absent one, and
        ``matches_json_type`` additionally refuses a Python ``bool`` for a field
        declared ``number`` — ``bool`` subclasses ``int``, so a bare
        ``isinstance`` would accept ``True`` for a count.
        """
        from .helpers import matches_json_type

        _set_approvals([])
        _set_executions_row(attempt_count=Decimal("2"))

        _, body = _parse_response(get_run_details(RUN_ID))

        for field, (_required, ts_type) in self._interface().items():
            if field not in body:
                continue
            assert matches_json_type(body[field], ts_type, self.OBJECT_TYPES), (
                f"{field}={body[field]!r} does not satisfy its declared type {ts_type!r}"
            )

    def test_pipeline_key_names_the_pipeline_when_its_row_is_gone(self):
        """The live case, stated concretely so the generic assertion above is not
        the only record of it. Measured on the validation account (2026-08-25): 10 of the 31
        ``original_pipeline_id`` values referenced by state rows have no row in the
        pipelines table, covering 13 runs.

        The value is not invented for the occasion — the pipelines read fetched the
        row AT key ``original_pipeline_id`` and returned that row's own
        ``pipeline_id``, so the answer was always the key it asked with. Same
        derivation ``handlers/files.py:37`` already publishes under this key name.
        """
        _set_approvals([])
        _set_executions_row()

        _, body = _parse_response(get_run_details(RUN_ID))

        assert body["pipeline_key"] == PIPELINE_ID

    def test_pipeline_name_is_not_promised_and_stays_withheld(self):
        """Why this task fixed one key of that block and not both. Nothing but the
        pipelines row establishes a pipeline's display name, so its absence is real
        not-established and omitting it is the correct treatment
        (``LGR-7561acb-7``, ``LGR-44d601e-1``). ``RunDetail`` does not declare it,
        so no consumer is promised it either.
        """
        from .helpers import required_fields

        _set_approvals([])
        _set_executions_row()

        _, body = _parse_response(get_run_details(RUN_ID))

        assert "pipeline_name" not in required_fields(self._interface())
        assert "pipeline_name" not in body


# ===========================================================================
# Contract test — ``schema_version`` is one attribute name across the producer
# and all three readers (p54 task 4.2, R4.2 / R4.3)
# ===========================================================================


class TestRunDetailsSchemaVersionKeyContract:
    """R4.3 in its literal form: the run detail carries the schema version under
    **the same attribute name the executions read uses**, and every surface that
    renders it reads that one name.

    Why a spelling assertion is worth a test. p54 task 4.2 added a fifth
    ``RunMetadataPanel`` cell whose expression is copied byte-for-byte from the
    two Run_List_Surfaces, so three readers now depend on one key produced in two
    places (``get_pipeline_executions`` and ``get_run_details``). If any one of
    the five spellings drifted, the failure is silent in the worst way: the cell
    renders the not-established em dash for a version the producer resolved, and
    nothing raises. ``tsc`` cannot catch it either — the two list surfaces read
    ``item.schema_version`` off types that declare the field optional, and
    ``PipelineOverviewPage``'s items are ``Record<string, unknown>``, so a
    misspelling there is ``undefined`` rather than a type error.

    The assertions are over **source text on both sides of the language
    boundary**, which is what ``LGR-28b4d75-5`` (**template / ratified**) asks
    for: a contract whose oracle is the producer's own output cannot detect the
    producer drifting. Same regex-the-source technique
    ``test_approval_window_cross_language.py`` uses, and for the same reason —
    TypeScript cannot import Python.
    """

    #: The one spelling. Written once here so a rename has exactly one place to
    #: fail rather than five places to be missed.
    KEY = "schema_version"

    @staticmethod
    def _frontend(*relative_parts: str) -> Path:
        path = Path(__file__).resolve().parents[3] / "dashboard-react" / "src" / Path(*relative_parts)
        assert path.is_file(), (
            f"{path} is missing. This contract derives its expectations from that file; if it moved, "
            f"repoint the constant rather than deleting the assertion."
        )
        return path

    @staticmethod
    def _producer() -> Path:
        path = (
            Path(__file__).resolve().parents[3]
            / "lambda"
            / "agentic_data_ingestion_dashboard_api"
            / "handlers"
            / "pipelines_runs.py"
        )
        assert path.is_file(), f"{path} is missing — the producer half of this contract has moved."
        return path

    def test_the_run_detail_interface_declares_the_key_and_declares_it_optional(self):
        """Both halves matter. The spelling is R4.3; the ``?`` is R4.4 — absence is
        how "could not be resolved" is expressed, so a required declaration would
        promise a value the producer deliberately omits.
        """
        from .helpers import load_interfaces

        interface = load_interfaces(self._frontend("types.ts"), "RunDetail")["RunDetail"]

        assert self.KEY in interface, (
            f"RunDetail does not declare {self.KEY!r}. The run detail page's Schema Version cell reads "
            f"it, and an undeclared field renders as `undefined` — i.e. as the em dash — for every run."
        )
        required, ts_type = interface[self.KEY]
        assert not required, (
            f"RunDetail declares {self.KEY} required. get_run_details omits the key when the version "
            f"cannot be resolved (R4.4), so a required declaration promises a value the producer does "
            f"not send."
        )
        assert ts_type == "number", (
            f"RunDetail declares {self.KEY} as {ts_type!r}. The producer emits the version map's "
            f"ordinal, and both Run_List_Surfaces render it as a number."
        )

    def test_the_executions_interface_uses_the_same_spelling(self):
        """R4.3's "same attribute name the executions read uses", asserted against
        the executions reader's own interface rather than against a transcribed
        literal.
        """
        from .helpers import load_interfaces

        interface = load_interfaces(self._frontend("hooks", "use-executions.ts"), "PipelineExecution")[
            "PipelineExecution"
        ]

        assert self.KEY in interface, (
            f"PipelineExecution does not declare {self.KEY!r}, so the run list and the run detail no "
            f"longer name one attribute. R4.3 requires they do."
        )
        assert interface[self.KEY][0] is False, (
            f"PipelineExecution declares {self.KEY} required; the executions read omits it for a hash "
            f"that is not in the version map."
        )

    @pytest.mark.parametrize(
        "surface,receiver",
        [
            (("pages", "ExecutionsPage.tsx"), "item"),
            (("pages", "PipelineOverviewPage.tsx"), "item"),
            (("components", "run-detail", "RunMetadataPanel.tsx"), "runDetail"),
        ],
    )
    def test_all_three_render_surfaces_read_that_one_key(self, surface, receiver):
        """The two run lists and the run detail panel, asserted together. p54 task
        4.2 copied the list surfaces' expression into the panel byte-for-byte, so
        this is the assertion that they stayed copies: each surface guards the same
        key with ``!= null`` and falls back to U+2014 EM DASH.
        """
        source = self._frontend(*surface).read_text(encoding="utf-8")

        guard = f"{receiver}.{self.KEY} != null"
        assert guard in source, (
            f"{surface[-1]} does not contain {guard!r}. All three surfaces render the schema version "
            f"from one key under one guard (p54 R4.2); a divergence here renders the not-established "
            f"em dash for a version the producer resolved."
        )
        # The em dash is asserted on the same line as the guard so that a surface
        # keeping the key but swapping the fallback for '', '0' or 'N/A' fails —
        # that is a third spelling of "not established" and LGR-7561acb-7 refuses
        # it.
        guard_line = next(line for line in source.splitlines() if guard in line)
        assert "\u2014" in guard_line, (
            f"{surface[-1]}'s schema-version cell does not fall back to U+2014 EM DASH. R4.2 asks for "
            f"the SAME not-established indication the other surfaces use, not a new one."
        )

    def test_the_producer_writes_that_one_key_on_both_read_paths(self):
        """The Python half. ``get_pipeline_executions`` and ``get_run_details`` are
        two functions deriving one fact, and R4.3 is the requirement that they
        publish it under one name. Asserted over the producer's source because the
        run-detail derivation is bounded — a page-capped or deadline-truncated walk
        legitimately produces no key — so no single seeded response can witness
        both writers.
        """
        import re

        source = self._producer().read_text(encoding="utf-8")

        assignments = re.findall(rf'\[\s*[\'"]{re.escape(self.KEY)}[\'"]\s*\]\s*=', source)
        assert len(assignments) >= 2, (
            f"pipelines_runs.py assigns {self.KEY!r} {len(assignments)} time(s); both the executions "
            f"read and the run-detail read must publish it under that name (R4.3)."
        )
        assert f'"{self.KEY}"' in source, f"{self.KEY!r} is not spelled as a literal key in the producer."
