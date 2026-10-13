# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The frame-mode dispatch, the S3 sample, and the JDBC fallback.

Spec `real-spark-verification` task 5.5. Requirements 9.1, 9.2, 9.3, 9.4, 9.5;
design §4.4's ``sample`` paragraph and C8.

**No real Glue session, no real S3 object, and no JDBC anything** — every call
goes through a MagicMock, and a good half of these tests exist to assert that a
read was *not* attempted.

What is pinned here:

* **the scheme gate (R9.1, R9.3).** ``is_s3_source_uri`` is the only route to a
  source read in the module, and it answers ``False`` for every near-miss.
  ``LGR-819980f-1`` (**standing invariant**) is what that gate protects: a
  ``jdbc://`` URI must be *unreachable*, not merely unhandled. Task 5.6
  generalises the scheme claim as a property; these are the named shapes.
* **the fallback is recorded, and distinguishable (R9.2).** "No sample was
  requested" and "a sample was requested and the source has none" are different
  states on :class:`FrameSource`, which is what the requirement asks for.
* **the conditional retirement (R9.4, R9.5).** The absent-schema refusal is
  retired for a Sample_Frame and stays for a Synthetic_Frame — including for a
  *requested* sample that fell back, which is the case that would otherwise
  claim a verification the synthetic path cannot deliver. Task 5.7 generalises
  this as a property.
* **no JDBC read anywhere.** Asserted over the emitted code as well as over the
  dispatch, because the emitted code is what would actually reach Spark.
* **the sample reader is the generated job's reader.** The four S3-source
  templates read csv with ``inferSchema`` False and json with ``multiLine``
  True (``pyspark_templates/glue_job_s3_tables.py.j2:72-82`` and siblings); a
  frame read differently is a frame the job never produces.

A note for task 9.3, recorded rather than pre-empted
---------------------------------------------------
``source_data_uri`` is a constructor argument (R9.6 — ``run_test``'s parameter
list must not move) and nothing production-side supplies it yet. So on the live
path today ``frame_mode="sample"`` always falls back with
``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE``, which is the *correct* answer for an absent
URI rather than a defect. 9.3 changes what is passed in, not anything asserted
here.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from tests.fixtures.run_test_result_shapes import FRAME_REFUSAL

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
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    FrameBuildError,
    FrameSource,
    GlueSessionTestRunner,
    build_frame_code,
    build_sample_frame_code,
    frame_refusal_results,
    is_s3_source_uri,
    sample_read_format,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

S3_URI = "s3://adi-processed-files-123456789012-us-west-2/pipeline-a/orders.csv"

#: The ``schema_extract`` fallback, verbatim in shape
#: (``schema_extract/lambda_function.py:747``). The whole reason a non-``s3://``
#: URI is an ordinary case rather than an error.
JDBC_URI = "jdbc://pipeline/pipeline-a"

BUILDABLE_SCHEMA = {
    "properties": {
        "customer_id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "double"},
    }
}


def _wide_schema(field_count: int) -> dict:
    return {"properties": {f"col_{index}": {"type": "string"} for index in range(field_count)}}


