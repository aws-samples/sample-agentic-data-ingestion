# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: reuse-candidate eligibility filters on approval status.

Covers the defect task C2 fixes alongside p47 R4 Arm B. The reuse-candidate lookup
in ``lambda_handler`` (`schema_compare/lambda_function.py:690-708`) had **no covering
tests** before this file — the directory's other suites drive `lambda_handler` for
target-schema and previous-schema behaviour and never reach the identical-hash
branch's mapping lookup.

**The defect.** The lookup selected *the newest ``schema_mapping`` state row carrying
a ``mapping_id``*, filtering on::

    Attr("step_name").eq("schema_mapping") & Attr("mapping_id").exists()

and nothing else. The codegen path's equivalent,
``create_code/mapping_resolution.py::_query_approved_schema_mapping_rows``
(``:164-196``), additionally filters ``Attr("status").eq("success")``, and its
docstring defines the semantics this file asserts against:

    schema_mapping writes ``status="success"``; when a manual approval is required
    the approval_handler flips it to ``waiting_approval``; the approvals flow then
    sets ``success`` (approved) or ``rejected``. So ``status == "success"`` means
    approved-or-approval-not-required, which is exactly the set of mappings safe to
    substitute.

So a **rejected** mapping — and a mapping still sitting at **waiting_approval** —
was a valid reuse candidate. The human approval gate on the reuse path (p47 R4 Arm
B, invariant ``LGR-69ca78b-1``) was the only thing standing between a rejected
mapping and its use, which is precisely why R4 Arm A would have been dangerous.

**Where the expectations come from.** Not from current output — current output is the
defect. They come from p47's requirements and from
``_query_approved_schema_mapping_rows``' documented semantics above. The three
requirement-derived tests (``rejected``, ``waiting_approval``, and the newest-row
shadowing case) failed on the unfixed lookup; that failure is what confirmed the
defect before the filter changed.

**Every status here is reachable, and none of them is absent.** Enumerated from the
writers rather than assumed:

* ``success`` — ``schema_mapping/lambda_function.py:1584``, at row creation.
* ``waiting_approval`` — ``approval_handler/lambda_function.py:467``, when the gate
  opens. A gate that never gets a decision (a timeout) leaves the row here.
* ``rejected`` — the decision echo, on both reject paths: the dashboard's
  (``handlers/approvals.py::_echo_rejection_onto_state_row``, added after a
  dashboard-rejected mapping was reused on 2026-10-09) and ``approval_handler``'s.

A ``success`` row can also carry ``reuse_revoked_at``: a rejection at a later run
that reused its mapping stamps it, leaving the earlier run's status alone
(``shared/mapping_reuse.py``). The lookup's predicate is
``approved_for_reuse_condition()``, which excludes it.
* *absent* — no writer produces it. The sole writer of a row carrying a
  ``mapping_id`` always sets ``status`` (``:1579-1604``); that step's failure row
  carries no ``mapping_id``, so ``Attr("mapping_id").exists()`` already excludes it
  (``:2024-2036``); and both mutators use ``SET`` under
  ``ConditionExpression="attribute_exists(run_id)"``, never ``REMOVE``.

That is why the predicate is ``.eq("success")`` rather than something admitting an
absent status: there is no unknown-status row to be permissive about, and a
``| not_exists()`` clause would be a second, divergent definition of "approved"
pointing the wrong way on a governance-sensitive path.

**Degradation is safe, so C25 / ``LGR-a419843-1``'s "a comparison that cannot be
made must not become a refusal" is not engaged.** When the filter finds nothing,
``existing_mapping_id`` is falsy and the pre-existing branch at ``:711-716`` sets
``action = "create_new_pipeline"`` with ``summary["force_new_mapping"] = True``. The
run computes a fresh mapping and takes the normal gate — it costs a Bedrock call, it
does not refuse the run. ``test_no_candidate_falls_through_to_create_new_pipeline``
and ``test_a_rejected_only_pipeline_falls_through_rather_than_failing`` assert that
fall-through directly rather than assuming it.

**Fixtures.** DynamoDB comes from the shared ``mock_dynamodb_tables`` fixture per the
project testing standards; this file creates no ``moto.mock_aws()`` and no ``boto3``
client of its own. The filter runs server-side in the moto query engine, so what is
asserted is the real ``FilterExpression`` semantics, not a scripted ``query`` stub.

