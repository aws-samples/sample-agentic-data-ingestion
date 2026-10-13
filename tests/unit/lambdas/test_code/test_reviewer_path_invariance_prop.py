# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: real-spark-verification, Property 18: The reviewer path is invariant under the execution result
"""Property 18 — the reviewer path is invariant under the execution result.

*For any* two execution results, the reviewer verdict check and the
``static_analysis:`` checks that ``aggregate_check_results`` derives from one
fixed ``parsed_results`` are identical — same entries, in the same positions,
byte for byte.

**Validates: Requirement 12.2**

Why this is a regression guard rather than a new claim
-----------------------------------------------------
Section 11 of the plan guards what must **not** change when a second executor is
introduced. R12's own derivation says why the invariance is written down as a
requirement instead of left implicit: an executor change is the most plausible
way to weaken the gate by accident.

The invariance is structural today. Read end to end,
``aggregate_check_results`` appends the ``ai_review_verdict`` entry and then the
``static_analysis:{category}`` entries from ``parsed_results`` alone
(``lambda_function.py:1106-1143`` as the function reads on disk at the time of
writing), and its **first** read of ``execution_results`` is the
``execution_results is None`` test immediately after (``:1145``). Nothing in the
reviewer half can see an execution result, so a Glue-shaped result cannot move
it. The spec located that seam at ``:445-484`` / ``:485``; the function has since
grown, which is exactly why nothing below addresses it by line number — every
assertion goes through the imported symbol.

What "the reviewer half" is, precisely — and the one trap
---------------------------------------------------------
``ai_review_verdict`` plus every ``static_analysis:*`` entry. It does **not**
include the computed ``schema_compliance`` check, and that distinction is a fence
rather than a detail: ``LGR-b2f79ef-1`` (**template / ratified**) records that
``schema_compliance`` is deliberately kept distinct from
``static_analysis:Schema Compliance`` — "one is a computed column-set comparison,
the other an LLM judgement, and merging them would lose both" — and the constant
carries the same warning at ``lambda_function.py:228-232``. The reviewer's
*opinion* on schema compliance is invariant under the execution result; the
*measurement* is not, and must not be. A filter that confused the two would
either report a false failure of this property or hide a real one, so
:func:`test_the_reviewer_schema_compliance_opinion_is_not_the_computed_check`
pins both halves of the distinction with the two entries side by side.

Both executor shapes are generated
----------------------------------
The arm vocabulary spans the pandas proxy and the Glue runner, because R12.2 has
to hold across the executor boundary and not merely across two pandas outcomes.
``tests/fixtures/run_test_result_shapes.py`` is the contract for both, and every
generated result is checked against it as it is built, so no assertion below can
pass on a dict neither ``run_test`` could return.

The documented asymmetry is honoured rather than papered over: the Glue path
returns ``conversion_report: None`` on **every** outcome (task 8.1), while the
fixture's per-outcome narrowings to ``dict`` describe the *pandas* call path
(``run_test:304``/``:316`` overwriting the builder's ``None``). Violations
mentioning that key are therefore excluded for Glue results only — the same
substring exclusion, for the same recorded reason, as
``test_shadow_disagreement_exactness_prop.py:334`` and
``test_result_shape_equivalence_prop.py:259``. The two Glue-only harness shapes
carry no width keys, so they are held to ``FRAME_REFUSAL`` minus those two keys,
derived from the fixture rather than written out — the derivation
``test_result_shape_equivalence_prop.py:165`` already makes.

Results come from the shipped producers wherever one exists
-----------------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**) — "a test written from the producer's
output cannot detect the producer drifting". Here the execution results are
*inputs* to the function under test rather than the oracle, so the record's rule
does not bind them the way it binds a shape table. It still points the right way,
so five of the seven shapes are produced by the real builders
(``_empty_results``, ``_failed_execution_results``, ``_conversion_failure_results``,
``_frame_truncation_results``, ``session_capacity_results``, ``_base_results``)
and a producer that renames a key fails here rather than passing against a stale
copy. The two hand-built ones are the successes, which need a parsed
``VALIDATION:`` payload to come out of ``_parse_results``; both are validated
against the declared ``SUCCESS`` shape.

Anti-vacuity, which is the real hazard in an invariance property
---------------------------------------------------------------
An invariance test passes trivially if nothing varies, if the filter matches
nothing, or if the reviewer half is empty. All three are closed:

* **The filter cannot silently match nothing.** The expected reviewer names are
  computed from the *input* ``parsed_results`` — one entry for the verdict, one
  per declared category — and asserted for equality, so renaming
  ``ai_review_verdict`` in the producer fails here instead of quietly emptying
  the compared list. There is no module constant for either name (both are
  literals at ``lambda_function.py:1129`` and ``:1139``), which is what makes
  that guard load-bearing rather than decorative.
* **The execution half provably moves.** Every example includes the
  ``execution_results is None`` arm alongside at least one concrete result, and
  :func:`test_the_execution_derived_half_moves_across_the_generated_arms`
  asserts the non-reviewer half takes at least two distinct values per example.
  Without it, "the reviewer half did not change" would be indistinguishable from
  "nothing changed".
* **The reviewer half is never empty.** Reviewer blocks are built through the
  shipped ``parse_review_verdict`` → ``verdict_to_parsed_results`` path, never
  transcribed, and always carry at least one category — the trap task 3.6 hit,
  recorded in ``test_harness_non_verification_prop.py``, where a transcribed
  block produced zero checks and left every assertion holding vacuously.

The input is also asserted **unmutated** after every call. A derivation that
wrote back into ``parsed_results`` would be a second way to break invariance
across a sequence of calls, and one that happens to be invisible to a
single-call test.

What this does NOT assert, deliberately
---------------------------------------
**That a successful execution cannot excuse a blocking static finding.** That is
Property 17 and lives in ``test_review_gate_calibration.py`` (task 11.1). This
property is about the *entries*, not about the status they derive.

**Anything about ``test_status``.** Varying the execution result is *supposed* to
move the status; only the reviewer-derived entries are invariant. Asserting on
the status here would contradict the point.

**Anything about the source text of** ``lambda_function.py``. It is being edited
concurrently, and a text assertion would pin formatting rather than behaviour.
Every claim here goes through the imported function.

Ledger provenance
-----------------
``why`` on ``lambda_function.py:1145`` (the first ``execution_results`` read)
returns only ``LGR-0176ccc-1`` (**backfilled**) — the documented 634-file
working-tree snapshot that matches nearly every query in this tree, whose body
does not name this function. No fence is specific to this seam.
``decisions(scope="…/agentic_data_ingestion_test_code")`` and
``decisions(concept="testing", include_backfilled=True)`` returned the records
that did govern choices here:

* ``LGR-b2f79ef-1`` (**template / ratified**) — the computed ``schema_compliance``
  check stays distinct from the reviewer's ``static_analysis:Schema Compliance``
  opinion. This is what the reviewer-half filter must get right.
* ``LGR-b2f79ef-2`` (**template / ratified**) — the three-state encoding reuses
  ``verified: False`` rather than adding a state, with the caveat that a
  ``verified: False`` entry must never be the only check. The reviewer entries
  carry no ``verified`` key and are always present, which is half of why that
  caveat holds; nothing here changes either.
* ``LGR-28b4d75-5`` (**template / ratified**) — producer output is not its own
  oracle. Why the shipped builders are called.
* ``LGR-3e45e65-1`` (**template / ratified**) — ``tests/unit/conftest.py``
  assigns ``AWS_REGION`` before any module in this directory imports, so the
  ``setdefault`` calls below are consistent no-ops rather than the thing
  choosing the region.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from tests.fixtures.run_test_result_shapes import (
    CLIENT_UNAVAILABLE,
    CONVERSION_REFUSAL,
    EXECUTOR_INDEPENDENT_OUTCOMES,
    FRAME_REFUSAL,
    LIFECYCLE_EXCEPTION,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

# Region/account before any boto3 import. LGR-3e45e65-1 pins the region in
# tests/unit/conftest.py, so these are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_test_code"
sys.path.insert(0, str(_TEST_CODE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "shared"))

# The REAL producers, imported BEFORE the patch.dict below: patch.dict restores
# sys.modules on exit, so these stay the real modules afterwards. Nothing here
# reaches a reason constant *through* `_mod` — the hazard the producers record at
# `lambda_function.SESSION_CAPACITY_REASON`, which is a second spelling precisely
# because a module that stubs `code_interpreter_runner` cannot import through it.
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    CodeInterpreterTestRunner,
)
from glue_session_runner import _base_results, session_capacity_results
from pyspark_to_pandas import ConversionError

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
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The reviewer half, named
# ---------------------------------------------------------------------------

#: The check ``aggregate_check_results`` appends for the reviewer's verdict.
#: A bare literal in the producer's verdict branch — unlike every other check
#: name in that function it has no module constant to import — which is why
#: :func:`_expected_reviewer_names` derives the expected names from the INPUT and
#: asserts equality: a rename there must fail loudly here rather than shrink the
#: compared list to nothing.
REVIEWER_VERDICT_CHECK_NAME: Final = "ai_review_verdict"

#: Prefix of the per-category reviewer checks, likewise a literal
#: (``f"static_analysis:{category}"``) in the producer's category loop.
STATIC_ANALYSIS_PREFIX: Final = "static_analysis:"


def _is_reviewer_derived(check: dict[str, Any]) -> bool:
    """Is *check* derived from ``parsed_results`` alone?

    Deliberately excludes the computed ``schema_compliance`` entry, which is a
    measurement over the execution's validation output and is named
    ``schema_compliance`` precisely so it cannot be confused with the reviewer's
    ``static_analysis:Schema Compliance`` opinion (``LGR-b2f79ef-1``).
    """
    name = check["name"]
    return name == REVIEWER_VERDICT_CHECK_NAME or name.startswith(STATIC_ANALYSIS_PREFIX)


def _reviewer_derived(checks: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """``(position, check)`` for every reviewer-derived entry, in list order.

    The position travels with the entry so that invariance covers *where* the
    entries are as well as *what* they are: an execution result that reordered
    the list would be a change to the reviewer path even with every entry intact.
    """
    return [(index, check) for index, check in enumerate(checks) if _is_reviewer_derived(check)]


def _execution_derived(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything the reviewer half is not — the part that is *meant* to move."""
    return [check for check in checks if not _is_reviewer_derived(check)]


