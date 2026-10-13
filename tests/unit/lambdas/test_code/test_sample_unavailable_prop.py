# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — an unavailable sample is a verified synthetic run.

Spec `p44-verify-against-real-source-data` task 6.3. Requirements 2.6, 2.7, 4.2,
4.4, 4.5; design §4.10 (the JDBC path).

**Feature: p44-verify-against-real-source-data, Property 5: An unavailable
sample is a verified synthetic run with no non-verification marker.**

The universal: *for any* URI for which a requested sample cannot be had — a
``jdbc://`` URI, any other non-``s3://`` URI, an ``s3://`` prefix with no
extension, an ``s3://`` object whose extension nothing recognises — the run
builds a Synthetic_Frame, records the unavailability reason on the
:class:`FrameSource`, carries **no** ``not_verified_reason`` traceable to the
missing sample, and leaves the absent-schema arm of the frame refusal armed.

Two halves, and they pull in opposite directions, which is why both are here
--------------------------------------------------------------------------
* **The no-marker half is C9 and do-NOT 7 quantified.** The two
  ``SAMPLE_UNAVAILABLE_*`` values are deliberately *not*
  ``not_verified_reason`` values — ``glue_session_runner.py:213-229``, read in
  full: "a fallback to ``synthetic`` is a fully verified run against a generated
  frame — exactly what ``frame_mode='synthetic'`` produces on purpose — so
  attaching a non-verification marker to it would report nothing-was-verified
  about a run that verified something."
* **The armed-refusal half is R2.7.** ``frame_refusal_results(...,
  sample_frame=frame_source.is_sample)`` is called with ``False`` on this path,
  so a fallen-back run with no declared schema is still refused with
  ``no_source_schema``. That is the **existing** behaviour and this file
  **asserts it rather than altering it**.

The two are only compatible because the marker they forbid and the marker they
require are *different facts*. So the no-marker half is not stated as "the
result carries no ``not_verified_reason``" — that would be false on the
absent-schema arm, and weakening it to the declared-schema arm alone would let
the interesting case escape. It is stated the way C9 states it: **no
sample-availability value ever becomes a non-verification marker.** A missing
sample is a fact about the source; ``no_source_schema`` is a fact about the
declared schema. On a run with a declared schema there is then no
``not_verified_reason`` at all, which is asserted too.

This is also the JDBC path's property (design §4.10)
----------------------------------------------------
A JDBC run is a **requested-and-refused** run: not a failure, not a silent
synthetic. `_jdbc_uris` covers both shapes a JDBC-sourced pipeline can carry —
``jdbc://{engine}/{schema}/{table}`` from ``file_validation``, and
``schema_extract``'s ``jdbc://pipeline/{pipeline_id}`` fallback
(``schema_extract/lambda_function.py:747``). R4.4 and R4.5 are the same code
path; a watermark predicate changes nothing this module can see, so both land on
``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE``.

``LGR-819980f-1`` (**standing invariant**) is the fence behind that arm:
credentials reach Glue only by Secrets Manager ARN, a Verification_Session is
granted none, and do-NOT 3 forbids a JDBC arm in ``build_frame_code``. So a
``jdbc://`` URI must be **unreachable for a read**, not merely unhandled —
which is why `test_no_unavailable_uri_reaches_the_submitted_code` asserts the URI
appears in no statement that would reach Spark, rather than only asserting the
dispatch chose the synthetic arm.

No Glue session is created and no data source is contacted
----------------------------------------------------------
do-NOT 1 and do-NOT 2. Every runner is built with
``glue_session_runner.boto3.client`` patched, so ``self.client`` is a
``MagicMock`` and **no real client exists to call** — that is the load-bearing
measure, and it is stated rather than assumed because task 5.1's execution walked
into the alternative: a sample-mode run with a *readable* ``s3://`` URI reaches
the real ``CreateSession`` even with ``source_schema=None``, because ``is_sample``
retires the absent-schema arm and nothing is then left to refuse. Every draw here
is an *unavailable* URI, so the sample arm is unreachable by construction — and
`test_the_generator_draws_only_unavailable_uris` asserts that antecedent instead
of trusting it, because a generator that leaked one readable URI would take that
path. On the refusal arm ``create_session`` is additionally asserted *unused*.

