#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 7 — the reuse fact is three-state and never defaults.

Feature: p54-reused-and-skipped-run-honesty, Property 7: The reuse fact is
three-state and never defaults

p54 R2.1 / R2.2 / R2.3, design 4.2 and *Correctness Properties* Property 7:

    *For any* set of a run's state rows and any resolvability of a mapping id, the
    Run_Detail_Read response carries ``reused_mapping: False`` exactly when a
    ``schema_mapping`` row is present, ``reused_mapping: True`` exactly when none is
    present and an id resolves, and omits the key entirely otherwise — never
    ``False`` for the unknown case.

WHY THIS IS A PROPERTY AND NOT A SEVENTEENTH EXAMPLE
----------------------------------------------------
``test_run_details_reuse_fact.py`` already holds 16 examples across the three states,
the omit-on-failure path and the no-extra-read assertion. Each of them asserts **one
outcome for one input**. Three things are only sayable generatively, and each is a
sentence in the requirement that no example states:

1. **Exhaustiveness and mutual exclusivity of the key PAIR.** The three states are a
   partition, so the two keys can only appear in three combinations: ``False`` alone,
   ``True`` beside a non-blank id, or neither. The fourth-state possibilities — an id
   beside ``False``, an id with no flag, a flag ``True`` with no id, a ``True`` beside
   a blank id — are what ``_classify`` below rejects on **every** input. No example
   asserts that as a general rule; a couple assert ``"reused_mapping_id" not in body``
   for their own specific input.

2. **The joint space, rather than points in it.** The examples pick particular
   (state rows × approvals) pairs. Here the two axes are drawn independently: the
   ``schema_mapping`` row is present-in-one-of-six-spellings or absent, beside an
   arbitrary subset of other rows including two near-misses, and the approvals list is
   an arbitrary sequence over seven resolution outcomes. A defect that needs a
   particular *combination* — a near-miss row beside a resolvable id, say — is
   reachable here and is not enumerable by hand.

3. **The step-name alias space.** The examples test exactly two spellings,
   ``schema_mapping`` and ``SchemaMapping``. ``step_names._normalize`` is case- and
   ``_``/``-``-insensitive, so **six** spellings canonicalise onto this step, and two
   near-misses that share the substring do **not**: ``schema_mapping_approval``
   and ``check_schema_mapping_approval`` both canonicalise to
   ``check_schema_mapping_approval`` (``_LEGACY_ALIASES`` maps the former to the gate,
   not to the step). A row of either near-miss shape must NOT make the fact ``False``,
   and nothing in the tree asserted that before this file.

Every mapping in the two pools below was verified by running ``from_any`` over it
rather than inferred from reading ``_normalize``.

WHAT IS GENERATED
-----------------
* **The state rows.** ``min_size=1`` because zero state rows is a 404 (``:1457``) and
  a different requirement (``test_run_details_not_found_prop.py`` owns it). Names are
  unique so no two rows collide on ``steps``. Each row is drawn together with the
  generator's own answer to *"is this the schema_mapping step"*, so the oracle below
  is generator-side truth and never a re-derivation of ``_canonical_or_raw``.
* **The approvals rows**, each drawn as one of seven resolution outcomes: an id that
  resolves, a blank id, no key at all, a non-string id, a null id, an unparseable
  ``approval_data``, and an ``approval_data`` holding a JSON scalar so ``details``
  parses to a non-dict. Each carries the id it resolves, or ``None``.
* **Whether the run is parked at the schema-mapping gate**, which is trap 1 from the
  example file swept generatively: the running-step injection APPENDS a synthetic
  ``schema_mapping`` entry to ``run_details["steps"]`` when a pending approval maps to
  that step, with no state row behind it. So on the canonical Reuse_Run ``steps`` and
  ``items`` disagree, and a derivation reading ``steps`` reports ``False`` for exactly
  the run R2 is about. Drawing this independently of the row axis means the property
  asserts the injection can never move the answer.

