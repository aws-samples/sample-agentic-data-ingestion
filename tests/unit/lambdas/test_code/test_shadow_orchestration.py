# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shadow-mode orchestration: fail-soft, the deadline, and the state row.

Spec ``real-spark-verification`` tasks 10.3 and 10.5. Requirements 4.1, 4.3,
4.4 and 4.6, design §4.7.

**No Glue session is created anywhere in this file.** Every Glue-side outcome
arrives through the ``runner_factory`` seam, which exists so that a test can
produce a success, a raised exception or a late return without a Glue client and
without a billable session.

What is pinned here, and why each would fail differently:

* the row is the **separate** ``(run_id, "shadow_verification")`` row, carrying
  no ``status``/``step_sequence`` and nothing verdict-shaped (R4.3). A
  regression is a comparison payload landing somewhere a gate can read.
* the item is **DynamoDB-writable**: no ``float`` survives into it. A regression
  is a silent ``Float types are not supported`` at ``PutItem`` — a fail-soft
  path that fails to record, which is the one failure mode fail-soft cannot
  cover for itself.
* **fail-soft** (R4.4): a raising runner, a raising factory and a raising writer
  each return a label instead of propagating. A regression turns a working step
  into a failing one, which ``LGR-60c4985-5`` names as the thing this discipline
  exists to prevent.
* the **deadline** (R4.6) in both directions — not started when the step has
  already spent its budget, abandoned when the result arrives late — and, in
  task 10.5's section at the foot of the file, at its boundaries, through its
  configuration, and as a mechanism that is deliberately post-hoc rather than
  preemptive.

Neighbouring coverage, deliberately not duplicated here: task 10.4's Property 15
generates arbitrary Glue-side outcomes and compares ``(test_status, checks)``
against the ``pandas`` run.
"""

import math
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The stub set the sibling handler tests use. `shadow_comparison` is NOT stubbed:
# the orchestration is only meaningful against the real comparison, which is a
# pure function and needs nothing. `glue_session_runner` is not stubbed either —
# nothing here reaches it, and a stub would hide it if something did.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


ROLE = "role/AgenticDataIngestion-VerificationSession-Role"

# A pandas-proxy result in the Test_Result_Contract's shape, trimmed to the
# dimensions `shadow_comparison` compares. `schema_compliance` is a float on
# purpose: it is the value that makes the DynamoDB conversion load-bearing.
PANDAS_RESULT = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "output_records": 100,
    "data_quality_results": {
        "duplicates": 0,
        "schema_compliance": 83.3,
        "missing_columns": ["city"],
        "null_counts": {"id": 0, "name": 5},
    },
}

# The same transform under real Spark, disagreeing on two dimensions.
GLUE_RESULT = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "output_records": 100,
    "data_quality_results": {
        "duplicates": 0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "null_counts": {"id": 0, "name": 5},
    },
}


class _Recorder:
    """A writer seam that keeps what it was handed."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, item: dict) -> None:
        self.items.append(item)


def _factory_returning(result):
    """A ``runner_factory`` whose runner returns *result* from ``run_test``."""

    def factory(**kwargs):
        runner = MagicMock()
        runner.run_test.return_value = result
        factory.kwargs = kwargs
        factory.runner = runner
        return runner

    factory.kwargs = None
    factory.runner = None
    return factory


def _factory_raising(exc):
    def factory(**kwargs):
        runner = MagicMock()
        runner.run_test.side_effect = exc
        return runner

    return factory


def _stepped_clock(*readings):
    """A monotonic-shaped clock returning each reading in turn, then the last."""
    values = list(readings)

    def clock():
        return values.pop(0) if len(values) > 1 else values[0]

    return clock


def _record(**overrides):
    """Run the orchestration with the common arguments, overridable."""
    kwargs = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "transform_block": "df = df",
        "source_schema": {"fields": [{"name": "id", "type": "string"}]},
        "target_schema": {"properties": {"id": {"type": "string"}}},
        "pandas_result": PANDAS_RESULT,
        "source_data_uri": "s3://bucket/key.json",
        "session_role_arn": ROLE,
        "step_started_at": 0.0,
        "clock": _stepped_clock(1.0),
        "runner_factory": _factory_returning(GLUE_RESULT),
        "writer": _Recorder(),
    }
    kwargs.update(overrides)
    return _mod.record_shadow_verification(**kwargs), kwargs