Expectations come from `requirements.md` R2.6 / R2.7 / R4.2 / R4.4 / R4.5,
design §4.10 and the comment at ``glue_session_runner.py:213-229`` — never read
back from what the runner currently returns (``LGR-28b4d75-5``, **template /
ratified**: a test written from the producer's output cannot detect the producer
drifting, because the output is its own oracle).
"""

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    GlueSessionTestRunner,
    build_synthetic_frame_code,
    frame_build_statement,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: A transform the frame's one declared column supports, so a success outcome is
#: reachable and the "verified run" half of the property is not vacuous.
TRANSFORM = "df_transformed = df.withColumn('name', initcap(col('name')))"

TARGET_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

#: A schema the synthetic builder can type. Present, so the absent-schema arm
#: does **not** fire and the run proceeds — which is the state the no-marker half
#: is about.
BUILDABLE_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

#: Both markers, in the form ``_parse_results`` reads, so a "verified run" is a
#: PARSED success rather than an asserted one.
GENERATED_STDOUT = 'GENERATED:{"record_count": 100, "columns": ["name"]}'
VALIDATION_STDOUT = 'VALIDATION:{"record_count": 100, "schema_compliance": 100.0}'

_STAGE_COUNT = 3

#: Every key that carries a non-verification, from either producer. Scanned for
#: as a set so a *new* marker added later is caught here rather than surfacing as
#: an unexplained field on a run that verified something.
NOT_VERIFIED_KEYS = frozenset(
    {
        "not_verified_by_harness",
        "not_verified_by_proxy",
        "not_verified_reason",
        "not_verified_field_count",
        "not_verified_frame_ceiling",
    }
)

#: The values C9 forbids from ever becoming a ``not_verified_reason``. The two
#: sample-availability reasons, plus p44's own source-read reason — which belongs
#: to a sample that was *built and failed to read*, a different fact from a
#: sample that was never built at all (R2.1 versus R2.6).
FORBIDDEN_REASON_VALUES = frozenset(
    {
        SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
        SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
        NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    }
)


# ---------------------------------------------------------------------------
# The unavailable-URI space, one strategy per arm
# ---------------------------------------------------------------------------

_IDENTIFIERS = st.from_regex(r"[a-z][a-z0-9_-]{1,14}", fullmatch=True)

#: The engines ``file_validation`` writes into a ``jdbc://`` URI.
_JDBC_ENGINES = ("mysql", "postgresql", "postgres", "oracle", "sqlserver", "mariadb")

#: Schemes that are not ``s3://``. ``s3a``/``s3n`` are Hadoop spellings and
#: ``S3`` is the case ``is_s3_source_uri`` deliberately does not normalise — all
#: three are shapes ``urllib.parse.urlsplit`` would have reported as scheme
#: ``s3``, which is why the predicate compares a literal prefix.
_NON_S3_SCHEMES = ("s3a", "s3n", "S3", "https", "http", "file", "gs", "abfss", "wasbs", "hdfs")

#: Degenerate URIs that carry no bucket, or no scheme at all. Fixed rather than
#: generated because each one is a named shape in ``is_s3_source_uri``'s own
#: table of refusals.
_DEGENERATE_URIS = (
    "s3:/bucket/orders.csv",
    "s3:bucket/orders.csv",
    "s3://",
    "s3:///orders.csv",
    "s3://   /orders.csv",
    "/bucket/orders.csv",
    "bucket/orders.csv",
    "",
    "   ",
)

#: Extensions outside ``_SOURCE_FORMAT_BY_EXTENSION``. ``gz`` alone is the
#: interesting one: it is a compression suffix with nothing under it, so
#: ``sample_read_format`` must not look through it to nothing.
#:
#: ``txt`` was drawn here until p64 1c.1. R3.1 added it to the mirror as a member
#: of the ``csv`` family, so it now resolves to a readable format and drawing it
#: as unrecognised asserted the opposite of the truth. ``xlsx`` is the entry that
#: shows why this list is not simply "outside ``FORMAT_MAP``": it IS in the
#: mirror, and is still unrecognised here, because ``excel`` has no
#: ``_SAMPLE_READERS`` entry.
_UNRECOGNISED_EXTENSIONS = ("xlsx", "xls", "log", "zip", "gz", "bz2", "pdf", "yaml", "db")


@st.composite
def _jdbc_uris(draw: st.DrawFn) -> str:
    """Both shapes a JDBC-sourced pipeline's ``source_data_uri`` can carry.

    ``jdbc://{engine}/{schema}/{table}`` is what ``file_validation`` writes;
    ``jdbc://pipeline/{pipeline_id}`` is ``schema_extract``'s fallback when the
    previous state carried nothing (``schema_extract/lambda_function.py:747``).
    R4.4 and R4.5 are the same code path — a watermark predicate is invisible
    from here — so both arms of Requirement 4's JDBC half draw from this.
    """
    if draw(st.booleans()):
        return f"jdbc://{draw(st.sampled_from(_JDBC_ENGINES))}/{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}"
    return f"jdbc://pipeline/{draw(_IDENTIFIERS)}"


@st.composite
def _other_non_s3_uris(draw: st.DrawFn) -> str:
    """A URI with a scheme, and the scheme is not ``s3``."""
    scheme = draw(st.sampled_from(_NON_S3_SCHEMES))
    return f"{scheme}://{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}.csv"


@st.composite
def _s3_uris_with_no_extension(draw: st.DrawFn) -> str:
    """R4.2's multi-file prefix, and the two other extension-less S3 shapes.

    The prefix form is the **observed** instance rather than a hypothetical:
    ``file_validation`` writes ``original_s3_uri = source_s3_uri`` for a
    multi-file upload (``file_validation/lambda_function.py:368``), and a prefix
    carries no extension.
    """
    bucket = f"adi-source-files-{draw(_IDENTIFIERS)}"
    shape = draw(st.sampled_from(("trailing-slash", "bucket-only", "bare-key", "dotted-directory")))
    if shape == "bucket-only":
        return f"s3://{bucket}"
    if shape == "bare-key":
        return f"s3://{bucket}/{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}"
    if shape == "dotted-directory":
        # `s3://b/my.dir/orders` — the dot is in a directory segment, so the
        # last path segment still carries no extension.
        return f"s3://{bucket}/{draw(_IDENTIFIERS)}.dir/{draw(_IDENTIFIERS)}"
    return f"s3://{bucket}/{draw(_IDENTIFIERS)}/"


@st.composite
def _s3_uris_with_unrecognised_extension(draw: st.DrawFn) -> str:
    """An ``s3://`` object whose extension is outside ``FORMAT_MAP``'s content."""
    bucket = f"adi-source-files-{draw(_IDENTIFIERS)}"
    return f"s3://{bucket}/{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}.{draw(st.sampled_from(_UNRECOGNISED_EXTENSIONS))}"