@pytest.fixture
def glue():
    """A mock Glue client wired for a clean happy path."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.return_value = {"Id": 1}
    client.get_statement.return_value = {
        "Statement": {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}}
    }
    return client


@pytest.fixture
def make_runner(glue):
    """Build a runner over the mock client, with sleeping patched out."""

    def _make(**kwargs) -> GlueSessionTestRunner:
        with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
            return GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, **kwargs)

    return _make


# ---------------------------------------------------------------------------
# The scheme gate (R9.1, R9.3) — the one route to a source read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_uri",
    [
        "s3://bucket/key.csv",
        "s3://bucket/nested/path/key.parquet",
        "s3://bucket/prefix/",
        "s3://bucket",
    ],
    ids=["object", "nested-object", "prefix", "bucket-only"],
)
def test_an_s3_object_uri_is_readable(source_uri):
    """A bucket is required; a key is not.

    ``s3://bucket`` and ``s3://bucket/prefix/`` are real locations, and the
    ``.limit`` in the emitted read is what bounds them. What such a URI usually
    lacks is a recognisable format, which is a separate question with a separate
    answer — see the format tests below.
    """
    assert is_s3_source_uri(source_uri) is True


@pytest.mark.parametrize(
    "source_uri",
    [
        "s3a://bucket/key.csv",
        "s3n://bucket/key.csv",
        "S3://bucket/key.csv",
        "s3:/bucket",
        "s3:bucket/key.csv",
        "s3://",
        "s3:///key.csv",
        "s3://   /key.csv",
        JDBC_URI,
        "jdbc://mysql/sales/orders",
        "https://bucket.s3.amazonaws.com/key.csv",
        "file:///tmp/key.csv",
        "/bucket/key.csv",
        "",
        "   ",
    ],
    ids=[
        "s3a",
        "s3n",
        "uppercase-scheme",
        "no-authority",
        "opaque",
        "no-bucket",
        "empty-bucket",
        "whitespace-bucket",
        "schema-extract-fallback",
        "jdbc-engine-form",
        "https",
        "file",
        "bare-path",
        "empty",
        "whitespace",
    ],
)
def test_every_near_miss_is_refused(source_uri):
    """R9.3: only ``s3://`` reaches a read, so every other shape falls back.

    ``S3://`` and ``s3:/b`` are here because ``urllib.parse.urlsplit`` reports
    scheme ``s3`` for both — it lower-cases the scheme and needs no authority —
    which is why the predicate compares a literal prefix instead of parsing.
    """
    assert is_s3_source_uri(source_uri) is False


@pytest.mark.parametrize("source_uri", [None, 0, b"s3://bucket/key.csv", ["s3://bucket/key.csv"], {}])
def test_a_non_string_uri_is_refused_rather_than_raising(source_uri):
    """``source_data_uri`` is read off a payload, so these are reachable inputs.

    A predicate that raised ``AttributeError`` on them would turn an absent URI
    into an exception on the pre-flight path, where the honest answer is a
    generated frame.
    """
    assert is_s3_source_uri(source_uri) is False


# ---------------------------------------------------------------------------
# Format resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_uri", "expected"),
    [
        ("s3://b/orders.csv", "csv"),
        ("s3://b/orders.CSV", "csv"),
        ("s3://b/orders.tsv", "csv"),
        # p64 R3.1. MOVED here from `test_an_unclassifiable_uri_has_no_format`
        # below, which is the pair to read together: `txt` is in the mirror now,
        # so `None` is no longer its answer. Unlike `xlsx` — which stays
        # unclassifiable because `excel` has no `_SAMPLE_READERS` entry — `txt`
        # resolves to `csv`, a format this module can read.
        ("s3://b/orders.txt", "csv"),
        ("s3://b/orders.json", "json"),
        ("s3://b/orders.jsonl", "json"),
        ("s3://b/orders.ndjson", "json"),
        ("s3://b/orders.parquet", "parquet"),
        ("s3://b/orders.pq", "parquet"),
        ("s3://b/orders.avro", "avro"),
        ("s3://b/orders.orc", "orc"),
        ("s3://b/orders.csv.gz", "csv"),
        ("s3://b/orders.json.bz2", "json"),
        ("s3://b/orders.parquet.snappy", "parquet"),
        ("s3://b/orders.csv.zst", "csv"),
    ],
)
def test_the_extension_map_is_file_validations(source_uri, expected):
    """``FORMAT_MAP``'s content plus ``detect_format``'s compression handling.

    Reproduced rather than imported — ``file_validation`` is a different Lambda
    package and is not on this Lambda's import path at runtime — so the pairing
    is held by test instead of by the language.
    """
    assert sample_read_format(source_uri) == expected


@pytest.mark.parametrize(
    "source_uri",
    [
        "s3://bucket/prefix/",
        "s3://bucket",
        "s3://bucket/orders",
        "s3://bucket/orders.xlsx",
        # `orders.txt` was here until p64 1c.1. R3.1 put `txt` in the mirror, so
        # it now classifies as `csv`; its row moved up to the map test above
        # rather than being deleted.
        "s3://bucket/orders.gz",
        "s3://bucket/my.dir/orders",
    ],
    ids=["prefix", "bucket-only", "no-extension", "xlsx", "compression-only", "dotted-path-no-extension"],
)
def test_an_unclassifiable_uri_has_no_format(source_uri):
    """``None`` means "how to read this is not known", which is a fallback.

    The folder-upload case is the observed instance rather than a hypothetical:
    ``file_validation`` writes the prefix as ``original_s3_uri`` for a
    multi-file upload, and a prefix carries no extension.
    """
    assert sample_read_format(source_uri) is None


# ---------------------------------------------------------------------------
# resolve_frame_source — the three answers (R9.1, R9.2)
# ---------------------------------------------------------------------------


def test_the_default_mode_requests_no_sample(make_runner):
    """Synthetic is the default, and it is not a fallback.

    ``unavailable_reason`` is ``None`` because nothing was unavailable — which is
    the distinction R9.2 turns on.
    """
    frame_source = make_runner().resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.requested_mode == FRAME_MODE_SYNTHETIC
    assert frame_source.sample_requested is False
    assert frame_source.sample_unavailable is False
    assert frame_source.unavailable_reason is None
    assert frame_source.is_sample is False


def test_a_sample_of_an_s3_object_is_selected(make_runner):
    """R9.1: an ``s3://`` object in ``sample`` mode gets a real read."""
    frame_source = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI).resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SAMPLE
    assert frame_source.is_sample is True
    assert frame_source.source_uri == S3_URI
    assert frame_source.read_format == "csv"
    assert frame_source.sample_requested is True
    assert frame_source.sample_unavailable is False


