# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 11 — the absent-schema non-verification is exact.

Spec ``real-spark-verification`` task 5.7. **Validates: Requirements 9.4, 9.5.**

    R9.4 — WHERE the Test_Frame is a Sample_Frame, THE Glue_Session_Runner SHALL
    report no Harness_Non_Verification for an absent source schema, because the
    sample supplies the columns the schema would have declared.

    R9.5 — WHERE the Test_Frame is a Synthetic_Frame AND no source schema is
    available, THE Glue_Session_Runner SHALL report a Harness_Non_Verification
    for the absent schema, matching the existing behaviour
    (``code_interpreter_runner.py:320-350``).

Read together the two are one biconditional, and stating it as one is the point
of this file: the absent-schema refusal is reported **exactly when** the frame is
a Synthetic_Frame *and* no source schema is available. Either half alone is
satisfiable by something useless. "Synthetic and absent ⇒ refused" alone is
satisfied by a runner that refuses every run, which spends nothing and verifies
nothing — the class of dishonesty p32 exists to remove. "Sample ⇒ not refused"
alone is satisfied by a runner that never refuses anything, which is the *worse*
direction: it reports a verification for a run measured entirely against
generated stand-ins.

**No real Glue session, no real S3 object, no JDBC anything, and no deploy.**
Every AWS call goes through a ``MagicMock``, and most of what follows asserts
that even the mock was never reached. No production code is modified by this
task.

The two axes, and why each value is on them
-------------------------------------------

Task 5.7's instruction is "cross frame mode against schema presence", taken
literally.

**Frame mode — three values, not two.**

* ``synthetic`` — no sample was requested. The default, and the mode
  ``frame_mode`` carries until task 9.3 supplies a URI.
* ``sample`` **and available** — an ``s3://`` object with a recognised format, so
  ``FrameSource.is_sample`` is true and the retirement applies.
* ``sample`` **but unavailable** — a sample was requested and the source has
  none, so the frame is generated and ``is_sample`` is false.

**The third value is the load-bearing one and is the reason the retirement is
conditional at all.** A JDBC-sourced pipeline's ``source_data_uri`` is
``jdbc://pipeline/<id>`` (``schema_extract/lambda_function.py:747``) and this
runner reads no JDBC source (R9.3, design C8, ``LGR-819980f-1`` — **standing
invariant**), so such a run requests a sample, cannot have one, and gets a
synthetic frame. A synthetic frame with no declared schema proves nothing. Had
the retirement been keyed on the *executor* — "real Spark, so we can read the
source", the shape a reader expects — it would report a full verification there,
on what is currently the only live path. So this file asserts that a requested
sample that fell back is refused **like a synthetic frame**, and asserts it
separately as well as inside the cross, because it is the case a plausible wrong
implementation gets wrong.

An unrecognised ``frame_mode`` is on the axis too, and it belongs with
``synthetic``: a typo must not retire a refusal, which is the safe direction.

**Schema presence — three values.**

* **declared** — 1..8 accepted-type fields; the refusal has nothing to be about.
* **absent** — every way of declaring nothing, which is more shapes than
  "``None`` or ``{}``". ``declared_source_fields``
  (``code_interpreter_runner.py:204-217``) reads
  ``schema.get("properties", schema.get("fields", {}))``, and p33 3.14 pins that
  ``properties`` wins whenever the KEY is present *even when its value is
  empty*. So a schema with an empty ``properties`` beside a populated ``fields``
  looks populated and declares nothing, and a ``properties`` that arrived as JSON
  ``null`` does too. Both are generated.
* **too wide** — past ``_MAX_TEST_FRAME_COLUMNS``, which is the arm the
  retirement must **not** touch (below).

The two arms are disjoint by construction, not by luck: ``_frame_refusal``
returns on ``not fields`` before it reaches the width comparison, and a schema
past the ceiling has fields by definition. The expected-reason helper below
mirrors that, and mirrors it from the requirements rather than from the
production function — which is what makes the biconditional falsifiable instead
of a tautology.

What the retirement must NOT do
-------------------------------

