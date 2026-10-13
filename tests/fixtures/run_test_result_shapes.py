# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Test_Result_Contract, declared: what ``run_test`` returns, per outcome.

p42 (real-spark-verification) task 1.2. This module is the **characterization
baseline** for requirement 1.1 and 1.2 — the key set, and the exact type of each
value, of every outcome shape
``CodeInterpreterTestRunner.run_test`` produces today
(``lambda/agentic_data_ingestion_test_code/code_interpreter_runner.py:235-317``).

**Why the table is authored rather than captured.** ``LGR-28b4d75-5`` (template)
gives the rule: "a test written from the producer's output cannot detect the
producer drifting, because the output is its own oracle". So the shapes below are
written out by hand from a read of the six return paths rather than snapshotted
from a run, and ``tests/contracts/api_responses/test_run_test_shape.py`` asserts
the live runner conforms to them.

Read that record before trusting this one too far: its chosen mechanism is to
parse an independent *consumer* declaration at test time, and it explicitly
rejects hand-transcription. That rejection does not transfer, because ``run_test``
has no consumer that declares its envelope — see the contract test's docstring
for the full argument. The residual hazard is real and worth naming: a runner
change plus a matching edit here passes. What the split buys is that such an edit
is a reviewable diff in a file that exists only to be the oracle.

**Why it is importable rather than a pile of assertions.** Requirement 1.2 is
checked for the *second* executor by Property 2 (task 8.2), which compares
``GlueSessionTestRunner.run_test`` against this table. "Same dict shape" is
unfalsifiable without a written-down shape to be the same as, which is what
these constants are.

Two things a reader of this table needs to know:

* **Types are exact, not ``isinstance``.** :func:`observed_shape` reports
  ``type(value)`` and :func:`shape_violations` compares by identity. That is
  deliberate: ``bool`` is a subclass of ``int``, so an ``isinstance`` check
  would let ``executed=True`` satisfy ``input_records: int`` and let a
  count-shaped key silently become a flag. The same trap ``helpers.py``'s
  ``matches_json_type`` guards for the wire format.
* **``conversion_report`` is the one key whose type is set by the CALL PATH,
  not by the return statement.** All five ``_*_results`` builders return
  ``None`` for it; ``run_test`` then overwrites it with the real report dict on
  every path that reached a successful conversion (``:304``, ``:316``). So the
  declared type here is what ``run_test`` returns, which is not what the
  builder it delegates to returns — read the builders alone and you will get
  three of these six wrong.

Source of each shape, all in ``code_interpreter_runner.py``:

===========================  ===========  ======================================
outcome                      run_test     builder
===========================  ===========  ======================================
``success``                  ``:307-317`` ``_parse_results`` (``:582``)
``statement_failure``        ``:307-317`` ``_failed_execution_results`` (``:746``)
``conversion_refusal``       ``:272-276`` ``_conversion_failure_results`` (``:644``)
``frame_refusal``            ``:290-305`` ``_frame_truncation_results`` (``:698``)
``client_unavailable``       ``:268-270`` ``_empty_results`` (``:774``)
``lifecycle_exception``      ``:307-317`` ``_empty_results`` (``:774``)
===========================  ===========  ======================================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

#: Spelled out because ``None`` is a value and ``type(None)`` is the type, and
#: the table below is a table of types.
NONE = type(None)


@dataclass(frozen=True)
class OutcomeShape:
    """One outcome's key set and per-key value type.

    ``types`` maps every key the outcome carries to the tuple of exact types
    that key's value may have. A single-element tuple means the type is fixed.

    ``executor_independent`` marks the outcomes a *second* executor must be
    able to produce in this same shape. It is False for exactly one outcome —
    the conversion refusal — because that outcome is the pandas proxy's own
    limitation and requirement 3.2 forbids the Glue runner from producing it at
    all. Property 2 (task 8.2) must therefore compare the Glue runner against
    :data:`EXECUTOR_INDEPENDENT_OUTCOMES`, not against every entry here;
    comparing it against the refusal shape would demand the marker R3.2
    prohibits.
    """

    name: str
    types: Mapping[str, tuple[type, ...]]
    executor_independent: bool
    source: str

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.types)


