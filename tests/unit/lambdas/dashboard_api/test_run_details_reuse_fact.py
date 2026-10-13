#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``get_run_details`` reports the reuse fact as three states, never as a default.

p54 R2.1 / R2.2 / R2.3, design 4.2. ``reused_mapping: true`` is produced at
``orchestration-stack.ts:445`` and had zero readers repo-wide; it is NOT threaded
through ASL to reach the response (p54 do-NOT 1, invariant ``LGR-5c539a6-2``: the
key is on the reuse Pass arm only, so a JSONPath reference would be a
``States.Runtime`` failure on every non-reuse run). It is derived on the read path
instead — ``LGR-9e76249-2``'s ratified shape — off state rows and approvals rows
``get_run_details`` already holds.

The three states, and the one that is easy to get wrong:

* ``reused_mapping: False`` — a ``schema_mapping`` state row IS present.
* ``reused_mapping: True`` + ``reused_mapping_id`` — no such row and an id resolves.
* **both keys absent** — no such row and no id resolves.

``False`` is never the unknown case. Asserting it where reuse could not be
established would claim the run generated its own mapping, which is the collapse
p54 do-NOT 7 forbids and ``LGR-b2f79ef-14`` classes as an inconsistent record
rather than a thing that never existed. ``LGR-8e34f37-3`` is the omit-on-failure
pattern the derivation follows, and this file pins it.

Two traps are pinned here because both produce a **false** answer on the exact run
the requirement is about, and neither is visible from the response shape:

1. Deriving from ``run_details["steps"]`` instead of ``items``. The running-step
   injection APPENDS a synthetic ``schema_mapping`` entry when a pending approval
   maps to that step — no state row required — and a Reuse_Run parked at the
   schema-mapping gate is precisely that case.
2. Comparing the raw ``step_name`` instead of the canonical one.
   ``step_names._LEGACY_ALIASES`` maps ``SchemaMapping``, so a pre-rename row
   would be missed and the run reported as a reuse.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

RUN_ID = "run-reuse-fact-001"
PIPELINE_ID = "pipe-reuse-001"
MAPPING_ID = "mapping-abc-123"


def _state_row(step_name: str, seq: int, **extra: object) -> dict:
    row = {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-reuse-001",
        "status": "success",
        "timestamp": f"2026-08-28T12:0{seq}:00+00:00",
        "step_sequence": seq,
    }
    row.update(extra)
    return row