def _expected_reviewer_names(parsed_results: dict[str, Any]) -> list[str]:
    """The reviewer entries *the input* implies, in the order the producer emits.

    Computed from ``parsed_results`` rather than read off the output, so this is
    an independent statement of what should be there.
    """
    names: list[str] = []
    if parsed_results.get("verdict") is not None:
        names.append(REVIEWER_VERDICT_CHECK_NAME)
    names.extend(f"{STATIC_ANALYSIS_PREFIX}{category}" for category in (parsed_results.get("test_categories") or {}))
    return names


# ---------------------------------------------------------------------------
# Contract-shaped execution results, per executor and outcome
# ---------------------------------------------------------------------------

#: The shape the two Glue-only harness non-verifications carry: ``FRAME_REFUSAL``
#: minus the two width keys, which a capacity refusal has no honest value for.
#: Derived from the fixture rather than written out — the same derivation, for the
#: same reason, as ``test_result_shape_equivalence_prop.py:165``.
_HARNESS_WITHOUT_A_WIDTH: Final = OutcomeShape(
    name="harness_non_verification_without_a_width",
    types={
        key: allowed
        for key, allowed in FRAME_REFUSAL.types.items()
        if key not in ("not_verified_field_count", "not_verified_frame_ceiling")
    },
    executor_independent=True,
    source="derived from FRAME_REFUSAL minus the two width keys — glue_session_runner.session_capacity_results",
)

