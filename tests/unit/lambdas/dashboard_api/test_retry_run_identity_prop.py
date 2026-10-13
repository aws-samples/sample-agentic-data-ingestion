# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests over the retry endpoint's run identity (p38 §4.1, §4.2).

This module is the home of **five** of p38's properties, and as of task 3.6 all
five have landed: Property 1 first (task 3.2), Property 2 with it (task 3.3),
then Property 6 (task 3.4), Property 4 (task 3.5) and Property 5 (task 3.6).

**So the generators and the mock world below are module-level on purpose.**
Each of the four later tasks added one ``@given`` function and nothing else:

- :data:`run_ids`, :data:`pipeline_ids` — the two id shapes.
- :func:`step_name_variants` — canonical, SFN ``PascalCase`` and legacy
  ``PascalCase`` state-row spellings of one step.
- :func:`failed_runs` — the whole generator: a failed run's state rows, its
  executions row and its pipeline record, as a :class:`FailedRun`.
- :func:`retry_run_environment` — the mock world, as a context manager
  yielding a :class:`RetryMocks` whose ``executions_table`` records every
  ``update_item`` / ``put_item`` (Property 6) and whose ``sfn`` records the
  ``start_execution`` input (Property 2).
- :func:`parse` — ``(statusCode, body)`` off a handler response.

If a later property needs a *different* executions row (Property 4 wants any
``status``, any ``steps_status`` and ``completed_at`` present or absent;
Property 5 wants 1..8 successive retries), pass it through
``failed_runs(...)``'s keyword arguments or the ``executions_rows`` argument of
:func:`retry_run_environment` rather than rebuilding this world.

Property 1: A reported run id resolves
--------------------------------------

# Feature: p38-retry-run-identity, Property 1: A reported run id resolves

*For any* failed run with at least one state row, the ``run_id`` in the retry
endpoint's ``200`` body is a run id that ``get_run_details`` resolves to a
``200`` against the same state table.

**Validates: Requirements 1.1**

**The round trip is the assertion, and that is the whole point.** R1.1 is
written about *resolvability* rather than about which id is reported, because
the shipped bug was an id that looked perfectly well-formed: `retry_run` minted
``retry-{run_id[:40]}-{ts_hash}`` and returned it while starting the execution
under the original id, so a test asserting a shape ("starts with ``run-``",
"contains the original id") would have passed against the broken code. No test
covered this invariant at all — ``test_pipelines_retry.py`` asserted only
``"run_id" in body`` — and that absence is why the defect reached production.
So the reported id is fed **back through** ``get_run_details`` against the
**same** mocked state table, and a 404 there fails the property.

**Which is why the mock state table is keyed by run id** (see
:func:`retry_run_environment`). A ``MagicMock`` whose ``query`` returned the
run's rows unconditionally would answer ``200`` for *any* reported id and the
property would pass against the bug it exists to catch.
``test_the_mock_state_table_is_keyed_by_run_id`` guards that non-vacuity
directly, so a future edit cannot quietly flatten the world.

Property 2: The reported id is the executing id
-----------------------------------------------

# Feature: p38-retry-run-identity, Property 2: The reported id is the executing id

*For any* failed run, the ``run_id`` in the retry endpoint's ``200`` body equals
the ``run_id`` inside the Step Functions input, equals the requested run id, and
matches no ``retry-`` prefix.

**Validates: Requirements 1.2, 1.6, 5.4**

Property 1 asks whether the reported id resolves; Property 2 asks whether it is
the id that actually ran. They are separate because R1.1 alone could be
satisfied by a tolerant reader bridging two ids, which is the repair R1.2 exists
to forbid — so the executing id is read out of the recorded
``start_execution`` input rather than out of the response. The R1.6 clause and
its sort-order reasoning, and R5.4's resolvable-lineage clause, are in the test's
own docstring.

Property 6: The retry never creates a row, and never leaves an incomplete one
----------------------------------------------------------------------------

# Feature: p38-retry-run-identity, Property 6: The retry never creates a row, and never leaves an incomplete one

*For any* failed run, the retry endpoint calls ``put_item`` zero times, issues
exactly one executions-table ``update_item`` carrying
``ConditionExpression="attribute_exists(run_id)"``, and afterwards either the
row still carries its ``file_name``, ``ai_model_used`` and ``schema_hash``
unchanged, or no row exists for that run id at all.

**Validates: Requirements 3.1, 3.6**

Property 6 is the only one of the five that asserts a **disjunction**, and both
disjuncts are exercised on every example — the row-present world through
``retry_run_environment(run)`` and the row-absent one through
``retry_run_environment(run, executions_rows=[])``. The "or no row exists"
branch is R3.6's "or absent", and it is what makes R3.1 constructive rather
than only prohibitive: the write is guarded, so when there is no row to
annotate the guard refuses and the outcome is *nothing*, never a half-filled
row.

**The three fields are judged by what the update expression does not name**,
not by folding the update onto the row — see
:func:`_expression_attribute_targets` for why.

Property 4: An attempt snapshot reproduces the attempt it displaces
-------------------------------------------------------------------

# Feature: p38-retry-run-identity, Property 4: An attempt snapshot reproduces the attempt it displaces

*For any* executions row — any ``status``, any ``steps_status`` map,
``completed_at`` present or absent — the attempt node the retry appends
reproduces that row's pre-retry ``status`` and ``steps_status`` verbatim, and
omits ``completed_at`` rather than defaulting it when the row had none.

**Validates: Requirements 2.1, 2.3**

The row comes through :func:`displaced_rows`, which reshapes the realistic
FAILED row :func:`failed_runs` already builds and hands it to
``retry_run_environment(run, executions_rows=[row])`` — the world above is
reused, not rebuilt, exactly as the module header asks. Its shape axis
(:data:`_ROW_SHAPES`) is what makes R2.3's ``status``/``steps_status``
disagreement and the absent-``completed_at`` case first-class draws rather than
values that happen to turn up.

**One decision the property had to take a side on, stated here and argued in
the test's own docstring: the snapshot's guard is truthiness, not presence.**
So an *empty* ``steps_status`` (``{}``) or an empty-string ``status`` is omitted
from the node rather than reproduced as empty. The property asserts the
truthiness reading — reproduce a value the row carries, omit the key otherwise,
never default it — and generates the falsy-but-present rows deliberately rather
than steering around them.

Property 5: Attempt numbering is strictly increasing and the list is capped
--------------------------------------------------------------------------

# Feature: p38-retry-run-identity, Property 5: Attempt numbering is strictly increasing and the list is capped

*For any* number of successive retries of one run, the recorded attempt numbers
are unique and strictly increasing, the list length is ``min(retries, 5)``, and
the retained nodes are the most recent ones.

**Validates: Requirements 2.4**

Property 5 is the only one of the five that spans **more than one call**, so it
needs the world above to carry state forward: :func:`_fold_update_onto_row`
applies each retry's own write back onto the mocked executions row, which is the
pattern ``test_pipelines_retry.py``'s ``_apply_update`` established for its
five- and eight-retry cases. Nothing else here is rebuilt for it —
:func:`failed_runs` supplies the run and
``retry_run_environment(run, executions_rows=[row])`` supplies the world.
:func:`_sole_executions_update` and :func:`_appended_attempt_node` are
deliberately *not* reused: both assert exactly one ``update_item``, which is
false by construction once there are eight retries.

The cap and the numbering are one property because the cap is what makes the
numbering non-obvious — numbering comes off ``attempt_count`` rather than
``len(attempts)`` precisely so that it keeps increasing past a list that cannot.

No moto and no boto3 DynamoDB client mock: the module-level table objects on
``handlers.pipelines_runs`` are replaced with ``MagicMock``\\ s for the duration
of each example, which is the pattern this directory's other readers' tests
already use (``test_run_details_not_found_prop.py``,
``test_run_details_load_outcome.py``, ``test_pipelines_retry.py``). The patch is
applied inside the test body rather than through a fixture because a Hypothesis
``@given`` test cannot take a function-scoped fixture.

``AWS_REGION`` is deliberately **not** set here — the unit layer pins it by
assignment in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``, **template /
ratified**), and a module that assigned its own would fight that fix.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from itertools import pairwise
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path / env setup — the shape test_run_details_not_found_prop.py uses for the
# sibling reader property. The directory conftest.py also bootstraps sys.path;
# this is belt-and-braces for a direct single-file run.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(LAMBDA_DIR), os.path.abspath(SHARED_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("LOCKS_TABLE", "test-locks-table")
# `require_env("STATE_MACHINE_ARN")` has NO default, so retry_run raises OSError
# without this one — it is the only env var this module needs that the other
# readers' tests do not.
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-west-2:123456789012:stateMachine:agentic-data-ingestion-pipeline",
)
# Keeps `get_aws_account_id` off STS if `_fallback_source_bucket` is ever
# reached. It is not on the path these properties take — every generated
# pipeline record carries a `source_bucket` — but a memoized STS call would be
# an ordering-dependent surprise rather than a failure.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

from handlers import pipelines_runs as runs

from step_names import SFN_STATE_NAMES, TASK_STEPS, StepName

# ---------------------------------------------------------------------------
# Id shapes
# ---------------------------------------------------------------------------

#: The 8-hex-character suffix real run ids and pipeline ids both carry.
_hex8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)


def _format_run_timestamp(parts: tuple[int, int, int, int, int, int]) -> str:
    """``2026-08-09-15-38-19`` — the timestamp segment of a real run id."""
    year, month, day, hour, minute, second = parts
    return f"{year:04d}-{month:02d}-{day:02d}-{hour:02d}-{minute:02d}-{second:02d}"


#: ``run-2026-08-09-15-38-19-5c5f5f8a`` — the shape ``file_validation`` mints
#: and the only shape a *real* run id has ever had. Random ids, per task 3.2.
run_ids = st.builds(
    lambda ts, suffix: f"run-{ts}-{suffix}",
    ts=st.tuples(
        st.integers(min_value=2024, max_value=2030),
        st.integers(min_value=1, max_value=12),
        st.integers(min_value=1, max_value=28),
        st.integers(min_value=0, max_value=23),
        st.integers(min_value=0, max_value=59),
        st.integers(min_value=0, max_value=59),
    ).map(_format_run_timestamp),
    suffix=_hex8,
)

#: ``newtest-ab2a08a0`` — the pipeline-id shape, which is also the lock key and
#: the S3 upload prefix.
pipeline_ids = st.builds(
    lambda name, suffix: f"{name}-{suffix}",
    name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=12),
    suffix=_hex8,
)

