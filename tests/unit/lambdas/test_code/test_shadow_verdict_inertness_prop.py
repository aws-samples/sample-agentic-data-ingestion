# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 15 — shadow mode cannot change a verdict.

Spec ``real-spark-verification`` task 10.4. Requirements 4.1, 4.3 and 4.4,
design §4.7.

    *For any* transform and *any* Glue-side outcome — a contract-shaped result of
    any outcome type, a thrown exception of any kind raised at any reachable
    point, a deadline overrun, a capacity refusal, or nothing at all — the
    ``test_status`` and the check list produced under ``shadow`` are identical to
    those produced under ``pandas`` for the same transform and frame.

**Validates: Requirements 4.1, 4.3, 4.4**

**No Glue session is created anywhere in this file.** Every Glue-side outcome
arrives through the ``runner_factory`` seam or a patched attribute, and the write
goes to an injected ``writer``. Nothing here can reach a billable session, and
nothing here reaches DynamoDB.

What this adds over the example-based file next door
---------------------------------------------------

``test_shadow_orchestration.py`` (task 10.3) already pins the row shape, the
float→Decimal conversion, fail-soft at four raise sites, and the deadline in both
directions — as **worked examples**. What Property 15 adds is the *quantifier*:
the same guarantee over a generated Glue-side outcome, generated verdict inputs,
and a genuinely generated exception axis rather than one ``RuntimeError``. So this
file deliberately does not restate the row's shape, the ``Decimal`` conversion or
the exact status label at the deadline boundary; those are that file's subject and
task 10.5 extends them.

The five readings, and why one alone would not be enough
--------------------------------------------------------

R4.1 says the verdict derives from the pandas result *alone*; R4.3 says the
comparison lands where no gate reads it; R4.4 says a Glue-side failure returns the
same pair ``pandas`` would have. Together they are one claim about a pair, checked
five ways because each catches a different regression:

1. **Two independent runs agree.** The pair from a ``pandas`` run and the pair
   from a ``shadow`` run over deep-copied copies of the same inputs are
   bit-identical. This is the property as the design states it.
2. **The pair is not mutated across the dispatch.** Snapshotted immediately before
   ``record_shadow_verification`` and compared immediately after. Reading 1 would
   also catch an in-place mutation, but not tell you where it happened.
3. **Re-deriving after the shadow half gives the same pair.** The shadow half
   receives the authoritative ``execution_results`` dict itself, so it is in a
   position to mutate the verdict's *input* — a regression readings 1 and 2 would
   both miss, because the pair is already computed by then. The next run over that
   dict would silently disagree.
4. **A taint marker unique to the Glue side reaches the row and never the pair.**
   R4.3 is checkable as data flow rather than as "no gate function was called":
   the ``(run_id, "shadow_verification")`` row has no reader anywhere in the repo,
   so what has to be shown is that no Glue-derived *value* is reachable from the
   returned pair. Every generated Glue result carries
   :data:`GLUE_TAINT` in its ``error_details``, its ``stderr`` and — where the
   shape carries a data-quality payload — in a compared dimension. The marker
   appearing in the written row is what proves the arm was actually exercised;
   its absence from the serialised pair is R4.3.
5. **The call never raises and always returns a known label.** ``never raises`` is
   the contract the function's own docstring claims, and the label set is read off
   the module rather than restated, so a seventh ``SHADOW_STATUS_*`` cannot be
   added without a covering arm here.

Where the expected answer comes from
------------------------------------

There is no re-implementation of the verdict anywhere in this file: the oracle for
every reading is **the same producer run with the shadow half switched off**.
That is what makes "identical to the ``pandas`` run" checkable without a second
copy of ``aggregate_check_results`` to share a bug with the first. The deadline
boundary is deliberately *not* re-derived here — the status label at the boundary
is task 10.5's subject — so the general property asserts only that the label is
one the module declares, and a separate anti-vacuity test pins that the arm
vocabulary reaches every one of them.

Inputs are the shipped producers, not transcriptions
----------------------------------------------------

``parsed_results`` comes from ``parse_review_verdict`` →
``verdict_to_parsed_results``; the pandas results come from
``CodeInterpreterTestRunner``'s own builders; the Glue results come from
``glue_session_runner._base_results`` and ``session_capacity_results``. Every
generated result is checked against ``tests/fixtures/run_test_result_shapes.py``
as it is built, so no property here is proved over a dict ``run_test`` could not
return. ``conversion_report`` is excluded from the Glue side's check for the
reason ``test_reviewer_path_invariance_prop.py:511`` and
``test_shadow_disagreement_exactness_prop.py`` both record: the Glue path returns
``None`` there on every outcome (task 8.1), while the fixture's narrowings to
``dict`` describe the pandas call path.

Two honest limits, stated rather than hidden
--------------------------------------------

* **The exception axis is ``Exception`` subclasses, not ``BaseException``.**
  ``record_shadow_verification``'s guarantee is an ``except Exception``, so a
  ``KeyboardInterrupt`` or ``SystemExit`` out of the Glue half *would* propagate.
  Generating those would assert a guarantee the implementation does not make.
  Both are unreachable from a Glue client call in a Lambda or a container, so the
  gap is recorded here rather than closed.
* **The handler is not driven end-to-end.** Reaching Phase 2 needs a review
  verdict, a pipeline record and a generated-code artifact, which is why the
  sibling file asserts the *ordering* of the aggregation and the dispatch against
  the handler's own source instead (``test_shadow_orchestration.py``'s
  ``test_the_shadow_half_runs_after_the_verdict_is_computed``). This file composes
  the same two calls in that order and quantifies over what flows between them.
  So the structural fact that ``test_status`` and ``checks`` are bound *before*
  the dispatch is pinned there and relied on here.

Verified to have teeth, not just to be green
--------------------------------------------

Three mutations were applied to ``record_shadow_verification`` through a wrapper
during development — none of them committed, and ``lambda_function.py`` was not
edited — to check that each reading can go red and to find out which reading
covers what:

============================================  ==========================================
mutation                                      caught by
============================================  ==========================================
flips a value on the verdict's input dict     reading 3 only
writes a Glue-derived string onto that dict   reading 3, and only on an arm whose
                                              verdict actually reads the mutated key
removes the fail-soft arm (raises instead)    all five readings
============================================  ==========================================

Two things worth keeping from that. **Reading 3 is not redundant** — it is the
only reading that catches a shadow half which mutates the verdict's *input*,
because readings 1, 2 and 4 all compare a pair that was bound before the dispatch
ran. And the middle row is a correct "nothing caught it", not a hole: a Glue value
written onto ``error_details`` of a *successful* pandas result reaches no check,
because ``aggregate_check_results`` reads that key only on the failure branch
(``lambda_function.py:1326``). On a failing arm the same mutation is caught.

Ledger notes
------------

``LGR-60c4985-5`` (template / ratified) is the discipline this property
quantifies. Its words for the analogous resume hooks: "Both halves … **fail
soft**: neither can turn a working step into a failing one." Read in full — its
mechanism is that the behaviour lives in one place (the ``@step_handler``
decorator) rather than in ten copies, and the same shape holds here: one outer
``except Exception`` in one function, so there is one thing to quantify over
rather than a discipline spread across call sites.

