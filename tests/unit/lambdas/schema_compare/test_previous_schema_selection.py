# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``get_previous_schema`` selection path (p30 task 4.0).

``get_previous_schema`` had **no covering tests** before this file, and p30 task
4.5 rewrites its selection logic, so this is the safety net that must exist
first (p30 design §5 O10).

**Two kinds of test live here.**

1. **Pinned** — current behaviour that is *correct* under Requirement 8: the
   first-run case, the query's server-side filter, pipeline scoping, and the
   three-version case R8.5 names (when the current run is the newest row).
2. **Written to the requirement** — the two halves of **R8.3** the code did not
   satisfy: selection relative to the current run's own row rather than to
   ``items[0]``, and pagination of the GSI query. These were marked
   ``xfail(strict=True)`` when 4.0 wrote them, so that they became a hard suite
   failure the moment task 4.5 made them pass — which was 4.5's acceptance
   signal. **Task 4.5 has landed and the markers are gone; the tests stay.**

**The two defects, precisely (R8.3).**

*Selection.* The function sorts every matching row by ``timestamp`` descending
and returns ``items[1]`` — "the second most recent". That is only the previous
schema **if the current run's row is items[0]**. When another run's
``schema_extract`` row is newer (a concurrently started run, a retry, or clock
skew), ``items[1]`` is the current run's **own row**, so the current schema is
compared against itself, reports identical, and the remap is skipped.

*Pagination.* The query has no ``ExclusiveStartKey`` loop. DynamoDB applies
``FilterExpression`` **after** the 1 MB page read, so a page whose rows are all
filtered out comes back with ``Items: []`` and a ``LastEvaluatedKey`` — and this
function treats that as "no previous schema". The state table accumulates a row
per step per run, so the ``schema_extract`` rows for an established pipeline sit
behind pages of other steps' rows.

**Fixtures.** Per the project testing standards, DynamoDB comes from the shared
``mock_dynamodb_tables`` fixture; this file creates no ``moto.mock_aws()`` and no
``boto3`` client of its own. The pagination test drives **real** paging through
that fixture — items sized so the first 1 MB page holds only rows the filter
rejects — rather than scripting a stubbed ``query``, so the filter-after-page
semantics that cause the defect are enforced by the query engine.

**Validates: p30 design §5 O10; R8.3, R8.5 (xfail cases); R16.3**
"""

import importlib.util
import inspect
import json
import os
import sys
import unittest.mock as mock

# Imported at module scope so it is already in sys.modules before the
# patch.dict(sys.modules) block in _load_module() runs. patch.dict evicts
# modules first imported inside it, and a re-imported boto3.dynamodb.conditions
# would carry NEW Key/Attr classes that the moto Table resource's condition
# translation does not recognize — the query would then fail validation.
import boto3.dynamodb.conditions  # noqa: F401
import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_schema_compare",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_p30_prev_schema",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p30_prev_schema"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

PIPELINE_ID = "pipe-prev-schema"
OTHER_PIPELINE_ID = "pipe-unrelated"


@pytest.fixture
def state_table(mock_dynamodb_tables):
    """Point the module's ``table`` global at the shared moto state table."""
    state = mock_dynamodb_tables["agentic_data_ingestion_state"]
    with mock.patch.object(_lf, "table", state):
        yield state


def _seed_step_row(
    state_table,
    *,
    run_id,
    timestamp,
    step_name="schema_extract",
    status="success",
    pipeline_id=PIPELINE_ID,
    schema=None,
    padding=0,
):
    """Seed a state-table row that appears in the PipelineIndex GSI.

    ``pipeline_id`` and ``timestamp`` are the GSI's partition and sort keys, so
    both are required for the row to be visible to the query under test.
    ``padding`` inflates the item so the pagination test can fill a 1 MB page.
    """
    item = {
        "run_id": run_id,
        "step_name": step_name,
        "pipeline_id": pipeline_id,
        "original_pipeline_id": pipeline_id,
        "timestamp": timestamp,
        "status": status,
    }
    if schema is not None:
        item["extracted_schema"] = json.dumps(schema)
        item["schema_hash"] = f"hash-of-{run_id}"
    if padding:
        item["padding"] = "x" * padding
    state_table.put_item(Item=item)