WHAT IS NOT GENERATED, AND WHY
------------------------------
* **Whitespace-only mapping ids.** ``" "`` is truthy, so the derivation would report
  it as a resolved id. That is a state no producer can reach — ``reused_mapping_id``
  is written by p54 task 2.2 from a ``mapping_id`` that came from the mappings table
  or the ASL payload — so generating it would manufacture a counterexample about an
  input that does not exist rather than find a defect. Recorded rather than swept, on
  the same ground design gives for not property-testing configuration values.
* **A raising read.** ``LGR-8e34f37-3``'s omit-on-failure path is deterministic and is
  pinned by three examples in the sibling file. A generator over exception types would
  be three spellings of one guard.

FIXTURES
--------
**No fixture at all, deliberately.** ``get_run_details``' table handles are patched
with ``patch.object`` inside each example over **freshly built** ``MagicMock``s, so
nothing accumulates across examples. A function-scoped fixture is invoked **once** for
the whole ``@given`` run, so a harness that carried ``return_value`` or ``call_count``
state between examples could make this property pass vacuously — the trap p54 task 2.2
hit. Building the mocks per example closes it by construction and needs no
``HealthCheck.function_scoped_fixture`` suppression. No ``moto.mock_aws()`` is created
here (``tests/unit/test_no_duplicate_mocking_prop.py``).

**Validates: Requirements 2.1, 2.2, 2.3**
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path and import-time env — ``handlers/common.py`` calls ``require_env`` at
# import time, so these must precede the handler import.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _var, _value in {
    "AGENTIC_DATA_INGESTION_ENV": "local",
    "DYNAMODB_TABLE": "test-state-table",
    "MAPPINGS_TABLE": "test-mappings-table",
    "PIPELINES_TABLE": "test-pipelines-table",
    "GENERATED_CODE_TABLE": "test-generated-code-table",
    "TEST_RESULTS_TABLE": "test-results-table",
    "APPROVALS_TABLE_NAME": "test-approvals-table",
    "EXECUTIONS_TABLE": "test-executions-table",
    "AWS_REGION": "us-west-2",
}.items():
    os.environ.setdefault(_var, _value)

#: The property tag, on one line so it is greppable verbatim. The docstrings above
#: and below carry the same string wrapped to the line limit.
PROPERTY_TAG = (
    "Feature: p54-reused-and-skipped-run-honesty, Property 7: The reuse fact is three-state and never defaults"
)

RUN_ID = "run-reuse-fact-prop-001"
PIPELINE_ID = "pipe-reuse-prop-001"

# ---------------------------------------------------------------------------
# The two step-name pools. Every entry verified by running
# ``step_names.from_any`` over it — see the module docstring.
# ---------------------------------------------------------------------------

#: Spellings that all canonicalise to ``StepName.SCHEMA_MAPPING``. Six rather than
#: the two the example file uses, because ``_normalize`` strips ``_``/``-`` and
#: lowercases, and ``SFN_STATE_NAMES`` contributes the PascalCase form. A row in ANY
#: of these spellings is the run's own mapping row and must make the fact ``False``.
_SCHEMA_MAPPING_SPELLINGS: tuple[str, ...] = (
    "schema_mapping",
    "SchemaMapping",
    "schema-mapping",
    "SCHEMA_MAPPING",
    "Schema_Mapping",
    "schemamapping",
)

#: Names whose canonical form is NOT ``schema_mapping``. The first four are other
#: real steps. The next two are the **near-misses** that carry the substring:
#: ``_LEGACY_ALIASES`` maps ``schema_mapping_approval`` to
#: ``CHECK_SCHEMA_MAPPING_APPROVAL``, so neither is this step. The last three have no
#: canonical form at all and pass through ``_canonical_or_raw`` unchanged — the
#: records (``load_metrics``, ``data_quality``) and one invented sentinel.
_NOT_SCHEMA_MAPPING: tuple[str, ...] = (
    "schema_extract",
    "schema_compare",
    "create_code",
    "test_code",
    "schema_mapping_approval",
    "check_schema_mapping_approval",
    "load_metrics",
    "data_quality",
    "mapping_reuse",
)