**Validates: Requirements R4.1, R4.2**
"""

import importlib.util
import os
import sys
import unittest.mock as mock

# Imported at module scope so it is already in sys.modules before the
# patch.dict(sys.modules) block in _load_module() runs — same reason as
# test_previous_schema_selection.py: a re-imported boto3.dynamodb.conditions would
# carry NEW Key/Attr classes that the moto Table resource cannot translate.
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
        "schema_compare_lf_p47_reuse_status",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p47_reuse_status"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

PIPELINE_ID = "pipe-p47-reuse"
OTHER_PIPELINE_ID = "pipe-p47-unrelated"
RUN_ID = "run-p47-reuse"
SAME_HASH = "hash-identical"
SCHEMA = {"type": "object", "properties": {"id": {"type": "integer"}}}


@pytest.fixture
def state_table(mock_dynamodb_tables):
    """Point the module's ``table`` global at the shared moto state table.

    ``table`` is the resource the candidate lookup queries, so the
    ``FilterExpression`` under test is evaluated by the real query engine.
    """
    state = mock_dynamodb_tables["agentic_data_ingestion_state"]
    with mock.patch.object(_lf, "table", state):
        yield state


def _seed_mapping_row(
    state_table,
    *,
    run_id,
    timestamp,
    mapping_id="map-default",
    status="success",
    pipeline_id=PIPELINE_ID,
    step_name="schema_mapping",
    include_mapping_id=True,
    padding=0,
    extra=None,
):
    """Seed a ``schema_mapping`` state row visible in the PipelineIndex GSI.

    ``pipeline_id`` and ``timestamp`` are the GSI's partition and sort keys, so both
    are required for the row to be a candidate at all. ``include_mapping_id=False``
    reproduces the shape ``schema_mapping``'s failure path writes (``:2024-2036``),
    which carries no ``mapping_id``. ``padding`` inflates the item so the pagination
    test can fill a real 1 MB page.
    """
    item = {
        "run_id": run_id,
        "step_name": step_name,
        "pipeline_id": pipeline_id,
        "original_pipeline_id": pipeline_id,
        "timestamp": timestamp,
        "status": status,
    }
    if include_mapping_id:
        item["mapping_id"] = mapping_id
    if padding:
        item["padding"] = "x" * padding
    if extra:
        item.update(extra)
    state_table.put_item(Item=item)


def _run_handler():
    """Drive ``lambda_handler`` down the identical-hash branch.

    The current and previous schema hashes are equal and the target schema is
    unchanged, which is the only route to the reuse-candidate lookup. Everything
    outside that branch is stubbed so the assertions are about the lookup alone.
    """
    current_row = {
        "extracted_schema": SCHEMA,
        "schema_hash": SAME_HASH,
        "original_pipeline_id": PIPELINE_ID,
    }
    previous_row = {"extracted_schema": SCHEMA, "schema_hash": SAME_HASH}

    with (
        mock.patch.object(_lf, "get_current_schema", return_value=current_row),
        mock.patch.object(_lf, "get_previous_schema", return_value=previous_row),
        mock.patch.object(
            _lf,
            "get_pipeline_metadata",
            return_value={"target_schema_bucket": "bucket-p47", "target_schema_s3_key": "schemas/target.json"},
        ),
        mock.patch.object(_lf, "check_target_schema_changed", return_value=(False, None, None)),
        mock.patch.object(_lf, "store_comparison_result"),
    ):
        return _lf.lambda_handler({"run_id": RUN_ID}, None)


# ===========================================================================
# PINNED — current behaviour that is correct and must survive the fix
# ===========================================================================


def test_an_approved_mapping_is_selected_for_reuse(state_table):
    """The whole point of the fast path: a ``success`` mapping is reused.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", mapping_id="map-approved")

    response = _run_handler()

    assert response["action"] == "use_existing_pipeline"
    assert response["existing_mapping_id"] == "map-approved"