def test_a_jdbc_uri_falls_back_and_records_that_it_did(make_runner):
    """R9.2 + C8: the ordinary case for a JDBC-sourced pipeline.

    Both halves matter. The frame is synthetic, *and* the run carries a reason —
    so "a sample was requested and the source has none" is readable, and
    distinguishable from the default above where nothing was requested.
    """
    frame_source = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI).resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.is_sample is False
    assert frame_source.requested_mode == FRAME_MODE_SAMPLE
    assert frame_source.sample_requested is True
    assert frame_source.sample_unavailable is True
    assert frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    # The offending URI is kept, so a reader can see WHICH source had no sample.
    assert frame_source.source_uri == JDBC_URI


def test_an_absent_uri_falls_back_the_same_way(make_runner):
    """Nothing supplies ``source_data_uri`` until task 9.3.

    So this is today's live behaviour under ``frame_mode="sample"``, and it is
    the correct answer for an absent URI rather than a defect.
    """
    frame_source = make_runner(frame_mode=FRAME_MODE_SAMPLE).resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    assert frame_source.source_uri is None


def test_an_s3_uri_with_no_recognisable_format_falls_back(make_runner):
    """The second fallback arm, and the reason is its own.

    Distinguished from the scheme arm because the two are actionable in
    different ways: one says "this source is not S3", the other says "this
    source is S3 but nothing says how to read it".
    """
    frame_source = make_runner(
        frame_mode=FRAME_MODE_SAMPLE, source_data_uri="s3://bucket/pipeline-a/"
    ).resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT
    assert frame_source.read_format is None


@pytest.mark.parametrize("frame_mode", ["Sample", "SAMPLE", "sampled", "glue", "", "true"])
def test_an_unrecognised_mode_is_synthetic_and_requests_nothing(make_runner, frame_mode):
    """The R2.3 posture one level down: fall through to the existing behaviour.

    ``sample_requested`` is False, so the absent-schema refusal stays armed —
    which is the safe direction for a typo. A mode nobody recognised must not
    retire a refusal.
    """
    frame_source = make_runner(frame_mode=frame_mode, source_data_uri=S3_URI).resolve_frame_source()

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.requested_mode == frame_mode
    assert frame_source.sample_requested is False
    assert frame_source.unavailable_reason is None