def _schema_with(field_type):
    """A minimal streaming-path emitted schema, as captured in p30 task 0.3."""
    return {"type": "object", "properties": {"id": {"type": field_type}}}


def _call_get_previous_schema(pipeline_id, current_run_id):
    """Invoke ``get_previous_schema``, passing the current run id if it is accepted.

    R8.3 requires selection "by explicit timestamp comparison against the current
    run's own row", which the function can only do if it is told which run is
    current — and today's signature takes ``pipeline_id`` alone. Task 4.5 owns
    the signature, so this adapter deliberately does not prescribe it: the
    assertions stay about behaviour, and the tests keep working whether 4.5
    threads the run id through or not. (``lambda_handler`` already has ``run_id``
    in scope at the single call site, so this is a local change.)
    """
    if len(inspect.signature(_lf.get_previous_schema).parameters) >= 2:
        return _lf.get_previous_schema(pipeline_id, current_run_id)
    return _lf.get_previous_schema(pipeline_id)


# ===========================================================================
# PINNED — current behaviour is correct under R8; task 4.5 must not break it
# ===========================================================================


def test_returns_none_when_the_pipeline_has_no_schema_extract_rows(state_table):
    """A pipeline with rows for other steps only has no previous schema.

    **Validates: p30 design §5 O10**
    """
    _seed_step_row(state_table, run_id="run-1", timestamp="2026-03-01T00:00:00", step_name="file_validation")
    _seed_step_row(state_table, run_id="run-1", timestamp="2026-03-01T00:01:00", step_name="schema_compare")

    assert _call_get_previous_schema(PIPELINE_ID, "run-1") is None


def test_returns_none_on_the_first_run_of_a_pipeline(state_table):
    """One successful ``schema_extract`` row means this is the first schema.

    ``lambda_handler`` depends on this ``None`` to take its
    ``create_new_pipeline`` first-run branch.

    **Validates: p30 design §5 O10**
    """
    _seed_step_row(state_table, run_id="run-1", timestamp="2026-03-01T00:00:00", schema=_schema_with("integer"))

    assert _call_get_previous_schema(PIPELINE_ID, "run-1") is None


def test_failed_and_other_step_rows_are_excluded_by_the_query_filter(state_table):
    """Only ``schema_extract`` rows with ``status == "success"`` are candidates.

    The exclusion happens server-side in the query's ``FilterExpression``, so it
    is enforced here by the query engine rather than mocked away. A *failed*
    extraction — and any other step's row — must not be offered as the previous
    schema, however recent it is.

    **Validates: p30 design §5 O10**
    """
    _seed_step_row(state_table, run_id="run-1", timestamp="2026-03-01T00:00:00", schema=_schema_with("integer"))
    _seed_step_row(state_table, run_id="run-2", timestamp="2026-03-02T00:00:00", schema=_schema_with("integer"))
    # Newer, but failed — must not win.
    _seed_step_row(
        state_table, run_id="run-3", timestamp="2026-03-03T00:00:00", status="failed", schema=_schema_with("string")
    )
    # Newer still, but a different step.
    _seed_step_row(state_table, run_id="run-3", timestamp="2026-03-03T00:01:00", step_name="file_validation")

    previous = _call_get_previous_schema(PIPELINE_ID, "run-2")

    assert previous is not None
    assert previous["run_id"] == "run-1"