def _scheme_bearing_unavailable_uris() -> st.SearchStrategy[str]:
    """Every arm whose URI is a non-empty string carrying ``://``.

    Split out from :func:`_unavailable_uris` for the one assertion that searches
    the *submitted code* for the URI: a degenerate draw like ``""`` or ``"   "``
    is a substring of anything, and ``None`` has no textual form, so including
    them would make that assertion vacuous rather than stronger.
    """
    return st.one_of(
        _jdbc_uris(),
        _other_non_s3_uris(),
        _s3_uris_with_no_extension(),
        _s3_uris_with_unrecognised_extension(),
    )


def _unavailable_uris() -> st.SearchStrategy[Any]:
    """The whole antecedent: every URI for which a requested sample cannot be had.

    ``None`` and the non-strings are in because ``source_data_uri`` is read off a
    payload (``lambda_function.py``'s ``or None`` normalises only ``""``), so
    they are reachable inputs rather than invented ones — and
    ``is_s3_source_uri`` accepts ``Any`` for exactly that reason.
    """
    return st.one_of(
        _scheme_bearing_unavailable_uris(),
        st.sampled_from(_DEGENERATE_URIS),
        st.none(),
        st.sampled_from((0, 1, b"s3://bucket/orders.csv", ["s3://bucket/orders.csv"], {}, ())),
    )


