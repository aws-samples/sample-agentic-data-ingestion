# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The fence's own invariants and the reason plumbing — p40 task 4.4.

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/`. Requirements **R2** and
**R4** (`design.md` §1); design §4.1 (the fence) and §4.4 (a refusal that
carries its reason); binding constraint **B9** (`design.md` §3).

WHAT THIS FILE HOLDS, AND WHY IT IS SEPARATE FROM THE 1.x SHAPE FILES.
`test_literal_receiver_cast.py`, `..._string_functions.py` and
`..._composition.py` assert the *outcomes* the ratified line gives each defect
shape. This file asserts the two things that hold *whatever* the line is:

  1. **The exception's shape did not move.** `ConversionError` gained a fourth
     field, `reason`, and the whole of B9 is that it gained it *additively* —
     optional, keyword-able, defaulted to `None`, and LAST. There are 13 call
     sites and 23 construction/reference sites (`design.md` Appendix A), all
     passing three positional arguments, and `str(exc)` is what an operator
     reads in `_conversion_failure_results`' `error_details`
     (`code_interpreter_runner.py:627`). Reordering the parameters or folding
     the reason into the message would break both silently — nothing else in
     the suite pins the parameter ORDER, only the behaviour that happens to use
     it.
  2. **A fence refusal cannot be mistaken for a permanent one.** The reason
     rides on the exception rather than on the construct name precisely because
     the fence's constructs — `cast`, `trim`, `lower`, `upper`,
     `regexp_replace`, `concat_ws`, `to_date`, `alias` — are the same names a
     legitimate not-yet-expressible refusal uses (`ConversionError`'s docstring;
     design §4.4). The corollary the design verified by reading, and this file
     asserts, is that none of those eight is in
     `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS` (`code_interpreter_runner.py:164-186`),
     so a fence refusal can never come out labelled "nothing to fix, ever".
     (p41 task 9.3 corrected this citation: it read `:116-139`, which was already
     stale, and task 9.2's +27 comment lines moved the set again. **No assertion
     in this file was amended.** p41 2.7 removed `initcap` from the set, and this
     file consumes the set BY IMPORT, so the disjointness assertion tracked that
     automatically — shrinking the permanent set can only reduce collisions with
     the fence's eight names, never create one.)

WHAT THE DISJOINTNESS ASSERTION DOES AND DOES NOT CATCH. It catches an addition
to `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS` that collides with a name the fence
already uses — the direction that would silently relabel an existing refusal.
It cannot catch a NEW guard site whose construct is on the permanent list,
because a name no test shape produces is a name this file cannot see; the
eight-name set below is therefore stated from design §4.1 and cross-checked
against the shapes, not derived from the source. The class-level cover for a new
guard site is task 12.1's generative property.

ORACLE RULE FOR THIS FILE. Expectations come from `design.md` §4.1/§4.4 and
from the exception's PRE-p40 rendered message, which is a preservation surface
(`bugfix.md` 3.10). It is neither of the two rules that face each other in this
directory: nothing here is transcribed from what the converter emits today
(task 2.1's rule), and nothing here needs Spark semantics either (tasks 3.1-3.3'
rule), because the subject is the refusal's own shape rather than a translation.

EXPECTED-RED WINDOW: closed. This file was written after tasks 4.2 and 4.3
landed, so every assertion is expected GREEN on its first run. A red here is a
finding about 4.1/4.2/4.3, not a test to adjust.

**Validates: Requirements R2, R4**
"""

import inspect
import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
from code_interpreter_runner import (
    _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS,
    NOT_VERIFIED_REASON_NOT_YET,
    classify_conversion_refusal,
)
from pyspark_to_pandas import (
    REFUSAL_REASON_SCALAR_LITERAL,
    ConversionError,
    PySpark2PandasConverter,
    _Unconvertible,
)

#: The eight constructs the receiver-shape fence can name, from `design.md` §4.1
#: — the construct that OWNS the member, never the `lit(...)` that produced the
#: scalar. One per guard site, `alias`/`cast` covering more than one emission:
#: `trim`/`lower`/`upper` (`pyspark_to_pandas.py:762`), `regexp_replace` (`:773`),
#: `concat_ws` (`:794`, literal parts only), `to_date` (`:859`), `alias` (`:889`),
#: `cast` (`:942`, both emissions guarded on the receiver).
_FENCE_CONSTRUCTS = frozenset({"cast", "trim", "lower", "upper", "regexp_replace", "concat_ws", "to_date", "alias"})

#: One refused input per fence construct. Each is valid Spark, and each is
#: refused under the ratified line in `design.md` §4.2 — the `cast` case is a
#: NUMERIC target on purpose, because the string/StringType cast of a `None` or
#: `str` literal is the one family that CONVERTS (Arm A, task 5.1) and would not
#: exercise the fence at all.
_REFUSED_BY_THE_FENCE = [
    ("cast", 'df_transformed = df.withColumn("a", lit(0).cast("int"))'),
    ("trim", 'df_transformed = df.withColumn("a", trim(lit("x")))'),
    ("lower", 'df_transformed = df.withColumn("a", lower(lit("x")))'),
    ("upper", 'df_transformed = df.withColumn("a", upper(lit("x")))'),
    ("regexp_replace", 'df_transformed = df.withColumn("a", regexp_replace(lit("x"), "x", "y"))'),
    ("concat_ws", 'df_transformed = df.withColumn("a", concat_ws("-", lit("x"), col("b")))'),
    ("to_date", 'df_transformed = df.withColumn("a", to_date(lit("2020-01-01"), "yyyy-MM-dd"))'),
    ("alias", 'df_transformed = df.withColumn("a", lit(None).alias("z"))'),
]

#: Two refusals that predate p40 and must keep classifying by construct name:
#: one on the permanent list, one an unrecognised bare name in function position
#: (`pyspark_to_pandas.py:830`, the `posexplode` hole p32 closed).
_REFUSED_WITHOUT_A_REASON = [
    ("groupBy", 'df_transformed = df.groupBy("a").agg(count("b"))'),
    ("posexplode", 'df_transformed = df.withColumn("a", posexplode(col("b")))'),
]


class TestConversionErrorGainedItsReasonAdditively:
    """B9: the fourth field is optional, defaulted, last, and out of the message."""

    def test_reason_defaults_to_none_on_the_three_positional_construction(self):
        exc = ConversionError("cast", 'df = df.withColumn("a", lit(0).cast("int"))', 4)

        assert exc.reason is None
        assert exc.construct == "cast"
        assert exc.line == 'df = df.withColumn("a", lit(0).cast("int"))'
        assert exc.line_number == 4

    def test_positional_signature_order_is_unchanged(self):
        """The 23 construction sites pass three positionals; order is the contract.

        Asserted on the signature rather than on one construction, because a
        reorder that kept the arity would keep every existing call *working*
        while silently swapping `line` and `line_number` in the message.
        """
        parameters = inspect.signature(ConversionError.__init__).parameters

        assert list(parameters) == ["self", "construct", "line", "line_number", "reason"]
        # The first three stay REQUIRED: a default on any of them would let a
        # construction site drop an argument and still pass.
        for name in ("construct", "line", "line_number"):
            assert parameters[name].default is inspect.Parameter.empty
        assert parameters["reason"].default is None

    def test_str_is_unchanged_and_never_carries_the_reason(self):
        """`str(exc)` is a preservation surface (`bugfix.md` 3.10).

        It is what reaches an operator through `_conversion_failure_results`'
        `error_details` (`code_interpreter_runner.py:627`), so the reason being a
        separate FIELD rather than extra prose is the whole of why the 13 call
        sites did not have to change.
        """
        without = ConversionError("trim", 'df = df.withColumn("a", trim(lit("x")))', 2)
        with_reason = ConversionError(
            "trim", 'df = df.withColumn("a", trim(lit("x")))', 2, reason=REFUSAL_REASON_SCALAR_LITERAL
        )

        assert (
            str(without)
            == 'unconvertible PySpark construct \'trim\' on line 2: df = df.withColumn("a", trim(lit("x")))'
        )
        assert str(with_reason) == str(without)
        assert REFUSAL_REASON_SCALAR_LITERAL not in str(with_reason)

    def test_the_internal_signal_gained_the_same_field_the_same_way(self):
        """`_Unconvertible` carries the reason from the refusing site to `fail`.

        Same additive shape for the same reason: every pre-p40 raise site
        constructs it with two positionals (`pyspark_to_pandas.py:597`, `:602`,
        `:617-618`, and the ten in `_function_call`/`_method_call`), and
        `convert`'s `fail` closure forwards `exc.reason` verbatim (`:1310`).
        """
        parameters = inspect.signature(_Unconvertible.__init__).parameters

        assert list(parameters) == ["self", "construct", "lineno", "reason"]
        assert parameters["reason"].default is None
        assert _Unconvertible("groupBy", 7).reason is None


class TestFenceRefusalCarriesItsReason:
    """R4: the third reason travels on the exception, out of `convert()`."""

    @pytest.mark.parametrize(("construct", "code"), _REFUSED_BY_THE_FENCE, ids=[c for c, _ in _REFUSED_BY_THE_FENCE])
    def test_the_refusal_names_the_owning_construct_and_the_scalar_reason(self, construct, code):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(code)

        exc = raised.value
        assert exc.construct == construct
        assert exc.reason == REFUSAL_REASON_SCALAR_LITERAL
        # 3.4 is unchanged by the new field: the construct, the offending line
        # and its number all still travel.
        assert exc.line == code
        assert exc.line_number == 1

    def test_the_parametrised_inputs_cover_every_fence_construct(self):
        """Guards the case above against a shape being dropped from the list."""
        assert {construct for construct, _ in _REFUSED_BY_THE_FENCE} == set(_FENCE_CONSTRUCTS)

    @pytest.mark.parametrize(
        ("construct", "code"), _REFUSED_WITHOUT_A_REASON, ids=[c for c, _ in _REFUSED_WITHOUT_A_REASON]
    )
    def test_a_refusal_that_predates_the_fence_still_carries_no_reason(self, construct, code):
        """The default keeps every older refusal classified by its construct.

        `_conversion_failure_results` reads `exc.reason or
        classify_conversion_refusal(exc.construct)` (task 7.1), so a `None` here
        is what leaves the two p32 reasons exactly where they were (3.7).
        """
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(code)

        assert raised.value.construct == construct
        assert raised.value.reason is None


class TestFenceConstructsCannotCollideWithThePermanentReason:
    """Verified by reading during design (§4.1); asserted here so it stays true."""

    def test_the_fence_constructs_are_disjoint_from_the_permanent_list(self):
        collisions = _FENCE_CONSTRUCTS & set(_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS)

        assert not collisions, (
            "A fence construct is on the permanently-inexpressible list, so a receiver-shape "
            f"refusal would read as 'nothing to fix, ever': {sorted(collisions)}. Either the "
            "fence must stop naming that construct or the reason precedence in "
            "_conversion_failure_results must be revisited (p40 design §4.1, §4.4)."
        )

    @pytest.mark.parametrize("construct", sorted(_FENCE_CONSTRUCTS))
    def test_a_fence_construct_falls_to_not_yet_expressible_without_a_reason(self, construct):
        """The consequence disjointness buys, asserted directly.

        `classify_conversion_refusal` is deliberately NOT touched by this spec
        (design §4.4), so this is what it says about the fence's names — the
        conservative default, which is also the correct fallback if some future
        refusal forgets to carry a reason.
        """
        assert classify_conversion_refusal(construct) == NOT_VERIFIED_REASON_NOT_YET