def test_resolving_the_frame_source_touches_no_aws(make_runner, glue):
    """The decision is the URI's shape, so it costs nothing to reach.

    Which is why ``run_guarded_session`` can evaluate it before the refusal
    without weakening R7.3.
    """
    for frame_mode, source_data_uri in (
        (FRAME_MODE_SYNTHETIC, None),
        (FRAME_MODE_SAMPLE, S3_URI),
        (FRAME_MODE_SAMPLE, JDBC_URI),
    ):
        make_runner(frame_mode=frame_mode, source_data_uri=source_data_uri).resolve_frame_source()

    glue.create_session.assert_not_called()
    glue.get_session.assert_not_called()
    glue.run_statement.assert_not_called()


# ---------------------------------------------------------------------------
# FrameSource cannot be constructed dishonestly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_uri": None, "read_format": None},
        {"source_uri": S3_URI, "read_format": None},
        {"source_uri": None, "read_format": "csv"},
        {"source_uri": "", "read_format": "csv"},
    ],
    ids=["neither", "no-format", "no-uri", "empty-uri"],
)
def test_a_sample_without_a_readable_source_is_refused(kwargs):
    """The one combination that could produce the dishonesty this task prevents.

    ``is_sample`` true retires the absent-schema refusal (R9.4); a sample with
    nothing to read would then fall through to a generated frame with the
    refusal disarmed. Unreachable through ``resolve_frame_source``; made
    unreachable through any other constructor too.
    """
    with pytest.raises(FrameBuildError, match="needs both a source_uri and a read_format"):
        FrameSource(mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, **kwargs)


def test_a_built_sample_cannot_also_claim_something_was_unavailable():
    """The two states are exclusive: a sample was built, or it was not."""
    with pytest.raises(FrameBuildError, match="nothing was unavailable"):
        FrameSource(
            mode=FRAME_MODE_SAMPLE,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=S3_URI,
            read_format="csv",
            unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
        )


# ---------------------------------------------------------------------------
# The emitted sample read
# ---------------------------------------------------------------------------


def test_the_sample_read_binds_the_frame_variable_and_is_bounded():
    """R9.1: a bounded read, bound to the name the transform reads."""
    code = build_sample_frame_code(S3_URI, "csv")

    assert code.startswith("#"), "the emitted statement leads with the comment explaining the reader"
    assert f"\n{glue_session_runner.FRAME_VARIABLE} = (" in code
    assert f".limit({glue_session_runner._SAMPLE_FRAME_ROWS})" in code
    compile(code, "<sample-frame>", "exec")


def test_the_sample_row_bound_equals_the_synthetic_row_count():
    """``input_records`` is a shadow-compared dimension.

    Two frames of different heights would differ on it for a reason that has
    nothing to do with either executor.
    """
    assert glue_session_runner._SAMPLE_FRAME_ROWS == glue_session_runner._SYNTHETIC_FRAME_ROWS


@pytest.mark.parametrize(
    ("read_format", "expected_reader"),
    [
        # p55 R4.1: the templates' csv branch passes the source's own
        # Source_Delimiter, so the sample read does too. `S3_URI` ends `.csv`, so
        # the expected delimiter here is the comma `delimiter_for_key` returns
        # for it — a `.tsv` URI would emit `"\t"` and is covered by
        # `test_the_csv_sample_read_uses_the_sources_own_delimiter` below.
        ("csv", 'spark.read.option("header", True).option("inferSchema", False).option("sep", ",").csv('),
        ("json", 'spark.read.option("multiLine", True).json('),
        ("parquet", "spark.read.parquet("),
        ("avro", 'spark.read.format("avro").load('),
        ("orc", "spark.read.orc("),
    ],
)
def test_each_reader_is_the_generated_jobs_reader(read_format, expected_reader):
    """The four S3-source templates' ``source_format`` branch, verbatim.

    ``pyspark_templates/glue_job_s3_tables.py.j2:72-82`` and its three siblings.
    A frame read with different options is a frame the job never produces, so
    verifying against it would verify the wrong thing — including
    ``inferSchema`` False, which means every csv column arrives as a string in
    the real job too.
    """
    code = build_sample_frame_code(S3_URI, read_format)

    assert expected_reader in code
    compile(code, "<sample-frame>", "exec")