# ---------------------------------------------------------------------------
# Step-name variants — canonical, SFN PascalCase, and legacy PascalCase
# ---------------------------------------------------------------------------
# `step_names._LEGACY_ALIASES` catalogues six aliases, but only two of them are
# spellings a *state row* was ever written with: `create_code` wrote
# `step_name="CodeGeneration"` and `test_code` wrote `"CodeTesting"` until P28
# task 14.2 adopted the canonical names, and rows carrying them persist in live
# tables. The other four (`code_generation`, `infrastructure`, `first_run`,
# `schema_mapping_approval`) are approval types, and three of them are on
# `get_run_details`' synthetic-step denylist (`pipelines_runs.py:757-767`,
# `LGR-9e76249-9`), so generating them as step rows would model a row shape the
# retry path never reads back.
_LEGACY_STATE_ROW_SPELLINGS: dict[StepName, str] = {
    StepName.CREATE_CODE: "CodeGeneration",
    StepName.TEST_CODE: "CodeTesting",
}


def step_name_variants(step: StepName) -> tuple[str, ...]:
    """Every ``step_name`` spelling a state row for *step* may carry.

    Canonical snake_case first, then the SFN ``PascalCase`` state id, then the
    legacy ``PascalCase`` spelling where one exists. All three parse through
    ``step_names.from_any``, which is what ``retry_run`` normalises with before
    it keys ``cached_step_results`` — so a run whose rows are a *mix* of
    generations is the case this tuple exists to reach.
    """
    variants = [str(step), SFN_STATE_NAMES[step]]
    legacy = _LEGACY_STATE_ROW_SPELLINGS.get(step)
    if legacy is not None:
        variants.append(legacy)
    return tuple(variants)


#: The statuses a failed state row carries. `retry_run` treats both as failure
#: (`item_str(item, "status").lower() in ("error", "failed")`), and both occur:
#: `step_errors` writes `"failed"`, several task Lambdas write `"error"`.
_FAILED_STATUSES = st.sampled_from(["error", "failed"])


# ---------------------------------------------------------------------------
# The generated world
# ---------------------------------------------------------------------------
class FailedRun(NamedTuple):
    """One failed run, as the three tables hold it.

    ``state_rows`` is every ``(run_id, step_name)`` row: a prefix of
    ``TASK_STEPS`` in execution order, the last one failed and the earlier ones
    ``success``. That is a real failed run's shape — a run fails *at* a step and
    writes nothing after it — and it satisfies task 3.2's "1..N steps, one
    failed step".

    ``executions_row`` is the row ``retry_run``'s pre-flight
    ``executions_table.query`` must find. It matters that it is found: the
    ``attempts`` / ``attempt_count`` SET clauses are omitted entirely when that
    read comes back empty (``pipelines_runs.py``, the ``if exec_row:`` branch),
    so Properties 4, 5 and 6 have nothing to assert against unless the mock
    returns it.
    """

    run_id: str
    pipeline_id: str
    state_rows: list[dict[str, Any]]
    failed_step_name: str
    pipeline_record: dict[str, Any]
    executions_row: dict[str, Any]


def _state_row(
    *,
    run_id: str,
    pipeline_id: str,
    step_name: str,
    status: str,
    sequence: int,
    timestamp: str,
) -> dict[str, Any]:
    """One state-table row, carrying the attributes both readers read.

    ``output_data`` is present on successful rows because that is what
    ``retry_run`` turns into ``cached_step_results`` — a row without it is
    skipped, so a run of rows with no ``output_data`` would exercise the retry
    input's empty case only.
    """
    row: dict[str, Any] = {
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "original_pipeline_id": pipeline_id,
        "step_name": step_name,
        "status": status,
        "step_sequence": sequence,
        "timestamp": timestamp,
        "execution_id": f"arn:aws:states:us-west-2:123456789012:execution:adi:{run_id}",
    }
    if status == "success":
        row["output_data"] = {"step": step_name, "ok": True}
    else:
        row["output_data"] = {}
        row["error_message"] = f"{step_name} failed"
    return row