def test_returns_the_immediately_previous_of_three_successful_versions(state_table):
    """R8.5's three-version case: with v1, v2 and v3 recorded and v3 current,
    the previous schema is **v2**, not v1.

    **Validates: Requirement 8.5; p30 design §5 O10**
    """
    _seed_step_row(state_table, run_id="run-v1", timestamp="2026-04-01T00:00:00", schema=_schema_with("integer"))
    _seed_step_row(state_table, run_id="run-v2", timestamp="2026-04-02T00:00:00", schema=_schema_with("number"))
    _seed_step_row(state_table, run_id="run-v3", timestamp="2026-04-03T00:00:00", schema=_schema_with("string"))

    previous = _call_get_previous_schema(PIPELINE_ID, "run-v3")

    assert previous is not None
    assert previous["run_id"] == "run-v2"
    assert json.loads(previous["extracted_schema"]) == _schema_with("number")


def test_selection_is_scoped_to_the_requested_pipeline(state_table):
    """Another pipeline's newer schema rows are never selected.

    The GSI partitions on ``pipeline_id``, so this pins the scoping rather than
    testing DynamoDB — a rewrite that widened the key condition or moved to a
    scan would fail here.

    **Validates: p30 design §5 O10**
    """
    _seed_step_row(state_table, run_id="run-v1", timestamp="2026-05-01T00:00:00", schema=_schema_with("integer"))
    _seed_step_row(state_table, run_id="run-v2", timestamp="2026-05-02T00:00:00", schema=_schema_with("number"))
    for i in range(3):
        _seed_step_row(
            state_table,
            run_id=f"other-run-{i}",
            timestamp=f"2026-06-0{i + 1}T00:00:00",
            pipeline_id=OTHER_PIPELINE_ID,
            schema=_schema_with("string"),
        )

    previous = _call_get_previous_schema(PIPELINE_ID, "run-v2")

    assert previous is not None
    assert previous["run_id"] == "run-v1"
    assert previous["pipeline_id"] == PIPELINE_ID


# ===========================================================================
# WRITTEN TO THE REQUIREMENT — xfail(strict) until task 4.5; passing since
# ===========================================================================


def test_previous_is_selected_relative_to_the_current_run_not_to_the_newest_row(state_table):
    """R8.3: the previous schema is the one immediately older than **the current
    run's own row**, even when a newer row exists.

    Three successful versions are recorded and the current run is **v2** — v3
    belongs to a later run whose ``schema_extract`` row already landed (a
    concurrently started run, a retry, or clock skew). The previous schema for
    this run is therefore **v1**.

    Today the function sorts descending and returns ``items[1]``, which here is
    the current run's **own** row: the run compares its schema against itself,
    reports identical, and skips the remap. That is the failure R8.3 exists to
    remove.

    **Validates: Requirement 8.3**
    """
    _seed_step_row(state_table, run_id="run-v1", timestamp="2026-07-01T00:00:00", schema=_schema_with("integer"))
    _seed_step_row(state_table, run_id="run-v2", timestamp="2026-07-02T00:00:00", schema=_schema_with("number"))
    _seed_step_row(state_table, run_id="run-v3", timestamp="2026-07-03T00:00:00", schema=_schema_with("string"))

    previous = _call_get_previous_schema(PIPELINE_ID, "run-v2")

    assert previous is not None
    assert previous["run_id"] != "run-v2", "returned the current run's own row as its own predecessor"
    assert previous["run_id"] == "run-v1"