def _expected_reason(source_uri: Any) -> str:
    """Which of the two fallback reasons *source_uri* must record.

    Derived from the requirements' own split — R4.4/R4.5 name
    ``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE`` for a non-S3 source, and R4.2 names "no
    recognisable format" for a prefix — not from ``resolve_frame_source``'s
    branch order. The two are actionable differently: one says "this source is
    not S3", the other says "this source is S3 but nothing says how to read it".
    """
    if not isinstance(source_uri, str) or not source_uri.startswith("s3://"):
        return SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    # "a literal ``s3://`` prefix with a non-empty bucket", and a whitespace
    # bucket is not a bucket — ``is_s3_source_uri``'s own table of refusals lists
    # ``s3://   /key`` among them. Transcribed from that table rather than by
    # calling the predicate, so this oracle can disagree with it.
    bucket = source_uri[len("s3://") :].split("/", 1)[0].strip()
    return SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT if bucket else SAMPLE_UNAVAILABLE_NOT_S3_SOURCE


# ---------------------------------------------------------------------------
# Driving a run, with no real client anywhere
# ---------------------------------------------------------------------------


def _ok_statement(text: str = "") -> dict:
    return {"Statement": {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}}


def _client() -> MagicMock:
    """A Glue client double wired for a clean three-statement run.

    Every statement reports ``ok``; stage 0 prints the ``GENERATED:`` marker and
    stage 2 the ``VALIDATION:`` one, so ``_parse_results`` produces the success
    rather than this helper asserting one.
    """
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": index + 1} for index in range(_STAGE_COUNT)]
    client.get_statement.side_effect = [
        _ok_statement(GENERATED_STDOUT),
        _ok_statement(),
        _ok_statement(VALIDATION_STDOUT),
    ]
    return client


def _make_runner(client: MagicMock, source_uri: Any) -> GlueSessionTestRunner:
    """A ``sample``-mode runner over *client*, with no real boto3 client built.

    The patch is the measure that makes do-NOT 1 structural rather than
    aspirational: ``self.client`` is *client*, so there is no AWS client in
    existence for any path — reached or unreached — to call.
    """
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=FRAME_MODE_SAMPLE,
            source_data_uri=source_uri,
        )
    assert runner.client is client, "guard: a real or absent client would make every property below vacuous"
    return runner


def _run(source_uri: Any, source_schema: dict | None) -> tuple[dict, GlueSessionTestRunner, MagicMock]:
    """Drive one run through the composed public method and return everything.

    ``run_test`` rather than a builder, because Property 5 is a claim about what
    a caller receives: the routing between the frame decision, the refusal and
    the result mapping is exactly where a marker could be picked up.
    """
    client = _client()
    runner = _make_runner(client, source_uri)
    with patch("glue_session_runner.time.sleep"):
        result: dict = runner.run_test(TRANSFORM, source_schema, TARGET_SCHEMA)
    return result, runner, client


def _submitted(client: MagicMock) -> list[str]:
    return [call.kwargs["Code"] for call in client.run_statement.call_args_list]


def _forbidden_values_in(payload: Any) -> list[str]:
    """Every :data:`FORBIDDEN_REASON_VALUES` member reachable anywhere in *payload*.

    Recursive, and over values rather than over one key, because C9's claim is
    about the *value* escaping into a non-verification channel — a sample reason
    copied into ``error_details`` or nested inside ``data_quality_results``
    would be the same defect wearing a different key.
    """
    found: list[str] = []
    if isinstance(payload, str):
        found += [value for value in sorted(FORBIDDEN_REASON_VALUES) if value in payload]
    elif isinstance(payload, dict):
        for key, value in payload.items():
            found += _forbidden_values_in(key)
            found += _forbidden_values_in(value)
    elif isinstance(payload, (list, tuple, set, frozenset)):
        for item in payload:
            found += _forbidden_values_in(item)
    return found


# ---------------------------------------------------------------------------
# Property 5 — the frame is synthetic, and the reason is recorded (R2.6)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris())
def test_an_unavailable_sample_builds_a_synthetic_frame(source_uri: Any):
    """R2.6, first clause: the frame is generated from the declared schema.

    **Validates: Requirements 2.6, 4.2, 4.4, 4.5**

    Asserted over the statement that would reach Spark — byte-equal to
    ``frame_build_statement`` on a synthetic source — rather than over
    ``is_sample``, because the emitted code is the thing that decides what the
    session actually reads. A dispatch that reported synthetic while emitting a
    read would pass the weaker form.
    """
    _, runner, client = _run(source_uri, BUILDABLE_SCHEMA)

    submitted = _submitted(client)
    assert submitted, "guard: a run that submitted nothing cannot show which frame it built"
    assert submitted[0].startswith(build_synthetic_frame_code(BUILDABLE_SCHEMA)), (
        f"a requested sample that could not be had must build a Synthetic_Frame (R2.6); "
        f"source_data_uri={source_uri!r} emitted {submitted[0][:120]!r}"
    )
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.is_sample is False
    assert submitted[0] == frame_build_statement(runner.last_frame_source, BUILDABLE_SCHEMA)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris(), source_schema=st.sampled_from((BUILDABLE_SCHEMA, {}, None)))
