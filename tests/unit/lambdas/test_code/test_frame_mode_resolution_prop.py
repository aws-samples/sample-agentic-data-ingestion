# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Frame-source resolution is total, AWS-free, and sample-only-if-gated.

Spec `p44-verify-against-real-source-data` task 9.2 — requirements 1.2, 1.4, 1.5,
4.3, 4.4, 4.6; design §4.4.

# Feature: p44-verify-against-real-source-data, Property 1: Frame-source
# resolution is total, AWS-free, and sample-only-if-gated.

    *For any* configured frame-mode string and *any* source-URI value including
    ``None`` and non-strings, :meth:`GlueSessionTestRunner.resolve_frame_source`
    returns exactly one :class:`FrameSource` without calling any AWS API; and
    ``is_sample`` is true **only if** the mode is byte-equal to ``"sample"``
    **and** the URI passes :func:`is_s3_source_uri` **and**
    :func:`sample_read_format` resolves a format — so no ``jdbc://`` URI, no
    ``s3a://`` URI and no extension-less prefix can reach a read.

**Validates: Requirements 1.2, 1.4, 1.5, 4.3, 4.4, 4.6**

What is new here, and what is deliberately not restated
-------------------------------------------------------
Task 9.2 is marked optional because p42 already quantifies both halves of the
dispatch. Read before writing, so this file consolidates rather than duplicates:

* ``test_s3_scheme_gate_prop.py`` (p42 task 5.6) quantifies the **URI** axis with
  the mode fixed to one of two values, and pins :func:`is_s3_source_uri` as a
  biconditional against labels derived from the requirement. **Not restated** —
  the gate's own correctness is that file's claim, and this file consumes it.
* ``test_sample_frame_dispatch.py`` (p42 task 5.5) carries the worked examples:
  the three answers, six named unrecognised modes, the format table, and the
  :class:`FrameSource` construction refusals. **Not restated.**
* ``test_sample_unavailable_prop.py`` (task 6.3) quantifies the four unavailable
  arms in depth with the mode fixed at ``sample``, and asserts its own
  antecedent. **Not restated** — in particular the two ``SAMPLE_UNAVAILABLE_*``
  reasons' no-marker property (C9) is entirely 6.3's.
* ``test_frame_provenance.py`` (task 5.2) owns :meth:`FrameSource.provenance` and
  the ``last_frame_source`` lifetime. **Not restated** — the partition asserted
  below is over the :class:`FrameSource` predicates, not over the six-key dict.

Three things are genuinely new, and they are the whole reason this file exists:

1. **The cross product.** No existing file quantifies over *unconstrained* mode
   strings: the two that vary the mode use 6 and 4 named literals. Here the mode
   axis is arbitrary text plus a near-miss corpus, drawn against the whole URI
   space including non-strings, so totality is a claim about
   ``mode × uri`` rather than about two slices of it.
2. **Totality stated as a partition.** That the three answers are mutually
   exclusive and exhaustive — rather than three cases that happen to be
   asserted separately — and that ``unavailable_reason`` is drawn from a closed
   two-value vocabulary. A fourth arm, or a mode that raises, fails here.
3. **The AWS-free claim measured with a raising client** rather than checked
   after the fact. See below.

R1.5 asserted rather than assumed
---------------------------------
The sibling files assert "no AWS call" as an absence: ``mock_calls == []``, or
``assert_not_called`` on three named methods out of the Glue client's several
dozen. That is an after-the-fact check over an enumeration, and an enumeration
is exactly what a new call site slips past. Here the client the runner holds is
:class:`RaisingGlueClient`, which raises :class:`GlueClientTouched` from **any**
attribute access — so "resolution touches no AWS API" is enforced at the seam
instead of inspected afterwards, and it covers operations nobody thought to
enumerate.

Two vacuity traps that measure comes with, both closed:

* ``GlueSessionTestRunner.__init__`` (``glue_session_runner.py:604-609``) catches
  **every** exception out of ``boto3.client`` and sets ``self.client = None``. A
  client that raised at *construction* would therefore leave the instance with no
  client at all, and "no AWS call" would hold trivially. Every drive asserts
  ``runner.client is client`` for that reason — the sentinel raises when it is
  *used*, never when it is built.
* :class:`GlueClientTouched` derives from ``BaseException`` so that a stray
  ``except Exception`` on the resolution path cannot swallow it and report a
  fallback instead of a failure. ``no_glue_api_calls`` is the backstop for a
  client built through some other seam, and
  :func:`test_the_glue_api_guard_is_not_inert` provokes it on purpose so it
  cannot be silently dead.

Non-vacuity — the "only if" direction has to be able to fail
------------------------------------------------------------
An "only if" is satisfied for free by a resolver that never samples, so the
positive side is quantified too:
:func:`test_a_readable_s3_object_under_the_exact_mode_reaches_a_sample` requires
``is_sample`` **true** for every readable draw under the exact mode, and
:func:`test_a_sample_is_reached_exactly_when_the_mode_and_the_uri_both_qualify`
asserts the biconditional against generator **labels** rather than against the
production predicates. So a resolver that closed the gate entirely fails here,
and one that opened it wider fails there.