* **The width arm survives in both modes.** ``_MAX_TEST_FRAME_COLUMNS`` is a
  DynamoDB item-size bound on the pipeline-runs state row —
  ``null_counts``/``dtypes`` carry one entry per transformed column at ~45 bytes,
  so 512 columns is ~46 KB against a 400 KB item limit
  (``code_interpreter_runner.py:105-127``) — and a Sample_Frame writes the *same*
  row through the *same* parser (R10.4, design C5). ``sample_frame`` gates the
  reason, not the refusal.
* **The retirement has to be asked for.** ``sample_frame`` is keyword-only and
  defaults to ``False``, so the refusal is kept by default. The dangerous mistake
  here is retiring it by accident; keeping it by accident is merely a refused
  run. Both the default and the keyword-only-ness are asserted, because a
  positional third argument would let a caller retire the refusal by getting an
  argument order wrong.
* **A retired arm returns nothing, not a softened marker.** ``None`` — not a dict
  carrying ``not_verified_by_harness: False``, which would put a
  never-verified-anything key on a run that verified something.

Deliberately out of scope
-------------------------

**Task 5.6 owns the scheme claim** (Property 10 — a source is read only when its
scheme is ``s3``, generated over URIs including ``s3a://``, ``S3://``, ``s3:/x``
and the ``jdbc://`` fallback). This file generates no URI space: the frame-mode
axis is three *named* shapes used as inputs, one per value of
``FrameSource.is_sample`` × requested-ness, and every assertion here is about the
refusal that follows rather than about which URIs are readable. Where the mode
table's expected ``is_sample`` is checked, it is checked so that the refusal claim
is anchored to a known frame mode — not as a claim about URI parsing.

Task 8.1 owns mapping the refusal onto ``run_test``'s pre-flight order, and task
6.1 is concurrently adding the validation statement and the marker emission to
this module. Nothing here reads emitted statement text or ``error_details`` copy
for that reason: the assertions are on the Test_Result_Contract's
``not_verified_*`` keys, which are the fields R9.4/R9.5 are about and which
``tests/fixtures/run_test_result_shapes.py`` already pins.

Ledger notes
------------

* ``LGR-b2f79ef-9`` (**template / ratified**) is the governing record and it is
  the whole argument for the *conditional* half. It left
  ``_build_test_data_code``'s ``{id, value}`` fallback in place "but never
  scored", because reaching it means ``schema_extract`` produced no source schema,
  so "the frame does not represent the pipeline's input and any
  ``schema_compliance`` figure computed over it is a harness artifact" — a run
  built on it "reports **not verified** … rather than a score". A Sample_Frame
  removes that premise rather than the consequence: its columns come from the
  run's real source object, so there is no artifact to refuse to score. Its own
  revisit condition is a *different* route to the same place ("``schema_extract``
  gains a guarantee that it always writes a source schema, at which point the
  fallback is dead code"); this is not that, and the properties below are what
  pin that the fallback is still never scored on the synthetic arm.
* ``LGR-819980f-1`` (**standing invariant**) is why the sample-but-unavailable
  value exists at all rather than being a hypothetical: credentials reach Glue
  only by Secrets Manager ARN, a verification session is granted none, so a
  JDBC-sourced pipeline can never have a sample and its frame is always
  synthetic.
* ``LGR-3e45e65-1`` (**template / ratified**) pins ``AWS_REGION`` /
  ``AWS_DEFAULT_REGION`` by assignment in ``tests/unit/conftest.py``, so the
  ``setdefault`` calls below are consistent no-ops kept for the module-run case.
* **Provenance gap, stated rather than glossed.** ``why`` on
  ``glue_session_runner.py`` returns **nothing** — the file is not yet committed —
  and ``why`` on ``code_interpreter_runner.py:330`` (inside ``_frame_refusal``
  itself) returns one commit, ``60c4985b9d``, whose seven records are the p32
  test-gate work, all flagged BROAD at 64 files and none naming the frame
  refusal. ``LGR-b2f79ef-9`` was found only via
  ``decisions(scope="…/agentic_data_ingestion_test_code/**")``, which is the same
  gap task 5.5 reported. ``decisions(concept="schema")`` and
  ``decisions(concept="testing", include_backfilled=True)`` return nothing about
  frame construction or the retirement.
* Codegraph blast radius, recorded because the coverage flags on ``lambda/**``
  are unreliable (tasks.md Notes): ``frame_refusal_results`` — 18 call sites in
  ``glue_session_runner.py``, covered by ``test_frame_refusal_reuse.py``,
  ``test_frame_refusal_precedes_session_prop.py`` and
  ``test_sample_frame_dispatch.py``; ``resolve_frame_source`` — 8, and
  ``FrameSource`` — 6, both covered by ``test_sample_frame_dispatch.py``;
  ``NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA`` — 8 across both runner modules, with
  five covering test files. No zero-coverage symbol is touched, and this file
  adds no production code.

Sibling coverage
----------------

* ``test_sample_frame_dispatch.py`` — task 5.5's worked examples, including the
  named retirement cases this generalises.
* ``test_frame_refusal_precedes_session_prop.py`` — Property 8, which pins the
  refusal's *position* (before ``CreateSession``) with ``frame_mode`` held at
  ``synthetic`` precisely so that this file owns the mode axis.
* ``test_test_frame_width_prop.py`` — the same width rule on the pandas executor.
"""

import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings
from tests.fixtures.run_test_result_shapes import FRAME_REFUSAL

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
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    GlueSessionTestRunner,
    frame_refusal_results,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

TRANSFORM_STATEMENT = (STAGE_TRANSFORM, "df_t = df")

#: An ``s3://`` object whose extension resolves to a reader, so ``sample`` mode
#: reaches :data:`FRAME_MODE_SAMPLE`. One literal, not a generated space — see
#: the module docstring on task 5.6's ownership of the scheme claim.
S3_URI = "s3://adi-processed-files-123456789012-us-west-2/pipeline-a/orders.csv"

#: The ``schema_extract`` fallback, verbatim in shape
#: (``schema_extract/lambda_function.py:747``). The ordinary URI for a
#: JDBC-sourced pipeline, and therefore the ordinary way a requested sample is
#: unavailable.
JDBC_URI = "jdbc://pipeline/pipeline-a"

#: An ``s3://`` prefix — readable scheme, unrecognisable format. The *second*
#: way a requested sample is unavailable, included so "unavailable" is not
#: accidentally a property of the scheme arm alone.
S3_PREFIX_URI = "s3://adi-processed-files-123456789012-us-west-2/pipeline-a/"

#: Every declared type the frame builder accepts, read from the module rather
#: than transcribed so a bucket gaining a member widens this automatically.
_ACCEPTED_TYPES = sorted(
    glue_session_runner._INT_DECLARED_TYPES
    | glue_session_runner._FLOAT_DECLARED_TYPES
    | glue_session_runner._STRING_DECLARED_TYPES
)

#: The frame-mode axis: ``(label, frame_mode, source_data_uri, is_sample)``.
#:
#: Three values of the requirement's ``Test_Frame`` distinction, plus a typo.
#: ``is_sample`` is what R9.4/R9.5 turn on, and it is **false for four of the
#: five** — which is the asymmetry the conditional retirement exists to preserve.
_MODE_SHAPES = [
    ("synthetic-default", FRAME_MODE_SYNTHETIC, None, False),
    ("synthetic-with-a-readable-uri", FRAME_MODE_SYNTHETIC, S3_URI, False),
    ("sample-available", FRAME_MODE_SAMPLE, S3_URI, True),
    ("sample-unavailable-not-s3", FRAME_MODE_SAMPLE, JDBC_URI, False),
    ("sample-unavailable-no-format", FRAME_MODE_SAMPLE, S3_PREFIX_URI, False),
    # A typo must not retire a refusal: an unrecognised mode falls through to
    # the existing behaviour, which is `synthetic` (the R2.3 posture).
    ("unrecognised-mode", "Sample", S3_URI, False),
]

#: The modes whose frame is generated — every value of the axis but one. The
#: population R9.5 governs.
_SYNTHETIC_SHAPES = [shape for shape in _MODE_SHAPES if not shape[3]]

#: The modes whose frame is a real sample. The population R9.4 governs.
_SAMPLE_SHAPES = [shape for shape in _MODE_SHAPES if shape[3]]

#: A requested sample that could not be had — the load-bearing value, isolated.
_FELL_BACK_SHAPES = [shape for shape in _MODE_SHAPES if shape[1] == FRAME_MODE_SAMPLE and not shape[3]]

#: No frame is built by :func:`frame_refusal_results`, so widths cost nothing
#: there; the run-level properties draw from the narrower strategies below.
_SETTINGS = settings(max_examples=200, deadline=None)

#: For the properties that build a real frame on every example. A synthetic
#: frame emits ~100 rows × width of generated code, so the admitted side is kept
#: narrow.
_RUN_SETTINGS = settings(max_examples=60, deadline=None)


# ── The mock Glue client ──────────────────────────────────────────────


def _happy_glue_client() -> MagicMock:
    """A Glue client wired so that a session, if reached, succeeds.

    Wired for the happy path deliberately: if ``create_session`` returned a bare
    ``MagicMock``, "the retired arm reaches a session" could be satisfied by a
    client too broken to create one, and R9.4's positive side would be
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
def _mock_runner(frame_mode: str, source_data_uri: str | None):
    """Yield ``(runner, glue)`` over a fresh mock client, sleeping patched out.

    A context manager entered inside each ``@given`` body rather than a fixture:
    a function-scoped fixture is applied once per test *function* and is never
    reset between Hypothesis examples, so a mock built there accumulates calls
    across examples and every call-count assertion measures the wrong thing. The
    reasoning ``test_frame_refusal_precedes_session_prop.py`` records for the
    same helper.
    """
    glue = _happy_glue_client()
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield (
            GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, frame_mode=frame_mode, source_data_uri=source_data_uri),
            glue,
        )


# ── Generators ────────────────────────────────────────────────────────


def _fields(width: int, declared_type: str) -> dict:
    """``width`` declared fields, all of one accepted type."""
    return {f"col_{index}": {"type": declared_type} for index in range(width)}


@st.composite
def _absent_schemas(draw):
    """Every way of declaring nothing (the R9.5 antecedent's second half).

    More shapes than ``None`` or ``{}``, because ``declared_source_fields`` reads
    ``schema.get("properties", schema.get("fields", {}))`` and p33 3.14 pins that
    ``properties`` wins whenever the KEY is present — even when its value is
    empty or ``null``. So the last three shapes *look* populated, or at least
    non-trivial, and declare nothing. The payload that must be ignored is
    generated rather than fixed, so "it is ignored" is checked against varying
    content.
    """
    ignored = _fields(draw(st.integers(min_value=1, max_value=4)), draw(st.sampled_from(_ACCEPTED_TYPES)))
    return draw(
        st.sampled_from(
            [
                None,
                {},
                {"properties": {}},
                {"fields": {}},
                # No container key at all — both `.get`s fall through to `{}`.
                {"type": "object", "title": "declares nothing"},
                # Looks populated, declares nothing (p33 3.14).
                {"properties": {}, "fields": ignored},
                # `properties` arrived as JSON null. The key is present, so it
                # still wins, and `not None` is true.
                {"properties": None, "fields": ignored},
            ]
        )
    )


@st.composite
def _declared_schemas(draw, widths=st.integers(min_value=1, max_value=8)):
    """A schema that declares fields a frame can carry. Returns ``(schema, count)``."""
    width = draw(widths)
    fields = _fields(width, draw(st.sampled_from(_ACCEPTED_TYPES)))
    container = draw(st.sampled_from(("properties", "fields")))
    return ({"type": "object", "properties": fields} if container == "properties" else {"fields": fields}), width


@st.composite
def _too_wide_schemas(draw):
    """A schema past the ceiling — the arm the retirement must not touch.

    Widths start at ``ceiling + 1`` because the boundary itself is Property 8's
    (``test_frame_refusal_precedes_session_prop.py`` walks the ±3 window). What
    is at stake here is which *mode* the refusal survives in, not where the
    boundary sits.
    """
    over = draw(st.integers(min_value=1, max_value=16))
    width = _MAX_TEST_FRAME_COLUMNS + over
    return {"properties": _fields(width, draw(st.sampled_from(_ACCEPTED_TYPES)))}, width


@st.composite
def _any_schemas(draw):
    """The schema-presence axis, all three values. Returns ``(schema, count)``."""
    presence = draw(st.sampled_from(("absent", "declared", "too_wide")))
    if presence == "absent":
        return draw(_absent_schemas()), 0
    if presence == "declared":
        return draw(_declared_schemas())
    return draw(_too_wide_schemas())


def _expected_reason(*, is_sample: bool, declared_count: int) -> str | None:
    """The reason R9.4/R9.5 and R10.4 require, in the requirements' own terms.

    Written from the requirements, not from ``_frame_refusal`` — which is what
    makes the properties below a check rather than a restatement that would
    agree with the production code under any rewrite.

    * ``declared_count == 0`` is R9.5's "no source schema is available". Refused
      **unless** the frame is a Sample_Frame, which is R9.4: the sample supplies
      the columns the schema would have declared, so there is nothing left for
      this arm to be about.
    * ``declared_count > ceiling`` is refused in **either** mode. The ceiling is a
      DynamoDB item-size bound on the state row and a Sample_Frame writes the
      same row (R10.4, design C5), so ``sample_frame`` gates the reason and not
      the refusal.
    * The two arms are disjoint: nothing declared cannot also be too wide.
    """
    if declared_count == 0:
        return None if is_sample else NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
    if declared_count > _MAX_TEST_FRAME_COLUMNS:
        return NOT_VERIFIED_REASON_FRAME_TRUNCATED
    return None


def _assert_no_glue_call(glue: MagicMock, context: str) -> None:
    """Nothing at all was asked of Glue.

    ``mock_calls`` rather than ``create_session.assert_not_called()``: the honest
    reading of "reports a Harness_Non_Verification" for a host-side refusal is
    that no session API was reached at all, and asserting the whole call list
    also catches a later edit that reaches Glue by some other route before the
    refusal is evaluated.
    """
    assert glue.mock_calls == [], f"a refused run reached Glue: {glue.mock_calls} ({context})"


# ── Property 11, on the refusal itself ───────────────────────────────


@given(shape=_any_schemas(), is_sample=st.booleans())
@_SETTINGS
def test_the_absent_schema_refusal_is_reported_exactly_when_the_frame_is_synthetic(shape, is_sample):
    """Property 11. **Validates: Requirements 9.4, 9.5**

    The biconditional, over the full cross of frame mode against schema
    presence: ``frame_refusal_results`` reports the absent-schema reason **iff**
    the frame is not a Sample_Frame *and* the schema declares nothing.

    Both directions in one property on purpose. R9.5 alone is satisfied by a
    runner that refuses everything; R9.4 alone by one that refuses nothing. The
    second is the more dangerous failure — it reports a verification for a run
    measured against generated stand-ins, which is exactly what
    ``LGR-b2f79ef-9`` refused to score.
    """
    source_schema, declared_count = shape
    expected = _expected_reason(is_sample=is_sample, declared_count=declared_count)

    results = frame_refusal_results(source_schema, sample_frame=is_sample)

    if expected is None:
        assert results is None, (
            f"{declared_count} declared field(s), is_sample={is_sample}: expected no refusal, got "
            f"{results and results.get('not_verified_reason')!r}"
        )
        return

    assert results is not None, f"{declared_count} declared field(s), is_sample={is_sample}: expected {expected!r}"
    assert results["not_verified_reason"] == expected
    assert results["not_verified_by_harness"] is True
    assert results["not_verified_field_count"] == declared_count
    assert results["not_verified_frame_ceiling"] == _MAX_TEST_FRAME_COLUMNS
    # Nothing ran, so nothing may claim to have. Property 9 (task 3.7) states
    # this over every reason; repeated here because a retirement that softened
    # the marker instead of removing it would pass the reason check above.
    assert results["executed"] is False
    assert results["execution_successful"] is False
    assert results["exit_code"] is None
    # Empty rather than absent: this is what suppresses the `schema_compliance`
    # score, so a frame the harness could not build cannot report a shortfall
    # against the transform (design C13).
    assert results["data_quality_results"] == {}
    assert set(results) == set(FRAME_REFUSAL.types)


@given(source_schema=_absent_schemas())
@_SETTINGS
def test_a_retired_arm_returns_nothing_rather_than_a_softened_marker(source_schema):
    """**Validates: Requirement 9.4**

    "Report no Harness_Non_Verification" means ``None``, not a dict carrying
    ``not_verified_by_harness: False``. The latter would put a
    nothing-was-verified key onto a run that verified something against real
    columns, and every consumer of the contract — ``is_not_verified_check``, the
    verified-check partition, the harness branch that renders the reason as text
    — reads the key's presence.
    """
    assert frame_refusal_results(source_schema, sample_frame=True) is None


@given(source_schema=_absent_schemas())
@_SETTINGS
def test_keeping_the_refusal_is_the_default_and_asking_is_the_only_way_out(source_schema):
    """**Validates: Requirement 9.5**

    The default matters as much as the behaviour. The dangerous mistake is
    retiring the refusal by accident — a run reported as verified against
    generated data — where keeping it by accident is merely a refused run. So an
    omitted ``sample_frame`` behaves as ``False``, and the two spellings agree.
    """
    omitted = frame_refusal_results(source_schema)
    explicit = frame_refusal_results(source_schema, sample_frame=False)

    assert omitted is not None
    assert omitted["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
    assert omitted == explicit


def test_the_retirement_cannot_be_triggered_positionally():
    """**Validates: Requirement 9.5**

    ``sample_frame`` is keyword-only, so no caller can retire the refusal by
    getting an argument order wrong. Structural rather than behavioural, and
    that is the point: the argument that disarms a safety refusal should be
    impossible to pass by accident.
    """
    with pytest.raises(TypeError):
        frame_refusal_results({}, True)  # type: ignore[misc]


@given(shape=_too_wide_schemas(), is_sample=st.booleans())
@_SETTINGS
def test_the_width_refusal_survives_in_both_modes(shape, is_sample):
    """**Validates: Requirements 9.4, 9.5** (the boundary of the retirement)

    ``sample_frame`` gates the *reason*, not the refusal.
    ``_MAX_TEST_FRAME_COLUMNS`` is derived from the DynamoDB item-size limit on
    the pipeline-runs state row, and a Sample_Frame is read into the same
    ``null_counts``/``dtypes`` maps on the same row through the same parser
    (R10.4, design C5) — so neither side of that arithmetic moves. A retirement
    that took the width arm with it would admit a frame silently missing columns
    the transform references, whose symptom is a false **low**
    ``schema_compliance`` attributed to the transform.
    """
    source_schema, declared_count = shape
    results = frame_refusal_results(source_schema, sample_frame=is_sample)

    assert results is not None, f"width {declared_count} was admitted with is_sample={is_sample}"
    assert results["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED
    assert results["not_verified_field_count"] == declared_count
    # And the report is byte-identical across the modes it survived, so the
    # retirement changed nothing about what a reviewer of a wide schema reads.
    assert results == frame_refusal_results(source_schema, sample_frame=not is_sample)


@given(shape=_declared_schemas())
@_SETTINGS
def test_a_declared_schema_is_admitted_in_both_modes(shape):
    """The positive control on the refusal itself. **Validates: Requirement 9.5**

    Without it, every assertion above is satisfied by a function that refuses
    every input. The retirement changes which *arms* fire, not which schemas are
    admitted.
    """
    source_schema, _ = shape

    assert frame_refusal_results(source_schema) is None
    assert frame_refusal_results(source_schema, sample_frame=True) is None


# ── Property 11, through the runner that resolves the mode ───────────


@given(mode_shape=st.sampled_from(_MODE_SHAPES), shape=_any_schemas())
@_RUN_SETTINGS
def test_a_guarded_run_reports_the_absent_schema_refusal_exactly_when_it_is_true(mode_shape, shape):
    """Property 11 where the mode is *resolved* rather than asserted.

    **Validates: Requirements 9.4, 9.5**

    The property above takes ``is_sample`` as a boolean; this one crosses the
    real frame-mode axis, so the retirement is keyed on the frame the runner
    actually decided to build. That is the distinction the whole design turns on
    — ``FrameSource.is_sample``, not the executor and not the requested mode.

    A refused run reaches no Glue API; an admitted one reaches a created session
    with the frame statement first. The second half is what stops "not refused"
    from being satisfied by a run that never got anywhere.
    """
    label, frame_mode, source_data_uri, is_sample = mode_shape
    source_schema, declared_count = shape
    expected = _expected_reason(is_sample=is_sample, declared_count=declared_count)
    context = f"{label}, {declared_count} declared field(s)"

    with _mock_runner(frame_mode, source_data_uri) as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        # Anchor: the mode table's expectation is the frame the runner chose.
        # Not a claim about URI parsing (task 5.6's) — it is what makes the
        # refusal assertion below attributable to a known frame mode.
        assert run.frame_source is not None
        assert run.frame_source.is_sample is is_sample, context

        assert run.refused is (expected is not None), (
            f"{context}: expected refusal reason {expected!r}, got "
            f"{run.refusal and run.refusal.get('not_verified_reason')!r}"
        )

        if expected is not None:
            assert run.refusal is not None
            assert run.refusal["not_verified_reason"] == expected
            assert run.refusal["not_verified_by_harness"] is True
            _assert_no_glue_call(glue, context)
            assert run.outcomes == []
        else:
            assert run.refusal is None
            glue.create_session.assert_called_once()
            assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]


@given(mode_shape=st.sampled_from(_FELL_BACK_SHAPES), source_schema=_absent_schemas())
@_RUN_SETTINGS
def test_a_requested_sample_that_was_unavailable_is_refused_like_a_synthetic_frame(mode_shape, source_schema):
    """The case the conditional retirement exists for. **Validates: Requirement 9.5**

    A JDBC-sourced pipeline requests a sample, cannot have one, and gets a
    generated frame — and a generated frame with no declared schema proves
    nothing (``LGR-b2f79ef-9``). Retiring the refusal on the *executor* rather
    than on the *frame* would report a full verification here, on what is
    currently the only live path, because ``frame_mode`` defaults to synthetic and
    a JDBC ``source_data_uri`` is unreadable by construction
    (``LGR-819980f-1``, **standing invariant**).

    Stated separately from the cross above because it is the case a plausible
    wrong implementation gets wrong, and because "behaves like synthetic" must not
    mean "indistinguishable from synthetic": the run still records that a sample
    was requested and was unavailable (R9.2), so a reader can tell the two apart.
    """
    label, frame_mode, source_data_uri, _ = mode_shape

    with _mock_runner(frame_mode, source_data_uri) as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert run.refused, f"{label} with no declared schema was admitted"
        assert run.refusal is not None
        assert run.refusal["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
        _assert_no_glue_call(glue, label)

        # Refused like a synthetic frame, still distinguishable from one.
        assert run.frame_source is not None
        assert run.frame_source.sample_requested is True
        assert run.frame_source.sample_unavailable is True
        assert run.frame_source.is_sample is False


@given(mode_shape=st.sampled_from(_SAMPLE_SHAPES), source_schema=_absent_schemas())
@_RUN_SETTINGS
def test_a_sample_frame_with_no_declared_schema_reaches_a_session(mode_shape, source_schema):
    """R9.4's positive side, at the runner. **Validates: Requirement 9.4**

    The retirement is only worth anything if the run then happens. Asserted one
    step past ``create_session`` — a frame statement and the transform statement
    both submitted — so "not refused" means the run reached Spark rather than
    that a session object was allocated.

    Every way of declaring nothing is generated here, including the shapes that
    look populated: a sample frame does not consult the schema at all, so all of
    them must behave identically.
    """
    _label, frame_mode, source_data_uri, _ = mode_shape

    with _mock_runner(frame_mode, source_data_uri) as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert not run.refused
        assert run.refusal is None
        assert run.frame_source is not None and run.frame_source.is_sample
        glue.create_session.assert_called_once()
        assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]


@given(mode_shape=st.sampled_from(_SYNTHETIC_SHAPES), source_schema=_absent_schemas())
@_RUN_SETTINGS
def test_every_synthetic_mode_keeps_the_refusal_for_every_absent_schema(mode_shape, source_schema):
    """R9.5 over the whole synthetic side of the axis. **Validates: Requirement 9.5**

    Four of the five mode values build a generated frame — the default, the
    default with a readable URI that nobody asked to sample, the two fallbacks,
    and a typo'd mode. All four must keep the refusal, and the refusal must be
    the pandas path's own report rather than a re-derived one, which is what
    makes a Spark-side refusal and a Code Interpreter refusal literally the same
    thing a reviewer reads.
    """
    label, frame_mode, source_data_uri, _ = mode_shape

    with _mock_runner(frame_mode, source_data_uri) as (runner, glue):
        run = runner.run_guarded_session(source_schema, [TRANSFORM_STATEMENT])

        assert run.refused, f"{label} with no declared schema was admitted"
        assert run.refusal == frame_refusal_results(source_schema)
        _assert_no_glue_call(glue, label)
