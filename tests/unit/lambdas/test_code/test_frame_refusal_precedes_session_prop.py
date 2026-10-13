# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 8 — the frame refusal precedes session creation.

Spec ``real-spark-verification`` task 5.4. **Validates: Requirement 7.3.**

    IF no Test_Frame can be built that carries every declared source field, THEN
    THE Glue_Session_Runner SHALL return a Harness_Non_Verification without
    creating a session.

Design §5's Property 8 states it over ``run_test``, which is task 8.1 and does
not exist yet. What exists is the **position** that requirement constrains:
:meth:`GlueSessionTestRunner.run_guarded_session` evaluates the refusal and
builds the frame code before ``run_session`` is reached, so this file pins the
property there. 8.1 composes that method, so a ``run_test`` built on it inherits
the guarantee rather than re-establishing it; what 8.1 still owes is the
*mapping* of the raised route (below), not the position.

**No real Glue session is created anywhere in this file.** Every call goes
through a ``MagicMock``, and the point of most of what follows is that even the
mock's ``create_session`` is never reached. Nothing is deployed and no production
code is modified by this task.

Both directions, because one of them is vacuous alone
-----------------------------------------------------

A property asserting only "refused ⇒ no session" passes on a runner that refuses
everything — which would satisfy R7.3 while verifying nothing at all, the exact
class of dishonesty p32 exists to remove. So the central property here is a
**biconditional**: a schema is refused *if and only if* it declares no fields or
declares more than the ceiling, and a session is created *if and only if* it is
not refused.

The expected verdict is computed from the **generated** field count, not by
calling the production refusal. That is what makes this a check rather than a
tautology: ``_frame_refusal`` could agree with itself under any rewrite.

What is generated
-----------------

Task 5.4's instruction is "schema widths and emptiness", and both are taken
literally:

* **Widths crossing the ceiling in both directions.** The boundary is a ``>``
  comparison in ``_frame_refusal`` (``code_interpreter_runner.py:347``), so 512
  is admitted and 513 is refused, and an off-by-one either way is invisible in
  any test that does not sit on the boundary — one direction refuses a
  legitimate schema, the other admits a frame that silently drops a column.
  Widths are drawn from three bands: narrow, a ±3 window around the ceiling, and
  well past it. :func:`test_the_ceiling_is_the_boundary_between_refusal_and_a_session`
  additionally walks the window exhaustively.
* **Every way of declaring nothing**, including two the four worked examples in
  ``test_frame_refusal_reuse.py`` do not reach: a schema carrying no container
  key at all, and a schema whose ``properties`` is empty while its ``fields`` is
  populated. The second is the one that matters — p33 3.14's rule is that
  ``properties`` wins whenever the KEY is present, even when its value is empty,
  so that schema *looks* populated and declares nothing.
* **Buildable widths as the positive control**, drawn in the same strategy as the
  refused ones so the two verdicts are compared under one property rather than in
  two tests that could drift apart.

The second host-side route, covered because R7.3 constrains it identically
-------------------------------------------------------------------------

``FrameBuildError`` — raised by ``build_synthetic_frame_code`` for a declared type
outside the vocabulary — reaches the same pre-flight position and is the *other*
way no frame can be built. R7.3 constrains its position identically even though
mapping it onto the Test_Result_Contract is task 8.1's, and
``FrameBuildError``'s own docstring (``glue_session_runner.py:184-210``) records
that both routes are decided host-side and share the one respect R7.3
constrains. Generalised here over width and over which column carries the bad
type.

Where a schema is **both** too wide and badly typed, the disjunction is asserted
rather than the precedence: whichever route reports, no Glue call happened. That
is the requirement-level claim, and it stays true if task 5.5's frame-mode
dispatch reorders the two.

Deliberately out of scope
-------------------------