``LGR-b46a5bb-1`` (**standing invariant**) is why the Glue half runs inline and
therefore why a wall-clock deadline exists at all: ``threading.Thread`` for
post-response work is forbidden in ``lambda/**`` because "Lambda freezes the
execution environment when the handler returns, so a thread doing the AgentCore
call is killed mid-flight". Its rejected alternative is recorded as *worse than
plainly broken* — a warm container occasionally let the thread finish, and
"non-deterministic success cannot be tested or relied on". That is the reason the
deadline cannot preempt a call in progress, which is why the pre-flight check asks
whether the whole worst case fits. ``LGR-b46a5bb-4`` scopes the repo-wide AST
guard to ``lambda/`` with **no allowlist**, "because an allowlist entry is a
door"; no outcome is exempted here for being awkward, which is why the
exception axis includes an exception whose ``__str__`` itself raises.

``LGR-60c4985-3`` is the reason a deadline is required rather than nice to have:
a ``waitForTaskToken`` step does not fail fast, so a stall "would hang for its
full ``TimeoutSeconds``". ``LGR-3e45e65-1`` is why this file performs no
environment dance for the region — ``tests/unit/conftest.py`` pins it.

``why`` on ``lambda_function.py`` returns four commits' worth: ``LGR-4dd9b42-1``
(the refusal whose revisit condition shadow mode exists to answer), the five
``LGR-69ca78b-*`` and the seven ``LGR-60c4985-*`` — both of those commits flagged
**BROAD** at 59 and 64 files, so they are background rather than fences on this
function — and two commits carrying **no records at all** (``LGR-0176ccc-1``'s
working-tree snapshot and the ``2b45b7291`` publication-cleanup commit). The
snapshot trap, exactly as documented. Nothing returned governs the shadow
orchestration, which is task 10.3's and still untracked: a gap, not an all-clear.
The one record in that set which does bear on this file is ``LGR-60c4985-5``, read
in full above. ``decisions(concept="testing", include_backfilled=True)`` returns
five records, all template; ``rootmap`` reports 122 records, all template
provenance, so ``include_backfilled=True`` changes nothing in this ledger.

Codegraph, with the coverage flags checked by hand
-------------------------------------------------

``record_shadow_verification`` (``lambda_function.py:796``), ``shadow_glue_run_fits``
(``:679``) and ``aggregate_check_results`` (``:1018``) each report **1 caller in
lambda_function.py** — verified by grep: the single call sites are ``:2628``,
``:875`` and ``:2588`` respectively, so the counts are right this time. All three
are flagged **"no covering tests"**, and all three are **false negatives**:
``record_shadow_verification`` is exercised by ``test_shadow_orchestration.py``
and ``test_verification_executor_selection.py``, ``shadow_glue_run_fits`` by
``test_shadow_orchestration.py``, and ``aggregate_check_results`` by 26 test files.
The cause is the one the spec's Notes record for ``lambda/**`` — ``sys.path``
manipulation plus bare-name imports, which the symbol graph does not follow.
Nothing in ``shared/`` is in the blast radius of what this file drives:
``record_shadow_verification`` reaches ``shadow_comparison`` (stdlib only) and,
through the seam, ``glue_session_runner`` — which is never constructed here.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. `tests/unit/conftest.py` already pins
# the region by assignment (`LGR-3e45e65-1`); these are the account id and the
# import-order guard the sibling handler tests use.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_test_code"
sys.path.insert(0, str(_TEST_CODE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "shared"))

# The REAL producers, imported BEFORE the `patch.dict` below: `patch.dict`
# restores `sys.modules` on exit, so these stay the real modules afterwards.
# Nothing here reaches a reason constant *through* `_mod` — the hazard the
# producers record at `lambda_function.SESSION_CAPACITY_REASON`, which is a
# second spelling precisely because a module that stubs
# `code_interpreter_runner` cannot import through it.
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    CodeInterpreterTestRunner,
)
from glue_session_runner import (
    NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    _base_results,
    session_capacity_results,
)
from pyspark_to_pandas import ConversionError
from tests.fixtures.run_test_result_shapes import (
    CLIENT_UNAVAILABLE,
    CONVERSION_REFUSAL,
    FRAME_REFUSAL,
    LIFECYCLE_EXCEPTION,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

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
# The taint marker
# ---------------------------------------------------------------------------

#: A string no producer emits, planted on every generated Glue-side result.
#:
#: Reading 4 in the module docstring: R4.3's guarantee is about data flow, so it
#: is checked by planting a value only the Glue half can have seen and asserting
#: it is unreachable from the returned pair. Deliberately not a UUID: a stable
#: literal means a failure message names the same string every time and the
#: example shrinks to something a reader can grep for.
GLUE_TAINT: Final = "glue-side-taint-4f2b9c"

#: Every ``SHADOW_STATUS_*`` label the module declares, read off the module
#: rather than restated, so a label added without a covering arm below fails
#: :func:`test_the_arm_vocabulary_reaches_every_declared_status` rather than
#: passing unquantified.
SHADOW_STATUSES: Final[frozenset[str]] = frozenset(
    value for name, value in vars(_mod).items() if name.startswith("SHADOW_STATUS_") and isinstance(value, str)
)

ROLE: Final = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"

#: What the handler passes as the shadow half's ``target_schema``: always a dict,
#: because the handler hands it ``target_schema_for_run`` (the ``{}``-coerced
#: value) rather than the resolved state the verdict is derived from
#: (``lambda_function.py:2588`` against ``:2634``). Held fixed here for that
#: reason — the target-schema *state* is a verdict-side axis, generated below.
_SHADOW_TARGET_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"order_key": {"type": "string"}, "total_amount": {"type": "number"}},
}

_TARGET_SCHEMA: Final[dict[str, Any]] = copy.deepcopy(_SHADOW_TARGET_SCHEMA)

_SOURCE_SCHEMA: Final[dict[str, Any]] = {
    "fields": [{"name": "order_key", "type": "string"}, {"name": "total_amount", "type": "double"}]
}


# ---------------------------------------------------------------------------
# Exact-type structural identity — this file's own comparator
# ---------------------------------------------------------------------------


def _identical(left: Any, right: Any) -> bool:
    """True when *left* and *right* are the same value, exactly.

    Exact type at every level and then equality, for the reason
    ``run_test_result_shapes`` gives for the same choice: ``bool`` subclasses
    ``int``, so ``0 == False`` and an ``isinstance``-based comparison would let
    ``passed: False`` and ``passed: 0`` read as the same check.

    **Written here rather than imported from** ``shadow_comparison.values_agree``,
    which does exactly this. That module is part of the change under test, so
    borrowing its comparator would make a broken comparator able to hide a broken
    verdict — the two failures would cancel. Nine lines is the price of an
    independent oracle.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_identical(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _serialise(pair: tuple[str, list[dict[str, Any]]]) -> str:
    """The pair as text, for the taint scan and for a legible failure message."""
    return json.dumps(pair, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Pandas-side results — the authoritative half, from the shipped builders
# ---------------------------------------------------------------------------

_PANDAS_DATA_QUALITY: Final[dict[str, Any]] = {
    "completeness_score": 95.0,
    "uniqueness_score": 100.0,
    "schema_compliance": 100.0,
    "overall_quality_score": 98.3,
    "null_counts": {"order_key": 5},
    "duplicates": 0,
    "missing_columns": [],
}


def _with_conversion_report(result: dict[str, Any]) -> dict[str, Any]:
    """What ``run_test`` returns once conversion succeeded (``:304``/``:316``).

    The builders all return ``conversion_report=None`` and ``run_test``
    overwrites three of the six; reading the builders alone gets those three
    wrong, which the fixture module's own docstring warns about.
    """
    return {**result, "conversion_report": {"constructs_converted": ["col", "when"]}}


def _pandas_success() -> dict[str, Any]:
    """A transform that ran and validated, in ``_parse_results``' shape.

    Hand-built rather than produced: ``_parse_results`` needs a parsed
    ``VALIDATION:`` payload in combined stdout, which is task 6.1's subject.
    Checked against the declared ``SUCCESS`` shape below.
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


def _pandas_conversion_refusal() -> dict[str, Any]:
    """The outcome a second executor must never produce (R3.2).

    Present on the *pandas* side because it is the shape that replaces
    ``code_interpreter_execution`` with a differently named entry — the widest
    movement the verdict's execution half makes — and because it is the
    population shadow mode exists to count (``LGR-4dd9b42-1``'s revisit
    condition).
    """
    return CodeInterpreterTestRunner._conversion_failure_results(
        ConversionError("initcap", "df.withColumn('name', initcap(col('name')))", 3)
    )


def _pandas_frame_refusal() -> dict[str, Any]:
    return _with_conversion_report(
        CodeInterpreterTestRunner._frame_truncation_results(640, 512, NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA)
    )


def _pandas_client_unavailable() -> dict[str, Any]:
    return CodeInterpreterTestRunner._empty_results("bedrock-agentcore client unavailable")


def _pandas_lifecycle_exception() -> dict[str, Any]:
    return _with_conversion_report(CodeInterpreterTestRunner._empty_results("session lifecycle raised: boom"))


#: label → (builder, declared shape). The absent arm (``execution_results is
#: None``) is added separately: it is a legitimate input (the parameter is typed
#: ``dict | None``) with no shape to conform to.
_PANDAS_ARMS: Final[dict[str, tuple[Any, OutcomeShape]]] = {
    "success": (_pandas_success, SUCCESS),
    "statement_failure": (_pandas_statement_failure, STATEMENT_FAILURE),
    "conversion_refusal": (_pandas_conversion_refusal, CONVERSION_REFUSAL),
    "frame_refusal": (_pandas_frame_refusal, FRAME_REFUSAL),
    "client_unavailable": (_pandas_client_unavailable, CLIENT_UNAVAILABLE),
    "lifecycle_exception": (_pandas_lifecycle_exception, LIFECYCLE_EXCEPTION),
}

_PANDAS_LABELS: Final[tuple[str, ...]] = (*_PANDAS_ARMS, "absent")


def _pandas_result(label: str) -> dict[str, Any] | None:
    """Build *label*'s pandas result, asserting it conforms to its shape."""
    if label == "absent":
        return None
    builder, shape = _PANDAS_ARMS[label]
    result = builder()
    violations = shape_violations(result, shape)
    assert not violations, (
        f"the pandas {label!r} arm is not a dict run_test could return, so any property "
        f"proved over it would be proved over an unreachable input:\n  " + "\n  ".join(violations)
    )
    return result


# ---------------------------------------------------------------------------
# Glue-side results — tainted, and checked against the same contract
# ---------------------------------------------------------------------------

#: The Glue side's data-quality payload. ``null_counts`` and ``missing_columns``
#: carry the taint because both are dimensions ``compare_results`` compares, so
#: the marker reaches the written row and reading 4's anti-vacuity half has
#: something to find. Values differ from the pandas payload on purpose: identical
#: payloads would make the executor axis invisible.
_GLUE_DATA_QUALITY: Final[dict[str, Any]] = {
    "completeness_score": 94.5,
    "uniqueness_score": 100.0,
    "schema_compliance": 66.7,
    "overall_quality_score": 87.1,
    "null_counts": {"order_key": 6, GLUE_TAINT: 1},
    "duplicates": 2,
    "missing_columns": [GLUE_TAINT],
}


def _glue_success() -> dict[str, Any]:
    return _base_results(
        execution_successful=True,
        executed=True,
        exit_code=0,
        stderr=f"spark stderr {GLUE_TAINT}",
        input_records=50,
        output_records=100,
        data_quality_results=copy.deepcopy(_GLUE_DATA_QUALITY),
        error_details=None,
    )


def _glue_statement_failure() -> dict[str, Any]:
    return _base_results(
        execution_successful=False,
        executed=True,
        exit_code=1,
        stderr=f"spark stderr {GLUE_TAINT}",
        error_details=f"AnalysisException: [UNRESOLVED_COLUMN] `zip` {GLUE_TAINT}",
    )


def _glue_client_unavailable() -> dict[str, Any]:
    return _base_results(error_details=f"glue client not available {GLUE_TAINT}")


def _glue_lifecycle_exception() -> dict[str, Any]:
    return _base_results(error_details=f"glue session lifecycle raised: {GLUE_TAINT}")


def _glue_capacity_refusal() -> dict[str, Any]:
    """R8.1's mapped refusal — a *result*, not a raise.

    The capacity refusal reaches the shadow half both ways and they are different
    events: mapped, the Glue half returned a Harness_Non_Verification and there is
    something to compare; raised, it produced nothing. Both are generated.
    """
    return session_capacity_results(
        ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": f"limit reached {GLUE_TAINT}"}},
            "CreateSession",
        )
    )


def _glue_frame_refusal() -> dict[str, Any]:
    """The harness refusal the Glue runner returns before creating a session."""
    return _base_results(
        error_details=f"no test frame could be built: {GLUE_TAINT}",
        not_verified_by_harness=True,
        not_verified_reason=NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    )


#: label → (builder, declared shape). ``returns_none`` is separate: a runner
#: returning ``None`` is the shape ``compare_results`` documents as a normal
#: input, and it has no contract shape to conform to.
_GLUE_RESULT_ARMS: Final[dict[str, tuple[Any, OutcomeShape]]] = {
    "glue_success": (_glue_success, SUCCESS),
    "glue_statement_failure": (_glue_statement_failure, STATEMENT_FAILURE),
    "glue_client_unavailable": (_glue_client_unavailable, CLIENT_UNAVAILABLE),
    "glue_lifecycle_exception": (_glue_lifecycle_exception, LIFECYCLE_EXCEPTION),
    "glue_capacity_refusal": (_glue_capacity_refusal, FRAME_REFUSAL),
    "glue_frame_refusal": (_glue_frame_refusal, FRAME_REFUSAL),
}

#: The Glue arms whose payload puts the taint inside a dimension
#: ``compare_results`` compares, so the marker must reach the written row.
_GLUE_ARMS_TAINTING_THE_ROW: Final[frozenset[str]] = frozenset({"glue_success"})


def _glue_shape_violations(result: dict[str, Any], shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding ``conversion_report``.

    The Glue path returns ``None`` there on every outcome (task 8.1) while the
    fixture's narrowings to ``dict`` describe the pandas call path. Same
    exclusion, same reason, as ``test_reviewer_path_invariance_prop.py:511`` and
    ``test_shadow_disagreement_exactness_prop.py``. The capacity and frame
    refusals additionally omit the two width keys ``FRAME_REFUSAL`` declares,
    which ``session_capacity_results``' docstring records as deliberate — carrying
    them as zeroes would render "the source schema declares 0 field(s)", a false
    claim about the schema.
    """
    return [
        violation
        for violation in shape_violations(result, shape)
        if "conversion_report" not in violation
        and "not_verified_field_count" not in violation
        and "not_verified_frame_ceiling" not in violation
    ]


def _glue_result(label: str) -> dict[str, Any]:
    """Build *label*'s Glue result, asserting it conforms to its shape."""
    builder, shape = _GLUE_RESULT_ARMS[label]
    result = builder()
    violations = _glue_shape_violations(result, shape)
    assert not violations, f"the Glue {label!r} arm is not a dict run_test could return:\n  " + "\n  ".join(violations)
    return result


# ---------------------------------------------------------------------------
# The exception axis
# ---------------------------------------------------------------------------


class _TaintedFailure(Exception):
    """A custom exception, because the Glue runner may raise one of its own."""


class _UnprintableFailure(Exception):
    """An exception whose ``__str__`` itself raises.

    The nastiest reachable shape, and generated rather than exempted:
    ``LGR-b46a5bb-4`` recorded that "an allowlist entry is a door" for the
    analogous repo-wide guard. The fail-soft claim is about the *pair*, so an
    exception that cannot even be logged must still leave the verdict alone —
    whether it is classified by the inner arm or caught by the outer one.

    **This input found a real hole, and the hole is now closed.** Kept, and its
    history kept with it, because it is the only input that can tell whether the
    fix is still in place.

    What it found: both fail-soft arms rendered the exception with ``%s``, so
    rendering it raised *inside the recovery path*. The inner arm survived that —
    the resulting ``RuntimeError`` is printable and the outer arm caught and
    logged it — but the OUTER arm's log call is the last statement before its
    ``return``, so a raise there escaped the function and defeated R4.4 and
    ``LGR-60c4985-5``'s guarantee that a shadow-side failure cannot turn a
    working step into a failing one. It was invisible in production and visible
    only in a test run, because the two ``handleError`` policies differ:

    * the stdlib ``StreamHandler`` — what the Lambda and AgentCore runtimes use —
      writes the failure to stderr and returns, so the escape never happened
      there;
    * ``_pytest.logging.LogCaptureHandler.handleError`` **re-raises** while
      ``logging.raiseExceptions`` is true, deliberately, so that a bad log call
      fails the test that made it.

    The fix, and it is the whole fix: both arms now render the exception with
    ``%r`` (``lambda_function.py:910`` and ``:967``). ``BaseException.__repr__``
    formats ``self.args`` and never reaches an overridden ``__str__``, so it
    cannot raise for this reason; ``type(exc).__name__`` stays ``%s`` because it
    is already a plain string.

    **What changed in this file as a result**, recorded here rather than only in
    a commit message, because ``LGR-60c4985-6`` (citing ``LGR-5906ec6-2``) is
    this repo's pattern that a deliberate hold lives as a test assertion, so
    lifting one means changing a test on purpose:

    * ``_lenient_logging()`` — a context manager that set
      ``logging.raiseExceptions = False`` so the affected arms ran under the
      runtime's policy instead of pytest's — is **removed**, along with the
      ``lenient_logging`` field on :class:`GlueOutcome` that selected it. It was
      a workaround for the hole; left in place it would mask a regression if the
      fix were ever reverted.
    * ``test_an_unrenderable_exception_still_returns_under_the_runtimes_logging_policy``
      asserted only the runtime direction and said in as many words that pinning
      the propagation would pin a wart. It is **replaced** by
      :func:`test_an_unrenderable_exception_is_fail_soft_under_the_strict_logging_policy`,
      which asserts the **strict** direction and carries its own anti-vacuity
      check. Strictly stronger, and the actual proof the fix holds.
    * the ``factory`` and ``run_test`` arms' ``expected_statuses`` **narrow** from
      ``{glue_produced_nothing, error}`` to ``{glue_produced_nothing}``. The
      second label was only ever reachable because the inner arm's own logging
      escaped into the outer one; it no longer can, so admitting it would be a
      weaker test than the code now supports.
    """

    def __str__(self) -> str:
        raise RuntimeError("this exception cannot be rendered")


#: Every generated exception, as ``(label, factory)``. Genuinely an axis rather
#: than one ``RuntimeError``: a ``ClientError`` (what botocore actually raises,
#: including the capacity refusal when it escapes unmapped), a ``TimeoutError``
#: (R4.4's named timeout as an exception rather than as a clock), the two
#: resource-exhaustion builtins, a ``KeyError`` whose ``str`` is quoted rather
#: than plain, a custom exception, and one that cannot be rendered at all.
#:
#: ``BaseException``-only classes (``KeyboardInterrupt``, ``SystemExit``) are
#: deliberately absent — see the module docstring's honest limits.
_EXCEPTIONS: Final[tuple[tuple[str, Any], ...]] = (
    ("RuntimeError", lambda: RuntimeError(f"boom {GLUE_TAINT}")),
    ("ValueError", lambda: ValueError(f"bad value {GLUE_TAINT}")),
    ("TypeError", lambda: TypeError(f"bad type {GLUE_TAINT}")),
    ("KeyError", lambda: KeyError(GLUE_TAINT)),
    ("AttributeError", lambda: AttributeError(f"no attribute {GLUE_TAINT}")),
    ("TimeoutError", lambda: TimeoutError(f"the session never reached READY {GLUE_TAINT}")),
    ("ImportError", lambda: ImportError(f"no glue_session_runner {GLUE_TAINT}")),
    ("OSError", lambda: OSError(f"connection reset {GLUE_TAINT}")),
    ("MemoryError", lambda: MemoryError()),
    ("RecursionError", lambda: RecursionError("maximum recursion depth exceeded")),
    (
        "ClientError_capacity",
        lambda: ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": f"limit {GLUE_TAINT}"}},
            "CreateSession",
        ),
    ),
    (
        "ClientError_access_denied",
        lambda: ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": f"not authorized {GLUE_TAINT}"}},
            "CreateSession",
        ),
    ),
    ("custom", lambda: _TaintedFailure(f"custom failure {GLUE_TAINT}")),
    ("unprintable", _UnprintableFailure),
)

#: The four reachable points an exception can come from, named as the task
#: enumerates them. ``comparison`` and ``item_build`` are both "the comparison"
#: — the pure function and the item builder — and they exercise different
#: statements inside the outer ``try``; the sibling file covers the second one as
#: a worked example, so the first is the addition here.
_RAISE_SITES: Final[tuple[str, ...]] = ("factory", "run_test", "comparison", "item_build", "write")

#: The two sites the inner ``except`` classifies, so the Glue half is recorded as
#: having produced nothing rather than as shadow mode having failed.
_INNER_ARM_SITES: Final[frozenset[str]] = frozenset({"factory", "run_test"})


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class _Recorder:
    """A writer seam that keeps what it was handed."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def __call__(self, item: dict[str, Any]) -> None:
        self.items.append(item)


class _RaisingWriter:
    """A writer seam that fails, so the write is a generated raise site.

    A ``PutItem`` rejection — a float that slipped through, a throttle, an item
    over 400 KB — loses the measurement. R4.4 says it must not lose the run.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self, item: dict[str, Any]) -> None:
        self.calls += 1
        raise self.exc


def _factory_returning(result: dict[str, Any] | None):
    """A ``runner_factory`` whose runner returns *result* from ``run_test``."""

    def factory(**kwargs: Any):
        runner = MagicMock()
        runner.run_test.return_value = result
        factory.calls.append(kwargs)  # type: ignore[attr-defined]
        return runner

    factory.calls = []  # type: ignore[attr-defined]
    return factory


def _factory_whose_runner_raises(exc: BaseException):
    def factory(**kwargs: Any):
        runner = MagicMock()
        runner.run_test.side_effect = exc
        factory.calls.append(kwargs)  # type: ignore[attr-defined]
        return runner

    factory.calls = []  # type: ignore[attr-defined]
    return factory


def _factory_raising(exc: BaseException):
    """Construction itself fails — the Glue client being unavailable."""

    def factory(**kwargs: Any):
        factory.calls.append(kwargs)  # type: ignore[attr-defined]
        raise exc

    factory.calls = []  # type: ignore[attr-defined]
    return factory


def _stepped_clock(*readings: float):
    """A monotonic-shaped clock returning each reading in turn, then the last.

    The same helper the sibling file uses, so the two files read the same way at
    the deadline; generalised here only in that the readings are generated.
    """
    values = list(readings)

    def clock() -> float:
        return values.pop(0) if len(values) > 1 else values[0]

    return clock


# ---------------------------------------------------------------------------
# One Glue-side outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlueOutcome:
    """One generated Glue-side outcome and how it is driven.

    ``expected_statuses`` is a *set* rather than a single label because this
    property is about the pair, not about the label: where more than one label is
    correct — the three deadline outcomes, whose boundary arithmetic is task
    10.5's subject — pinning one would be pinning a detail this file does not own.
    It is **not** a licence to leave a set wide once only one member is reachable:
    the unrenderable exception's arms admitted two labels while the fail-soft
    arms rendered with ``%s``, and narrowed to one when that was fixed (see
    :class:`_UnprintableFailure`).
    """

    label: str
    #: Extra kwargs merged into the ``record_shadow_verification`` call, last.
    kwargs: Mapping[str, Any]
    #: Dotted target patched to raise, or ``None``.
    raising_patch: str | None
    #: The exception the patched target raises.
    patch_exception: BaseException | None
    expected_statuses: frozenset[str]
    #: The written row must carry :data:`GLUE_TAINT`.
    row_carries_taint: bool

    def patches(self) -> ExitStack:
        stack = ExitStack()
        if self.raising_patch is not None:
            stack.enter_context(patch(self.raising_patch, MagicMock(side_effect=self.patch_exception)))
        return stack


def _result_outcome(label: str) -> GlueOutcome:
    return GlueOutcome(
        label=label,
        kwargs={"runner_factory": _factory_returning(_glue_result(label))},
        raising_patch=None,
        patch_exception=None,
        expected_statuses=frozenset({_mod.SHADOW_STATUS_COMPARED}),
        row_carries_taint=label in _GLUE_ARMS_TAINTING_THE_ROW,
    )


def _none_outcome() -> GlueOutcome:
    return GlueOutcome(
        label="returns_none",
        kwargs={"runner_factory": _factory_returning(None)},
        raising_patch=None,
        patch_exception=None,
        expected_statuses=frozenset({_mod.SHADOW_STATUS_COMPARED}),
        row_carries_taint=False,
    )


def _raising_outcome(site: str, exc_label: str, exc: BaseException) -> GlueOutcome:
    """An exception raised at one of the four reachable points.

    Every site's expected label is a **single** one, and the same one for every
    exception in the axis including the unrenderable one — which is a property of
    the fix, not of the harness. While both fail-soft arms rendered with ``%s``,
    the two inner-arm sites had to admit ``SHADOW_STATUS_ERROR`` as well, because
    the inner arm's own logging call could raise and fall through to the outer
    one; the arms now render with ``%r`` and it cannot. See
    :class:`_UnprintableFailure`.
    """
    label = f"raises_at_{site}:{exc_label}"
    if site == "factory":
        return GlueOutcome(
            label=label,
            kwargs={"runner_factory": _factory_raising(exc)},
            raising_patch=None,
            patch_exception=None,
            expected_statuses=frozenset({_mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING}),
            row_carries_taint=False,
        )
    if site == "run_test":
        return GlueOutcome(
            label=label,
            kwargs={"runner_factory": _factory_whose_runner_raises(exc)},
            raising_patch=None,
            patch_exception=None,
            expected_statuses=frozenset({_mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING}),
            row_carries_taint=False,
        )
    if site == "comparison":
        return GlueOutcome(
            label=label,
            kwargs={"runner_factory": _factory_returning(_glue_result("glue_success"))},
            raising_patch="shadow_comparison.compare_results",
            patch_exception=exc,
            expected_statuses=frozenset({_mod.SHADOW_STATUS_ERROR}),
            row_carries_taint=False,
        )
    if site == "item_build":
        return GlueOutcome(
            label=label,
            kwargs={"runner_factory": _factory_returning(_glue_result("glue_success"))},
            raising_patch="lambda_function.build_shadow_verification_item",
            patch_exception=exc,
            expected_statuses=frozenset({_mod.SHADOW_STATUS_ERROR}),
            row_carries_taint=False,
        )
    return GlueOutcome(
        label=label,
        kwargs={
            "runner_factory": _factory_returning(_glue_result("glue_success")),
            "writer": _RaisingWriter(exc),
        },
        raising_patch=None,
        patch_exception=None,
        expected_statuses=frozenset({_mod.SHADOW_STATUS_ERROR}),
        row_carries_taint=False,
    )


def _deadline_outcome(*, started: float, returned: float, budget: int) -> GlueOutcome:
    """A generated clock, so the timeout axis is not two hand-picked examples.

    The label is left off the expected status on purpose: which of the three
    deadline labels is correct is a function of the boundary arithmetic, and
    re-deriving that here would make this file a second copy of
    ``shadow_glue_run_fits``. What Property 15 needs is that the pair is
    unchanged whichever fires.
    """
    return GlueOutcome(
        label=f"deadline_{started:g}_{returned:g}_of_{budget}",
        kwargs={
            "runner_factory": _factory_returning(_glue_result("glue_success")),
            "clock": _stepped_clock(started, returned, returned),
            "deadline_seconds": budget,
        },
        raising_patch=None,
        patch_exception=None,
        expected_statuses=frozenset(
            {
                _mod.SHADOW_STATUS_COMPARED,
                _mod.SHADOW_STATUS_NOT_STARTED_DEADLINE,
                _mod.SHADOW_STATUS_ABANDONED_DEADLINE,
            }
        ),
        # The abandoned and not-started paths discard the Glue result, so the
        # taint may or may not reach the row. Not asserted either way here.
        row_carries_taint=False,
    )


def _no_role_outcome() -> GlueOutcome:
    return GlueOutcome(
        label="no_session_role",
        kwargs={
            "runner_factory": _factory_returning(_glue_result("glue_success")),
            "session_role_arn": "",
        },
        raising_patch=None,
        patch_exception=None,
        expected_statuses=frozenset({_mod.SHADOW_STATUS_NO_SESSION_ROLE}),
        row_carries_taint=False,
    )


_OUTCOME_FAMILIES: Final[tuple[str, ...]] = ("result", "none", "raises", "deadline", "no_role")


@st.composite
def _glue_outcomes(draw: st.DrawFn) -> GlueOutcome:
    """One Glue-side outcome, over every family the task enumerates."""
    family = draw(st.sampled_from(_OUTCOME_FAMILIES))
    if family == "result":
        return _result_outcome(draw(st.sampled_from(sorted(_GLUE_RESULT_ARMS))))
    if family == "none":
        return _none_outcome()
    if family == "raises":
        site = draw(st.sampled_from(_RAISE_SITES))
        exc_label, factory = draw(st.sampled_from(_EXCEPTIONS))
        return _raising_outcome(site, exc_label, factory())
    if family == "deadline":
        budget = draw(st.sampled_from((60, 660, 700, 900, 1200)))
        # Readings around the two boundaries — `budget - worst_case` for the
        # pre-flight check and `budget` for the post-flight one — plus values
        # well clear of both, so neither direction is reached only by luck.
        # Clamped at zero: elapsed time is measured from the step's monotonic
        # start, so a negative reading is not something a clock can produce —
        # and a budget below the runner's worst case (a legitimate operator
        # setting) would otherwise generate one.
        boundary = max(0.0, float(budget - _mod.SHADOW_GLUE_WORST_CASE_SECONDS))
        started = draw(st.sampled_from((0.0, 1.0, boundary, boundary + 0.5, float(budget), float(budget) + 100.0)))
        returned = draw(st.sampled_from((started, started + 1.0, float(budget), float(budget) + 0.5, 5000.0)))
        return _deadline_outcome(started=started, returned=returned, budget=budget)
    return _no_role_outcome()


# ---------------------------------------------------------------------------
# The verdict-side case
# ---------------------------------------------------------------------------

_CATEGORY_NAMES: Final[tuple[str, ...]] = ("Correctness", "Security", "Schema Compliance", "Performance")

#: Every ordered selection of one to three category names, precomputed.
#:
#: Enumerated rather than drawn from ``st.permutations``, which is a
#: ``unique=True`` list internally and so rejection-samples — the trap the
#: sibling property recorded ("197 invalid examples against 300 valid") and
#: reached for ``permutations`` to escape, one level too shallow. Forty
#: arrangements through ``sampled_from`` reject nothing and cover more than a
#: permutation prefix did.
#:
#: **What that did NOT fix, measured rather than assumed.**
#: ``--hypothesis-show-statistics`` still reports ~100 overrun examples against
#: 250 valid for the properties below, and the cause is not this strategy: it is
#: that :func:`_reviewer_blocks` makes a *variable number of draws* (one boolean
#: per selected category, and the issue block only on some paths), so mutating a
#: choice sequence generated for three categories into a one-category shape
#: leaves the mutation short of choices. Inherent to a composite with conditional
#: draws, shared by every sibling property in this directory, and harmless — the
#: full ``max_examples`` of *valid* examples still runs, at a cost of about half a
#: second of generation. Recorded so the next reader does not go looking for a
#: rejection filter that is not there.
_CATEGORY_SELECTIONS: Final[tuple[tuple[str, ...], ...]] = tuple(
    arrangement for size in (1, 2, 3) for arrangement in itertools.permutations(_CATEGORY_NAMES, size)
)


@st.composite
def _reviewer_blocks(draw: st.DrawFn) -> dict[str, Any]:
    """A ``parsed_results`` block, through the shipped parsers.

    Never transcribed — the sibling property records that a transcribed block
    produced ZERO checks during development, which is the vacuity this property
    cannot afford. At least one category is always drawn, so the reviewer half is
    never empty for want of an input, and a ``fail`` verdict always carries an
    issue because ``parse_review_verdict`` rejects a failing verdict with nothing
    to act on.
    """
    names = list(draw(st.sampled_from(_CATEGORY_SELECTIONS)))
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


@dataclass(frozen=True)
class Case:
    """The verdict-side inputs, held identical across the two executors."""

    parsed_results: dict[str, Any]
    pandas_result: dict[str, Any] | None
    pandas_label: str
    target_schema: Any
    target_schema_label: str


@st.composite
def _cases(draw: st.DrawFn) -> Case:
    parsed_results = draw(_reviewer_blocks())
    pandas_label = draw(st.sampled_from(_PANDAS_LABELS))
    target_schema_label = draw(st.sampled_from(("resolved", "none", "unspecified")))
    target_schema = {
        "resolved": copy.deepcopy(_TARGET_SCHEMA),
        "none": None,
        "unspecified": _mod.TARGET_SCHEMA_UNSPECIFIED,
    }[target_schema_label]
    return Case(
        parsed_results=parsed_results,
        pandas_result=_pandas_result(pandas_label),
        pandas_label=pandas_label,
        target_schema=target_schema,
        target_schema_label=target_schema_label,
    )


# ---------------------------------------------------------------------------
# Driving the two executors
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """What one executor's run of the step produced."""

    pair: tuple[str, list[dict[str, Any]]]
    #: The pair as it stood immediately before the shadow dispatch.
    pair_before_dispatch: tuple[str, list[dict[str, Any]]]
    #: The pair re-derived from the same inputs *after* the shadow half ran.
    pair_rederived: tuple[str, list[dict[str, Any]]]
    #: ``record_shadow_verification``'s label, or ``None`` under ``pandas``.
    shadow_status: str | None
    rows: list[dict[str, Any]]


def _run(case: Case, arm: GlueOutcome | None, *, executor: str) -> Run:
    """One run of the step's verdict half, with the shadow half per *executor*.

    Mirrors the handler's ordering, which is the structural reason R4.1 holds:
    ``aggregate_check_results`` at ``lambda_function.py:2588`` binds the pair, and
    the ``shadow`` dispatch at ``:2628`` runs afterwards. The sibling file pins
    that ordering against the handler's own source; this composes the same two
    calls in it.

    Inputs are deep-copied per run so the two executors share no object — without
    that, "the two runs agree" could be satisfied by both reading one mutated
    dict.
    """
    parsed_results = copy.deepcopy(case.parsed_results)
    pandas_result = copy.deepcopy(case.pandas_result)
    target_schema = copy.deepcopy(case.target_schema) if isinstance(case.target_schema, dict) else case.target_schema

    test_status, checks = _mod.aggregate_check_results(parsed_results, pandas_result, target_schema)
    before = copy.deepcopy((test_status, checks))

    recorder = _Recorder()
    shadow_status: str | None = None
    if executor == _mod.VERIFICATION_EXECUTOR_SHADOW:
        assert arm is not None
        kwargs: dict[str, Any] = {
            "run_id": "run-1",
            "pipeline_id": "pipe-1",
            "transform_block": "df = df.withColumn('total_amount', col('total_amount'))",
            "source_schema": copy.deepcopy(_SOURCE_SCHEMA),
            "target_schema": copy.deepcopy(_SHADOW_TARGET_SCHEMA),
            "pandas_result": pandas_result,
            "source_data_uri": "s3://bucket/key.json",
            "session_role_arn": ROLE,
            "step_started_at": 0.0,
            "clock": _stepped_clock(1.0),
            "writer": recorder,
        }
        kwargs.update(arm.kwargs)
        with arm.patches():
            shadow_status = _mod.record_shadow_verification(**kwargs)

    rederived = _mod.aggregate_check_results(parsed_results, pandas_result, target_schema)
    return Run(
        pair=(test_status, checks),
        pair_before_dispatch=before,
        pair_rederived=rederived,
        shadow_status=shadow_status,
        rows=list(recorder.items),
    )


def _describe(case: Case, arm: GlueOutcome) -> str:
    return f"pandas={case.pandas_label}, target_schema={case.target_schema_label}, glue={arm.label}"


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), arm=_glue_outcomes())
def test_shadow_mode_cannot_change_a_verdict(case: Case, arm: GlueOutcome):
    """R4.1/R4.4 — the pair under ``shadow`` is the pair under ``pandas``.

    Whatever the Glue half did: returned a contract-shaped result of any outcome
    type, returned nothing, raised any exception at any of four reachable points,
    overran its deadline, or was declined for want of a role.
    """
    pandas_run = _run(case, None, executor=_mod.VERIFICATION_EXECUTOR_PANDAS)
    shadow_run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)

    assert _identical(shadow_run.pair, pandas_run.pair), (
        f"shadow mode changed the verdict ({_describe(case, arm)}):\n"
        f"  pandas: {_serialise(pandas_run.pair)}\n"
        f"  shadow: {_serialise(shadow_run.pair)}"
    )


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), arm=_glue_outcomes())
def test_the_pair_is_not_mutated_across_the_shadow_dispatch(case: Case, arm: GlueOutcome):
    """The same guarantee localised: nothing the dispatch does touches the pair.

    Reading 2 in the module docstring. The property above would also fail on an
    in-place mutation of ``checks``; this says *where*.
    """
    run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
    assert _identical(run.pair, run.pair_before_dispatch), (
        f"the shadow dispatch mutated the pair in place ({_describe(case, arm)}):\n"
        f"  before: {_serialise(run.pair_before_dispatch)}\n"
        f"  after:  {_serialise(run.pair)}"
    )


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), arm=_glue_outcomes())
def test_the_shadow_half_leaves_the_verdicts_own_inputs_alone(case: Case, arm: GlueOutcome):
    """Reading 3 — the pair stays *re-derivable* after the shadow half.

    ``record_shadow_verification`` is handed the authoritative
    ``execution_results`` dict itself, so it is in a position to mutate the
    verdict's input rather than the verdict. Neither of the two properties above
    would notice: the pair is already computed by then. What would notice is the
    next derivation over that dict, which is what this performs.
    """
    run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
    assert _identical(run.pair_rederived, run.pair), (
        f"the shadow half mutated an input the verdict derives from ({_describe(case, arm)}):\n"
        f"  first:      {_serialise(run.pair)}\n"
        f"  re-derived: {_serialise(run.pair_rederived)}"
    )


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), arm=_glue_outcomes())
def test_no_glue_side_value_is_reachable_from_the_returned_pair(case: Case, arm: GlueOutcome):
    """R4.3 as data flow, with its own anti-vacuity half.

    Every generated Glue result carries :data:`GLUE_TAINT`. If any Glue-derived
    value reached the verdict, the marker would be in the serialised pair — so its
    absence is the check, and its *presence in the written row* is what proves the
    Glue outcome was actually consumed rather than the arm having been inert.

    The row itself needs no assertion beyond that: the
    ``(run_id, "shadow_verification")`` key has no reader anywhere in the repo,
    which task 10.3 verified by grep, so R4.3's guarantee holds by construction
    once nothing flows back into the pair.
    """
    run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)

    assert GLUE_TAINT not in _serialise(run.pair), (
        f"a Glue-side value reached the verdict ({_describe(case, arm)}): {_serialise(run.pair)}"
    )
    assert GLUE_TAINT not in str(run.shadow_status)

    if arm.row_carries_taint:
        assert run.rows, f"no row was written, so the Glue arm may have been inert ({_describe(case, arm)})"
        assert GLUE_TAINT in json.dumps(run.rows, sort_keys=True, default=str), (
            f"the Glue result never reached the comparison, so this example proves nothing "
            f"about it ({_describe(case, arm)})"
        )


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), arm=_glue_outcomes())
def test_the_shadow_half_returns_a_known_label_and_never_raises(case: Case, arm: GlueOutcome):
    """R4.4's mechanism: a label out, never an exception (``LGR-60c4985-5``).

    That the call returns at all is the assertion — an exception escaping would
    fail the test by propagating out of :func:`_run`. The label is then checked
    against the set the module declares, and against the narrower set this arm
    admits.
    """
    run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)

    assert run.shadow_status in SHADOW_STATUSES, (
        f"{run.shadow_status!r} is not a label the module declares ({_describe(case, arm)})"
    )
    assert run.shadow_status in arm.expected_statuses, (
        f"expected one of {sorted(arm.expected_statuses)}, got {run.shadow_status!r} ({_describe(case, arm)})"
    )


# ---------------------------------------------------------------------------
# Verified to have teeth, not just to be green
# ---------------------------------------------------------------------------


def _all_arms() -> list[GlueOutcome]:
    """Every arm the strategy can draw, built once for the coverage tests."""
    arms = [_result_outcome(label) for label in sorted(_GLUE_RESULT_ARMS)]
    arms.append(_none_outcome())
    arms.extend(_raising_outcome(site, label, factory()) for site in _RAISE_SITES for label, factory in _EXCEPTIONS)
    arms.append(_no_role_outcome())
    arms.append(_deadline_outcome(started=0.0, returned=1.0, budget=900))
    arms.append(_deadline_outcome(started=880.0, returned=881.0, budget=900))
    arms.append(_deadline_outcome(started=10.0, returned=1000.0, budget=900))
    return arms


def _plain_case() -> Case:
    """A case whose verdict is unambiguous, for the coverage tests."""
    return Case(
        parsed_results=_mod.verdict_to_parsed_results(
            _mod.parse_review_verdict(
                json.dumps({"verdict": "pass", "issues": [], "categories": {"Correctness": "pass"}})
            )
        ),
        pandas_result=_pandas_result("success"),
        pandas_label="success",
        target_schema=copy.deepcopy(_TARGET_SCHEMA),
        target_schema_label="resolved",
    )


def test_the_arm_vocabulary_reaches_every_declared_status():
    """A seventh ``SHADOW_STATUS_*`` cannot be added without a covering arm.

    The set is read off the module, so this fails on a label the generator cannot
    produce — which is the failure mode that would otherwise leave a new outcome
    silently unquantified. It is also the anti-vacuity check for the exception and
    deadline axes: without it, an arm that never reached the code under test would
    still pass every property above.
    """
    case = _plain_case()
    reached = {_run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW).shadow_status for arm in _all_arms()}
    assert reached == SHADOW_STATUSES, (
        f"unreached: {sorted(SHADOW_STATUSES - reached)}; extra: {sorted(reached - SHADOW_STATUSES)}"
    )


def test_every_raise_site_is_fail_soft_and_every_exception_type_is_exercised():
    """The four reachable raise sites the task enumerates, each over every
    exception in the axis.

    Asserted directly rather than left to the generator's sampling, so "an
    exception raised at the write is fail-soft" is a fact this file states rather
    than one it probably drew.
    """
    case = _plain_case()
    seen: set[tuple[str, str]] = set()
    for site in _RAISE_SITES:
        for exc_label, factory in _EXCEPTIONS:
            arm = _raising_outcome(site, exc_label, factory())
            run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
            assert run.shadow_status in arm.expected_statuses, f"{site}/{exc_label} → {run.shadow_status!r}"
            seen.add((site, exc_label))

    assert len(_EXCEPTIONS) >= 10, "the exception axis has collapsed towards a single type"
    assert seen == {(site, label) for site in _RAISE_SITES for label, _ in _EXCEPTIONS}


def test_the_two_inner_arm_sites_are_recorded_as_the_glue_half_producing_nothing():
    """The inner ``except`` is a *classifier*, not a second guard.

    A Glue-side failure at construction or in ``run_test`` is a normal outcome of
    this function and must read as "the Glue executor could not run this
    transform" — the fact the retirement decision needs — rather than as "shadow
    mode failed". Pinned with a renderable exception, because the unrenderable one
    is legitimately allowed to fall through to the outer arm.
    """
    case = _plain_case()
    for site in sorted(_INNER_ARM_SITES):
        arm = _raising_outcome(site, "RuntimeError", RuntimeError(f"boom {GLUE_TAINT}"))
        run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
        assert run.shadow_status == _mod.SHADOW_STATUS_GLUE_PRODUCED_NOTHING, site
        # The row is still written: "shadow ran and got nothing" and "shadow
        # never ran" are different facts and must not read the same.
        assert len(run.rows) == 1, site


def test_the_generated_cases_produce_more_than_one_verdict():
    """Anti-vacuity for the verdict axis.

    Every property above compares two pairs. If every generated case produced the
    same pair, they would all pass against an implementation that returned a
    constant. This asserts the pandas arms and reviewer blocks reach both statuses
    and several distinct check lists.
    """
    pairs = set()
    statuses = set()
    for pandas_label in _PANDAS_LABELS:
        for verdict in ("pass", "fail"):
            raw = {
                "verdict": verdict,
                "issues": (
                    []
                    if verdict == "pass"
                    else [{"severity": "error", "description": "generated issue", "attribution": "transform_block"}]
                ),
                "categories": {"Correctness": "pass", "Security": verdict},
            }
            case = Case(
                parsed_results=_mod.verdict_to_parsed_results(_mod.parse_review_verdict(json.dumps(raw))),
                pandas_result=_pandas_result(pandas_label),
                pandas_label=pandas_label,
                target_schema=copy.deepcopy(_TARGET_SCHEMA),
                target_schema_label="resolved",
            )
            run = _run(case, None, executor=_mod.VERIFICATION_EXECUTOR_PANDAS)
            statuses.add(run.pair[0])
            pairs.add(_serialise(run.pair))

    assert statuses == {"passed", "failed"}, f"the verdict axis reaches only {statuses}"
    assert len(pairs) >= len(_PANDAS_LABELS), "the pandas arms collapse onto too few distinct check lists"


def test_the_property_would_notice_a_verdict_the_shadow_half_had_touched():
    """The comparator and the taint scan can both go red.

    A green property is only evidence if it *can* fail, and readings 1, 2 and 4 all
    rest on :func:`_identical` and :func:`_serialise`. So a real pair is tampered
    with in the two ways a shadow half reaching the verdict would show up — an
    extra Glue-derived check, and a count silently becoming a flag — and both are
    asserted to be rejected. Without this, a comparator that always returned True
    would look identical from the outside.

    The complementary half of that argument — the properties failing when the
    *producer* is broken rather than the pair — is recorded in the module
    docstring's teeth table, measured with three wrappers around
    ``record_shadow_verification`` that are deliberately not committed.
    """
    case = _plain_case()
    pandas_run = _run(case, None, executor=_mod.VERIFICATION_EXECUTOR_PANDAS)
    tampered = copy.deepcopy(pandas_run.pair)
    tampered[1].append({"name": "shadow_disagreement", "passed": False, "detail": GLUE_TAINT})

    assert not _identical(tampered, pandas_run.pair)
    assert GLUE_TAINT in _serialise(tampered)
    # And the subtler one the exact-type rule exists for: a count becoming a flag.
    flipped = copy.deepcopy(pandas_run.pair)
    flipped[1][0]["passed"] = 1 if flipped[1][0]["passed"] is False else 0
    assert not _identical(flipped, pandas_run.pair)


def test_the_glue_arms_are_contract_shaped_and_carry_the_taint():
    """Every Glue arm is a dict ``run_test`` could return, and is tainted.

    Two failures this catches: an arm drifting off the Test_Result_Contract, so a
    property would be proved over an input no executor produces; and an arm losing
    its taint, which would make reading 4 pass by having nothing to find.
    """
    for label in _GLUE_RESULT_ARMS:
        result = _glue_result(label)  # asserts the shape as it builds
        assert GLUE_TAINT in json.dumps(result, sort_keys=True, default=str), label


def test_every_arm_supplies_the_runner_factory_seam():
    """The billing fence, asserted rather than trusted.

    Omitting ``runner_factory`` falls through to
    ``_default_shadow_glue_runner``, which imports and constructs the real
    ``GlueSessionTestRunner`` — a billable interactive session. Every arm must
    therefore supply the seam, and an arm added without one would otherwise pass
    every property above while creating sessions.
    """
    for arm in _all_arms():
        assert "runner_factory" in arm.kwargs, arm.label
        assert callable(arm.kwargs["runner_factory"]), arm.label

    # And the production factory is never the one that runs: asserted on the
    # module's own attribute rather than on this file's text, because a source
    # scan for the runner's name matches the sentence above that explains it —
    # the trap `LGR-b46a5bb-4` recorded for substring matching, which "forbids
    # documenting the fence".
    with patch.object(_mod, "_default_shadow_glue_runner", MagicMock(side_effect=AssertionError)) as production:
        for arm in _all_arms():
            _run(_plain_case(), arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
    production.assert_not_called()


def test_an_unrenderable_exception_is_fail_soft_under_the_strict_logging_policy(caplog):
    """The "never raises" claim no longer depends on the logging handler.

    **Replaces** ``…_still_returns_under_the_runtimes_logging_policy``, which
    asserted only the runtime direction because the strict one genuinely failed:
    both fail-soft arms rendered the exception with ``%s``, so an exception whose
    ``__str__`` raises made the *recovery path* a raise site, and the outer arm's
    log call is the last statement before its ``return``. The arms now use ``%r``
    (``lambda_function.py:910``, ``:967``), so this asserts the **strict**
    direction — the harder one, and the only one that is evidence the fix holds.
    ``LGR-60c4985-6``, citing ``LGR-5906ec6-2``: lifting a hold encoded as a test
    assertion means changing the test on purpose, so the reversal is recorded
    here and in :class:`_UnprintableFailure` rather than left to a diff.

    No ``logging.raiseExceptions`` dance and no lenient context manager: the
    handler pytest installs re-raises a failed log call on purpose, which is
    exactly the policy this should be measured under. Every one of the
    :data:`_RAISE_SITES` returns a label and leaves the pair untouched.
    """
    # The harness has teeth, asserted rather than assumed: a green strict test is
    # only evidence if this policy would actually surface a bad log call. Same
    # logger, same handlers, one operand rendered the old way — and it raises,
    # which is what the arms would still do had they kept `%s`.
    assert logging.raiseExceptions is True, "pytest's re-raising handleError is gated on this"
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        _mod.logger.warning("anti-vacuity, rendered the old way: %s", _UnprintableFailure())
    # That record is in `caplog.records` and its `getMessage()` cannot be called,
    # so it is dropped before the scan below rather than poisoning it.
    caplog.clear()

    case = _plain_case()
    with caplog.at_level(logging.WARNING):
        for site in _RAISE_SITES:
            arm = _raising_outcome(site, "unprintable", _UnprintableFailure())
            run = _run(case, arm, executor=_mod.VERIFICATION_EXECUTOR_SHADOW)
            # Never raises (R4.4, `LGR-60c4985-5`), and lands on the one label
            # the site is now allowed — not "either of two".
            assert run.shadow_status in arm.expected_statuses, site
            assert len(arm.expected_statuses) == 1, site
            # An unrenderable exception is not a special case any more: it lands
            # exactly where a printable one does.
            printable = _raising_outcome(site, "RuntimeError", RuntimeError(f"boom {GLUE_TAINT}"))
            assert arm.expected_statuses == printable.expected_statuses, site
            # The point of the whole exercise: the verdict is untouched.
            assert _identical(run.pair, run.pair_before_dispatch), site

    # And the arms really did log — so the `%r` operand was exercised, not
    # skipped by a level filter that would make the whole test vacuous. Matched
    # on the raw template rather than on `getMessage()`: interpolation is the
    # thing under test here, so the scan must not depend on it.
    logged = [record for record in caplog.records if "shadow verification" in str(record.msg)]
    assert logged, "no arm logged, so the rendering was never exercised"
    # Both arms, not just one: the inner classifier and the outer guarantee.
    assert any("the Glue half raised" in str(record.msg) for record in logged)
    assert any("failed and was skipped" in str(record.msg) for record in logged)
    # Every captured line renders — the assertion that would have gone red before
    # the fix, stated on the records themselves rather than on a return value.
    for record in logged:
        assert record.getMessage()