# ──────────────────────────────────────────────────────────────────────────
# The deadline knob
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", "  ", "abc", "12.5", "0", "-1", "-900"])
def test_an_unusable_deadline_falls_through_to_the_default(raw):
    """R4.6's budget is configurable, and an unusable value is not fatal.

    Zero and negatives are refused rather than honoured: zero would disable the
    shadow half through a knob that reads like a timeout, and turning shadow
    mode off is what ``VERIFICATION_EXECUTOR`` is for.
    """
    assert _mod.resolve_shadow_deadline_seconds(raw) == _mod.DEFAULT_SHADOW_DEADLINE_SECONDS


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("120", 120), ("900", 900), ("1800", 1800)])
def test_a_positive_integer_deadline_is_honoured(raw, expected):
    assert _mod.resolve_shadow_deadline_seconds(raw) == expected


def test_the_default_deadline_leaves_room_inside_the_steps_own_timeout():
    """Design C3 — TestCode's 1200 s is not headroom.

    Pinned as an inequality rather than as the literal 900 so that raising the
    default has to stay inside the enclosing step's timeout, which is the
    constraint rather than the number.
    """
    assert _mod.DEFAULT_SHADOW_DEADLINE_SECONDS < 1200
    assert _mod.DEFAULT_SHADOW_DEADLINE_SECONDS > _mod.SHADOW_GLUE_WORST_CASE_SECONDS


def test_the_worst_case_bound_still_covers_the_runners_own_budgets():
    """The drift fence on :data:`SHADOW_GLUE_WORST_CASE_SECONDS`.

    That constant is a second spelling of numbers owned by
    ``glue_session_runner``, spelled again so the default path does not import a
    Glue client to read them. A second spelling is only safe if something
    compares the two, so this does — if the runner's per-stage budgets rise, the
    pre-flight check silently stops covering the worst case without this test.
    """
    import glue_session_runner as runner_mod

    worst_case = (
        runner_mod._SESSION_READY_DEADLINE_SECONDS
        + 3 * runner_mod._STATEMENT_DEADLINE_SECONDS
        + runner_mod._RELEASE_DEADLINE_SECONDS
    )
    assert worst_case <= _mod.SHADOW_GLUE_WORST_CASE_SECONDS


@pytest.mark.parametrize(
    ("elapsed", "fits"),
    [
        (0.0, True),
        (100.0, True),
        # The boundary: the runner's whole worst case has to fit, so the last
        # instant a shadow run may start is `deadline - worst_case`.
        (900 - 660, True),
        (900 - 660 + 0.5, False),
        (900.0, False),
        (5000.0, False),
    ],
)
def test_a_shadow_run_starts_only_when_its_worst_case_fits(elapsed, fits):
    """R4.6 — the pre-flight half, and the only half that can prevent an overrun.

    The deadline cannot preempt a call in progress (a thread is forbidden here by
    ``LGR-b46a5bb-1``), so "is there budget left" is the wrong question; "does
    the whole worst case still fit" is the one with teeth.
    """
    assert _mod.shadow_glue_run_fits(elapsed, 900) is fits


# ──────────────────────────────────────────────────────────────────────────
# The item
# ──────────────────────────────────────────────────────────────────────────


def _item(**overrides):
    kwargs = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "comparison": {"disagreements": [], "omitted": 0, "dimensions_compared": 8},
        "shadow_status": _mod.SHADOW_STATUS_COMPARED,
        "elapsed_seconds": 12.7,
    }
    kwargs.update(overrides)
    return _mod.build_shadow_verification_item(**kwargs)


def test_the_item_is_keyed_on_the_separate_shadow_row():
    """R4.3 — ``(run_id, "shadow_verification")``, which no gate reads."""
    item = _item()
    assert item["run_id"] == "run-1"
    assert item["step_name"] == "shadow_verification"
    assert _mod.SHADOW_VERIFICATION_STEP_NAME == "shadow_verification"


def test_the_shadow_row_name_is_not_a_step_name():
    """It is a record row, not a Step_Row — the ``load_metrics`` category.

    ``step_names.from_any`` raising on it is the property that keeps
    ``execution_updater``'s allowlist routing it away from the step machinery
    without any change there.
    """
    from step_names import from_any

    with pytest.raises(ValueError, match="Unknown step name"):
        from_any(_mod.SHADOW_VERIFICATION_STEP_NAME)


def test_the_item_carries_nothing_a_gate_could_read_as_a_verdict():
    """R4.3 by construction.

    No ``status`` and no ``step_sequence`` — their absence is what says this is
    not a Step_Row — and nothing verdict-shaped, because a field that looks like
    a verdict invites the reader that would break R4.3.
    """
    item = _item()
    for absent in ("status", "step_sequence", "test_status", "checks", "approval_required", "output_data"):
        assert absent not in item


def test_the_item_carries_its_pipeline_id():
    """``LGR-9e76249-3`` made this call explicitly for the ``load_metrics`` row:
    omitting ``pipeline_id`` to make a downstream reader drop the row is "a free
    dodge resting on an omission rather than on a rule"."""
    assert _item()["pipeline_id"] == "pipe-1"