@pytest.mark.parametrize(
    ("source_uri", "expected_sep"),
    [
        ("s3://bucket/pipeline-a/orders.csv", '.option("sep", ",")'),
        ("s3://bucket/pipeline-a/orders.tsv", '.option("sep", "\\t")'),
        ("s3://bucket/pipeline-a/orders.tsv.gz", '.option("sep", "\\t")'),
        # No extension — the observed multi-file prefix shape. Total lookup,
        # comma default (p55 R4.6), which is what this read did before p55.
        ("s3://bucket/pipeline-a/", '.option("sep", ",")'),
    ],
)
def test_the_csv_sample_read_uses_the_sources_own_delimiter(source_uri, expected_sep):
    """p55 R4.1 — the delimiter comes from the source key, not from a constant.

    A tab must reach the emitted code as the two characters ``\\t`` inside a
    string literal, never as a literal tab, which is why the reader is filled
    through ``json.dumps`` on this side and ``| tojson`` in the templates. The
    ``compile`` is the check that it landed as a literal: a raw tab inside the
    generated ``.option("sep", …)`` would still parse, but the round trip through
    ``json.dumps`` is what keeps a future delimiter with a quote or a backslash
    from emitting code that reads a differently separated frame.
    """
    code = build_sample_frame_code(source_uri, "csv")

    assert expected_sep in code, f"{source_uri!r} produced {code!r}"
    compile(code, "<sample-frame>", "exec")


def test_the_uri_is_emitted_as_a_python_literal():
    """A key containing a quote or a backslash must not emit different code.

    "Different code" is the worse failure of the two: it would silently verify
    against another object rather than failing to parse.
    """
    awkward = 's3://bucket/pipe"line\\a/orders.csv'
    code = build_sample_frame_code(awkward, "csv")

    compile(code, "<sample-frame>", "exec")
    assert '"s3://bucket/pipe\\"line\\\\a/orders.csv"' in code


def test_an_unknown_read_format_is_refused_rather_than_guessed():
    """``spark_type_for``'s posture: the default cannot be mistaken for a verification."""
    with pytest.raises(FrameBuildError, match="no Spark reader is defined"):
        build_sample_frame_code(S3_URI, "xlsx")


@pytest.mark.parametrize("read_format", ["csv", "json", "parquet", "avro", "orc"])
def test_no_emitted_read_mentions_jdbc_or_a_credential(read_format):
    """R9.3 over the code that would actually reach Spark.

    ``LGR-819980f-1`` (**standing invariant**): credentials reach Glue only by
    Secrets Manager ARN, because plaintext leaked a real credential once. A
    verification session is granted none, so the emitted read must name no
    connection, no driver, no url and no secret.
    """
    code = build_sample_frame_code(S3_URI, read_format).lower()

    for forbidden in ("jdbc", "secret", "password", '"user"', "'user'", "driver", "dbtable", "connection"):
        assert forbidden not in code, f"the emitted sample read must not mention {forbidden!r}"


# ---------------------------------------------------------------------------
# build_frame_code — the dispatch
# ---------------------------------------------------------------------------


def test_a_sample_source_dispatches_to_the_sample_read():
    source = FrameSource(mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=S3_URI, read_format="csv")

    assert build_frame_code(source, BUILDABLE_SCHEMA) == build_sample_frame_code(S3_URI, "csv")


