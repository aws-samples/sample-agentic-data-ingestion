# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 7 — the phantom predicate admits exactly the phantom shape (p38 task 2.2).

Feature: p38-retry-run-identity, Property 7: The phantom predicate admits exactly the phantom shape

*For any* executions row produced by any known writer shape — ``execution_updater``-touched,
a duplicate-content skip, a contention skip, or the retry phantom —
``is_phantom_execution_row`` is true exactly for the phantom shape, and the filtered
``get_pipeline_executions`` response excludes exactly those rows.

**Validates: Requirements 3.2, 3.5**

WHY THE GENERATOR IS KEYED ON WRITER SHAPES AND NOT ON FIELD COMBINATIONS
------------------------------------------------------------------------
``is_phantom_execution_row`` reads two fields, so a generator over
``{current_step present/absent} x {steps_status present/absent}`` would be four cases and
would look thorough. It would also be **useless**, because it would pass against a predicate
keyed on ``steps_status`` alone — and that predicate hides both skip records, which carry
``current_step`` and no ``steps_status``.

The two skip records are shaped the way they are on purpose. ``LGR-21a1b5b-4``
(**template / ratified**, confirmed resolvable at full fidelity while writing this file)
rejected a ``skipped-`` prefix for the contention record because "it sorts above ``run-``
under the run-history descending sort with a page limit of ten", and chose the real run-id
shape so skips "interleave chronologically" with real runs. A predicate that dropped those
rows from the run list would undo that decision at the reader instead of at the writer, which
is worse: the row would still be written and would still be invisible.

So the unit of generation here is **one row per writer**, taken from that writer's own code,
with the expected verdict attached to the writer rather than to a field pattern. Adding a
newly discovered writer means adding one entry to ``WRITER_SHAPES`` — which is also how design
§5 delta D3 closes if the QUEUED placeholder's writer is ever located.

THE PHANTOM'S WRITER NO LONGER EXISTS, AND THAT IS THE REASON FOR THIS TEST
--------------------------------------------------------------------------
The ``put_item`` that minted the phantom (``pipelines_runs.py``, formerly ``:1345-1356``) was
**deleted by task 3.1** — ``pipelines_runs.py:1628`` now carries a comment where it stood. Its
shape therefore cannot be read from live code and is pinned here from the defect report in
``requirements.md`` (§Introduction defect 2 and the Glossary's Phantom_Execution_Row /
Reported_Run_Id entries): ``run_id``, ``pipeline_id``, ``status`` (``RUNNING``), ``trigger``,
``retry_source_run_id``, ``execution_name``, ``started_at`` and ``updated_at``, with
``started_at == updated_at`` and with **neither** ``current_step`` **nor** ``steps_status``.

Seven rows in that shape exist live in account ``777788889999``, and design §4.5 chose to
exclude them at the readers rather than delete them, precisely so any phantom predating the
fix stays harmless. That is what makes a pinned shape load-bearing rather than historical: the
producer is gone, the rows are not.

WHY THE ``steps_status == {}`` CASE IS PINNED SEPARATELY
-------------------------------------------------------
``execution_updater``'s first ``update_item`` sets ``#steps_status = if_not_exists(...,
:empty_map)`` (``execution_updater/lambda_function.py:238-253``, issued at ``:340-345``) and
only its **second** ``update_item`` (``:350-358``) writes the nested per-step entry. Between
the two, the row carries ``steps_status == {}``, which is falsy. That row is still correctly
**not** a phantom, because ``current_step`` is set — and it is the case that shows the
conjunction is the right shape rather than an accident that happens to hold. It is a first-class
registry entry (``execution_updater_first_update``), not an afterthought.

FIXTURES AND MOCKING
--------------------
Shared fixtures only: ``mock_dynamodb_tables`` and ``mock_lambda_env`` from
``tests/fixtures/conftest.py``. Nothing here builds a moto backend or a boto3 client stand-in
of its own (``tests/unit/test_no_duplicate_mocking_prop.py``). ``AWS_REGION`` is not assigned
— the unit layer pins it in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``, **template /
ratified**).

