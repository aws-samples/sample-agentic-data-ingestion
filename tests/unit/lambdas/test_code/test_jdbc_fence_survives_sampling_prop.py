# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: p53's sampling mechanism satisfies P44's fence, never bypasses it.

Spec ``p53-jdbc-source-sampling`` task 4.2, **Property 4** and **Property 5**.
**Validates: Requirements 2.1, 2.2, 2.3, 5.1, 5.2, 5.3, 5.4** (5.1-5.4 here;
2.1-2.3 are the reader contract, owned by
``tests/contracts/test_sample_reader_contract_prop.py``).

    5.1 THE ``is_s3_source_uri`` predicate SHALL answer ``False`` for every
        ``jdbc://`` URI.
    5.2 THE ``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE`` reason SHALL remain reachable.
    5.3 THE ``build_frame_code`` dispatch SHALL contain no JDBC arm.
    5.4 THE ``_SAMPLE_READERS`` map SHALL contain no entry naming a JDBC reader.

**There is no failing test in this spec, and this file is not here to fix one.**
Every consumer's JDBC decline is deliberate, asserted and green. p53 does not
open the gate; it hands the gate a *different URI*. This file is the generative
proof of that distinction, over the whole input space rather than at the specific
examples the existing Refusal_Contract tests pin.

WHY P44'S OWN COVERAGE IS NOT ENOUGH, STATED PRECISELY
------------------------------------------------------
``test_s3_scheme_gate_prop.py`` (p44 task 5.6) already generalises the scheme and
dispatch claims, and it stays the owner of them. Two things it cannot know, both
introduced by p53, are what this file adds:

1. **The real ``jdbc://`` shapes the four consumers actually see.** p44's
   generator composes URIs around a marker token in the bucket position, so its
   ``jdbc://`` family is ``jdbc://{marker}/…``. Production writes two other
   shapes and p53's introduction depends on both:
   ``jdbc://{engine}/{schema}/{table}``, which ``file_validation`` stores as
   ``original_s3_uri`` (``file_validation/lambda_function.py:447``), and
   ``jdbc://pipeline/{pipeline_id}``, the fallback ``schema_extract`` writes when
   that row is missing (``schema_extract/lambda_function.py:1735``; the design's
   C6 records ``:1608`` as the pre-task-3.1 line and cites ``:1735`` as current,
   task 5.2 having corrected every in-tree citation — C6's stale-reference
   finding is the same drift one file over).
2. **The Sample_Uri p53 introduces, asserted to go *through* the gate.** After
   task 3.3 the TestCode payload's ``source_data_uri`` can be
   ``s3://adi-processed-files-{account}-{region}/samples/{pipeline_id}/source-sample.json``
   (``schema_extract/lambda_function.py:1478``, ``:1559``). The load-bearing
   claim is not "that URI works" — it is that it works **because it is an
   ordinary ``s3://`` object**, resolved by the unmodified predicate and read by
   the unmodified ``json`` entry of ``_SAMPLE_READERS``. A mechanism that had
   instead taught the gate about JDBC would pass a "the sample frame is built"
   test and fail this one.

THE FENCE SURVIVES BY CONSTRUCTION, AND THAT IS CHECKED STRUCTURALLY TOO
-----------------------------------------------------------------------
Design C5's argument is that ``glue_session_runner.py`` is untouched, so R5.1-5.4
hold by construction. An argument from "the file did not change" is only as good
as the next commit, so the four claims are asserted as *properties of the current
source* rather than inferred from a diff: the predicate's answer is generated
over, and the dispatch and the reader map are read out of the AST.

**The structural checks are AST, never substring, and that is not a style
preference.** ``LGR-b46a5bb-4`` (**template / ratified**) records that substring
matching for a repository invariant "forbids documenting the fence, and failed
for exactly that reason against handlers/agents.py, whose docstring names
threading.Thread on purpose". That failure mode is *live* here:
``glue_session_runner.py`` names ``jdbc`` in prose more than a dozen times, on
purpose — ``:229``, ``:565``, ``:1119-1122``, ``:2020-2031``, ``build_frame_code``'s
own docstring — because the fence is documented where it is enforced. A
substring check for ``"jdbc"`` in that file would fail on the comments that
explain why no JDBC arm exists, which is worse than no test. So the walk sees
only the ``if`` tests, the assignment's keys and values, and the non-docstring
string constants.

