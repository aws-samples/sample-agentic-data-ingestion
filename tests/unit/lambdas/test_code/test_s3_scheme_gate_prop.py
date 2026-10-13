# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 10 — a source is read only when its scheme is S3.

Spec ``real-spark-verification`` task 5.6. **Validates: Requirements 9.1, 9.2,
9.3.** Design §4.4's ``sample`` paragraph, and C8.

    9.1 WHERE Sample_Frame construction is selected AND the Source_Data_Uri
        scheme is ``s3``, THE Glue_Session_Runner SHALL build the Test_Frame from
        a bounded sample of that object.
    9.2 IF Sample_Frame construction is selected AND the Source_Data_Uri scheme
        is not ``s3``, THEN THE Glue_Session_Runner SHALL build a Synthetic_Frame
        and record that a requested sample was unavailable.
    9.3 THE Glue_Session_Runner SHALL read no source over a JDBC connection.

**No real Glue session, no real S3 object, no JDBC anything, nothing deployed.**
Every call goes through a ``MagicMock``, and most of what follows exists to
assert that a read was *not* attempted. No production code is modified by this
task.

Why this is not stated as a biconditional
-----------------------------------------

Design §5's Property 10 reads "attempts a read **if and only if** the URI's
scheme is ``s3``". The **only if** direction — the safety-critical one — is
exactly true and is what :func:`test_no_non_s3_uri_can_reach_a_source_read` pins.
The **if** direction is narrower than the design sentence, and deliberately so:
an ``s3://`` URI whose *format* is unrecognised also falls back, with the second
reason :data:`SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT`. That is not a defect and
not a shortfall to be fixed here — ``sample_read_format``'s own docstring
(``glue_session_runner.py:1747-1768``) records the narrowing and its observed
instance: ``file_validation`` writes the *prefix* as ``original_s3_uri`` for a
multi-file upload (``file_validation/lambda_function.py:368``) and a prefix
carries no extension, so "how to read this" is genuinely unknown and guessing a
reader would build a frame of ``_corrupt_record`` rows and report the transform's
failure against it as test-data generation.

So the claim is split into the two halves that are each *exactly* true, rather
than asserted as a biconditional that is false:

* the **scheme** claim, over :func:`is_s3_source_uri` — the standalone predicate
  written so that this property has a target that is only about the scheme;
* the **dispatch** claim, over
  :meth:`GlueSessionTestRunner.resolve_frame_source` — keyed on
  ``is_s3 and a recognised format``, with the fallback reason naming *which* of
  the two conditions failed.

Asserting the false biconditional would have made this file fail on the folder-
prefix URI that is currently the second most common real shape.

Three levels, because the dispatch is not what reaches Spark
------------------------------------------------------------

R9.3 is a claim about what the verification session *does*, so it is pinned at
three levels rather than one:

1. the predicate (:func:`is_s3_source_uri`);
2. the decision (:meth:`GlueSessionTestRunner.resolve_frame_source`, plus "no
   Glue API was touched reaching it");
3. **the emitted code** — the statements ``run_guarded_session`` actually
   submits. This is the level that matters: the dispatch could be right and the
   emitted statement could still name a connection. Every generated URI carries
   a marker token, so "the non-``s3://`` URI's text never reached Spark" is
   assertable as a biconditional over the submitted code
   (:func:`test_only_a_sample_read_names_the_source_object`) rather than as an
   absence that might hold by coincidence.

Ledger notes
------------

* ``LGR-819980f-1`` (**invariant**, template / ratified) is the governing record
  and is why level 3 exists: JDBC credentials are only ever referenced by Secrets
  Manager ARN, and plaintext user/password never appear in Glue job arguments or
  ``ConnectionProperties``, "because this leaked a target DB credential once
  already". Its scope is ``agentic_data_ingestion/lambda/**``, which is where the
  module under test lives. A verification session is granted none of that
  machinery, so the honest reading is that the emitted statement must name no
  connection, driver, dbtable, secret or password at all — which is what
  :func:`test_no_statement_any_path_submits_reaches_a_jdbc_source` asserts over
  every path, not only the sample builder.
* ``LGR-819980f-2`` (template / ratified) adds that ``SECRET_ID`` in
  ``ConnectionProperties`` is load-bearing and the ``adi:secret_arn`` tag is a
  read-path mirror only, and that removing either copy broke all JDBC
  provisioning once (``LGR-33eed95-1``, now SUPERSEDED). Nothing here touches
  that machinery; it is cited because it is the reason a *test* must not be the
  thing that widens the credential surface.
* ``LGR-b2f79ef-9`` (template / ratified) leaves ``_build_test_data_code``'s
  ``{id, value}`` fallback in place "but never scored", because a frame that does
  not represent the pipeline's input makes any ``schema_compliance`` figure over
  it "a harness artifact". That is the premise R9.4 removes for a *sample* frame
  and keeps for a fallback — so the fallback recorded here has to stay
  distinguishable from a success, which is why every fallback assertion below
  checks ``unavailable_reason`` and not merely ``mode``.
* ``LGR-3e45e65-1`` (template / ratified) pins ``AWS_REGION`` /
  ``AWS_DEFAULT_REGION`` by assignment in ``tests/unit/conftest.py``, so the
  ``setdefault`` calls below are consistent no-ops kept for the module-run case.

Ledger gaps, stated as gaps rather than as all-clears
-----------------------------------------------------

* ``why`` on ``lambda/agentic_data_ingestion_test_code/glue_session_runner.py``
  returns **nothing** — the file is not committed yet, so it has no history to
  govern it.
* ``decisions(concept="security", include_backfilled=True)`` and
  ``decisions(concept="networking", include_backfilled=True)`` both return
  **empty**, despite both being in the documented vocabulary and despite this
  property being squarely about a security boundary. That is a hole in the
  ledger, not evidence the area is undecided — the governing constraint is
  ``LGR-819980f-1`` above, reached through ``concept="credentials"``.
* ``decisions(scope=".../agentic_data_ingestion_test_code/**")`` returns ten
  records; the two that bear on frame construction are ``LGR-b2f79ef-9`` (above)
  and ``LGR-b2f79ef-12`` (the 512-column ceiling, task 5.4's property, not this
  one's). The rest are BROAD snapshot commits at 51-64 files and name nothing
  here.

Blast radius and coverage, from codegraph
-----------------------------------------

``is_s3_source_uri`` 5 callers, ``sample_read_format`` 4, ``build_frame_code`` 5,
``build_sample_frame_code`` 9, ``resolve_frame_source`` 8 — all within
``glue_session_runner.py``; nothing outside the module calls any of them yet,
because executor selection is task 9.1. All five report covering tests in
``test_sample_frame_dispatch.py``, so none is a zero-coverage change area and no
characterization pass is owed. This file adds a second, generalising layer over
that one rather than replacing it.

Sibling coverage
----------------

* ``test_sample_frame_dispatch.py`` — task 5.5's worked examples: 15 named
  near-misses, 5 non-string inputs, the format table, and the ``FrameSource``
  construction refusals. This file generalises its scheme and dispatch claims.
* ``test_frame_refusal_precedes_session_prop.py`` — Property 8 (task 5.4), which
  pins ``frame_mode`` to synthetic precisely so that the conditional retirement
  landing in 5.5 could not falsify it.
* Property 11 (task 5.7) owns the **absent-schema** half of R9.4/R9.5. This file
  passes a buildable schema almost everywhere for that reason: it is about which
  source is read, not about what an absent schema costs.
"""

import contextlib
import os
import sys
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    GlueSessionTestRunner,
    build_sample_frame_code,
    is_s3_source_uri,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

TRANSFORM_STATEMENT = (STAGE_TRANSFORM, "df_t = df")

#: A token every generated URI with room for one carries, in its **bucket**
#: position. Nothing the frame builders emit contains it — the synthetic builder
#: emits only ``col_N`` names and its own commentary
#: (``glue_session_runner.py:1559-1580``) — so "this URI's text did not reach
#: Spark" becomes an assertion about the emitted statement rather than a
#: coincidence. It is deliberately not a plausible column name.
SOURCE_MARKER = "adi-p5x6-source-marker"

#: Tokens that must appear in no statement any path submits (R9.3,
#: ``LGR-819980f-1``). ``user`` is quoted because ``inferSchema`` and the
#: synthetic builder's prose both contain the bare substring in innocent words.
FORBIDDEN_IN_EMITTED_CODE = (
    "jdbc",
    "secret",
    "password",
    "driver",
    "dbtable",
    "connection",
    '"user"',
    "'user'",
)

#: A schema the synthetic builder always accepts, so that a run reaches a
#: session on the fallback arm too and "what was submitted" is never empty for
#: the trivial reason that nothing was submitted. Property 11 (task 5.7) owns the
#: absent-schema case; keeping it out of here is what stops this file from
#: restating 5.7's claim badly.
BUILDABLE_SCHEMA = {
    "properties": {
        "col_0": {"type": "integer"},
        "col_1": {"type": "string"},
        "col_2": {"type": "double"},
    }
}


class UriCandidate(NamedTuple):
    """A generated URI together with what the *requirement* says about it.

    ``is_s3`` and ``read_format`` are labels attached at **construction**, from
    the parts the URI was assembled out of — never by calling the production
    predicate or the production format resolver. That is what makes the
    properties below a check rather than a comparison of the module with itself.

    ``read_format`` is the format the requirement implies for the URI, or
    ``None`` when nothing in it says how to read it. It is only *consulted* where
    ``is_s3`` is true; on a non-``s3://`` URI it is ignored, since the scheme has
    already settled the answer.
    """

    uri: Any
    is_s3: bool
    read_format: str | None
    #: What the shape is, for a readable failure message.
    label: str


# ── Generators ────────────────────────────────────────────────────────
#
# Three arms: composed parts, hand-labelled literals, and non-strings. The
# composed arm is where the volume is; the literal arm carries the shapes whose
# label is easier to read off design §4.4's table than to derive; the non-string
# arm exists because `source_data_uri` is read off a payload
# (`lambda_function.py:1464`) and a bad type is a reachable input rather than a
# programming error.

#: Scheme prefixes and whether each is the one design §4.4 spells. Only
#: ``s3://`` — slashes included — can yield a readable source; every other entry
#: is a near-miss that must fall back.
#:
#: The non-``s3://`` schemes are all combined with a **non-empty, non-slash**
#: bucket below, which is what keeps the label honest: ``"s3:/"`` plus an empty
#: bucket plus a rooted key would re-assemble into a genuine ``s3://`` URI, and a
#: generator that produced it while labelling it not-S3 would report a false
#: failure. The composition below cannot reach that.
_SCHEMES: list[tuple[str, bool]] = [
    ("s3://", True),
    ("s3a://", False),  # a Hadoop scheme; nothing here writes it
    ("s3n://", False),
    ("S3://", False),  # case is not normalised — urlsplit would report `s3`
    ("S3A://", False),
    ("s3:/", False),  # no authority — urlsplit would report `s3` here too
    ("s3:", False),  # opaque
    ("jdbc://", False),  # the `schema_extract` fallback shape (C8)
    ("https://", False),
    ("http://", False),
    ("file://", False),
    ("gs://", False),
    ("", False),  # a bare path
]

#: Buckets that are real, and all carry :data:`SOURCE_MARKER`. Dot-free on
#: purpose: with an empty key the *bucket* is the basename, so a dotted bucket
#: would move where the format label comes from.
_REAL_BUCKETS = [SOURCE_MARKER, f"{SOURCE_MARKER}-2", f"a-{SOURCE_MARKER}-b"]

#: Buckets that are not buckets. Combined only with ``"s3://"``, where they
#: produce ``s3://``, ``s3:///key`` and ``s3://   /key`` — the three no-bucket
#: near-misses. ``strip()`` is why the whitespace one is refused: an S3 bucket
#: name cannot contain a space, so nothing already valid is rejected by it.
_ABSENT_BUCKETS = ["", " ", "   ", "\t"]

#: ``(key, expected_format)``. The extension map is ``file_validation``'s
#: ``FORMAT_MAP`` content plus ``detect_format``'s compression handling —
#: reproduced here as expectations rather than imported, for the same reason the
#: production module reproduces it: ``file_validation`` is a different Lambda
#: package and is not on this Lambda's import path at runtime.
_KEYS: list[tuple[str, str | None]] = [
    ("", None),  # bucket only — the basename is the (dot-free) bucket
    ("/", None),  # bucket root
    ("/prefix/", None),  # the multi-file-upload shape: a prefix has no extension
    ("/pipeline-a/orders", None),  # no extension at all
    ("/pipeline-a/orders.csv", "csv"),
    ("/pipeline-a/orders.CSV", "csv"),  # extension case IS normalised
    ("/pipeline-a/orders.tsv", "csv"),
    ("/pipeline-a/orders.txt", "csv"),  # p64 R3.1 — pipe-delimited, `csv` family
    ("/pipeline-a/orders.json", "json"),
    ("/pipeline-a/orders.jsonl", "json"),
    ("/pipeline-a/orders.ndjson", "json"),
    ("/pipeline-a/orders.parquet", "parquet"),
    ("/pipeline-a/orders.pq", "parquet"),
    ("/pipeline-a/orders.avro", "avro"),
    ("/pipeline-a/orders.orc", "orc"),
    ("/pipeline-a/orders.csv.gz", "csv"),  # look through the compression suffix
    ("/pipeline-a/orders.json.bz2", "json"),
    ("/pipeline-a/orders.parquet.snappy", "parquet"),
    ("/pipeline-a/orders.csv.zst", "csv"),
    ("/pipeline-a/orders.gz", None),  # compression with nothing under it
    # `xlsx` IS in FORMAT_MAP — the old comment here said "outside FORMAT_MAP",
    # which is the confusion that also made the `orders.txt` row below wrong.
    # What makes it unreadable is the absence of an `_SAMPLE_READERS` entry for
    # `excel`. `txt` moved up to the readable rows in p64 1c.1 (R3.1): it entered
    # FORMAT_MAP as a `csv`-family member, and `csv` has a reader.
    ("/pipeline-a/orders.xlsx", None),  # in FORMAT_MAP, no _SAMPLE_READERS entry
    ("/pipeline-a/my.dir/orders", None),  # the dot is in a directory, not the name
    ("/pipeline-a/orders.parquet/part-0000", None),  # a written directory
]


@st.composite
def _composed_uris(draw) -> UriCandidate:
    """Assemble ``scheme + bucket + key`` and label it from its parts.

    The label derivation is design §4.4's condition, in its own terms: a source
    is readable when the URI carries the literal ``s3://`` prefix **and** names a
    bucket. Writing it from the requirement rather than from
    :func:`is_s3_source_uri`'s body is what makes the scheme property falsifiable
    — a transcription of the implementation would agree with it under any
    rewrite.
    """
    scheme, scheme_is_s3 = draw(st.sampled_from(_SCHEMES))
    key, read_format = draw(st.sampled_from(_KEYS))

    if scheme_is_s3:
        # Only the `s3://` arm may draw an absent bucket: that is where
        # `s3://`, `s3:///key` and `s3://   /key` come from, and it is the one
        # arm where "no bucket" is the reason for the answer rather than the
        # scheme being wrong.
        bucket = draw(st.sampled_from(_REAL_BUCKETS + _ABSENT_BUCKETS))
    else:
        bucket = draw(st.sampled_from(_REAL_BUCKETS))

    return UriCandidate(
        uri=f"{scheme}{bucket}{key}",
        is_s3=scheme_is_s3 and bool(bucket.strip()),
        read_format=read_format,
        label=f"scheme={scheme!r} bucket={bucket!r} key={key!r}",
    )


#: Shapes whose label is clearer written out than derived, including every
#: near-miss task 5.6 names by hand. Read off the table in
#: :func:`is_s3_source_uri`'s docstring, which is design §4.4's condition spelled
#: case by case.
_LITERAL_URIS: list[UriCandidate] = [
    UriCandidate("", False, None, "empty string"),
    UriCandidate("   ", False, None, "whitespace only"),
    UriCandidate("s3:/x", False, None, "task 5.6: no authority"),
    UriCandidate("s3://", False, None, "scheme only"),
    UriCandidate("s3:///key.csv", False, "csv", "task 5.6-adjacent: empty bucket"),
    UriCandidate("s3://   /key.csv", False, "csv", "whitespace bucket"),
    UriCandidate(f"s3a://{SOURCE_MARKER}/key.csv", False, "csv", "task 5.6: s3a"),
    UriCandidate(f"s3n://{SOURCE_MARKER}/key.csv", False, "csv", "s3n"),
    UriCandidate(f"S3://{SOURCE_MARKER}/key.csv", False, "csv", "task 5.6: uppercase scheme"),
    UriCandidate(f"s3:{SOURCE_MARKER}/key.csv", False, "csv", "opaque"),
    UriCandidate("jdbc://pipeline/x", False, None, "task 5.6: the schema_extract fallback"),
    UriCandidate(f"jdbc://mysql/{SOURCE_MARKER}/orders", False, None, "a JDBC engine form"),
    UriCandidate(f"jdbc:postgresql://{SOURCE_MARKER}:5432/sales", False, None, "a real JDBC URL"),
    UriCandidate(f"https://{SOURCE_MARKER}.s3.amazonaws.com/key.csv", False, "csv", "the virtual-hosted form"),
    UriCandidate(f"file:///tmp/{SOURCE_MARKER}/key.csv", False, "csv", "a local file"),
    UriCandidate(f"/{SOURCE_MARKER}/key.csv", False, "csv", "a bare absolute path"),
    UriCandidate(f"{SOURCE_MARKER}/key.csv", False, "csv", "a bare relative path"),
    # `s3://` with a dotted bucket and no key: the basename is the bucket, whose
    # extension (`data`) is outside FORMAT_MAP, so the source is S3 and the
    # format is unknown — the second fallback arm, reached without a key.
    UriCandidate("s3://my.data", True, None, "a dotted bucket, no key"),
    UriCandidate(f"s3://{SOURCE_MARKER}", True, None, "bucket only, readable but formatless"),
    UriCandidate(f"s3://{SOURCE_MARKER}/prefix/", True, None, "the multi-file-upload prefix"),
    UriCandidate(f"s3://{SOURCE_MARKER}/pipeline-a/orders.csv", True, "csv", "the ordinary readable case"),
    UriCandidate(f"s3://{SOURCE_MARKER}/pipeline-a/orders.parquet", True, "parquet", "readable parquet"),
    # A bucket or key carrying a quote or a backslash must not emit code that
    # parses as a different object.
    UriCandidate(f's3://{SOURCE_MARKER}/pipe"line\\a/orders.json', True, "json", "an awkward key"),
]

#: Non-strings. ``source_data_uri`` is read off a payload, so these are reachable
#: inputs; a predicate that raised on them would turn an ordinary absent URI into
#: an exception on the pre-flight path, where a generated frame is the honest
#: answer.
_NON_STRING_URIS: list[UriCandidate] = [
    UriCandidate(None, False, None, "None — today's live value until task 9.3"),
    UriCandidate(0, False, None, "int"),
    UriCandidate(3.5, False, None, "float"),
    UriCandidate(True, False, None, "bool"),
    UriCandidate(b"s3://bucket/key.csv", False, None, "bytes"),
    UriCandidate(["s3://bucket/key.csv"], False, None, "list"),
    UriCandidate({}, False, None, "dict"),
    UriCandidate({"uri": "s3://bucket/key.csv"}, False, None, "a dict that looks like a payload"),
]


def _uri_candidates() -> st.SearchStrategy[UriCandidate]:
    """Every arm, weighted so the composed arm carries the volume."""
    return st.one_of(
        _composed_uris(),
        st.sampled_from(_LITERAL_URIS),
        st.sampled_from(_NON_STRING_URIS),
    )


#: The composed arm is 13 × 23 × up-to-7 shapes and every example that reaches a
#: session emits a ~100-row frame, so examples are not free. 300 covers the
#: composed space many times over while keeping the module inside a few seconds.
_SETTINGS = settings(max_examples=300, deadline=None)


# ── The mock Glue client ──────────────────────────────────────────────


def _happy_glue_client() -> MagicMock:
    """A Glue client wired so that a session, if reached, succeeds.

    Wired for the happy path deliberately: if ``create_session`` returned a bare
    ``MagicMock``, "the sample read was submitted" could fail for reasons that
    have nothing to do with the scheme, and "no read happened" could be satisfied
    by a client too broken to do anything at all.
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
def _mock_runner(*, frame_mode: str, source_data_uri: Any):
    """Yield ``(runner, glue)`` over a fresh mock client, sleeping patched out.

    A context manager entered inside each ``@given`` body rather than a fixture:
    a function-scoped fixture is applied once per test *function* and is never
    reset between Hypothesis examples, so a mock built there would accumulate
    calls across examples and every "what was submitted" assertion would be
    reading another example's statements. The same reasoning
    ``test_frame_refusal_precedes_session_prop.py`` records for its own helper.
    """
    glue = _happy_glue_client()
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield (
            GlueSessionTestRunner(
                REGION,
                role_arn=ROLE_ARN,
                frame_mode=frame_mode,
                source_data_uri=source_data_uri,
            ),
            glue,
        )


def _submitted_code(glue: MagicMock) -> list[str]:
    """Every statement body handed to ``RunStatement``, in submission order."""
    return [call.kwargs["Code"] for call in glue.run_statement.call_args_list]


def _expected_unavailable_reason(candidate: UriCandidate) -> str | None:
    """Which of R9.2's two records a requested sample earns, in the requirement's terms.

    ``None`` where the sample is readable. Otherwise the reason names *which*
    condition failed, and the two are actionable in different ways: one says
    "this source is not S3" — the ordinary case for a JDBC-sourced pipeline — and
    the other says "this source is S3 but nothing says how to read it", which is
    the multi-file-upload prefix.
    """
    if not candidate.is_s3:
        return SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    if candidate.read_format is None:
        return SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT
    return None


# ── Level 1: the scheme predicate ─────────────────────────────────────


@given(candidate=_uri_candidates())
@_SETTINGS
def test_the_scheme_gate_answers_yes_exactly_for_an_s3_object_uri(candidate):
    """Property 10, the scheme half. **Validates: Requirements 9.1, 9.3**

    For any generated URI, :func:`is_s3_source_uri` is true **iff** the URI
    carries the literal ``s3://`` prefix and names a bucket — the condition
    design §4.4 spells, slashes included.

    A biconditional here rather than one direction, because "refuses everything"
    would satisfy R9.3 while making a sample unreachable: R9.1 is what stops the
    gate from being closed by simply never opening.

    Both halves of the "not parsed" decision are generated: ``S3://`` and
    ``s3:/x`` are the two shapes ``urllib.parse.urlsplit`` reports scheme ``s3``
    for — it lower-cases the scheme and needs no authority — so a rewrite to
    parsing rather than prefix-matching fails here rather than in production.
    """
    assert is_s3_source_uri(candidate.uri) is candidate.is_s3, (
        f"{candidate.uri!r} ({candidate.label}): expected is_s3={candidate.is_s3}"
    )


# ── Level 2: the dispatch ─────────────────────────────────────────────


@given(candidate=_uri_candidates())
@_SETTINGS
def test_no_non_s3_uri_can_reach_a_source_read(candidate):
    """The **only if** direction — the safety-critical one. **Validates: Requirement 9.3**

    For any URI whose scheme is not ``s3``, sample mode resolves to a synthetic
    frame with no ``read_format``, so there is nothing for
    :func:`build_frame_code` to read from. A ``jdbc://`` URI is not merely
    unhandled; it is *unreachable* — :data:`FRAME_MODE_SAMPLE` is returned from
    exactly one place in ``resolve_frame_source`` and that place is guarded by
    :func:`is_s3_source_uri`.

    This is what ``LGR-819980f-1`` (**standing invariant**) protects. A JDBC read
    would need VPC configuration, a Glue connection and credentials, and
    credentials reach Glue only by Secrets Manager ARN because plaintext leaked a
    real credential once. Widening that surface for a *test* is the trade design
    C8 refuses.

    Asserted unconditionally rather than only for the ``jdbc://`` shapes: the
    requirement is about every non-``s3`` scheme, and a future scheme nobody has
    thought of inherits the answer.
    """
    if candidate.is_s3:
        return

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=candidate.uri) as (runner, glue):
        frame_source = runner.resolve_frame_source()

        assert frame_source.is_sample is False, f"{candidate.uri!r} ({candidate.label}) reached a source read"
        assert frame_source.mode == FRAME_MODE_SYNTHETIC
        assert frame_source.read_format is None, "a fallback has nothing to read, so it names no reader"
        # R9.2's second half: the fallback is RECORDED, and recorded as a
        # requested-but-unavailable sample rather than as a plain synthetic run.
        assert frame_source.sample_requested is True
        assert frame_source.sample_unavailable is True
        assert frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
        # Deciding costs nothing: the URI's shape is the whole input.
        assert glue.mock_calls == [], f"resolving a frame source touched Glue: {glue.mock_calls}"


@given(candidate=_uri_candidates())
@_SETTINGS
def test_a_sample_is_built_exactly_when_the_source_is_a_readable_s3_object(candidate):
    """Property 10's readable key. **Validates: Requirements 9.1, 9.2**

    The dispatch claim, keyed on the two conditions that are each *exactly*
    true: a sample is built **iff** the scheme is ``s3`` and the URI says how to
    read it. Where either fails, the frame is synthetic and
    ``unavailable_reason`` names *which* failed.

    Stated this way rather than as design §5's biconditional over the scheme
    alone, because that sentence is false for the folder-prefix URI
    ``file_validation`` really writes for a multi-file upload — see this module's
    docstring. The **only if** half of the design sentence is still exactly true
    and is the property above; this one adds what happens on the other side of
    the gate.
    """
    expected_sample = candidate.is_s3 and candidate.read_format is not None

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=candidate.uri) as (runner, glue):
        frame_source = runner.resolve_frame_source()

        assert frame_source.is_sample is expected_sample, (
            f"{candidate.uri!r} ({candidate.label}): is_s3={candidate.is_s3} "
            f"format={candidate.read_format!r} ⇒ expected is_sample={expected_sample}"
        )
        assert frame_source.unavailable_reason == _expected_unavailable_reason(candidate)
        # `requested_mode` survives the fallback, which is what makes "a sample
        # was requested and the source has none" distinguishable from "no sample
        # was requested" (R9.2).
        assert frame_source.requested_mode == FRAME_MODE_SAMPLE
        assert frame_source.sample_requested is True

        if expected_sample:
            assert frame_source.mode == FRAME_MODE_SAMPLE
            assert frame_source.read_format == candidate.read_format
            assert frame_source.source_uri == candidate.uri
        else:
            assert frame_source.mode == FRAME_MODE_SYNTHETIC
            assert frame_source.read_format is None

        assert glue.mock_calls == []