#: A parsed ``VALIDATION:`` payload's six keys plus the two the score gate reads.
#: ``schema_compliance``'s presence is what "the execution produced validation
#: output" means to ``aggregate_check_results``, so a success arm carries it and
#: every failure/refusal arm carries ``{}`` — which is what the shipped builders
#: already return.
_PANDAS_DATA_QUALITY: Final[dict[str, Any]] = {
    "completeness_score": 95.0,
    "uniqueness_score": 100.0,
    "schema_compliance": 100.0,
    "overall_quality_score": 98.3,
    "null_counts": {"order_key": 5},
    "dtypes": {"order_key": "object"},
    "duplicates": 0,
    "missing_columns": [],
}

#: The Glue side's payload. Same key set (R10.1); different values, because C7
#: predicts ``dtypes`` disagreeing on every column. Values differ from the pandas
#: payload on purpose: identical payloads would make the executor axis invisible.
_GLUE_DATA_QUALITY: Final[dict[str, Any]] = {
    "completeness_score": 94.5,
    "uniqueness_score": 100.0,
    "schema_compliance": 66.7,
    "overall_quality_score": 87.1,
    "null_counts": {"order_key": 6},
    "dtypes": {"order_key": "string"},
    "duplicates": 0,
    "missing_columns": ["total_amount"],
}

_EXECUTOR_PANDAS: Final = "pandas"
_EXECUTOR_GLUE: Final = "glue"


def _with_conversion_report(result: dict[str, Any]) -> dict[str, Any]:
    """What ``run_test`` returns once conversion succeeded (``:304``/``:316``).

    The builders all return ``conversion_report=None``; three of the six pandas
    outcomes are overwritten by ``run_test`` before it returns. Reading the
    builders alone gets those three wrong, which the fixture's own docstring
    warns about, so the overwrite is reproduced here.
    """
    return {**result, "conversion_report": {"constructs_converted": ["col", "when"]}}