def test_the_item_reports_the_bound_and_what_was_comparable():
    """R4.5 — ``omitted`` distinguishes a bounded record from a complete one, and
    ``dimensions_compared`` distinguishes agreement from nothing to compare."""
    item = _item(comparison={"disagreements": [{"dimension": "x"}], "omitted": 41, "dimensions_compared": 300})
    assert item["omitted"] == 41
    assert item["dimensions_compared"] == 300
    assert item["disagreements"] == [{"dimension": "x"}]


def _floats_in(value):
    """Every ``float`` reachable in *value*, at any depth."""
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for item in value.values() for f in _floats_in(item)]
    if isinstance(value, list):
        return [f for item in value for f in _floats_in(item)]
    return []


def test_no_float_survives_into_the_item():
    """The named trap in task 10.1's note to this writer.

    ``compare_results`` passes executor values through as the executors produced
    them, so ``schema_compliance`` arrives as a ``float`` — and the DynamoDB
    resource client rejects ``float`` outright. Converting inside the comparison
    would change the values it reports, so it is this writer's job. Asserted
    recursively, because the floats are nested inside disagreement entries
    rather than at the top level.
    """
    comparison = {
        "disagreements": [
            {"dimension": "schema_compliance", "pandas": 83.3, "glue": 100.0},
            {"dimension": "null_counts[name]", "pandas": 5, "glue": 4},
        ],
        "omitted": 0,
        "dimensions_compared": 9,
    }
    item = _item(comparison=comparison, elapsed_seconds=12.7)
    assert _floats_in(item) == []
    entry = item["disagreements"][0]
    assert entry["pandas"] == Decimal("83.3")
    assert entry["glue"] == Decimal("100.0")
    # A string round-trip, not a binary-float artifact (`shared/decimal_utils`).
    assert str(entry["pandas"]) == "83.3"


def test_the_elapsed_time_is_recorded_as_an_int():
    """``LGR-9e76249-3``'s producer-side discipline for the sibling row: ints and
    strings only."""
    item = _item(elapsed_seconds=12.7)
    assert item["shadow_elapsed_seconds"] == 13
    assert isinstance(item["shadow_elapsed_seconds"], int)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_float_becomes_a_string_rather_than_losing_the_record(bad):
    """``Decimal("nan")`` is as unwritable as a ``float``.

    Not an expected input — no compared dimension is a value either executor
    computes as non-finite — but the cost of being wrong about that would be
    losing the whole record on a path whose entire job is to record something.
    """
    comparison = {"disagreements": [{"dimension": "d", "pandas": bad}], "omitted": 0, "dimensions_compared": 1}
    item = _item(comparison=comparison)
    recorded = item["disagreements"][0]["pandas"]
    assert isinstance(recorded, str)
    assert recorded.startswith("non_finite:")
    assert _floats_in(item) == []


def test_a_finite_float_is_left_alone_by_the_non_finite_guard():
    assert _mod._finite_floats_only({"a": [1.5, {"b": 2.5}]}) == {"a": [1.5, {"b": 2.5}]}
    assert math.isfinite(_mod._finite_floats_only(1.5))


# ──────────────────────────────────────────────────────────────────────────
# Orchestration — the happy path
# ──────────────────────────────────────────────────────────────────────────


def test_both_executors_ran_and_the_disagreements_are_recorded():
    """R4.2 — the row carries an entry per differing dimension and no more."""
    status, kwargs = _record()
    assert status == _mod.SHADOW_STATUS_COMPARED
    (item,) = kwargs["writer"].items
    dimensions = {entry["dimension"] for entry in item["disagreements"]}
    # The two the fixtures differ on, and nothing else.
    assert dimensions == {"schema_compliance", "missing_columns"}
    assert item["shadow_status"] == _mod.SHADOW_STATUS_COMPARED
    assert item["verification_executor"] == "shadow"


def test_the_glue_runner_is_built_with_the_role_and_the_wired_uri():
    """The shadow half constructs the runner the same way the ``glue`` branch
    does — role ARN from configuration, ``source_data_uri`` on the constructor
    (R9.6), never a fourth argument to ``run_test``."""
    factory = _factory_returning(GLUE_RESULT)
    _record(runner_factory=factory)
    assert factory.kwargs["role_arn"] == ROLE
    assert factory.kwargs["source_data_uri"] == "s3://bucket/key.json"
    factory.runner.run_test.assert_called_once_with(
        "df = df",
        {"fields": [{"name": "id", "type": "string"}]},
        {"properties": {"id": {"type": "string"}}},
    )


def test_agreement_records_a_row_with_no_disagreements():
    """An agreeing pair still writes the row.

    ``LGR-28b4d75-3``'s argument, one domain over: an early return would make
    "the two agreed" indistinguishable in the database from a shadow half that
    never ran.
    """
    status, kwargs = _record(runner_factory=_factory_returning(dict(PANDAS_RESULT)))
    assert status == _mod.SHADOW_STATUS_COMPARED
    (item,) = kwargs["writer"].items
    assert item["disagreements"] == []
    assert item["dimensions_compared"] > 0