# ---------------------------------------------------------------------------
# The nine keys every outcome carries
# ---------------------------------------------------------------------------
# Every one of the six return paths sets all nine. They are listed once here so
# that a key added to the contract has one place to be added, and so that the
# per-outcome tables below carry only what differs.
#
# `executed` vs `execution_successful` are two distinct booleans on purpose and
# not a redundancy: `executed=False, execution_successful=False` is "nothing
# ran", `executed=True, execution_successful=False` is "it ran and failed".
# p32's whole posture depends on those being distinguishable.
_BASE: dict[str, tuple[type, ...]] = {
    "execution_successful": (bool,),
    "executed": (bool,),
    "exit_code": (int, NONE),
    "stderr": (str,),
    "input_records": (int,),
    "output_records": (int,),
    "data_quality_results": (dict,),
    "error_details": (str, NONE),
    "conversion_report": (dict, NONE),
}


def _shape(**overrides: tuple[type, ...]) -> dict[str, tuple[type, ...]]:
    """Return the base contract with *overrides* narrowed or added."""
    return {**_BASE, **overrides}


# ---------------------------------------------------------------------------
# The outcomes
# ---------------------------------------------------------------------------

#: A transform that ran and validated. R1.5's shape: `execution_successful`
#: true, `executed` true, `exit_code` zero. `error_details` is None — the
#: `_parse_results` branch that sets it to a string is the validation-error
#: variant, which shares this key set exactly.
SUCCESS = OutcomeShape(
    name="success",
    types=_shape(
        exit_code=(int,),
        error_details=(NONE,),
        conversion_report=(dict,),
    ),
    executor_independent=True,
    source="code_interpreter_runner.py:582-641 via run_test:307-317",
)

#: Code that RAN and failed — a non-zero exit code, or a traceback on stderr
#: after an exit 0. `executed` is True and that is the whole point of the
#: distinction from `client_unavailable`/`lifecycle_exception`.
STATEMENT_FAILURE = OutcomeShape(
    name="statement_failure",
    types=_shape(
        exit_code=(int,),
        error_details=(str,),
        conversion_report=(dict,),
    ),
    executor_independent=True,
    source="code_interpreter_runner.py:746-771 via run_test:307-317",
)

#: The pandas proxy could not express the transform. Five extra keys, and the
#: one outcome a second executor must NOT be able to produce (R3.2).
#:
#: `not_verified_line_number` is `int` and never None: `ConversionError`
#: declares it `int` (`pyspark_to_pandas.py:123`) and its single construction
#: site clamps `exc.lineno or 1` into range (`:1950-1962`), so no refusal
#: reaches here without one.
CONVERSION_REFUSAL = OutcomeShape(
    name="conversion_refusal",
    types=_shape(
        exit_code=(NONE,),
        error_details=(str,),
        # Returned BEFORE conversion succeeded, so run_test never overwrites it.
        conversion_report=(NONE,),
        not_verified_by_proxy=(bool,),
        not_verified_reason=(str,),
        not_verified_construct=(str,),
        not_verified_line_number=(int,),
        not_verified_source_line=(str,),
    ),
    executor_independent=False,
    source="code_interpreter_runner.py:644-696 via run_test:272-276",
)

#: The harness could not build a frame carrying every declared source field —
#: no source schema at all, or more fields than the 512-column ceiling.
#:
#: `conversion_report` is a dict here and None on the refusal above, and the
#: difference is real rather than incidental: conversion DID run and succeed on
#: this path, so `run_test:304` overwrites the builder's None.
#:
#: `data_quality_results` stays empty, which is load-bearing — it is what
#: suppresses the `schema_compliance` score for a frame the harness caused to be
#: short (C13). An empty dict is still a dict, so the type does not change.
FRAME_REFUSAL = OutcomeShape(
    name="frame_refusal",
    types=_shape(
        exit_code=(NONE,),
        error_details=(str,),
        conversion_report=(dict,),
        not_verified_by_harness=(bool,),
        not_verified_reason=(str,),
        not_verified_field_count=(int,),
        not_verified_frame_ceiling=(int,),
    ),
    executor_independent=True,
    source="code_interpreter_runner.py:698-743 via run_test:290-305",
)