def _pandas_success() -> dict[str, Any]:
    """A transform that ran and validated, as ``_parse_results`` builds it.

    Hand-built rather than produced: ``_parse_results`` needs a parsed
    ``VALIDATION:`` payload in combined stdout, which is task 6.1's subject and
    not this property's. Validated against the declared ``SUCCESS`` shape.
    """
    return {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": copy.deepcopy(_PANDAS_DATA_QUALITY),
        "error_details": None,
        "conversion_report": {"constructs_converted": ["col", "when"]},
    }


def _glue_success() -> dict[str, Any]:
    """The same outcome on real Spark. ``conversion_report`` is None (task 8.1).

    Record counts differ from the pandas arm so the two successes are
    distinguishable in the execution half — a generator whose arms collapsed onto
    one another could not show the invariance holding across a real difference.
    """
    return _base_results(
        execution_successful=True,
        executed=True,
        exit_code=0,
        stderr="spark stderr",
        input_records=50,
        output_records=100,
        data_quality_results=copy.deepcopy(_GLUE_DATA_QUALITY),
        error_details=None,
    )


def _pandas_statement_failure() -> dict[str, Any]:
    return _with_conversion_report(
        CodeInterpreterTestRunner._failed_execution_results(
            "Transform execution",
            {
                "failure_reason": "AttributeError: 'Series' object has no attribute 'zip'",
                "stderr": "pandas traceback",
                "exit_code": 1,
            },
        )
    )


def _glue_statement_failure() -> dict[str, Any]:
    return _base_results(
        execution_successful=False,
        executed=True,
        exit_code=1,
        stderr="spark stderr",
        error_details="AnalysisException: [UNRESOLVED_COLUMN] `zip`",
    )


def _pandas_conversion_refusal() -> dict[str, Any]:
    """The one outcome a second executor must never produce (R3.2).

    Present because R12.2 quantifies over *any* execution result, and a refusal
    is the shape that replaces ``code_interpreter_execution`` with a differently
    named entry — the widest movement the execution half can make.
    """
    return CodeInterpreterTestRunner._conversion_failure_results(
        ConversionError("initcap", "df.withColumn('name', initcap(col('name')))", 3)
    )


def _pandas_frame_refusal(reason: str) -> dict[str, Any]:
    return _with_conversion_report(CodeInterpreterTestRunner._frame_truncation_results(640, 512, reason))


def _glue_capacity_refusal() -> dict[str, Any]:
    return session_capacity_results(
        ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "limit reached"}},
            "CreateSession",
        )
    )


def _pandas_client_unavailable() -> dict[str, Any]:
    return CodeInterpreterTestRunner._empty_results("bedrock-agentcore client unavailable")


def _glue_client_unavailable() -> dict[str, Any]:
    return _base_results(error_details="glue client unavailable")


def _pandas_lifecycle_exception() -> dict[str, Any]:
    """Same builder as ``client_unavailable``; ``run_test:316`` overwrites the
    report, which is the only thing distinguishing the two declared shapes."""
    return _with_conversion_report(CodeInterpreterTestRunner._empty_results("session lifecycle raised: boom"))


def _glue_lifecycle_exception() -> dict[str, Any]:
    return _base_results(error_details="glue session lifecycle raised: boom")


@dataclass(frozen=True)
class Arm:
    """One generated execution result, with the shape it must conform to."""

    label: str
    executor: str | None
    #: The fixture outcome this arm builds, or ``None`` for the absent arm.
    outcome: str | None
    result: dict[str, Any] | None
    shape: OutcomeShape | None


