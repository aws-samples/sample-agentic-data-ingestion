# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: declared-type classification and its routing (p30 task 4.4).

Task 4.0 wrote the characterization safety net in
``test_compare_schemas_characterization.py`` and deliberately left two decisions
to task 4.4, because **R8 does not settle either one**. This file covers exactly
those two, so the characterization file stays a characterization file:

1. **Which declared-type changes are breaking.** Task 0.3 found two extractor
   behaviours that make an *unchanged* pipeline report a different declared type
   between runs, and warned that "a spurious block on a re-upload will read as a
   regression". 4.4's call, asserted here so the decision is visible and cannot
   be changed silently:

   | Transition | Verdict | Why |
   |---|---|---|
   | ``integer`` → ``number`` | **compatible** | ``infer_schema_from_records`` samples 100 records and resolves {integer, number} → number, so identical data can read either. A ``number`` column holds every integer. |
   | anything involving ``null`` | **compatible** | ``null`` is the legacy reader's "no values observed" token where the streaming reader emits ``string`` for the same input. It is not a type claim, and nulls are writable into any nullable column. |
   | everything else, both directions | **blocking** | Deterministic given the input, and it changes the physical type the target column must hold. |

   The last row deliberately includes 0.3's highest-volume false-positive
   candidate, ``string`` → ``integer`` on a ``.csv`` → ``.parquet`` re-upload of
   identical data. It blocks. That is an approval prompt, not a failed run, and
   R8's whole point is that a change to what the target column holds gets a
   human decision. It is asserted below rather than left implicit.

   This matches the repo's existing widening convention,
   ``create_glue_pipeline/schema_evolution.py::is_type_change_compatible``,
   which allows ``int`` → ``double``/``decimal`` — the SQL-vocabulary analogue
   of ``integer`` → ``number`` — and does not allow ``int`` → ``string``.

2. **R8.2's routing half.** ``is_compatible: false`` must reach an action that
   requires human approval before the target is written. 4.4 reuses the route
   ``field_removed`` already takes — ``create_new_pipeline`` — rather than
   inventing a parallel action. The second hop of that chain
   (``create_new_pipeline`` → forced approval in ``schema_mapping``) is already
   covered by
   ``tests/unit/lambdas/schema_mapping/test_schema_mapping_approval.py::test_create_new_pipeline_forces_approval``;
   asserting it again here would mean loading a second Lambda's module under the
   same ``sys.modules["lambda_function"]`` key that file owns.

**The transition table is exercised exhaustively, not sampled.** The emitted type
vocabulary is seven tokens (task 0.3), so all 49 ordered pairs are cheap and
strictly stronger than generated cases. Field names are parametrized over the
names that break a path-string implementation — ``a.b``, ``x.type`` — and over
the schema keywords themselves — ``type``, ``properties``, ``items``,
``required`` — because 4.4's predicate is positional and must not consult field
names at all.