def _executions_row(
    *,
    run_id: str,
    pipeline_id: str,
    state_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The FAILED executions row for a run, as ``execution_updater`` leaves it.

    Carries the three fields R3.6 names — ``file_name``, ``ai_model_used``,
    ``schema_hash`` — because Property 6 asserts they survive the retry's
    snapshot update, and ``steps_status`` / ``completed_at`` because Property 4
    asserts the attempt node reproduces them.
    """
    return {
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "status": "FAILED",
        "trigger": "s3_upload",
        "file_name": f"{pipeline_id}.csv",
        "ai_model_used": "anthropic.claude-sonnet-4-20250514-v1:0",
        "schema_hash": "0f1e2d3c4b5a6978",
        "execution_name": f"adi-{run_id}",
        "started_at": state_rows[0]["timestamp"],
        "completed_at": state_rows[-1]["timestamp"],
        "current_step": state_rows[-1]["step_name"],
        "steps_status": {row["step_name"]: row["status"] for row in state_rows},
    }


@st.composite
def failed_runs(
    draw: st.DrawFn,
    *,
    min_steps: int = 1,
    max_steps: int = len(TASK_STEPS),
) -> FailedRun:
    """A failed run: 1..N state rows, mixed name generations, one failed step.

    Both spellings task 3.2 names are reachable on every step and all three on
    ``create_code`` / ``test_code``, drawn per row, so one run's rows can mix
    canonical and legacy generations — which is the state a live table is
    actually in after P28 task 14.2.
    """
    run_id = draw(run_ids)
    pipeline_id = draw(pipeline_ids)
    step_count = draw(st.integers(min_value=min_steps, max_value=max_steps))
    steps = TASK_STEPS[:step_count]

    state_rows: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        spelling = draw(st.sampled_from(step_name_variants(step)))
        is_failed = index == step_count - 1
        state_rows.append(
            _state_row(
                run_id=run_id,
                pipeline_id=pipeline_id,
                step_name=spelling,
                status=draw(_FAILED_STATUSES) if is_failed else "success",
                sequence=index + 1,
                timestamp=f"2026-08-09T15:{index:02d}:00+00:00",
            )
        )

    return FailedRun(
        run_id=run_id,
        pipeline_id=pipeline_id,
        state_rows=state_rows,
        failed_step_name=state_rows[-1]["step_name"],
        pipeline_record={
            "pipeline_id": pipeline_id,
            "source_type": "S3_UPLOAD",
            "source_bucket": "adi-source-files-123456789012-us-west-2",
        },
        executions_row=_executions_row(run_id=run_id, pipeline_id=pipeline_id, state_rows=state_rows),
    )


# ---------------------------------------------------------------------------
# The mock world
# ---------------------------------------------------------------------------
class RetryMocks(NamedTuple):
    """The mocks a property may assert against after the block exits.

    ``executions_table.update_item`` / ``.put_item`` carry the calls Property 6
    counts; ``sfn.start_execution`` carries the input Property 2 reads its
    ``run_id`` out of.
    """

    state_table: MagicMock
    executions_table: MagicMock
    pipelines_table: MagicMock
    approvals_table: MagicMock
    locks_table: MagicMock
    dynamodb: MagicMock
    sfn: MagicMock


def _key_condition_value(condition: Any) -> Any:
    """The value a ``Key(<attr>).eq(<value>)`` condition compares against.

    Both readers build their query the same way —
    ``KeyConditionExpression=Key("run_id").eq(run_id)`` in ``get_run_details``
    (``:442``) and in ``retry_run``'s paginated read, and
    ``Key("pipeline_id").eq(pipeline_id)`` on the executions table — so one
    accessor lets the mock tables answer *by key* instead of unconditionally.
    That is what makes the round trip in Property 1 a real test.
    """
    return condition.get_expression()["values"][1]


@contextlib.contextmanager
def retry_run_environment(
    run: FailedRun,
    *,
    executions_rows: Sequence[dict[str, Any]] | None = None,
    lock_held: bool = False,
) -> Iterator[RetryMocks]:
    """Patch ``handlers.pipelines_runs``' AWS surface for one example.

    The state and executions tables resolve **by key**, so a query for an id
    that was never written answers ``{"Items": []}`` exactly as DynamoDB would.
    Everything a property might assert against is on the yielded
    :class:`RetryMocks`.

    Args:
        run: the run the tables hold.
        executions_rows: override the executions-table contents. Defaults to
            ``[run.executions_row]``. Pass ``[]`` to model the row being absent
            — R3.6's "or absent" branch, where the retry's ``update_item``
            fails its ``attribute_exists(run_id)`` condition.
        lock_held: when true the locks table reports a held lock and
            ``retry_run`` refuses with ``409``.
    """
    rows_by_run_id = {run.run_id: list(run.state_rows)}
    exec_rows = [run.executions_row] if executions_rows is None else list(executions_rows)

    state_table = MagicMock(name="state_table")
    state_table.query.side_effect = lambda **kwargs: {
        "Items": list(rows_by_run_id.get(_key_condition_value(kwargs["KeyConditionExpression"]), []))
    }

    executions_table = MagicMock(name="executions_table")
    executions_table.query.side_effect = lambda **kwargs: {
        "Items": [
            row for row in exec_rows if row.get("pipeline_id") == _key_condition_value(kwargs["KeyConditionExpression"])
        ]
    }

    def _executions_get_item(**kwargs: Any) -> dict[str, Any]:
        # `ProjectionExpression` is deliberately ignored — the whole row comes
        # back. That is more permissive than DynamoDB, and it is the right
        # simplification here because no property in this file judges the
        # projection. A test that DOES judge it (task 5.1 widens
        # `get_run_details`' `ProjectionExpression="step_logs"` to carry the
        # lineage fields) must assert on the recorded call kwargs rather than
        # on what this mock returns.
        key = kwargs.get("Key", {})
        for row in exec_rows:
            if row.get("pipeline_id") == key.get("pipeline_id") and row.get("run_id") == key.get("run_id"):
                return {"Item": row}
        return {}

    executions_table.get_item.side_effect = _executions_get_item

    pipelines_table = MagicMock(name="pipelines_table")
    pipelines_table.get_item.side_effect = lambda **kwargs: (
        {"Item": run.pipeline_record} if kwargs.get("Key", {}).get("pipeline_id") == run.pipeline_id else {}
    )

    approvals_table = MagicMock(name="approvals_table")
    approvals_table.query.return_value = {"Items": []}

    locks_table = MagicMock(name="locks_table")
    locks_table.get_item.return_value = (
        {"Item": {"pipeline_prefix": run.pipeline_id, "execution_name": "held"}} if lock_held else {}
    )

    dynamodb = MagicMock(name="dynamodb")
    dynamodb.Table.return_value = locks_table

    sfn = MagicMock(name="stepfunctions")

    def _client(service_name: str, *args: Any, **kwargs: Any) -> MagicMock:
        # Only Step Functions is asserted against; anything else gets an inert
        # mock rather than a real client, so no example can reach AWS.
        return sfn if service_name == "stepfunctions" else MagicMock(name=f"{service_name}_client")

    with (
        patch.object(runs, "state_table", state_table),
        patch.object(runs, "executions_table", executions_table),
        patch.object(runs, "pipelines_table", pipelines_table),
        patch.object(runs, "approvals_table", approvals_table),
        patch.object(runs, "dynamodb", dynamodb),
        # `get_run_details` fans out to CloudWatch for any step whose logs are
        # not cached on the executions row. Patched to empty rather than mocked
        # per-call: log text is not what any of these five properties judge, and
        # an empty list also keeps `get_run_details`' own log-caching
        # `update_item` from firing, which Property 6 counts.
        patch.object(runs, "get_step_logs", lambda *_a, **_k: []),
        patch.object(runs.boto3, "client", side_effect=_client),
    ):
        yield RetryMocks(
            state_table=state_table,
            executions_table=executions_table,
            pipelines_table=pipelines_table,
            approvals_table=approvals_table,
            locks_table=locks_table,
            dynamodb=dynamodb,
            sfn=sfn,
        )


def parse(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``(statusCode, decoded body)`` off a handler response."""
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@given(run=failed_runs())
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_a_reported_run_id_resolves(run: FailedRun) -> None:
    """Property 1: A reported run id resolves.

    # Feature: p38-retry-run-identity, Property 1: A reported run id resolves

    For any failed run with at least one state row, the ``run_id`` in the retry
    endpoint's ``200`` body is a run id that ``get_run_details`` resolves to a
    ``200`` against the same state table.

    **Validates: Requirements 1.1**
    """
    with retry_run_environment(run):
        retry_status, retry_body = parse(runs.retry_run(run.run_id))

        assert retry_status == 200, (
            f"the retry of a failed run must succeed before its reported id can be judged; "
            f"got {retry_status} {retry_body!r} for {run.run_id!r} "
            f"({len(run.state_rows)} state rows, failed at {run.failed_step_name!r})"
        )

        reported_run_id = retry_body.get("run_id")
        assert isinstance(reported_run_id, str) and reported_run_id, (
            f"the retry response must report a run id; got {reported_run_id!r} in {retry_body!r}"
        )

        # The round trip. Same state table, same objects, no re-seeding: the
        # only question asked is whether the id the endpoint just handed its
        # caller resolves to a run.
        detail_status, detail_body = parse(runs.get_run_details(reported_run_id))

    assert detail_status == 200, (
        f"the retry reported run_id={reported_run_id!r}, which get_run_details does not resolve "
        f"({detail_status} {detail_body!r}). R1.1: a reported run id must be resolvable — the "
        f"requested run {run.run_id!r} has {len(run.state_rows)} state rows"
    )

    # A 200 that describes nothing would satisfy the status check while being
    # the tolerant reader do-NOT 6 forbids, so the resolved run has to carry the
    # run's actual content.
    assert detail_body.get("steps"), (
        f"get_run_details resolved {reported_run_id!r} to a 200 carrying no steps — a run that "
        f"resolves must resolve to the run's own state rows, not to an empty shell: {detail_body!r}"
    )
    assert detail_body.get("pipeline_id") == run.pipeline_id, (
        f"the resolved run belongs to pipeline {detail_body.get('pipeline_id')!r}, not to "
        f"{run.pipeline_id!r} — the reported id resolved to a different run"
    )


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

#: The prefix the shipped defect put on the Reported_Run_Id (the deleted
#: ``f"retry-{run_id[:40]}-{ts_hash}"``). Named once so the R1.6 clause and the
#: sort-order demonstration below cannot drift apart.
_FABRICATED_PREFIX = "retry-"


def _sfn_start_execution_input(mocks: RetryMocks) -> dict[str, Any]:
    """The decoded Step Functions input ``start_execution`` was called with.

    ``retry_run`` passes all three arguments by keyword
    (``stateMachineArn=``, ``name=``, ``input=json.dumps(sfn_input, ...)``), so
    the payload is read off ``call_args[1]["input"]`` and decoded.

    Exactly one execution per retry is **asserted** rather than assumed: with
    two calls "the" input would be ambiguous and the equality below would be
    reading whichever one happened to be last.
    """
    call_count = mocks.sfn.start_execution.call_count
    assert call_count == 1, f"a retry starts exactly one execution; start_execution was called {call_count} times"
    decoded = json.loads(mocks.sfn.start_execution.call_args[1]["input"])
    assert isinstance(decoded, dict), f"the Step Functions input must be a JSON object; got {decoded!r}"
    return decoded


@given(run=failed_runs())
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_the_reported_id_is_the_executing_id(run: FailedRun) -> None:
    """Property 2: The reported id is the executing id.

    # Feature: p38-retry-run-identity, Property 2: The reported id is the executing id

    For any failed run, the ``run_id`` in the retry endpoint's ``200`` body
    equals the ``run_id`` inside the Step Functions input, equals the requested
    run id, and matches no ``retry-`` prefix.

    **Validates: Requirements 1.2, 1.6, 5.4**

    **The executing id is read off the recorded ``start_execution`` input, not
    off the response.** That is the whole content of R1.2. The shipped defect
    was these two disagreeing eleven lines apart: the execution started under
    the Original_Run's id — deliberately, so the Resume_Cache's
    ``(run_id, step_name)`` entries resolve (``LGR-60c4985-4``, **template /
    ratified**) — while the body reported a fabricated id. R1.1 could have been
    satisfied by a tolerant reader bridging the two; R1.2 exists to forbid that,
    so nothing short of comparing the *reported* id against the id actually sent
    to Step Functions tests it. A shape assertion, or the body compared against
    itself, passes against the bug.

    **The no-``retry-``-prefix clause is R1.6 / do-NOT 1, and its reason is sort
    order.** The executions table's sort key is ``run_id``
    (``deploy-cdk/lib/data-stack.ts:156-157``) and ``get_pipeline_executions``
    queries it ``ScanIndexForward=False`` with a **server-side**
    ``Limit=page_size``, default 10 — verified at
    ``pipelines_runs.py:227-231`` (the spec cites ``:62-66``; the function has
    moved down the file since, the query is unchanged). ``"retry-…" < "run-…"``
    lexicographically, so every real ``run-`` row sorts ahead of every prefixed
    row: on a pipeline holding ``page_size`` or more real runs a prefixed id
    never reaches page one, and its position is never chronological.
    ``LGR-21a1b5b-4`` (**template / ratified**) is the governing record — it
    rejected a ``skipped-`` prefix on exactly this ground ("it sorts above
    ``run-`` under the run-history descending sort with a page limit of ten")
    and shaped the record's id like a real run id so the rows "interleave
    chronologically". The lexicographic fact is asserted below rather than only
    described, so the reasoning is executable rather than a comment that can go
    stale.

    **The clause is scoped to the run id and not to the execution name** (§4.8).
    ``execution_name`` still carries the ``retry-`` prefix and its own
    80-character truncation, because that bound is AWS's, on execution names
    only. Keeping the two identifiers distinct is asserted so the prefix clause
    cannot be satisfied by unifying them — which would reintroduce an external
    length bound into an identifier that has none.

    **R5.4: the lineage identifier is the same resolvable id, not a second
    one.** Task 3.1 deleted the forward pointer ``retried_by_run_id``, which was
    written by this endpoint and read by nothing, so the only lineage identifier
    the body reports is ``retry_source_run_id`` — and under the continuation
    model it equals ``run_id``, the id Property 1 proves ``get_run_details``
    resolves. Asserting the equality is what keeps a future change from
    reporting lineage as a separate id a reader cannot resolve.
    """
    with retry_run_environment(run) as mocks:
        retry_status, body = parse(runs.retry_run(run.run_id))

        assert retry_status == 200, (
            f"the retry of a failed run must succeed before its reported id can be judged; "
            f"got {retry_status} {body!r} for {run.run_id!r} "
            f"({len(run.state_rows)} state rows, failed at {run.failed_step_name!r})"
        )

        sfn_input = _sfn_start_execution_input(mocks)

    reported_run_id = body.get("run_id")
    executing_run_id = sfn_input.get("run_id")

    assert reported_run_id == executing_run_id == run.run_id, (
        f"R1.2: the reported id, the executing id and the requested id must be ONE id, not two "
        f"bridged by a reader — reported={reported_run_id!r}, inside the Step Functions "
        f"input={executing_run_id!r}, requested={run.run_id!r}"
    )

    assert not reported_run_id.startswith(_FABRICATED_PREFIX), (
        f"R1.6 / do-NOT 1: the reported id {reported_run_id!r} carries the {_FABRICATED_PREFIX!r} "
        f"prefix, which sorts below every real run id on the executions table's descending "
        f"run_id sort with a server-side Limit, so its page position is never chronological "
        f"(LGR-21a1b5b-4)"
    )

    # The lexicographic fact the clause rests on. It holds for every id this
    # generator produces, which is the point: prefixing is not a stylistic
    # choice, it demotes the row below every sibling.
    assert f"{_FABRICATED_PREFIX}{run.run_id}" < run.run_id, (
        f"the R1.6 reasoning assumes {_FABRICATED_PREFIX + run.run_id!r} sorts below "
        f"{run.run_id!r}; if that stops holding, the sort-order argument in this docstring "
        f"needs rewriting rather than the assertion above needs relaxing"
    )

    # §4.8: the execution NAME is a separate identifier and keeps the prefix.
    execution_name = body.get("execution_name")
    assert isinstance(execution_name, str) and execution_name.startswith(_FABRICATED_PREFIX), (
        f"the execution name is a separate identifier with a real AWS bound and keeps its "
        f"{_FABRICATED_PREFIX!r} prefix (§4.8); got {execution_name!r}"
    )
    assert execution_name != reported_run_id, (
        f"the run id and the execution name must stay two identifiers — unifying them "
        f"reintroduces AWS's 80-character execution-name bound into an id that has none "
        f"(§4.8); both read {reported_run_id!r}"
    )

    assert body.get("retry_source_run_id") == reported_run_id, (
        f"R5.4: the lineage identifier the endpoint records must be one a reader can resolve. "
        f"retry_source_run_id={body.get('retry_source_run_id')!r} is not the reported id "
        f"{reported_run_id!r}, so it is a second identifier rather than the resolvable one"
    )


# ---------------------------------------------------------------------------
# The harness guard
# ---------------------------------------------------------------------------


def test_the_mock_state_table_is_keyed_by_run_id() -> None:
    """The world above is not vacuous: an unwritten id 404s against it.

    Property 1's whole content is that a *particular* id resolves. If the mock
    state table answered every query with the run's rows, the property would
    return ``200`` for any id at all — including the fabricated
    ``retry-run-…`` id the live defect reported — and would pass against the
    bug it exists to catch. This test asserts the negative directly so that a
    future simplification of :func:`retry_run_environment` fails here rather
    than silently emptying Property 1.

    **Validates: Requirements 1.1**
    """
    run = FailedRun(
        run_id="run-2026-08-09-15-38-19-5c5f5f8a",
        pipeline_id="newtest-ab2a08a0",
        state_rows=[
            _state_row(
                run_id="run-2026-08-09-15-38-19-5c5f5f8a",
                pipeline_id="newtest-ab2a08a0",
                step_name="file_validation",
                status="error",
                sequence=1,
                timestamp="2026-08-09T15:38:19+00:00",
            )
        ],
        failed_step_name="file_validation",
        pipeline_record={
            "pipeline_id": "newtest-ab2a08a0",
            "source_type": "S3_UPLOAD",
            "source_bucket": "adi-source-files-123456789012-us-west-2",
        },
        executions_row={},
    )
    run = run._replace(
        executions_row=_executions_row(run_id=run.run_id, pipeline_id=run.pipeline_id, state_rows=run.state_rows)
    )

    with retry_run_environment(run) as _mocks:
        resolved_status, _ = parse(runs.get_run_details(run.run_id))
        # The exact id the shipped bug reported for this run.
        phantom_status, phantom_body = parse(runs.get_run_details(f"retry-{run.run_id}-537b2303"))

    assert resolved_status == 200, "the run that WAS written must resolve"
    assert phantom_status == 404, (
        f"an id no state row was written under must 404 against this mock world, otherwise "
        f"Property 1 cannot fail; got {phantom_status} {phantom_body!r}"
    )


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

#: The three fields R3.6 names. They are not knowable at the instant a retry
#: starts — `execution_updater` writes them as steps report — so a row created
#: eagerly is *necessarily* incomplete, which is the `—` / `Unknown` /
#: no-schema-version display the phantom produced. R3.6's answer is therefore
#: "carry them, or be absent", and the retry satisfies it by never writing the
#: three at all: they survive because the update does not name them.
_RUN_HISTORY_FIELDS = ("file_name", "ai_model_used", "schema_hash")

#: The tables the retry could conceivably write. `put_item` is asserted zero on
#: every one of them rather than on the executions table alone: R3.1 forbids
#: *creating a row*, and a fabricated row moved to a different table would
#: satisfy a narrower assertion while breaking the requirement.
_TABLE_FIELDS = ("state_table", "executions_table", "pipelines_table", "approvals_table", "locks_table")

#: DynamoDB's four update-expression sections. Only SET and REMOVE are used
#: today (§4.2); ADD and DELETE are parsed so that adding one later cannot slip
#: a write to a preserved field past this property.
_UPDATE_SECTION_KEYWORD = re.compile(r"\b(SET|REMOVE|ADD|DELETE)\b")


def _expression_attribute_targets(update_kwargs: dict[str, Any]) -> frozenset[str]:
    """Every attribute name an ``update_item``'s expression writes or removes.

    **Why the property is asserted this way, and not by folding the update onto
    the row.** "Afterwards the row still carries its ``file_name``,
    ``ai_model_used`` and ``schema_hash`` unchanged" is a claim about what the
    update expression *does not touch* — §4.2's expression SETs six or eight
    named attributes and REMOVEs two, and none of the three is among them. A
    fold (applying the parsed clauses to a copy of the row, as
    ``test_pipelines_retry.py``'s ``_apply_update`` does for its five-retry
    sequence) would re-derive the same answer from the same parse and add a
    simulation of DynamoDB to be wrong about. Asserting the expression directly
    is both stronger and narrower: it fails on a write that a fold would have
    to be told how to apply.

    ``ExpressionAttributeNames`` is resolved, which is the point of doing this
    with a parser rather than a substring search. ``#status`` and ``#trigger``
    are aliased today because both are DynamoDB reserved words, so an aliased
    ``#fn`` → ``file_name`` would leave the literal name nowhere in the
    expression string and a ``"file_name" not in expression`` assertion would
    pass against it. Document paths are reduced to their root attribute, so
    ``SET file_name.original = :x`` counts as touching ``file_name``.
    """
    expression = update_kwargs["UpdateExpression"]
    names: dict[str, str] = update_kwargs.get("ExpressionAttributeNames") or {}

    targets: set[str] = set()
    parts = _UPDATE_SECTION_KEYWORD.split(expression)
    # split() yields [prelude, KEYWORD, body, KEYWORD, body, ...] — the prelude
    # is empty for a well-formed expression and is dropped either way.
    for keyword, body in zip(parts[1::2], parts[2::2], strict=True):
        for raw_clause in body.split(","):
            clause = raw_clause.strip()
            if not clause:
                continue
            # SET writes `<path> = <value>`; REMOVE names a path alone; ADD and
            # DELETE name `<path> <value>`.
            path = clause.split("=", 1)[0] if keyword == "SET" else clause.split()[0]
            root = re.split(r"[.\[]", path.strip(), maxsplit=1)[0].strip()
            if root:
                targets.add(names.get(root, root))
    return frozenset(targets)


def _sole_executions_update(mocks: RetryMocks) -> dict[str, Any]:
    """The kwargs of the one guarded executions-table ``update_item``.

    Exactly one is asserted rather than assumed, for the same reason
    :func:`_sfn_start_execution_input` asserts one ``start_execution``: with two
    calls "the" expression is whichever happened to be last, and the count is
    itself part of Property 6 — the snapshot is a read-modify-write off the
    pre-flight read (`pipelines_runs.py`'s ``exec_row``), so a second
    ``update_item`` would mean a second, unread-from write.
    """
    calls = mocks.executions_table.update_item.call_args_list
    assert len(calls) == 1, (
        f"a retry issues exactly one executions-table update_item — the snapshot-and-reset of "
        f"§4.2 — but {len(calls)} were recorded: {[call[1].get('UpdateExpression') for call in calls]}"
    )
    return dict(calls[0][1])


def _assert_no_row_was_created(mocks: RetryMocks, *, world: str) -> None:
    """R3.1 / do-NOT 2: nothing anywhere is created by a retry.

    The deleted ``put_item`` (`pipelines_runs.py:1345-1356` before task 3.1) is
    the Phantom_Execution_Row three separate readers would each have to learn to
    ignore — the WebSocket stream, the ``StatusIndex`` GSI and the paged
    run-history read (``LGR-21a1b5b-2``, **template / ratified**, also an
    in-code fence at ``data-stack.ts:237-251``).
    """
    for field in _TABLE_FIELDS:
        table = getattr(mocks, field)
        calls = table.put_item.call_args_list
        written = [call[1].get("Item") for call in calls]
        assert calls == [], (
            f"R3.1 / do-NOT 2: the retry called put_item on {field} in the {world} world "
            f"({len(calls)} times, Items={written!r}). A row a retry creates is the phantom "
            f"LGR-21a1b5b-2 refused: it broadcasts a dashboard event, enters the StatusIndex "
            f"partition and consumes a run-history page slot"
        )


def _conditional_check_failed() -> ClientError:
    """What DynamoDB raises when ``attribute_exists(run_id)`` finds no row.

    The mock tables in :func:`retry_run_environment` do not evaluate condition
    expressions, so the absent-row world is completed here: with no row to
    annotate, the guarded write fails, which is the branch R3.6's "or absent"
    exists to permit. Same construction as
    ``test_pipelines_retry.py``'s error-path arms.
    """
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "UpdateItem",
    )