def test_the_newest_approved_mapping_wins_among_several(state_table):
    """Recency still decides *within* the approved set.

    Pinned so the added status filter is visibly a narrowing of the candidate set
    rather than a change to how a winner is chosen from it.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", mapping_id="map-old")
    _seed_mapping_row(state_table, run_id="run-b", timestamp="2026-03-02T00:00:00", mapping_id="map-mid")
    _seed_mapping_row(state_table, run_id="run-c", timestamp="2026-03-03T00:00:00", mapping_id="map-new")

    response = _run_handler()

    assert response["existing_mapping_id"] == "map-new"


def test_no_candidate_falls_through_to_create_new_pipeline(state_table):
    """The pre-existing fall-through, pinned because the fix makes it fire more often.

    An identical schema with no reusable mapping already routes to
    ``create_new_pipeline`` with ``force_new_mapping``. That branch is what makes the
    status filter safe: no approved mapping means compute a new one and take the
    normal gate, not refuse the run.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", step_name="create_code")

    response = _run_handler()

    assert response["action"] == "create_new_pipeline"
    assert response["comparison_result"]["summary"]["force_new_mapping"] is True
    # `None`, not `""`. The handler's `comparison_result.get("existing_mapping_id",
    # "")` at `:779` never reaches its default, because `:708` always sets the key —
    # to `None` when nothing was found. Pinned exactly here, once, so the contract is
    # recorded honestly; the other tests assert falsiness rather than restating it.
    assert response["existing_mapping_id"] is None


def test_a_row_without_a_mapping_id_is_not_a_candidate(state_table):
    """``schema_mapping``'s failure row carries no ``mapping_id`` (``:2024-2036``).

    Pinned next to the status filter so it stays clear that ``mapping_id.exists()``
    is what excludes a *failed* mapping step, and the status filter is what excludes
    an *unapproved* one. They are different exclusions.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(
        state_table,
        run_id="run-a",
        timestamp="2026-03-01T00:00:00",
        status="failed",
        include_mapping_id=False,
    )

    response = _run_handler()

    assert response["action"] == "create_new_pipeline"
    assert not response["existing_mapping_id"]


def test_another_pipelines_mapping_is_never_selected(state_table):
    """Candidate scoping is by GSI partition, pinned so a widened key condition fails here.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", mapping_id="map-mine")
    _seed_mapping_row(
        state_table,
        run_id="other-run",
        timestamp="2026-04-01T00:00:00",
        mapping_id="map-theirs",
        pipeline_id=OTHER_PIPELINE_ID,
    )

    response = _run_handler()

    assert response["existing_mapping_id"] == "map-mine"


# ===========================================================================
# WRITTEN TO THE REQUIREMENT — these failed on the unfixed lookup, which is
# what confirmed the defect
# ===========================================================================


def test_a_rejected_mapping_is_not_a_reuse_candidate(state_table):
    """A mapping a human rejected must never be substituted.

    ``rejected`` is written by ``approval_handler/lambda_function.py:687``, so this
    is a reachable state and not a hypothetical. Before the fix the lookup filtered
    only on ``step_name`` and ``mapping_id``, so this row was selected and the
    approval gate was the sole remaining obstacle to its use.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(
        state_table,
        run_id="run-a",
        timestamp="2026-03-01T00:00:00",
        mapping_id="map-rejected",
        status="rejected",
    )

    response = _run_handler()

    assert not response["existing_mapping_id"], "a rejected mapping was offered for reuse"
    assert response["action"] == "create_new_pipeline"


def test_a_mapping_awaiting_approval_is_not_a_reuse_candidate(state_table):
    """``waiting_approval`` means a human has been asked and has not answered.

    Also the resting state of a gate that timed out without a decision.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(
        state_table,
        run_id="run-a",
        timestamp="2026-03-01T00:00:00",
        mapping_id="map-pending",
        status="waiting_approval",
    )

    response = _run_handler()

    assert not response["existing_mapping_id"], "an unapproved mapping was offered for reuse"
    assert response["action"] == "create_new_pipeline"


def test_a_mapping_rejected_at_a_later_run_is_not_a_reuse_candidate(state_table):
    """The 2026-10-09 shape: rejected at a reuse run, which has no row of its own.

    The rejection revokes the earlier run's row with ``reuse_revoked_at`` and leaves
    its ``success`` status alone. The next run must not propose it again.
    """
    _seed_mapping_row(
        state_table,
        run_id="run-a",
        timestamp="2026-03-01T00:00:00",
        mapping_id="map-refused-later",
        extra={"reuse_revoked_at": "2026-03-02T00:00:00+00:00"},
    )

    response = _run_handler()

    assert not response["existing_mapping_id"], "a mapping refused at a later run was offered for reuse"
    assert response["action"] == "create_new_pipeline"