Why the safety direction is the load-bearing one
------------------------------------------------
``LGR-819980f-1`` (**standing invariant**, template / ratified, read in full):
JDBC credentials are only ever referenced by Secrets Manager ARN and plaintext
user/password never appear in Glue job arguments or ``ConnectionProperties``,
"because this leaked a target DB credential once already"; its revisit condition
— Glue shipping native secret references — is not met. Its scope is
``agentic_data_ingestion/lambda/**``, which is where the module under test lives.
A Verification_Session is granted none of that machinery, so R4.3 and R4.6 are
absolute rather than not-yet: :data:`FRAME_MODE_SAMPLE` is returned from exactly
one place, guarded by :func:`is_s3_source_uri`, which makes a ``jdbc://`` URI
*structurally unreachable* rather than merely unhandled.
:func:`test_no_jdbc_no_s3a_and_no_extensionless_prefix_can_reach_a_read` states
that over the three named shapes without consulting the production predicates, so
it is a check rather than a restatement of the implementation.

R1.4's unnormalised value is the other governed claim.
``LGR-ad78946-1`` (**template / ratified**) rejected case-insensitive matching and
``.strip()`` for ``VERIFICATION_EXECUTOR`` one level up: "normalising removes the
only signal that the deployed value is not the intended one, so a near-miss takes
the unrecognised arm and is named in a WARNING". The mode corpus below is built to
attack that — ``"Sample"``, ``"sample "``, a Cyrillic ``а`` and a zero-width space
are all *equal-looking* and *not byte-equal*, and each must take the synthetic arm
while ``requested_mode`` keeps the value verbatim. Task 9.3 owns the WARNING
itself and the two-invocation caller claim; this file's mode axis stops at
resolution.

No Glue session is created and no data source is contacted
----------------------------------------------------------
do-NOT 1 and do-NOT 2. This file calls :meth:`resolve_frame_source` and nothing
else — never ``run_guarded_session``, never ``run_test``. That is deliberate and
not incidental: task 5.1's execution found that a sample-mode run with a
*readable* ``s3://`` URI reaches the real ``CreateSession`` **even with
``source_schema=None``**, because ``is_sample`` retires the absent-schema refusal
and nothing is then left to refuse. Every readable draw below is therefore
resolved and dropped, which is the one thing that keeps the readable half of this
property free.

Expectations come from `requirements.md` R1.2 / R1.4 / R1.5 / R4.3 / R4.4 / R4.6
and from :func:`sample_read_format`'s own account of ``file_validation``'s
``FORMAT_MAP`` — the extension table below is **transcribed, not imported**, per
``LGR-28b4d75-5`` (**template / ratified**): a test written from the producer's
output cannot detect the producer drifting, because the output is its own oracle.

Blast radius and coverage, from codegraph `explore`
--------------------------------------------------
Counts are as reported and are inflated by test-site attribution to the module
under test — codegraph counts a call from a test file as a caller "in
``glue_session_runner.py``". Corrected by reading, the production figures are 1
each.

* ``resolve_frame_source`` (``:1066``) — reported 20 callers, all attributed to
  ``glue_session_runner.py``; **1 production caller**, ``run_guarded_session``.
  Covering tests reported: ``test_sample_frame_dispatch``,
  ``test_s3_scheme_gate_prop``, ``test_frame_refusal_reuse``,
  ``test_frame_refusal_precedes_session_prop`` +4.
* ``is_s3_source_uri`` (``:2102``) — reported 8; **1 production caller**,
  ``resolve_frame_source``. Covering: ``test_sample_frame_dispatch``,
  ``test_s3_scheme_gate_prop``, ``test_sample_unavailable_prop``.
* ``sample_read_format`` (``:2157``) — reported 9; **1 production caller**,
  ``resolve_frame_source``. Covering: ``test_sample_frame_dispatch``,
  ``test_sample_read_failure_builders``, ``test_sample_unavailable_prop``,
  ``test_sample_read_failure_prop``.
* ``FrameSource`` (``:323``) — reported 20. Covering: ``test_sample_frame_dispatch``,
  ``test_validation_statement``, ``test_transform_namespace_preamble``,
  ``test_sample_read_failure_builders`` +2.

None is a zero-coverage change area, so no characterization pass is owed, and
**nothing in this task modifies production code** — it is a test-only
consolidation over an already-covered antecedent.

Ledger gaps, stated as gaps rather than as licence
--------------------------------------------------
``decisions(concept="security", include_backfilled=True)`` returns **nothing**,
which requirements C10 already records; ``concept="credentials"`` is where the
governing invariant actually lives. ``why`` on ``glue_session_runner.py`` returns
the three ``5c30733`` records (dedent, transform namespace, the executor caveat)
and the five ``ad78946`` records, every one of the latter carrying
``NOTE: BROAD`` over a 60-file commit — so there is **no fence specific to the
resolver itself**. ``LGR-5c30733-3``'s stated basis is ``frame_mode`` having no
caller, which task 9.1 has now removed; that record's wording is task 12's
subject and not this file's.
"""

import os
import sys
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import botocore.client
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    FrameSource,
    GlueSessionTestRunner,
    is_s3_source_uri,
    sample_read_format,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: The closed vocabulary R1.2 and R4.4 allow in ``unavailable_reason``. A third
#: value appearing here is a change to the requirement, not a detail.
UNAVAILABLE_REASONS = frozenset({SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT})


# ---------------------------------------------------------------------------
# A Glue client that cannot be used, and a backstop under it
# ---------------------------------------------------------------------------


class GlueClientTouched(BaseException):
    """The resolver reached for the Glue client. R1.5 says it must not.

    ``BaseException`` on purpose: an ``Exception`` here could be caught by a
    broad ``except`` on the resolution path and reported as a fallback, which
    would turn a violated requirement into a passing test with a synthetic
    frame.
    """


class RaisingGlueClient:
    """A stand-in for the boto3 Glue client that raises on **any** use.

    Not a ``MagicMock``. A mock records calls and answers them, so the AWS-free
    claim becomes "these particular attributes were not called", checked after
    the fact against an enumeration of methods someone had to remember. This
    raises from ``__getattr__``, so every operation is covered — including ones
    added to the Glue API tomorrow — and the failure lands *at* the offending
    line rather than in a trailing assertion.

    Deliberately raises on *use* and not on construction:
    ``GlueSessionTestRunner.__init__`` catches everything out of
    ``boto3.client`` and falls back to ``self.client = None``
    (``glue_session_runner.py:604-609``), so a client that failed to build would
    make every claim below vacuous.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise GlueClientTouched(
            f"resolve_frame_source reached the Glue client (`.{name}`) — R1.5 requires the frame "
            f"decision to be reached with no AWS API call, and p44 do-NOT 1 forbids a session."
        )


class GlueApiGuardViolation(BaseException):
    """A test in this file reached a real Glue API call. Stub the client."""


#: The session lifecycle operations. Named rather than "everything" so an
#: unrelated call — a region lookup, a credential resolution — is not reported as
#: a session; the instance-level :class:`RaisingGlueClient` is what covers the
#: unenumerated remainder.
GUARDED_GLUE_OPERATIONS = frozenset(
    {
        "CreateSession",
        "GetSession",
        "RunStatement",
        "GetStatement",
        "CancelStatement",
        "StopSession",
        "DeleteSession",
        "ListSessions",
    }
)

_REAL_MAKE_API_CALL = botocore.client.BaseClient._make_api_call


@pytest.fixture(autouse=True)
def no_glue_api_calls(monkeypatch):
    """Make a real Glue session call unreachable from this file.

    The backstop under :class:`RaisingGlueClient`: it guards ``botocore``'s own
    call seam, so a client built through a route the construction patch cannot
    see — ``boto3.Session().client``, a module-import-time client, a patch target
    that moved — fires this instead of reaching AWS. Same measure and same
    reasoning as ``test_sample_read_failure_prop.py``, and
    :func:`test_the_glue_api_guard_is_not_inert` keeps it honest.
    """

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_GLUE_OPERATIONS:
            raise GlueApiGuardViolation(
                f"a unit test reached a real Glue {operation_name} call — p44 do-NOT 1 forbids creating "
                f"an interactive session, which bills. Patch `glue_session_runner.boto3.client` before "
                f"constructing the runner rather than weakening this guard."
            )
        return _REAL_MAKE_API_CALL(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


def _resolve(frame_mode: Any, source_data_uri: Any) -> FrameSource:
    """Build a runner over a client that cannot be used, and resolve once.

    Returns only the :class:`FrameSource`: there is nothing else this file is
    entitled to look at, because going further would reach ``CreateSession`` on
    the readable arm (see the module docstring).
    """
    client = RaisingGlueClient()
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=frame_mode,
            source_data_uri=source_data_uri,
        )

    # The vacuity guard, not decoration: a `None` client would satisfy every
    # AWS-free claim below for the wrong reason.
    assert runner.client is client, (
        "the runner fell back to `self.client = None`, so nothing below measures anything — "
        "`RaisingGlueClient` must raise on use, never on construction"
    )
    assert runner.frame_mode == frame_mode, "the constructor stored a mode other than the one it was given"
    return runner.resolve_frame_source()


# ---------------------------------------------------------------------------
# The mode axis — arbitrary text, plus a corpus built to defeat normalisation
# ---------------------------------------------------------------------------

#: Values that look like ``"sample"`` to a human and are not byte-equal to it.
#: ``LGR-ad78946-1`` is why each must take the synthetic arm rather than being
#: normalised into a sample: a near-miss is the only evidence that the deployed
#: value is not the intended one.
_NEAR_MISS_MODES = (
    "Sample",
    "SAMPLE",
    "SaMpLe",
    "sample ",
    " sample",
    "  sample  ",
    "sample\n",
    "sample\t",
    "sample\r\n",
    "samples",
    "sampl",
    "smaple",
    "sample.",
    "sample/",
    "sample=true",
    "'sample'",
    '"sample"',
    "s\u0430mple",  # a Cyrillic `а` — indistinguishable on screen, not byte-equal
    "sam\u200bple",  # a zero-width space in the middle
    "\ufeffsample",  # a BOM in front
    "ｓａｍｐｌｅ",  # fullwidth forms
    # Not near-misses, but reachable deployed values: a synthetic near-miss, a
    # leftover from the executor variable, and the empty string task 9.1's `or`
    # turns into "unset".
    "Synthetic",
    "synthetic ",
    "SYNTHETIC",
    "glue",
    "shadow",
    "true",
    "1",
    "none",
    "None",
    "",
    "   ",
)


def _modes() -> st.SearchStrategy[str]:
    """Every configured frame-mode string, weighted towards the interesting ones.

    Three arms. The two exact spellings, because they are the only values that
    must be recognised. The near-miss corpus, because that is where a
    normalisation would show. And arbitrary text, because ``frame_mode`` arrives
    from an environment variable and nothing validates it before the runner is
    constructed (task 9.1 deliberately does not — do-NOT 20 and do-NOT 21).
    """
    return st.one_of(
        st.sampled_from((FRAME_MODE_SAMPLE, FRAME_MODE_SYNTHETIC)),
        st.sampled_from(_NEAR_MISS_MODES),
        st.text(alphabet=st.characters(codec="utf-8"), max_size=24),
        # Text drawn from `sample`'s own alphabet, so Hypothesis can reach
        # `"sampl"`, `"ssample"`, `"sample "` and friends on its own rather than
        # only where the corpus above happens to list them.
        st.text(alphabet="samplesynthetic ", max_size=12),
    )


# ---------------------------------------------------------------------------
# The URI axis — four labelled families
# ---------------------------------------------------------------------------

#: The extension table, **transcribed** from ``file_validation``'s ``FORMAT_MAP``
#: content as :func:`sample_read_format` documents it, not imported from the
#: module under test. Importing ``_SOURCE_FORMAT_BY_EXTENSION`` would make the
#: producer its own oracle (``LGR-28b4d75-5``).
_RECOGNISED_EXTENSIONS: dict[str, str] = {
    "csv": "csv",
    "tsv": "csv",
    # p64 R3.1: a `.txt` upload is read "by collapsing onto the `csv`
    # Canonical_Format exactly as `tsv` already does" — so it is transcribed here
    # as the `tsv` row above is, from the requirement's wording rather than from
    # the mirror's current contents (``LGR-28b4d75-5``). R3.6 accepts that `.txt`
    # is a weak signal; that residual is about the *delimiter* a read uses, not
    # about whether a format resolves, so it does not move this row. The pipe
    # itself is `delimiter_for_key`'s and is asserted where the frame is built,
    # not here — this file stops at resolution.
    "txt": "csv",
    "json": "json",
    "jsonl": "json",
    "ndjson": "json",
    "parquet": "parquet",
    "pq": "parquet",
    "avro": "avro",
    "orc": "orc",
}

#: Compression suffixes the resolver looks *through* — but only when something
#: recognisable sits under them. ``orders.gz`` alone is unreadable, and that
#: shape is in the no-format family below.
_COMPRESSION_SUFFIXES = ("gz", "bz2", "snappy", "zst")

#: Extensions outside the table. ``gz``/``bz2`` alone are the interesting ones:
#: a compression suffix with nothing under it must not be looked through to
#: nothing.
#: ``txt`` was here until p64 R3.1 made it readable; it now sits in the table
#: above. ``xlsx``/``xls`` stay, and are the reason this tuple means "not readable
#: by this module" rather than "outside ``FORMAT_MAP``": both *are* in the mirror,
#: as ``excel``, and :func:`sample_read_format` still answers ``None`` for them
#: because ``excel`` has no :data:`_SAMPLE_READERS` entry.
_UNRECOGNISED_EXTENSIONS = ("xlsx", "xls", "log", "zip", "pdf", "yaml", "db", "gz", "bz2")

#: Non-``s3://`` schemes. ``s3a``/``s3n`` are Hadoop spellings and ``S3`` is the
#: case the predicate deliberately does not normalise — all three are shapes
#: ``urllib.parse.urlsplit`` would have reported as scheme ``s3``.
_NON_S3_SCHEMES = ("s3a", "s3n", "S3", "S3A", "https", "http", "file", "gs", "abfss", "wasbs", "hdfs")

_IDENTIFIERS = st.from_regex(r"[a-z][a-z0-9_-]{1,14}", fullmatch=True)

#: Family labels. The URI axis is labelled at **construction**, from the parts it
#: was assembled out of, so the biconditional below is a check rather than a
#: comparison of the module with itself.
READABLE_S3 = "readable_s3"
S3_NO_FORMAT = "s3_no_format"
NOT_S3 = "not_s3"
NON_STRING = "non_string"


class Uri(NamedTuple):
    """A generated source-URI value together with what the requirement says of it.

    ``read_format`` is the format the requirement implies, and is set only on the
    :data:`READABLE_S3` family — everywhere else the answer is settled before a
    format is reached. ``expected_reason`` is the reason a *requested* sample
    earns; it is ``None`` on the readable family, where nothing was unavailable.
    """

    value: Any
    family: str
    read_format: str | None
    expected_reason: str | None
    label: str


def _bucket(name: str) -> str:
    """A source-files bucket, spelled the way `foundation-stack.ts:73-84` does.

    Dot-free, which matters: with no key the *bucket* is the basename, so a
    dotted bucket would move where the format label comes from.
    """
    return f"adi-source-files-{name}"


def _vary_case(draw: st.DrawFn, extension: str) -> str:
    """Extension case, which *is* normalised — the basename is lowercased.

    Included because the scheme's case is deliberately **not** normalised while
    the extension's is, and a file that only ever drew lowercase extensions
    could not tell the two apart.
    """
    return draw(st.sampled_from((extension, extension.upper(), extension.capitalize())))


@st.composite
def _readable_s3_uris(draw: st.DrawFn) -> Uri:
    """An ``s3://`` object whose last path segment says how to read it."""
    extension = draw(st.sampled_from(sorted(_RECOGNISED_EXTENSIONS)))
    read_format = _RECOGNISED_EXTENSIONS[extension]
    suffix = _vary_case(draw, extension)
    if draw(st.booleans()):
        # Look through a compression suffix to the format under it.
        suffix = f"{suffix}.{_vary_case(draw, draw(st.sampled_from(_COMPRESSION_SUFFIXES)))}"
    shape = draw(st.sampled_from(("plain", "nested", "dotted-directory", "awkward-key")))
    bucket = _bucket(draw(_IDENTIFIERS))
    name = draw(_IDENTIFIERS)
    if shape == "nested":
        key = f"{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}/{name}.{suffix}"
    elif shape == "dotted-directory":
        # The dot is in a directory segment; the basename still carries the
        # extension, so this is readable.
        key = f"{draw(_IDENTIFIERS)}.dir/{name}.{suffix}"
    elif shape == "awkward-key":
        # A quote and a backslash in the key. Resolution must answer normally —
        # what stops such a key emitting code that reads a different object is
        # `json.dumps` in the builder, which is task 5.5's claim, not this one's.
        key = f'{draw(_IDENTIFIERS)}/pipe"line\\{name}.{suffix}'
    else:
        key = f"{name}.{suffix}"
    return Uri(f"s3://{bucket}/{key}", READABLE_S3, read_format, None, f"{shape} .{suffix}")


@st.composite
def _s3_uris_with_no_format(draw: st.DrawFn) -> Uri:
    """An ``s3://`` location that names no readable format.

    R4.2's multi-file prefix is the observed instance rather than a hypothetical:
    ``file_validation`` writes the prefix as ``original_s3_uri`` for a multi-file
    upload, and a prefix carries no extension.
    """
    bucket = _bucket(draw(_IDENTIFIERS))
    shape = draw(
        st.sampled_from(
            ("trailing-slash", "bucket-only", "bare-key", "dotted-directory", "unrecognised", "written-directory")
        )
    )
    if shape == "bucket-only":
        uri = f"s3://{bucket}"
    elif shape == "bare-key":
        uri = f"s3://{bucket}/{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}"
    elif shape == "dotted-directory":
        uri = f"s3://{bucket}/{draw(_IDENTIFIERS)}.dir/{draw(_IDENTIFIERS)}"
    elif shape == "unrecognised":
        uri = f"s3://{bucket}/{draw(_IDENTIFIERS)}.{draw(st.sampled_from(_UNRECOGNISED_EXTENSIONS))}"
    elif shape == "written-directory":
        # Spark writes a *directory* named `.parquet`; the object under it has no
        # extension of its own.
        uri = f"s3://{bucket}/{draw(_IDENTIFIERS)}.parquet/part-00000"
    else:
        uri = f"s3://{bucket}/{draw(_IDENTIFIERS)}/"
    return Uri(uri, S3_NO_FORMAT, None, SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT, shape)


@st.composite
def _jdbc_uris(draw: st.DrawFn) -> Uri:
    """Both shapes a JDBC-sourced pipeline's ``source_data_uri`` can carry (R4.4, R4.5).

    ``jdbc://{engine}/{schema}/{table}`` is what ``file_validation`` writes;
    ``jdbc://pipeline/{pipeline_id}`` is ``schema_extract``'s fallback
    (``schema_extract/lambda_function.py:747``). A watermark predicate is
    invisible from here, so R4.4 and R4.5 are the same code path. Also drawn: a
    real ``jdbc:postgresql://`` URL, because that is the shape a reader would
    expect and it must be refused for the same reason.
    """
    engine = draw(st.sampled_from(("mysql", "postgresql", "postgres", "oracle", "sqlserver", "mariadb")))
    shape = draw(st.sampled_from(("engine", "pipeline-fallback", "real-url")))
    if shape == "pipeline-fallback":
        uri = f"jdbc://pipeline/{draw(_IDENTIFIERS)}"
    elif shape == "real-url":
        uri = f"jdbc:{engine}://{draw(_IDENTIFIERS)}.example.internal:5432/{draw(_IDENTIFIERS)}"
    else:
        uri = f"jdbc://{engine}/{draw(_IDENTIFIERS)}/{draw(_IDENTIFIERS)}"
    return Uri(uri, NOT_S3, None, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, f"jdbc {shape}")


@st.composite
def _other_non_s3_uris(draw: st.DrawFn) -> Uri:
    """A URI with a scheme, and the scheme is not the literal ``s3://``."""
    scheme = draw(st.sampled_from(_NON_S3_SCHEMES))
    uri = f"{scheme}://{_bucket(draw(_IDENTIFIERS))}/{draw(_IDENTIFIERS)}.csv"
    return Uri(uri, NOT_S3, None, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, f"scheme {scheme}")


#: URIs with no scheme, or a scheme that names no bucket. Fixed rather than
#: generated because each is a named row in :func:`is_s3_source_uri`'s own table
#: of refusals, and the label is clearer read off that table than derived.
_DEGENERATE_URIS: tuple[Uri, ...] = tuple(
    Uri(value, NOT_S3, None, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, label)
    for value, label in (
        ("s3://", "scheme only, no bucket"),
        ("s3:///orders.csv", "empty bucket"),
        ("s3://   /orders.csv", "whitespace bucket — not a bucket"),
        ("s3:/adi-source-files-a/orders.csv", "no authority; urlsplit would say scheme s3"),
        ("s3:adi-source-files-a/orders.csv", "opaque"),
        ("/adi-source-files-a/orders.csv", "a bare absolute path"),
        ("adi-source-files-a/orders.csv", "a bare relative path"),
        ("", "the empty string — task 9.1's `or` turns this into None upstream"),
        ("   ", "whitespace only"),
        ("\t", "a tab"),
    )
)

#: Non-strings. ``source_data_uri`` is read off a payload, so these are reachable
#: inputs rather than programming errors — which is why
#: :func:`is_s3_source_uri` is annotated ``Any`` and why totality has to cover
#: them. ``True`` is in because ``bool`` is an ``int`` and a truthy value that is
#: not a string is the shape most likely to slip a naive guard.
_NON_STRING_URIS: tuple[Uri, ...] = tuple(
    Uri(value, NON_STRING, None, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, label)
    for value, label in (
        (None, "None — the live value before task 9.1"),
        (0, "int zero"),
        (1, "int one"),
        (True, "bool True"),
        (False, "bool False"),
        (3.5, "float"),
        (b"s3://adi-source-files-a/orders.csv", "bytes that would read as an s3 URI"),
        (bytearray(b"s3://adi-source-files-a/orders.csv"), "bytearray"),
        (["s3://adi-source-files-a/orders.csv"], "a list of one URI"),
        (("s3://adi-source-files-a/orders.csv",), "a tuple of one URI"),
        ({}, "an empty dict"),
        ({"uri": "s3://adi-source-files-a/orders.csv"}, "a dict that looks like a payload"),
        (object(), "an arbitrary object"),
    )
)


def _uris() -> st.SearchStrategy[Uri]:
    """The whole URI space: every family, labelled."""
    return st.one_of(
        _readable_s3_uris(),
        _s3_uris_with_no_format(),
        _jdbc_uris(),
        _other_non_s3_uris(),
        st.sampled_from(_DEGENERATE_URIS),
        st.sampled_from(_NON_STRING_URIS),
    )


#: 300, comfortably over the 100 the task requires. Every example is host-side —
#: no session, no emitted frame, no Spark — so the whole module runs in about a
#: second and the cross product is worth sampling deeply.
#:
#: ``function_scoped_fixture`` is suppressed because :func:`no_glue_api_calls` is
#: autouse and function-scoped. That is correct here rather than tolerated: the
#: guard is applied once per test function and is *not* reset between examples,
#: which is exactly what a fence wants — there is no per-example state in it to
#: leak. The same suppression, for the same fixture, is in
#: ``test_sample_unavailable_prop.py``.
_SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


# ---------------------------------------------------------------------------
# Totality: one answer, always, and the three answers partition the space
# ---------------------------------------------------------------------------


@given(frame_mode=_modes(), uri=_uris())
@_SETTINGS
def test_resolution_is_total_over_every_mode_and_uri(frame_mode: str, uri: Uri):
    """One well-formed :class:`FrameSource`, for any input. **Validates: Requirements 1.2, 1.4, 1.5**

    Totality is the claim: no mode string and no URI value — including a
    non-string, an empty string and ``None`` — makes resolution raise, return
    ``None``, or produce a record whose fields disagree with each other.

    The partition is the second half, and it is what makes "exactly one" mean
    something. The three answers design §4.4 names are asserted **mutually
    exclusive and exhaustive** rather than checked one at a time: a run either
    got a sample, or wanted one and could not have it, or never asked. A fourth
    arm — some future "partial sample", or a mode that produced a record with a
    reason *and* ``is_sample`` — fails here rather than in production.

    ``requested_mode`` carries the configured value byte-for-byte, which is
    ``LGR-ad78946-1``'s posture at this level (R1.4). Compared on the encoded
    bytes as well as with ``==``, so a Unicode normalisation — which would leave
    two visually identical strings comparing equal under some schemes — cannot
    pass.
    """
    frame_source = _resolve(frame_mode, uri.value)

    assert isinstance(frame_source, FrameSource), f"{type(frame_source)!r} is not a FrameSource"
    assert frame_source.mode in (FRAME_MODE_SYNTHETIC, FRAME_MODE_SAMPLE), (
        f"mode={frame_source.mode!r} is outside the recognised pair"
    )
    assert frame_source.requested_mode == frame_mode
    assert frame_source.requested_mode.encode("utf-8", "surrogatepass") == frame_mode.encode(
        "utf-8", "surrogatepass"
    ), "the configured mode was normalised on its way onto the record (R1.4, LGR-ad78946-1)"
    assert frame_source.unavailable_reason is None or frame_source.unavailable_reason in UNAVAILABLE_REASONS, (
        f"unavailable_reason={frame_source.unavailable_reason!r} is outside the two-value vocabulary"
    )

    built_a_sample = frame_source.is_sample
    wanted_one_and_could_not = frame_source.sample_requested and frame_source.sample_unavailable
    asked_for_nothing = not frame_source.sample_requested
    arms = (built_a_sample, wanted_one_and_could_not, asked_for_nothing)
    assert sum(arms) == 1, (
        f"the three answers must partition the space; mode={frame_mode!r} uri={uri.value!r} "
        f"({uri.label}) landed on {arms} — {frame_source!r}"
    )

    # Field-level consistency, in the direction `__post_init__` does not check.
    if built_a_sample:
        assert frame_source.read_format is not None, "a sample names the reader it will use"
        assert frame_source.unavailable_reason is None, "a sample was built, so nothing was unavailable"
    else:
        assert frame_source.mode == FRAME_MODE_SYNTHETIC
        assert frame_source.read_format is None, "a synthetic frame has nothing to read, so it names no reader"
    if asked_for_nothing:
        assert frame_source.unavailable_reason is None, "nothing was requested, so nothing was unavailable"


@given(frame_mode=_modes(), uri=_uris())
@_SETTINGS
def test_resolution_is_deterministic_for_one_configuration(frame_mode: str, uri: Uri):
    """ "Exactly one" answer, not merely "at least one". **Validates: Requirement 1.5**

    Resolving the same configuration twice gives an equal record. Cheap to state
    and it closes the door on a resolver that consulted anything outside its two
    inputs — a clock, a cache, a random reader choice — which is the other way
    "the decision is the URI's shape" could stop being true.
    """
    assert _resolve(frame_mode, uri.value) == _resolve(frame_mode, uri.value)


@given(frame_mode=_modes(), uri=_uris())
@_SETTINGS
def test_resolution_reaches_no_aws_api(frame_mode: str, uri: Uri):
    """R1.5, measured at the seam. **Validates: Requirement 1.5**

    The client the runner holds raises :class:`GlueClientTouched` from any
    attribute access, so reaching resolution's return at all *is* the assertion:
    a single ``self.client.head_object``, ``get_session`` or anything else would
    have raised before this line. ``botocore``'s call seam is guarded underneath
    for a client built some other way.

    This is what makes the decision free — no session, no S3 ``HeadObject``, no
    catalog read — and therefore checkable without Spark or a network, which is
    the property design §4.4 relies on when it evaluates the frame source
    *before* the refusal and before ``CreateSession``.
    """
    frame_source = _resolve(frame_mode, uri.value)

    # Reached only if nothing touched the client. Asserted on the result rather
    # than on a call list, because there is no call list to inspect.
    assert frame_source is not None


# ---------------------------------------------------------------------------
# The gate: is_sample only if the mode AND the URI both qualify
# ---------------------------------------------------------------------------


@given(frame_mode=_modes(), uri=_uris())
@_SETTINGS
def test_is_sample_only_if_the_mode_is_byte_equal_sample_and_the_uri_is_readable(frame_mode: str, uri: Uri):
    """The safety direction, in the task's own three conjuncts. **Validates: Requirements 1.2, 4.6**

    Whenever ``is_sample`` is true, all three antecedents hold: the mode is
    byte-equal to ``"sample"``, the URI passes :func:`is_s3_source_uri`, and
    :func:`sample_read_format` resolves a format. Stated as an implication over
    the production predicates because that is the structural claim R4.6 makes —
    :data:`FRAME_MODE_SAMPLE` is returned from **one** place and that place is
    guarded by both — and ``LGR-819980f-1`` (**standing invariant**) is what the
    guard protects.

    The predicates' own correctness is not this file's claim:
    ``test_s3_scheme_gate_prop.py`` pins :func:`is_s3_source_uri` as a
    biconditional against independently derived labels, and
    ``test_sample_frame_dispatch.py`` pins the format table. What is new here is
    that the *conjunction* is necessary across the whole mode × URI cross
    product, which neither file quantifies.

    Non-vacuous by construction: the readable family under the exact mode reaches
    a sample, which the two tests below require in the opposite direction.
    """
    frame_source = _resolve(frame_mode, uri.value)

    if not frame_source.is_sample:
        return

    assert frame_mode == FRAME_MODE_SAMPLE, (
        f"a sample was built for mode {frame_mode!r}, which is not byte-equal to {FRAME_MODE_SAMPLE!r} — "
        f"the mode was normalised somewhere (R1.4)"
    )
    assert is_s3_source_uri(uri.value) is True, (
        f"a sample was built for {uri.value!r} ({uri.label}), which does not pass the s3:// gate — LGR-819980f-1"
    )
    assert sample_read_format(uri.value) is not None, (
        f"a sample was built for {uri.value!r} ({uri.label}), whose format is unresolvable, so the "
        f"read would be a guess"
    )
    assert frame_source.read_format == sample_read_format(uri.value)
    assert frame_source.source_uri is uri.value, "the URI is carried verbatim, not rebuilt"


@given(
    # Both axes are re-weighted towards the antecedent, and the numbers are
    # measured rather than guessed. With the plain `_modes()` and `_uris()` the
    # joint condition — the exact mode AND the readable family, one arm of six —
    # holds on 8 of 300 examples: non-vacuous, but too thin to explore the
    # readable shapes against the gate. Giving each axis a second arm for its
    # qualifying half takes it to 36 of 300, with the other two answers still
    # drawn 30 and 234 times.
    frame_mode=st.one_of(st.just(FRAME_MODE_SAMPLE), _modes()),
    uri=st.one_of(_readable_s3_uris(), _uris()),
)
@_SETTINGS
def test_a_sample_is_reached_exactly_when_the_mode_and_the_uri_both_qualify(frame_mode: str, uri: Uri):
    """Both directions, against generator **labels**. **Validates: Requirements 1.2, 1.4, 4.6**

    The biconditional the test above deliberately does not state: a sample is
    built **iff** the mode is byte-equal to ``"sample"`` and the URI was
    assembled as a readable ``s3://`` object. The expectation comes from the
    family label attached at construction — from the parts the URI is made of —
    never from calling :func:`is_s3_source_uri` or :func:`sample_read_format`, so
    this can disagree with them.

    That is what makes the "only if" above falsifiable rather than free. A
    resolver that never sampled would satisfy the implication and fail here; one
    that sampled a ``jdbc://`` URI fails there.

    ``read_format`` is checked against the label too, so a reader resolved from
    the wrong path segment — the whole key rather than the last segment — shows
    up as a wrong format rather than as a missing one.
    """
    expected_sample = frame_mode == FRAME_MODE_SAMPLE and uri.family == READABLE_S3

    frame_source = _resolve(frame_mode, uri.value)

    assert frame_source.is_sample is expected_sample, (
        f"mode={frame_mode!r} family={uri.family} ({uri.label}) uri={uri.value!r} ⇒ "
        f"expected is_sample={expected_sample}, got {frame_source!r}"
    )
    if expected_sample:
        assert frame_source.mode == FRAME_MODE_SAMPLE
        assert frame_source.read_format == uri.read_format
    else:
        assert frame_source.mode == FRAME_MODE_SYNTHETIC


@given(uri=_readable_s3_uris())
@_SETTINGS
def test_a_readable_s3_object_under_the_exact_mode_reaches_a_sample(uri: Uri):
    """The positive control, quantified. **Validates: Requirement 1.2**

    Every readable draw under the exact mode reaches a sample, with the labelled
    reader, the URI verbatim, and no unavailability recorded. Without this the
    gate could be shut — refuse everything — and every safety claim above would
    still pass.

    Resolution and nothing further: a sample-mode run with a readable URI reaches
    the real ``CreateSession`` even with ``source_schema=None``, because
    ``is_sample`` retires the absent-schema refusal (do-NOT 1, task 5.1's
    finding).
    """
    frame_source = _resolve(FRAME_MODE_SAMPLE, uri.value)

    assert frame_source.is_sample is True, f"{uri.value!r} ({uri.label}) is readable but reached no sample"
    assert frame_source.mode == FRAME_MODE_SAMPLE
    assert frame_source.requested_mode == FRAME_MODE_SAMPLE
    assert frame_source.read_format == uri.read_format
    assert frame_source.source_uri is uri.value
    assert frame_source.sample_unavailable is False
    assert frame_source.unavailable_reason is None


# ---------------------------------------------------------------------------
# The three shapes R4.3, R4.4 and R4.6 name, asserted without the predicates
# ---------------------------------------------------------------------------


@given(
    frame_mode=_modes(),
    uri=st.one_of(_jdbc_uris(), _other_non_s3_uris(), _s3_uris_with_no_format(), st.sampled_from(_NON_STRING_URIS)),
)
@_SETTINGS
def test_no_jdbc_no_s3a_and_no_extensionless_prefix_can_reach_a_read(frame_mode: str, uri: Uri):
    """R4.3, R4.4 and R4.6 over the named shapes. **Validates: Requirements 4.3, 4.4, 4.6**

    For any mode at all — the exact ``sample``, a near-miss, arbitrary text — a
    ``jdbc://`` URI, an ``s3a://`` URI, an extension-less ``s3://`` prefix and a
    non-string all resolve to a synthetic frame with no reader. Asserted directly
    on the family label, so it does not depend on :func:`is_s3_source_uri` being
    right; if the gate were rewritten to parse the scheme — which would report
    ``s3`` for both ``S3://`` and ``s3:/x`` — this fails here.

    ``LGR-819980f-1`` (**standing invariant**) is the reason R4.3 is absolute
    rather than not-yet: a JDBC read needs VPC configuration, a Glue connection
    and credentials, credentials reach Glue only by Secrets Manager ARN because
    plaintext leaked a real credential once, and a Verification_Session is
    granted none of it. ``LGR-819980f-2`` adds that ``SECRET_ID`` in
    ``ConnectionProperties`` is load-bearing — this file touches none of that
    machinery, and it is cited because a *test* must not be the thing that widens
    the credential surface.

    The reason assertion is conditional on the exact mode, and that condition is
    the point rather than a concession: a near-miss requested **nothing**, so it
    records no unavailability. A file that asserted a reason unconditionally
    would be asserting that a typo requests a sample.
    """
    frame_source = _resolve(frame_mode, uri.value)

    assert frame_source.is_sample is False, (
        f"{uri.value!r} ({uri.label}) reached a source read under mode {frame_mode!r} — LGR-819980f-1"
    )
    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.read_format is None

    if frame_mode == FRAME_MODE_SAMPLE:
        assert frame_source.sample_requested is True
        assert frame_source.unavailable_reason == uri.expected_reason, (
            f"{uri.value!r} ({uri.label}) recorded {frame_source.unavailable_reason!r}, "
            f"expected {uri.expected_reason!r}"
        )
    else:
        # Nothing was asked for, so nothing was unavailable — and the
        # absent-schema refusal therefore stays armed, which is the safe
        # direction for a typo (R2.7, whose depth is task 6.3's).
        assert frame_source.sample_requested is False
        assert frame_source.unavailable_reason is None


@given(frame_mode=_modes().filter(lambda mode: mode != FRAME_MODE_SAMPLE), uri=_uris())
@_SETTINGS
def test_no_mode_other_than_the_exact_spelling_requests_a_sample(frame_mode: str, uri: Uri):
    """R1.4's resolution half. **Validates: Requirements 1.4, 4.6**

    Any mode that is not byte-equal to ``"sample"`` — including a perfectly
    readable URI alongside it — builds a synthetic frame and requests nothing.
    The value is still carried verbatim on ``requested_mode``, which is what
    keeps a typo visible to a reader of the run.

    Task 9.3 owns the WARNING itself and the caller's per-invocation freshness;
    this is the resolver's side of the same requirement, and it is stated over
    arbitrary text rather than over a list of six named typos.
    """
    frame_source = _resolve(frame_mode, uri.value)

    assert frame_source.mode == FRAME_MODE_SYNTHETIC
    assert frame_source.is_sample is False
    assert frame_source.sample_requested is False
    assert frame_source.sample_unavailable is False
    assert frame_source.unavailable_reason is None
    assert frame_source.requested_mode == frame_mode


# ---------------------------------------------------------------------------
# The two guards, so neither is silently inert
# ---------------------------------------------------------------------------


def test_the_raising_client_is_armed():
    """:class:`RaisingGlueClient` raises on use, so the AWS-free claim is measured.

    Without this the sentinel could be a no-op — an ``__getattr__`` that a later
    edit turned into a ``MagicMock``-alike — and every R1.5 assertion above would
    hold for no reason. Two halves: an operation raises, and it raises as a
    ``BaseException`` so a broad ``except Exception`` cannot absorb it into a
    fallback.
    """
    client = RaisingGlueClient()

    with pytest.raises(GlueClientTouched):
        client.create_session(Id="never")

    assert not issubclass(GlueClientTouched, Exception), (
        "GlueClientTouched must sit outside Exception, or a broad except on the resolution path "
        "could swallow it and report a synthetic fallback for a violated R1.5"
    )


def test_the_raising_client_survives_construction():
    """The vacuity trap ``__init__`` sets, closed and stated.

    ``GlueSessionTestRunner.__init__`` catches everything out of ``boto3.client``
    and sets ``self.client = None`` (``glue_session_runner.py:604-609``). If the
    sentinel raised at construction, every runner in this file would hold no
    client and "no AWS call" would be true because there was nothing to call.
    """
    client = RaisingGlueClient()
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)

    assert runner.client is client


def test_the_glue_api_guard_is_not_inert():
    """The backstop itself, so do-NOT 1 is measured rather than asserted in prose.

    Without this, :func:`no_glue_api_calls` could be silently dead — a renamed
    ``botocore`` seam, a monkeypatch that did not take — and every claim that no
    session was created would rest on the construction patch alone. Here the
    guard is provoked on purpose, through the real ``_make_api_call`` seam.
    """
    with pytest.raises(GlueApiGuardViolation):
        botocore.client.BaseClient._make_api_call(MagicMock(), "CreateSession", {})


# ---------------------------------------------------------------------------
# The generators' own antecedents
# ---------------------------------------------------------------------------


@given(uri=_uris())
@_SETTINGS
def test_every_family_label_matches_what_the_predicates_say(uri: Uri):
    """The labels are honest, or the biconditional above is measuring nothing.

    A generator that labelled a readable ``s3://`` object ``not_s3`` — or the
    reverse — would report a false failure or hide a real one. The labels are
    derived from assembly parts, so this is the one place they are checked
    against the predicates, deliberately as a **generator** assertion rather than
    as the property: if a label and a predicate disagree, this test names which
    family drifted instead of the failure surfacing as a resolution bug.

    ``read_format`` is checked here too, which is what lets
    :func:`test_a_sample_is_reached_exactly_when_the_mode_and_the_uri_both_qualify`
    assert the reader from the label.
    """
    if uri.family == READABLE_S3:
        assert is_s3_source_uri(uri.value) is True, f"{uri.value!r} is labelled readable but fails the gate"
        assert sample_read_format(uri.value) == uri.read_format
        assert uri.expected_reason is None
    elif uri.family == S3_NO_FORMAT:
        assert is_s3_source_uri(uri.value) is True, f"{uri.value!r} is labelled s3 but fails the gate"
        assert sample_read_format(uri.value) is None
        assert uri.expected_reason == SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT
    else:
        assert uri.family in (NOT_S3, NON_STRING)
        assert is_s3_source_uri(uri.value) is False, f"{uri.value!r} is labelled non-s3 but passes the gate"
        assert uri.expected_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE


@given(uri=_readable_s3_uris())
@_SETTINGS
def test_the_readable_arm_only_produces_readable_uris(uri: Uri):
    """The readable composite's own antecedent, asserted rather than trusted.

    :func:`test_a_readable_s3_object_under_the_exact_mode_reaches_a_sample` and
    the biconditional both rest on this arm producing genuinely readable URIs. A
    branch that drifted — a shape whose basename lost its extension, a
    compression suffix with nothing under it — would turn the positive control
    into a false failure, and this says which arm drifted instead.
    """
    assert uri.family == READABLE_S3
    assert is_s3_source_uri(uri.value) is True
    assert sample_read_format(uri.value) == uri.read_format
    assert uri.read_format in ("csv", "json", "parquet", "avro", "orc")