# ──────────────────────────────────────────────────────────────────────────
# Fail-soft (R4.4)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        TimeoutError("the session never reached READY"),
        # The capacity refusal R4.4 names, as the runner would surface it if it
        # escaped rather than being mapped.
        Exception("ResourceNumberLimitExceededException"),
    ],
)
def test_a_raising_glue_half_is_recorded_rather_than_propagated(exc):
    """R4.4 — an exception, a timeout and a capacity refusal all record instead
    of raising. ``compare_results`` documents a ``None`` Glue side as a normal
    input for exactly this path."""
    status, kwargs = _record(runner_factory=_factory_raising(exc))
    assert status == _mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == _mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING
    # Every entry names a pandas value and no Glue value: the Glue side carried
    # nothing, which is a different fact from the two sides agreeing.
    assert item["disagreements"]
    assert all("glue" not in entry for entry in item["disagreements"])


def test_a_factory_that_cannot_even_construct_a_runner_is_fail_soft():
    """The Glue client being unavailable is the same category of event as the
    session failing, and must not escape either."""

    def factory(**kwargs):
        raise ImportError("no glue_session_runner in this environment")

    status, kwargs = _record(runner_factory=factory)
    assert status == _mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING
    assert len(kwargs["writer"].items) == 1


def test_a_failing_write_is_fail_soft_and_returns_a_label():
    """The write is the last thing that can fail, and R4.4 covers it too.

    A ``PutItem`` rejection — a float that slipped through, a throttle, an item
    over 400 KB — loses the measurement. It must not lose the run.
    """

    def writer(item):
        raise RuntimeError("Float types are not supported")

    status, _ = _record(writer=writer)
    assert status == _mod.SHADOW_STATUS_ERROR


def test_a_comparison_that_raises_is_fail_soft():
    """The one ``except Exception`` covers the whole body, including code this
    task does not own."""
    with patch.object(_mod, "build_shadow_verification_item", side_effect=ValueError("nope")):
        status, kwargs = _record()
    assert status == _mod.SHADOW_STATUS_ERROR
    assert kwargs["writer"].items == []


def test_a_missing_session_role_declines_the_glue_half_cleanly():
    """``shadow`` deliberately survives the role guard in
    ``resolve_effective_executor`` — its authoritative half needs no role — so
    arriving here with no ARN must decline rather than raise."""
    factory = _factory_returning(GLUE_RESULT)
    status, kwargs = _record(session_role_arn="", runner_factory=factory)
    assert status == _mod.SHADOW_STATUS_NO_SESSION_ROLE
    assert factory.kwargs is None, "no session may be created without a role to pass it"
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == _mod.SHADOW_STATUS_NO_SESSION_ROLE


def test_a_pandas_result_of_none_is_survivable():
    """Nothing about the shadow half may depend on the pandas half having
    produced a dict — the authoritative path has its own failure shapes."""
    status, kwargs = _record(pandas_result=None)
    assert status == _mod.SHADOW_STATUS_COMPARED
    assert len(kwargs["writer"].items) == 1


# ──────────────────────────────────────────────────────────────────────────
# The deadline, end to end (R4.6) — the worked examples task 10.5 builds on
# ──────────────────────────────────────────────────────────────────────────


def test_a_spent_budget_means_the_glue_half_never_starts():
    """The pre-flight check, through the orchestration.

    The step arriving here with 880 s of a 900 s budget spent is design C3's
    case: the review agent's reparse retry ate the wall clock, and a 660 s Glue
    run on top would push a step that does not fail fast towards its 1200 s
    timeout.
    """
    factory = _factory_returning(GLUE_RESULT)
    status, kwargs = _record(clock=_stepped_clock(880.0), runner_factory=factory, deadline_seconds=900)
    assert status == _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE
    assert factory.kwargs is None
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE


def test_a_late_result_is_abandoned_rather_than_compared():
    """The post-flight check: the result arrived, but past the budget.

    Discarded rather than recorded, because a measurement that overran is not
    evidence about a run that has already been decided — and recording it would
    hide that the budget was exceeded.
    """
    # Starts at 10 s of a 900 s budget — early enough that the pre-flight check
    # lets it start — and returns at 1000 s, having overrun the runner's own
    # per-stage deadlines as well.
    clock = _stepped_clock(10.0, 1000.0, 1000.0)
    status, kwargs = _record(clock=clock, deadline_seconds=900)
    assert status == _mod.SHADOW_STATUS_ABANDONED_DEADLINE
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == _mod.SHADOW_STATUS_ABANDONED_DEADLINE
    # The abandoned result is not in the row: every entry has a pandas side only.
    assert all("glue" not in entry for entry in item["disagreements"])