def test_a_revoked_mapping_does_not_shadow_an_older_approved_one(state_table):
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", mapping_id="map-approved")
    _seed_mapping_row(
        state_table,
        run_id="run-b",
        timestamp="2026-03-05T00:00:00",
        mapping_id="map-refused-later",
        extra={"reuse_revoked_at": "2026-03-06T00:00:00+00:00"},
    )

    response = _run_handler()

    assert response["existing_mapping_id"] == "map-approved"


def test_a_newer_rejected_mapping_does_not_shadow_an_older_approved_one(state_table):
    """The sharpest shape: recency must not outrank approval status.

    The lookup sorted every matching row by ``timestamp`` and took the first, so the
    newest row won whatever its status. A pipeline whose most recent mapping was
    rejected therefore lost access to its last *approved* mapping — the newest row
    was selected instead. Both halves matter: the rejected mapping must not be
    chosen, **and** the approved one must still be reusable.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-a", timestamp="2026-03-01T00:00:00", mapping_id="map-approved")
    _seed_mapping_row(
        state_table,
        run_id="run-b",
        timestamp="2026-03-05T00:00:00",
        mapping_id="map-rejected",
        status="rejected",
    )

    response = _run_handler()

    assert response["existing_mapping_id"] != "map-rejected", "a rejected mapping shadowed an approved one"
    assert response["existing_mapping_id"] == "map-approved"
    assert response["action"] == "use_existing_pipeline"


def test_a_rejected_only_pipeline_falls_through_rather_than_failing(state_table):
    """No approved mapping ⇒ compute a new one, never refuse the run.

    The degradation half of the fix, asserted rather than assumed. C25 /
    ``LGR-a419843-1`` records that "a comparison that cannot be made must not become
    a refusal. That is the shape that turns this fix into an outage." Here the
    narrowed filter cannot turn into a refusal, because the only path out of an
    empty candidate set is ``create_new_pipeline`` with ``force_new_mapping``.

    **Validates: Requirement R4.2**
    """
    for i, status in enumerate(("rejected", "waiting_approval", "rejected")):
        _seed_mapping_row(
            state_table,
            run_id=f"run-{i}",
            timestamp=f"2026-03-0{i + 1}T00:00:00",
            mapping_id=f"map-{i}",
            status=status,
        )

    response = _run_handler()

    assert response["statusCode"] == 200, "a pipeline with no approved mapping must still complete this step"
    assert response["action"] == "create_new_pipeline"
    assert response["comparison_result"]["summary"]["force_new_mapping"] is True


def test_a_filtered_out_page_does_not_end_the_candidate_walk(state_table):
    """The status filter makes an all-rejected page ordinary, so the walk must resume.

    DynamoDB applies ``FilterExpression`` **after** the 1 MB page read, so a page
    holding only rows the filter rejects returns ``Items: []`` together with a
    ``LastEvaluatedKey`` — the hazard ``LGR-eee8ac8-3`` records about this file's
    other GSI query. Narrowing the filter to approved rows makes it likelier: this
    partition holds ~9 step rows per run and at most one of them now matches, and a
    run rejected several times in a row contributes only non-matching
    ``schema_mapping`` rows.

    Here the pipeline's newest rows are large **rejected** mappings that fill the
    first descending page, and the last approved mapping sits on the second. Without
    an ``ExclusiveStartKey`` loop the lookup reports "no approved mapping" and the
    pipeline silently loses the fast path — a degradation with no symptom, since the
    fall-through completes the run normally. Real paging through the moto fixture, not
    a scripted ``query``, so the filter-after-page semantics are enforced by the query
    engine.

    **Validates: Requirement R4.2**
    """
    _seed_mapping_row(state_table, run_id="run-approved", timestamp="2026-03-01T00:00:00", mapping_id="map-approved")
    # Descending order reads newest first, so these must be NEWER than the approved
    # row to sit ahead of it. 6 x 200 KB is comfortably over the 1 MB page limit.
    for i in range(6):
        _seed_mapping_row(
            state_table,
            run_id=f"run-rejected-{i}",
            timestamp=f"2026-03-1{i}T00:00:00",
            mapping_id=f"map-rejected-{i}",
            status="rejected",
            padding=200_000,
        )

    response = _run_handler()

    assert response["existing_mapping_id"] == "map-approved", (
        "the first page was empty after filtering and the query was not resumed"
    )
    assert response["action"] == "use_existing_pipeline"
