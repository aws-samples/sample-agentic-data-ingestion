# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``first_run`` approval payload is read from ``output_data.approval``.

# Feature: p50-approval-payload-and-actor-display, C5

WHAT THIS FILE IS
-----------------
The regression suite for p50 C5 — the ``first_run`` twin of C1. It fails on the
pre-C5 tree and passes after, the same polarity as
``test_schema_mapping_payload_read.py`` in this directory.

WHAT WAS WRONG, AND HOW IT DIFFERS FROM THE C1 TWIN
---------------------------------------------------
``get_approval_data``'s ``first_run`` branch read ``quality_checks`` and
``data_quality_rules`` off the ``(run_id, "create_quality_checks")`` row's TOP
level. The in-code note that used to sit beside them said so outright — "where
that writer never writes them — a pre-existing gap left alone here" — and named
the test that pinned the behaviour.

The difference from C1 matters and is why this needed its own investigation
rather than a copied fix. For ``schema_mapping``, seven of the nine keys existed
verbatim in the producer's payload, so the correction was purely an address
change. Here **neither key exists under that spelling anywhere in the writer**.
Verified against ``create_quality_checks/lambda_function.py``, not inferred:

* The step builds a ``first_run`` approval payload in
  ``combine_infrastructure_and_quality_approval_data`` (``:266``, payload dict at
  ``:287-322``) and stores it whole at ``output_data["approval"]`` (``:433``) —
  the same shape ``create_code`` and ``schema_mapping`` use, so C5 is the third
  branch reading a producer's own approval payload and not a new mechanism.
* ``quality_checks`` is that payload's ``pre_run_validation.quality_checks``
  (``:307-312``), the writer's **only** key of that name. It is a list of
  human-readable lines, which is what the consumer wants:
  ``ApprovalsPage.tsx:100-107`` renders it under a "Quality checks" label.
* ``data_quality_rules`` is the generated rule list, at that payload's
  ``quality_check_configuration.quality_rules``. It also resolves at
  ``output_data.quality_configuration.quality_rules`` (``:420``), and the two are
  the **same object** at write time — ``quality_config`` is passed into the
  combiner by reference — so reading payload-first, row-second is the two-source
  shape ``code_version_id`` uses at ``approval_handler:381`` and ``generated_by``
  at ``:299``, not a second authority.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
-------------------------------------------
Two things, both recorded rather than tested, so a later reader does not mistake
silence for coverage:

1. **The producer's boilerplate.** Only the first of
   ``pre_run_validation.quality_checks``' four lines is derived from anything;
   the other three are unconditional string literals, so a zero-rule run still
   tells its approver "Null value checks configured". That is a producer-side
   defect. C5 is a read correction and does not fix it — but reading the
   producer's own key is still strictly better than reading an address nothing
   writes, because today the approver is shown *nothing*, including the rule
   count.
2. **The three ``create_infrastructure`` reads in the same branch.** They are
   wrong too, in three different ways (wrong level / no producer at all / wrong
   row), and R4.1 scopes C5 to exactly two keys. ``test_the_out_of_scope_infra_
   reads_are_still_null`` below pins that they are *untouched*, so the follow-up
   has a starting point and so this change cannot be mistaken for having fixed
   them.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables``, ``mock_lambda_env`` and the two
p50 C5 row fixtures from ``tests/fixtures/conftest.py``. No ``moto.mock_aws()``
call and no boto3 client or resource mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and ``AWS_REGION`` is not set
here — the unit layer pins it in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``,
template / ratified).

