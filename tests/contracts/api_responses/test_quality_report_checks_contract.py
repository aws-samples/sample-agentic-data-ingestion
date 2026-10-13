# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the ``checks[]`` shape of GET /pipelines/{id}/runs/{runId}/quality-report.

p64 R8.4 — a new ``checks[]`` entry is a response-shape change, so it must reach
``schemas.py`` and the dashboard's DQ tab. This file is the ``schemas.py`` half.
Task 2d.2 added the entry (the non-printable character check, R8.1); task 2d.3
declared the shape it landed in.

**There was no declared shape at all before this.** ``get_quality_report``
(``handlers/agents.py:115-128``) returns the ``(run_id, "data_quality")`` state row
**verbatim**, with no projection, so until now the wire format of this endpoint was
whatever two unrelated producers happened to write — the generated Glue job's inline
evaluation (``source: "glue_inline_dq"``) and the on-demand data quality agent
(``agent_type: "data_quality"``). A verbatim passthrough is exactly the case where a
contract test earns its keep: there is no handler code to read to find out what the
response looks like.

**Pinned against the frontend type, not against the producer.** The field
expectations are PARSED at test time out of::

    dashboard-react/src/hooks/use-quality-report.ts

(``QualityReport`` / ``QualityCheck``), via the shared reader in ``helpers.py`` —
``LGR-99050e6-5``, which extracted that reader precisely so a new contract test
would not add another copy. This file adds **no parser of its own**. Deriving the
expectations from the consumer is the point of the pattern (``LGR-28b4d75-5``): a
test written from the producer's own output cannot detect the producer drifting,
because that output is its own oracle.

What is pinned here, beyond the field types:

* ``status`` has **three** values, and the R8 entry uses ``warning``. 2d.1 decided
  the check FLAGS rather than quarantines, so ``fail`` — which means rows were
  removed from the frame — would be a false statement about the data. A fourth value
  is refused by the enum in ``QUALITY_CHECK_SCHEMA`` and by the frontend's literal
  union, which is asserted to still admit ``warning`` rather than assumed to.
* The entry is counted in **none** of ``rules_passed`` / ``rules_failed`` /
  ``rules_total``, and adds nothing to ``rows_quarantined``. So ``len(checks)`` and
  ``rules_total`` deliberately disagree, and a future reader who "fixes" that
  disagreement fails a test that says why.
* ``overall_status`` does not move. It is derived from the DQDL fail count, so a
  clean ruleset reports ``pass`` **while carrying a warning entry** — the one
  assertion here that would catch the R8 entry being wired into the verdict.
* ``failure_reason`` is on the wire AND declared by the consumer. It carries the
  sentence stating the flagged rows were "flagged, not quarantined", which is the
  only place the response says those rows reached the target. Pinning the frontend
  declaration is what keeps the render (``QualityReportCard``) from being dropped as
  unused.

Template-side rendering — that both halves of the check render together, on all five
templates — is pinned by ``tests/unit/templates/test_nonprintable_check_all_targets.py``.
This file pins only the **wire format**.

Validates: Requirements 8.1, 8.4
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jsonschema
import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
LAMBDA_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api"
SHARED_DIR = REPO_DIR / "shared"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# handlers/common.py calls require_env for each table at import time, before any
# fixture can run — mirrors test_cost_recommendations_contract.py. The real table
# handle is replaced by a mock below; nothing here touches AWS.
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import agents as _agents

# The shared frontend-type reader. `LGR-99050e6-5` extracted it so that the next
# contract test would start from the version handling unions rather than adding an
# eighth parser — this file is that next contract test, and adds none.
from .helpers import load_interfaces, matches_json_type, required_fields
from .schemas import QUALITY_REPORT_RESPONSE_SCHEMA, QUALITY_REPORT_SCHEMA

FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-quality-report.ts"
REPORT_TYPE = "QualityReport"
CHECK_TYPE = "QualityCheck"

#: The name the template gives the R8 entry (`_partials/_data_quality.py.j2`).
NP_CHECK_NAME = "NonPrintableCharacters"

RUN_ID = "run-np-2026-09-12"
PIPELINE_ID = "pipe-np"


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """The two consumer interfaces this endpoint's row must satisfy.

    ``load_interfaces`` asserts the file exists and that neither interface parsed
    empty, so a moved hook fails loudly instead of reducing every assertion below to
    a vacuous pass.
    """
    return load_interfaces(FRONTEND_TYPE_FILE, REPORT_TYPE, CHECK_TYPE)