def _state_row(step_name: str, seq: int) -> dict[str, Any]:
    """A state-table row in the shape the run_id base-table query returns."""
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-reuse-prop-001",
        "status": "success",
        "timestamp": f"2026-08-28T12:{seq:02d}:00+00:00",
        "step_sequence": seq,
    }


def _approval_row(approval_data: Any, *, status: str) -> dict[str, Any]:
    """An approvals-table row as the ``run_id`` GSI returns it.

    ``approval_data`` is a JSON **string** on the wire (``approval_handler:850``
    writes ``json.dumps(...)``), and ``get_run_details`` parses it into ``details``.
    A ``str`` passes through verbatim so the unparseable and scalar shapes stay
    honest about what the derivation reads through.
    """
    return {
        "approval_id": "appr-reuse-prop-001",
        "run_id": RUN_ID,
        "approval_type": "schema_mapping",
        "status": status,
        "approval_data": approval_data if isinstance(approval_data, str) else json.dumps(approval_data),
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Non-blank after strip, so every generated id is one a producer could write. See
#: "WHAT IS NOT GENERATED" for why whitespace-only ids are out.
_mapping_ids = st.from_regex(r"mapping-[A-Za-z0-9_-]{1,20}", fullmatch=True)


@st.composite
def _state_rows(draw: st.DrawFn) -> tuple[list[dict[str, Any]], bool]:
    """Draw a run's state rows plus the generator's own answer for the row axis.

    Returns ``(rows, schema_mapping_row_present)``. The boolean is decided by the
    draw and never derived from the row names, so the oracle does not re-implement
    ``_canonical_or_raw`` — which would make the test agree with the code by
    construction rather than with the requirement.
    """
    others = draw(st.lists(st.sampled_from(_NOT_SCHEMA_MAPPING), unique=True, max_size=5))
    present = draw(st.booleans())
    names = list(others)
    if present:
        names.append(draw(st.sampled_from(_SCHEMA_MAPPING_SPELLINGS)))
    if not names:
        # Zero state rows is a 404 and a different requirement's subject.
        names = [draw(st.sampled_from(_NOT_SCHEMA_MAPPING))]
        present = False
    rows = [_state_row(name, seq) for seq, name in enumerate(names, start=1)]
    return rows, present


@st.composite
def _approval_rows(draw: st.DrawFn) -> tuple[list[dict[str, Any]], str | None]:
    """Draw the approvals rows plus the id the derivation should resolve.

    The second element is the **first** non-blank string id in list order, or
    ``None``. First-wins is the derivation's own rule and it is stated here
    independently: the walk breaks on the first truthy id and otherwise ends with
    the last row's answer, which for every non-resolving shape is ``""``.
    """
    kinds = draw(
        st.lists(
            st.sampled_from(
                (
                    "resolves",
                    "blank_id",
                    "no_key",
                    "non_string_id",
                    "null_id",
                    "unparseable",
                    "json_scalar",
                    "empty_approval_data",
                )
            ),
            max_size=4,
        )
    )
    statuses = draw(
        st.lists(st.sampled_from(("approved", "pending", "rejected")), min_size=len(kinds), max_size=len(kinds))
    )

    rows: list[dict[str, Any]] = []
    first_id: str | None = None
    for kind, status in zip(kinds, statuses, strict=True):
        if kind == "resolves":
            mid = draw(_mapping_ids)
            payload: Any = {"reused_mapping": True, "reused_mapping_id": mid}
            if first_id is None:
                first_id = mid
        elif kind == "blank_id":
            payload = {"reused_mapping": True, "reused_mapping_id": ""}
        elif kind == "no_key":
            payload = {"source_schema": {"fields": []}, "proposed_mappings": []}
        elif kind == "non_string_id":
            # ``item_str`` returns "" for a non-string, so this does not resolve.
            payload = {"reused_mapping": True, "reused_mapping_id": draw(st.integers())}
        elif kind == "null_id":
            payload = {"reused_mapping": True, "reused_mapping_id": None}
        elif kind == "unparseable":
            payload = "{not json"
        elif kind == "json_scalar":
            # ``details`` parses to a non-dict; ``item_dict`` yields {}.
            payload = "42"
        else:  # empty_approval_data — falsy ``raw``, so ``details`` stays {}
            payload = ""
        rows.append(_approval_row(payload, status=status))
    return rows, first_id


# ---------------------------------------------------------------------------
# The three states, named, plus the fourth that must never occur
# ---------------------------------------------------------------------------

_OWN_MAPPING = "the run generated its own mapping"
_REUSED = "the run reused a named mapping"
_NOT_ESTABLISHED = "not established"
_FOURTH_STATE = "a fourth state"


def _classify(body: dict[str, Any]) -> str:
    """Classify a response body into one of the three states, or ``_FOURTH_STATE``.

    This is where exhaustiveness and mutual exclusivity are actually asserted: the
    two keys admit more combinations than three, and every extra combination is a
    way of being dishonest about the reuse fact. ``_FOURTH_STATE`` catches an id
    beside ``False``, an id with no flag, ``True`` with no id, ``True`` with a blank
    id, and any non-boolean flag.
    """
    has_flag = "reused_mapping" in body
    has_id = "reused_mapping_id" in body
    flag = body.get("reused_mapping")
    mapping_id = body.get("reused_mapping_id")

    if has_flag and flag is False and not has_id:
        return _OWN_MAPPING
    if has_flag and flag is True and has_id and isinstance(mapping_id, str) and mapping_id:
        return _REUSED
    if not has_flag and not has_id:
        return _NOT_ESTABLISHED
    return _FOURTH_STATE


def _invoke(
    state_rows: list[dict[str, Any]],
    approvals_rows: list[dict[str, Any]],
    *,
    parked_at_gate: bool,
) -> tuple[int, dict[str, Any]]:
    """Call ``get_run_details`` over freshly built table mocks.

    Fresh per call, which is what keeps one example from seeing another's harness
    state. ``handlers/pipelines_runs.py`` copies the table handles into its own
    namespace with ``from .common import …``, so both modules are patched or the
    handler reads names that were never replaced.
    """
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    state = MagicMock()
    approvals = MagicMock()
    executions = MagicMock()
    pipelines = MagicMock()
    dynamo = MagicMock()

    state.query.return_value = {"Items": state_rows}
    approvals.query.return_value = {"Items": approvals_rows}
    executions.query.return_value = {"Items": []}
    executions.get_item.return_value = (
        {
            "Item": {
                "status": "WAITING_APPROVAL",
                "current_step": "schema_compare",
                "updated_at": "2026-08-28T12:30:00+00:00",
            }
        }
        if parked_at_gate
        else {}
    )
    pipelines.get_item.return_value = {}
    dynamo.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    handles = (
        ("state_table", state),
        ("approvals_table", approvals),
        ("executions_table", executions),
        ("pipelines_table", pipelines),
        ("dynamodb", dynamo),
    )
    with ExitStack() as stack:
        for module in (_common, _runs):
            for attribute, handle in handles:
                stack.enter_context(patch.object(module, attribute, handle))
        stack.enter_context(patch.object(_runs, "get_step_logs", lambda *a, **kw: []))
        response = _runs.get_run_details(RUN_ID)

    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# Property 7
# ===========================================================================


class TestTheReuseFactIsThreeStateAndNeverDefaults:
    """Feature: p54-reused-and-skipped-run-honesty, Property 7: The reuse fact is
    three-state and never defaults."""

    @given(rows=_state_rows(), approvals=_approval_rows(), parked_at_gate=st.booleans())
    @settings(max_examples=200, deadline=None)
    def test_exactly_one_of_three_states_for_every_input(
        self,
        rows: tuple[list[dict[str, Any]], bool],
        approvals: tuple[list[dict[str, Any]], str | None],
        parked_at_gate: bool,
    ) -> None:
        """Feature: p54-reused-and-skipped-run-honesty, Property 7: The reuse fact is
        three-state and never defaults.

        The whole property in one assertion set, over the joint space of
        (state-row sets × id resolvability × whether the gate injection fired):

        * the response is in **exactly one** of the three states — never a fourth;
        * which one is decided by the row axis first and the id axis second;
        * a resolved state names the id the approvals actually carried.

        **Validates: Requirements 2.1, 2.2, 2.3**
        """
        state_rows, schema_mapping_row_present = rows
        approvals_rows, expected_id = approvals

        status, body = _invoke(state_rows, approvals_rows, parked_at_gate=parked_at_gate)

        # The derivation never changes the response's own outcome.
        assert status == 200

        # The oracle, from the requirement rather than from the code: the row axis
        # dominates, and the id axis only decides between the other two states.
        if schema_mapping_row_present:
            expected = _OWN_MAPPING
        elif expected_id is not None:
            expected = _REUSED
        else:
            expected = _NOT_ESTABLISHED

        observed = _classify(body)

        # Exhaustive and mutually exclusive: no input produces a fourth outcome.
        assert observed != _FOURTH_STATE, (
            f"the two keys landed in a combination that is none of the three states: "
            f"reused_mapping={body.get('reused_mapping', '<absent>')!r}, "
            f"reused_mapping_id={body.get('reused_mapping_id', '<absent>')!r}"
        )
        assert observed == expected, (
            f"expected {expected!r}, got {observed!r} for "
            f"{len(state_rows)} state row(s) "
            f"(schema_mapping row present={schema_mapping_row_present}), "
            f"{len(approvals_rows)} approval(s) resolving {expected_id!r}, "
            f"parked_at_gate={parked_at_gate}"
        )

        # R2.2 — the resolved state identifies the mapping, and identifies the one
        # the approvals carried rather than any id.
        if expected is _REUSED:
            assert body["reused_mapping_id"] == expected_id

    @given(rows=_state_rows(), parked_at_gate=st.booleans())
    @settings(max_examples=150, deadline=None)
    def test_false_is_never_the_unknown_case(
        self,
        rows: tuple[list[dict[str, Any]], bool],
        parked_at_gate: bool,
    ) -> None:
        """Feature: p54-reused-and-skipped-run-honesty, Property 7: The reuse fact is
        three-state and never defaults.

        The same property restricted to the arm the requirement names explicitly, so
        that the unknown case is a large fraction of examples rather than a slice of
        the joint sweep: **no approval resolves an id at all.** Whatever the rows are
        and whether or not the gate injection fired, the answer is ``False`` only
        where a ``schema_mapping`` row establishes it, and **omission** otherwise —
        never ``False``, which would assert the run generated its own mapping.

        p54 do-NOT 7, and ``LGR-b2f79ef-14``'s shape: a named reference to something
        absent is an inconsistent record, not a thing that never existed.
        ``LGR-99050e6-2`` and ``LGR-44d601e-1`` are the same rule for the counter and
        string cases.

        **Validates: Requirements 2.1, 2.3**
        """
        state_rows, schema_mapping_row_present = rows

        # Every non-resolving shape, all at once — none of them is an id.
        non_resolving = [
            _approval_row({"reused_mapping": True, "reused_mapping_id": ""}, status="pending"),
            _approval_row({"source_schema": {"fields": []}}, status="approved"),
            _approval_row("{not json", status="approved"),
            _approval_row("42", status="rejected"),
        ]

        status, body = _invoke(state_rows, non_resolving, parked_at_gate=parked_at_gate)

        assert status == 200
        if schema_mapping_row_present:
            assert _classify(body) == _OWN_MAPPING
        else:
            assert _classify(body) == _NOT_ESTABLISHED
            assert body.get("reused_mapping") is not False, (
                "``False`` was reported for a run whose reuse could not be "
                "established, which asserts it generated its own mapping (do-NOT 7)"
            )
            assert "reused_mapping" not in body
            assert "reused_mapping_id" not in body
