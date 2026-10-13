# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the ``test_failure`` approval payload against the shape the UI reads.

This is the test that would have caught Defect 1 (``bugfix.md`` 1.1-1.3). The
producer and the reader disagreed on two keys at once —
``details.test_result`` vs ``details.report``, and per-check ``detail`` vs
``message``/``details`` — and the per-check table's unit test could not detect it
because that test's fixture was transcribed from the *reader's* shape
(``ApprovalArtifact.test.tsx:147-172``). A fixture built from either side alone is
that side's own oracle.

So this file pins the two sides against each other, and takes **neither** on
trust:

* the **producer** half is executed, not transcribed. ``aggregate_check_results``
  (``test_code/lambda_function.py:177``) builds the real per-check list — the
  ``detail`` key at ``:234``, ``:244``, ``:253``, ``:261`` and ``:273`` — the real
  ``test_result`` envelope of ``{status, checks, report_s3_uri}``
  (``:940-944``) goes into ``get_approval_data("test_failure", ...)``
  (``approval_handler/lambda_function.py:248-276``), and the result is carried
  across the wire hop the dashboard actually performs: stored as a JSON string in
  ``approval_data`` (``approval_handler/lambda_function.py:373``) and parsed back
  into ``details`` (``dashboard_api/handlers/pipelines_runs.py:626-633``).
* the **reader** half is PARSED at test time out of::

      dashboard-react/src/components/approvals/ApprovalArtifact.tsx

  ``TestFailureArtifact`` reads its keys through ``??`` chains rather than through
  a declared interface, so what is parsed here is the chains themselves: which
  keys are accepted, and in which order. The assertions then require the
  producer to satisfy the **first** link of each chain, because the later links
  exist only as back-compatibility for payloads that carry them (2.2) — a
  producer that only satisfied a fallback would be drift that happens to render.

Deriving expectations from the consumer rather than from the producer's own
output is the ratified pattern in ``LGR-28b4d75-5`` (template provenance), which
established it for ``/cost-recommendations`` after the same class of silent shape
drift. The parse is strict for the same reason it is there: a pattern that stops
matching **fails**, because a chain that silently parsed as empty would make
every assertion below trivially true.

Also pinned here: the empty-checks and absent-report payloads still produce the
neutral note rather than a crash (3.13).

Validates: Requirements 2.1, 2.2; 3.13
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path / environment setup (before importing either Lambda module)
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
SHARED_DIR = REPO_DIR / "shared"
TEST_CODE_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_test_code"
APPROVAL_HANDLER_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_approval_handler"

#: ``shared/`` is the Lambda layer; ``test_code/`` is on the path for its own
#: sibling modules (``review_verdict`` — the effective-verdict source the
#: ``ai_review_verdict`` check's ``detail`` is built from).
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(TEST_CODE_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

#: Heavy / AWS-touching imports the two handlers pull at module scope. Stubbed so
#: both load in-process; nothing under test lives in them.
_STUBS = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "notification_service": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
}


def _load(module_name: str, path: Path) -> Any:
    """Load one Lambda ``lambda_function.py`` under a private module name.

    Both handlers are called ``lambda_function``, so they are loaded from an
    explicit file location rather than by import — otherwise the second one wins
    and this test would silently exercise a single side of the wire.
    """
    spec = importlib.util.spec_from_file_location(module_name, path / "lambda_function.py")
    assert spec and spec.loader, f"could not load {path / 'lambda_function.py'}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with (
    patch.dict("sys.modules", _STUBS),
    patch("boto3.client", MagicMock()),
    patch("boto3.resource", MagicMock()),
):
    sys.modules.pop("lambda_function", None)
    test_code = _load("p32_payload_contract_test_code_lf", TEST_CODE_DIR)
    approval_handler = _load("p32_payload_contract_approval_handler_lf", APPROVAL_HANDLER_DIR)

# The Lambda directory comes back off ``sys.path`` once its sibling modules are
# imported. It contains a ``lambda_function.py``, and several test files later in
# the run do a bare ``import lambda_function`` against their own directory — a
# lingering entry here would shadow theirs depending on collection order.
if str(TEST_CODE_DIR) in sys.path:
    sys.path.remove(str(TEST_CODE_DIR))

# ---------------------------------------------------------------------------
# The reader half: `??` chains parsed out of TestFailureArtifact
# ---------------------------------------------------------------------------
#: The consumer this contract is pinned against. Asserted rather than assumed: a
#: moved or renamed component must fail this test loudly instead of quietly
#: reducing it to a no-op.
ARTIFACT_TSX = REPO_DIR / "dashboard-react" / "src" / "components" / "approvals" / "ApprovalArtifact.tsx"