def test_a_result_inside_the_budget_is_compared():
    """The other direction, so the test above is not passing for the wrong
    reason."""
    clock = _stepped_clock(10.0, 120.0, 120.0)
    status, kwargs = _record(clock=clock, deadline_seconds=900)
    assert status == _mod.SHADOW_STATUS_COMPARED
    (item,) = kwargs["writer"].items
    assert any("glue" in entry for entry in item["disagreements"])


# ──────────────────────────────────────────────────────────────────────────
# The default writer
# ──────────────────────────────────────────────────────────────────────────


def test_the_default_writer_puts_the_row_on_the_state_table():
    """``PutItem``, not ``UpdateItem`` (design §4.7).

    This row has exactly one writer and no other attributes to preserve, which
    is the condition ``build_state_row_update``'s comment names as the reason the
    ``test_code`` row cannot use ``PutItem``: there, a full-row write deleted
    another writer's attribute in the same invocation.
    """
    item = _item()
    _mod.pipeline_runs_table.put_item.reset_mock()
    _mod.pipeline_runs_table.update_item.reset_mock()
    _mod._put_shadow_verification_row(item)
    _mod.pipeline_runs_table.put_item.assert_called_once_with(Item=item)
    _mod.pipeline_runs_table.update_item.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# The handler wiring
# ──────────────────────────────────────────────────────────────────────────