def test_an_unavailable_sample_records_its_reason_and_what_was_asked_for(source_uri: Any, source_schema: dict | None):
    """R2.6, second clause: the reason is recorded, and the request survives it.

    **Validates: Requirements 2.6, 4.2, 4.4, 4.5**

    Both halves, because R9.2's own distinction is between them:
    ``requested_mode`` still says ``sample``, which is what makes "a sample was
    requested and the source has none" distinguishable from "no sample was
    requested". Over all three schema arms, so the record survives the refusal as
    well as the run — that ordering is what task 5.1 exists for.
    """
    _, runner, _ = _run(source_uri, source_schema)

    frame_source = runner.last_frame_source
    assert frame_source is not None, "the decision must be readable on every route out of a run"
    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.requested_mode == FRAME_MODE_SAMPLE
    assert frame_source.sample_requested is True
    assert frame_source.sample_unavailable is True
    assert frame_source.unavailable_reason == _expected_reason(source_uri)
    assert frame_source.read_format is None
    # The offending URI is kept, so a reader can see WHICH source had no sample.
    assert frame_source.source_uri == source_uri
    provenance = frame_source.provenance()
    assert provenance["frame"] == "sample_unavailable"
    assert provenance["row_bound"] is None


# ---------------------------------------------------------------------------
# Property 5 — the no-marker half (C9, do-NOT 7)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris())
def test_a_fallen_back_run_with_a_schema_is_a_verified_run(source_uri: Any):
    """C9: a fallback to synthetic is a fully verified run, so it carries no marker.

    **Validates: Requirements 2.6**

    ``glue_session_runner.py:213-229``, read in full: attaching a
    non-verification marker here "would report nothing-was-verified about a run
    that verified something". Asserted as key **absence** rather than as a falsy
    value, because ``aggregate_check_results`` and the dashboard's label map both
    dispatch on presence — ``not_verified_by_harness: False`` would still put the
    key on the wire.

    The positive half is what makes the negative one mean something: this run
    ``executed``, reported an ``exit_code`` of 0 and a parsed
    ``execution_successful``. A result that verified nothing would satisfy "no
    marker" trivially.
    """
    result, _, _ = _run(source_uri, BUILDABLE_SCHEMA)

    present = sorted(NOT_VERIFIED_KEYS & result.keys())
    assert not present, (
        f"a requested-but-unavailable sample produced non-verification key(s) {present} for "
        f"source_data_uri={source_uri!r} — C9 and do-NOT 7 forbid it: the run verified the "
        f"transform against a generated frame, which is what synthetic mode does on purpose"
    )
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["execution_successful"] is True, "guard: 'no marker' is trivial on a run that verified nothing"


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris(), source_schema=st.sampled_from((BUILDABLE_SCHEMA, {}, None)))
def test_no_sample_availability_reason_ever_becomes_a_non_verification(source_uri: Any, source_schema: dict | None):
    """C9 stated so it survives the arm where a ``not_verified_reason`` is correct.

    **Validates: Requirements 2.6, 2.7**

    On the absent-schema arm the result *does* carry a ``not_verified_reason`` —
    ``no_source_schema``, which R2.7 requires — so "no marker" cannot be the
    universal. What is universal is narrower and is exactly what
    ``glue_session_runner.py:213-229`` says: the two ``SAMPLE_UNAVAILABLE_*``
    values are **not** ``not_verified_reason`` values and must not become them.
    p44's own ``source_read_failed`` is forbidden here too, for a reason worth
    keeping apart: it belongs to a sample that was built and failed to read
    (R2.1), which is a different fact from a sample that was never built.

    Scanned recursively over the whole result, so a reason copied into
    ``error_details`` or nested in ``data_quality_results`` fails as well.
    """
    result, _, _ = _run(source_uri, source_schema)

    leaked = sorted(set(_forbidden_values_in(result)))
    assert not leaked, (
        f"the sample-availability value(s) {leaked} reached the result for "
        f"source_data_uri={source_uri!r} — C9: a fallback to synthetic verified something, so "
        f"none of {sorted(FORBIDDEN_REASON_VALUES)} may appear on it"
    )


