# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the ``?pipeline_id=`` approvals response (p51 C1, task 2.2).

``get_approvals`` gained an optional ``?pipeline_id=`` parameter served by a keyed
Query on ``pipeline-status-index`` (p51 task 2.1). The response shape was
**deliberately left unchanged** — a bare ``created_at``-descending
``Approval[]``, ``Content-Type: application/json``, and a 200 with ``[]`` for no
matches rather than a 404 — precisely so the approvals contracts already in this
directory keep applying to the new path as written.

This file is the assertion that they do, against the **existing** oracles and
nothing new:

* ``schemas.py:APPROVAL_SCHEMA``, unmodified. The two suites that already use it
  (``test_api_contract_conformance_prop.py``,
  ``test_api_contract_props.py``) validate ``get_approvals``' items **one at a
  time** rather than wrapping them in an array schema, and that is copied here
  rather than improved on — an array wrapper would have been a new schema, which
  R1.3's task text forbids.
* the frontend ``Approval`` interface in ``dashboard-react/src/types.ts``, parsed
  at test time by the shared reader in ``helpers.py``.

**Why both, and why the schema alone would not do.** ``LGR-28b4d75-5``
(**template / ratified**) rejects a hand-transcribed field list "*or … a JSON
schema*" as an oracle, because a test written from the producer's output cannot
detect the producer drifting — the output is its own oracle. ``APPROVAL_SCHEMA``
is exactly such a transcription (``schemas.py`` says so: "Derived from the
frontend TypeScript interfaces"), so it is used because the task names it, and
the parsed interface is used beside it because that is what can actually catch a
divergence. They already disagree in one direction, recorded below rather than
reconciled: the schema declares ``has_task_token`` and ``Approval`` does not.
That gap predates this task and is not its subject.

**The parser is the shared one; a local copy is forbidden.** ``LGR-99050e6-5``
(**template / ratified**, scope ``tests/contracts/api_responses/**``) extracted
the reader into ``helpers.py`` rather than copying it per endpoint, "because the
copies would have diverged immediately … and the next contract test would
inherit whichever it happened to copy". ``LGR-cd58706-3`` (**template /
ratified**) applied that to a later consumer and called a local copy "the eighth
parser and the first divergence". So this file imports ``load_interfaces``,
``required_fields`` and ``matches_json_type`` and adds no parser. No widening of
any of them was needed, verified by running the real reader against the file:
``Approval`` is a flat interface of ``field[?]: type;`` lines, so every line
parses, and every field this endpoint actually emits is declared ``string`` or
``number``, both of which the reader already maps.

**Deliberately not asserted here**, so each omission reads as a statement:

* the ``IndexName`` and the key condition. Those are P1's subject and are pinned
  structurally at
  ``tests/unit/lambdas/dashboard_api/test_approvals_pipeline_key.py``, including
  the ``"PENDING"``-vs-``"pending"`` trap the two status vocabularies create.
  Re-asserting them from the contract layer would duplicate a unit assertion
  without strengthening it.
* ``details``. ``Approval`` declares it ``Record<string, unknown>``, which the
  shared reader does not model — but no fixture here carries it, because
  ``get_approvals`` returns raw approvals rows and ``details`` is assembled by a
  different read (``pipelines_runs.py``' run detail). Teaching the reader a type
  no row on this path emits would be widening it for a hypothetical.

Validates: Requirements R1.3 — Design: C1
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jsonschema
import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time,
# before any fixture can run. Every table handle is replaced by a mock below;
# nothing here touches AWS.
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
LAMBDA_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api"
SHARED_DIR = REPO_DIR / "shared"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import approvals as _approvals
from handlers import common as _common
from handlers.approvals import get_approvals

from .helpers import load_interfaces, matches_json_type, required_fields
from .schemas import APPROVAL_SCHEMA

#: The consumer this contract is pinned against, beside ``APPROVAL_SCHEMA``.
#: ``load_interfaces`` asserts the file exists and that the interface parsed
#: non-empty, so a moved or renamed type fails loudly rather than quietly
#: reducing every assertion below to a vacuous pass.
FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "types.ts"
APPROVAL_TYPE = "Approval"

#: Live pipeline keys, measured on the validation account / us-west-2 (p51 task 1.1), so the
#: fixtures are shaped like rows the endpoint actually serves rather than like
#: invented ones. ``helloworld-f9a76ce9`` holds 11 approvals across three
#: statuses; ``newtest-ab2a08a0`` holds the account's other pending approval.
PIPELINE_KEY = "helloworld-f9a76ce9"
OTHER_PIPELINE_KEY = "newtest-ab2a08a0"


def _approval_row(
    approval_id: str,
    *,
    status: str,
    created_at: str | None,
    pipeline_id: str = PIPELINE_KEY,
    run_id: str = "run-2026-08-22-12-43-58-c3e56b8f",
    approval_type: str = "schema_mapping",
) -> dict[str, Any]:
    """An approvals row as ``pipeline-status-index`` returns it.

    ``ProjectionType.ALL`` on that index, verified live (p51 task 1.1: a keyed
    Query with no ``ProjectionExpression`` returns 16 attributes), so the row is
    the whole item and not a projection subset.

    ``pipeline_key`` is deliberately **absent**: DynamoDB stores only
    ``pipeline_id`` and ``get_approvals`` derives the frontend's key name from it.
    The frontend declares ``pipeline_key`` required, so that normalisation is
    load-bearing on this path and is asserted below rather than assumed.

    ``created_at=None`` omits the key entirely — an undated row, which is
    reachable on the partition-only path for the first time (see the undated-row
    class at the bottom of this file). It is a parameter rather than a separate
    factory so the dated and undated rows cannot drift in any other respect.

    The timeout trio is included because both live pending approvals carry it
    (p51 task 1.2: ``timeout_occurred: true`` with ``timeout_behavior: "manual"``,
    which is the designed steady state for a timed-out-but-still-pending
    approval). Neither schema declares those keys and neither forbids extras, so
    they are here to keep the fixture honest, not to be asserted.
    """
    row: dict[str, Any] = {
        "approval_id": approval_id,
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "approval_type": approval_type,
        "status": status,
        "has_task_token": True,
        "timeout_at": "2026-08-25T12:44:35.411868+00:00",
        "timeout_behavior": "manual",
        "timeout_occurred": True,
    }
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _parse(response: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
    return response["statusCode"], json.loads(response["body"]), response["headers"]


@pytest.fixture
def approvals_table(monkeypatch) -> MagicMock:
    """Mock the one table handle ``get_approvals`` reads.

    ``?pipeline_id=`` issues exactly **one** Query whether or not ``?status=`` is
    also given — the key condition carries both parts, so there is no second read
    to branch on. A single ``query.return_value`` therefore serves both shapes,
    unlike the no-parameter path which enumerates ``_APPROVAL_STATUSES``.
    """
    table = MagicMock()
    table.query.return_value = {"Items": []}
    monkeypatch.setattr(_common, "approvals_table", table)
    monkeypatch.setattr(_approvals, "approvals_table", table)
    return table


@pytest.fixture(scope="module")
def approval_interface() -> dict[str, tuple[bool, str]]:
    """The frontend ``Approval`` interface, parsed at test time by the shared reader."""
    return load_interfaces(FRONTEND_TYPE_FILE, APPROVAL_TYPE)[APPROVAL_TYPE]


class TestPipelineKeyedResponseAgainstTheExistingSchema:
    """The ``?pipeline_id=`` body validates against ``APPROVAL_SCHEMA`` unmodified.

    Both shapes of the new parameter are covered, because they read the index
    differently: with ``?status=`` the key condition is the index's full key, and
    without it the partition alone — which is the shape that admits every status
    of the pipeline at once, and the only shape whose result arrives ordered by
    something other than ``created_at`` (the index's range key is ``status``).
    """

    def test_the_partition_only_response_validates_item_by_item(self, approvals_table):
        """``?pipeline_id=`` alone: every status of one pipeline, all conforming.

        Three statuses in one response, which the ``?status=`` path cannot
        produce. Item-by-item validation, matching the two suites that already
        govern this handler.
        """
        approvals_table.query.return_value = {
            "Items": [
                _approval_row("hw_schema_mapping_1787402675", status="pending", created_at="2026-08-22T12:44:35Z"),
                _approval_row("hw_schema_mapping_1787300000", status="approved", created_at="2026-08-21T09:15:00Z"),
                _approval_row("hw_schema_mapping_1787200000", status="rejected", created_at="2026-08-20T18:02:11Z"),
            ]
        }

        status, body, headers = _parse(get_approvals({"pipeline_id": PIPELINE_KEY}))

        assert status == 200
        assert isinstance(body, list), "the endpoint returns a bare array, not an envelope"
        assert len(body) == 3
        for approval in body:
            jsonschema.validate(approval, APPROVAL_SCHEMA)
        assert headers["Content-Type"] == "application/json"

    def test_the_pipeline_and_status_response_validates_item_by_item(self, approvals_table):
        """``?pipeline_id=&status=pending``: the index's full key, one status.

        The approvals vocabulary is lowercase — ``"pending"``, not ``"PENDING"``
        — and reaches the key condition exactly as given, so this is also the
        shape the frontend hook issues (``use-approvals.ts``:
        ``?pipeline_id=<key>&status=pending``).
        """
        approvals_table.query.return_value = {
            "Items": [
                _approval_row("hw_schema_mapping_1787402675", status="pending", created_at="2026-08-22T12:44:35Z"),
                _approval_row(
                    "nt_schema_mapping_1787402782",
                    status="pending",
                    created_at="2026-08-22T12:46:23Z",
                    pipeline_id=OTHER_PIPELINE_KEY,
                    run_id="run-2026-08-22-12-46-00-8032490a",
                ),
            ]
        }

        status, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY, "status": "pending"}))

        assert status == 200
        assert isinstance(body, list)
        assert len(body) == 2
        for approval in body:
            jsonschema.validate(approval, APPROVAL_SCHEMA)
            assert approval["status"] == "pending"

    def test_no_matches_is_a_200_with_an_empty_array(self, approvals_table):
        """An empty result is still the same contract: 200, ``[]``, JSON.

        Never a 404 — the same stance ``get_pipeline_executions`` takes for an
        empty filtered match. Asserted here rather than left to the unit layer
        because "zero approvals" is the state the overview card renders most
        often, and an envelope or a 404 would break the consumer as surely as a
        malformed item would.
        """
        approvals_table.query.return_value = {"Items": []}

        status, body, headers = _parse(get_approvals({"pipeline_id": "no-such-pipeline-00000000"}))

        assert status == 200
        assert body == []
        assert headers["Content-Type"] == "application/json"


class TestPipelineKeyedResponseAgainstTheFrontendInterface:
    """The stronger oracle: ``Approval`` as the browser declares it.

    ``APPROVAL_SCHEMA`` is a transcription and cannot detect the producer
    drifting away from its consumer (``LGR-28b4d75-5``). This class does not name
    fields — it reads them out of ``types.ts`` at test time, so adding a required
    field to the interface fails here until the producer supplies it, and
    renaming one on the wire fails until the interface follows.
    """

    def test_every_required_field_of_the_frontend_interface_is_present(self, approvals_table, approval_interface):
        approvals_table.query.return_value = {
            "Items": [
                _approval_row("hw_schema_mapping_1787402675", status="pending", created_at="2026-08-22T12:44:35Z")
            ]
        }

        status, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY, "status": "pending"}))

        assert status == 200
        missing = required_fields(approval_interface) - set(body[0])
        assert not missing, (
            f"Approval declares {sorted(missing)} required and the ?pipeline_id= response does not send "
            f"them. Either the producer emits them or the interface marks them optional — a required "
            f"field the API omits reads as `undefined` in a component with no reason to guard it."
        )

    def test_every_field_the_body_does_send_matches_its_declared_type(self, approvals_table, approval_interface):
        """The other half of the same oracle.

        A key present but of the wrong JSON type is the same silent failure as an
        absent one. ``matches_json_type`` additionally refuses a Python ``bool``
        for a field declared ``number`` — ``bool`` subclasses ``int``, so a bare
        ``isinstance`` would accept ``True`` for ``response_time_ms``.
        """
        approvals_table.query.return_value = {
            "Items": [
                _approval_row("hw_schema_mapping_1787402675", status="pending", created_at="2026-08-22T12:44:35Z")
            ]
        }

        _, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY}))

        for field, (_required, ts_type) in approval_interface.items():
            if field not in body[0]:
                continue
            assert matches_json_type(body[0][field], ts_type), (
                f"{field}={body[0][field]!r} does not satisfy its declared type {ts_type!r}"
            )

    def test_pipeline_key_is_supplied_by_normalisation_not_by_the_stored_row(self, approvals_table, approval_interface):
        """``pipeline_key`` is required by the interface and absent from the table.

        The index's partition key is ``pipeline_id``; ``pipeline_key`` exists only
        because ``get_approvals`` derives it. Stated as its own assertion because
        the generic required-field check above would report it as one missing name
        among several, and this one is a rename rather than an omission — the fix
        for it is not "emit another field".
        """
        stored = _approval_row("hw_schema_mapping_1787402675", status="pending", created_at="2026-08-22T12:44:35Z")
        assert "pipeline_key" not in stored, "the fixture must not pre-supply what the handler derives"
        approvals_table.query.return_value = {"Items": [stored]}

        _, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY}))

        assert "pipeline_key" in required_fields(approval_interface)
        assert body[0]["pipeline_key"] == PIPELINE_KEY


class TestTheUndatedRowInteraction:
    """A finding, pinned rather than left in prose.

    p51 task 2.1 decided that an approvals row carrying no ``created_at`` is
    **returned** and sorts **last** rather than being dropped — last place
    because the order claims recency and an undated row's recency is unknown, and
    not dropped because dropping it would cost the row its Approve/Reject
    affordance, which is the defect R1.1 exists to fix.

    That row is reachable on this path **for the first time**:
    ``status-created-index`` is sparse on ``created_at``, so an undated row is
    absent from it by ``LGR-7d19eca-2`` (**invariant** — a GSI indexes only items
    carrying both key attributes), while ``pipeline-status-index`` keys on
    ``(pipeline_id, status)`` and does index it.

    Both oracles declare ``created_at`` required, so such a row does **not**
    validate. That is a fact about the **row**, not about C1's response shape —
    the endpoint's contract is "a bare array of approvals rows" and it is
    honoured either way — so neither ``APPROVAL_SCHEMA`` nor ``Approval`` is
    relaxed to accommodate it. Live evidence that this is an edge and not the
    common case: a ``Scan`` for ``attribute_not_exists(created_at)`` over all 87
    rows of the live approvals table returned **0** (p51 task 1.1 §4). The
    interaction is asserted here so a future reader finds it as a test rather
    than as an absence.
    """

    @pytest.fixture
    def mixed_dating(self, approvals_table) -> MagicMock:
        approvals_table.query.return_value = {
            "Items": [
                _approval_row("hw-dated", status="pending", created_at="2026-08-22T12:44:35Z"),
                _approval_row("hw-undated", status="pending", created_at=None),
            ]
        }
        return approvals_table

    def test_both_oracles_declare_created_at_required(self, approval_interface):
        """Establishes why the row below fails, from the oracles rather than from memory."""
        assert "created_at" in APPROVAL_SCHEMA["required"]
        assert "created_at" in required_fields(approval_interface)

    def test_the_undated_row_is_returned_and_sorts_last(self, mixed_dating):
        """2.1's decision, observed through the response rather than the sort key."""
        status, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY}))

        assert status == 200
        assert [row["approval_id"] for row in body] == ["hw-dated", "hw-undated"]
        assert "created_at" not in body[-1]

    def test_the_dated_row_validates_and_the_undated_one_does_not(self, mixed_dating):
        """The interaction, stated as an asymmetry between two rows of one response.

        If this ever starts passing for the undated row, something has either
        begun synthesising a ``created_at`` — which would assert a recency nobody
        observed — or relaxed a schema that two consumers rely on. Both are
        changes worth failing a test.
        """
        _, body, _ = _parse(get_approvals({"pipeline_id": PIPELINE_KEY}))
        dated, undated = body

        jsonschema.validate(dated, APPROVAL_SCHEMA)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(undated, APPROVAL_SCHEMA)