#: label → (executor, declared outcome, declared shape, builder).
#: The pandas half spans all six declared outcomes; the Glue half spans the five
#: executor-independent ones, with the proxy refusal absent by construction
#: (R3.2) and capacity standing in as its Glue-only harness refusal.
_ARM_BUILDERS: Final[dict[str, tuple[str, str, OutcomeShape, Any]]] = {
    "pandas/success": (_EXECUTOR_PANDAS, "success", SUCCESS, _pandas_success),
    "pandas/statement_failure": (_EXECUTOR_PANDAS, "statement_failure", STATEMENT_FAILURE, _pandas_statement_failure),
    "pandas/conversion_refusal": (
        _EXECUTOR_PANDAS,
        "conversion_refusal",
        CONVERSION_REFUSAL,
        _pandas_conversion_refusal,
    ),
    "pandas/client_unavailable": (
        _EXECUTOR_PANDAS,
        "client_unavailable",
        CLIENT_UNAVAILABLE,
        _pandas_client_unavailable,
    ),
    "pandas/lifecycle_exception": (
        _EXECUTOR_PANDAS,
        "lifecycle_exception",
        LIFECYCLE_EXCEPTION,
        _pandas_lifecycle_exception,
    ),
    "glue/success": (_EXECUTOR_GLUE, "success", SUCCESS, _glue_success),
    "glue/statement_failure": (_EXECUTOR_GLUE, "statement_failure", STATEMENT_FAILURE, _glue_statement_failure),
    "glue/client_unavailable": (_EXECUTOR_GLUE, "client_unavailable", CLIENT_UNAVAILABLE, _glue_client_unavailable),
    "glue/lifecycle_exception": (_EXECUTOR_GLUE, "lifecycle_exception", LIFECYCLE_EXCEPTION, _glue_lifecycle_exception),
    "glue/capacity_refusal": (_EXECUTOR_GLUE, "frame_refusal", _HARNESS_WITHOUT_A_WIDTH, _glue_capacity_refusal),
}

#: The pandas harness refusal is drawn with its reason, so it is built by the
#: composite below rather than by a nullary factory.
_PANDAS_FRAME_REFUSAL_LABEL: Final = "pandas/frame_refusal"

#: The two p33 width reasons, read off the producer's own constants.
_WIDTH_REASONS: Final = (NOT_VERIFIED_REASON_FRAME_TRUNCATED, NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA)

#: Every concrete arm. The absent arm is not here — it is added unconditionally.
_CONCRETE_ARM_LABELS: Final[tuple[str, ...]] = (*_ARM_BUILDERS, _PANDAS_FRAME_REFUSAL_LABEL)

#: ``execution_results is None`` — a legitimate input (the parameter is typed
#: ``dict | None``) and the arm that guarantees the execution half moves.
_ABSENT_ARM: Final = Arm(label="absent", executor=None, outcome=None, result=None, shape=None)


def _violations(result: dict[str, Any], shape: OutcomeShape, executor: str) -> list[str]:
    """Shape violations, excluding ``conversion_report`` for a Glue result.

    The Glue path returns ``None`` there on every outcome (task 8.1) while the
    fixture's narrowings to ``dict`` describe the pandas call path. Same
    exclusion, same reason, as ``test_shadow_disagreement_exactness_prop.py``.
    """
    violations = shape_violations(result, shape)
    if executor == _EXECUTOR_GLUE:
        return [violation for violation in violations if "conversion_report" not in violation]
    return violations


def _build_arm(label: str, width_reason: str) -> Arm:
    """Build *label*'s arm and assert it conforms to its declared shape."""
    if label == _PANDAS_FRAME_REFUSAL_LABEL:
        executor, outcome, shape, result = (
            _EXECUTOR_PANDAS,
            "frame_refusal",
            FRAME_REFUSAL,
            _pandas_frame_refusal(width_reason),
        )
    else:
        executor, outcome, shape, factory = _ARM_BUILDERS[label]
        result = factory()

    violations = _violations(result, shape, executor)
    assert not violations, (
        f"the {label!r} arm is not a dict run_test could return, so any property proved over it "
        f"would be proved over an unreachable input:\n  " + "\n  ".join(violations)
    )
    return Arm(label=label, executor=executor, outcome=outcome, result=result, shape=shape)


# ---------------------------------------------------------------------------
# Reviewer blocks, built by the shipped parsers
# ---------------------------------------------------------------------------

_CATEGORY_NAMES: Final = ["Correctness", "Security", "Schema Compliance", "Performance"]


@st.composite
def _reviewer_blocks(draw: st.DrawFn) -> dict[str, Any]:
    """A ``parsed_results`` block, via ``parse_review_verdict`` → ``verdict_to_parsed_results``.

    Never transcribed: a transcribed block produced ZERO checks during task 3.6's
    development, which is the vacuity this property cannot afford. At least one
    category is always drawn, so the reviewer half is never empty for want of an
    input.

    ``Schema Compliance`` is drawn deliberately often, because it is the category
    whose reviewer entry sits next to the *computed* ``schema_compliance`` check
    — the one distinction the reviewer-half filter has to get right
    (``LGR-b2f79ef-1``).

    Names come off a permutation rather than a ``unique=True`` list, which the
    sibling property measured as generating 197 invalid examples against 300
    valid ones. A ``fail`` verdict always carries an issue —
    ``parse_review_verdict`` rejects a failing verdict with nothing to act on.
    """
    count = draw(st.integers(min_value=1, max_value=3))
    if draw(st.booleans()):
        names = ["Schema Compliance", *draw(st.permutations([n for n in _CATEGORY_NAMES if n != "Schema Compliance"]))]
        names = names[:count]
    else:
        names = draw(st.permutations(_CATEGORY_NAMES))[:count]

    if draw(st.booleans()):
        raw: dict[str, Any] = {"verdict": "pass", "issues": [], "categories": dict.fromkeys(names, "pass")}
    else:
        verdict_passed = draw(st.booleans())
        issues: list[dict[str, Any]] = []
        if not verdict_passed or draw(st.booleans()):
            issues.append(
                {
                    "severity": draw(st.sampled_from(["error", "major", "minor"])),
                    "description": "generated issue",
                    "attribution": draw(st.sampled_from(["transform_block", "template_scaffolding"])),
                }
            )
        raw = {
            "verdict": "pass" if verdict_passed else "fail",
            "issues": issues,
            "categories": {name: ("pass" if draw(st.booleans()) else "fail") for name in names},
        }
    return _mod.verdict_to_parsed_results(_mod.parse_review_verdict(json.dumps(raw)))