@given(run=failed_runs())
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_the_retry_never_creates_a_row_and_never_leaves_an_incomplete_one(run: FailedRun) -> None:
    """Property 6: The retry never creates a row, and never leaves an incomplete one.

    # Feature: p38-retry-run-identity, Property 6: The retry never creates a row, and never leaves an incomplete one

    For any failed run, the retry endpoint calls ``put_item`` zero times, issues
    exactly one executions-table ``update_item`` carrying
    ``ConditionExpression="attribute_exists(run_id)"``, and afterwards either
    the row still carries its ``file_name``, ``ai_model_used`` and
    ``schema_hash`` unchanged, or no row exists for that run id at all.

    **Validates: Requirements 3.1, 3.6**

    **Both disjuncts are exercised on every example**, because R3.6 is a
    disjunction and a property that only ever reached one arm would leave the
    other unasserted:

    - **Row present** — ``retry_run_environment(run)``. The retry annotates the
      row it read and the three run-history fields survive, because the update
      expression does not name them.
    - **Row absent** — ``retry_run_environment(run, executions_rows=[])``, with
      the guarded write failing its condition. This is R3.6's "or absent", and
      asserting it as a first-class case is what makes R3.1 *constructive*: the
      guard means the alternative to a complete row is **no** row, never a
      half-filled one. Two implementation facts are pinned with it — the
      ``attempts`` / ``attempt_count`` SET clauses are omitted entirely when the
      pre-flight read found nothing (writing them would fabricate an
      Attempt_Record, which ``LGR-99050e6-2``, **template / ratified**, refuses:
      absent is reported as unknown, never defaulted), and the retry still
      returns ``200`` and still starts its execution, because the snapshot is
      bookkeeping *about* a run and R2.5 forbids it failing one
      (``LGR-eee8ac8-4``, **template / ratified**).

    **The guard is the whole mechanism, and it is borrowed rather than
    invented.** ``ConditionExpression="attribute_exists(run_id)"`` is the same
    guard ``cache_step_result`` uses one table over
    (``retry_utils.py:236-247``, ratified at ``LGR-60c4985-4``, **template /
    ratified**, whose own body records the fence: the cache write "annotates an
    existing row and never creates one … so a step that never wrote a state row
    cannot leave a phantom row for the dashboard's step readers"). Without it
    ``update_item`` **creates** the row — so deleting the ``put_item`` without
    adding the condition would have moved the phantom rather than removed it,
    and that is why the condition is asserted by value here rather than merely
    assumed present.
    """
    # Held before the call so an in-place mutation of the row the mock handed
    # the handler is detectable — the pre-flight read yields the very dict the
    # executions table holds, so "unchanged" includes "not mutated in process".
    row_before = copy.deepcopy(run.executions_row)

    # --- Disjunct 1: the row exists, and its run-history fields survive ----
    with retry_run_environment(run) as mocks:
        status, body = parse(runs.retry_run(run.run_id))

        assert status == 200, (
            f"the retry of a failed run must succeed before its writes can be judged; "
            f"got {status} {body!r} for {run.run_id!r} "
            f"({len(run.state_rows)} state rows, failed at {run.failed_step_name!r})"
        )

        _assert_no_row_was_created(mocks, world="row-present")
        update_kwargs = _sole_executions_update(mocks)

    assert update_kwargs["ConditionExpression"] == "attribute_exists(run_id)", (
        f"the snapshot update must be guarded by attribute_exists(run_id) (LGR-60c4985-4); "
        f"got {update_kwargs.get('ConditionExpression')!r}. Unguarded, update_item CREATES the "
        f"row, so the phantom would have moved from the put_item to here rather than gone"
    )
    assert update_kwargs["Key"] == {"pipeline_id": run.pipeline_id, "run_id": run.run_id}, (
        f"the snapshot annotates the ORIGINAL row; got Key={update_kwargs['Key']!r} for "
        f"pipeline {run.pipeline_id!r} / run {run.run_id!r}"
    )

    touched = _expression_attribute_targets(update_kwargs)
    assert touched.isdisjoint(_RUN_HISTORY_FIELDS), (
        f"R3.6: the retry's update expression writes {sorted(touched.intersection(_RUN_HISTORY_FIELDS))}, "
        f"which the retry cannot know yet — `execution_updater` fills them as steps report, so any "
        f"value written here is fabricated. Expression: {update_kwargs['UpdateExpression']!r}"
    )
    # The clause is only worth asserting if the parser saw something: an empty
    # target set would make the disjointness above vacuously true.
    assert touched, f"no attribute targets parsed out of {update_kwargs['UpdateExpression']!r}"

    for field in _RUN_HISTORY_FIELDS:
        assert run.executions_row.get(field) == row_before.get(field), (
            f"R3.6: {field!r} on the run's executions row read {run.executions_row.get(field)!r} "
            f"after the retry and {row_before.get(field)!r} before it"
        )
    assert run.executions_row == row_before, (
        "the retry mutated the executions row dict in process rather than only describing an "
        "update to it, so what the row 'still carries' would depend on call order"
    )

    # --- Disjunct 2: no row exists, and the retry leaves none -------------
    with retry_run_environment(run, executions_rows=[]) as absent_mocks:
        absent_mocks.executions_table.update_item.side_effect = _conditional_check_failed()

        absent_status, absent_body = parse(runs.retry_run(run.run_id))

        _assert_no_row_was_created(absent_mocks, world="row-absent")
        absent_update = _sole_executions_update(absent_mocks)

        # Nothing exists for the run id afterwards — asserted through both reads
        # a surface uses: the paged run-history query on `pipeline_id` and the
        # keyed read of the row itself.
        history = absent_mocks.executions_table.query(
            KeyConditionExpression=Key("pipeline_id").eq(run.pipeline_id),
            ScanIndexForward=False,
            Limit=20,
        )
        keyed = absent_mocks.executions_table.get_item(Key={"pipeline_id": run.pipeline_id, "run_id": run.run_id})
        started = absent_mocks.sfn.start_execution.call_count

    assert history.get("Items") == [], (
        f"R3.6's 'or absent' branch must leave NOTHING behind, but the run-history read returns "
        f"{history.get('Items')!r} for pipeline {run.pipeline_id!r}"
    )
    assert "Item" not in keyed, f"a keyed read of the refused row returned {keyed.get('Item')!r}"

    assert absent_update["ConditionExpression"] == "attribute_exists(run_id)", (
        "the write against a missing row must be the guarded one — that is what makes it fail "
        "instead of creating the row"
    )
    absent_values = absent_update["ExpressionAttributeValues"]
    assert ":attempts" not in absent_values and ":attempt_count" not in absent_values, (
        f"with no pre-flight row read, the attempts clauses must be omitted rather than written "
        f"from nothing (LGR-99050e6-2: absent is unknown, never defaulted); got "
        f"{sorted(absent_values)}"
    )

    assert absent_status == 200, (
        f"R2.5: a refused snapshot must not fail the retry — the row being absent is a fact about "
        f"bookkeeping, not about the run; got {absent_status} {absent_body!r}"
    )
    assert absent_body.get("run_id") == run.run_id
    assert started == 1, (
        f"R2.5: the retry proceeds without its bookkeeping, so start_execution must still run "
        f"exactly once; it ran {started} times"
    )