moto rather than a ``MagicMock`` table is deliberate, and load-bearing for the
``Decimal`` case: boto3's resource deserializer returns every ``N`` attribute as
``Decimal`` whatever the writer supplied, where a hand-built mock return value
just asserts whatever the author typed.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
-----------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` are the sixth copy of
this preamble in this directory. ``test_serialization_preservation_prop.py``
records the reasoning at length and files the extraction as follow-up work to be
done as one mechanical change with a full-suite run behind it; that has not
happened yet, so this file copies rather than extracts. The distinct module name
is load-bearing — each copy execs the handler under its own name so a sibling's
rebound tables cannot reach this one's.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from tests.fixtures.conftest import (
    FIRST_RUN_DECIMAL_PIPELINE_ID,
    FIRST_RUN_DECIMAL_RUN_ID,
)

LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
for _path in (LAMBDA_DIR, SHARED_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

APPROVALS_TABLE = "agentic_data_ingestion_approvals"
STATE_TABLE = "agentic_data_ingestion_state"
TASK_TOKENS_TABLE = "agentic_data_ingestion_task_tokens"

RUN_ID = FIRST_RUN_DECIMAL_RUN_ID
PIPELINE_ID = FIRST_RUN_DECIMAL_PIPELINE_ID
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "c5p50" * 9

#: The two keys C5 corrects.
CORRECTED_KEYS = ("quality_checks", "data_quality_rules")

#: The third key in the same branch, correct before C5 and unchanged by it.
UNTOUCHED_KEY = "quality_verdict"

#: The three reads C5 explicitly leaves alone (R4.1 scopes C5 to two keys).
OUT_OF_SCOPE_INFRA_KEYS = ("infrastructure_status", "cloudformation_stack_id", "glue_job_name")


# ---------------------------------------------------------------------------
# Loading the handler
# ---------------------------------------------------------------------------
_HANDLER = None

_LAYER_STUBS = {
    "eventbridge_utils": MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
    "notification_service": MagicMock(
        send_approval_notification=MagicMock(return_value={"success": True}),
        send_approval_timeout_alert=MagicMock(return_value={"success": True}),
    ),
}


def _metrics_stub() -> MagicMock:
    """A ``metrics_facade`` whose ``time_function`` is a real pass-through.

    ``create_approval_request`` is decorated with ``@time_function(...)``. A bare
    ``MagicMock`` replaces it with a call-swallowing mock, so no approval row
    would be written and the boundary assertion below would hold vacuously.
    """
    stub = MagicMock()
    stub.time_function = lambda *_a, **_k: lambda f: f
    stub.record_error = MagicMock()
    stub.ApprovalMetrics = MagicMock()
    stub.PipelineMetrics = None
    return stub


def _load_handler():
    """Exec the handler module once, under a name unique to this file."""
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER

    stubs = {**_LAYER_STUBS, "metrics_facade": _metrics_stub()}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "approval_handler_first_run_payload_read_lf",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    for name in ("create_approval_request", "get_approval_data"):
        assert inspect.isfunction(getattr(module, name)), (
            f"{name} is not a plain function — the @time_function decorator was replaced by a "
            f"mock, which would make the boundary assertion in this file vacuous"
        )
    _HANDLER = module
    return _HANDLER


@pytest.fixture
def approval_env(mock_lambda_env, mock_dynamodb_tables):
    """The handler bound to the shared fixture's moto tables."""
    handler = _load_handler()

    region = os.environ.get("AWS_REGION", "us-west-2")
    handler.dynamodb = boto3.resource("dynamodb", region_name=region)
    handler.approvals_table = mock_dynamodb_tables[APPROVALS_TABLE]
    handler.state_table = mock_dynamodb_tables[STATE_TABLE]
    handler.task_tokens_table = mock_dynamodb_tables[TASK_TOKENS_TABLE]

    return SimpleNamespace(
        handler=handler,
        approvals=mock_dynamodb_tables[APPROVALS_TABLE],
        state=mock_dynamodb_tables[STATE_TABLE],
    )


def _seed(env, *rows: dict) -> None:
    for row in rows:
        env.state.put_item(Item=row)


def _read(env) -> dict[str, Any]:
    return env.handler.get_approval_data("first_run", RUN_ID)


def _event() -> dict[str, Any]:
    return {
        "approval_type": "first_run",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": TASK_TOKEN,
    }


# ---------------------------------------------------------------------------
# R4.1 — the two corrected keys reach the approver
# ---------------------------------------------------------------------------
def test_the_two_corrected_keys_resolve(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """``quality_checks`` and ``data_quality_rules`` are no longer null.

    Both were ``None`` for every run before C5, against a state row faithful to
    what ``create_quality_checks`` writes — which is what the fixture is.

    **Validates: Requirements 4.1**
    """
    env = approval_env
    _seed(env, first_run_infrastructure_state_row, first_run_quality_checks_state_row)

    payload = _read(env)

    unresolved = sorted(key for key in CORRECTED_KEYS if payload.get(key) is None)
    assert unresolved == [], (
        f"R4.1: these were read off the row's TOP level, where create_quality_checks writes "
        f"nothing under that spelling, so they were null for every run: {unresolved}"
    )

    rules = payload["data_quality_rules"]
    assert isinstance(rules, list) and rules, f"the generated rule list must survive the read — got {rules!r}"
    assert {rule["rule_name"] for rule in rules} >= {"row_count_check", "duplicate_check"}, (
        f"the two unconditional overall rules create_quality_checks always appends (:186-206) "
        f"must be present; got {[r.get('rule_name') for r in rules]}"
    )

    lines = payload["quality_checks"]
    assert isinstance(lines, list) and lines, f"the pre-run check lines must survive the read — got {lines!r}"
    assert "5 quality rules configured" in lines, (
        f"the one line in pre_run_validation.quality_checks that is actually derived — the rule "
        f"count the approver is shown — must be present; got {lines!r}"
    )


def test_the_two_corrected_keys_are_read_from_output_data_and_not_the_top_level(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """Decoys at the top level lose to the producer's payload.

    A row carrying a value at the top level under each corrected key is not a
    shape any producer writes — no writer puts either key there, which is the
    defect. It is here because it is the only way to prove *which* address the
    branch read, rather than that it read something.

    **Validates: Requirements 4.1**
    """
    env = approval_env
    decoys = {key: f"TOP-LEVEL-DECOY-{key}" for key in CORRECTED_KEYS}
    _seed(env, first_run_infrastructure_state_row, {**first_run_quality_checks_state_row, **decoys})

    payload = _read(env)

    wrong = {key: value for key, value in payload.items() if isinstance(value, str) and value.startswith("TOP-LEVEL")}
    assert wrong == {}, (
        f"R4.1: these keys were still read off the row's top level, where the writer writes "
        f"nothing (combine_infrastructure_and_quality_approval_data -> output_data.approval, "
        f"create_quality_checks:433): {wrong}"
    )


def test_data_quality_rules_falls_back_to_the_rows_own_configuration(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """The second source is the same writer's own ``output_data``.

    ``quality_config`` is passed into the combiner by reference, so
    ``output_data.quality_configuration`` and
    ``output_data.approval.quality_check_configuration`` are one object at write
    time. Dropping the ``approval`` copy must therefore still resolve — the
    two-source shape ``code_version_id`` uses at ``approval_handler:381``.

    **Validates: Requirements 4.1**
    """
    env = approval_env
    row = first_run_quality_checks_state_row
    del row["output_data"]["approval"]["quality_check_configuration"]
    _seed(env, first_run_infrastructure_state_row, row)

    payload = _read(env)

    rules = payload["data_quality_rules"]
    assert isinstance(rules, list) and rules, (
        f"with the approval-payload copy absent, the read must fall through to "
        f"output_data.quality_configuration.quality_rules — got {rules!r}"
    )


# ---------------------------------------------------------------------------
# R4.3 — quality_verdict is unchanged
# ---------------------------------------------------------------------------
def test_quality_verdict_still_reads_from_output_data(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """The one key in this branch that was already correct stays correct.

    R4.3 / do-NOT 5. Asserted against a resolving value rather than against
    ``None``, so the test would fail if C5 had moved this read while relocating
    its two neighbours — the realistic way to break it.

    **Validates: Requirements 4.3**
    """
    env = approval_env
    _seed(env, first_run_infrastructure_state_row, first_run_quality_checks_state_row)

    payload = _read(env)

    verdict = payload[UNTOUCHED_KEY]
    assert isinstance(verdict, dict) and verdict.get("mode") == "advisory", (
        f"quality_verdict must still resolve from output_data.quality_verdict "
        f"(create_quality_checks:432) — got {verdict!r}"
    )


def test_the_out_of_scope_infra_reads_are_still_null(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """C5 corrected two keys, not five — pinned so that stays legible.

    All three ``create_infrastructure`` reads in this branch are also wrong, in
    three different ways: ``infrastructure_status`` is written at
    ``output_data.infrastructure_status`` and as a **dict** where the consumer
    casts it to a string; ``cloudformation_stack_id`` has no producer anywhere in
    that Lambda; ``glue_job_name`` belongs to a different step's row. R4.1 scopes
    C5 to two keys, so these are recorded and left.

    This assertion is a **statement of scope, not an endorsement**. It exists so
    the follow-up has a starting point and so this change cannot be read as
    having fixed them. It is the assertion to invert when that follow-up lands.

    **Validates: Requirements 4.1**
    """
    env = approval_env
    _seed(env, first_run_infrastructure_state_row, first_run_quality_checks_state_row)

    payload = _read(env)

    resolved = sorted(key for key in OUT_OF_SCOPE_INFRA_KEYS if payload.get(key) is not None)
    assert resolved == [], (
        f"these three reads are out of C5's scope and were expected to stay null against a row "
        f"faithful to create_infrastructure's writes. If they now resolve, either the writer "
        f"changed or someone widened C5 past R4.1: {resolved}"
    )


# ---------------------------------------------------------------------------
# R1.3 — degraded rows do not raise
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutation", "label"),
    [
        (lambda row: row.pop("output_data"), "output_data absent"),
        (lambda row: row.__setitem__("output_data", None), "output_data None"),
        (lambda row: row.__setitem__("output_data", "a json string"), "output_data not a dict"),
        (lambda row: row["output_data"].__setitem__("approval", None), "approval None"),
        (lambda row: row["output_data"].__setitem__("approval", ["not", "a", "dict"]), "approval not a dict"),
        (
            lambda row: row["output_data"]["approval"].__setitem__("pre_run_validation", "not a dict"),
            "pre_run_validation not a dict",
        ),
        (
            lambda row: row["output_data"]["approval"].__setitem__("quality_check_configuration", 7),
            "quality_check_configuration not a dict",
        ),
    ],
)
def test_a_degraded_row_yields_absent_keys_rather_than_raising(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row, mutation, label
):
    """Every level C5 traverses degrades to ``{}`` instead of raising.

    The ``isinstance`` guard shape is the ``code_generation`` branch's
    ``template_selection`` treatment at ``approval_handler:374-376``, applied to
    each new level. Rows written before C5's producer existed are exactly the
    ``approval``-absent case, so this is a live path and not a hypothetical.

    Note ``get_approval_data``'s outer ``except`` returns ``{}`` and logs, so a
    raise would NOT surface as an exception here — it would surface as an empty
    payload. That is why this asserts on the key being *present and null* rather
    than merely on not raising: the weaker assertion would pass on a swallowed
    ``TypeError``.

    **Validates: Requirements 4.1**
    """
    env = approval_env
    row = first_run_quality_checks_state_row
    mutation(row)
    _seed(env, first_run_infrastructure_state_row, row)

    payload = _read(env)

    assert payload != {}, (
        f"{label}: the payload came back empty, which is get_approval_data's except branch — "
        f"a guard is missing and the exception was swallowed rather than avoided"
    )
    for key in CORRECTED_KEYS:
        assert key in payload, f"{label}: {key} must still be present, valued None"


# ---------------------------------------------------------------------------
# R1.6 / O2 — Decimal is now reachable on this branch too
# ---------------------------------------------------------------------------
def test_the_rule_lists_decimals_survive_the_serialization_boundary(
    approval_env, first_run_infrastructure_state_row, first_run_quality_checks_state_row
):
    """A populated ``first_run`` payload serialises, ``Decimal``s and all.

    Same condition R1.6 flagged for ``schema_mapping``, arriving on this branch
    for the same reason: the boundary conversion at ``approval_handler:659``
    (``convert_decimals_to_float`` before ``json.dumps``, ``LGR-86698d4-1``,
    template / ratified) had never had a populated ``first_run`` rule list to
    convert, because the branch never surfaced one. C5 is the change that first
    makes those ``Decimal``s reachable.

    The fixture's numerics are real ``decimal.Decimal`` — ``min_rows`` and the
    ``min_value`` / ``max_value`` pair — because a ``float`` fixture would
    serialise cleanly and pass while the real path raised.

    **Validates: Requirements 4.1 — Design O2, CP1**
    """
    env = approval_env
    _seed(env, first_run_infrastructure_state_row, first_run_quality_checks_state_row)

    payload = _read(env)

    range_rules = [rule for rule in payload["data_quality_rules"] if rule["rule_name"] == "customer_id_range_check"]
    assert len(range_rules) == 1
    assert isinstance(range_rules[0]["max_value"], Decimal), (
        "the fixture must hand the read real Decimals — a float fixture makes this test vacuous"
    )

    env.handler.create_approval_request(_event())

    rows = env.approvals.scan()["Items"]
    assert len(rows) == 1, f"expected exactly one approval row, got {len(rows)}"
    stored = json.loads(rows[0]["approval_data"])

    stored_range = [rule for rule in stored["data_quality_rules"] if rule["rule_name"] == "customer_id_range_check"]
    assert len(stored_range) == 1
    assert stored_range[0]["max_value"] == pytest.approx(999999.5), (
        f"the boundary conversion must turn Decimal('999999.5') into a JSON number; "
        f"got {stored_range[0]['max_value']!r}"
    )
    assert stored["quality_checks"], "the pre-run check lines must survive serialization too"