# ---------------------------------------------------------------------------
# Property 5 — the absent-schema arm stays armed (R2.7)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris(), source_schema=st.sampled_from(({}, None, {"properties": {}}, {"fields": {}})))
def test_the_absent_schema_refusal_stays_armed(source_uri: Any, source_schema: dict | None):
    """R2.7: a fallen-back run gets the refusal a synthetic run gets.

    **Validates: Requirements 2.7**

    This is the case the conditional retirement exists for. Retiring the arm on
    the *executor* rather than on the *frame* would report a full verification
    for a run measured entirely against the pandas builder's ``{id, value}``
    fallback — reintroducing precisely the dishonesty ``LGR-b2f79ef-9`` removed,
    on what is currently the only live path.

    ``create_session`` is asserted unused because the refusal must precede it
    (R7.3): nothing is spent on a run that was never going to prove anything.
    """
    result, runner, client = _run(source_uri, source_schema)

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA, (
        f"a fallen-back sample must keep the absent-schema arm armed (R2.7); "
        f"source_data_uri={source_uri!r} source_schema={source_schema!r}"
    )
    assert result["executed"] is False
    assert result["exit_code"] is None
    client.create_session.assert_not_called()
    client.run_statement.assert_not_called()
    # The refusal dict carries only the schema fact, so without the provenance a
    # reader cannot tell this from a run that never asked for a sample.
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.unavailable_reason == _expected_reason(source_uri)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_unavailable_uris(), source_schema=st.sampled_from((BUILDABLE_SCHEMA, {}, None)))
def test_the_refusal_is_evaluated_with_sample_frame_false(source_uri: Any, source_schema: dict | None):
    """R2.7 at the call, not only at its consequence — existing behaviour, asserted.

    **Validates: Requirements 2.7**

    ``run_guarded_session`` calls ``frame_refusal_results(source_schema,
    sample_frame=frame_source.is_sample)``. On this path ``is_sample`` is
    ``False``, so the argument is ``False`` and every arm of the refusal stands.
    The task's instruction is explicit that this **needs no change** and is to be
    asserted rather than altered; the call site is pinned as well as the outcome
    because ``sample_frame=True`` with a declared schema returns ``None``
    either way, so the outcome alone cannot distinguish the two.

    The keyword is asserted by name, not by position: it is keyword-only, and a
    positional assertion would pass while reading the wrong thing.
    """
    calls: list[dict] = []
    real = glue_session_runner.frame_refusal_results

    def _spy(schema: dict | None, **kwargs: Any) -> dict | None:
        calls.append(kwargs)
        return real(schema, **kwargs)

    client = _client()
    runner = _make_runner(client, source_uri)
    with patch("glue_session_runner.time.sleep"), patch.object(glue_session_runner, "frame_refusal_results", _spy):
        runner.run_test(TRANSFORM, source_schema, TARGET_SCHEMA)

    assert len(calls) == 1, "the refusal is evaluated exactly once per run, before the frame is built"
    assert calls[0]["sample_frame"] is False, (
        f"a fallen-back run must evaluate the refusal with sample_frame=False so the "
        f"absent-schema arm stays armed (R2.7); source_data_uri={source_uri!r}"
    )