def test_the_update_expression_parser_sees_a_touched_field() -> None:
    """Property 6's third clause is not vacuous: a write to a preserved field is seen.

    :func:`_expression_attribute_targets` is the only thing standing between
    "the three fields survive" and an assertion that passes on any expression at
    all, so both ways of naming an attribute are checked directly: literally,
    and through ``ExpressionAttributeNames``. The aliased case is the one a
    substring search would miss, and aliasing is not hypothetical here —
    ``#status`` and ``#trigger`` are aliased in the real expression because both
    are DynamoDB reserved words.

    **Validates: Requirements 3.6**
    """
    real_shape = {
        "UpdateExpression": (
            "SET #status = :running, #trigger = :retry, retry_source_run_id = :run_id, "
            "execution_name = :execution_name, updated_at = :now, attempts = :attempts, "
            "attempt_count = :attempt_count REMOVE completed_at, retried_by_run_id"
        ),
        "ExpressionAttributeNames": {"#status": "status", "#trigger": "trigger"},
    }
    targets = _expression_attribute_targets(real_shape)
    assert targets == {
        "status",
        "trigger",
        "retry_source_run_id",
        "execution_name",
        "updated_at",
        "attempts",
        "attempt_count",
        "completed_at",
        "retried_by_run_id",
    }
    assert targets.isdisjoint(_RUN_HISTORY_FIELDS)

    # A literal write to a preserved field.
    assert "file_name" in _expression_attribute_targets({"UpdateExpression": "SET updated_at = :now, file_name = :fn"})
    # The same write hidden behind an alias, and behind a document path.
    assert "ai_model_used" in _expression_attribute_targets(
        {"UpdateExpression": "SET #m = :m", "ExpressionAttributeNames": {"#m": "ai_model_used"}}
    )
    assert "schema_hash" in _expression_attribute_targets({"UpdateExpression": "SET schema_hash.value = :h"})
    # And a REMOVE of one, which is a change to what the row carries just as
    # much as a SET is.
    assert "file_name" in _expression_attribute_targets({"UpdateExpression": "REMOVE completed_at, file_name"})


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

#: Every key the attempt node may carry (§4.2's table, as task 3.1 built it).
#: Asserted as an upper bound so a field defaulted into the node later — the
#: exact move `LGR-99050e6-2` refuses — fails here rather than passing because
#: nothing looked for it.
_ATTEMPT_NODE_KEYS = frozenset(
    {"attempt", "failed_step", "status", "steps_status", "started_at", "completed_at", "execution_name"}
)

#: The row-derived keys the snapshot copies, in the handler's own loop order.
#: `attempt` and `failed_step` are computed rather than copied and are asserted
#: separately.
_ROW_DERIVED_NODE_KEYS = ("status", "steps_status", "started_at", "completed_at", "execution_name")

#: Row-level `status` values. Terminal and non-terminal, in both casings, plus
#: the empty string — the falsy-but-present case the truthiness decision turns
#: on. `execution_updater` maps a step's word to an uppercase row status
#: (`execution_updater/lambda_function.py:131-132`), and `retry_run` still
#: uppercases on read (`item_str(ex, "status").upper()`), so lowercase rows are
#: generated too: the snapshot copies whatever the row holds and must not
#: normalise it on the way into the node.
_ROW_STATUSES = st.sampled_from(["FAILED", "SUCCEEDED", "TIMED_OUT", "ABORTED", "RUNNING", "failed", "succeeded", ""])

#: Per-step `steps_status` values. Both success spellings and both failure
#: spellings occur in live rows (`step_errors` writes `failed`, several task
#: Lambdas write `error`, `execution_updater` mirrors the state row's own word),
#: and the non-terminal ones are what a row interrupted mid-run carries.
_STEP_STATUSES = st.sampled_from(["success", "completed", "failed", "error", "running", "pending", "skipped"])

#: `completed_at`: **absent**, present-but-empty, or a real timestamp. Absent is
#: a first-class draw and not an afterthought — it is the case the
#: omit-rather-than-default clause exists for, and it is the common case for
#: every row a retry displaces mid-flight.
_COMPLETED_AT_VALUES = st.one_of(st.none(), st.just(""), st.just("2026-08-09T16:04:11+00:00"))

#: The shape axis. Two arms force R2.3's disagreement in each direction, four
#: force a falsy or absent field, and `as_drawn` leaves the drawn values alone
#: so agreeing rows and incidental disagreements both keep occurring.
#:
#: `row_succeeded_step_failed` is the **observed live state**: `run-…-5c5f5f8a`
#: reads `SUCCEEDED` while its own `steps_status.test_code` reads `FAILED`
#: (`requirements.md` Introduction, defect 3). Every retried run predating this
#: fix is in that state, so it is the common case rather than an edge one
#: (`requirements.md` §5 risk 5).
_ROW_SHAPES = (
    "as_drawn",
    "row_succeeded_step_failed",
    "row_failed_step_completed",
    "row_status_absent",
    "row_status_empty",
    "steps_status_absent",
    "steps_status_empty",
)

#: Words that read as an outcome. Used only to decide whether a
#: `status` / `steps_status` pair *disagrees* — the property never reconciles
#: them, it asserts both survive.
_SUCCESS_WORDS = frozenset({"success", "succeeded", "completed", "complete"})
_FAILURE_WORDS = frozenset({"failed", "failure", "error"})


def _outcome(status: Any) -> str | None:
    """``"success"`` / ``"failure"``, or ``None`` when the word says neither.

    ``RUNNING``, ``pending``, ``skipped`` and the empty string all answer
    ``None``: they are not a claim about an outcome, so they cannot contradict
    one.
    """
    lowered = str(status).strip().lower()
    if lowered in _SUCCESS_WORDS:
        return "success"
    if lowered in _FAILURE_WORDS:
        return "failure"
    return None


def _disagreeing_step_entries(status: Any, steps_status: Any) -> list[tuple[str, Any]]:
    """The ``steps_status`` entries whose outcome contradicts *status*.

    R2.3's disagreement lives **inside** the map and the row status together —
    neither is wrong on its own — so it can only be read off the pair. This
    returns the pair's witnesses, and the property asserts they are all still
    readable *from the attempt node alone* after the snapshot.
    """
    row_outcome = _outcome(status)
    if row_outcome is None or not isinstance(steps_status, dict):
        return []
    return [
        (name, value)
        for name, value in steps_status.items()
        if _outcome(value) is not None and _outcome(value) != row_outcome
    ]


def _shape_executions_row(
    base_row: dict[str, Any],
    *,
    shape: str,
    row_status: str | None,
    steps_status: dict[str, str] | None,
    completed_at: str | None,
    execution_name_present: bool,
) -> dict[str, Any]:
    """Apply one :data:`_ROW_SHAPES` shape to a realistic executions row.

    Pure, so :func:`test_the_displaced_row_generator_reaches_the_named_shapes`
    can check each named shape directly instead of hoping Hypothesis reached it.

    ``None`` means **absent** and ``""``/``{}`` mean present-but-falsy — the
    distinction the truthiness decision is about, so it is carried in the
    generator's own vocabulary rather than collapsed.
    """
    row = copy.deepcopy(base_row)
    step_keys = list(steps_status or {})

    if shape == "row_succeeded_step_failed" and step_keys:
        row_status = "SUCCEEDED"
        steps_status = {**(steps_status or {}), step_keys[-1]: "failed"}
    elif shape == "row_failed_step_completed" and step_keys:
        row_status = "FAILED"
        steps_status = {**(steps_status or {}), step_keys[-1]: "completed"}
    elif shape == "row_status_absent":
        row_status = None
    elif shape == "row_status_empty":
        row_status = ""
    elif shape == "steps_status_absent":
        steps_status = None
    elif shape == "steps_status_empty":
        steps_status = {}

    for key, value in (
        ("status", row_status),
        ("steps_status", steps_status),
        ("completed_at", completed_at),
    ):
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value

    if not execution_name_present:
        # Rows written before `execution_name` was populated carry none, and
        # the node's only durable link to a Step Functions execution is then
        # genuinely unknown rather than empty.
        row.pop("execution_name", None)

    return row


