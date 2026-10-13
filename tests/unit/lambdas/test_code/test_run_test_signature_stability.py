# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``run_test``'s signature is the contract; the per-executor values are not.

Spec ``real-spark-verification`` task 9.4.
**Validates: Requirement 9.6.**

R9.6 asks for two things at once, and they pull in opposite directions: the
Source_Data_Uri has to *reach* the Glue runner, and ``run_test``'s parameter list
has to *not move*. Task 9.3 resolved that by pushing the divergence into the
constructor. This file is the structural fence on both halves of that resolution:

* **Convergence at ``run_test``.** Both executors' ``run_test`` take the same
  three arguments, in the same order, positionally. That is what lets the single
  production call site (``lambda_function.py``'s executor dispatch) hand the same
  three values to either runner — R1's interchangeability, expressed as a
  signature rather than as a hope.
* **Divergence at ``__init__``.** ``source_data_uri`` is a keyword argument on
  ``GlueSessionTestRunner`` and is absent from ``run_test`` on both runners.

Why a *signature* test rather than only the source assertions
------------------------------------------------------------

Task 9.1's ``test_verification_executor_selection.py`` already pins the **call
site** by source text: that ``source_data_uri=source_data_uri`` appears in the
construction, and that ``runner.run_test(transform_block, source_schema,
target_schema_for_run)`` appears exactly once. Those assertions catch a call site
that stops passing the URI. They cannot catch the opposite failure — a *runner*
that grows a fourth ``run_test`` parameter — because a new parameter with a
default keeps every existing call site's text valid. That is the gap this file
closes, and it is why the two files are complements rather than duplicates. No
assertion here reads ``lambda_function.py``'s source.

Nothing here constructs a Glue session. Both constructors are exercised over a
patched ``boto3.client``; no AWS call is made and nothing is deployed.

What the arity is worth, stated so the fence is not read as ceremony
-------------------------------------------------------------------

Measured with codegraph rather than taken from the spec's prose, and the two
disagree: codegraph reports **49 references** to
``CodeInterpreterTestRunner.run_test`` across 9 covering test files, where R9.6
says 32 and five. The spec's figure is the older count and neither is asserted
here — the point either way is that the number is large and spread across
files. ``GlueSessionTestRunner.run_test`` has 3 references and 3 covering test
files; ``GlueSessionTestRunner`` itself has 32 and 13. **Neither method is
flagged as uncovered**, so no characterization test was needed before writing
this one. A fourth parameter is therefore not a local edit — and
``GlueSessionTestRunner.run_test``'s own docstring says so outright: "Three
arguments, and the arity is the contract … Do not add a fourth parameter."

Those counts are a snapshot and they drift: every task that touches the executor
dispatch in ``lambda_function.py`` moves the ``GlueSessionTestRunner`` figure.
They are recorded to show the order of magnitude, which is the part that makes
the fence worth having, and deliberately not asserted anywhere below — a test
that failed when a caller was added would be measuring the wrong thing.

Ledger notes
------------

``why`` on ``glue_session_runner.py`` returns **nothing** — the file is not yet
committed, so no record governs it. Stated as a gap rather than as an all-clear.
``why`` on ``code_interpreter_runner.py`` returns ``LGR-f6519e1-1`` (literal
fences), ``LGR-4dd9b42-1`` (the Series-only refusal, whose revisit condition is
this spec's authorisation) and the seven ``LGR-60c4985-*`` records; none of them
governs a signature. ``decisions(scope=".../agentic_data_ingestion_test_code/**",
include_backfilled=True)`` returns nine records at the time of writing, of which
the two standing invariants touching this directory are ``LGR-b46a5bb-1`` (no
``threading.Thread`` in ``lambda/**``) and ``LGR-819980f-1`` (JDBC credentials by
Secrets Manager ARN only) — neither reached by a signature assertion.
``decisions(concept="testing", include_backfilled=True)`` returns five records,
none about signature pinning. So the arity contract lives in R9.6 and in the
method's own docstring, and this file is what makes it checkable.

Sibling coverage — deliberately not duplicated here
---------------------------------------------------

* ``test_verification_executor_selection.py`` — the call site, by source text
  (task 9.1, amended by 9.3).
* ``test_sample_frame_dispatch.py`` / ``test_s3_scheme_gate_prop.py`` — what the
  stored URI then *does*: the ``sample``/``synthetic`` decision, the S3 scheme
  gate, and the non-string inputs a payload can deliver (tasks 5.5, 5.6). This
  file stops at "the constructor accepts it and the instance holds it"; those two
  own every behavioural consequence.
* ``test_run_test_composition.py`` — what ``run_test`` does with the three
  arguments it takes (task 8.1). It carried one signature assertion of its own,
  ``test_run_test_takes_exactly_the_three_contract_arguments`` (then at
  ``:425``), written before this file existed: the Glue runner's parameter list
  plus ``source_data_uri``'s presence on its constructor. **That assertion was
  deleted on 2026-08-18**, and this file is now the only place ``run_test``'s
  arity is pinned. Do not re-add it there.

  The deletion is recorded rather than silent because this repo treats one as a
  decision: ``LGR-5906ec6-2`` (**template / ratified**) encodes a deliberate
  hold *as* a test assertion precisely so that lifting the hold means deleting a
  test on purpose, and ``LGR-60c4985-6`` (**template / ratified**) then prefers
  *amending* such an assertion to deleting it, so "the amendment stays legible
  as a reversal, not read as a regression". **Neither applies to this deletion,
  and the reason they do not is the whole argument for it:** that assertion
  encoded no hold and reversed nothing. It was covering a gap — R9.6 had no
  structural fence when task 8.1 was written — and this file now covers that gap
  strictly more strongly. There is no behaviour being enabled here, so there is
  no reversal for a reader to mistake for a regression, which is the only harm
  ``LGR-60c4985-6`` names. ``LGR-60c4985-6`` also shows the preference is not
  absolute: it *replaced* ``test_no_code_regeneration_string_remains_in_lambda_source``
  outright when what needed pinning changed.

  Verified subset rather than assumed, assertion by assertion, before removal:
  its parameter-list check is reproduced at equal strength by
  ``test_run_test_parameter_names_are_exactly_the_three_the_contract_pins``
  (same four names, same order, ``[glue_session]`` covering the same runner),
  and its ``"source_data_uri" in …__init__`` membership check is reproduced
  strictly more strongly by ``test_source_data_uri_is_a_constructor_parameter``,
  which subscripts the parameter — so absence raises ``KeyError`` — and then
  also pins its kind and default. It asserted nothing about ``run_test``'s
  *behaviour*: it never called the method, and it pinned no constant this file
  does not pin in ``EXPECTED_RUN_TEST_PARAMETERS``. Its docstring cited R1.1,
  which six surviving tests in that file still validate, so no requirement lost
  its only coverage.
"""

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from code_interpreter_runner import CodeInterpreterTestRunner
from glue_session_runner import GlueSessionTestRunner

REGION = "us-east-1"
ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
S3_URI = "s3://processed-files/pipeline-a/input.csv"

#: The parameter list R9.6 pins, ``self`` included so the order is unambiguous.
EXPECTED_RUN_TEST_PARAMETERS = ("self", "transform_block", "source_schema", "target_schema")

#: Both executors, so every assertion below covers the pair. R1's contract is
#: that the two are interchangeable at the call site, which is a statement about
#: BOTH signatures — asserting it of one proves nothing about the swap.
_BOTH_EXECUTORS = pytest.mark.parametrize(
    "runner_class",
    [CodeInterpreterTestRunner, GlueSessionTestRunner],
    ids=["pandas_proxy", "glue_session"],
)


def _run_test_signature(runner_class: type) -> inspect.Signature:
    """The unbound ``run_test`` signature, so ``self`` is visible.

    Taken off the class rather than off an instance deliberately: a bound method
    hides ``self``, and ``self``'s position is part of what makes the three
    remaining parameters positional in the order the call site uses.
    """
    return inspect.signature(runner_class.run_test)


# ──────────────────────────────────────────────────────────────────────────
# The parameter list itself (R9.6)
# ──────────────────────────────────────────────────────────────────────────


@_BOTH_EXECUTORS
def test_run_test_parameter_names_are_exactly_the_three_the_contract_pins(runner_class):
    """R9.6 — names and order, as a tuple comparison.

    A tuple rather than a set or a length: a *rename* is as much of a break as an
    addition for the keyword-argument callers, and a *reorder* breaks the
    positional ones while leaving both the set and the length intact.
    """
    assert tuple(_run_test_signature(runner_class).parameters) == EXPECTED_RUN_TEST_PARAMETERS


@_BOTH_EXECUTORS
def test_run_test_takes_exactly_three_arguments_besides_self(runner_class):
    """The count, stated separately from the names.

    Redundant with the tuple comparison above by construction, and kept because
    it is the assertion whose failure message says *arity* — which is the word
    R9.6 and the method's docstring both use.
    """
    parameters = [name for name in _run_test_signature(runner_class).parameters if name != "self"]
    assert len(parameters) == 3


@_BOTH_EXECUTORS
def test_run_test_accepts_no_variadic_parameters(runner_class):
    """``*args``/``**kwargs`` would make every assertion above vacuous.

    A fourth argument could then arrive at ``run_test`` without the signature
    naming it, so the parameter list would read as unchanged while the arity had
    in fact moved — the exact drift R9.6 forbids, hidden from the exact tool R9.6
    asks to detect it with.
    """
    kinds = [p.kind for p in _run_test_signature(runner_class).parameters.values()]
    assert inspect.Parameter.VAR_POSITIONAL not in kinds
    assert inspect.Parameter.VAR_KEYWORD not in kinds


@_BOTH_EXECUTORS
def test_no_run_test_parameter_carries_a_default(runner_class):
    """A default is how a fourth parameter gets added without anything breaking.

    This is the failure mode task 9.1's source assertions structurally cannot
    see: ``run_test(self, transform_block, source_schema, target_schema,
    source_data_uri=None)`` leaves all 48 existing call sites valid and all of
    their source text unchanged. Requiring every parameter to be mandatory means
    such an addition has to break something on purpose.
    """
    defaulted = [
        name
        for name, parameter in _run_test_signature(runner_class).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    ]
    assert defaulted == []


@_BOTH_EXECUTORS
def test_every_run_test_parameter_can_be_passed_positionally(runner_class):
    """The production call site passes all three positionally.

    ``runner.run_test(transform_block, source_schema, target_schema_for_run)`` —
    so a parameter made keyword-only would break that call while leaving the
    name tuple identical.
    """
    kinds = {
        name: parameter.kind
        for name, parameter in _run_test_signature(runner_class).parameters.items()
        if name != "self"
    }
    assert set(kinds.values()) == {inspect.Parameter.POSITIONAL_OR_KEYWORD}


@_BOTH_EXECUTORS
def test_three_positional_arguments_bind(runner_class):
    """The arity contract, exercised the way the call site exercises it.

    ``bind`` is what a call does, so this asserts the *usable* consequence of the
    structural assertions above rather than restating them.
    """
    bound = _run_test_signature(runner_class).bind(None, "df_t = df", {}, {})
    assert list(bound.arguments) == list(EXPECTED_RUN_TEST_PARAMETERS)


@_BOTH_EXECUTORS
def test_a_fourth_positional_argument_does_not_bind(runner_class):
    """Nothing may be smuggled in as a fourth argument.

    The complement of the test above, and the one that would start passing if a
    fourth parameter were added — whether or not it carried a default.
    """
    with pytest.raises(TypeError):
        _run_test_signature(runner_class).bind(None, "df_t = df", {}, {}, S3_URI)


@_BOTH_EXECUTORS
def test_source_data_uri_is_not_a_run_test_parameter(runner_class):
    """R9.6, named directly.

    Asserted on both executors: the pandas path never had the value, and the
    Glue path is the one that wanted it, so this is the assertion that says the
    want was satisfied somewhere other than here.
    """
    assert "source_data_uri" not in _run_test_signature(runner_class).parameters


def test_the_two_executors_are_interchangeable_at_the_call_site():
    """R1 — one call site, two runners, one set of arguments.

    The whole reason R9.6 constrains the parameter list rather than merely
    documenting it. Asserted as an equality between the two signatures' parameter
    tuples so that a change to *either* runner fails here, including one that
    changes both consistently — a coordinated widening is still a contract
    change and should have to be a deliberate one.
    """
    pandas_parameters = tuple(_run_test_signature(CodeInterpreterTestRunner).parameters)
    glue_parameters = tuple(_run_test_signature(GlueSessionTestRunner).parameters)

    assert pandas_parameters == glue_parameters == EXPECTED_RUN_TEST_PARAMETERS


# ──────────────────────────────────────────────────────────────────────────
# The URI's route in: the constructor (R9.6's other half)
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def glue_client():
    """A mock Glue client. No AWS call is made anywhere in this file."""
    return MagicMock()


@pytest.fixture
def make_glue_runner(glue_client):
    """Construct a ``GlueSessionTestRunner`` over the mock client."""

    def _make(**kwargs) -> GlueSessionTestRunner:
        with patch("glue_session_runner.boto3.client", return_value=glue_client):
            return GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, **kwargs)

    return _make


def test_source_data_uri_is_a_constructor_parameter():
    """R9.6 — the route in, asserted structurally.

    Keyword-only and optional, both on purpose. Keyword-only because the
    constructor carries four keyword arguments and a positional URI would be
    indistinguishable from a role ARN at a glance; optional because an absent URI
    is an ordinary case rather than a defect — a JDBC-sourced pipeline's URI is
    ``jdbc://pipeline/<id>`` by construction, and nothing selects ``sample`` mode
    on any live path yet.
    """
    parameter = inspect.signature(GlueSessionTestRunner.__init__).parameters["source_data_uri"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_the_constructor_stores_the_uri_it_is_given(make_glue_runner):
    """Reachable through the constructor means the instance holds it.

    The behavioural half of the assertion above: a constructor that accepted the
    argument and dropped it would satisfy every signature check in this file and
    deliver nothing. What the stored value then decides —
    ``sample`` against ``synthetic``, the S3 scheme gate — belongs to
    ``test_sample_frame_dispatch.py`` and ``test_s3_scheme_gate_prop.py`` and is
    not restated here.
    """
    assert make_glue_runner(source_data_uri=S3_URI).source_data_uri == S3_URI


def test_an_unsupplied_uri_is_none_rather_than_empty(make_glue_runner):
    """Absent has one spelling on this runner, and it is ``None``.

    The TestCode payload defaults ``source_data_uri`` to ``""``, and task 9.3's
    call site coerces that to ``None`` (``source_data_uri or None``) for this
    reason. Pinning the default here is what makes that coercion meaningful: if
    the constructor's own absent value were ``""``, the call site would be
    normalising to a value the runner did not treat as canonical.
    """
    assert make_glue_runner().source_data_uri is None


def test_the_pandas_constructor_is_unchanged_and_the_asymmetry_is_the_mechanism():
    """The two constructors are deliberately NOT interchangeable.

    R1's interchangeability is a claim about ``run_test``, not about
    construction, and that asymmetry is exactly how R9.6 is satisfied:
    per-executor values live on the constructor *so that* ``run_test`` can stay
    identical across both. Adding ``source_data_uri`` to
    ``CodeInterpreterTestRunner`` would imply a consumer that does not exist —
    the pandas proxy reads no source object — so the pandas constructor keeping
    its single ``region`` argument is a fact worth pinning rather than an
    oversight.
    """
    assert tuple(inspect.signature(CodeInterpreterTestRunner.__init__).parameters) == ("self", "region")
    assert "source_data_uri" not in inspect.signature(CodeInterpreterTestRunner.__init__).parameters