def _artifact_source() -> str:
    assert ARTIFACT_TSX.is_file(), (
        f"{ARTIFACT_TSX} is missing. This contract test derives the reader half of its expectations "
        f"from TestFailureArtifact in that file; if the component moved, repoint ARTIFACT_TSX rather "
        f"than deleting the assertion."
    )
    return ARTIFACT_TSX.read_text(encoding="utf-8")


def _chain_keys(expression: str, obj: str, label: str) -> list[str]:
    """Ordered, de-duplicated ``obj.<key>`` reads inside one ``??`` chain."""
    keys = list(dict.fromkeys(re.findall(rf"\b{re.escape(obj)}\.(\w+)", expression)))
    assert keys, f"{label}: no `{obj}.<key>` read parsed out of {expression!r} in {ARTIFACT_TSX}"
    return keys


def _match(source: str, pattern: str, label: str) -> str:
    found = re.search(pattern, source, re.DOTALL)
    assert found, (
        f"{label}: {pattern!r} no longer matches {ARTIFACT_TSX}. The reader this contract is pinned "
        f"against has been rewritten — update the parse, do not delete it: an unparsed chain would "
        f"make every assertion here pass vacuously."
    )
    return found.group(1)


def _column_cell(source: str, column_id: str) -> str:
    """The cell expression of one ``columnDefinitions`` entry."""
    marker = f"id: '{column_id}'"
    start = source.find(marker)
    assert start != -1, f"column {column_id!r} not found in {ARTIFACT_TSX} — the test-report table lost a column"
    rest = source[start + len(marker) :]
    ends = [index for index in (rest.find("id: '"), rest.find("]")) if index != -1]
    return rest[: min(ends)] if ends else rest


def _function_body(source: str, name: str) -> str:
    return _match(source, rf"function {re.escape(name)}\([^)]*\)[^{{]*\{{(.*?)\n\}}", f"function {name}")


_SOURCE = _artifact_source()

#: `const report = (details.test_result ?? details.report ?? {})` — where the
#: report envelope is read from `approval.details`, in preference order.
_CONTAINER_EXPR = _match(_SOURCE, r"const report = \((details\.[^)]*)\)", "report container chain")
CONTAINER_CHAIN = _chain_keys(_CONTAINER_EXPR, "details", "report container chain")

#: `Array.isArray(report.checks)` — the per-check list key.
CHECKS_KEY = _chain_keys(_match(_SOURCE, r"Array\.isArray\((report\.\w+)\)", "checks key"), "report", "checks key")[0]

#: Per-check cell chains, in the order the reader tries them.
NAME_CHAIN = _chain_keys(_column_cell(_SOURCE, "check"), "check", "check-name chain")
DETAIL_CHAIN = _chain_keys(_column_cell(_SOURCE, "details"), "check", "details-cell chain")

#: Keys the two status helpers read. Order does not matter here — either key is
#: enough for the reader to render a status, so this is a set.
STATUS_KEYS = frozenset(
    _chain_keys(_function_body(_SOURCE, "checkStatusType"), "check", "checkStatusType")
    + _chain_keys(_function_body(_SOURCE, "checkStatusLabel"), "check", "checkStatusLabel")
)

#: Every per-check key the reader knows how to render. A producer key outside
#: this set reaches the screen nowhere — which is the Defect 1 failure mode.
READABLE_CHECK_KEYS = frozenset(NAME_CHAIN) | frozenset(DETAIL_CHAIN) | STATUS_KEYS

#: The placeholder the reader renders when no key in a chain resolves. `—` in
#: every Details cell is exactly what the live gate showed (1.2).
PLACEHOLDER = "—"


def _resolve_chain(obj: dict[str, Any], chain: list[str]) -> Any:
    """What the reader's ``??`` chain resolves to: first non-null value, else None.

    ``??`` skips only ``null``/``undefined``, so a present-but-empty string is a
    hit — mirrored here so a producer that writes ``detail: ""`` is not scored as
    a miss the reader would treat as a hit.
    """
    for key in chain:
        value = obj.get(key)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# The producer half: executed, not transcribed
# ---------------------------------------------------------------------------
RUN_ID = "run-2026-01-01-00-00-00-p32c001"
REPORT_S3_URI = "s3://test-outputs/pipe-1/run/test_report.md"

#: A reviewer verdict plus issues, so the `ai_review_verdict` check is emitted
#: with its issue-summary detail (`test_code/lambda_function.py:234`).
PARSED_RESULTS = {
    "verdict": "FAIL",
    "issues": [
        {"severity": "high", "description": "to_date drops the format", "attribution": "transform_block"},
        {"severity": "low", "description": "unused import", "attribution": "template_scaffolding"},
    ],
    "test_categories": {
        "syntax": {"status": "PASSED", "passed": True},
        "schema_conformance": {"status": "FAILED", "passed": False},
    },
    "recommendation": "NEEDS_REVIEW",
}