def test_the_handler_records_the_shadow_row_only_under_shadow():
    """The dispatch is keyed on ``shadow`` and calls this orchestration.

    Asserted against the handler's own source rather than by driving the whole
    handler, for the reason the sibling selection tests give: reaching Phase 2
    needs a review verdict, a pipeline record and a generated-code artifact. The
    weaker assertion still catches the failure that matters here — an
    orchestration with no caller.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert "if verification_executor == VERIFICATION_EXECUTOR_SHADOW:" in source
    assert source.count("record_shadow_verification(") >= 2  # the def and the call


def test_the_shadow_half_runs_after_the_verdict_is_computed():
    """R4.1/R4.3 by placement, not by discipline.

    ``test_status`` and ``checks`` are bound before the shadow dispatch, so the
    shadow half cannot be an input to them. A refactor that hoists the dispatch
    above the aggregation would put a Glue result in scope before the verdict
    exists, which is the state R4.1 exists to prevent.
    """
    import inspect

    source = inspect.getsource(_mod)
    aggregation = source.index("test_status, checks = aggregate_check_results(")
    dispatch = source.index("if verification_executor == VERIFICATION_EXECUTOR_SHADOW:")
    assert aggregation < dispatch


def test_the_shadow_half_is_not_on_a_thread():
    """``LGR-b46a5bb-1`` (standing invariant) forbids a thread for post-response
    work in ``lambda/**``: the environment freezes on return and kills it. The
    repo-wide AST guard covers this too; asserted here because a second executor
    run added after the verdict is precisely the shape that invites one.

    Asserted against the import and the constructor rather than the bare word.
    ``LGR-b46a5bb-4`` made that same call for the repo-wide guard and recorded
    why: substring matching "forbids documenting the fence", and it failed for
    exactly that reason against a docstring that named the antipattern on
    purpose. This module's prose names it too.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert "import threading" not in source
    assert "Thread(" not in source


# ──────────────────────────────────────────────────────────────────────────
# The default runner factory — the seam's production side
# ──────────────────────────────────────────────────────────────────────────
#
# Everything above drives the Glue half through an injected `runner_factory`,
# which is what keeps a unit test from creating a billable session. That leaves
# the factory the handler ACTUALLY uses with no coverage at all, and it is the
# only part of this orchestration that runs in production unmocked. The three
# tests below cover it without a Glue client: the module is stubbed on
# `sys.modules`, so the function-local import resolves to the stub.


def test_the_default_factory_constructs_the_glue_runner_with_its_four_arguments():
    """The production factory passes region, role ARN, URI and Frame_Mode.

    **Amended by p44 task 10.1, and the amendment is a reversal rather than a
    regression** (``LGR-60c4985-6``, **template / ratified**, on amending an
    assertion that encoded a hold rather than deleting it; ``LGR-5906ec6-2`` for
    the underlying convention). This test previously asserted *three* arguments
    and its own docstring gave the reason as "so the shadow half verifies the
    same transform against the same frame source as a ``glue`` run would". That
    parity is what p44 R1.6 establishes is **unimplementable**, not merely
    unimplemented: the pandas half is ``CodeInterpreterTestRunner(region=…)``,
    which has no source read of any kind, so a real sample on the Glue half
    would make every data-quality dimension disagree for a reason that has
    nothing to do with either executor. The shadow half is therefore pinned to
    ``FRAME_MODE_SYNTHETIC`` and a Sample_Frame is reachable only under the
    non-shadow ``glue`` executor.

    ``source_data_uri`` is still on the CONSTRUCTOR and ``run_test`` still takes
    three arguments (R9.6, p42 C6) — that half is unchanged.

    The Frame_Mode is asserted as **the stub module's own attribute**, not as a
    transcribed ``"synthetic"``. Two reasons. It pins that the factory takes the
    value from ``glue_session_runner``'s vocabulary rather than spelling a bare
    string, so the pin cannot drift from the value the runner recognises; and it
    is what makes the import safe at that site — the constant arrives on the same
    statement as ``GlueSessionTestRunner``, so a test that stubs the module has
    stubbed the runner too and is mocking the whole factory. There is no state in
    which the constant is a ``MagicMock`` while a real runner is constructed.
    """
    runner_class = MagicMock(return_value="the-runner")
    stub = MagicMock(GlueSessionTestRunner=runner_class)
    with patch.dict("sys.modules", {"glue_session_runner": stub}):
        result = _mod._default_shadow_glue_runner(
            region="us-east-1",
            role_arn=ROLE,
            source_data_uri="s3://bucket/key.json",
        )
    assert result == "the-runner"
    runner_class.assert_called_once_with(
        region="us-east-1",
        role_arn=ROLE,
        source_data_uri="s3://bucket/key.json",
        frame_mode=stub.FRAME_MODE_SYNTHETIC,
    )


def test_the_glue_runner_import_stays_function_local():
    """The default path must not import a Glue client it will not use.

    ``LGR-60c4985-5``'s reason for importing ``retry_utils`` lazily, one domain
    over: 12 test files patch ``CodeInterpreterTestRunner`` on this module and
    none of them stubs ``glue_session_runner``, so hoisting this import to module
    level changes the import graph for every caller that never selects ``shadow``
    or ``glue``. Asserted on indentation rather than on absence, because the
    import has to exist somewhere — what matters is that it is inside a function
    body.
    """
    import inspect

    lines = [
        line for line in inspect.getsource(_mod).splitlines() if line.lstrip().startswith("from glue_session_runner ")
    ]
    assert lines, "the Glue runner import has disappeared entirely"
    for line in lines:
        assert line.startswith(" "), f"module-level Glue import: {line!r}"


def test_omitting_the_factory_reaches_the_production_one():
    """The seam defaults to production rather than to nothing.

    Without this, every test above could pass against an orchestration whose
    ``runner_factory`` default was wrong — the shadow half would then record
    ``glue_produced_nothing`` forever and look fail-soft while verifying
    nothing.
    """
    with patch.object(_mod, "_default_shadow_glue_runner", MagicMock(wraps=_factory_returning(GLUE_RESULT))) as default:
        status, kwargs = _record(runner_factory=None)
    assert status == _mod.SHADOW_STATUS_COMPARED
    default.assert_called_once()
    assert default.call_args.kwargs["role_arn"] == ROLE
    assert default.call_args.kwargs["source_data_uri"] == "s3://bucket/key.json"
    assert len(kwargs["writer"].items) == 1


# ──────────────────────────────────────────────────────────────────────────
# The deadline, completed (R4.6) — task 10.5
# ──────────────────────────────────────────────────────────────────────────
#
# Task 10.3 built the worked examples above: a spent budget declines, a late
# result is abandoned, an on-time result is compared, and the knob resolves.
# What it left is the edges and the wiring, which is what these add:
#
#   * the post-flight boundary, which is `>` and therefore admits a run that
#     lands *exactly* on the budget;
#   * the budget axis of the pre-flight check — 10.3 varied `elapsed` against a
#     fixed 900, so a budget too small for the worst case was never tested;
#   * the environment as the budget's source, which no test reached because
#     "unset resolves to 900" is indistinguishable from "never read";
#   * the WARNING the resolver's docstring promises;
#   * and, explicitly, that enforcement is **two-point and post-hoc** — the
#     deadline never interrupts a call in progress.
#
# On that last point: two points is the honest maximum here, not a gap in the
# implementation. Interrupting a `run_test` already in flight would need a
# second thread, and `LGR-b46a5bb-1` (standing invariant) forbids one anywhere
# under `lambda/**` — the environment freezes when the handler returns and kills
# it. So the deadline is enforced where this handler holds control: before the
# Glue half starts (does its whole worst case still fit) and after it returns
# (did it overrun). `test_the_deadline_does_not_interrupt_a_run_already_in_progress`
# pins that shape so a later reader does not file the absence as a bug.
#
# Nothing here waits. Every reading comes from an injected clock, which is what
# `record_shadow_verification`'s `clock` parameter exists for.


@pytest.mark.parametrize(
    ("returned_at", "expected"),
    [
        # Comfortably inside — the control.
        (400.0, _mod.SHADOW_STATUS_COMPARED),
        # One tick short of the budget.
        (899.5, _mod.SHADOW_STATUS_COMPARED),
        # Exactly on it. The post-flight check is `>`, not `>=`, so a run that
        # lands on its budget has met it: the budget is what may be spent, and
        # refusing at the boundary would discard a measurement that cost the
        # full 660 s worst case and came in on time.
        (900.0, _mod.SHADOW_STATUS_COMPARED),
        # One tick over, which is the first reading that is genuinely late.
        (900.5, _mod.SHADOW_STATUS_ABANDONED_DEADLINE),
    ],
)
def test_the_post_flight_boundary_admits_a_run_that_lands_exactly_on_time(returned_at, expected):
    """R4.6, post-flight, at the edge in both directions.

    10.3's two cases sit at 120 s and 1000 s against a 900 s budget, so either
    comparison operator passes them. These four straddle the boundary, which is
    where `>` and `>=` differ and where an off-by-one would live.
    """
    clock = _stepped_clock(0.0, returned_at)
    status, kwargs = _record(clock=clock, step_started_at=0.0, deadline_seconds=900)
    assert status == expected
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == expected


@pytest.mark.parametrize(
    ("budget", "fits"),
    [
        # The smallest budget a fresh step can run the Glue half under: the
        # worst case exactly fills it. (The default 900 is already covered at
        # elapsed 0 by ``test_a_shadow_run_starts_only_when_its_worst_case_fits``
        # and is not repeated here.)
        (660, True),
        (661, True),
        # One second short of the worst case, and below.
        (659, False),
        (300, False),
        # `resolve_shadow_deadline_seconds` honours 1 as a positive integer, so
        # this is a reachable configuration rather than a hypothetical.
        (1, False),
    ],
)
def test_a_budget_smaller_than_the_worst_case_never_fits_even_on_a_fresh_step(budget, fits):
    """The other axis of the pre-flight check, which 10.3 held fixed at 900.

    Consequence worth stating plainly, because the label is the only place it
    becomes visible: an operator who sets
    ``SHADOW_VERIFICATION_DEADLINE_SECONDS`` below
    :data:`SHADOW_GLUE_WORST_CASE_SECONDS` turns the Glue half off for every
    run, on a knob that reads like a timeout. That is the correct behaviour —
    starting a 660 s run inside a 300 s budget would overrun it, and the
    deadline cannot preempt — and it is why the outcome is recorded as
    ``not_started_deadline`` rather than passing silently.
    """
    assert _mod.shadow_glue_run_fits(0.0, budget) is fits


def test_the_budget_is_read_from_the_environment_when_not_supplied():
    """The wiring no existing test could see.

    Every orchestration test above either passes ``deadline_seconds`` outright
    or leaves it ``None`` with the variable unset — and "unset resolves to 900"
    reads identically to "the environment is never consulted". So a misspelled
    :data:`SHADOW_DEADLINE_SECONDS_ENV`, or a dropped ``os.environ.get``, would
    leave the budget permanently at its default with the whole suite green.

    Made observable through the pre-flight check: 600 is a legal positive
    integer and smaller than the 660 s worst case, so honouring it declines the
    Glue half on a step that has spent nothing.
    """
    factory = _factory_returning(GLUE_RESULT)
    with patch.dict(os.environ, {_mod.SHADOW_DEADLINE_SECONDS_ENV: "600"}):
        status, kwargs = _record(clock=_stepped_clock(0.0), step_started_at=0.0, runner_factory=factory)
    assert status == _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE
    assert factory.kwargs is None, "the configured budget was read but not applied"
    (item,) = kwargs["writer"].items
    assert item["shadow_status"] == _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE


def test_an_environment_budget_larger_than_the_default_is_honoured():
    """The same wiring in the direction the test above cannot distinguish.

    A budget read and then discarded would also produce
    ``not_started_deadline`` there. Here the step arrives with 880 s spent —
    which the 900 s default would refuse, since 880 + 660 > 900 — and the
    configured 5000 admits it. Only an honoured value reaches ``compared``.
    """
    with patch.dict(os.environ, {_mod.SHADOW_DEADLINE_SECONDS_ENV: "5000"}):
        status, kwargs = _record(clock=_stepped_clock(880.0, 900.0), step_started_at=0.0)
    assert status == _mod.SHADOW_STATUS_COMPARED
    (item,) = kwargs["writer"].items
    assert any("glue" in entry for entry in item["disagreements"])


@pytest.mark.parametrize("raw", ["", "  ", "abc", "12.5", "0", "-1", "-900"])
def test_an_unusable_budget_is_named_in_a_warning(raw, caplog):
    """The resolver's docstring says it falls through "and says so"; this is the
    "says so".

    ``test_an_unusable_deadline_falls_through_to_the_default`` asserts the
    return value, which is the safe half — a configuration typo does not fail a
    run. But silently substituting a different budget than the one an operator
    typed is exactly the state where they conclude the knob does not work. Same
    posture, and the same assertions, as R2.4's WARNING on the executor value
    (``test_unrecognised_values_are_named_in_a_warning``).
    """
    with caplog.at_level("WARNING"):
        assert _mod.resolve_shadow_deadline_seconds(raw) == _mod.DEFAULT_SHADOW_DEADLINE_SECONDS
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert repr(raw) in message, "the received value is what makes the line actionable"
    assert _mod.SHADOW_DEADLINE_SECONDS_ENV in message
    assert str(_mod.DEFAULT_SHADOW_DEADLINE_SECONDS) in message


@pytest.mark.parametrize("raw", ["1", "120", "900", "1800", None])
def test_a_usable_budget_and_absence_log_no_warning(raw, caplog):
    """``None`` is the unset default, not a mistake.

    Warning on it would put a line in the log for every correctly-deployed run,
    which is how a reader learns to ignore the case the test above cares about.
    """
    with caplog.at_level("WARNING"):
        _mod.resolve_shadow_deadline_seconds(raw)
    assert [record for record in caplog.records if record.levelname == "WARNING"] == []


def test_the_deadline_does_not_interrupt_a_run_already_in_progress():
    """Two-point enforcement, asserted as the design rather than tolerated.

    ``LGR-b46a5bb-1`` (standing invariant) forbids a thread under ``lambda/**``,
    so there is no mechanism that could preempt ``run_test`` mid-flight — and
    ``test_the_shadow_half_is_not_on_a_thread`` above pins that there is not one.
    What follows is that a Glue half which overruns runs to **completion** and
    is then discarded, rather than being cut short.

    The distinction is invisible in the outcome — both spellings would report
    ``abandoned_deadline`` — so it is asserted on the runner's own progress: the
    call is entered once, and it leaves. A future reader who mistakes the
    absence of preemption for an unfinished implementation should find this test
    before they add the thread.
    """
    now = [0.0]
    progress: list[str] = []

    def factory(**kwargs):
        runner = MagicMock()

        def run_test(*args):
            progress.append("entered")
            # The run itself overruns the 900 s budget, mid-call.
            now[0] = 1000.0
            progress.append("left")
            return GLUE_RESULT

        runner.run_test.side_effect = run_test
        return runner

    status, kwargs = _record(
        clock=lambda: now[0],
        step_started_at=0.0,
        deadline_seconds=900,
        runner_factory=factory,
    )
    assert progress == ["entered", "left"], "the overrunning call was not allowed to finish"
    assert status == _mod.SHADOW_STATUS_ABANDONED_DEADLINE
    # Discarded after the fact: the row carries the pandas side only.
    (item,) = kwargs["writer"].items
    assert all("glue" not in entry for entry in item["disagreements"])


def test_the_two_deadline_outcomes_are_distinguishable_in_the_row():
    """The labels are two facts, not one, and the row has to keep them apart.

    :data:`SHADOW_STATUS_NOT_STARTED_DEADLINE`'s comment draws the line —
    "nothing was spent here" — and that is the operational difference: a step
    that declined cost nothing, a step that overran spent the whole worst case
    and has to be read as evidence the budget is too small. Collapsing the two
    constants into one string would leave both of 10.3's deadline tests passing,
    so the distinction needs its own assertion.

    ``shadow_elapsed_seconds`` is the second half: it measures the shadow
    half's own elapsed time, so it is 0 on the decline and the overrun on the
    abandonment. A row claiming a short run for a step that overran would hide
    the very thing that makes the record useful.
    """
    assert _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE != _mod.SHADOW_STATUS_ABANDONED_DEADLINE

    # Declined: 880 s of a 900 s budget already spent, so nothing is spent here.
    _, declined = _record(clock=_stepped_clock(880.0), step_started_at=0.0, deadline_seconds=900)
    (declined_item,) = declined["writer"].items
    assert declined_item["shadow_status"] == _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE
    assert declined_item["shadow_elapsed_seconds"] == 0

    # Abandoned: started at 10 s, returned at 1000 s — 990 s spent, over budget.
    _, abandoned = _record(clock=_stepped_clock(10.0, 1000.0), step_started_at=0.0, deadline_seconds=900)
    (abandoned_item,) = abandoned["writer"].items
    assert abandoned_item["shadow_status"] == _mod.SHADOW_STATUS_ABANDONED_DEADLINE
    assert abandoned_item["shadow_elapsed_seconds"] == 990