class DisplacedRow(NamedTuple):
    """A failed run plus the executions row its retry is about to displace.

    ``row`` replaces ``run.executions_row`` for the example — it is passed to
    ``retry_run_environment(run, executions_rows=[row])``, which is the
    injection point the module header names.
    """

    run: FailedRun
    row: dict[str, Any]
    shape: str


@st.composite
def displaced_rows(draw: st.DrawFn) -> DisplacedRow:
    """A failed run whose executions row varies in every field Property 4 reads.

    ``status`` and each ``steps_status`` entry are drawn independently, so
    disagreeing pairs arise on their own; :data:`_ROW_SHAPES` then forces the
    two named disagreements and the four falsy/absent cases so none of them
    depends on the draw happening to land there.
    """
    run = draw(failed_runs())
    step_keys = [row["step_name"] for row in run.state_rows]
    shape = draw(st.sampled_from(_ROW_SHAPES))

    row = _shape_executions_row(
        run.executions_row,
        shape=shape,
        row_status=draw(_ROW_STATUSES),
        steps_status={name: draw(_STEP_STATUSES) for name in step_keys},
        completed_at=draw(_COMPLETED_AT_VALUES),
        execution_name_present=draw(st.booleans()),
    )
    return DisplacedRow(run=run, row=row, shape=shape)


def _appended_attempt_node(update_kwargs: dict[str, Any]) -> dict[str, Any]:
    """The attempt node the snapshot appends, off the one ``update_item``.

    ``:attempts`` is the whole trimmed list; the node this retry contributed is
    its last element. Its presence is asserted rather than assumed — when the
    pre-flight read finds no row the clause is omitted entirely (Property 6's
    absent-row disjunct), and Property 4's generator always supplies a row, so
    a missing ``:attempts`` here means the row was not found and the property
    would otherwise pass by having nothing to check.
    """
    values = update_kwargs["ExpressionAttributeValues"]
    assert ":attempts" in values, (
        f"the snapshot wrote no attempts clause, so there is no node to judge — the generated "
        f"executions row was not returned by the pre-flight read. Values: {sorted(values)}"
    )
    attempts = values[":attempts"]
    assert isinstance(attempts, list) and attempts, f"the attempts clause must be a non-empty list; got {attempts!r}"
    node = attempts[-1]
    assert isinstance(node, dict), f"an attempt node must be a map; got {node!r}"
    return node


@given(displaced=displaced_rows())
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_an_attempt_snapshot_reproduces_the_attempt_it_displaces(displaced: DisplacedRow) -> None:
    """Property 4: An attempt snapshot reproduces the attempt it displaces.

    # Feature: p38-retry-run-identity, Property 4: An attempt snapshot reproduces the attempt it displaces

    For any executions row — any ``status``, any ``steps_status`` map,
    ``completed_at`` present or absent — the attempt node the retry appends
    reproduces that row's pre-retry ``status`` and ``steps_status`` verbatim,
    and omits ``completed_at`` rather than defaulting it when the row had none.

    **Validates: Requirements 2.1, 2.3**

    **Why the snapshot has to be verbatim rather than derived.** Under the
    continuation model the retry executes and reports under the Original_Run's
    id, so the original's executions row is the **only** row — `execution_updater`
    overwrites its ``status``, ``current_step`` and ``steps_status`` entries from
    the retry's first reported step (`execution_updater/lambda_function.py`,
    read end to end at the two write sites the handler's own comment cites). The
    node taken *before* ``start_execution`` is therefore the entire surviving
    record of the displaced attempt, and anything it derives instead of copying
    is a claim about an attempt nobody can check afterwards.

    **The omit-rather-than-default clause, and the record that governs it.**
    ``LGR-99050e6-2`` (**template / ratified**, verified resolvable): "an absent
    quarantine counter is reported as unknown rather than as zero, because rows
    written before the row-level split carry no counters and did no
    quarantining". Applied here: a row with no ``completed_at`` is a row whose
    attempt has no recorded end, and stamping one — ``started_at``, ``now``,
    ``""``, ``None`` — invents the timestamp a reader would then take at face
    value. So the assertion is that the **key is absent from the node**, not
    that it holds some benign filler, and :data:`_ATTEMPT_NODE_KEYS` bounds the
    node's key set so a later default cannot arrive unnoticed.

    **The decision this property had to take a side on: the guard is truthiness,
    not presence** (`pipelines_runs.py`, the ``if _node_value:`` in the
    snapshot's copy loop). Absent and present-but-falsy are therefore treated
    alike — an empty ``steps_status`` (``{}``) or an empty-string ``status`` is
    omitted, not reproduced as empty — and "reproduces verbatim" and "omits
    falsy" would collide if the property demanded both. **The property asserts
    the truthiness reading:** a value the row carries is reproduced verbatim, and
    a key the row does not carry *a value for* is omitted, never defaulted. The
    falsy-but-present rows are generated on purpose
    (``row_status_empty``, ``steps_status_empty``) rather than steered around, so
    the reading is pinned rather than avoided.

    **That reading is a small, deliberate divergence from
    ``LGR-b2f79ef-14``** (**template / ratified**, verified resolvable), whose
    reasoning separates "could not check" from "checked and empty" and would say
    ``steps_status: {}`` deserves to survive as *checked and empty* rather than
    being reported as unknown. It is recorded here rather than fixed, for two
    reasons. First, no writer produces the shape: `execution_updater` sets
    ``current_step`` **and** ``steps_status`` together on its first write, and
    `pipeline_trigger` sets neither — which is exactly why §4.5's phantom
    predicate can test the two fields as a pair — so a row carrying an empty map
    or an empty status is unreachable through the live write paths. Second, the
    only difference in outcome is between one unreadable rendering and another:
    an attempt node whose ``steps_status`` is ``{}`` tells a reader nothing that
    its absence does not. **A handler change would be needed to close it, and
    Property 4 does not ask for one** — it is reported as a finding instead.

    **R2.3 is the interesting half, and it is asserted from the node alone.**
    The disagreement lives *inside* the ``steps_status`` map and the row
    ``status`` together, so the generator forces contradicting pairs in both
    directions — ``SUCCEEDED`` over a ``failed`` step, which is the observed live
    state of ``run-…-5c5f5f8a``, and ``FAILED`` over a ``completed`` one — and
    the property recomputes the disagreement **from the node** rather than
    comparing the node against the row twice. That is what shows both values
    survived into *one* node instead of one being reconciled away.
    ``tests/contracts/api_responses/test_run_details_contract.py`` (task 5.5)
    pins the same disagreeing pair on the read side, so the write side asserted
    here and the read side meet.

    **Not asserted here, on purpose:** attempt numbering, the cap of five and
    which nodes are retained. Those are Property 5 (task 3.6), which owns the
    ``attempt_count``-derived numbering that keeps increasing past the cap.
    ``attempt`` is checked only for being a positive integer, which no numbering
    scheme this design permits can fail.
    """
    run, row, shape = displaced
    # Held before the call: "verbatim" means the value the row carried when the
    # snapshot was taken, and the node holds the row's own nested dict by
    # reference, so a comparison against a post-call read would be comparing the
    # node with itself.
    row_before = copy.deepcopy(row)

    with retry_run_environment(run, executions_rows=[row]) as mocks:
        status_code, body = parse(runs.retry_run(run.run_id))

        assert status_code == 200, (
            f"the retry of a failed run must succeed before its snapshot can be judged; "
            f"got {status_code} {body!r} for {run.run_id!r} (shape={shape!r}, "
            f"row status={row.get('status')!r}, steps_status={row.get('steps_status')!r})"
        )

        node = _appended_attempt_node(_sole_executions_update(mocks))

    assert row == row_before, (
        f"the retry mutated the executions row dict in process rather than only reading it, so "
        f"what the node 'reproduces' depends on call order. Before: {row_before!r} after: {row!r}"
    )

    unexpected = set(node) - _ATTEMPT_NODE_KEYS
    assert not unexpected, (
        f"the attempt node carries {sorted(unexpected)}, which §4.2's table does not list. A field "
        f"that is not copied from the row is derived, and a derived field in a record of a past "
        f"attempt is a claim nobody can check afterwards"
    )

    # `attempt` and `failed_step` are the node's two computed fields. Numbering
    # itself belongs to Property 5; all that is asserted here is that the node
    # is attributed at all (R2.4's per-attempt attribution is what makes the
    # reproduced statuses readable as *one attempt's*), and that `failed_step`
    # records what the next attempt started from.
    assert isinstance(node.get("attempt"), int) and not isinstance(node.get("attempt"), bool), (
        f"an attempt node must be attributed to an attempt number; got {node.get('attempt')!r}"
    )
    assert node["attempt"] >= 1, f"attempt numbers start at 1; got {node['attempt']!r}"
    assert node.get("failed_step") == body["retry_from_step"], (
        f"the node's failed_step must record the step the next attempt started from — "
        f"{node.get('failed_step')!r} against the response's {body['retry_from_step']!r}"
    )

    # --- The reproduce-or-omit clause, over every row-derived field ---------
    for key in _ROW_DERIVED_NODE_KEYS:
        row_value = row_before.get(key)
        if row_value:
            assert key in node, (
                f"the row carried {key}={row_value!r} and the attempt node omits it, so the "
                f"displaced attempt's {key} is lost the moment `execution_updater` overwrites the "
                f"row (R2.1). Node: {node!r}"
            )
            assert node[key] == row_value, (
                f"the attempt node must reproduce the row's {key} VERBATIM: node has {node[key]!r}, "
                f"the row carried {row_value!r} (shape={shape!r})"
            )
        else:
            assert key not in node, (
                f"the row carried no {key} ({row_value!r}) and the node reports "
                f"{node.get(key)!r} — LGR-99050e6-2: absent is reported as unknown, never "
                f"defaulted, so the key is omitted rather than filled in (shape={shape!r})"
            )

    # The clause the task singles out, asserted in its own right so a failure
    # names it rather than naming a loop iteration.
    if "completed_at" not in row_before:
        assert "completed_at" not in node, (
            f"the displaced row had NO completed_at and the node reports "
            f"{node.get('completed_at')!r}. A stamped end time for an attempt whose end was never "
            f"recorded is a fabricated Attempt_Record — LGR-99050e6-2 (template / ratified) "
            f"refuses exactly that: absent is unknown, not defaulted"
        )
    elif row_before["completed_at"]:
        assert node.get("completed_at") == row_before["completed_at"], (
            f"a completed_at the row DID carry must be reproduced verbatim; row "
            f"{row_before['completed_at']!r}, node {node.get('completed_at')!r}"
        )
    else:
        # Present but empty. The truthiness reading omits it — the divergence
        # from LGR-b2f79ef-14 argued in this docstring, pinned rather than
        # avoided so a handler change to close it fails here loudly.
        assert "completed_at" not in node, (
            f"the row carried completed_at={row_before['completed_at']!r} (present but empty) and "
            f"the node reports {node.get('completed_at')!r}. If the handler has started "
            f"distinguishing 'checked and empty' from 'could not check', this property's stated "
            f"truthiness reading is what needs revisiting — see the docstring"
        )

    # --- R2.3: the disagreement survives into ONE node ---------------------
    disagreements = _disagreeing_step_entries(row_before.get("status"), row_before.get("steps_status"))

    if shape in ("row_succeeded_step_failed", "row_failed_step_completed"):
        # Generator sanity: these two shapes exist to force a disagreement, so a
        # future edit that stops producing one must fail here rather than leave
        # the R2.3 assertions below quietly unreached.
        assert disagreements, (
            f"shape {shape!r} is supposed to produce a status/steps_status disagreement and "
            f"produced none: status={row_before.get('status')!r}, "
            f"steps_status={row_before.get('steps_status')!r}"
        )

    if disagreements:
        node_steps = node.get("steps_status")
        assert isinstance(node_steps, dict), (
            f"the row's status {row_before.get('status')!r} disagrees with "
            f"{[name for name, _ in disagreements]} in its steps_status, and the node carries no "
            f"steps_status at all — the disagreement is only readable from the pair, so dropping "
            f"either half reconciles it away (R2.3)"
        )
        for name, value in disagreements:
            assert node_steps.get(name) == value, (
                f"the disagreeing entry {name}={value!r} must survive verbatim into the node; "
                f"it reads {node_steps.get(name)!r} there"
            )
        # Recomputed from the NODE, not from the row: this is what shows both
        # values reached one node together rather than one being resolved in
        # favour of the other on the way in.
        assert _disagreeing_step_entries(node.get("status"), node_steps), (
            f"the node reports status={node.get('status')!r} with "
            f"steps_status={node_steps!r}, in which nothing contradicts it — the retry reconciled "
            f"a disagreement the row genuinely held ({disagreements!r}). R2.3: show both rather "
            f"than either alone (LGR-99050e6-2)"
        )