# ---------------------------------------------------------------------------
# Fixture data — shaped as the template actually writes it
# ---------------------------------------------------------------------------
# Faithful to `_partials/_data_quality.py.j2`'s report stage: floats arrive as
# Decimal, because the template stores the row through
# `json.loads(json.dumps(...), parse_float=Decimal)`. Two DQDL rules, both passing,
# plus the R8 entry — the combination that matters, because it is the one where a
# `warning` entry coexists with `overall_status: "pass"`.
def _row(np_rows: int, dq_fail_count: int = 0) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {"name": "RowCount > 0", "status": "pass", "actual": Decimal("1000")},
        {
            "name": 'Completeness "patient_id" >= 0.95',
            "status": "fail" if dq_fail_count else "pass",
            "actual": Decimal("0.5") if dq_fail_count else Decimal("0.99"),
        },
    ]
    if dq_fail_count:
        checks[1]["failure_reason"] = "Value: 0.5 does not meet the constraint requirement!"

    np_check: dict[str, Any] = {
        "name": NP_CHECK_NAME,
        "status": "warning" if np_rows > 0 else "pass",
        "actual": Decimal(np_rows),
    }
    if np_rows > 0:
        np_check["failure_reason"] = (
            f"{np_rows} row(s) written to the target contain control characters (flagged, not quarantined)"
        )
    checks.append(np_check)

    return {
        "run_id": RUN_ID,
        "step_name": "data_quality",
        "pipeline_id": PIPELINE_ID,
        "overall_status": "pass" if dq_fail_count == 0 else "fail",
        "checks": checks,
        "root_cause": None if dq_fail_count == 0 else "data_issue",
        "recommendations": [] if dq_fail_count == 0 else ["Review failed data quality rules"],
        "source": "glue_inline_dq",
        "timestamp": "2026-09-12T18:00:00+00:00",
        # Two DQDL rules. The R8 entry is in none of these three counts, so
        # `rules_total` stays 2 while `checks` has 3 members.
        "rules_passed": 2 - dq_fail_count,
        "rules_failed": dq_fail_count,
        "rules_total": 2,
        "rows_passed": 1000,
        # The check flags and diverts nothing, so it contributes nothing here.
        "rows_quarantined": 0,
        "quarantine_path": None,
    }


def _fetch(monkeypatch, item: dict[str, Any] | None) -> tuple[int, dict]:
    table = MagicMock()
    table.get_item.return_value = {"Item": item} if item is not None else {}
    monkeypatch.setattr(_agents, "state_table", table)
    response = _agents.get_quality_report(PIPELINE_ID, RUN_ID)
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def flagged(monkeypatch) -> dict:
    """A run whose DQDL ruleset passed and whose R8 check found three rows."""
    status, body = _fetch(monkeypatch, _row(np_rows=3))
    assert status == 200, body
    return body


# ---------------------------------------------------------------------------
# The declared shape
# ---------------------------------------------------------------------------


def test_the_response_validates_against_the_declared_envelope(flagged):
    """``schemas.py`` describes the row this endpoint actually returns (R8.4)."""
    jsonschema.validate(flagged, QUALITY_REPORT_RESPONSE_SCHEMA)


def test_an_absent_row_is_a_null_report_not_a_404(monkeypatch):
    """The empty arm of the same envelope.

    ``get_quality_report`` returns 200 with ``{"report": None}`` for a run with no
    row, and the hook coalesces it to null. Pinned so the nullable branch of the
    schema is exercised rather than merely declared.
    """
    status, body = _fetch(monkeypatch, None)

    assert status == 200
    assert body == {"report": None}
    jsonschema.validate(body, QUALITY_REPORT_RESPONSE_SCHEMA)


def test_a_fourth_status_value_is_refused(flagged):
    """2d.1's decision as a schema property, not a comment.

    Three values, and the R8 entry reuses one of them. A producer inventing
    ``"flagged"`` would render as ``error`` in ``QualityReportCard.checkStatusType``
    (its fallthrough branch), i.e. a flag would be displayed as a failure.
    """
    report = dict(flagged["report"])
    report["checks"] = [*report["checks"], {"name": "Invented", "status": "flagged"}]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, QUALITY_REPORT_SCHEMA)


# ---------------------------------------------------------------------------
# The consumer's view of it
# ---------------------------------------------------------------------------


def test_every_required_consumer_field_is_present(flagged, frontend_types):
    """The report satisfies ``QualityReport``'s required fields."""
    missing = required_fields(frontend_types[REPORT_TYPE]) - set(flagged["report"])

    assert not missing, f"{REPORT_TYPE} declares {sorted(missing)} as required and the row does not carry them"