#: The three execution outcomes `aggregate_check_results` branches on, so this
#: contract covers the `detail` written at `:253` (never ran), `:261` (executed)
#: and `:273` (execution failed) as well as `:234` and `:244`.
EXECUTION_RESULTS = {
    "absent": None,
    "succeeded": {"execution_successful": True, "input_records": 10, "output_records": 9},
    "failed": {
        "execution_successful": False,
        "error_details": "PySpark-to-pandas conversion failed: unconvertible PySpark construct 'withColumn' on line 5",
    },
}


def _produced_checks(execution_key: str) -> list[dict[str, Any]]:
    """The real per-check list, from the real aggregator."""
    _status, checks = test_code.aggregate_check_results(PARSED_RESULTS, EXECUTION_RESULTS[execution_key])
    assert checks, "aggregate_check_results produced no checks — the producer half would be vacuous"
    return checks


def _payload(test_result: Any) -> dict[str, Any]:
    """Run the producer and carry its output across the wire hop the dashboard does.

    ``get_approval_data`` builds the payload; ``create_approval_request`` stores it
    as a JSON string under ``approval_data``; the run-detail handler parses that
    string back into ``details``. The round trip is part of the contract — a value
    the store cannot serialize never reaches the reader at all.
    """
    approval_handler.state_table = MagicMock()
    approval_handler.state_table.get_item.return_value = {}
    event = {"approval_type": "test_failure", "run_id": RUN_ID, "test_result": test_result}
    produced = approval_handler.get_approval_data("test_failure", RUN_ID, event)
    return json.loads(json.dumps(produced))


@pytest.fixture(params=sorted(EXECUTION_RESULTS))
def details(request: pytest.FixtureRequest) -> dict[str, Any]:
    """``approval.details`` as the dashboard receives it, one per execution outcome."""
    checks = _produced_checks(request.param)
    status, _ = test_code.aggregate_check_results(PARSED_RESULTS, EXECUTION_RESULTS[request.param])
    return _payload({"status": status, "checks": checks, "report_s3_uri": REPORT_S3_URI})


# ---------------------------------------------------------------------------
# The oracle itself must not be vacuous
# ---------------------------------------------------------------------------


class TestReaderChainsParsed:
    """Guards on the parse. Every assertion below is built on these."""

    def test_container_chain_parsed(self):
        assert CONTAINER_CHAIN, "no container keys parsed"
        assert "{}" in _CONTAINER_EXPR, (
            "the report container chain no longer ends in a `{}` default — an absent or null "
            "test_result would then crash the artifact instead of showing the neutral note (3.13)"
        )

    def test_per_check_chains_parsed(self):
        assert NAME_CHAIN and DETAIL_CHAIN and STATUS_KEYS
        assert CHECKS_KEY == "checks", f"the reader now reads the per-check list from {CHECKS_KEY!r}"


# ---------------------------------------------------------------------------
# 2.1 / 2.2 — producer shape satisfies reader shape
# ---------------------------------------------------------------------------


