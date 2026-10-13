# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — a run's cost is published exactly once.

Feature: p39-metrics-truth, task 21.4. Subject: ``_publish_run_cost``
(``lambda_function.py:629-711``) and its call site at ``:951``.

# Feature: p39-metrics-truth, Property 5: A run's cost is published exactly once

*For any* terminal Glue state in ``{SUCCEEDED, FAILED, STOPPED, TIMEOUT}`` and
*any number* of EventBridge deliveries of that completion event, exactly one
run-cost datapoint is published across all deliveries — carrying that run's own
dimensions and its own DPU-derived estimate.

**Validates: Requirements 2.1, 2.2**

Where the expectations come from
--------------------------------

"Exactly one" is ``R2.2``'s wording, not a count taken from what a run happens
to produce. ``LGR-28b4d75-5`` (**template / ratified**): a test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle. So the cost value asserted below is re-derived here from the basis
``R2.7`` requires the dashboard to state — DPU-minutes at $0.44 per DPU-hour with
a 10-minute per-run minimum — rather than read back from ``collect_job_metrics``.

Why the state axis is all four terminal states
----------------------------------------------

``R2.2`` says "each Glue run that reaches a terminal state", and this writer
**deliberately diverges** from the three writers above it on
``LGR-eee8ac8-4``'s post-callback anchor, which all no-op on non-``SUCCEEDED``.
They record *data* outcomes — a version that was written, a file that was
ingested, a metadata address that now exists — and a failed run establishes none
of those. This records *spend*, and AWS bills the DPU-minutes of a run that
FAILED, was STOPPED or hit TIMEOUT exactly as it bills a successful one. Gating
on ``SUCCEEDED`` would under-report the cost tile by the whole of every failed
run, so the property has to hold for each of the four separately rather than for
``SUCCEEDED`` with the rest folded into a "no datapoint" branch.

Why nothing here looks for a dedupe guard
-----------------------------------------

Task 5.2 introduced **no** new idempotency, and the property is written to
exercise the existing one rather than to assume a guard sits at the publish site.
The mechanism is the task token: the first delivery redeems it, so on a duplicate
the record is still found but Step Functions rejects the send with a
consumed-token code and the handler returns from ``_consumed_token_response``
(``:862`` / ``:878``) — structurally before ``:951``. That is why
:class:`_StepFunctionsStub` is **stateful** and shared across a run's deliveries:
a stub that accepted every send would make every delivery publish, and the
property would be measuring the stub rather than the handler.
:class:`TestTheTripwireIsArmed` runs exactly that leaky stub and requires the
oracle to catch it, so "exactly one" is known to be an assertion and not an
accident of the fixture.

It also means the once-per-run guarantee is **positional**: it holds only while
the publish sits after the callback. A refactor that hoisted it above
``send_task_success`` would keep every existing unit test green and start
double-counting spend on every redelivery. Clause 2 below is what fails in that
world.

What is deliberately **not** asserted
-------------------------------------

1. **The best-effort clause.** A publisher that raises must log at WARNING and
   leave the run's status untouched — already covered, per terminal state, by
   ``test_run_cost_publication.py::test_a_raising_publisher_leaves_the_status_unchanged``
   (task 5.3). Re-generating it here would add iterations, not coverage.
2. **The absent-cost branch.** An unreadable Glue job run yields no
   ``actual_cost`` and correctly publishes *nothing* — ``R2.4``'s
   Not_Established, covered by task 5.3's
   ``test_an_unreadable_job_run_publishes_nothing_and_warns``. It is excluded
   from the generator rather than merely unasserted: it is outside this
   property's antecedent, which is a run that has a price.
3. **The publisher's own shape** — metric name, unit, dimension names, and that
   it flushes. That is ``tests/unit/shared/test_metrics_facade_run_cost.py``, and
   the dimension set is separately pinned by
   ``test_metrics_facade_service_dimension.py`` (task 13.2), which is why
   nothing below asserts a ``service`` dimension: ``_COST_METRIC_SERIES`` is a
   brace SEARCH on exactly four dimension names and a fifth would empty the
   monthly-cost tile's read.