def test_every_check_field_matches_the_consumer_declaration(flagged, frontend_types):
    """Each ``checks[]`` member's fields type-check against ``QualityCheck``.

    Decimal is the one translation this needs: DynamoDB stores the template's floats
    as ``Decimal`` and the handler's JSON encoder emits them as numbers, which is
    what the browser receives, so the parsed body is compared as the browser would
    see it.
    """
    declared = frontend_types[CHECK_TYPE]

    for check in flagged["report"]["checks"]:
        missing = required_fields(declared) - set(check)
        assert not missing, f"{check.get('name')!r} is missing {sorted(missing)}"
        for field, value in check.items():
            if field not in declared:
                continue  # producer extras are allowed; the consumer ignores them
            _, ts_type = declared[field]
            assert matches_json_type(value, ts_type), (
                f"{check.get('name')!r}.{field} = {value!r} does not satisfy {ts_type!r}"
            )


def test_the_consumer_still_admits_the_warning_status(frontend_types):
    """The R8 entry's status value is in ``QualityCheck.status``'s union.

    Asserted rather than assumed: the union is what makes ``warning`` renderable as
    a warning instead of falling through to ``error``, and narrowing it back to
    ``'pass' | 'fail'`` would break the R8 entry silently — the type is not read at
    runtime.
    """
    _, status_type = frontend_types[CHECK_TYPE]["status"]

    assert "'warning'" in status_type, (
        f"{CHECK_TYPE}.status is declared {status_type!r}, which no longer admits the value p64 R8.1's check reports"
    )


def test_the_consumer_declares_the_failure_reason_field(frontend_types):
    """``failure_reason`` has a consumer, which is why the DQ tab renders it.

    It has been on the wire for every failing DQDL rule all along and was undeclared
    and undisplayed until 2d.3. On the R8 entry it is the only place the report says
    the flagged rows reached the target, so an undeclared field here would mean the
    honest half of the finding exists and is invisible.
    """
    declared = frontend_types[CHECK_TYPE]

    assert "failure_reason" in declared, (
        f"{CHECK_TYPE} no longer declares failure_reason — the producer still sends it, "
        f"so dropping it here makes the reason invisible rather than absent"
    )
    assert matches_json_type("a reason", declared["failure_reason"][1])


# ---------------------------------------------------------------------------
# The R8 entry's own invariants
# ---------------------------------------------------------------------------


def test_the_entry_reports_a_warning_and_the_row_count_it_found(flagged):
    entry = next(c for c in flagged["report"]["checks"] if c["name"] == NP_CHECK_NAME)

    assert entry["status"] == "warning"
    assert entry["actual"] == 3
    assert "flagged, not quarantined" in entry["failure_reason"], (
        "the reason must state that the rows reached the target; that sentence is the "
        "whole difference between this check and a quarantine"
    )


def test_a_clean_run_reports_the_entry_as_a_pass_with_no_reason(monkeypatch):
    """Zero rows found is ``pass``, and carries no ``failure_reason``.

    The paired negative for the assertion above: the reason is emitted only when
    ``_adi_np_rows > 0``, so a clean check must not carry a sentence describing a
    finding it did not make.
    """
    _, body = _fetch(monkeypatch, _row(np_rows=0))
    entry = next(c for c in body["report"]["checks"] if c["name"] == NP_CHECK_NAME)

    assert entry["status"] == "pass"
    assert entry["actual"] == 0
    assert "failure_reason" not in entry


def test_the_entry_moves_neither_the_verdict_nor_the_rule_counts(flagged):
    """The R8 entry is reported and counted in nothing (R8.1, 2d.1).

    All four properties in one test because they are one claim: a flag is not an
    outcome. ``overall_status`` is derived from the DQDL fail count, the three
    ``rules_*`` fields describe the DQDL ruleset, and ``rows_quarantined`` counts
    rows removed from the frame — the check removed none.
    """
    report = flagged["report"]

    assert report["overall_status"] == "pass", (
        "a warning entry moved the verdict; overall_status is the DQDL ruleset's "
        "outcome and a flag is not a rule outcome"
    )
    assert report["rules_total"] == 2
    assert report["rules_passed"] + report["rules_failed"] == report["rules_total"]
    assert report["rows_quarantined"] == 0
    # The deliberate disagreement: three checks, two rules. Asserted so that
    # "fixing" it is a decision rather than a tidy-up.
    assert len(report["checks"]) == report["rules_total"] + 1


def test_a_dqdl_failure_still_fails_the_verdict_alongside_the_warning(monkeypatch):
    """The other direction: the entry does not mask a real failure either.

    A run with a failing DQDL rule AND a non-printable finding reports ``fail``, and
    both entries survive to the wire with their own statuses.
    """
    _, body = _fetch(monkeypatch, _row(np_rows=3, dq_fail_count=1))
    report = body["report"]
    statuses = {c["name"]: c["status"] for c in report["checks"]}

    assert report["overall_status"] == "fail"
    assert statuses[NP_CHECK_NAME] == "warning"
    assert statuses['Completeness "patient_id" >= 0.95'] == "fail"
    jsonschema.validate(body, QUALITY_REPORT_RESPONSE_SCHEMA)