@given(candidate=_uri_candidates())
@_SETTINGS
def test_no_sample_is_requested_or_recorded_in_synthetic_mode(candidate):
    """The other side of R9.2's distinction: nothing was asked for.

    For any URI — including a perfectly readable ``s3://`` object — synthetic
    mode reads no source and records no unavailability, because nothing was
    unavailable. Without this, ``unavailable_reason`` could be set on every
    synthetic run and R9.2's distinction would carry no information.
    """
    with _mock_runner(frame_mode=FRAME_MODE_SYNTHETIC, source_data_uri=candidate.uri) as (runner, glue):
        frame_source = runner.resolve_frame_source()

        assert frame_source.is_sample is False
        assert frame_source.mode == FRAME_MODE_SYNTHETIC
        assert frame_source.sample_requested is False
        assert frame_source.sample_unavailable is False
        assert frame_source.unavailable_reason is None
        assert glue.mock_calls == []


# ── Level 3: the emitted code, which is what would reach Spark ────────


@given(candidate=_uri_candidates())
@_SETTINGS
def test_only_a_sample_read_names_the_source_object(candidate):
    """R9.3 over the statements actually submitted. **Validates: Requirement 9.3**

    A biconditional over the emitted code: the source URI's text appears in what
    is submitted to Spark **iff** the frame is a sample. So a non-``s3://`` URI's
    text never reaches the session at all — not in a reader, not in a comment,
    not in a fallback branch — and a readable ``s3://`` object's text does, which
    is the positive control that keeps the negative half from being satisfied by
    a runner that reads nothing.

    Asserted over the emitted statement rather than only over the dispatch
    because the emitted statement is what Spark receives; a correct decision
    followed by an incorrect statement would satisfy every property above.

    Written against the *presence of the marker*, and against **containment** of
    a builder's output rather than equality with it, so that additions to the
    submitted frame statement cannot falsify it. That is not hypothetical: task
    6.1 landed ``frame_build_statement`` — ``build_frame_code(…) +
    build_generated_marker_code()`` (``glue_session_runner.py:2085``) — while this
    file was being written, and ``run_guarded_session`` now submits the frame code
    **plus** a ``GENERATED:`` print. Every equality-based assertion about the
    submitted statement broke on that; these did not.
    """
    expected_sample = candidate.is_s3 and candidate.read_format is not None

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=candidate.uri) as (runner, glue):
        run = runner.run_guarded_session(BUILDABLE_SCHEMA, [TRANSFORM_STATEMENT])

        assert not run.refused, "a buildable schema is never refused, so a statement is always submitted"
        submitted = _submitted_code(glue)
        assert submitted, "the run must actually have submitted something"
        assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]

        frame_statement = submitted[0]
        names_the_source = SOURCE_MARKER in "\n".join(submitted)

        if expected_sample:
            assert names_the_source, (
                f"{candidate.uri!r} ({candidate.label}) is readable, so the submitted read must name it"
            )
            # The read is the sample builder's, so it is bounded — `.limit`
            # is what keeps a real object from being pulled in whole.
            assert f".limit({glue_session_runner._SAMPLE_FRAME_ROWS})" in frame_statement
            assert build_sample_frame_code(candidate.uri, candidate.read_format) in frame_statement
        else:
            assert not names_the_source, (
                f"{candidate.uri!r} ({candidate.label}) is not a readable S3 object, yet its text "
                f"reached Spark: {frame_statement!r}"
            )
            # The fallback frame is generated, so it is the synthetic builder's
            # output — containment rather than equality, because what is submitted
            # is the frame code plus task 6.1's `GENERATED:` marker line.
            assert glue_session_runner.build_synthetic_frame_code(BUILDABLE_SCHEMA) in frame_statement