``handlers/common.py`` calls ``require_env`` for nine table names at **import** time and
``require_env`` raises unless the variable is set (``shared/env_utils.py:16-31``), so every
import of the code under test is lazy and happens after ``mock_lambda_env`` has run. This is
the pattern this directory's ``conftest.py`` already documents for ``handlers.schemas``.
"""

import ast
import json
import pathlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
# ``sys.path`` already carries ``shared/`` and the dashboard API package: this
# directory's conftest.py inserts both at collection start so no test module
# here depends on which file was imported first.
_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_HANDLER_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"
_RUNS_MODULE_PATH = _HANDLER_DIR / "handlers" / "pipelines_runs.py"

#: The two tables the run-list reader touches, as ``tests/fixtures/conftest.py``
#: names them. Asserted against the fixture's own dict in ``executions_reader_env``
#: so a rename there fails loudly here instead of seeding a table nothing reads.
EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
STATE_TABLE = "agentic_data_ingestion_state"

#: The region ``mock_dynamodb_tables`` creates its tables in
#: (``tests/fixtures/conftest.py:211``). Used only to obtain a resource handle for
#: ``batch_get_item`` inside the fixture's moto window; the handler's own
#: module-level handles are replaced by monkeypatch rather than relied upon.
_FIXTURE_REGION = "us-east-1"

PIPELINE_ID = "pipe-p38-phantom-0001"


def _common_handler():
    """Import ``handlers.common`` lazily and return it.

    Deliberately not a module-level import — see the module docstring's note on
    ``require_env`` at import time.
    """
    from handlers import common

    return common


def _runs_handler():
    """Import ``handlers.pipelines_runs`` lazily and return it."""
    from handlers import pipelines_runs

    return pipelines_runs


# ---------------------------------------------------------------------------
# Has task 6.1's reader-side filter landed?
# ---------------------------------------------------------------------------
# Read off disk and inspected with ``ast`` rather than imported, because a
# ``skipif`` condition is evaluated at **collection** time, before any fixture has
# set the environment the handler package needs to import at all.
#
# AST rather than a substring search, for the reason ``LGR-b46a5bb-4``
# (**template / ratified**) gives for the anti-threading meta-test: a substring
# test cannot tell a call from a mention, so it would forbid the file under test
# from *documenting* the filter in a comment. The check asks specifically whether
# the read path reachable from ``get_pipeline_executions`` references the predicate.
#
# **AMENDED BY p48 TASK 4.3, and the amendment is the gate's own stated worry coming
# true.** It used to inspect ``get_pipeline_executions``' body *only*. p48's status
# filter had to run per index page, so the QUEUED split, the phantom filter, the
# ``WAITING_APPROVAL`` derivation and the new filter moved together into a helper —
# ``_split_derive_and_filter`` — which ``get_pipeline_executions`` calls. The filter is
# therefore still applied on exactly the same list in exactly the same position, but the
# one-function check stopped seeing it and **silently parked this file's reader half in
# "skipped"** for a refactor that changed no behaviour. That is the failure
# :func:`_reader_filter_state`'s own docstring warns about, so the gate now follows one
# level of delegation instead of pinning itself to a single function name.
#
# One level, not the whole call graph, deliberately: it is enough to see a helper the
# reader delegates to, and a full transitive walk would make the gate answer "somewhere
# in this module" — which is a different and much weaker question.


def _reader_filter_state() -> tuple[bool, bool]:
    """``(the function was found, the read path references the predicate)``.

    The first element exists so the skip below cannot become permanent by
    accident: ``test_the_reader_half_gate_is_looking_at_a_real_function`` asserts
    it, so a rename or a move makes that test fail rather than silently parking
    the reader half in "skipped" forever.

    The second is answered over ``get_pipeline_executions`` **plus every module-level
    function it names**, because since p48 task 4.3 the filter lives in one of those
    (``_split_derive_and_filter``) rather than inline. See the note above.
    """
    tree = ast.parse(_RUNS_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_RUNS_MODULE_PATH))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    reader = functions.get("get_pipeline_executions")
    if reader is None:
        return False, False
    names = {n.id for n in ast.walk(reader) if isinstance(n, ast.Name)}
    for delegate in names & set(functions):
        names |= {n.id for n in ast.walk(functions[delegate]) if isinstance(n, ast.Name)}
    return True, "is_phantom_execution_row" in names


_READER_FUNCTION_FOUND, _READER_FILTER_HAS_LANDED = _reader_filter_state()

_WAITING_ON_6_1 = (
    "p38 task 6.1 has not landed: get_pipeline_executions does not yet apply "
    "is_phantom_execution_row, so the reader half of Property 7 has nothing to assert against. "
    "This test becomes live automatically when 6.1 adds the filter — no edit to this file is "
    "needed, and it must pass on the first run after 6.1."
)


# ---------------------------------------------------------------------------
# Timestamp and identifier helpers
# ---------------------------------------------------------------------------
_EPOCH_SECONDS = st.integers(min_value=1_700_000_000, max_value=1_900_000_000)
_HEX8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
_HEX32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)


def _iso(epoch: int) -> str:
    """An ISO-8601 UTC stamp, the form every writer below stores."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _run_stamp(epoch: int) -> str:
    """``YYYY-MM-DD-HH-MM-SS`` — ``file_validation.generate_run_id:104``'s format."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d-%H-%M-%S")


#: ``run-{YYYY-MM-DD-HH-MM-SS}-{uuid8}`` — ``generate_run_id``
#: (``file_validation/lambda_function.py:102-108``).
_REAL_RUN_IDS = st.builds(lambda e, u: f"run-{_run_stamp(e)}-{u}", _EPOCH_SECONDS, _HEX8)

#: ``skipped-{fingerprint[:32]}`` — ``_duplicate_skip_record_id``
#: (``pipeline_trigger/lambda_function.py:179-189``). Kept in the generator even
#: though its prefix is the one ``LGR-21a1b5b-4`` rejected for the *contention*
#: record: this row exists in that shape and must survive the predicate.
_DUPLICATE_SKIP_IDS = _HEX32.map(lambda f: f"skipped-{f}")

#: ``global_concurrency`` / ``pipeline_lock_contention`` — ``pipeline_trigger``
#: ``SKIP_REASON_CONCURRENCY`` (``:322``) and ``SKIP_REASON_LOCK_CONTENTION`` (``:325``).
_CONTENTION_REASONS = ("global_concurrency", "pipeline_lock_contention")

#: ``run-{YYYY-MM-DD-HH-MM-SS}-skip-{reason}`` — ``_contention_skip_record_id``
#: (``pipeline_trigger/lambda_function.py:341-...``), deliberately shaped like a real
#: run id (``LGR-21a1b5b-4``).
_CONTENTION_SKIP_IDS = st.builds(
    lambda e, r: f"run-{_run_stamp(e)}-skip-{r}",
    _EPOCH_SECONDS,
    st.sampled_from(_CONTENTION_REASONS),
)

#: ``retry-{run_id[:40]}-{ts_hash}`` — the **deleted** producer's shape, pinned from
#: ``requirements.md`` rather than from code (see the module docstring). The observed
#: live id ``retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303`` is of this form.
_RETRY_PHANTOM_IDS = st.builds(lambda base, h: f"retry-{base[:40]}-{h}", _REAL_RUN_IDS, _HEX8)

#: The step names ``execution_updater`` writes into ``current_step`` and as
#: ``steps_status`` keys. Both generations are generated: new rows carry canonical
#: snake_case names, rows written before the P28 R8.2 rename persist with the legacy
#: PascalCase variants (``pipelines_runs.py:51-62``).
_STEP_NAMES = st.sampled_from(
    (
        "file_validation",
        "schema_extract",
        "create_code",
        "test_code",
        "run_pipeline",
        "CodeGeneration",
        "RunPipeline",
    )
)

#: ``_STATUS_MAP``'s range plus the derived values
#: (``execution_updater/lambda_function.py:128-139``, ``:612-621``).
_EXECUTION_STATUSES = st.sampled_from(("RUNNING", "SUCCEEDED", "FAILED", "WAITING_APPROVAL"))


# ---------------------------------------------------------------------------
# The writer shapes
# ---------------------------------------------------------------------------
@st.composite
def _execution_updater_first_update(draw: Any, run_id: str) -> dict[str, Any]:
    """A row after ``execution_updater``'s FIRST ``update_item`` only.

    ``execution_updater/lambda_function.py:238-253`` builds
    ``#status``, ``current_step``, ``updated_at`` and
    ``#steps_status = if_not_exists(#steps_status, :empty_map)``, and issues it at
    ``:340-345``. The per-step entry arrives on a second ``update_item`` at
    ``:350-358``, so ``steps_status`` is ``{}`` — **falsy** — in between.

    Not a phantom: ``current_step`` is set. This is the case the conjunction earns
    its keep on.

    ``started_at`` is unconditional — see the comment at the assignment for why,
    and why it used to be an optional draw.
    """
    epoch = draw(_EPOCH_SECONDS)
    row: dict[str, Any] = {
        "run_id": run_id,
        "status": draw(_EXECUTION_STATUSES),
        "current_step": draw(_STEP_NAMES),
        "updated_at": _iso(epoch),
        "steps_status": {},
    }
    # ``started_at`` is set by EVERY step update, not only the first-step arm —
    # ``execution_updater/lambda_function.py:288`` appends
    # ``started_at = if_not_exists(started_at, :started_at)`` to
    # ``update_parts`` unconditionally, before the
    # ``step_name == "file_validation" and eventName == "INSERT"`` branch below
    # it. So this shape ALWAYS carries the attribute.
    #
    # AMENDED BY p48 task 2.2 (was ``if draw(st.booleans())``, setting it on
    # roughly half of examples). The optional draw modelled the writer as it was
    # BEFORE p48 task 5.4 hoisted that clause out of the ``file_validation``
    # INSERT branch, and it went stale the moment 5.4 landed. It surfaced at 2.2
    # rather than at 5.4 because a row with no ``started_at`` renders fine while
    # the read is on the base table: task 2.2 moved the read to
    # ``RunsByTimeIndex``, which is SPARSE, so such a row is absent from the
    # index and cannot be returned at all. Both run-list properties below then
    # failed on ``real rows that were dropped``.
    #
    # This is a generator correction, not a weakened assertion. The shape must
    # match what the writer it names actually writes; the p48 R9.1 exposure of
    # rows that genuinely lack ``started_at`` is asserted in
    # ``tests/unit/lambdas/test_executions_index_sparseness.py``, and the
    # writer-side guarantee in ``test_executions_started_at_invariant.py``.
    # Do NOT restore the optional draw as a way of testing sparseness here —
    # p48 do-NOT 2 / R9.4: index membership is not the phantom predicate, and
    # this file's subject is the predicate.
    row["started_at"] = _iso(epoch)
    # file_name IS first-step-arm only (:253-303), along with trigger,
    # original_run_id and ai_model_used.
    if draw(st.booleans()):
        row["file_name"] = draw(st.sampled_from(("customers.csv", "orders.json", "")))
    return row


@st.composite
def _execution_updater_touched(draw: Any, run_id: str) -> dict[str, Any]:
    """A row both of ``execution_updater``'s updates have reached.

    The nested ``steps_status`` entry is written by the second ``update_item``
    (``:350-358``). The optional scalars are the ones the first update sets as steps
    report — ``execution_id`` (``:305-308``), ``ai_model_used`` (``:310-313``),
    ``schema_version`` (``:315-325``), ``schema_hash`` (``:327-333``) and
    ``completed_at`` on a terminal final step (``:335-337``).

    Not a phantom.
    """
    epoch = draw(_EPOCH_SECONDS)
    steps = draw(st.lists(_STEP_NAMES, min_size=1, max_size=4, unique=True))
    current = steps[-1]
    row: dict[str, Any] = {
        "run_id": run_id,
        "status": draw(_EXECUTION_STATUSES),
        "current_step": current,
        "started_at": _iso(epoch),
        "updated_at": _iso(epoch + draw(st.integers(min_value=1, max_value=600))),
        "steps_status": {step: draw(_EXECUTION_STATUSES) for step in steps},
    }
    if draw(st.booleans()):
        row["file_name"] = "customers.csv"
        row["ai_model_used"] = "anthropic.claude-3-haiku-20240307-v1:0"
        row["schema_hash"] = draw(_HEX32)
        row["execution_id"] = f"arn:aws:states:us-east-1:123456789012:execution:p:{draw(_HEX8)}"
    if draw(st.booleans()):
        row["completed_at"] = _iso(epoch + 900)
    return row


@st.composite
def _duplicate_content_skip(draw: Any, run_id: str) -> dict[str, Any]:
    """The duplicate-content Skipped_Run_Record.

    ``pipeline_trigger/lambda_function.py:226-246``, put at ``:227``. Carries
    ``current_step="duplicate_check"`` and **no** ``steps_status``.

    Not a phantom, and this is the row the two-field conjunction exists to protect.
    """
    epoch = draw(_EPOCH_SECONDS)
    fingerprint = draw(_HEX32)
    return {
        "run_id": run_id,
        "status": "SKIPPED",
        "skip_reason": "duplicate_content",
        "skip_detail": (
            "Identical content was already processed successfully by run "
            f"'{draw(_REAL_RUN_IDS)}'. Use Run Now with the duplicate "
            "acknowledgement to process it again."
        ),
        "file_name": "customers.csv",
        "source_key": "uploads/customers.csv",
        "content_fingerprint": fingerprint,
        "previous_run_id": draw(_REAL_RUN_IDS),
        "triggered_by": draw(st.sampled_from(("s3_upload", "scheduled", "manual"))),
        "started_at": _iso(epoch),
        "current_step": "duplicate_check",
        "ttl": epoch + 604_800,
    }


@st.composite
def _contention_skip(draw: Any, run_id: str) -> dict[str, Any]:
    """The scheduled-contention Skipped_Run_Record.

    ``pipeline_trigger/lambda_function.py:487-501``, put at ``:511``. Carries
    ``current_step`` of ``"concurrency_gate"`` or ``"pipeline_lock_gate"`` and **no**
    ``steps_status``. The optional ``source_key`` / ``file_name`` /
    ``running_count`` fields are the conditional arms at ``:503-509``.

    Not a phantom. ``LGR-21a1b5b-4`` shaped this row's id to interleave with real
    runs; a predicate that hid it would undo that.
    """
    epoch = draw(_EPOCH_SECONDS)
    reason = draw(st.sampled_from(_CONTENTION_REASONS))
    row: dict[str, Any] = {
        "run_id": run_id,
        "status": "SKIPPED",
        "skip_reason": reason,
        "skip_detail": "A run was already in progress for this pipeline.",
        "triggered_by": "scheduled",
        "started_at": _iso(epoch),
        "last_skipped_at": _iso(epoch + draw(st.integers(min_value=0, max_value=290))),
        "skip_count": draw(st.integers(min_value=1, max_value=12)),
        "skip_window_seconds": 300,
        "current_step": ("concurrency_gate" if reason == "global_concurrency" else "pipeline_lock_gate"),
        "ttl": epoch + 604_800,
    }
    if draw(st.booleans()):
        row["source_key"] = "uploads/orders.json"
        row["file_name"] = "orders.json"
    if draw(st.booleans()):
        row["running_count"] = draw(st.integers(min_value=0, max_value=10))
        row["running_count_is_floor"] = draw(st.booleans())
        row["running_max"] = 10
    return row


@st.composite
def _retry_phantom(draw: Any, run_id: str) -> dict[str, Any]:
    """The retry phantom — the ONE shape the predicate must admit.

    Pinned from ``requirements.md`` (§Introduction defect 2, Glossary), not from
    code: task 3.1 deleted the ``put_item`` that produced it. Eight attributes, with
    ``started_at == updated_at`` and neither ``current_step`` nor ``steps_status``.

    ``status`` is generated rather than fixed at ``RUNNING``: ``RUNNING`` is what the
    seven live rows carry and what makes them permanent in the ``StatusIndex``
    partition, but the predicate is structural and must not depend on the status —
    a phantom is a phantom because no step ever reported, not because of what it
    claims.
    """
    epoch = draw(_EPOCH_SECONDS)
    stamp = _iso(epoch)
    source_run_id = draw(_REAL_RUN_IDS)
    return {
        "run_id": run_id,
        "status": draw(st.sampled_from(("RUNNING", "SUCCEEDED", "FAILED"))),
        "trigger": "retry",
        "retry_source_run_id": source_run_id,
        "execution_name": f"retry-{source_run_id}-{draw(_HEX8)}",
        "started_at": stamp,
        "updated_at": stamp,
    }


class WriterShape(NamedTuple):
    """One executions-table writer, its id shape, and the predicate's verdict on it.

    ``phantom`` is the **expected** answer and it is attached to the writer, not
    derived from the row's fields — which is what makes this a test of the predicate
    rather than a restatement of it.
    """

    name: str
    phantom: bool
    run_ids: st.SearchStrategy[str]
    row: Callable[[str], st.SearchStrategy[dict[str, Any]]]


#: Every writer of an executions-table row located by the design sweep (§4.5's table).
#:
#: **Add a writer by adding an entry.** That is the extension point design §5 delta
#: D3 names: the QUEUED placeholder's writer was never found, and closing D3 means
#: appending its shape here with ``phantom=False``-or-``True`` decided from the
#: writer, not from the fields. It is deliberately **absent** today — see
#: ``_queued_placeholder_row`` for how the reader half covers it in the meantime.
WRITER_SHAPES: tuple[WriterShape, ...] = (
    WriterShape(
        name="execution_updater_first_update",
        phantom=False,
        run_ids=_REAL_RUN_IDS,
        row=_execution_updater_first_update,
    ),
    WriterShape(
        name="execution_updater_touched",
        phantom=False,
        run_ids=_REAL_RUN_IDS,
        row=_execution_updater_touched,
    ),
    WriterShape(
        name="duplicate_content_skip",
        phantom=False,
        run_ids=_DUPLICATE_SKIP_IDS,
        row=_duplicate_content_skip,
    ),
    WriterShape(
        name="contention_skip",
        phantom=False,
        run_ids=_CONTENTION_SKIP_IDS,
        row=_contention_skip,
    ),
    WriterShape(
        name="retry_phantom",
        phantom=True,
        run_ids=_RETRY_PHANTOM_IDS,
        row=_retry_phantom,
    ),
)


class ShapedRow(NamedTuple):
    """A generated row, the writer it came from, and the expected verdict."""

    shape: str
    phantom: bool
    row: dict[str, Any]


@st.composite
def shaped_row(draw: Any) -> ShapedRow:
    """One row from one writer shape, drawn uniformly over the registry."""
    shape = draw(st.sampled_from(WRITER_SHAPES))
    run_id = draw(shape.run_ids)
    return ShapedRow(shape=shape.name, phantom=shape.phantom, row=draw(shape.row(run_id)))


@st.composite
def shaped_rows(draw: Any, min_size: int = 1, max_size: int = 6) -> tuple[ShapedRow, ...]:
    """A run history: several rows, each from some writer shape.

    Capped at six because ``get_pipeline_executions`` passes ``Limit=page_size``
    (default 10) to its query (``pipelines_runs.py:227-231``), so a longer history
    would lose rows to paging and the reader-half assertions would be about the page
    rather than about the filter.

    Deduplicated on ``run_id``: the contention shape's id space is
    ``(reason, window)`` and is small enough for Hypothesis to shrink two draws onto
    one id, which would collapse two seeded rows into one row in DynamoDB and make a
    set comparison fail for a reason that has nothing to do with the predicate.
    """
    drawn = draw(st.lists(shaped_row(), min_size=min_size, max_size=max_size))
    by_run_id: dict[str, ShapedRow] = {}
    for shaped in drawn:
        by_run_id.setdefault(shaped.row["run_id"], shaped)
    return tuple(by_run_id.values())


# ---------------------------------------------------------------------------
# The predicate half — independently green, no reader involved
# ---------------------------------------------------------------------------
@pytest.fixture
def phantom_predicate(mock_lambda_env):
    """The real ``is_phantom_execution_row``, imported after the env is in place.

    Imported rather than replicated. ``requirements.md`` §Code-intelligence records
    the failure mode this avoids: ``use-run-detail.property.test.ts`` declares a local
    copy of the logic it means to test and therefore covers nothing.
    """
    return _common_handler().is_phantom_execution_row


@given(shaped=shaped_row())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_the_predicate_is_true_exactly_for_the_phantom_shape(shaped, phantom_predicate):
    """Feature: p38-retry-run-identity, Property 7: The phantom predicate admits exactly the phantom shape

    The predicate half: for a row from any known writer shape, the verdict is the
    one that writer's shape earns — true for the retry phantom, false for every
    writer that records state, including both skip records.

    **Validates: Requirements 3.2, 3.5**
    """
    verdict = phantom_predicate(shaped.row)

    assert verdict is shaped.phantom, (
        f"is_phantom_execution_row returned {verdict!r} for a row written by "
        f"{shaped.shape!r}, expected {shaped.phantom!r}. "
        f"current_step={shaped.row.get('current_step')!r} "
        f"steps_status={shaped.row.get('steps_status')!r} "
        f"run_id={shaped.row['run_id']!r}"
    )


class TestTheGeneratorItself:
    """Guards on the generator, not further properties.

    A shape-keyed generator is only as good as its registry, and both failure
    modes here are silent: a shape that is never drawn tests nothing, and a shape
    added without a considered verdict would be asserted against whatever the
    predicate already does.
    """

    def test_the_generator_reaches_every_writer_shape(self):
        """Every registered shape is produced, and exactly one is the phantom."""
        seen: set[str] = set()

        @given(shaped=shaped_row())
        @settings(max_examples=200, deadline=None)
        def collect(shaped):
            seen.add(shaped.shape)

        collect()

        registered = {shape.name for shape in WRITER_SHAPES}
        assert seen == registered, f"shapes never generated: {sorted(registered - seen)}"

        phantom_shapes = {shape.name for shape in WRITER_SHAPES if shape.phantom}
        assert phantom_shapes == {"retry_phantom"}, (
            "Property 7 says the predicate admits EXACTLY the phantom shape. A second "
            f"shape marked phantom={{True}} changes the property: {sorted(phantom_shapes)}"
        )

    def test_the_reader_half_gate_is_looking_at_a_real_function(self):
        """The task-6.1 gate must be able to find ``get_pipeline_executions``.

        Without this, a rename or a move would make ``_reader_filter_state`` return
        ``(False, False)`` and park the reader half in "skipped" permanently — a
        gate that can never open is worse than no gate.
        """
        assert _READER_FUNCTION_FOUND, (
            f"get_pipeline_executions was not found in {_RUNS_MODULE_PATH}. The reader "
            "half of Property 7 is gated on whether that function applies the phantom "
            "predicate, and the gate can no longer see the function."
        )


# ---------------------------------------------------------------------------
# The reader half — get_pipeline_executions
# ---------------------------------------------------------------------------
_QUEUED_FILE_NAME = "p38-queued-placeholder.csv"


def _queued_placeholder_row(epoch: int) -> dict[str, Any]:
    """A QUEUED placeholder: matches the predicate, and must NOT be excluded.

    Design §5 delta D3: the writer of these rows was never located, so this is
    **not** a registry shape — it has no verified writer to key an expectation on.
    It is seeded by the reader half anyway, because it is the row that tells the
    two possible filter placements apart. ``get_pipeline_executions`` splits
    ``status == "QUEUED"`` rows into ``queued_files`` before task 6.1's filter runs
    (``pipelines_runs.py:236-247``); a filter applied to ``all_items`` instead would
    delete these rows from the response, which is excluding something other than the
    phantoms and so fails Property 7's word "exactly".
    """
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": f"queued-{_run_stamp(epoch)}",
        "status": "QUEUED",
        "file_name": _QUEUED_FILE_NAME,
        "source_key": f"uploads/{_QUEUED_FILE_NAME}",
        "started_at": _iso(epoch),
    }


@pytest.fixture
def executions_reader_env(mock_dynamodb_tables, mock_lambda_env, monkeypatch):
    """``get_pipeline_executions`` bound to the shared moto tables.

    The three module-level handles are replaced rather than trusted: they are
    created at ``handlers/common.py`` import time, which may precede this fixture's
    moto window, so a call through them could leave the mocked backend entirely.

    Returns the executions table to seed.
    """
    assert EXECUTIONS_TABLE in mock_dynamodb_tables and STATE_TABLE in mock_dynamodb_tables, (
        f"{EXECUTIONS_TABLE!r} and {STATE_TABLE!r} must both be among the tables "
        f"mock_dynamodb_tables created ({sorted(mock_dynamodb_tables)}) — "
        "TABLE_DEFINITIONS in tests/fixtures/conftest.py has been renamed and this "
        "fixture would seed a table nothing reads"
    )
    runs = _runs_handler()
    monkeypatch.setattr(runs, "executions_table", mock_dynamodb_tables[EXECUTIONS_TABLE])
    monkeypatch.setattr(runs, "state_table", mock_dynamodb_tables[STATE_TABLE])
    monkeypatch.setattr(runs, "dynamodb", boto3.resource("dynamodb", region_name=_FIXTURE_REGION))
    return mock_dynamodb_tables[EXECUTIONS_TABLE]


def _wipe(table) -> None:
    """Empty the executions table.

    Hypothesis reuses one fixture across every example, so without this a row from
    an earlier example survives into the next and an assertion passes — or fails —
    for a history the property was not given.
    """
    for item in table.scan(ProjectionExpression="pipeline_id, run_id").get("Items", []):
        table.delete_item(Key={"pipeline_id": item["pipeline_id"], "run_id": item["run_id"]})


def _seed(table, shaped_history: tuple[ShapedRow, ...]) -> dict[str, Any]:
    """Wipe, write the history plus one QUEUED placeholder, return the run list body."""
    _wipe(table)
    for shaped in shaped_history:
        table.put_item(Item={**shaped.row, "pipeline_id": PIPELINE_ID})
    table.put_item(Item=_queued_placeholder_row(1_800_000_000))

    response = _runs_handler().get_pipeline_executions(PIPELINE_ID)
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def _returned_run_ids(body: dict[str, Any]) -> set[str]:
    return {execution["run_id"] for execution in body["executions"]}


@given(history=shaped_rows())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_the_run_list_keeps_every_row_a_writer_touched(history, executions_reader_env):
    """Feature: p38-retry-run-identity, Property 7: The phantom predicate admits exactly the phantom shape

    The half of "excludes exactly those rows" that says **nothing else** is
    excluded, and the half that protects the two skip records at the reader. True
    both before and after task 6.1, so it runs unconditionally — it is the arm that
    would catch a filter keyed on ``steps_status`` alone, or applied ahead of the
    QUEUED split.

    **Validates: Requirements 3.2, 3.5**
    """
    body = _seed(executions_reader_env, history)
    returned = _returned_run_ids(body)

    survivors = {shaped.row["run_id"] for shaped in history if not shaped.phantom}
    assert survivors <= returned, (
        "rows written by a state-recording writer were dropped from the run list: "
        f"{sorted(survivors - returned)} "
        f"(shapes: {sorted({s.shape for s in history if s.row['run_id'] not in returned})})"
    )

    seeded = {shaped.row["run_id"] for shaped in history}
    assert returned <= seeded, f"the run list carries run ids nothing seeded: {sorted(returned - seeded)}"

    queued_names = [queued["file_name"] for queued in body["queued_files"]]
    assert queued_names == [_QUEUED_FILE_NAME], (
        "the QUEUED placeholder did not reach queued_files. It carries neither "
        "current_step nor steps_status, so a phantom filter applied before the QUEUED "
        f"split would delete it (design §5 delta D3). Got: {queued_names}"
    )


@pytest.mark.skipif(not _READER_FILTER_HAS_LANDED, reason=_WAITING_ON_6_1)
@given(history=shaped_rows())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_the_run_list_excludes_exactly_the_phantom_rows(history, executions_reader_env):
    """Feature: p38-retry-run-identity, Property 7: The phantom predicate admits exactly the phantom shape

    The half of "excludes exactly those rows" that says every phantom **is**
    excluded. Gated on task 6.1: until ``get_pipeline_executions`` applies the
    predicate there is no filter to assert against, and the gate opens by itself
    when it does.

    **Validates: Requirements 3.2, 3.5**
    """
    body = _seed(executions_reader_env, history)
    returned = _returned_run_ids(body)

    survivors = {shaped.row["run_id"] for shaped in history if not shaped.phantom}
    phantoms = {shaped.row["run_id"] for shaped in history if shaped.phantom}

    assert returned == survivors, (
        "the run list is not exactly the rows a writer touched. "
        f"phantoms that survived: {sorted(returned & phantoms)}; "
        f"real rows that were dropped: {sorted(survivors - returned)}"
    )

    queued_names = [queued["file_name"] for queued in body["queued_files"]]
    assert queued_names == [_QUEUED_FILE_NAME], (
        f"the QUEUED placeholder was filtered out of queued_files (design §5 delta D3): {queued_names}"
    )