def test_every_gsi_page_is_considered(state_table):
    """R8.3: the GSI query must be paginated.

    DynamoDB applies ``FilterExpression`` **after** reading a 1 MB page, so a
    page containing only rows the filter rejects returns ``Items: []`` together
    with a ``LastEvaluatedKey``. Here the pipeline's earliest rows are large
    ``file_validation`` rows that fill the first page, and all three
    ``schema_extract`` rows sit on the second — the shape an established
    pipeline reaches, since the state table accumulates a row per step per run.

    Without an ``ExclusiveStartKey`` loop the function sees an empty first page
    and reports "no previous schema", so a real schema change is compared
    against nothing and the drift is missed.

    **Validates: Requirement 8.3**
    """
    # Ascending GSI order (ScanIndexForward defaults to True) puts the earliest
    # timestamps first, so the filtered-out rows occupy page one. 6 x 200 KB is
    # comfortably over the 1 MB page limit.
    for i in range(6):
        _seed_step_row(
            state_table,
            run_id=f"run-noise-{i}",
            timestamp=f"2026-08-0{i + 1}T00:00:00",
            step_name="file_validation",
            padding=200_000,
        )
    _seed_step_row(state_table, run_id="run-v1", timestamp="2026-09-01T00:00:00", schema=_schema_with("integer"))
    _seed_step_row(state_table, run_id="run-v2", timestamp="2026-09-02T00:00:00", schema=_schema_with("number"))
    _seed_step_row(state_table, run_id="run-v3", timestamp="2026-09-03T00:00:00", schema=_schema_with("string"))

    previous = _call_get_previous_schema(PIPELINE_ID, "run-v3")

    assert previous is not None, "the first page was empty after filtering and the query was not resumed"
    assert previous["run_id"] == "run-v2"


# ===========================================================================
# ADDED BY TASK 4.5 — pins the pagination loop in the shape the descending
# walk actually meets, which the test above no longer reaches
# ===========================================================================

# (run_id, step_name) pairs for the page-filling noise rows. The base table is
# keyed (run_id, step_name), so each pair must be distinct or the puts overwrite
# each other and the page never fills. These are the previous run's later steps
# and the current run's earlier steps — the rows that genuinely sit between two
# consecutive `schema_extract` rows.
_INTERLEAVED_NOISE = (
    ("run-v1", "schema_mapping"),
    ("run-v1", "create_code"),
    ("run-v1", "test_code"),
    ("run-v2", "file_validation"),
    ("run-v2", "deploy_glue_job"),
    ("run-v2", "glue_job_execution"),
)


def test_a_filtered_out_page_between_the_two_runs_does_not_end_the_walk(state_table):
    """R8.3's pagination half, in the shape a newest-first walk actually meets.

    Selection reads newest-first from just below the current run's own timestamp,
    so the rows it must page over are the ones **between** this run's
    ``schema_extract`` row and the previous one: the previous run's later steps
    and this run's earlier steps. The state table holds a row per step per run and
    those rows carry artifacts, so they fill a 1 MB page on their own — and
    DynamoDB applies ``FilterExpression`` AFTER the page read, so that page comes
    back ``Items: []`` with a ``LastEvaluatedKey``. Without resuming on that key
    the run reports "no previous schema" and a real schema change is compared
    against nothing.

    This is deliberately a different page shape from
    ``test_every_gsi_page_is_considered``: that test's noise rows are *older* than
    both schemas, which a newest-first bounded query never has to read, so it no
    longer exercises the resume. This one does.

    **Validates: Requirement 8.3**
    """
    _seed_step_row(state_table, run_id="run-v1", timestamp="2026-10-01T00:00:00", schema=_schema_with("integer"))
    # 6 x 200 KB is comfortably over the 1 MB page limit.
    for i, (noise_run, noise_step) in enumerate(_INTERLEAVED_NOISE):
        _seed_step_row(
            state_table,
            run_id=noise_run,
            timestamp=f"2026-10-0{i + 2}T00:00:00",
            step_name=noise_step,
            padding=200_000,
        )
    _seed_step_row(state_table, run_id="run-v2", timestamp="2026-10-09T00:00:00", schema=_schema_with("number"))

    previous = _call_get_previous_schema(PIPELINE_ID, "run-v2")

    assert previous is not None, "the first page was empty after filtering and the query was not resumed"
    assert previous["run_id"] == "run-v1"
    assert json.loads(previous["extracted_schema"]) == _schema_with("integer")