def test_the_displaced_row_generator_reaches_the_named_shapes() -> None:
    """Property 4's generator really produces the two cases the task names.

    A Hypothesis property can only assert what its generator reaches, and the
    two cases task 3.5 singles out — ``completed_at`` absent, and a
    ``status``/``steps_status`` disagreement — are exactly the ones a
    well-meaning simplification of :func:`_shape_executions_row` would stop
    producing while every assertion above kept passing. So each named shape is
    checked here against the pure shaping function directly, rather than left to
    the draw.

    **Validates: Requirements 2.1, 2.3**
    """
    base = _executions_row(
        run_id="run-2026-08-09-15-38-19-5c5f5f8a",
        pipeline_id="newtest-ab2a08a0",
        state_rows=[
            _state_row(
                run_id="run-2026-08-09-15-38-19-5c5f5f8a",
                pipeline_id="newtest-ab2a08a0",
                step_name="test_code",
                status="error",
                sequence=1,
                timestamp="2026-08-09T15:38:19+00:00",
            )
        ],
    )
    assert base.get("completed_at") and base.get("status") and base.get("steps_status"), (
        "the base row must carry all three fields, otherwise the shapes below are shaping nothing"
    )

    def shaped(shape: str, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "shape": shape,
            "row_status": "FAILED",
            "steps_status": {"test_code": "failed"},
            "completed_at": "2026-08-09T16:04:11+00:00",
            "execution_name_present": True,
        }
        kwargs.update(overrides)
        return _shape_executions_row(base, **kwargs)

    # completed_at absent — the omit-rather-than-default case, first class.
    assert "completed_at" not in shaped("as_drawn", completed_at=None)
    assert shaped("as_drawn", completed_at="")["completed_at"] == ""

    # The two forced disagreements, in both directions.
    succeeded_over_failed = shaped("row_succeeded_step_failed")
    assert succeeded_over_failed["status"] == "SUCCEEDED"
    assert _disagreeing_step_entries(succeeded_over_failed["status"], succeeded_over_failed["steps_status"]) == [
        ("test_code", "failed")
    ]

    failed_over_completed = shaped("row_failed_step_completed", steps_status={"test_code": "success"})
    assert failed_over_completed["status"] == "FAILED"
    assert _disagreeing_step_entries(failed_over_completed["status"], failed_over_completed["steps_status"]) == [
        ("test_code", "completed")
    ]

    # The falsy/absent shapes the truthiness decision turns on.
    assert "status" not in shaped("row_status_absent")
    assert shaped("row_status_empty")["status"] == ""
    assert "steps_status" not in shaped("steps_status_absent")
    assert shaped("steps_status_empty")["steps_status"] == {}
    assert "execution_name" not in shaped("as_drawn", execution_name_present=False)

    # And the outcome reader itself: a non-terminal word is not a contradiction,
    # so `RUNNING` over a failed step must not be counted as a disagreement the
    # property then demands the node preserve.
    assert _disagreeing_step_entries("RUNNING", {"test_code": "failed"}) == []
    assert _disagreeing_step_entries("", {"test_code": "failed"}) == []
    assert _disagreeing_step_entries("SUCCEEDED", {"test_code": "skipped"}) == []


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

#: §4.2's cap. Five attempts, newest kept, oldest dropped —
#: ``[*prior_attempts, attempt_node][-5:]`` in the handler.
_ATTEMPTS_CAP = 5

#: Successive retries of one run. 1..8 straddles the cap from **both** sides:
#: 1..4 under it, 5 exactly at it, 6..8 past it. Both sides are needed because
#: the two halves of this property fail in opposite directions — a broken cap
#: shows up only at or above five, and numbering that stops increasing at the
#: cap shows up only above it. Reaching both is verified rather than assumed:
#: :func:`hypothesis.event` labels every example with its retry count, so
#: ``--hypothesis-show-statistics`` prints the distribution.
retry_counts = st.integers(min_value=1, max_value=8)


def _fold_update_onto_row(row: dict[str, Any]) -> Callable[..., None]:
    """A ``update_item`` side effect that applies the retry's own write to *row*.

    **Property 5 is about *successive* retries, so each call must see the state
    the previous one produced.** Without this the mocked pre-flight read would
    hand every retry the same pristine row, every retry would compute
    ``attempt 1``, and the property would assert a cap that is never reached and
    numbering that never increases — passing while testing one retry eight
    times.

    Follows ``test_pipelines_retry.py``'s ``_apply_update``
    (``test_five_successive_retries_all_report_the_original_run_id``) rather than
    introducing a second fold: same clauses, same order, same
    ``row.pop("completed_at")``. That test verified the single eight-retry case
    by execution; this property generalises it over 1..8.

    Only the clauses §4.2's expression actually writes are applied, and
    ``attempts`` is **deep-copied** in so that each recorded ``:attempts`` list
    stays an independent snapshot of what that retry wrote — the per-retry
    assertions below read those recorded lists, and a shared list object would
    make every one of them read the final state.

    Assigned in place rather than through
    ``test_pipelines_retry.py``'s ``_temporary_side_effect``: that helper exists
    because the module-level mocks there survive ``reset_mock()``, which clears
    recorded calls but **not** ``side_effect``, so an in-place assignment would
    leak into every test collected afterwards. :func:`retry_run_environment`
    builds a fresh ``MagicMock`` per example, so there is nothing to leak into —
    the same reason Property 6 assigns its ``ConditionalCheckFailedException``
    side effect directly.
    """

    def _apply(**kwargs: Any) -> None:
        values = kwargs["ExpressionAttributeValues"]
        row["status"] = values[":running"]
        row["trigger"] = values[":retry"]
        row["retry_source_run_id"] = values[":run_id"]
        row["execution_name"] = values[":execution_name"]
        row["updated_at"] = values[":now"]
        if ":attempts" in values:
            row["attempts"] = copy.deepcopy(values[":attempts"])
            row["attempt_count"] = values[":attempt_count"]
        # REMOVE completed_at, retried_by_run_id
        row.pop("completed_at", None)
        row.pop("retried_by_run_id", None)

    return _apply


def _attempt_numbers(attempts: Any) -> list[int]:
    """The ``attempt`` number of every node in an ``attempts`` list.

    Each node's number is asserted to be an ``int`` here rather than trusted, so
    a node numbered ``"3"`` or ``True`` fails on the field rather than on a
    later comparison that would still order correctly.
    """
    assert isinstance(attempts, list), f"the attempts clause must be a list; got {attempts!r}"
    numbers: list[int] = []
    for node in attempts:
        assert isinstance(node, dict), f"an attempt node must be a map; got {node!r}"
        number = node.get("attempt")
        assert isinstance(number, int) and not isinstance(number, bool), (
            f"every attempt node must carry an integer attempt number; got {number!r} in {node!r}"
        )
        numbers.append(number)
    return numbers