``frame_mode`` is pinned to ``"synthetic"`` on every runner built here. Task 5.5
retires the no-source-schema arm of the refusal **conditionally** — for an
``s3://`` source in ``sample`` mode, where the sample supplies the columns a
declared schema would have (R9.4) — and that case is Property 11's (task 5.7).
Pinning the mode is what keeps this file's biconditional true after 5.5 lands
rather than making it a hostage to it.

Ledger notes
------------

* ``LGR-b2f79ef-12`` (**template / ratified**) is the governing record, and it
  states R7.3's shape for the pandas executor in the same words: above the
  ceiling, or with no source schema at all, the run "refuses **before** starting
  a session and reports a ``not_verified_by_harness`` non-verification instead of
  truncating silently". It also records why the number is 512 — ``null_counts``
  and ``dtypes`` are per-column maps reaching the DynamoDB state row at ~46 KB
  for 512 columns against a 400 KB item limit — and that when they stop reaching
  that row the ceiling "can be removed rather than raised". Real Spark changes
  neither side of that arithmetic (R10.4), which is why this file imports the
  constant instead of restating it.
* ``LGR-b2f79ef-9`` (**template / ratified**) leaves ``_build_test_data_code``'s
  ``{id, value}`` fallback in place "but never scored". This property is what
  pins the *never reached* half on the Glue path: every width-0 schema is refused
  before a session, so the fallback frame cannot be measured against.
* ``LGR-3e45e65-1`` (**template / ratified**) pins ``AWS_REGION`` /
  ``AWS_DEFAULT_REGION`` by assignment in ``tests/unit/conftest.py``, so the
  ``setdefault`` calls below are consistent no-ops kept for the module-run case.
* ``why`` on ``glue_session_runner.py`` returns **nothing** — the file is not yet
  committed. ``why`` on ``code_interpreter_runner.py:320`` (``_frame_refusal``
  itself) returns one commit, ``60c4985b9d``, whose seven records are the p32
  test-gate work; all seven are flagged BROAD at 64 files and none names the
  frame refusal. Stated as a gap rather than as an all-clear: the governing
  records for this area are the two ``LGR-b2f79ef-*`` above, found via
  ``decisions(scope="…/agentic_data_ingestion_test_code/**")``.
* ``decisions(concept="testing", include_backfilled=True)`` returns five records,
  none about frame construction or the refusal.

Sibling coverage
----------------

* ``test_frame_refusal_reuse.py`` — task 5.3's worked examples, including the
  four parametrized R7.3 cases this generalises, and the identity assertions that
  pin the refusal as the pandas path's own function object.
* ``test_test_frame_width_prop.py`` — the same rule on the **pandas** executor
  (p33 2.10/2.11). The two properties together are why one judgement about which
  schemas are verifiable can be trusted on both paths.