**Validates: Requirements 8.1, 8.2**
"""

import copy
import importlib.util
import os
import sys
import unittest.mock as mock

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
        "schema_compare_lf_p30_declared_type",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p30_declared_type"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


# The complete emitted type vocabulary across all six extractor paths
# (task 0.3): six tokens from the streaming and jdbc readers, plus `null`,
# which only the legacy recursive reader emits.
TYPE_TOKENS = ("string", "integer", "number", "boolean", "object", "array", "null")

# Field names that a path-string implementation gets wrong, plus the schema
# keywords themselves. 4.4's predicate is positional, so every one of these must
# behave identically.
FIELD_NAMES = ("id", "type", "properties", "items", "required", "a.b", "x.type", "a.b.c.type")


def _expected_blocking(old: str, new: str) -> bool:
    """The transition rule this file asserts, stated independently of the code.

    Spelled out here rather than imported from the Lambda so the test is an
    oracle and not a restatement of the implementation.
    """
    if old == new:
        return False
    if "null" in (old, new):
        return False
    return (old, new) != ("integer", "number")


def _flat(field: str, type_token: str) -> dict:
    """The shape every streaming extractor emits: object root, depth 1."""
    return {"type": "object", "properties": {field: {"type": type_token}}}


# ===========================================================================
# R8.1 — classification of a declared `type` change
# ===========================================================================


@pytest.mark.parametrize("new_type", TYPE_TOKENS)
@pytest.mark.parametrize("old_type", TYPE_TOKENS)
def test_every_declared_type_transition_is_classified_by_the_documented_rule(old_type, new_type):
    """All 49 ordered pairs over the emitted vocabulary, classified and routed.

    One assertion set, three linked outcomes: the diff kind, the summary count,
    and ``is_compatible``. R8.2 ties them together — a ``type_mismatch`` that
    left ``is_compatible`` true would be the original defect wearing a new label.

    **Validates: Requirements 8.1, 8.2**
    """
    before = _flat("col", old_type)
    after = _flat("col", new_type)

    result = _lf.compare_schemas(before, after)

    if old_type == new_type:
        assert result["differences"] == []
        assert result["is_identical"] is True
        assert result["is_compatible"] is True
        return

    assert len(result["differences"]) == 1, result["differences"]
    diff = result["differences"][0]
    assert diff["path"] == "properties.col.type"

    if _expected_blocking(old_type, new_type):
        assert diff["type"] == "type_mismatch"
        assert diff["old_type"] == old_type
        assert diff["new_type"] == new_type
        assert result["summary"]["type_mismatches"] == 1
        assert result["summary"]["value_changes"] == 0
        assert result["is_compatible"] is False
    else:
        assert diff["type"] == "value_changed"
        assert diff["old_value"] == old_type
        assert diff["new_value"] == new_type
        assert result["summary"]["type_mismatches"] == 0
        assert result["summary"]["value_changes"] == 1
        assert result["is_compatible"] is True


@pytest.mark.parametrize("field", FIELD_NAMES)
def test_classification_does_not_depend_on_the_field_name(field):
    """R8.5's case (``integer`` → ``string``) blocks whatever the column is called.

    ``a.b`` and ``x.type`` are the two names task 0.3 used to kill the
    path-string implementation: they emit ``properties.a.b.type`` and
    ``properties.x.type.type``, which a keyword parse misses and cannot resolve
    respectively. ``type``/``properties``/``items``/``required`` are the schema
    keywords, which a name-sniffing implementation would treat specially.

    **Validates: Requirement 8.1**
    """
    result = _lf.compare_schemas(_flat(field, "integer"), _flat(field, "string"))

    assert [d["type"] for d in result["differences"]] == ["type_mismatch"]
    assert result["differences"][0]["path"] == f"properties.{field}.type"
    assert result["is_compatible"] is False


def test_declared_type_change_under_a_chain_of_properties_and_items_blocks():
    """The predicate composes: ``properties`` and ``items`` steps both descend
    into a schema node, so a declaration at depth is still a declaration.

    The legacy extractor nests without bound (task 0.3), so the fix cannot
    assume any particular depth.

    **Validates: Requirement 8.1**
    """
    before = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"inner": {"type": "array", "items": {"type": "integer"}}},
                },
            }
        },
    }
    after = copy.deepcopy(before)
    after["properties"]["rows"]["items"]["properties"]["inner"]["items"]["type"] = "boolean"

    result = _lf.compare_schemas(before, after)

    assert [d["type"] for d in result["differences"]] == ["type_mismatch"]
    assert result["differences"][0]["path"] == "properties.rows.items.properties.inner.items.type"
    assert result["is_compatible"] is False


def test_a_list_valued_type_keyword_is_not_a_declared_type_change():
    """``{"type": ["string", "null"]}`` is not a single declared value.

    0.3's predicate is "parent is a schema node **and the value is a string**".
    A list-valued ``type`` fails the second half, so it walks as an ordinary
    list and stays ``value_changed``. Guards the predicate's second clause,
    which is otherwise unexercised.

    **Validates: Requirement 8.1**
    """
    before = {"type": "object", "properties": {"col": {"type": ["integer", "null"]}}}
    after = {"type": "object", "properties": {"col": {"type": ["string", "null"]}}}

    result = _lf.compare_schemas(before, after)

    assert [d["type"] for d in result["differences"]] == ["value_changed"]
    assert result["differences"][0]["path"] == "properties.col.type[0]"
    assert result["is_compatible"] is True


# ===========================================================================
# The widening decision — 4.4's call, asserted so it cannot change silently
# ===========================================================================


def test_integer_widening_to_number_stays_compatible():
    """``integer`` → ``number`` must NOT block: sampling alone produces it.

    ``infer_schema_from_records`` samples 100 records and resolves
    {integer, number} → number (task 0.3), so two runs of an unchanged pipeline
    over identical data can disagree. Blocking would make the pipeline
    non-deterministic, and a ``number`` column holds every integer.

    **Validates: Requirement 8.2**
    """
    result = _lf.compare_schemas(_flat("score", "integer"), _flat("score", "number"))

    assert [d["type"] for d in result["differences"]] == ["value_changed"]
    assert result["is_compatible"] is True
    assert result["summary"]["type_mismatches"] == 0


def test_number_narrowing_to_integer_blocks():
    """The reverse direction is a narrowing and must block.

    Asserted alongside the widening so the asymmetry is explicit: the allowance
    is directional, not "numeric changes are fine".

    **Validates: Requirement 8.2**
    """
    result = _lf.compare_schemas(_flat("score", "number"), _flat("score", "integer"))

    assert [d["type"] for d in result["differences"]] == ["type_mismatch"]
    assert result["is_compatible"] is False


@pytest.mark.parametrize("other", ("string", "integer", "number", "boolean", "object", "array"))
def test_transitions_involving_null_stay_compatible_in_both_directions(other):
    """``null`` carries no type information, so neither direction blocks.

    The legacy reader emits ``null`` for an all-null column where the streaming
    reader emits ``string`` for the same input (task 0.3), and an all-null
    sample in either run says nothing about the column's type. Nulls are
    writable into any nullable target column.

    **Validates: Requirement 8.2**
    """
    to_null = _lf.compare_schemas(_flat("col", other), _flat("col", "null"))
    from_null = _lf.compare_schemas(_flat("col", "null"), _flat("col", other))

    assert [d["type"] for d in to_null["differences"]] == ["value_changed"]
    assert to_null["is_compatible"] is True
    assert [d["type"] for d in from_null["differences"]] == ["value_changed"]
    assert from_null["is_compatible"] is True


def test_csv_to_parquet_reupload_of_identical_data_blocks_deliberately():
    """A format switch DOES block, and that is the decision, not an oversight.

    Task 0.3 flagged this as the highest-volume false-positive candidate: csv
    reads every column as ``string`` while parquet reads the same bytes as
    ``integer``/``number``, so re-uploading identical data as ``.parquet`` fires
    on every typed column. 4.4 leaves it blocking — the change is deterministic
    given the input, it changes the physical type the target column must hold,
    and the route is human approval rather than failure. Pinned so a future
    change to that judgement is a visible test change.

    **Validates: Requirement 8.2**
    """
    csv_shape = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "score": {"type": "string"}, "active": {"type": "string"}},
    }
    parquet_shape = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "score": {"type": "number"}, "active": {"type": "boolean"}},
    }

    result = _lf.compare_schemas(csv_shape, parquet_shape)

    assert [d["type"] for d in result["differences"]] == ["type_mismatch"] * 3
    assert result["summary"]["type_mismatches"] == 3
    assert result["is_compatible"] is False


# ===========================================================================
# R8.2 — routing to an action that requires human approval
# ===========================================================================


def _run_handler(previous_schema: dict, current_schema: dict) -> dict:
    """Drive ``lambda_handler`` past its DynamoDB/S3 reads to the routing decision.

    Only the module's own read/write helpers are stubbed, so the comparison and
    the action choice under test are the real ones.
    """
    current_row = {
        "extracted_schema": current_schema,
        "schema_hash": "hash-current",
        "original_pipeline_id": "pipe-r8",
    }
    previous_row = {"extracted_schema": previous_schema, "schema_hash": "hash-previous"}

    with (
        mock.patch.object(_lf, "get_current_schema", return_value=current_row),
        mock.patch.object(_lf, "get_previous_schema", return_value=previous_row),
        mock.patch.object(
            _lf,
            "get_pipeline_metadata",
            return_value={
                "target_schema_bucket": "bucket-r8",
                "target_schema_s3_key": "schemas/target.json",
            },
        ),
        mock.patch.object(_lf, "check_target_schema_changed", return_value=(False, None, None)),
        mock.patch.object(_lf, "store_comparison_result") as stored,
    ):
        response = _lf.lambda_handler({"run_id": "run-r8"}, None)

    assert stored.called, "the comparison must be recorded for the audit trail"
    return response


def test_declared_type_change_routes_to_an_approval_requiring_action():
    """R8.2: a ``type_mismatch`` reaches ``create_new_pipeline``.

    ``schema_mapping.determine_approval_requirement`` forces approval for that
    action, so the target is not written until a human decides
    (``test_schema_mapping_approval.py::test_create_new_pipeline_forces_approval``
    covers the second hop). This is the same route ``field_removed`` already
    took — reused, not duplicated.

    **Validates: Requirement 8.2**
    """
    response = _run_handler(_flat("id", "integer"), _flat("id", "string"))

    assert response["comparison_result"]["is_compatible"] is False
    assert response["comparison_result"]["summary"]["type_mismatches"] == 1
    assert response["action"] == "create_new_pipeline"


def test_field_removed_still_routes_to_the_same_action():
    """The pre-existing incompatibility route is unchanged by 4.4.

    Asserted next to the type-mismatch case to show 4.4 reused the route rather
    than adding a parallel one.

    **Validates: Requirement 8.2**
    """
    before = {"type": "object", "properties": {"id": {"type": "integer"}, "score": {"type": "number"}}}
    after = {"type": "object", "properties": {"id": {"type": "integer"}}}

    response = _run_handler(before, after)

    assert response["comparison_result"]["is_compatible"] is False
    assert response["action"] == "create_new_pipeline"


def test_a_widening_does_not_route_to_the_approval_requiring_action():
    """``integer`` → ``number`` stays on the compatible route.

    The complement of the test above: the widening allowance is only meaningful
    if it actually changes where the run goes.

    **Validates: Requirement 8.2**
    """
    response = _run_handler(_flat("score", "integer"), _flat("score", "number"))

    assert response["comparison_result"]["is_compatible"] is True
    assert response["action"] == "update_existing_pipeline"