class TestProducerPayloadMatchesReaderShape:
    def test_report_envelope_is_the_readers_first_choice_key(self, details):
        """2.1: the reader's preferred container key is the one the producer writes.

        Defect 1's first half was exactly this assertion failing: the reader's
        only container key was ``report`` and the producer has always written
        ``test_result``.
        """
        preferred = CONTAINER_CHAIN[0]
        assert preferred in details, (
            f"the reader reads the report envelope from details.{preferred} first, and the producer "
            f"writes {sorted(details)} — no per-check row can render"
        )
        assert isinstance(_resolve_chain(details, CONTAINER_CHAIN), dict)

    def test_checks_list_reaches_the_reader(self, details):
        report = _resolve_chain(details, CONTAINER_CHAIN)
        checks = report.get(CHECKS_KEY)
        assert isinstance(checks, list) and checks, (
            f"the reader guards on Array.isArray(report.{CHECKS_KEY}); the producer supplied {checks!r}"
        )

    def test_details_column_renders_the_producers_text(self, details):
        """2.2: ``detail`` is the key the producer writes, and it must resolve first.

        Defect 1's second half: the chain read ``message ?? details`` and every
        Details cell rendered the placeholder.
        """
        report = _resolve_chain(details, CONTAINER_CHAIN)
        for check in report[CHECKS_KEY]:
            resolved = _resolve_chain(check, DETAIL_CHAIN)
            assert resolved is not None, (
                f"check {check.get('name')!r} carries {sorted(check)}, none of which is in the reader's "
                f"Details chain {DETAIL_CHAIN} — the cell renders {PLACEHOLDER!r}"
            )
            assert isinstance(resolved, str) and resolved.strip()
            assert resolved == check[DETAIL_CHAIN[0]], (
                f"the Details cell resolves via a fallback link, not {DETAIL_CHAIN[0]!r}; the later "
                f"links exist only for payloads that carry them (2.2), so resolving through one means "
                f"the two sides have drifted"
            )

    def test_check_column_renders_the_producers_name(self, details):
        report = _resolve_chain(details, CONTAINER_CHAIN)
        for check in report[CHECKS_KEY]:
            assert _resolve_chain(check, NAME_CHAIN) == check[NAME_CHAIN[0]]

    def test_status_column_has_something_to_read(self, details):
        report = _resolve_chain(details, CONTAINER_CHAIN)
        for check in report[CHECKS_KEY]:
            supplied = STATUS_KEYS & set(check)
            assert supplied, (
                f"check {check.get('name')!r} carries none of the reader's status keys "
                f"{sorted(STATUS_KEYS)} — the status column would render 'unknown' for a failed check"
            )
            if "passed" in check:
                assert isinstance(check["passed"], bool)

    def test_no_produced_check_key_is_unrenderable(self, details):
        """Every key the producer writes has somewhere to go on the screen.

        The reverse direction of 2.1/2.2, and the cheap early warning for the next
        instance of this defect: a new per-check key added in
        ``aggregate_check_results`` with no column reading it is information the
        approver never sees.
        """
        report = _resolve_chain(details, CONTAINER_CHAIN)
        for check in report[CHECKS_KEY]:
            unreadable = set(check) - READABLE_CHECK_KEYS
            assert not unreadable, (
                f"check {check.get('name')!r} carries {sorted(unreadable)}, which TestFailureArtifact "
                f"reads nowhere. Either render it or drop it — a key the producer writes and the "
                f"reader ignores is how Defect 1 stayed invisible."
            )

    def test_payload_survives_the_store_and_read_round_trip(self):
        """The wire hop is lossless for every key the reader reads."""
        checks = _produced_checks("failed")
        produced = {"status": "failed", "checks": checks, "report_s3_uri": REPORT_S3_URI}
        approval_handler.state_table = MagicMock()
        approval_handler.state_table.get_item.return_value = {}
        before = approval_handler.get_approval_data(
            "test_failure", RUN_ID, {"approval_type": "test_failure", "run_id": RUN_ID, "test_result": produced}
        )
        after = json.loads(json.dumps(before))  # store as approval_data → parse into details
        assert after == before
        assert _resolve_chain(after, CONTAINER_CHAIN)[CHECKS_KEY] == checks

    def test_failed_check_names_agree_with_the_checks_the_reader_renders(self, details):
        """``failed_checks`` is a summary of the same list, not a second source of truth."""
        report = _resolve_chain(details, CONTAINER_CHAIN)
        rendered_failures = [c[NAME_CHAIN[0]] for c in report[CHECKS_KEY] if c.get("passed") is False]
        assert details["failed_checks"] == rendered_failures


# ---------------------------------------------------------------------------
# 3.13 — no checks, and no report at all, stay neutral rather than crashing
# ---------------------------------------------------------------------------


class TestEmptyAndAbsentReportStayNeutral:
    def test_empty_checks_payload_is_still_readable(self):
        """3.13: an empty check list reaches the reader as an empty list."""
        details = _payload({"status": "failed", "checks": [], "report_s3_uri": REPORT_S3_URI})
        report = _resolve_chain(details, CONTAINER_CHAIN)
        assert report.get(CHECKS_KEY) == []
        assert details["failed_checks"] == []
        assert details["test_status"] == "failed"

    def test_absent_report_resolves_to_the_chain_default(self):
        """3.13: ``test_result: null`` falls through ``??`` to ``{}``, not to a crash."""
        details = _payload(None)
        assert details["test_result"] is None
        assert details["test_status"] == "unknown"
        # `null ?? {}` — the reader sees an empty object, so `checks` is [].
        assert _resolve_chain(details, CONTAINER_CHAIN) is None
        assert "{}" in _CONTAINER_EXPR

    def test_reader_keeps_the_banner_and_the_neutral_note(self):
        """3.13: the no-checks branch is a note beside the failure banner, not nothing."""
        assert "approval-artifact-test-failure-alert" in _SOURCE, (
            "the 'Code testing failed' banner lost its test id — 3.13 requires the banner to survive a "
            "payload with no checks"
        )
        assert "approval-artifact-empty" in _SOURCE
        assert "No per-check details were recorded for this failure." in _SOURCE
        neutral_branch = _match(
            _SOURCE,
            r"checks\.length > 0 \? \((.*?)approval-artifact-empty",
            "no-checks ternary",
        )
        assert "approval-artifact-test-report" in neutral_branch, (
            "the table/neutral-note ternary no longer guards on checks.length — an empty list would "
            "render an empty table instead of the note (3.13)"
        )