@given(candidate=_uri_candidates(), frame_mode=st.sampled_from([FRAME_MODE_SYNTHETIC, FRAME_MODE_SAMPLE, "Sample", ""]))
@_SETTINGS
def test_no_statement_any_path_submits_reaches_a_jdbc_source(candidate, frame_mode):
    """R9.3 as a claim about credentials. **Validates: Requirement 9.3**

    For any URI and any frame mode — including an unrecognised one, which falls
    through to synthetic with a WARNING — no statement submitted to the session
    names a connection, a driver, a dbtable, a secret, a password or a user.

    ``LGR-819980f-1`` (**standing invariant**) is the reason this is asserted at
    the level of emitted text rather than inferred from the dispatch: plaintext
    credentials in Glue job arguments are visible in the console, CloudTrail and
    job-run history, and that leaked a target DB credential once already. The
    verification session is granted no Secrets Manager access at all
    (``LGR-819980f-2`` is why the ARN route exists and where it lives), so the
    honest check is that none of this vocabulary appears anywhere in what runs.

    Generating the mode as well as the URI is what makes this a claim about
    *every* path rather than about the sample arm: a ``jdbc://`` URI supplied to
    a runner whose mode is a typo must still reach no read.
    """
    with _mock_runner(frame_mode=frame_mode, source_data_uri=candidate.uri) as (runner, glue):
        runner.run_guarded_session(BUILDABLE_SCHEMA, [TRANSFORM_STATEMENT])

        submitted = "\n".join(_submitted_code(glue)).lower()
        assert submitted, "the run must actually have submitted something"

        for forbidden in FORBIDDEN_IN_EMITTED_CODE:
            assert forbidden not in submitted, (
                f"a statement submitted for {candidate.uri!r} ({candidate.label}) in mode "
                f"{frame_mode!r} mentions {forbidden!r} — LGR-819980f-1"
            )