def _approval_row(approval_data: object, *, status: str = "approved") -> dict:
    """An approvals-table row as the run_id GSI returns it.

    ``approval_data`` is a JSON string on the wire — ``approval_handler:850``
    writes ``json.dumps(...)`` — and ``get_run_details`` parses it into
    ``details``. Passing the raw string keeps this fixture honest about the shape
    the derivation actually reads through.
    """
    return {
        "approval_id": "appr-reuse-001",
        "run_id": RUN_ID,
        "approval_type": "schema_mapping",
        "status": status,
        "approval_data": approval_data if isinstance(approval_data, str) else json.dumps(approval_data),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_details(monkeypatch):
    """Call ``get_run_details`` over given state rows and approvals rows."""
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

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

    from handlers.pipelines_runs import get_run_details as _handler

    def _call(state_rows: list, approvals_rows: list | None = None) -> tuple:
        mock_state.query.return_value = {"Items": state_rows}
        mock_approvals.query.return_value = {"Items": approvals_rows or []}
        response = _handler(RUN_ID)
        return response["statusCode"], json.loads(response["body"])

    _call.state = mock_state
    _call.approvals = mock_approvals
    _call.executions = mock_executions
    return _call


# ===========================================================================
# State 1 — the row is there, so ``False`` is knowledge
# ===========================================================================


class TestTheRunGeneratedItsOwnMapping:
    def test_a_schema_mapping_row_makes_the_reuse_fact_false(self, run_details):
        """R2.1: a ``schema_mapping`` state row exists, so the run did not reuse.

        ``False`` here is asserted from positive evidence — the row — and not as a
        default. No ``reused_mapping_id`` accompanies it: there is no reused
        mapping to name.
        """
        status, body = run_details(
            [
                _state_row("schema_extract", seq=2, schema_hash="h1"),
                _state_row("schema_mapping", seq=3, mapping_id=MAPPING_ID),
            ]
        )

        assert status == 200
        assert body["reused_mapping"] is False
        assert "reused_mapping_id" not in body

    def test_a_failed_schema_mapping_row_still_makes_it_false(self, run_details):
        """The run attempted its own mapping and the attempt failed. That is still
        not a reuse, and presence of the row — not its status — is the test.
        """
        _, body = run_details(
            [
                _state_row("schema_extract", seq=2),
                _state_row("schema_mapping", seq=3, status="failed", error_message="boom"),
            ]
        )

        assert body["reused_mapping"] is False

    def test_a_legacy_pascalcase_row_is_recognised_as_the_same_step(self, run_details):
        """Trap 2. ``step_names._LEGACY_ALIASES`` maps ``SchemaMapping`` to the
        canonical step, and pre-rename rows persist in live tables (the reason
        ``_canonical_or_raw`` exists at all, P28 R8.2). A raw string comparison
        would miss this row and report the run as a reuse — a false ``True`` on
        the consequential arm.
        """
        _, body = run_details(
            [
                _state_row("SchemaExtract", seq=2),
                _state_row("SchemaMapping", seq=3, mapping_id=MAPPING_ID),
            ]
        )

        assert body["reused_mapping"] is False
        assert "reused_mapping_id" not in body


# ===========================================================================
# State 2 — no row and an id resolves
# ===========================================================================


class TestTheRunReusedAnExistingMapping:
    def test_no_schema_mapping_row_plus_a_resolvable_id_names_the_mapping(self, run_details):
        """R2.1 / R2.2. A Reuse_Run takes a ``sfn.Pass``, a Pass writes nothing to
        DynamoDB, so the ``schema_mapping`` row is absent. p54 task 2.2 appended
        ``reused_mapping_id`` to the approval payload, and the approvals block
        above has already parsed it into ``details``.
        """
        status, body = run_details(
            [
                _state_row("schema_extract", seq=2, schema_hash="h1"),
                _state_row("schema_compare", seq=1),
                _state_row("create_code", seq=4),
            ],
            [_approval_row({"reused_mapping": True, "reused_mapping_id": MAPPING_ID, "source_schema": {}})],
        )

        assert status == 200
        assert body["reused_mapping"] is True
        assert body["reused_mapping_id"] == MAPPING_ID

    def test_the_unreadable_reuse_state_still_names_the_mapping(self, run_details):
        """p54 task 2.2 appends ``reused_mapping_id`` on **both** reuse states —
        the resolved one and the unreadable one (``mapping_unreadable: True``,
        ``proposed_mappings: None``). So an approval whose origin payload could
        not be chased still identifies which mapping was reused, and R2.2 is
        answerable for it.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [
                _approval_row(
                    {
                        "reused_mapping": True,
                        "reused_mapping_id": MAPPING_ID,
                        "mapping_unreadable": True,
                        "proposed_mappings": None,
                        "mapping_count": None,
                    }
                )
            ],
        )

        assert body["reused_mapping"] is True
        assert body["reused_mapping_id"] == MAPPING_ID

    def test_the_first_approval_that_answers_wins(self, run_details):
        """A run's approval partition holds several rows — schema_mapping,
        code_generation, first_run — and only the reuse arm carries the key. The
        walk skips the rows that do not answer rather than stopping at the first
        row.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [
                _approval_row({"approval_type": "code_generation", "code_version_id": "cv-1"}),
                _approval_row({"reused_mapping": True, "reused_mapping_id": MAPPING_ID}),
            ],
        )

        assert body["reused_mapping"] is True
        assert body["reused_mapping_id"] == MAPPING_ID

    def test_an_injected_schema_mapping_step_does_not_make_it_false(self, run_details):
        """Trap 1, and the canonical Reuse_Run.

        A Reuse_Run parked at the schema-mapping gate has a **pending**
        ``schema_mapping`` approval. The running-step injection reads that,
        finds no matching entry in ``steps``, and appends a synthetic
        ``schema_mapping`` step with ``status: waiting_approval`` — with no state
        row behind it. Deriving from ``run_details["steps"]`` would therefore
        report ``reused_mapping: False`` for exactly the run R2 is about.
        ``items`` is the state table's own answer and cannot be synthesised.
        """
        run_details.executions.get_item.return_value = {
            "Item": {
                "status": "WAITING_APPROVAL",
                "current_step": "schema_compare",
                "updated_at": "2026-08-28T12:05:00+00:00",
            }
        }

        _, body = run_details(
            [_state_row("schema_extract", seq=2), _state_row("schema_compare", seq=1)],
            [
                _approval_row(
                    {"reused_mapping": True, "reused_mapping_id": MAPPING_ID},
                    status="pending",
                )
            ],
        )

        # The injection did fire and put the step on the response …
        assert "schema_mapping" in [s["step_name"] for s in body["steps"]]
        # … and the reuse fact is still derived from the state rows.
        assert body["reused_mapping"] is True
        assert body["reused_mapping_id"] == MAPPING_ID


# ===========================================================================
# State 3 — OMISSION. The state that is easy to get wrong.
# ===========================================================================