def test_a_sample_source_ignores_the_declared_schema():
    """The sample's columns come from the object, so the schema is not consulted.

    Which is exactly why the absent-schema refusal has nothing left to be about
    on this arm (R9.4).
    """
    source = FrameSource(mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=S3_URI, read_format="csv")

    assert build_frame_code(source, None) == build_frame_code(source, BUILDABLE_SCHEMA)


@pytest.mark.parametrize(
    "source",
    [
        FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC),
        FrameSource(
            mode=FRAME_MODE_SYNTHETIC,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=JDBC_URI,
            unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
        ),
    ],
    ids=["not-requested", "requested-and-unavailable"],
)
def test_every_non_sample_source_dispatches_to_the_synthetic_frame(source):
    """R9.2: a requested sample that fell back gets the generated frame.

    The dispatch reads ``is_sample`` and nothing else — not the requested mode,
    and not whether a URI happens to be present.
    """
    assert build_frame_code(source, BUILDABLE_SCHEMA) == glue_session_runner.build_synthetic_frame_code(
        BUILDABLE_SCHEMA
    )


# ---------------------------------------------------------------------------
# The conditional retirement (R9.4, R9.5)
# ---------------------------------------------------------------------------


def test_the_absent_schema_refusal_is_retired_for_a_sample_frame():
    """R9.4: the sample supplies the columns a declared schema would have.

    ``LGR-b2f79ef-9`` refused to score a run built on the ``{id, value}``
    fallback because that frame "proves nothing about the pipeline's actual
    input". A sample frame removes the premise: its columns ARE the pipeline's
    input.
    """
    for source_schema in (None, {}, {"properties": {}}, {"fields": {}}):
        assert frame_refusal_results(source_schema, sample_frame=True) is None


