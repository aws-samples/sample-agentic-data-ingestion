# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the shadow-mode comparison.

Spec ``real-spark-verification`` task 10.1. Requirements 4.2 and 4.5, design
§4.7.

Example-based, and deliberately so: the generative side of R4.2/R4.5 is Property
16 (task 10.2), which generates result pairs including the near-misses and widens
past the cap. What is pinned here is the set of *specific* cases whose answers
are decided by an argument rather than by a generator — the near-misses
themselves, the intentional asymmetries this comparison must not report, the
priority order the cap depends on, and the one dimension that is presently
unmeasurable.

Every result dict a test builds is checked against
``tests/fixtures/run_test_result_shapes.py`` first, so a fixture here cannot
drift from the Test_Result_Contract it is supposed to represent.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from code_interpreter_runner import CodeInterpreterTestRunner
from shadow_comparison import (
    _DQ_PER_COLUMN_DIMENSIONS,
    _DQ_SCALAR_DIMENSIONS,
    _TOP_LEVEL_DIMENSIONS,
    ABSENT,
    COMPARED_DIMENSIONS,
    DIMENSION_DTYPES,
    DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
    MAX_RECORDED_DISAGREEMENTS,
    compare_results,
    values_agree,
)
from tests.fixtures.run_test_result_shapes import (
    CONVERSION_REFUSAL,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

_MODULE_PATH = (
    Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_test_code" / "shadow_comparison.py"
)


# ---------------------------------------------------------------------------
# Builders — contract-checked so the fixtures cannot drift
# ---------------------------------------------------------------------------


def _dq(**overrides: Any) -> dict[str, Any]:
    """The seven keys ``_parse_results`` puts in ``data_quality_results``."""
    payload: dict[str, Any] = {
        "completeness_score": 95.0,
        "uniqueness_score": 100.0,
        "schema_compliance": 100.0,
        "overall_quality_score": 98.3,
        "null_counts": {"id": 0, "name": 5},
        "duplicates": 0,
        "missing_columns": [],
    }
    payload.update(overrides)
    return payload


def _violations(result: dict[str, Any], shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding every one that mentions ``conversion_report``.

    The same substring exclusion, for the same reason, as
    ``test_result_shape_equivalence_prop.py:259``: the Glue path returns ``None``
    for that key on every outcome (task 8.1), and Property 2 excludes exactly
    that narrowing. A builder here that could not express a Glue-shaped success
    could not test the asymmetry this comparison must ignore.
    """
    return [violation for violation in shape_violations(result, shape) if "conversion_report" not in violation]


def _success(**overrides: Any) -> dict[str, Any]:
    """A contract-shaped ``success`` result."""
    result: dict[str, Any] = {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": _dq(),
        "error_details": None,
        "conversion_report": {"constructs_converted": ["col"]},
    }
    result.update(overrides)
    assert _violations(result, SUCCESS) == []
    return result


def _statement_failure(**overrides: Any) -> dict[str, Any]:
    """A contract-shaped ``statement_failure`` result — it ran and it failed."""
    result: dict[str, Any] = {
        "execution_successful": False,
        "executed": True,
        "exit_code": 1,
        "stderr": "Traceback…",
        "input_records": 100,
        "output_records": 0,
        "data_quality_results": {},
        "error_details": "AnalysisException: cannot resolve 'zip'",
        "conversion_report": {"constructs_converted": ["col"]},
    }
    result.update(overrides)
    assert _violations(result, STATEMENT_FAILURE) == []
    return result


def _proxy_refusal() -> dict[str, Any]:
    """A contract-shaped ``conversion_refusal`` result — pandas only (R3.2)."""
    result: dict[str, Any] = {
        "execution_successful": False,
        "executed": False,
        "exit_code": None,
        "stderr": "",
        "input_records": 0,
        "output_records": 0,
        "data_quality_results": {},
        "error_details": "unsupported construct",
        "conversion_report": None,
        "not_verified_by_proxy": True,
        "not_verified_reason": "not_yet_expressible",
        "not_verified_construct": "initcap",
        "not_verified_line_number": 3,
        "not_verified_source_line": "df.withColumn('n', initcap(col('n')))",
    }
    assert shape_violations(result, CONVERSION_REFUSAL) == []
    return result


def _dimensions(comparison: dict[str, Any]) -> list[str]:
    return [entry["dimension"] for entry in comparison["disagreements"]]


# ---------------------------------------------------------------------------
# Agreement records nothing (R4.2's second half)
# ---------------------------------------------------------------------------


def test_a_result_compared_with_itself_records_no_disagreement() -> None:
    comparison = compare_results(_success(), _success())
    assert comparison["disagreements"] == []
    assert comparison["omitted"] == 0
    # Two columns in `null_counts` expand to two comparisons; the six scalar
    # dimensions are one each; `dtypes` is absent on both sides, which is one
    # comparison of two absences (see the dtypes tests below).
    assert comparison["dimensions_compared"] == 9


def test_two_missing_results_compare_without_raising() -> None:
    """``None`` on the Glue side is a normal input, not an error.

    Task 10.3's fail-soft discipline (``LGR-60c4985-5``) means the shadow side
    must be able to have produced nothing — raised, timed out, or been refused
    for capacity — without that altering anything. A comparison that raised on
    it would push work into the wrapper that this function can just handle.
    """
    assert compare_results(None, None)["disagreements"] == []
    assert compare_results(_success(), None)["disagreements"] != []


# ---------------------------------------------------------------------------
# Each dimension, disagreeing
# ---------------------------------------------------------------------------


def test_execution_successful_disagreement_is_recorded() -> None:
    comparison = compare_results(
        _statement_failure(data_quality_results=_dq(), output_records=100),
        _success(),
    )
    assert _dimensions(comparison) == ["execution_successful"]
    assert comparison["disagreements"][0] == {
        "dimension": "execution_successful",
        "pandas": False,
        "glue": True,
    }


def test_output_records_disagreement_is_recorded() -> None:
    comparison = compare_results(_success(output_records=100), _success(output_records=98))
    assert _dimensions(comparison) == ["output_records"]


def test_data_quality_scalar_disagreements_are_recorded() -> None:
    comparison = compare_results(
        _success(data_quality_results=_dq(duplicates=0, schema_compliance=100.0, missing_columns=[])),
        _success(data_quality_results=_dq(duplicates=2, schema_compliance=75.0, missing_columns=["zip"])),
    )
    assert _dimensions(comparison) == ["duplicates", "schema_compliance", "missing_columns"]


def test_null_counts_expand_to_one_entry_per_disagreeing_column() -> None:
    comparison = compare_results(
        _success(data_quality_results=_dq(null_counts={"id": 0, "name": 5, "age": 1})),
        _success(data_quality_results=_dq(null_counts={"id": 0, "name": 4, "age": 7})),
    )
    assert _dimensions(comparison) == ["null_counts[age]", "null_counts[name]"]
    # `id` agreed, so it contributes nothing — but it was still compared.
    assert comparison["dimensions_compared"] == 10


def test_a_container_mismatch_records_one_entry_not_one_per_column() -> None:
    """The one-executor-failed shape must not cost 512 entries.

    A failed or refused run carries ``data_quality_results: {}``, so the whole
    ``null_counts`` map is absent on that side. Expanding that to one
    half-absent entry per column would put the wide-frame payload back in the
    row that the cap exists to keep out of it.
    """
    comparison = compare_results(
        _success(data_quality_results=_dq(null_counts={f"c{i}": i for i in range(400)})),
        _success(data_quality_results={}),
    )
    assert "null_counts" in _dimensions(comparison)
    assert not any(dimension.startswith("null_counts[") for dimension in _dimensions(comparison))


def test_missing_columns_is_compared_in_order() -> None:
    """Both paths iterate the declared target columns in declaration order.

    ``code_interpreter_runner.py:550`` and
    ``glue_session_runner.declared_target_columns`` both preserve declaration
    order, so an order difference is a real difference in the emitted payload
    rather than an artefact of the comparison.
    """
    comparison = compare_results(
        _success(data_quality_results=_dq(missing_columns=["a", "b"])),
        _success(data_quality_results=_dq(missing_columns=["b", "a"])),
    )
    assert _dimensions(comparison) == ["missing_columns"]


# ---------------------------------------------------------------------------
# The proxy-refusal dimension — the number the feature exists to produce
# ---------------------------------------------------------------------------


def test_proxy_refused_and_glue_succeeded_is_recorded() -> None:
    """``LGR-4dd9b42-1``'s revisit condition asks for exactly this count.

    Note what a *value* comparison would have done here: the pandas side's
    marker is ``True`` and the Glue side's success is ``True``, so comparing
    them as two readings of one thing would call this agreement and record
    nothing — in precisely the case worth recording.
    """
    comparison = compare_results(_proxy_refusal(), _success())
    assert DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS in _dimensions(comparison)


def test_proxy_refused_and_glue_also_failed_is_not_recorded() -> None:
    comparison = compare_results(_proxy_refusal(), _statement_failure())
    assert DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS not in _dimensions(comparison)


def test_glue_succeeded_without_a_proxy_refusal_is_not_recorded() -> None:
    comparison = compare_results(_success(), _success())
    assert DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS not in _dimensions(comparison)


# ---------------------------------------------------------------------------
# Near-misses (the distinctions task 10.2 generates over)
# ---------------------------------------------------------------------------


def test_zero_does_not_agree_with_false() -> None:
    """``bool`` is a subclass of ``int`` and ``0 == False``.

    The same trap ``tests/fixtures/run_test_result_shapes.py`` guards for the
    result shape: an ``isinstance``-based comparison would let a count silently
    become a flag, and a record that cannot tell ``duplicates: 0`` from
    ``duplicates: False`` is measuring the wrong thing.
    """
    assert not values_agree(0, False)
    comparison = compare_results(
        _success(data_quality_results=_dq(duplicates=0)),
        _success(data_quality_results=_dq(duplicates=False)),
    )
    assert _dimensions(comparison) == ["duplicates"]


def test_none_does_not_agree_with_the_empty_string() -> None:
    assert not values_agree(None, "")


def test_an_empty_mapping_does_not_agree_with_an_absent_key() -> None:
    assert not values_agree({}, ABSENT)
    comparison = compare_results(
        _success(data_quality_results=_dq(null_counts={})),
        _success(data_quality_results={k: v for k, v in _dq().items() if k != "null_counts"}),
    )
    entries = comparison["disagreements"]
    assert _dimensions(comparison) == ["null_counts"]
    # Absence is expressed by absence: the Glue side carries no value key.
    assert entries[0] == {"dimension": "null_counts", "pandas": {}}


def test_an_int_does_not_agree_with_an_equal_float() -> None:
    """A numeric type changing between executors is real movement (C7)."""
    assert not values_agree(100, 100.0)


def test_two_nans_agree() -> None:
    """``nan != nan``, but two executors both reporting NaN disagree about nothing."""
    assert values_agree(float("nan"), float("nan"))


def test_nested_near_misses_are_seen_through_containers() -> None:
    assert not values_agree({"a": 1}, {"a": True})
    assert not values_agree(["x"], ["x", "y"])
    assert values_agree({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]})


# ---------------------------------------------------------------------------
# Intentional asymmetries that must NOT be reported
# ---------------------------------------------------------------------------


def test_the_conversion_report_asymmetry_is_not_reported() -> None:
    """``conversion_report`` is ``None`` on every Glue path, by design.

    Task 8.1's decision: the Glue runner imports and calls no converter, and the
    key's documented meaning is "None when conversion failed or never ran". So a
    shadow run's two results differ on it every single time, informatively never.
    Comparing it would add one guaranteed entry per run that says only that the
    Glue path is the Glue path.
    """
    comparison = compare_results(
        _success(conversion_report={"constructs_converted": ["col", "when"]}),
        _success(conversion_report=None),
    )
    assert comparison["disagreements"] == []


def test_input_records_is_not_compared() -> None:
    """A frame-parity failure is a frame-builder defect, not an executor difference.

    Task 5.1 seeds the synthetic frame deterministically so both executors get
    the same frame, and Property 14 is what checks that. Filing a parity failure
    under "the executors disagreed" would attribute it to the wrong thing.
    """
    comparison = compare_results(_success(input_records=100), _success(input_records=50))
    assert comparison["disagreements"] == []


def test_free_text_failure_fields_are_not_compared() -> None:
    comparison = compare_results(
        _statement_failure(error_details="pandas: AttributeError", stderr="a"),
        _statement_failure(error_details="spark: AnalysisException", stderr="b"),
    )
    assert comparison["disagreements"] == []


# ---------------------------------------------------------------------------
# The cap and the omitted count (R4.5)
# ---------------------------------------------------------------------------


def test_omitted_is_zero_when_nothing_was_dropped() -> None:
    comparison = compare_results(
        _success(data_quality_results=_dq(null_counts={"a": 1})),
        _success(data_quality_results=_dq(null_counts={"a": 2})),
        max_entries=4,
    )
    assert len(comparison["disagreements"]) == 1
    assert comparison["omitted"] == 0


def test_the_cap_bounds_the_entries_and_reports_what_it_dropped() -> None:
    wide_left = {f"c{i:03d}": i for i in range(50)}
    wide_right = {f"c{i:03d}": i + 1 for i in range(50)}
    comparison = compare_results(
        _success(data_quality_results=_dq(null_counts=wide_left)),
        _success(data_quality_results=_dq(null_counts=wide_right)),
        max_entries=10,
    )
    assert len(comparison["disagreements"]) == 10
    assert comparison["omitted"] == 40
    # The total found is derived, not stored, so the two cannot drift.
    assert len(comparison["disagreements"]) + comparison["omitted"] == 50


def test_columns_survive_the_cap_in_a_deterministic_order() -> None:
    """Which columns are recorded must be a function of the inputs alone.

    Not of dict iteration order — otherwise the same pair of results could
    produce two different records, and a disagreement that appeared in one run
    and not the next would look like movement in the transform.
    """
    left = {"zeta": 1, "alpha": 1, "mid": 1}
    right = {"zeta": 2, "alpha": 2, "mid": 2}
    first = compare_results(
        _success(data_quality_results=_dq(null_counts=left)),
        _success(data_quality_results=_dq(null_counts=right)),
        max_entries=2,
    )
    reversed_insertion = {k: right[k] for k in reversed(list(right))}
    second = compare_results(
        _success(data_quality_results=_dq(null_counts={k: left[k] for k in reversed(list(left))})),
        _success(data_quality_results=_dq(null_counts=reversed_insertion)),
        max_entries=2,
    )
    assert _dimensions(first) == ["null_counts[alpha]", "null_counts[mid]"]
    assert _dimensions(first) == _dimensions(second)


def test_the_cap_never_crowds_out_the_headline_dimensions() -> None:
    """The priority order is what makes the cap safe.

    C7 predicts every column's dtype disagrees, so an unordered cap would let the
    least informative dimension push out ``execution_successful`` and
    ``proxy_refusal_glue_success`` — the two the retirement decision is made on.
    """
    wide = {f"c{i:03d}": i for i in range(300)}
    comparison = compare_results(
        _proxy_refusal() | {"data_quality_results": _dq(null_counts=wide)},
        _success(data_quality_results=_dq(null_counts={k: v + 1 for k, v in wide.items()})),
        max_entries=3,
    )
    assert _dimensions(comparison)[:3] == [
        "execution_successful",
        DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
        "output_records",
    ]
    assert comparison["omitted"] > 0


def test_the_default_cap_is_below_a_full_width_frames_worth_of_columns() -> None:
    """256 is half ``_MAX_TEST_FRAME_COLUMNS``, and that is the point.

    A full-width frame whose every column disagrees is then *guaranteed* to
    report a non-zero ``omitted`` rather than fitting inside the cap by luck, so
    a bounded record is always visibly bounded.
    """
    from code_interpreter_runner import _MAX_TEST_FRAME_COLUMNS

    assert MAX_RECORDED_DISAGREEMENTS < _MAX_TEST_FRAME_COLUMNS


def test_a_zero_cap_records_nothing_and_omits_everything() -> None:
    comparison = compare_results(
        _statement_failure(data_quality_results=_dq(), output_records=100),
        _success(),
        max_entries=-1,
    )
    assert comparison["disagreements"] == []
    assert comparison["omitted"] == 1


# ---------------------------------------------------------------------------
# The dimension that is presently unmeasurable
# ---------------------------------------------------------------------------


def test_dtypes_does_not_reach_a_result_dict_today() -> None:
    """C7's loudest prediction is not observable from a result dict.

    ``dtypes`` is in the emitted ``VALIDATION:`` payload on both sides, but
    ``_parse_results`` copies seven keys into ``data_quality_results`` and
    ``dtypes`` is not one of them (``code_interpreter_runner.py:626-634``). The
    Glue runner reuses that parser verbatim (``glue_session_runner.py:2573``), so
    the key is dropped identically on both paths, both sides read as absent, the
    absences agree, and the dimension records nothing.

    This also contradicts ``code_interpreter_runner.py:108`` and
    ``LGR-b2f79ef-12``, both of which say ``_parse_results`` puts *both*
    ``null_counts`` and ``dtypes`` into ``data_quality_results``. Only
    ``null_counts`` does.

    **When this test fails**, the parser has started carrying ``dtypes`` — which
    is a good change, not a regression. Update it and the note in
    ``shadow_comparison``'s module docstring together, and expect C7's
    every-column disagreement to start appearing.
    """
    output = 'GENERATED:{"record_count": 2}\nVALIDATION:{"record_count": 2, "dtypes": {"id": "int64"}}'
    parsed = CodeInterpreterTestRunner._parse_results(None, output)

    assert "dtypes" not in parsed["data_quality_results"]

    comparison = compare_results(
        _success(data_quality_results=_dq()),
        _success(data_quality_results=_dq()),
    )
    assert not any(DIMENSION_DTYPES in dimension for dimension in _dimensions(comparison))


def test_the_dimension_groups_partition_the_compared_list() -> None:
    """What makes ``compare_results``' dispatch total.

    Its last branch is an unguarded ``else``, so a dimension added to
    :data:`COMPARED_DIMENSIONS` without being put in a group would be silently
    treated as a per-column data-quality map — comparing the wrong thing rather
    than failing. This is the check that turns that into a test failure.
    """
    grouped = (
        set(_TOP_LEVEL_DIMENSIONS)
        | set(_DQ_SCALAR_DIMENSIONS)
        | set(_DQ_PER_COLUMN_DIMENSIONS)
        | {DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS}
    )
    assert grouped == set(COMPARED_DIMENSIONS)
    assert len(COMPARED_DIMENSIONS) == len(set(COMPARED_DIMENSIONS)) == 8


def test_dtypes_is_still_a_declared_dimension() -> None:
    """Kept in the list so the measurement starts working with no change here."""
    assert DIMENSION_DTYPES in COMPARED_DIMENSIONS


def test_dtypes_is_recorded_once_both_sides_carry_it() -> None:
    comparison = compare_results(
        _success(data_quality_results=_dq() | {"dtypes": {"id": "int64", "name": "object"}}),
        _success(data_quality_results=_dq() | {"dtypes": {"id": "bigint", "name": "string"}}),
    )
    assert _dimensions(comparison) == ["dtypes[id]", "dtypes[name]"]


# ---------------------------------------------------------------------------
# Purity — the hard constraint of this task
# ---------------------------------------------------------------------------


def test_the_module_imports_nothing_that_could_reach_aws_or_the_environment() -> None:
    """A pure function over two dicts (task 10.1's stated constraint).

    Checked on the import list rather than by patching, because what the task
    forbids is the *capability*: the state-row write is task 10.3's, and a
    ``boto3`` import here would be the first step towards it happening in the
    wrong place.
    """
    tree = ast.parse(_MODULE_PATH.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {"__future__", "math", "collections", "typing"}, imported


def test_the_module_reads_no_environment_variable_and_no_clock() -> None:
    """Scanned as identifiers, not as text.

    A substring scan of the source is what a first version did, and it failed on
    the module's own docstring — which names ``boto3`` and ``PutItem`` precisely
    to say they are not here. Reading the AST means the prose can keep
    explaining the constraint without tripping the test that enforces it.
    """
    tree = ast.parse(_MODULE_PATH.read_text())
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    forbidden = {
        "boto3",
        "client",
        "resource",
        "put_item",
        "environ",
        "getenv",
        "now",
        "utcnow",
        "monotonic",
    }
    assert referenced & forbidden == set(), referenced & forbidden


def test_comparing_does_not_mutate_either_argument() -> None:
    left = _success(data_quality_results=_dq(null_counts={"a": 1}))
    right = _success(data_quality_results=_dq(null_counts={"a": 2}))
    import copy

    left_before, right_before = copy.deepcopy(left), copy.deepcopy(right)
    compare_results(left, right)
    assert left == left_before
    assert right == right_before