4. **Self-healing dispatch.** ``SELF_HEALING_FN`` is pinned empty so a
   non-``SUCCEEDED`` delivery adds no async invoke. That is P28 R9.3's subject.

Fixtures and threading
----------------------

``MagicMock`` / hand-written stubs, following the five sibling files in this
directory: CloudWatch and Step Functions are outside the shared DynamoDB / S3 /
SQS fixtures' scope, and there is no ``moto.mock_aws()`` call here
(``tests/unit/test_no_duplicate_mocking_prop.py``). No AWS call is made on any
path — the facade is replaced wholesale, so the unit layer's CloudWatch write
guard (``tests/unit/conftest.py``) is never even reached.

``LGR-b46a5bb-1`` (**standing invariant**) is why the publish is synchronous on
the handler's own path and why this file can count datapoints at all: deferring
it to a worker thread would have the execution environment freeze on return and
kill the worker, so the publish would never happen. Nothing here adds a thread;
``tests/unit/test_no_lambda_threads_prop.py`` pins that for ``lambda/**``.

**Validates: Requirements 2.1, 2.2**
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import event as _hypothesis_event
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import-time env and path, matching the five siblings in this directory.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_JOB_COMPLETION_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
for _path in (_SHARED_DIR, _JOB_COMPLETION_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Rebind ``sys.modules['lambda_function']`` — every sibling does this.

    Several Lambdas share the module name ``lambda_function``, so whichever test
    module imported last owns the entry.
    """
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The run under test
# ---------------------------------------------------------------------------
RUN_ID = "run-p39-once-1"
PIPELINE_ID = "pipe-p39-once"
JOB_NAME = "agentic-data-ingestion-pipe-p39-once"
JOB_RUN_ID = "jr-p39-once"
TASK_TOKEN = "token-p39-once"
ENVIRONMENT = "dev"

#: The four states an EventBridge Glue job state-change event carries at the end
#: of a run — the handler reads them at ``:770`` and ``R2.2``'s "terminal state"
#: names exactly these.
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT")

#: The three codes Step Functions answers with once a token has been redeemed,
#: as classified at ``lambda_function.py:60``. Which one comes back depends on
#: how long ago the first delivery landed, so it is generated rather than fixed.
CONSUMED_TOKEN_CODES = ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken")


# ---------------------------------------------------------------------------
# The cost oracle — R2.7's stated basis, re-derived rather than read back
# ---------------------------------------------------------------------------
#: Glue's DPU per worker for each worker type. Spelled out here rather than
#: imported from ``collect_job_metrics`` so the oracle is the pricing basis and
#: not the code under test (``LGR-28b4d75-5``).
_DPU_PER_WORKER = {"G.1X": 1.0, "G.2X": 2.0, "G.4X": 4.0, "G.8X": 8.0, "Z.2X": 2.0}

#: Glue 5.0 list price, and the per-run minimum ``R2.7`` requires the dashboard's
#: ``basis`` annotation to name. The minimum is why every short run prices at a
#: constant 0.147 and why **nothing in this system measures spend**.
_DPU_HOUR_RATE = 0.44
_DPU_MINIMUM_SECONDS = 600


class RunProfile(NamedTuple):
    """One Glue run's billable shape, and the label the survey reports it under."""

    label: str
    worker_type: str
    workers: int
    max_capacity: float
    execution_seconds: int

    @property
    def total_dpu(self) -> float:
        """Workers × DPU-per-worker, falling back to ``MaxCapacity``.

        An unrecognised worker type counts as 1.0 DPU per worker: Glue keeps
        adding types, and a name this code has not seen must still price rather
        than divide by nothing.
        """
        if self.workers > 0:
            return self.workers * _DPU_PER_WORKER.get(self.worker_type, 1.0)
        return self.max_capacity

    @property
    def expected_cost(self) -> float:
        """The estimate ``R2.1`` publishes, from ``R2.7``'s stated basis.

        DPU-minutes at $0.44 per DPU-hour, with a 10-minute per-run minimum. The
        minimum is expressed as a floor on the billed seconds, which is the same
        comparison ``collect_job_metrics`` makes on DPU-hours for any positive
        DPU count — and the generator holds ``total_dpu`` positive, because a
        zero-DPU run would price at 0.0 and a published 0.0 is the fabricated
        "this run was free" datapoint this anchor exists to prevent.
        """
        billed_seconds = max(self.execution_seconds, _DPU_MINIMUM_SECONDS)
        return round(self.total_dpu * billed_seconds / 3600.0 * _DPU_HOUR_RATE, 3)

    @property
    def minimum_applies(self) -> bool:
        """Whether the 10-minute floor, not the elapsed time, set the price."""
        return self.execution_seconds < _DPU_MINIMUM_SECONDS


#: Six billable shapes, chosen so the DPU minimum is exercised on both sides and
#: exactly at the boundary. Every one prices to a different number, so a publisher
#: that emitted a constant — or emitted the previous delivery's value — is visible.
_SHORT_G1X = RunProfile("short_g1x_under_the_minimum", "G.1X", 2, 2.0, 60)
_TINY_Z2X = RunProfile("tiny_z2x_far_under_the_minimum", "Z.2X", 3, 6.0, 30)
_BOUNDARY_G4X = RunProfile("g4x_exactly_at_the_minimum", "G.4X", 4, 16.0, 600)
_LONG_G2X = RunProfile("long_g2x_well_above_the_minimum", "G.2X", 10, 20.0, 3600)
_MAX_CAPACITY = RunProfile("legacy_max_capacity_no_workers", "G.1X", 0, 2.0, 1200)
_UNKNOWN_TYPE = RunProfile("unrecognised_worker_type", "G.12X", 3, 3.0, 900)

_RUN_PROFILES = (_SHORT_G1X, _TINY_Z2X, _BOUNDARY_G4X, _LONG_G2X, _MAX_CAPACITY, _UNKNOWN_TYPE)


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------
class Scenario(NamedTuple):
    """One run: its terminal state, how many times the event arrives, its shape."""

    state: str
    deliveries: int
    profile: RunProfile
    consumed_code: str

    @property
    def is_redelivered(self) -> bool:
        """Whether this scenario is the one the property is really about."""
        return self.deliveries > 1


#: Axis 2 — how many times EventBridge delivers the completion event.
#:
#: **Hoisted into two branches on purpose.** A property about redelivery whose
#: generator mostly draws a single delivery is close to vacuous: with one flat
#: ``integers(1, 6)`` pool Hypothesis biases toward the low end and toward the
#: shrink target of 1, so the multi-delivery case — the whole subject — would be
#: the minority. ``one_of`` splits roughly evenly across its branches, which puts
#: redelivered runs at ~50% without removing the single-delivery case that the
#: property must also hold for. The survey below measures the split and asserts a
#: floor on it, so the claim is checked rather than hoped for.
#:
#: Six is the ceiling because EventBridge's own retry budget is small and the
#: property is about "more than once", not about arbitrarily large N — every
#: delivery past the second exercises the identical branch.
_DELIVERY_COUNTS = st.one_of(st.just(1), st.integers(min_value=2, max_value=6))


@st.composite
def completion_scenarios(draw) -> Scenario:
    """One (terminal state × delivery count × run shape × consumed-token code)."""
    return Scenario(
        state=draw(st.sampled_from(TERMINAL_STATES)),
        deliveries=draw(_DELIVERY_COUNTS),
        profile=draw(st.sampled_from(_RUN_PROFILES)),
        consumed_code=draw(st.sampled_from(CONSUMED_TOKEN_CODES)),
    )


#: The cases this property exists for, as **explicit** inputs rather than as
#: draws that might happen. Earlier tasks in this plan shipped generators that
#: reached their target case 0 times out of 200 and passed anyway, so the pool
#: stays for variety and these eight are guaranteed: all four terminal states
#: singly and redelivered, all three consumed-token codes, and both sides of the
#: DPU minimum. ``Phase.explicit`` runs them first, in both the survey and the
#: property.
_CRITICAL_SCENARIOS = (
    # ── The single delivery: the property must hold here too ──
    Scenario("SUCCEEDED", 1, _SHORT_G1X, "TaskTimedOut"),
    Scenario("TIMEOUT", 1, _MAX_CAPACITY, "TaskDoesNotExist"),
    # ── The canonical duplicate ──
    Scenario("SUCCEEDED", 2, _SHORT_G1X, "TaskTimedOut"),
    # ── Redelivery on each of the three non-SUCCEEDED terminal states ──
    Scenario("FAILED", 2, _LONG_G2X, "TaskDoesNotExist"),
    Scenario("STOPPED", 3, _TINY_Z2X, "InvalidToken"),
    Scenario("TIMEOUT", 6, _BOUNDARY_G4X, "TaskTimedOut"),
    # ── Redelivery above the minimum, and an unpriced worker type ──
    Scenario("SUCCEEDED", 4, _BOUNDARY_G4X, "InvalidToken"),
    Scenario("FAILED", 5, _UNKNOWN_TYPE, "TaskTimedOut"),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_SCENARIOS`` as explicit examples, in order."""
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


# ---------------------------------------------------------------------------
# The stubs
# ---------------------------------------------------------------------------
class Datapoint(NamedTuple):
    """One recorded ``publish_glue_run_cost`` call, and which delivery made it."""

    delivery: int
    kwargs: dict[str, Any]


class _FacadeStub:
    """The stubbed metrics facade: records datapoints, calls no AWS.

    Stands in for ``publish_glue_run_cost``, which the handler imports into its
    own namespace at ``lambda_function.py:46`` — so patching the module attribute
    intercepts the call the handler actually makes. One instance spans every
    delivery of a run, which is what makes ``len(datapoints)`` the run's total
    rather than one delivery's.
    """

    def __init__(self) -> None:
        self.datapoints: list[Datapoint] = []
        self.delivery = 0

    def __call__(self, **kwargs: Any) -> None:
        self.datapoints.append(Datapoint(self.delivery, dict(kwargs)))


class _StepFunctionsStub:
    """The stubbed Step Functions client: a token is redeemable exactly once.

    This is the existing idempotency the property exercises, modelled rather than
    asserted about. The first ``send_task_success`` / ``send_task_failure`` for a
    token succeeds; every later send for that token raises the consumed-token
    ``ClientError`` real Step Functions returns, which is what routes the handler
    into ``_consumed_token_response`` and back out before the publish site.
    """

    def __init__(self, consumed_code: str) -> None:
        self._consumed_code = consumed_code
        self.redeemed: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def send_task_success(self, taskToken: str, output: str) -> dict[str, Any]:
        return self._redeem("SendTaskSuccess", taskToken)

    def send_task_failure(self, taskToken: str, error: str, cause: str) -> dict[str, Any]:
        return self._redeem("SendTaskFailure", taskToken)

    def _redeem(self, operation: str, token: str) -> dict[str, Any]:
        self.calls.append((operation, token))
        if token in self.redeemed:
            raise ClientError(
                {"Error": {"Code": self._consumed_code, "Message": f"token {token} already consumed"}},
                operation,
            )
        self.redeemed.append(token)
        return {}


class _LeakyStepFunctionsStub(_StepFunctionsStub):
    """A token that can be redeemed for ever — the world clause 2 rules out.

    Used only by :class:`TestTheTripwireIsArmed`. It is the observable behaviour
    of a handler that published *above* the callback: every delivery gets through,
    so every delivery publishes. If the property's oracle cannot see that, it is
    not measuring anything.
    """

    def _redeem(self, operation: str, token: str) -> dict[str, Any]:
        self.calls.append((operation, token))
        self.redeemed.append(token)
        return {}


def _event(state: str) -> dict[str, Any]:
    """The EventBridge Glue job state-change event, as the rule delivers it."""
    return {"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": state}}


def _runs_table() -> MagicMock:
    """The state table: the GSI lookup, plus the rows the sibling writers read.

    One instance spans a run's deliveries, because it is one record: a duplicate
    delivery finds it exactly as the first did — the token is what differs, not
    the record. The row carries no task token: run_pipeline stores that in the
    task-tokens table (:func:`_task_tokens_table`). The ``file_validation`` row carries ``triggered_by="s3_upload"``
    and no ``content_fingerprint``, and no ``schema_compare`` row exists, so
    ``_link_heal_rerun_outcome``, ``_commit_target_schema_version`` and
    ``_record_processed_file`` all return early — none of them is this property's
    subject, and an exception escaping one of them would be answered by the
    handler's outer ``except`` with a 500 and no publish at all.
    """
    rows = {"file_validation": {"run_id": RUN_ID, "step_name": "file_validation", "triggered_by": "s3_upload"}}
    runs = MagicMock(name="pipeline_runs_table")
    runs.query.return_value = {
        "Items": [
            {
                "run_id": RUN_ID,
                "step_name": "run_pipeline",
                "original_pipeline_id": PIPELINE_ID,
                "glue_job_run_id": JOB_RUN_ID,
                "status": "RUNNING",
            }
        ]
    }
    runs.get_item.side_effect = lambda Key: {"Item": rows[Key["step_name"]]} if Key.get("step_name") in rows else {}
    return runs


def _task_tokens_table() -> MagicMock:
    """The task-tokens table, answering every delivery with the run's token row.

    Static on purpose, like :func:`_runs_table`: this models a duplicate that
    reads the token row before the first delivery's post-callback delete lands
    (concurrent deliveries), or after that delete failed — the case where the
    token itself is the dedupe, which is what clause 2 asserts. The other
    duplicate, arriving after the row is gone, returns before the callback
    altogether; ``test_job_completion_token_table.py`` drives that one through
    several deliveries and asserts it publishes once.
    """
    tokens = MagicMock(name="task_tokens_table")
    tokens.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": TASK_TOKEN}}
    return tokens


def _pipelines_table() -> MagicMock:
    """The pipelines table with no record for this pipeline.

    ``calculate_performance_comparison`` returns ``{}`` (no estimate to compare
    against) and ``_populate_mirror_metadata_location`` returns early (no target
    config, so no S3 Tables mirror). Both are deliberate: this property is about
    the cost datapoint, and the comparison's own ``actual_cost`` passthrough must
    not be the thing that supplies the published value.
    """
    pipelines = MagicMock(name="pipelines_table")
    pipelines.get_item.return_value = {}
    return pipelines


def _glue_client(scenario: Scenario) -> MagicMock:
    """The Glue client, answering ``get_job_run`` with this run's billable shape."""
    glue = MagicMock(name="glue")
    glue.get_job_run.return_value = {
        "JobRun": {
            "JobRunState": scenario.state,
            "ExecutionTime": scenario.profile.execution_seconds,
            "NumberOfWorkers": scenario.profile.workers,
            "WorkerType": scenario.profile.worker_type,
            "MaxCapacity": scenario.profile.max_capacity,
            "GlueVersion": "5.0",
            "ErrorMessage": "" if scenario.state == "SUCCEEDED" else f"job {scenario.state}",
        }
    }
    return glue


class DeliveryLog(NamedTuple):
    """Everything a run's deliveries produced, for assertion afterwards."""

    results: list[dict[str, Any]]
    facade: _FacadeStub
    sfn: _StepFunctionsStub


def _deliver(scenario: Scenario, sfn: _StepFunctionsStub | None = None) -> DeliveryLog:
    """Deliver one run's completion event ``scenario.deliveries`` times.

    Driven through ``lambda_handler`` rather than through ``_publish_run_cost``,
    because ``R2.2``'s once-per-run lives in the *path* — the consumed-token
    return above the publish site — and calling the writer directly would assert
    nothing about it. The facade, the Step Functions stub and the state table
    persist across the deliveries; the clients are re-bound each time, which is
    what a fresh Lambda invocation does.
    """
    facade = _FacadeStub()
    sfn = sfn if sfn is not None else _StepFunctionsStub(scenario.consumed_code)
    runs = _runs_table()
    tokens = _task_tokens_table()
    pipelines = _pipelines_table()
    glue = _glue_client(scenario)
    results: list[dict[str, Any]] = []

    for delivery in range(1, scenario.deliveries + 1):
        facade.delivery = delivery
        with ExitStack() as stack:
            for patcher in (
                patch.object(_mod, "pipeline_runs_table", runs),
                patch.object(_mod, "task_tokens_table", tokens),
                patch.object(_mod, "pipelines_table", pipelines),
                patch.object(_mod, "glue_client", glue),
                patch.object(_mod, "sfn_client", sfn),
                patch.object(_mod, "lambda_client", MagicMock(name="lambda")),
                patch.object(_mod, "publish_glue_run_cost", facade),
                # ENVIRONMENT pinned so the published dimension is deterministic;
                # SELF_HEALING_FN emptied so a non-SUCCEEDED delivery adds no
                # async dispatch (P28 R9.3's subject, not this one's).
                patch.dict(os.environ, {"ENVIRONMENT": ENVIRONMENT, "SELF_HEALING_FN": ""}, clear=False),
            ):
                stack.enter_context(patcher)
            results.append(_mod.lambda_handler(_event(scenario.state), None))

    return DeliveryLog(results, facade, sfn)


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
#: Every case the survey observed. A module-level accumulator, because
#: Hypothesis's statistics are per-example and cannot carry a cross-example
#: claim — and because a coverage claim that only exists in
#: ``--hypothesis-show-statistics`` output is a claim nobody checks.
_OBSERVED: dict[str, int] = {}


def _report(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached."""

    def seen(label: str) -> None:
        _OBSERVED[label] = _OBSERVED.get(label, 0) + 1
        _hypothesis_event(label)

    seen("examples")
    seen(f"state={scenario.state}")
    seen(f"profile={scenario.profile.label}")
    seen(f"consumed_code={scenario.consumed_code}")

    if scenario.is_redelivered:
        seen("redelivered: the event arrives more than once")
        seen(f"deliveries={scenario.deliveries}")
    else:
        seen("a single delivery")

    if scenario.deliveries > 2:
        seen("delivered three or more times")
    if scenario.profile.minimum_applies:
        seen("the 10-minute DPU minimum set the price (R2.7)")
    else:
        seen("the elapsed time set the price, not the minimum")
    if scenario.profile.workers == 0:
        seen("priced from MaxCapacity rather than from a worker count")
    if scenario.profile.worker_type not in _DPU_PER_WORKER:
        seen("an unrecognised worker type")


# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no handler, no stubs,
# nothing but the generator and the reporter.
@_with_critical_examples
@given(scenario=completion_scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(scenario):
    """Every scenario, explicit or drawn, is a well-formed run.

    **Validates: Requirements 2.1, 2.2**
    """
    _report(scenario)

    assert scenario.state in TERMINAL_STATES, f"{scenario.state!r} is not a terminal Glue state"
    assert scenario.deliveries >= 1, "a run's event is delivered at least once"
    assert scenario.consumed_code in CONSUMED_TOKEN_CODES
    # The antecedent: a run that HAS a price. A zero-DPU run would price at 0.0,
    # and a published 0.0 asserts the run was free — the fabricated number this
    # anchor exists to prevent (R2.4's absence, covered by task 5.3).
    assert scenario.profile.total_dpu > 0, "the property is about runs that cost something"
    assert scenario.profile.expected_cost > 0


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: redelivery is the majority case, not a rarity.

    A property about "any number of deliveries" whose generator mostly drew one
    delivery would pass while testing almost nothing — so the split is asserted
    here rather than left to a reader of ``--hypothesis-show-statistics``. The
    floor is set well below the ~50% that hoisting into two ``one_of`` branches
    produces, so a drift in Hypothesis's branch weighting is reported rather than
    silently narrowing the property.

    **Validates: Requirement 2.2**
    """
    assert _OBSERVED, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        # Both branches of axis 2.
        "a single delivery",
        "redelivered: the event arrives more than once",
        "delivered three or more times",
        # Axis 1 — every terminal state R2.2 names, each on its own.
        *(f"state={state}" for state in TERMINAL_STATES),
        # Axis 3 — the billable shape, on both sides of the DPU minimum.
        "the 10-minute DPU minimum set the price (R2.7)",
        "the elapsed time set the price, not the minimum",
        "priced from MaxCapacity rather than from a worker count",
        "an unrecognised worker type",
        # Axis 4 — every code Step Functions answers a redeemed token with.
        *(f"consumed_code={code}" for code in CONSUMED_TOKEN_CODES),
    )
    missing = [case for case in required if not _OBSERVED.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED.items()))}"
    )

    total = _OBSERVED["examples"]
    redelivered = _OBSERVED["redelivered: the event arrives more than once"]
    assert redelivered / total >= 0.25, (
        f"only {redelivered}/{total} generated scenarios delivered the event more than once — "
        f"a property about redelivery that mostly draws a single delivery is close to vacuous"
    )


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=completion_scenarios())
@settings(
    max_examples=100,
    # Each example drives the whole handler once per delivery. The property
    # asserts what was published, not how fast.
    deadline=None,
    # ``tests/unit/conftest.py``'s CloudWatch write guard is function-scoped and
    # autouse. It is never reached here — the facade is replaced wholesale — but
    # Hypothesis still sees the fixture.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # ``Phase.explain`` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion messages already state
    # verbatim, on every run of the full suite. Shrinking is kept — a minimal
    # counter-example is the thing worth having if this ever goes red.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_runs_cost_is_published_exactly_once(scenario):
    """Property 5: exactly one cost datapoint per run, however often the event arrives.

    **Validates: Requirements 2.1, 2.2**
    """
    _report(scenario)
    log = _deliver(scenario)

    # ── Clause 1: every delivery is processed cleanly ──────────────────
    # Asserted first, because it is what makes "any number of deliveries"
    # bounded in practice: the handler's outer ``except`` turns an escaped
    # exception into a 500, and a 500 on an EventBridge target is a function
    # error that earns *another* redelivery. A publish that could fail the
    # invocation would therefore keep buying itself more chances to publish.
    for index, result in enumerate(log.results, start=1):
        assert result["statusCode"] == 200, (
            f"delivery {index} of {scenario.deliveries} did not terminate cleanly "
            f"(state={scenario.state}, profile={scenario.profile.label}): {result}"
        )

    # ── Clause 2: the mechanism — later deliveries stop at the token ───
    # R2.2 needs no new guard because the token is the dedupe: the first
    # delivery redeems it, every later one is rejected and returns from
    # ``_consumed_token_response`` above the publish site. Asserted so that a
    # refactor hoisting the publish above the callback fails HERE, naming the
    # reason, instead of quietly double-counting spend on every redelivery.
    assert log.sfn.redeemed == [TASK_TOKEN], (
        f"the task token must be redeemed exactly once across {scenario.deliveries} deliveries; "
        f"redemptions={log.sfn.redeemed}"
    )
    assert len(log.sfn.calls) == scenario.deliveries, (
        f"every delivery must attempt the callback — that attempt is what detects the duplicate; "
        f"{len(log.sfn.calls)} attempts over {scenario.deliveries} deliveries: {log.sfn.calls}"
    )
    for index, result in enumerate(log.results[1:], start=2):
        assert "already consumed" in result["message"], (
            f"delivery {index} must return the consumed-token response, which is what places it "
            f"BEFORE the publish site; got {result['message']!r}"
        )

    # ── Clause 3: exactly one datapoint, from the first delivery ───────
    assert len(log.facade.datapoints) == 1, (
        f"R2.2: exactly one run-cost datapoint per run. {len(log.facade.datapoints)} were published "
        f"across {scenario.deliveries} deliveries of the same {scenario.state} run "
        f"(profile={scenario.profile.label}); each extra one inflates the cost tile by a whole run. "
        f"Published: {log.facade.datapoints}"
    )
    datapoint = log.facade.datapoints[0]
    assert datapoint.delivery == 1, (
        f"the datapoint must come from the delivery that redeemed the token, i.e. the first; "
        f"this one came from delivery {datapoint.delivery}"
    )

    # ── Clause 4: it is THIS run's estimate ───────────────────────────
    # "Exactly one" is satisfied by one datapoint carrying anything at all, so
    # without this the property would pass against a publisher emitting a
    # constant. The cost is re-derived from R2.7's stated basis — DPU-minutes at
    # $0.44/DPU-hour with a 10-minute minimum — not read back from the handler.
    kwargs = datapoint.kwargs
    assert kwargs["pipeline_id"] == PIPELINE_ID, (
        f"PipelineId is what makes the datapoint attributable to a pipeline; got {kwargs['pipeline_id']!r}"
    )
    assert kwargs["job_name"] == JOB_NAME
    assert kwargs["worker_type"] == scenario.profile.worker_type, (
        f"the datapoint must carry this run's worker type; expected "
        f"{scenario.profile.worker_type!r}, got {kwargs['worker_type']!r}"
    )
    assert kwargs["environment"] == ENVIRONMENT, (
        "the Environment dimension is the deployment's own — it is what keeps a dev run's "
        f"spend out of a prod window; got {kwargs['environment']!r}"
    )
    assert kwargs["estimated_cost"] == pytest.approx(scenario.profile.expected_cost, abs=5e-4), (
        f"the published estimate must be this run's: {scenario.profile.total_dpu} DPU × "
        f"{max(scenario.profile.execution_seconds, _DPU_MINIMUM_SECONDS)}s (10-minute minimum "
        f"{'applied' if scenario.profile.minimum_applies else 'not reached'}) at "
        f"${_DPU_HOUR_RATE}/DPU-hour = {scenario.profile.expected_cost}; got {kwargs['estimated_cost']}"
    )
    # No ``service`` dimension is asserted, deliberately: task 13.2's
    # ``_COST_METRIC_SERIES`` is a brace SEARCH over exactly four dimension
    # names, and a fifth empties the monthly-cost tile's read.
    assert set(kwargs) == {"pipeline_id", "job_name", "estimated_cost", "worker_type", "environment"}, (
        f"the publisher's four dimensions and its value, and nothing else; got {sorted(kwargs)}"
    )


# ---------------------------------------------------------------------------
# The tripwire, proved armed
# ---------------------------------------------------------------------------
class TestTheTripwireIsArmed:
    """Clause 3 catches what it exists to catch.

    "Exactly one" passes on a single delivery no matter what the handler does, so
    on its own the oracle is indistinguishable from one that counts the wrong
    thing. These two make the failure happen and require it to be seen.
    """

    @staticmethod
    def _scenario() -> Scenario:
        return Scenario("SUCCEEDED", 4, _SHORT_G1X, "TaskTimedOut")

    def test_a_reusable_token_makes_every_delivery_publish(self):
        """With the dedupe removed, four deliveries publish four datapoints.

        This is the observable shape of a handler that published above the
        callback — the refactor clause 2 rules out. The count is asserted here so
        that the property's ``== 1`` is known to be measuring the handler's path
        rather than a fixture that could only ever produce one call.
        """
        scenario = self._scenario()
        log = _deliver(scenario, sfn=_LeakyStepFunctionsStub(scenario.consumed_code))

        assert [d.delivery for d in log.facade.datapoints] == [1, 2, 3, 4], (
            "a token that never reports as consumed must let every delivery through to the "
            f"publish site; got {log.facade.datapoints}"
        )
        assert len(log.facade.datapoints) != 1, "the tripwire scenario must violate clause 3"

    def test_the_stateful_stub_rejects_the_second_redemption(self):
        """The dedupe the property relies on is the stub's, and it works.

        Without this, a stub that silently accepted every send would make clause
        2's ``redeemed == [TASK_TOKEN]`` unprovable and clause 3 accidental.
        """
        sfn = _StepFunctionsStub("TaskTimedOut")
        sfn.send_task_success(taskToken=TASK_TOKEN, output="{}")

        with pytest.raises(ClientError) as raised:
            sfn.send_task_success(taskToken=TASK_TOKEN, output="{}")

        assert raised.value.response["Error"]["Code"] == "TaskTimedOut"
        assert raised.value.response["Error"]["Code"] in _mod._TOKEN_CONSUMED_ERROR_CODES, (
            "the stub must raise a code the handler classifies as a consumed token, or the "
            "property would be exercising the re-raise path instead of the duplicate path"
        )
        assert sfn.redeemed == [TASK_TOKEN]