@pytest.mark.parametrize("source_schema", [None, {}, {"properties": {}}, {"fields": {}}])
def test_the_absent_schema_refusal_stays_for_a_synthetic_frame(source_schema):
    """R9.5, and the default: keeping the refusal must not need asking for."""
    for kwargs in ({}, {"sample_frame": False}):
        results = frame_refusal_results(source_schema, **kwargs)

        assert results is not None
        assert results["not_verified_by_harness"] is True
        assert results["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA


def test_the_width_refusal_is_not_retired_by_a_sample_frame():
    """Only the no-source-schema arm goes.

    ``_MAX_TEST_FRAME_COLUMNS`` is a DynamoDB item-size bound on the state row
    (R10.4, design C5), and a sample frame writes the same row through the same
    parser — so the arithmetic is unchanged on both sides. ``sample_frame``
    gates the reason, not the refusal.
    """
    wide = _wide_schema(_MAX_TEST_FRAME_COLUMNS + 1)
    results = frame_refusal_results(wide, sample_frame=True)

    assert results is not None
    assert results["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED
    assert set(results) == set(FRAME_REFUSAL.types)


def test_a_declared_schema_is_admitted_in_either_mode():
    """The retirement changes only the arm; it does not change the admissions."""
    assert frame_refusal_results(BUILDABLE_SCHEMA) is None
    assert frame_refusal_results(BUILDABLE_SCHEMA, sample_frame=True) is None


# ---------------------------------------------------------------------------
# The whole thing, through run_guarded_session
# ---------------------------------------------------------------------------


def test_a_sample_run_with_no_schema_reaches_the_session(make_runner, glue):
    """The behaviour R9.4 exists for, end to end.

    An empty schema plus a real ``s3://`` source is no longer a refusal: the
    session runs, and the frame statement is the sample read.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI)
    run = runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert not run.refused
    glue.create_session.assert_called_once()
    submitted = [call.kwargs["Code"] for call in glue.run_statement.call_args_list]
    # The sample read followed by task 6.1's `GENERATED:` marker line — the prefix
    # is what pins that the frame reaching Spark is the sample and not a fallback.
    assert submitted[0].startswith(build_sample_frame_code(S3_URI, "csv"))
    assert submitted[0] == glue_session_runner.frame_build_statement(runner.resolve_frame_source(), {})
    assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]
    assert run.frame_source is not None and run.frame_source.is_sample


def test_a_sample_run_of_a_jdbc_source_with_no_schema_is_still_refused(make_runner, glue):
    """R9.5 in the case that matters most, and the reason the retirement is conditional.

    A JDBC-sourced pipeline requested a sample and cannot have one, so its frame
    is generated — and a generated frame with no declared schema proves nothing.
    Retiring the refusal on the *executor* rather than on the *frame* would
    report a full verification here, on what is currently the only live path.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI)
    run = runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refused
    assert run.refusal is not None
    assert run.refusal["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
    glue.create_session.assert_not_called()
    glue.run_statement.assert_not_called()
    # The refusal arm carries the frame decision too, so a reader can tell this
    # apart from a run that never asked for a sample.
    assert run.frame_source is not None
    assert run.frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE


def test_a_sample_run_of_a_jdbc_source_with_a_schema_runs_synthetic(make_runner, glue):
    """The fallback is a working run, not a degraded one.

    With a declared schema there is nothing to refuse: the frame is synthetic and
    the session runs. What differs from a synthetic-mode run is only the recorded
    reason.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI)
    run = runner.run_guarded_session(BUILDABLE_SCHEMA, [(STAGE_TRANSFORM, "df_t = df")])

    assert not run.refused
    submitted = glue.run_statement.call_args_list[0].kwargs["Code"]
    # The synthetic frame plus task 6.1's `GENERATED:` marker line.
    assert submitted.startswith(glue_session_runner.build_synthetic_frame_code(BUILDABLE_SCHEMA))
    assert submitted == glue_session_runner.frame_build_statement(runner.resolve_frame_source(), BUILDABLE_SCHEMA)
    assert run.frame_source is not None
    assert run.frame_source.sample_requested is True
    assert run.frame_source.sample_unavailable is True


def test_a_synthetic_run_with_no_schema_is_refused_as_before(make_runner, glue):
    """Task 5.3's behaviour is unchanged where no sample was requested (R9.5)."""
    run = make_runner().run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refused
    assert run.refusal == frame_refusal_results({})
    glue.create_session.assert_not_called()
    assert run.frame_source is not None
    assert run.frame_source.sample_requested is False
    assert run.frame_source.unavailable_reason is None


def test_a_too_wide_schema_is_refused_in_sample_mode_too(make_runner, glue):
    """The width arm survives the retirement, through the guard as well."""
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI)
    run = runner.run_guarded_session(_wide_schema(_MAX_TEST_FRAME_COLUMNS + 1))

    assert run.refused
    assert run.refusal is not None
    assert run.refusal["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED
    glue.create_session.assert_not_called()


def test_no_statement_a_guarded_run_submits_mentions_jdbc(make_runner, glue):
    """R9.3 over every path through the guard, not only the sample builder."""
    for frame_mode, source_data_uri, source_schema in (
        (FRAME_MODE_SYNTHETIC, None, BUILDABLE_SCHEMA),
        (FRAME_MODE_SAMPLE, S3_URI, {}),
        (FRAME_MODE_SAMPLE, S3_URI, BUILDABLE_SCHEMA),
        (FRAME_MODE_SAMPLE, JDBC_URI, BUILDABLE_SCHEMA),
    ):
        runner = make_runner(frame_mode=frame_mode, source_data_uri=source_data_uri)
        runner.run_guarded_session(source_schema, [(STAGE_TRANSFORM, "df_t = df")])

    submitted = "\n".join(call.kwargs["Code"] for call in glue.run_statement.call_args_list).lower()
    assert submitted, "the loop above must actually have submitted something"
    for forbidden in ("jdbc", "secret", "password", "driver", "dbtable"):
        assert forbidden not in submitted