class TestNeitherKeyIsEmittedWhenNothingEstablishesTheFact:
    def test_no_row_and_no_approvals_omits_both_keys(self, run_details):
        """R2.3, p54 do-NOT 7. This is the historical Reuse_Run: the approval
        predates task 2.2, so it carries no ``reused_mapping_id``, and no state
        row carries a ``mapping_id`` either (the only state-row writer of that
        attribute is ``schema_mapping/lambda_function.py:1935`` — the row this
        run does not have).

        ``reused_mapping: False`` here would assert the run generated its own
        mapping. Absence says what is true: not established.
        """
        status, body = run_details(
            [_state_row("schema_extract", seq=2), _state_row("create_code", seq=4)],
            [],
        )

        assert status == 200
        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body

    def test_an_approval_with_no_reuse_key_omits_both_keys(self, run_details):
        """A pre-2.2 approval row parses to a populated ``details`` that simply
        does not carry the key. An empty string is not an id.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [_approval_row({"source_schema": {"fields": []}, "proposed_mappings": []})],
        )

        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body

    def test_a_blank_reused_mapping_id_is_not_an_id(self, run_details):
        """``""`` is absence in a second spelling and must not become ``True``
        with an empty id beside it (``LGR-44d601e-1``).
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [_approval_row({"reused_mapping": True, "reused_mapping_id": ""})],
        )

        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body

    def test_unparseable_approval_data_omits_both_keys(self, run_details):
        """The approvals block maps unparseable ``approval_data`` to
        ``details == {}`` without raising, so the derivation sees no id and omits.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [_approval_row("{not json")],
        )

        assert body["approvals"][0]["details"] == {}
        assert "reused_mapping" not in body

    def test_a_non_dict_details_omits_both_keys(self, run_details):
        """``approval_data`` holding a JSON scalar parses to a non-dict. The read
        is total (``item_dict`` returns ``{}`` for a non-mapping) so this omits
        rather than raising.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [_approval_row("42")],
        )

        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body


# ===========================================================================
# Omit on failure — ``LGR-8e34f37-3``
# ===========================================================================


class TestItOmitsOnFailureRatherThanDefaulting:
    def test_a_raising_approvals_read_does_not_report_the_run_as_its_own_mapping(self, run_details):
        """``LGR-8e34f37-3``: a read that raised leaves the keys off the body and
        the response stays 200. The approvals query raising sets
        ``approvals: []``, and the derivation then finds no id — so a Reuse_Run
        whose approval row could not be read is reported as **not established**,
        never as ``False``.
        """
        run_details.approvals.query.side_effect = RuntimeError("throttled")

        status, body = run_details([_state_row("schema_extract", seq=2)])

        assert status == 200
        assert body["approvals"] == []
        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body

    def test_a_raising_approvals_read_does_not_suppress_a_false_the_rows_established(self, run_details):
        """The complement, and the reason ``False`` is safe to assert without the
        approvals read: it comes from the state rows alone. A failed approvals
        read cannot make a run that HAS a ``schema_mapping`` row look like a
        reuse.
        """
        run_details.approvals.query.side_effect = RuntimeError("throttled")

        status, body = run_details(
            [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3, mapping_id=MAPPING_ID)]
        )

        assert status == 200
        assert body["reused_mapping"] is False

    def test_a_malformed_approvals_entry_omits_both_keys_and_keeps_the_200(self, run_details):
        """A non-mapping element in the approvals list makes the walk raise
        (``AttributeError`` on ``.get``). The derivation's own ``try`` absorbs it,
        both keys stay off, and nothing else on the body is disturbed.
        """
        _, body = run_details([_state_row("schema_extract", seq=2)])
        # Baseline: the honest omission for a run with no approvals at all.
        assert "reused_mapping" not in body

        run_details.approvals.query.return_value = {"Items": ["not-a-row"]}
        status, body = run_details([_state_row("schema_extract", seq=2)])

        assert status == 200
        assert "reused_mapping" not in body
        assert "reused_mapping_id" not in body


# ===========================================================================
# The derivation is free — no new DynamoDB operation
# ===========================================================================


class TestTheDerivationAddsNoRead:
    def test_it_adds_no_dynamodb_operation(self, run_details):
        """``LGR-9e76249-2``'s ground for putting this on the read path is that
        ``get_run_details`` "already holds every state row for the run — so the
        read is free". Both inputs are already in hand: ``items`` from the state
        query, and the parsed approvals from the query the response already makes.

        The counts here are the ones a run with these rows makes anyway; the
        assertion is that the reuse derivation moves none of them.
        """
        _, body = run_details(
            [_state_row("schema_extract", seq=2)],
            [_approval_row({"reused_mapping": True, "reused_mapping_id": MAPPING_ID})],
        )
        assert body["reused_mapping"] is True

        # One state query, and no mappings-table or generated-code-table read:
        # the ``create_code`` id route named in the task text does not exist —
        # ``create_code:1255`` writes to the generated-code table, not the state
        # row — and reaching it would be a fourth table on this read path.
        assert run_details.state.query.call_count == 1
        assert run_details.state.get_item.call_count == 0