#: No `bedrock-agentcore` client could be constructed, so `run_test` returns
#: before conversion is even attempted — which is why `conversion_report` is
#: None here and a dict on `lifecycle_exception`, despite both coming out of
#: `_empty_results`.
CLIENT_UNAVAILABLE = OutcomeShape(
    name="client_unavailable",
    types=_shape(
        exit_code=(NONE,),
        error_details=(str,),
        conversion_report=(NONE,),
    ),
    executor_independent=True,
    source="code_interpreter_runner.py:774-785 via run_test:268-270",
)

#: The sixth shape, and the one the task's list of five does not name: any
#: exception out of `_run_lifecycle` is caught at `run_test:309-311` and turned
#: into `_empty_results(str(exc))`, after which `:316` overwrites
#: `conversion_report` with the real report. Same key set as
#: `client_unavailable`, different `conversion_report` type.
#:
#: Recorded because the Glue runner has the same shape of path — task 3.1 puts
#: `StopSession`/`DeleteSession` in a `finally`, so an exception mid-lifecycle
#: returns rather than propagates — and Property 2 needs a shape to hold it to.
LIFECYCLE_EXCEPTION = OutcomeShape(
    name="lifecycle_exception",
    types=_shape(
        exit_code=(NONE,),
        error_details=(str,),
        conversion_report=(dict,),
    ),
    executor_independent=True,
    source="code_interpreter_runner.py:774-785 via run_test:307-317",
)


#: Every outcome, keyed by name. ``MappingProxyType`` because this is a shared
#: oracle: a test that mutated it would silently re-baseline every other test
#: that imports it.
RUN_TEST_OUTCOME_SHAPES: Mapping[str, OutcomeShape] = MappingProxyType(
    {
        shape.name: shape
        for shape in (
            SUCCESS,
            STATEMENT_FAILURE,
            CONVERSION_REFUSAL,
            FRAME_REFUSAL,
            CLIENT_UNAVAILABLE,
            LIFECYCLE_EXCEPTION,
        )
    }
)

#: The five the spec enumerates for task 1.2, in the order it lists them. Kept
#: distinct from the full mapping so that "the task asked for these five" stays
#: legible next to "reading the source found a sixth".
SPEC_ENUMERATED_OUTCOMES: tuple[str, ...] = (
    "success",
    "statement_failure",
    "conversion_refusal",
    "frame_refusal",
    "client_unavailable",
)

#: What Property 2 (task 8.2) compares the Glue runner against — every outcome
#: except the proxy's own refusal (R3.2).
EXECUTOR_INDEPENDENT_OUTCOMES: tuple[str, ...] = tuple(
    name for name, shape in RUN_TEST_OUTCOME_SHAPES.items() if shape.executor_independent
)

#: The nine keys present on every outcome — the Test_Result_Contract's core.
BASE_CONTRACT_KEYS: frozenset[str] = frozenset(_BASE)


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def observed_shape(result: Mapping[str, object]) -> dict[str, type]:
    """Return *result*'s key → exact-value-type map.

    Exact type, not ``isinstance``: see the module docstring on the ``bool``/
    ``int`` trap.
    """
    return {key: type(value) for key, value in result.items()}


def shape_violations(result: Mapping[str, object], shape: OutcomeShape) -> list[str]:
    """Return one human-readable string per way *result* departs from *shape*.

    Empty list means the result's key set and every value type match. Returning
    the whole list rather than raising on the first mismatch is deliberate: when
    a second executor is being brought up against this contract (task 8.2), the
    useful output is every key that is wrong at once, not the alphabetically
    first one.
    """
    observed = observed_shape(result)
    violations: list[str] = []

    for key in sorted(shape.keys - observed.keys()):
        violations.append(f"missing key {key!r} (contract: {_names(shape.types[key])})")
    for key in sorted(observed.keys() - shape.keys):
        violations.append(f"unexpected key {key!r} (observed {observed[key].__name__})")
    for key in sorted(shape.keys & observed.keys()):
        allowed = shape.types[key]
        if observed[key] not in allowed:
            violations.append(f"{key!r} is {observed[key].__name__}, contract says {_names(allowed)}")

    return violations


def _names(types: tuple[type, ...]) -> str:
    return " | ".join(t.__name__ for t in types)