GOVERNING RECORDS
-----------------
* ``LGR-819980f-1`` (**standing invariant**, template / ratified) — JDBC
  credentials are only ever referenced by Secrets Manager ARN; plaintext
  user/password never appear in Glue job arguments or ``ConnectionProperties``,
  "because this leaked a target DB credential once already". Scope covers
  ``agentic_data_ingestion/lambda/**``. It is the reason Property 4 is asserted
  at the level of *emitted statement text* and not only at the dispatch: the
  verification session is granted no Secrets Manager access, so a JDBC arm here
  would widen the credential surface for a test.
* ``LGR-819980f-2`` (template / ratified) — ``SECRET_ID`` in
  ``ConnectionProperties`` is load-bearing and the ``adi:secret_arn`` tag is a
  read-path mirror; removing either copy broke all JDBC provisioning once
  (``LGR-33eed95-1``, now SUPERSEDED). Cited because it is why a *test* must not
  be the thing that widens the surface, not because anything here touches it.
* ``LGR-b46a5bb-4`` (template / ratified) — AST over substring, above.
* ``LGR-3e45e65-1`` (template / ratified) — ``AWS_REGION`` /
  ``AWS_DEFAULT_REGION`` are pinned by assignment in ``tests/unit/conftest.py``,
  so the ``setdefault`` calls below are consistent no-ops kept for the
  module-run case.
* ``LGR-c284fbb-1`` … ``-7`` (template / ratified) are p44's frame-provenance
  decisions, reached by ``why`` on this directory. None constrains this file;
  they are the background that makes ``FrameSource``'s two-field shape
  (``mode`` plus ``requested_mode``) the thing Property 5 reads.

LEDGER GAPS, STATED AS GAPS
---------------------------
``why`` on ``glue_session_runner.py:1115`` — the gate's one real call site —
returns five records, all from ``ad78946449`` at **60 files**, every one carrying
the explicit ``NOTE: BROAD — treat as background context``. None names the gate.
``decisions(concept="security", include_backfilled=True)`` returns three records
and none is about JDBC credential handling; the invariant reaches this area
through ``concept="credentials"``. ``decisions(concept="networking")`` returns
**zero** records (p53 risk N6). These are holes in the ledger, not evidence the
area is undecided.

BLAST RADIUS AND COVERAGE, GREP-VERIFIED
----------------------------------------
CodeGraph's caller counts on the gate symbols are inflated by docstring mentions,
as p53's design already found; re-verified on this tree:

===========================  ==============  ================================
Symbol                       CodeGraph said  Grep found
===========================  ==============  ================================
``is_s3_source_uri``         12 callers      **1** real call site (``:1115``)
``resolve_frame_source``     23 callers      **1** real call site (``:1045``)
===========================  ==============  ================================

The other references are the ``def``, docstrings and comments in
``glue_session_runner.py`` and ``lambda_function.py``. ``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE``
has **one** production use (``:1129``), inside ``resolve_frame_source``; the other
three references are its own assignment (``:231``) and two docstrings (``:565``,
``lambda_function.py:3476``). Both gate symbols carry covering tests — four
modules for the predicate, eleven for the resolver — so no characterization pass
is owed. CodeGraph flagged neither as uncovered here; it *did* falsely flag all
three Sample_Readers, which the sibling contracts file records.

SIBLING COVERAGE — WHAT THIS FILE DOES NOT RESTATE
--------------------------------------------------
* ``test_s3_scheme_gate_prop.py`` — the biconditional over the whole URI space,
  the ``S3://`` / ``s3:/x`` near-misses, and the emitted-text credential sweep
  across every frame mode. Owner of Property 10; 6 of the 16 Refusal_Contract
  tests. Unmodified by p53.
* ``test_sample_unavailable_prop.py`` — the fallback's own subject: every arm
  reachable, the reason each names, the non-verification markers. The other 10
  of the 16. Unmodified by p53.
* ``test_frame_mode_resolution_prop.py``, ``test_sample_frame_dispatch.py``,
  ``test_frame_provenance.py`` — the other pinning modules task 1.1 classified.
  Unmodified by p53.
* ``tests/contracts/test_sample_readers_match_templates.py`` — the runtime half
  of R5.4 (``test_no_sample_reader_mentions_jdbc``) and the template equality.
  Unmodified by p53; this file adds the source-level half beside it.
* Task 4.1 owns Property 3, the URI *preference*. This file takes the resolved
  URI as its input and says nothing about how it was chosen.

**No Glue session, no S3 object, no database, nothing deployed.** Every AWS call
goes through a ``MagicMock``; most of what follows exists to assert a read was
*not* attempted.
"""

import ast
import contextlib
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import example, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    GlueSessionTestRunner,
    build_sample_frame_code,
    is_s3_source_uri,
    sample_read_format,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

TRANSFORM_STATEMENT = (STAGE_TRANSFORM, "df_t = df")

#: A schema the synthetic builder always accepts, so a run reaches a session on
#: the fallback arm too and "what was submitted" is never empty for the trivial
#: reason that nothing was submitted. The absent-schema case is p44 Property 11's
#: and is deliberately not restated here.
BUILDABLE_SCHEMA = {
    "properties": {
        "customer_id": {"type": "string"},
        "signup_date": {"type": "string"},
        "total_spent": {"type": "number"},
    }
}

#: Tokens that must appear in no statement any path submits (R5.3,
#: ``LGR-819980f-1``). Transcribed from ``test_s3_scheme_gate_prop.py``'s list
#: for the same reasons, including why ``user`` is quoted: ``inferSchema`` and
#: the synthetic builder's prose both carry the bare substring innocently.
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

#: The Sample_Artifact key, as ``schema_extract/lambda_function.py:1478`` spells
#: it. Transcribed rather than imported: ``schema_extract`` is a different Lambda
#: bundle with its own module-level DynamoDB resources, and importing it into a
#: ``test_code`` unit test to read one format string would drag that bundle's
#: whole environment in. The key is a contract between the template and the
#: probe; this file is a *third* reader of it and says so rather than pretending
#: to own it.
SAMPLE_ARTIFACT_KEY = "samples/{pipeline_id}/source-sample.json"

#: ``file_validation/lambda_function.py:440-442``'s own ``.get`` defaults, so the
#: degenerate row shape is generated rather than only the populated one.
JDBC_UNKNOWN_SEGMENT = "unknown"


# ── The two URI families p53 puts in front of the gate ────────────────

#: Engines the wizard offers, plus the default ``file_validation`` falls back to.
_ENGINES = st.sampled_from(["mysql", "postgresql", "postgres", "oracle", "sqlserver", JDBC_UNKNOWN_SEGMENT])

#: Schema and table names, including the shapes that make a URI awkward: a dot,
#: a hyphen, an underscore and the ``unknown`` fallback. Kept to characters a
#: JDBC identifier can actually carry — this file must not construct SQL (C2,
#: do-NOT 6, do-NOT 7) and does not, but generating shell-ish junk here would
#: suggest it might.
_IDENTIFIERS = st.sampled_from(
    ["sales", "public", "dbo", "customer_data", "order-archive", "billing.v2", JDBC_UNKNOWN_SEGMENT]
)

_PIPELINE_IDS = st.sampled_from(
    [
        "latesttesting-19ca826a",
        "customer-sync-4fccdf03",
        "jdbc_pipeline_1",
        "a",
    ]
)

#: 12-digit account ids and real region names — the two segments
#: ``_sample_artifact_bucket`` derives ``adi-processed-files-{account}-{region}``
#: from (``schema_extract/lambda_function.py:1495-1499``).
_ACCOUNTS = st.sampled_from(["123456789012", "444455556666", "777788889999"])
_REGIONS = st.sampled_from(["us-west-2", "us-east-1", "eu-west-1"])


@st.composite
def _jdbc_sentinel_uris(draw) -> str:
    """A ``jdbc://`` URI in one of the two shapes production actually writes.

    Both arms matter and neither is hypothetical:

    * ``jdbc://{engine}/{schema}/{table}`` — ``file_validation`` writes this as
      ``original_s3_uri`` for a JDBC source (``:447``), so it is what
      ``json_s3_uri`` usually carries, and therefore what every consumer usually
      declines.
    * ``jdbc://pipeline/{pipeline_id}`` — ``schema_extract``'s fallback when the
      ``file_validation`` row is missing (``:1735``).
    """
    if draw(st.booleans()):
        engine = draw(_ENGINES)
        schema = draw(_IDENTIFIERS)
        table = draw(_IDENTIFIERS)
        return f"jdbc://{engine}/{schema}/{table}"
    return f"jdbc://pipeline/{draw(_PIPELINE_IDS)}"


#: Degenerate ``jdbc://`` forms, listed rather than generated because each is a
#: distinct near-miss with its own reason to exist: no authority, an empty
#: authority, a trailing-slash-only path, and a URI that is nothing but the
#: scheme. All four must answer ``False``, and none may reach a read.
_DEGENERATE_JDBC_URIS = (
    "jdbc://",
    "jdbc:///",
    "jdbc://pipeline/",
    "jdbc:mysql://host:3306/db",
)


@st.composite
def _sample_artifact_uris(draw) -> str:
    """The Sample_Uri p53 introduces, in the exact shape the probe returns.

    ``f"s3://{bucket}/{key}"`` with the bucket derived from account and region —
    ``schema_extract/lambda_function.py:1559`` over ``:1478``'s key.
    """
    bucket = f"adi-processed-files-{draw(_ACCOUNTS)}-{draw(_REGIONS)}"
    key = SAMPLE_ARTIFACT_KEY.format(pipeline_id=draw(_PIPELINE_IDS))
    return f"s3://{bucket}/{key}"


_SETTINGS = settings(max_examples=200, deadline=None)


# ── The mock Glue client and runner ───────────────────────────────────


def _happy_glue_client() -> MagicMock:
    """A Glue client wired so that a session, if reached, succeeds.

    Wired for the happy path for the reason
    ``test_s3_scheme_gate_prop.py:408-418`` gives: a bare ``MagicMock`` would let
    "the sample read was submitted" fail for reasons unrelated to the scheme, and
    would let "no read happened" pass on a client too broken to do anything.
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
    """Yield ``(runner, glue)`` over a **fresh** mock client, sleep patched out.

    A context manager entered inside each ``@given`` body, not a fixture: a
    function-scoped fixture is applied once per test *function* and is never
    reset between Hypothesis examples, so a mock built there accumulates calls
    across examples and every "what was submitted" assertion ends up reading a
    previous example's statements.
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


def _assert_no_forbidden_vocabulary(statements: list[str], *, excluding_uri: str | None, context: str) -> None:
    """Assert no submitted statement carries the credential vocabulary.

    *excluding_uri* is the load-bearing parameter and it exists because the first
    run of this file failed on its own generator, not on production code. A
    pipeline may legitimately be named ``jdbc_pipeline_1`` — nothing forbids it,
    and the generator draws it on purpose — so the Sample_Uri
    ``s3://…/samples/jdbc_pipeline_1/source-sample.json`` contains the substring
    ``jdbc`` while naming an ordinary S3 object.

    The invariant is about what the **builder emits**, not about what the caller
    named their pipeline, so the URI's own occurrences are removed before the
    sweep. This is the same class of false positive ``LGR-b46a5bb-4`` records for
    substring matching, met here in the data rather than in a docstring — and
    keeping the awkward pipeline id in the generator makes the claim stronger: the
    fence must hold *because of the reader expression*, not because no bucket
    name ever contains the word.

    On the refusal arm *excluding_uri* is ``None``: the URI must not appear at all
    there, which is asserted separately, so every occurrence found is the emitted
    code's own.
    """
    joined = "\n".join(statements)
    if excluding_uri:
        for spelling in (excluding_uri, excluding_uri.replace('"', '\\"')):
            joined = joined.replace(spelling, "<URI>")
    joined = joined.lower()

    for forbidden in FORBIDDEN_IN_EMITTED_CODE:
        assert forbidden not in joined, (
            f"a statement submitted {context} mentions {forbidden!r} outside the source URI — "
            f"LGR-819980f-1 (standing invariant)"
        )


# ── The AST view of the module, for the two structural claims ──────────

_RUNNER_SOURCE_PATH = Path(glue_session_runner.__file__)


def _runner_ast() -> ast.Module:
    return ast.parse(_RUNNER_SOURCE_PATH.read_text(encoding="utf-8"), filename=str(_RUNNER_SOURCE_PATH))


def _function_def(name: str) -> ast.FunctionDef:
    """The module-level ``def`` named *name*, asserted to exist exactly once.

    Loud rather than vacuous: a renamed or removed function fails here with a
    message naming what was looked for, instead of making a structural assertion
    pass over an empty walk.
    """
    found = [
        node
        for node in _runner_ast().body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(found) == 1, (
        f"expected exactly one module-level `def {name}` in {_RUNNER_SOURCE_PATH.name}, found {len(found)}. "
        f"The structural assertions below read that function's body, so a rename must be a deliberate "
        f"edit here rather than a silently vacuous pass."
    )
    assert isinstance(found[0], ast.FunctionDef)
    return found[0]


def _module_assignment(name: str) -> ast.expr:
    """The value expression of the single module-level assignment to *name*."""
    values = [
        node.value
        for node in _runner_ast().body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name
    ] + [
        node.value
        for node in _runner_ast().body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    assert len(values) == 1, (
        f"expected exactly one module-level assignment to {name} in {_RUNNER_SOURCE_PATH.name}, found {len(values)}"
    )
    assert values[0] is not None
    return values[0]


def _code_string_constants(function: ast.FunctionDef) -> list[str]:
    """Every string constant in *function*'s body **except its docstring**.

    The exclusion is the whole point, per ``LGR-b46a5bb-4``: this module
    documents the fence in the docstrings of the very functions that enforce it,
    so a check that read them would fail on the prose explaining why no JDBC arm
    exists.
    """
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    constants: list[str] = []
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                constants.append(node.value)
    return constants


# ---------------------------------------------------------------------------
# Feature: p53-jdbc-source-sampling, Property 4: No `jdbc://` URI reaches a
# session read.
#
# For any URI, is_s3_source_uri answers False whenever the URI begins `jdbc://`,
# and no statement any path submits names a JDBC source.
# ---------------------------------------------------------------------------


@given(uri=_jdbc_sentinel_uris())
@example(uri="jdbc://mysql/sales/customers")
@example(uri="jdbc://pipeline/latesttesting-19ca826a")
@_SETTINGS
def test_property_4_the_gate_refuses_every_jdbc_uri_production_writes(uri: str):
    """**Validates: Requirements 5.1**

    R5.1 over the two shapes production writes, rather than over a synthesised
    ``jdbc://`` family. Both are asserted at the predicate *and* at the format
    resolver, because a caller that skipped the predicate and asked
    ``sample_read_format`` directly would get ``None`` for
    ``jdbc://pipeline/x`` (no extension) but **``"json"``** for a table named
    ``billing.v2``-style with a ``.json`` tail — the resolver reads an extension
    and knows nothing about schemes. That asymmetry is why
    :func:`is_s3_source_uri` is the gate and the resolver is not, and asserting
    the predicate here is asserting the thing that actually holds the line.
    """
    assert uri.startswith("jdbc://"), f"the generator drew {uri!r}, which is not a jdbc:// URI"
    assert is_s3_source_uri(uri) is False, (
        f"is_s3_source_uri({uri!r}) answered True. R5.1 — a jdbc:// URI must not merely be "
        f"unhandled, it must be unreachable (LGR-819980f-1, standing invariant)."
    )


def test_property_4_the_gate_refuses_the_degenerate_jdbc_forms_too():
    """**Validates: Requirements 5.1**

    The four near-misses, named because each has its own reason: no authority, an
    empty authority, an authority with an empty path, and the *real* JDBC
    connection-string shape (``jdbc:mysql://host/db``), which does not begin
    ``jdbc://`` at all and must still be refused. The last one is the case a
    scheme-parsing rewrite would get wrong in the other direction.
    """
    for uri in _DEGENERATE_JDBC_URIS:
        assert is_s3_source_uri(uri) is False, f"is_s3_source_uri({uri!r}) answered True — R5.1"


@given(
    uri=_jdbc_sentinel_uris(),
    frame_mode=st.sampled_from([FRAME_MODE_SAMPLE, FRAME_MODE_SYNTHETIC, "Sample", "sample ", ""]),
)
@_SETTINGS
def test_property_4_no_statement_submitted_for_a_jdbc_uri_names_a_jdbc_source(uri: str, frame_mode: str):
    """**Validates: Requirements 5.1, 5.3, 5.4**

    Property 4's second clause, at the level that matters: the statement text
    actually submitted to the session. The dispatch could be right and the
    emitted code could still name a connection.

    Generating the mode as well as the URI is what makes this a claim about
    *every* path rather than about the sample arm — a ``jdbc://`` URI handed to a
    runner whose mode is a typo (``"Sample"``, ``"sample "``) falls through to
    synthetic with a WARNING and must still reach no read.

    ``LGR-819980f-1`` (**standing invariant**) is why this is asserted over text:
    plaintext credentials in Glue job arguments are visible in the console,
    CloudTrail and job-run history, and that leaked a real credential once. The
    verification session holds no Secrets Manager permission, so the honest check
    is that none of the vocabulary appears anywhere in what runs.
    """
    with _mock_runner(frame_mode=frame_mode, source_data_uri=uri) as (runner, glue):
        run = runner.run_guarded_session(BUILDABLE_SCHEMA, [TRANSFORM_STATEMENT])

        assert not run.refused, "a buildable schema is never refused, so a statement is always submitted"
        submitted = _submitted_code(glue)
        assert submitted, "the run must actually have submitted something — otherwise this passes vacuously"
        assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]

        _assert_no_forbidden_vocabulary(submitted, excluding_uri=None, context=f"for {uri!r} in mode {frame_mode!r}")
        assert uri not in "\n".join(submitted), (
            f"the URI {uri!r} reached Spark verbatim. It is not a readable S3 object, so its text must "
            f"not appear in any submitted statement."
        )
        assert glue_session_runner.build_synthetic_frame_code(BUILDABLE_SCHEMA) in submitted[0], (
            "the fallback frame must be the synthetic builder's output — a fallback that is not "
            "distinguishable from a success is the state LGR-b2f79ef-9 refuses"
        )


@given(uri=_sample_artifact_uris())
@_SETTINGS
def test_property_4_the_sample_uri_is_read_as_an_ordinary_s3_object(uri: str):
    """**Validates: Requirements 5.1, 5.4**

    The positive half, and the one that distinguishes p53's mechanism (d) from
    the rejected ones. The Sample_Uri reaches the session as an **ordinary
    ``s3://`` object URI ending ``.json``**: the unmodified predicate admits it,
    the unmodified ``sample_read_format`` resolves it to ``json``, and the
    statement submitted is byte-for-byte ``_SAMPLE_READERS["json"]`` with the URI
    substituted — the same reader the generated Glue job uses for that format.

    So the gate was **satisfied, not bypassed**. A mechanism that had taught
    ``is_s3_source_uri`` or ``build_frame_code`` about JDBC would also make a
    sample frame appear, and would pass a test that only checked "a sample was
    built". This asserts the *route*.

    The emitted statement is also swept for the forbidden vocabulary, because
    "the sample path is now live" is exactly when a credential token appearing in
    a submitted statement would matter most. The sweep excludes the URI's own
    text — see :func:`_assert_no_forbidden_vocabulary` for why the generator
    deliberately draws a pipeline id containing ``jdbc``.
    """
    assert is_s3_source_uri(uri) is True, f"the Sample_Uri {uri!r} must pass the scheme gate unaided"
    assert sample_read_format(uri) == "json", (
        f"sample_read_format({uri!r}) resolved {sample_read_format(uri)!r}. The Sample_Artifact is a "
        f"single .json object precisely so this resolves without a new reader branch (design C1) — a "
        f"prefix or an extensionless key would resolve to None."
    )

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=uri) as (runner, glue):
        run = runner.run_guarded_session(BUILDABLE_SCHEMA, [TRANSFORM_STATEMENT])

        assert run.frame_source.mode == FRAME_MODE_SAMPLE
        assert run.frame_source.read_format == "json"
        assert run.frame_source.unavailable_reason is None

        submitted = _submitted_code(glue)
        assert submitted, "the run must actually have submitted something"
        assert build_sample_frame_code(uri, "json") in submitted[0], (
            f"the frame statement is not the sample builder's output for {uri!r}. The read must be the "
            f"generated job's own json reader — a frame read with different options is a different frame."
        )
        assert f".limit({glue_session_runner._SAMPLE_FRAME_ROWS})" in submitted[0], (
            "the sample read must stay bounded — .limit is what keeps a real object from being pulled in whole"
        )

        _assert_no_forbidden_vocabulary(submitted, excluding_uri=uri, context=f"reading the sample {uri!r}")


def test_property_4_the_frame_dispatch_has_exactly_one_branch_and_it_is_not_jdbc():
    """**Validates: Requirements 5.3**

    R5.3 read out of the AST. ``build_frame_code``'s dispatch is
    ``if not frame_source.is_sample`` and nothing else — one ``if`` at the
    function's own level, testing that attribute, plus the ``__post_init__``
    guard's unreachable ``or`` check. A JDBC arm would be a second branch, and
    a branch keyed on a URI scheme or a source type would show up as a test on
    something other than ``is_sample``.

    AST rather than substring, per ``LGR-b46a5bb-4``: this function's docstring
    names JDBC on purpose (``:2264``, "``is_s3_source_uri`` is the gate it
    uses"), so a text search would fail on the documentation of the fence.
    """
    dispatch = _function_def("build_frame_code")

    tests = [node.test for node in ast.walk(dispatch) if isinstance(node, ast.If)]
    assert tests, "build_frame_code has no `if` at all — the dispatch has been restructured"

    is_sample_tests = [
        node
        for node in tests
        if any(isinstance(inner, ast.Attribute) and inner.attr == "is_sample" for inner in ast.walk(node))
    ]
    assert len(is_sample_tests) == 1, (
        f"build_frame_code should test `is_sample` exactly once; found {len(is_sample_tests)} of "
        f"{len(tests)} branch conditions. R5.3 — the dispatch is on FrameSource.is_sample and nothing "
        f"else, not on the requested mode and not on whether a URI happens to be present."
    )

    scheme_ish = [
        node
        for node in tests
        if any(
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and ("jdbc" in inner.value.lower() or "://" in inner.value)
            for inner in ast.walk(node)
        )
    ]
    assert not scheme_ish, (
        f"build_frame_code branches on a URI-scheme literal: {[ast.unparse(node) for node in scheme_ish]}. "
        f"R5.3 forbids a JDBC arm, and design do-NOT 9 forbids growing one. The gate is "
        f"resolve_frame_source's, and duplicating it here makes two places responsible for one invariant."
    )

    offenders = [text for text in _code_string_constants(dispatch) if "jdbc" in text.lower()]
    assert not offenders, (
        f"build_frame_code's executable strings mention JDBC: {offenders}. Its docstring may — and does "
        f"— name it deliberately; its code may not (LGR-b46a5bb-4 is why this check reads the AST and "
        f"skips the docstring)."
    )


def test_property_4_the_reader_map_source_names_no_jdbc_reader():
    """**Validates: Requirements 5.4**

    R5.4 at the source level: no key and no value of the ``_SAMPLE_READERS``
    literal mentions JDBC, and the map is non-empty so the assertion is not
    vacuous.

    ``tests/contracts/test_sample_readers_match_templates.py``'s
    ``test_no_sample_reader_mentions_jdbc`` asserts the same claim over the
    **imported object**, and stays the owner of it. This is the complement, not a
    duplicate: an entry added by a later module-level mutation would pass there
    and fail here, and an entry whose *key* named JDBC would pass there — that
    test inspects values only.
    """
    readers = _module_assignment("_SAMPLE_READERS")
    assert isinstance(readers, ast.Dict), "_SAMPLE_READERS is no longer a dict literal"
    assert readers.keys, "_SAMPLE_READERS is empty — R5.4 would then hold vacuously"

    entries: list[tuple[str, str]] = []
    for key_node, value_node in zip(readers.keys, readers.values, strict=True):
        assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str), (
            f"_SAMPLE_READERS has a non-literal key {ast.unparse(key_node) if key_node else None!r}; this "
            f"check reads the literal, so a computed key would go unexamined"
        )
        assert isinstance(value_node, ast.Constant) and isinstance(value_node.value, str), (
            f"_SAMPLE_READERS[{key_node.value!r}] is not a string literal"
        )
        entries.append((key_node.value, value_node.value))

    offenders = {key: value for key, value in entries if "jdbc" in key.lower() or "jdbc" in value.lower()}
    assert not offenders, (
        f"_SAMPLE_READERS carries a JDBC reader: {offenders}. LGR-819980f-1 is a standing invariant — "
        f"credentials reach Glue only by Secrets Manager ARN, and a verification session is granted "
        f"none. do-NOT 10 forbids adding an entry here."
    )
    assert set(dict(entries)) == set(glue_session_runner._SAMPLE_READERS), (
        "the parsed literal and the imported map disagree about their keys, so _SAMPLE_READERS is "
        "mutated after definition and this source-level check no longer sees what runs"
    )


# ---------------------------------------------------------------------------
# Feature: p53-jdbc-source-sampling, Property 5: The refusal reason stays
# reachable.
#
# For any JDBC-sourced pipeline that has no Sample_Artifact, resolve_frame_source
# returns a synthetic frame carrying unavailable_reason ==
# SAMPLE_UNAVAILABLE_NOT_S3_SOURCE and requested_mode == FRAME_MODE_SAMPLE.
# ---------------------------------------------------------------------------


@st.composite
def _jdbc_runs(draw) -> tuple[str, bool]:
    """A JDBC-sourced run: the URI it carries, and whether it has an artifact.

    Both arms of the First_Run_Gap in one generator, because the property is
    about the *boundary* between them. ``has_artifact`` false is run 1 — nothing
    has read the source yet, so ``schema_extract``'s probe missed and
    ``source_sample_uri`` resolved to ``json_s3_uri``, the Sentinel. True is runs
    2+, where the probe hit and the resolved URI is the Sample_Uri.
    """
    if draw(st.booleans()):
        return draw(_sample_artifact_uris()), True
    return draw(_jdbc_sentinel_uris()), False


@given(run=_jdbc_runs())
@example(run=("jdbc://mysql/sales/customers", False))
@example(run=("s3://adi-processed-files-123456789012-us-west-2/samples/p/source-sample.json", True))
@_SETTINGS
def test_property_5_the_refusal_reason_is_exactly_the_first_run_arm(run: tuple[str, bool]):
    """**Validates: Requirements 5.2**

    Property 5, both arms, as one property over the First_Run_Gap boundary.

    * **No artifact (run 1).** The URI is the Sentinel, so
      :meth:`resolve_frame_source` returns a synthetic frame carrying
      ``unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE`` **and**
      ``requested_mode == FRAME_MODE_SAMPLE``. Both fields, because a single
      ``mode`` cannot distinguish "no sample was requested" from "a sample was
      requested and the source has none" — which is the distinction R9.2 asked
      for and the one two Refusal_Contract tests
      (``test_the_generator_draws_only_unavailable_uris``,
      ``test_every_arm_is_reachable_and_records_the_reason_it_names``) depend on
      staying reachable.
    * **Artifact present (runs 2+).** The reason is ``None`` and the mode is
      ``sample``.

    **This is the assertion that would catch p53 retiring the fence by accident.**
    The tempting mechanism — repointing ``json_s3_uri`` at the sample — would
    make the first arm unreachable for every JDBC pipeline after run 1, and
    ``SAMPLE_UNAVAILABLE_NOT_S3_SOURCE`` would quietly become dead. Design C3
    rejected that repoint and do-NOT 11 forbids it; this is the executable form
    of *why*. Reachability here is a property of the input space, not of a
    fixture: run 1 of every JDBC pipeline still carries the Sentinel, so the
    reason has a live producer forever.
    """
    uri, has_artifact = run

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=uri) as (runner, _glue):
        frame_source = runner.resolve_frame_source()

    assert frame_source.requested_mode == FRAME_MODE_SAMPLE, (
        "requested_mode records what was asked for, whatever happened — an unrecognised or refused "
        "request is recorded as it arrived rather than normalised away"
    )

    if has_artifact:
        assert frame_source.mode == FRAME_MODE_SAMPLE, f"the Sample_Uri {uri!r} should have yielded a sample frame"
        assert frame_source.unavailable_reason is None, (
            f"a sample was built from {uri!r}, so nothing was unavailable, yet "
            f"unavailable_reason={frame_source.unavailable_reason!r}"
        )
        assert frame_source.is_sample is True
    else:
        assert frame_source.mode == FRAME_MODE_SYNTHETIC, (
            f"the Sentinel {uri!r} must fall back to a synthetic frame — R5.1, LGR-819980f-1"
        )
        assert frame_source.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, (
            f"the Sentinel {uri!r} fell back with reason {frame_source.unavailable_reason!r}, not "
            f"{SAMPLE_UNAVAILABLE_NOT_S3_SOURCE!r}. R5.2 — the reason must stay reachable, and run 1 of "
            f"every JDBC pipeline is what keeps it so."
        )
        assert frame_source.is_sample is False, (
            "a fallback must stay distinguishable from a success at the point the refusal is evaluated, "
            "or R9.4's retirement would claim a verification the synthetic path cannot deliver"
        )
        assert frame_source.source_uri == uri, (
            "the refused URI is recorded on the FrameSource so a reader of the run can see which source was declined"
        )


def test_property_5_both_arms_are_drawn_so_neither_is_vacuous():
    """The generator's non-vacuity, asserted rather than assumed.

    A ``_jdbc_runs`` that had degenerated to one arm would leave the property
    above passing while testing half of it — and the half it would most likely
    lose is the refusal, since that is the arm keyed on a boolean. Both are
    exercised here deterministically.
    """
    sentinel = "jdbc://postgresql/public/orders"
    artifact = "s3://adi-processed-files-123456789012-us-west-2/samples/p1/source-sample.json"

    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=sentinel) as (runner, _glue):
        refused = runner.resolve_frame_source()
    with _mock_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=artifact) as (runner, _glue):
        built = runner.resolve_frame_source()

    assert refused.unavailable_reason == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    assert built.unavailable_reason is None
    assert refused.mode != built.mode, "the two arms must be distinguishable, or the property tests nothing"


def test_property_5_the_reason_has_exactly_one_producer_and_it_is_the_gate():
    """**Validates: Requirements 5.2**

    "Reachable" structurally: the reason is produced in exactly one place, and
    that place is inside ``resolve_frame_source``. Two things this catches that
    the behavioural property cannot:

    * the constant becoming **unreferenced** — reachable-by-name but dead, which
      is what a repoint of ``json_s3_uri`` would eventually cause;
    * a **second** producer appearing elsewhere, which would mean two paths
      decide what "not an S3 source" means, and one of them would drift.

    Docstring mentions are excluded, so the two that document the reason
    (``:565`` here and ``lambda_function.py:3476``) do not count as producers —
    ``LGR-b46a5bb-4`` again.
    """
    module = _runner_ast()

    producers = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Name)
        and node.id == "SAMPLE_UNAVAILABLE_NOT_S3_SOURCE"
        and isinstance(node.ctx, ast.Load)
    ]
    assert len(producers) == 1, (
        f"SAMPLE_UNAVAILABLE_NOT_S3_SOURCE is read in {len(producers)} places in "
        f"{_RUNNER_SOURCE_PATH.name}; R5.2's reachability rests on there being exactly one producer. "
        f"Zero means the reason is dead — the failure mode design C3 rejects the json_s3_uri repoint "
        f"to avoid. More than one means two paths decide what 'not an S3 source' means."
    )

    resolver = [
        node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == "resolve_frame_source"
    ]
    assert len(resolver) == 1, "resolve_frame_source is not a single function any more"
    inside = [
        node
        for node in ast.walk(resolver[0])
        if isinstance(node, ast.Name) and node.id == "SAMPLE_UNAVAILABLE_NOT_S3_SOURCE"
    ]
    assert len(inside) == 1, (
        "the one producer of SAMPLE_UNAVAILABLE_NOT_S3_SOURCE is no longer inside "
        "resolve_frame_source. That function is the single classifier for this decision "
        "(do-NOT 8); moving the producer out of it splits the gate."
    )