@given(run=failed_runs(), retries=retry_counts)
@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_attempt_numbering_is_strictly_increasing_and_the_list_is_capped(run: FailedRun, retries: int) -> None:
    """Property 5: Attempt numbering is strictly increasing and the list is capped.

    # Feature: p38-retry-run-identity, Property 5: Attempt numbering is strictly increasing and the list is capped

    For any number of successive retries of one run, the recorded attempt
    numbers are unique and strictly increasing, the list length is
    ``min(retries, 5)``, and the retained nodes are the most recent ones.

    **Validates: Requirements 2.4**

    **Why the cap and the numbering are one property rather than two.** §4.2
    caps the list at five for a reason that is about item size, not about
    history: the same executions row carries ``step_logs``, up to 50 log lines
    per step (``get_run_details``' log cache), which grows without a bound of its
    own, and a 400 KB DynamoDB item is a **hard write failure** that R2.5
    forbids the snapshot from inflicting on a retry — so an unbounded list would
    eventually make R2.5 unsatisfiable rather than merely degrade
    (``LGR-21a1b5b-2``, **template / ratified**, is the record behind that
    reasoning; its own resolution — a purpose-built table with no stream and no
    GSI — is the named additive fallback if five ever proves too tight).

    Capping the list creates the trap this property exists to catch. **Attempt
    numbering comes off ``attempt_count``, not off ``len(attempts)``** — the
    handler computes ``max(item_int(exec_row, "attempt_count"),
    len(prior_attempts) + 1)`` and writes ``attempt_count = displaced + 1``,
    deliberately, because a length-derived number stops increasing the moment
    the list is full and R2.4's per-attempt attribution would then hand two
    different attempts the same number. So the cap is exactly what makes the
    numbering non-obvious, and asserting one without the other would leave the
    interaction between them untested. Verified against the handler's own read
    path: ``item_int`` defaults an absent ``attempt_count`` to ``0``, so a row
    that has never been retried yields ``max(0, 1) == 1``.

    **"The retained nodes are the most recent ones" is asserted by attempt-number
    identity across the whole sequence, not by list length.** A cap that kept the
    *oldest* five — ``[:5]`` instead of ``[-5:]`` — satisfies every length
    assertion, satisfies uniqueness, and satisfies strict increase within the
    retained list. What it cannot satisfy is the final list's numbers being the
    **tail** of the numbers ever appended: eight retries must leave
    ``[4, 5, 6, 7, 8]``, and an oldest-five cap leaves ``[1, 2, 3, 4, 5]``. So
    every appended node is recorded as it is written and the final list is
    compared against that sequence's tail, by number and by node value.

    **Numbering continuing past the cap is asserted in its own right**, because
    it is the clause the length-derived mutant breaks and the clause R2.4 needs:
    above five, ``attempt_count`` is no longer derivable from the list length at
    all, and that is stated as an assertion rather than left implicit.

    **Task 3.7 verified the single eight-retry case by execution** —
    ``test_five_successive_retries_all_report_the_original_run_id`` and its
    eight-retry sibling pin ``[4, 5, 6, 7, 8]`` with ``attempt_count == 9``.
    This property generalises that over 1..8 and over every run shape
    :func:`failed_runs` produces.

    **Not asserted here, on purpose:** the node's field fidelity — which
    ``status``, ``steps_status`` and ``completed_at`` a node reproduces — is
    Property 4 (task 3.5), which deliberately checks only that ``attempt`` is a
    positive int and defers numbering to here. The zero-``put_item`` and
    guarded-``update_item`` clauses are Property 6 (task 3.4). Neither is
    duplicated.
    """
    # Labels the example so `--hypothesis-show-statistics` reports how often each
    # side of the cap boundary was reached. The claim "1..8 exercises the cap
    # from both sides" is then checkable rather than asserted in a comment.
    event(f"retries={retries}")
    event("cap boundary: at or above" if retries >= _ATTEMPTS_CAP else "cap boundary: below")

    # The row the retries fold onto. A copy, so the shared `run.executions_row`
    # is not carried between examples.
    row = copy.deepcopy(run.executions_row)
    assert "attempts" not in row and "attempt_count" not in row, (
        "a run that has never been retried carries neither field — the first retry's numbering "
        "comes off item_int's default of 0, and seeding either would test a different premise"
    )

    with retry_run_environment(run, executions_rows=[row]) as mocks:
        mocks.executions_table.update_item.side_effect = _fold_update_onto_row(row)

        for index in range(1, retries + 1):
            status_code, body = parse(runs.retry_run(run.run_id))
            assert status_code == 200, (
                f"retry {index} of {retries} must succeed before its attempt numbering can be "
                f"judged; got {status_code} {body!r} for {run.run_id!r} "
                f"(row status={row.get('status')!r}, attempt_count={row.get('attempt_count')!r})"
            )

        updates = [dict(call[1]) for call in mocks.executions_table.update_item.call_args_list]
        final_row = copy.deepcopy(row)

    assert len(updates) == retries, (
        f"{retries} retries must issue {retries} executions-table update_item calls, one snapshot "
        f"each; {len(updates)} were recorded"
    )

    # --- Per-retry: numbering, uniqueness, strict increase, length ---------
    appended_nodes: list[dict[str, Any]] = []
    attempt_counts: list[int] = []

    for index, update_kwargs in enumerate(updates, start=1):
        values = update_kwargs["ExpressionAttributeValues"]
        assert ":attempts" in values and ":attempt_count" in values, (
            f"retry {index} wrote no attempts clause, so there is nothing to number — the folded "
            f"row was not returned by the pre-flight read. Values: {sorted(values)}"
        )
        attempts = values[":attempts"]
        numbers = _attempt_numbers(attempts)

        assert len(set(numbers)) == len(numbers), (
            f"R2.4 attributes each terminal status to the attempt that produced it, so two nodes "
            f"cannot share a number; after retry {index} the list reads {numbers}. A number "
            f"derived from len(attempts) does exactly this once the list is full"
        )
        assert all(later > earlier for earlier, later in pairwise(numbers)), (
            f"attempt numbers must increase strictly along the list; after retry {index} they read {numbers}"
        )
        assert numbers[-1] == index, (
            f"retry {index} must append attempt number {index}; it appended {numbers[-1]} (list {numbers})"
        )
        assert len(attempts) == min(index, _ATTEMPTS_CAP), (
            f"R2.4 / §4.2: after {index} retries the list holds min({index}, {_ATTEMPTS_CAP}) = "
            f"{min(index, _ATTEMPTS_CAP)} nodes; it holds {len(attempts)} ({numbers})"
        )
        assert values[":attempt_count"] == index + 1, (
            f"attempt_count is written as displaced + 1, so after retry {index} it must read "
            f"{index + 1}; it reads {values[':attempt_count']!r}"
        )

        appended_nodes.append(copy.deepcopy(attempts[-1]))
        attempt_counts.append(values[":attempt_count"])

    # --- Across the sequence: the retained nodes are the MOST RECENT ones ---
    every_number_appended = [node["attempt"] for node in appended_nodes]
    assert every_number_appended == list(range(1, retries + 1)), (
        f"across {retries} successive retries the numbers appended must be 1..{retries} with no "
        f"repeat and no gap; they were {every_number_appended}"
    )

    final_attempts = updates[-1]["ExpressionAttributeValues"][":attempts"]
    final_numbers = _attempt_numbers(final_attempts)
    retained = min(retries, _ATTEMPTS_CAP)
    expected_tail = every_number_appended[-retained:]

    # The clause a length assertion cannot see. An oldest-five cap (`[:5]`)
    # keeps the right NUMBER of nodes and the wrong nodes.
    assert final_numbers == expected_tail, (
        f"§4.2 keeps the newest {_ATTEMPTS_CAP} and drops the oldest, so after {retries} retries "
        f"the retained numbers must be {expected_tail} — the tail of everything appended "
        f"({every_number_appended}). They are {final_numbers}. A cap keeping the OLDEST five "
        f"satisfies the length and uniqueness clauses and fails only here"
    )
    assert final_attempts == appended_nodes[-retained:], (
        f"the retained nodes must be the most recent nodes themselves, not merely nodes carrying "
        f"the most recent numbers; retained {final_attempts!r} against the last {retained} "
        f"appended {appended_nodes[-retained:]!r}"
    )
    if retries > _ATTEMPTS_CAP:
        dropped = every_number_appended[:-retained]
        assert dropped and set(dropped).isdisjoint(final_numbers), (
            f"with {retries} retries the oldest {len(dropped)} node(s) {dropped} must have been "
            f"dropped, yet some survive in {final_numbers}"
        )

    # The fold is what made those retries successive, so the row it produced has
    # to agree with the last write — otherwise the sequence above was eight
    # independent first retries.
    assert _attempt_numbers(final_row.get("attempts")) == final_numbers
    assert final_row.get("attempt_count") == retries + 1

    # --- attempt_count keeps increasing PAST the cap -----------------------
    assert all(later > earlier for earlier, later in pairwise(attempt_counts)), (
        f"attempt_count must increase on every retry, capped list or not; it read {attempt_counts}"
    )
    assert attempt_counts[-1] == retries + 1

    if retries > _ATTEMPTS_CAP:
        # The implementation fact this property exists to pin: above the cap the
        # numbering is no longer derivable from the list at all. `len(attempts)`
        # is stuck at five, so a length-derived counter would be stuck at six.
        assert attempt_counts[-1] > _ATTEMPTS_CAP + 1, (
            f"after {retries} retries attempt_count must have carried past the cap to "
            f"{retries + 1}; it reads {attempt_counts[-1]}, which is what a number derived from "
            f"len(attempts) would produce"
        )
        assert attempt_counts[-1] != len(final_attempts) + 1, (
            f"attempt_count ({attempt_counts[-1]}) must not be derivable from the capped list "
            f"length ({len(final_attempts)}) — R2.4's per-attempt attribution is what breaks when "
            f"it is"
        )
        assert max(final_numbers) == retries > _ATTEMPTS_CAP, (
            f"the newest retained node must be numbered {retries}, past the cap of "
            f"{_ATTEMPTS_CAP}; it is numbered {max(final_numbers)}"
        )


def test_the_fold_is_what_makes_successive_retries_successive() -> None:
    """Property 5 is not vacuous: without the fold, every retry is the first.

    The property's whole content is what happens *across* retries, and the one
    edit that would empty it while leaving every assertion syntactically intact
    is dropping :func:`_fold_update_onto_row` — the mocked pre-flight read would
    then hand each call the same pristine row, every call would append
    ``attempt 1`` to a one-element list, and ``len(attempts) == min(index, 5)``
    would fail at index 2 rather than silently. This test pins that the harness
    difference is real, in both directions, so a future simplification of the
    fold fails here with a reason rather than inside a Hypothesis shrink.

    **Validates: Requirements 2.4**
    """
    run = FailedRun(
        run_id="run-2026-08-09-15-38-19-5c5f5f8a",
        pipeline_id="newtest-ab2a08a0",
        state_rows=[
            _state_row(
                run_id="run-2026-08-09-15-38-19-5c5f5f8a",
                pipeline_id="newtest-ab2a08a0",
                step_name="test_code",
                status="failed",
                sequence=1,
                timestamp="2026-08-09T15:38:19+00:00",
            )
        ],
        failed_step_name="test_code",
        pipeline_record={
            "pipeline_id": "newtest-ab2a08a0",
            "source_type": "S3_UPLOAD",
            "source_bucket": "adi-source-files-123456789012-us-west-2",
        },
        executions_row={},
    )
    run = run._replace(
        executions_row=_executions_row(run_id=run.run_id, pipeline_id=run.pipeline_id, state_rows=run.state_rows)
    )

    def numbers_after(retries: int, *, fold: bool) -> list[list[int]]:
        row = copy.deepcopy(run.executions_row)
        with retry_run_environment(run, executions_rows=[row]) as mocks:
            if fold:
                mocks.executions_table.update_item.side_effect = _fold_update_onto_row(row)
            for _ in range(retries):
                assert parse(runs.retry_run(run.run_id))[0] == 200
            return [
                _attempt_numbers(call[1]["ExpressionAttributeValues"][":attempts"])
                for call in mocks.executions_table.update_item.call_args_list
            ]

    # Unfolded: eight independent first attempts, which is the vacuous world.
    assert numbers_after(8, fold=False) == [[1]] * 8, (
        "without the fold every retry reads the same pristine row, so the cap is never reached "
        "and the numbering never advances — the world Property 5 must not be run in"
    )

    # Folded: the sequence task 3.7 verified by execution, ending [4, 5, 6, 7, 8].
    folded = numbers_after(8, fold=True)
    assert folded[-1] == [4, 5, 6, 7, 8], (
        f"with the fold, eight successive retries must retain the newest five, [4, 5, 6, 7, 8] "
        f"(task 3.7 verified this exact case by execution); got {folded[-1]}"
    )
    assert [numbers[-1] for numbers in folded] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [len(numbers) for numbers in folded] == [1, 2, 3, 4, 5, 5, 5, 5]