_TARGET_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"order_key": {"type": "string"}, "total_amount": {"type": "number"}},
}


@dataclass(frozen=True)
class Case:
    """One fixed reviewer block and target schema, against several execution results."""

    parsed_results: dict[str, Any]
    #: ``dict`` | ``None`` | ``TARGET_SCHEMA_UNSPECIFIED`` — held FIXED while the
    #: execution results vary, because it is not the axis this property quantifies
    #: over and it does (legitimately) move the computed schema-compliance entry.
    target_schema: Any
    target_schema_label: str
    arms: tuple[Arm, ...]


@st.composite
def _cases(draw: st.DrawFn) -> Case:
    """A reviewer block, a target-schema state, and 2-5 execution results.

    The absent arm is always first, so every example contains at least two arms
    whose execution halves differ — see the module docstring on anti-vacuity.
    """
    parsed_results = draw(_reviewer_blocks())
    target_schema_label = draw(st.sampled_from(("resolved", "none", "unspecified")))
    target_schema = {
        "resolved": _TARGET_SCHEMA,
        "none": None,
        "unspecified": _mod.TARGET_SCHEMA_UNSPECIFIED,
    }[target_schema_label]

    width_reason = draw(st.sampled_from(_WIDTH_REASONS))
    count = draw(st.integers(min_value=1, max_value=4))
    labels = draw(st.permutations(_CONCRETE_ARM_LABELS))[:count]
    arms = (_ABSENT_ARM, *(_build_arm(label, width_reason) for label in labels))
    return Case(
        parsed_results=parsed_results,
        target_schema=target_schema,
        target_schema_label=target_schema_label,
        arms=arms,
    )


def _aggregate(case: Case, arm: Arm) -> tuple[str, list[dict[str, Any]]]:
    """``aggregate_check_results`` over one arm, with the case's fixed inputs."""
    return _mod.aggregate_check_results(case.parsed_results, arm.result, case.target_schema)