"""

import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings
from tests.fixtures.run_test_result_shapes import FRAME_REFUSAL, shape_violations

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import (
    _MAX_TEST_FRAME_COLUMNS,
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
)
from glue_session_runner import (
    NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    FrameBuildError,
    GlueSessionTestRunner,
    frame_refusal_results,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: The stage a live run appends after the frame build. Passed on most calls so
#: "no statement was submitted" is a statement about the refusal rather than
#: about there having been nothing to submit.
TRANSFORM_STATEMENT = (STAGE_TRANSFORM, "df_t = df")

#: Every declared type the frame builder accepts, read from the module rather
#: than transcribed so a bucket gaining a member widens this automatically.
_ACCEPTED_TYPES = sorted(
    glue_session_runner._INT_DECLARED_TYPES
    | glue_session_runner._FLOAT_DECLARED_TYPES
    | glue_session_runner._STRING_DECLARED_TYPES
)

#: Widths in three bands, so the ceiling is crossed in both directions in the
#: same strategy: narrow (the ordinary case), a ±3 window around the ceiling
#: (the off-by-one), and well past it (unambiguously refused). Width 0 is
#: reachable from the first band and is the emptiness case expressed as a width.
_WIDTHS = st.one_of(
    st.integers(min_value=0, max_value=8),
    st.integers(min_value=_MAX_TEST_FRAME_COLUMNS - 3, max_value=_MAX_TEST_FRAME_COLUMNS + 3),
    st.integers(min_value=_MAX_TEST_FRAME_COLUMNS + 1, max_value=_MAX_TEST_FRAME_COLUMNS + 64),
)

#: Widths a frame is always built for, for the properties that only need the
#: admitted side.
_BUILDABLE_WIDTHS = st.integers(min_value=1, max_value=8)

#: A wide admitted schema emits ~100 KB of frame code and draws 512 × 100 null
#: positions, so the examples are not free. 200 keeps the whole module inside a
#: second or two while still landing on both sides of the boundary many times.
_REFUSAL_SETTINGS = settings(max_examples=200, deadline=None)

#: For the properties that build a ceiling-width frame on every example.
_WIDE_SETTINGS = settings(max_examples=25, deadline=None)


# ── The mock Glue client ──────────────────────────────────────────────


def _happy_glue_client() -> MagicMock:
    """A Glue client wired so that a session, if reached, succeeds.

    Wired for the happy path deliberately: if ``create_session`` were left to
    return a bare ``MagicMock``, "no session was created" could be satisfied by a
    client too broken to create one, and the positive control would be
    unfalsifiable.
    """
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.return_value = {"Id": 1}
    client.get_statement.return_value = {
        "Statement": {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}}
    }
    return client


@contextlib.contextmanager
def _mock_runner():
    """Yield ``(runner, glue)`` over a fresh mock client, sleeping patched out.

    A context manager entered inside each ``@given`` body rather than a fixture:
    a function-scoped fixture is applied once for the whole test function and
    never reset between examples, so a mock built there would accumulate calls
    across examples and every call-count assertion would be measuring the wrong
    thing. The same reasoning ``test_frame_parity_prop.py`` records for its
    ``_stubbed_modules`` helper.

    ``frame_mode`` is left at its ``"synthetic"`` default explicitly — see this
    module's docstring for why the mode is pinned rather than generated.
    """
    glue = _happy_glue_client()
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, frame_mode="synthetic"), glue


def _assert_no_glue_call(glue: MagicMock, source_schema) -> None:
    """Nothing at all was asked of Glue — the R7.3 claim, at its strongest.

    ``mock_calls`` rather than ``create_session.assert_not_called()``: R7.3 says
    "without creating a session", and the honest reading of that on a host-side
    refusal is that no session API was reached at all. Asserting the whole call
    list also catches a future edit that reaches Glue by some other route —
    ``GetSession`` on a stale id, a tagging call — before the refusal is
    evaluated.
    """
    assert glue.mock_calls == [], f"a refused schema reached Glue: {glue.mock_calls} (schema {source_schema!r})"


# ── Generators ────────────────────────────────────────────────────────


def _fields(width: int, declared_type: str) -> dict:
    """``width`` declared fields, all of one accepted type."""
    return {f"col_{index}": {"type": declared_type} for index in range(width)}


@st.composite
def _schemas(draw, widths=_WIDTHS):
    """Return ``(source_schema, declared_field_count)``.

    The count is returned rather than recomputed, because it is what the expected
    verdict is derived from — deriving it by calling ``declared_source_fields``
    would make the property compare the production code with itself.

    Both container keys are generated, and the ``both`` shape is the one p33 3.14
    pins: ``properties`` wins whenever the KEY is present, so a populated
    ``fields`` beside it declares nothing. At width 0 that shape is a schema that
    looks populated and is empty.
    """
    width = draw(widths)
    container = draw(st.sampled_from(("properties", "fields", "both")))
    declared_type = draw(st.sampled_from(_ACCEPTED_TYPES))
    fields = _fields(width, declared_type)

    if container == "properties":
        return {"type": "object", "properties": fields}, width
    if container == "fields":
        return {"fields": fields}, width
    # `properties` present decides it; `fields` must be ignored even when the
    # winning container is empty.
    return {"properties": fields, "fields": {"ignored_by_the_rule": {"type": "integer"}}}, width


@st.composite
def _nothing_declared_schemas(draw):
    """Every shape that declares no source field.

    The four ``test_frame_refusal_reuse.py`` parametrizes, plus two it does not
    reach: a schema with no container key at all, and the p33 3.14 shape whose
    empty ``properties`` beats a populated ``fields``. The populated payload in
    the last two is generated, so "it is ignored" is checked against varying
    content rather than against one literal.
    """
    ignored = _fields(draw(st.integers(min_value=1, max_value=4)), draw(st.sampled_from(_ACCEPTED_TYPES)))
    return draw(
        st.sampled_from(
            [
                None,
                {},
                {"properties": {}},
                {"fields": {}},
                # No container key at all — `declared_source_fields` falls
                # through both `.get`s to `{}`.
                {"type": "object", "title": "declares nothing"},
                # Looks populated, declares nothing (p33 3.14).
                {"properties": {}, "fields": ignored},
            ]
        )
    )


def _expected_refused(declared_count: int) -> bool:
    """R7.3's condition, in the requirement's own terms.

    "No Test_Frame can be built that carries every declared source field" is
    true in exactly two cases:

    * nothing is declared — the frame would be the ``{id, value}`` fallback,
      whose columns are not the declared ones, so it carries none of them
      (``LGR-b2f79ef-9``: kept "but never scored");
    * more fields are declared than the ceiling admits — the frame would be
      built from a slice and would silently omit the rest
      (``LGR-b2f79ef-12``).

    Written from the requirement rather than from ``_frame_refusal``, which is
    what makes the biconditional below falsifiable.
    """
    return declared_count == 0 or declared_count > _MAX_TEST_FRAME_COLUMNS


# ── Property 8, both directions ──────────────────────────────────────


@given(shape=_schemas())
@_REFUSAL_SETTINGS
def test_a_schema_is_refused_exactly_when_no_frame_carries_every_declared_field(shape):
    """Property 8. **Validates: Requirement 7.3**

    For any generated width and any container shape, ``run_guarded_session``
    refuses **iff** no frame can carry every declared field — and a refusal
    reaches no Glue API while an admitted schema reaches a created session.

    A biconditional on purpose. "Refused ⇒ no session" alone is satisfied by a
    runner that refuses everything, which would spend nothing and verify
    nothing; the second arm is what makes the first arm mean something.
    """
    source_schema, declared_count = shape
    expected = _expected_refused(declared_count)

    with _mock_runner() as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert run.refused is expected, (
            f"{declared_count} declared field(s) against a ceiling of {_MAX_TEST_FRAME_COLUMNS}: "
            f"expected refused={expected}, got {run.refused}"
        )

        if expected:
            # R7.3's consequent, in three parts: no session, a complete harness
            # non-verification, and nothing observed.
            _assert_no_glue_call(glue, source_schema)
            assert run.outcomes == []
            assert run.refusal is not None
            assert run.refusal["not_verified_by_harness"] is True
            assert run.refusal["executed"] is False
            assert run.refusal["execution_successful"] is False
            assert run.refusal["exit_code"] is None
            # Empty rather than merely absent: this is what suppresses the
            # `schema_compliance` score, so a frame the harness could not build
            # cannot report a shortfall against the transform (design C13).
            assert run.refusal["data_quality_results"] == {}
        else:
            assert run.refusal is None
            glue.create_session.assert_called_once()
            assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]


@given(shape=_schemas())
@_REFUSAL_SETTINGS
def test_the_refusal_names_the_reason_the_width_implies(shape):
    """The two reasons are distinguished by which condition was violated.

    **Validates: Requirement 7.3**

    A refusal that named the wrong reason would still create no session and
    would still satisfy the property above, while telling a reviewer the wrong
    thing about their schema — and ``lambda_function``'s harness branch renders
    the reason as text, so it reaches a human.

    No third reason may appear either: the vocabulary is the two
    ``code_interpreter_runner`` already declares, which is what lets
    ``is_not_verified_check`` and the verified-check partition stay untouched
    (R8.4's rule).
    """
    source_schema, declared_count = shape

    with _mock_runner() as (runner, _glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

    if not _expected_refused(declared_count):
        assert run.refusal is None
        return

    assert run.refusal is not None
    expected_reason = (
        NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA if declared_count == 0 else NOT_VERIFIED_REASON_FRAME_TRUNCATED
    )
    assert run.refusal["not_verified_reason"] == expected_reason
    assert run.refusal["not_verified_field_count"] == declared_count
    assert run.refusal["not_verified_frame_ceiling"] == _MAX_TEST_FRAME_COLUMNS


@given(source_schema=_nothing_declared_schemas())
@_REFUSAL_SETTINGS
def test_every_way_of_declaring_nothing_is_refused_before_any_glue_call(source_schema):
    """Emptiness, over every shape that expresses it.

    **Validates: Requirement 7.3**

    ``None`` is included because this runner's caller reads the schema off a
    payload, and an absent one must not become the two-column fallback silently.
    The last two shapes are the ones the worked examples do not reach: a schema
    with no container key, and one whose empty ``properties`` beats a populated
    ``fields`` — the shape that looks populated and declares nothing.
    """
    with _mock_runner() as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert run.refused
        assert run.refusal is not None
        assert run.refusal["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
        assert run.refusal["not_verified_field_count"] == 0
        _assert_no_glue_call(glue, source_schema)
        assert run.outcomes == []


@given(offset=st.integers(min_value=-2, max_value=2), declared_type=st.sampled_from(_ACCEPTED_TYPES))
@_WIDE_SETTINGS
def test_the_ceiling_is_the_boundary_between_refusal_and_a_session(offset, declared_type):
    """512 admitted, 513 refused — walked, not sampled.

    **Validates: Requirement 7.3**

    The ceiling is a ``>`` comparison (``code_interpreter_runner.py:347``), and
    an off-by-one in either direction is invisible away from the boundary: one
    direction refuses a legitimate schema, the other admits a frame that drops a
    column and reports the loss as a low ``schema_compliance`` attributed to the
    transform. The declared type is varied so the boundary is not accidentally a
    property of one type bucket.
    """
    width = _MAX_TEST_FRAME_COLUMNS + offset
    source_schema = {"properties": _fields(width, declared_type)}

    with _mock_runner() as (runner, glue):
        run = runner.run_guarded_session(source_schema)

        assert run.refused is (offset > 0), f"width {width} against ceiling {_MAX_TEST_FRAME_COLUMNS}"
        if offset > 0:
            _assert_no_glue_call(glue, source_schema)
        else:
            glue.create_session.assert_called_once()


@given(shape=_schemas())
@_REFUSAL_SETTINGS
def test_the_returned_refusal_is_the_shared_builders_own_dict(shape):
    """The refusal is reused, not re-derived, at every generated width.

    **Validates: Requirement 7.3**

    ``test_frame_refusal_reuse.py`` pins ``_frame_refusal`` and
    ``_frame_truncation_results`` by object identity and pins one refusal by
    value. This generalises the value equality over widths, so a future
    ``run_guarded_session`` that post-processed the refusal — adding a field,
    rewriting the message — fails here. Equality with the module-level function's
    output is what makes a Spark-side refusal and a Code Interpreter refusal
    literally the same report.
    """
    source_schema, _ = shape

    with _mock_runner() as (runner, _glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

    assert run.refusal == frame_refusal_results(source_schema)


# ── The positive control, stated on its own ──────────────────────────


@given(width=_BUILDABLE_WIDTHS, declared_type=st.sampled_from(_ACCEPTED_TYPES))
@_REFUSAL_SETTINGS
def test_a_buildable_schema_reaches_spark_with_the_frame_statement_first(width, declared_type):
    """The positive control, and it is about the FRAME, not about the mock.

    **Validates: Requirement 7.3**

    "No session was created" is only informative if a session is created when it
    should be — otherwise every assertion in this file is satisfied by a runner
    that refuses everything, or by a client too broken to create anything.

    Asserted one step beyond ``create_session``: the first statement submitted
    *begins with* what ``build_synthetic_frame_code`` emits, byte for byte, and is
    the full ``frame_build_statement`` — the frame code followed by task 6.1's
    ``GENERATED:`` marker line. So the admitted branch means the seeded frame R10.3
    pins actually reached Spark, rather than merely that a session object was
    allocated.
    """
    source_schema = {"properties": _fields(width, declared_type)}

    with _mock_runner() as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert not run.refused
        assert run.refusal is None
        glue.create_session.assert_called_once()

        submitted = [call.kwargs["Code"] for call in glue.run_statement.call_args_list]
        assert submitted == [
            glue_session_runner.frame_build_statement(runner.resolve_frame_source(), source_schema),
            TRANSFORM_STATEMENT[1],
        ]
        assert submitted[0].startswith(glue_session_runner.build_synthetic_frame_code(source_schema))


# ── The second host-side route: a raised FrameBuildError ─────────────


@given(
    width=_BUILDABLE_WIDTHS,
    position=st.integers(min_value=0, max_value=7),
    unknown=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
        lambda candidate: candidate not in _ACCEPTED_TYPES
    ),
)
@_REFUSAL_SETTINGS
def test_an_out_of_vocabulary_type_also_fails_before_any_glue_call(width, position, unknown):
    """The other host-side frame failure, in the same position.

    **Validates: Requirement 7.3**

    ``FrameBuildError`` is *raised* where the refusal is *returned*, and mapping
    it onto the Test_Result_Contract is task 8.1's. What R7.3 constrains
    identically is its position, and that is what is pinned here — generalised
    over width and over which column carries the bad type, because a schema with
    400 fields must fail on the one that is wrong wherever it sits.

    The refusal is why the narrowing costs no verification: Spark enforces the
    explicit ``StructType``, so a mis-typed column changes what every operation
    on it does, and the symptom would be a transform error or a wrong
    data-quality number attributed to the transform
    (``spark_type_for``'s docstring).
    """
    fields = _fields(width, "string")
    bad_column = f"col_{position % width}"
    fields[bad_column] = {"type": unknown}
    source_schema = {"properties": fields}

    with _mock_runner() as (runner, glue):
        with pytest.raises(FrameBuildError) as excinfo:
            runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        # Names the column as well as the type — a wide schema needs to say
        # which field is wrong.
        assert bad_column in str(excinfo.value)
        assert unknown in str(excinfo.value)
        _assert_no_glue_call(glue, source_schema)


@given(
    over=st.integers(min_value=1, max_value=8),
    unknown=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
        lambda candidate: candidate not in _ACCEPTED_TYPES
    ),
)
@_WIDE_SETTINGS
def test_a_schema_that_is_both_too_wide_and_badly_typed_still_creates_no_session(over, unknown):
    """Both failure modes at once — the disjunction, not the precedence.

    **Validates: Requirement 7.3**

    Which route reports first is an ordering inside ``run_guarded_session`` and
    is task 8.1's to settle when it maps the raised route onto the contract; task
    5.5 is editing that method's dispatch concurrently. What R7.3 constrains is
    the same either way, so that is what is asserted: whichever of the two
    host-side failures reports, **no Glue call happened**.

    Recorded as an observation for 8.1 rather than pinned: on the code as read,
    the refusal is evaluated before the frame is built, so a too-wide schema
    returns a refusal and the bad type is never reached.
    """
    fields = _fields(_MAX_TEST_FRAME_COLUMNS + over, "string")
    fields["col_0"] = {"type": unknown}
    source_schema = {"properties": fields}

    with _mock_runner() as (runner, glue):
        try:
            run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])
        except FrameBuildError:
            pass
        else:
            assert run.refused, "a schema past the ceiling was neither refused nor rejected at frame build"
            assert run.refusal is not None
            assert run.refusal["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED

        _assert_no_glue_call(glue, source_schema)


# ── Task 8.1: the MAPPING of both host-side routes onto the contract ──
#
# The position was pinned above and is `run_guarded_session`'s. What 8.1 owes is
# the other half: `run_test` turning each of the two routes into a
# Test_Result_Contract dict. Extended here rather than restated in a new file —
# this module's docstring already says the position is pinned here and that "what
# 8.1 still owes is the *mapping* of the raised route, not the position", so the
# two halves of one requirement stay in one place and the generators are shared.


def _run_test_no_glue_call(source_schema) -> tuple[dict, MagicMock]:
    """Call ``run_test`` and return ``(result, glue)`` for a refused schema.

    ``run_test`` rather than ``run_guarded_session``: the mapping is what is under
    test, and R7.3 constrains the public method. The Glue client is the same happy
    mock, so "no session was created" still means the refusal beat a client that
    would have succeeded.
    """
    with _mock_runner() as (runner, glue):
        result = runner.run_test("df_transformed = df", source_schema, {})
        return result, glue


@given(shape=_schemas())
@_REFUSAL_SETTINGS
def test_run_test_returns_the_harness_non_verification_without_a_session(shape):
    """R7.3 over the PUBLIC method, both directions, on the returned dict.

    **Validates: Requirement 7.3**

    The properties above pin the position on ``run_guarded_session``; this pins
    that ``run_test`` returns the refusal *as the result* rather than swallowing
    it, raising, or turning it into a failure — and that the admitted side still
    reaches Spark, so the biconditional is not satisfied by a method that refuses
    everything.

    The expected verdict is the generated field count run through
    :func:`_expected_refused`, i.e. the requirement's own condition, not
    ``_frame_refusal``'s answer.
    """
    source_schema, declared_count = shape
    result, glue = _run_test_no_glue_call(source_schema)

    if _expected_refused(declared_count):
        assert result["not_verified_by_harness"] is True
        assert result["executed"] is False
        assert result["execution_successful"] is False
        assert result["exit_code"] is None
        _assert_no_glue_call(glue, source_schema)
    else:
        assert "not_verified_by_harness" not in result
        glue.create_session.assert_called_once()


@given(shape=_schemas().filter(lambda shape: _expected_refused(shape[1])))
@_REFUSAL_SETTINGS
def test_run_test_returns_the_refusal_dict_unchanged(shape):
    """``run_test`` returns the shared builder's dict, not a copy of it.

    **Validates: Requirements 1.1, 1.2, 7.3**

    Equality against ``frame_refusal_results`` called directly, so a ``run_test``
    that reconstructed the refusal — or edited a key on the way out — fails here.
    That is what keeps a Spark-side refusal and a pandas-side refusal literally
    the same report rather than two reports that agree today.

    ``conversion_report`` is included in that equality and is ``None``: task 8.1's
    decision is that this runner overwrites nothing, because it converted nothing.
    """
    source_schema, _ = shape
    result, _ = _run_test_no_glue_call(source_schema)

    assert result == frame_refusal_results(source_schema, sample_frame=False)
    assert result["conversion_report"] is None


@given(
    width=_BUILDABLE_WIDTHS,
    position=st.integers(min_value=0, max_value=7),
    unknown=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
        lambda candidate: candidate not in _ACCEPTED_TYPES
    ),
)
@_REFUSAL_SETTINGS
def test_run_test_maps_a_raised_frame_build_error_to_a_harness_non_verification(width, position, unknown):
    """The raised route reaches the contract instead of escaping (R7.3).

    **Validates: Requirements 1.1, 1.2, 7.3**

    ``FrameBuildError``'s docstring defers this mapping to ``run_test`` and names
    the destination: "a harness non-verification with no session created". Three
    claims, and the middle one is the one that would be easy to get wrong:

    * it does not escape — ``run_test`` returns a dict for every input;
    * it is a NON-VERIFICATION, not a failure. Mapping a harness limitation onto
      ``not_executed_results`` would report it as a failed verification, which is
      the attribution error p32 exists to remove and which ``FrameBuildError``'s
      docstring warns against by name;
    * no session was created, which is the half R7.3 shares with the returned
      refusal.

    The reason is its own — ``test_frame_unbuildable`` — because the two width
    reasons render a sentence about the schema's width that is false here: the
    schema is present and inside the ceiling. Its detail names the offending type
    so the reviewer learns which declared type the harness could not represent.
    """
    fields = _fields(width, "string")
    bad_column = f"col_{position % width}"
    fields[bad_column] = {"type": unknown}
    source_schema = {"properties": fields}

    result, glue = _run_test_no_glue_call(source_schema)

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_UNBUILDABLE
    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert result["exit_code"] is None
    assert result["data_quality_results"] == {}
    # The reviewer-facing half: which column, and which declared type.
    assert bad_column in result["error_details"]
    assert unknown in result["error_details"]
    # NOT one of the width reasons — borrowing either would state a falsehood
    # about the schema on the way to removing one about the transform.
    assert result["not_verified_reason"] != NOT_VERIFIED_REASON_FRAME_TRUNCATED
    assert result["not_verified_reason"] != NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
    _assert_no_glue_call(glue, source_schema)


@given(
    width=_BUILDABLE_WIDTHS,
    unknown=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
        lambda candidate: candidate not in _ACCEPTED_TYPES
    ),
)
@_WIDE_SETTINGS
def test_the_two_host_side_routes_reach_the_same_contract_shape(width, unknown):
    """Both host-side routes land on ONE shape, and it is the declared one.

    **Validates: Requirements 1.1, 1.2**

    ``FrameBuildError``'s docstring says the raised route reaches "the *same*
    destination :func:`frame_refusal_results` already reaches". Same *destination*
    is a claim about the shape, not about the reason, so what is asserted is the
    key set: both carry the nine contract keys plus the harness marker and a
    reason, and the raised route carries neither ``not_verified_field_count`` nor
    ``not_verified_frame_ceiling`` — there is no width problem, so a count and a
    ceiling would render a sentence about a width that is not what went wrong.
    That is the same omission ``session_capacity_results`` already makes, for the
    same stated reason.

    Checked against ``FRAME_REFUSAL``'s declared shape rather than against the
    other result, with ``conversion_report`` excluded — the fixture narrows that
    key to ``dict`` because conversion ran on the pandas path, and task 8.1's
    decision is that this runner leaves it ``None``. The exclusion is the same one
    ``test_glue_result_mapping.py:157`` already applies.
    """
    unbuildable = {"properties": {**_fields(width, "string"), "col_0": {"type": unknown}}}
    raised, _ = _run_test_no_glue_call(unbuildable)
    returned, _ = _run_test_no_glue_call({})

    assert set(raised) == set(returned) - {"not_verified_field_count", "not_verified_frame_ceiling"}
    assert "not_verified_field_count" not in raised
    assert "not_verified_frame_ceiling" not in raised

    violations = [
        violation for violation in shape_violations(raised, FRAME_REFUSAL) if "conversion_report" not in violation
    ]
    # The two width keys are declared on FRAME_REFUSAL and deliberately absent
    # here, so their "missing key" violations are the expected difference.
    assert sorted(violations) == [
        "missing key 'not_verified_field_count' (contract: int)",
        "missing key 'not_verified_frame_ceiling' (contract: int)",
    ], violations