# ---------------------------------------------------------------------------
# LGR-819980f-1 at this seam — the URI is unreachable for a read
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source_uri=_scheme_bearing_unavailable_uris())
def test_no_unavailable_uri_reaches_the_submitted_code(source_uri: str):
    """``LGR-819980f-1`` (**standing invariant**), executable at this seam.

    **Validates: Requirements 4.2, 4.4, 4.5**

    A ``jdbc://`` URI must be *unreachable* for a read, not merely unhandled —
    which is a claim about the code that would reach Spark, not about the
    dispatch that chose it. Credentials reach Glue only by Secrets Manager ARN, a
    Verification_Session is granted none (``_create_session`` passes no
    ``Connections``), and do-NOT 3 forbids a JDBC arm in ``build_frame_code``.

    Extended to every unavailable URI rather than only the ``jdbc://`` ones: an
    unreadable ``s3://`` object must not be handed to Spark either, since a read
    of it would fail at the frame-build stage and be reported as test data
    generation — the misattribution R2.1 exists to remove.
    """
    _, _, client = _run(source_uri, BUILDABLE_SCHEMA)

    submitted = _submitted(client)
    assert submitted, "guard: no statement submitted means nothing was checked"
    for index, code in enumerate(submitted):
        assert source_uri not in code, (
            f"statement {index} carries the unavailable source URI {source_uri!r}; a URI with no "
            f"readable sample must reach no emitted read (LGR-819980f-1, do-NOT 3)"
        )
    joined = "\n".join(submitted).lower()
    for forbidden in ("jdbc", "secret", "password", "driver", "dbtable", "connection", "spark.read"):
        assert forbidden not in joined, f"a fallen-back run's statements must not mention {forbidden!r}"


# ---------------------------------------------------------------------------
# Non-vacuity: the antecedent, and every arm
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(source_uri=_unavailable_uris())
def test_the_generator_draws_only_unavailable_uris(source_uri: Any):
    """The antecedent, asserted rather than trusted — and it is do-NOT 1's guard.

    A generator that leaked one readable ``s3://`` URI would take the sample arm,
    where ``is_sample`` retires the absent-schema refusal and the run reaches
    ``CreateSession`` — the path task 5.1 walked into. It bills only against a
    real client, which the patch removes, but a property whose antecedent
    silently stopped holding is worthless either way.
    """
    client = _client()
    runner = _make_runner(client, source_uri)

    frame_source = runner.resolve_frame_source()

    assert frame_source.is_sample is False, f"{source_uri!r} resolves to a READ — it is not an unavailable URI"
    assert glue_session_runner.is_s3_source_uri(source_uri) is False or (
        glue_session_runner.sample_read_format(source_uri) is None
    )


@pytest.mark.parametrize(
    ("strategy", "expected_reason"),
    [
        (_jdbc_uris(), SAMPLE_UNAVAILABLE_NOT_S3_SOURCE),
        (_other_non_s3_uris(), SAMPLE_UNAVAILABLE_NOT_S3_SOURCE),
        (_s3_uris_with_no_extension(), SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT),
        (_s3_uris_with_unrecognised_extension(), SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT),
    ],
    ids=["jdbc", "other-non-s3", "s3-no-extension", "s3-unrecognised-extension"],
)
def test_every_arm_is_reachable_and_records_the_reason_it_names(strategy, expected_reason):
    """Each of the four arms fires, and lands on the reason the requirements give.

    Without this the property could pass while three of four arms were
    unreachable — a generator that stopped firing weakens a property invisibly.
    The two reasons are asserted per arm because they are actionable differently:
    R4.4/R4.5's is "this source is not S3", R4.2's is "this source is S3 but
    nothing says how to read it".
    """
    seen: list[str] = []

    @settings(max_examples=25, deadline=None)
    @given(source_uri=strategy)
    def _check(source_uri: str) -> None:
        client = _client()
        runner = _make_runner(client, source_uri)
        frame_source = runner.resolve_frame_source()
        seen.append(source_uri)
        assert frame_source.unavailable_reason == expected_reason

    _check()

    assert seen, "the arm drew nothing, so it verified nothing"


def test_the_three_stages_still_run_on_a_fallen_back_run():
    """The fallback is a working run, not a degraded one — and still three statements.

    Requirement 9.3's statement count is what ``SHADOW_GLUE_WORST_CASE_SECONDS``
    is derived from (``180 + 3 × 120 + 120``, with
    ``test_shadow_orchestration.py:232`` hardcoding the same ``3 ×``), so a fourth
    statement appearing on this path would silently invalidate the budget. One
    example rather than a property, because the count is not a function of the
    URI.
    """
    _, runner, client = _run("jdbc://pipeline/pipeline-a", BUILDABLE_SCHEMA)

    assert len(_submitted(client)) == _STAGE_COUNT
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