def _serialise(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Property 18
# ---------------------------------------------------------------------------


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_the_reviewer_derived_checks_are_invariant_under_the_execution_result(case: Case):
    """Property 18 — same reviewer entries, same positions, for every execution result.

    **Validates: Requirement 12.2**

    The comparison is over ``(position, entry)`` pairs, so a reordering counts as
    a change even with every entry intact. The expected *names* are derived from
    the input rather than from the first arm's output, so a producer rename fails
    here instead of emptying the compared list.
    """
    pristine = copy.deepcopy(case.parsed_results)
    expected_names = _expected_reviewer_names(case.parsed_results)
    assert len(expected_names) >= 2, (
        f"the generated reviewer block implies only {expected_names} — with fewer than a verdict plus "
        "one category this property would be quantified over almost nothing."
    )

    event(f"target_schema={case.target_schema_label}")
    baseline: list[tuple[int, dict[str, Any]]] | None = None
    baseline_label = ""

    for arm in case.arms:
        event(f"arm={arm.label}")
        _status, checks = _aggregate(case, arm)
        derived = _reviewer_derived(checks)

        assert [entry["name"] for _, entry in derived] == expected_names, (
            f"the {arm.label!r} arm produced reviewer entries "
            f"{[entry['name'] for _, entry in derived]}, but the input parsed_results implies "
            f"{expected_names}. Either the reviewer half no longer derives from parsed_results alone, "
            f"or one of the two literal names this file mirrors "
            f"({REVIEWER_VERDICT_CHECK_NAME!r}, {STATIC_ANALYSIS_PREFIX!r}) was renamed in the "
            f"producer — in which case this comparison would otherwise have gone vacuous.\n"
            f"full check list: {[check['name'] for check in checks]}"
        )

        if baseline is None:
            baseline, baseline_label = derived, arm.label
            continue

        assert derived == baseline, (
            f"R12.2 violated: the reviewer half changed between the {baseline_label!r} and "
            f"{arm.label!r} execution results, so the reviewer verdict / static_analysis derivation "
            f"is reading the execution result.\n"
            f"  {baseline_label}: {_serialise([entry for _, entry in baseline])}\n"
            f"  {arm.label}: {_serialise([entry for _, entry in derived])}"
        )

    assert case.parsed_results == pristine, (
        "aggregate_check_results mutated parsed_results, which is a second route to breaking "
        "invariance across a sequence of calls and one a single-call test cannot see."
    )


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_the_execution_derived_half_moves_across_the_generated_arms(case: Case):
    """Anti-vacuity: the part that is *meant* to vary does vary.

    **Validates: Requirement 12.2**

    Not a claim about the aggregator so much as a claim about this file: without
    it, "the reviewer half did not change" would be indistinguishable from
    "nothing changed", and Property 18 above would pass against a generator that
    had quietly collapsed onto one execution result.
    """
    portions = {_serialise(_execution_derived(_aggregate(case, arm)[1])) for arm in case.arms}
    assert len(portions) >= 2, (
        f"every one of the {len(case.arms)} generated execution results "
        f"({[arm.label for arm in case.arms]}) produced the same non-reviewer check entries, so the "
        "invariance asserted above held vacuously."
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_the_reviewer_half_leads_the_check_list(case: Case):
    """The reviewer entries occupy the front of the list, contiguously.

    **Validates: Requirement 12.2**

    This is *why* the invariance holds rather than an extra requirement: the
    entries are appended before the aggregator's first read of
    ``execution_results``, so they can only be a prefix. Pinning it behaviourally
    means a future edit that interleaved an execution-derived entry among them
    fails here — without this file asserting anything about
    ``lambda_function.py``'s source text, which a concurrent task is editing.
    """
    for arm in case.arms:
        _status, checks = _aggregate(case, arm)
        positions = [index for index, _ in _reviewer_derived(checks)]
        assert positions == list(range(len(positions))), (
            f"the {arm.label!r} arm put reviewer-derived entries at {positions} rather than at the "
            f"front of the list: {[check['name'] for check in checks]}"
        )


# ---------------------------------------------------------------------------
# Worked examples
# ---------------------------------------------------------------------------


def _all_pass_block(*categories: str) -> dict[str, Any]:
    return _mod.verdict_to_parsed_results(
        _mod.parse_review_verdict(
            json.dumps({"verdict": "pass", "issues": [], "categories": dict.fromkeys(categories, "pass")})
        )
    )


def test_a_pandas_success_and_a_glue_success_agree_on_the_reviewer_half():
    """The executor boundary itself, at one concrete point.

    Validates: Requirement 12.2
    """
    parsed = _all_pass_block("Correctness", "Security")

    pandas_checks = _mod.aggregate_check_results(parsed, _pandas_success(), _TARGET_SCHEMA)[1]
    glue_checks = _mod.aggregate_check_results(parsed, _glue_success(), _TARGET_SCHEMA)[1]

    assert _reviewer_derived(pandas_checks) == _reviewer_derived(glue_checks)
    # And the halves that are supposed to differ, do — otherwise the equality
    # above would be a statement about two identical inputs.
    assert _execution_derived(pandas_checks) != _execution_derived(glue_checks)


def test_absent_execution_results_leaves_the_reviewer_half_unchanged():
    """``execution_results is None`` is a legitimate input, not an error path.

    Validates: Requirement 12.2
    """
    parsed = _all_pass_block("Correctness", "Performance")

    absent_checks = _mod.aggregate_check_results(parsed, None, _TARGET_SCHEMA)[1]
    ran_checks = _mod.aggregate_check_results(parsed, _pandas_success(), _TARGET_SCHEMA)[1]

    assert _reviewer_derived(absent_checks) == _reviewer_derived(ran_checks)
    assert [check["name"] for check in _execution_derived(absent_checks)] == ["code_interpreter_execution"]


def test_the_reviewer_schema_compliance_opinion_is_not_the_computed_check():
    """``LGR-b2f79ef-1``'s distinction, with both entries side by side.

    The reviewer's ``static_analysis:Schema Compliance`` is invariant under the
    execution result; the computed ``schema_compliance`` score is not, and must
    not be — one is an LLM judgement, the other a column-set comparison over what
    the transform produced. A filter that merged them would make this property
    either falsely fail or falsely pass.

    Validates: Requirement 12.2
    """
    parsed = _all_pass_block("Schema Compliance")

    pandas_checks = _mod.aggregate_check_results(parsed, _pandas_success(), _TARGET_SCHEMA)[1]
    glue_checks = _mod.aggregate_check_results(parsed, _glue_success(), _TARGET_SCHEMA)[1]

    reviewer_opinion = f"{STATIC_ANALYSIS_PREFIX}Schema Compliance"
    computed = _mod.SCHEMA_COMPLIANCE_CHECK_NAME

    assert reviewer_opinion in [check["name"] for check in pandas_checks]
    assert computed in [check["name"] for check in pandas_checks]
    assert computed != reviewer_opinion

    # The opinion is in the reviewer half and identical across executors.
    assert [name for _, check in _reviewer_derived(pandas_checks) for name in (check["name"],)] == [
        REVIEWER_VERDICT_CHECK_NAME,
        reviewer_opinion,
    ]
    assert _reviewer_derived(pandas_checks) == _reviewer_derived(glue_checks)

    # The measurement is NOT in the reviewer half, and it did move: 100.0 with
    # nothing missing on the pandas payload, 66.7 with `total_amount` missing on
    # the Glue one.
    assert computed not in [check["name"] for _, check in _reviewer_derived(pandas_checks)]
    pandas_score = next(check for check in pandas_checks if check["name"] == computed)
    glue_score = next(check for check in glue_checks if check["name"] == computed)
    assert pandas_score["passed"] is True
    assert glue_score["passed"] is False
    assert pandas_score["detail"] != glue_score["detail"]


def test_a_parsed_results_carrying_no_verdict_still_yields_invariant_static_checks():
    """The verdict branch is conditional; the static loop is not.

    With no ``verdict`` key the reviewer half is the ``static_analysis:*`` entries
    alone, and it is still invariant. Built by hand rather than through
    ``verdict_to_parsed_results``, which always writes a verdict — this is the
    shape a caller that skipped the reviewer would pass.

    Validates: Requirement 12.2
    """
    parsed = {"test_categories": {"Correctness": {"status": "PASSED", "passed": True}}}

    for result in (None, _pandas_success(), _glue_success(), _pandas_conversion_refusal(), _glue_capacity_refusal()):
        checks = _mod.aggregate_check_results(parsed, result, _TARGET_SCHEMA)[1]
        assert [check["name"] for _, check in _reviewer_derived(checks)] == [f"{STATIC_ANALYSIS_PREFIX}Correctness"], [
            check["name"] for check in checks
        ]


def test_the_arm_vocabulary_covers_every_executor_independent_outcome():
    """A seventh declared outcome joins this property's input space, or fails here.

    The five ``EXECUTOR_INDEPENDENT_OUTCOMES`` must each be reachable as a Glue
    arm, and the proxy refusal must be reachable as a pandas one. An outcome added
    to the fixture with no arm here would silently narrow what R12.2 is proved
    over.
    """
    glue_outcomes = {
        outcome for _label, (executor, outcome, _shape, _factory) in _ARM_BUILDERS.items() if executor == _EXECUTOR_GLUE
    }
    undriven = sorted(set(EXECUTOR_INDEPENDENT_OUTCOMES) - glue_outcomes)
    assert not undriven, (
        f"{undriven} are declared executor-independent in tests/fixtures/run_test_result_shapes.py but "
        "no Glue arm below builds one, so Property 18 is not proved across them."
    )

    pandas_outcomes = {
        outcome
        for _label, (executor, outcome, _shape, _factory) in _ARM_BUILDERS.items()
        if executor == _EXECUTOR_PANDAS
    }
    assert "conversion_refusal" in pandas_outcomes
    assert "frame_refusal" not in pandas_outcomes, (
        "the pandas frame refusal is built by _build_arm (it draws its reason), not by _ARM_BUILDERS — "
        "if it moved, this assertion is the reminder to update the coverage claim above."
    )
    assert _PANDAS_FRAME_REFUSAL_LABEL in _CONCRETE_ARM_LABELS


def test_every_concrete_arm_builds_a_contract_shaped_result():
    """The generator's shape gate, exercised without hypothesis.

    ``_build_arm`` asserts conformance as it builds; running it over every label
    once means a broken arm fails as a named test rather than as a shrunk
    counterexample inside an unrelated property.
    """
    for label in _CONCRETE_ARM_LABELS:
        for reason in _WIDTH_REASONS:
            arm = _build_arm(label, reason)
            assert arm.result is not None
            assert arm.label == label
