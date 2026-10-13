# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the URI preference is a preference, not a replacement.

p53 task 4.1, **Property 3** — the *producer* side. ``schema_extract`` is where
the preference is resolved in Python and handed to the state machine as
``source_sample_uri``, on **both** arms.

**Validates: Requirements 2.4, 2.5, 2.6**

WHY THE PRODUCER NEEDS ITS OWN PROPERTY, AND WHY IT COVERS BOTH ARMS
--------------------------------------------------------------------
The three consumer sites do their own preference off the DynamoDB row, and
``tests/unit/lambdas/create_code/test_sample_uri_preference_prop.py`` and
``.../schema_mapping/test_sample_uri_preference_prop.py`` cover those. Consumer
3 — the TestCode payload at ``orchestration-stack.ts:594`` — **cannot**: Amazon
States Language has no coalesce and no conditional intrinsic, so task 3.3 could
not express a preference there. The preference is resolved here instead and
echoed as a new envelope key (design C3).

That makes one claim load-bearing in a way no consumer-side test can reach:
``source_sample_uri`` must be **present on every run of both arms**. A
``$.schema_extract_result.…`` reference to a missing path raises
``States.ParameterPathFailure``, which ``orchestration-stack.ts:338-340`` records
as interceptable by neither Retry nor Catch — so an absent key is not a degraded
read, it is an uncatchable run failure on every S3-upload run. This is the
``RESUME_MARKER_PATH`` pattern (``orchestration-stack.ts:332-350``): a value
echoed so it "defaults to '', never absent" and is therefore
"guaranteed-present by construction".

So both arms are driven here, generatively:

* the **JDBC arm** (``handle_jdbc_schema_extract``, ``:1564``) across every probe
  outcome — found, missing, forbidden, and an unclassifiable failure;
* the **S3 arm** (``lambda_handler``'s streaming path, ``:2037``) where
  ``source_sample_uri`` is ``canonical_uri``, byte-identical to the
  ``json_s3_uri`` that arm already returned before p53.

WHAT THE FOUR EXAMPLE TESTS TASK 3.1/3.3 WROTE DO NOT COVER
-----------------------------------------------------------
``test_jdbc_path.py:709-976`` pins the shapes with one fixed URI pair, one fixed
account and one fixed region, and ``test_streaming_schema.py:2789-2810`` pins the
S3 arm with one fixed prefix. Two claims are therefore assumed rather than tested
there, and both are generated here:

* **The envelope value is derived, not constant.** Generated accounts, regions,
  pipeline ids, engines, schemas and table names mean no assertion can be
  satisfied by a hard-coded string — including the deterministic bucket name
  ``adi-processed-files-{account}-{region}`` the probe builds at ``:1513``.
* **The two arms never disagree about presence.** The arms are deliberately NOT
  key-identical any more — the JDBC arm carries ``sample_s3_uri`` and the S3 arm
  does not (``:1798-1806`` records that the earlier "byte-identical shape" claim
  stopped being true at task 3.1) — so "every key any ASL path reads exists in
  both arms" is now a claim about a *subset*, and ``source_sample_uri`` is the
  key in question.

do-NOT 11 AT THE WRITER
-----------------------
``json_s3_uri`` is not repointed. On the JDBC arm it stays the sentinel derived
from ``original_s3_uri`` whatever the probe found; on the S3 arm it stays
``canonical_uri``. Asserted on both, in the envelope and on the stored row,
because the writer is the one place a repoint would be a single-line change.

MODULE LOADING — a private copy, never published to sys.modules
---------------------------------------------------------------
``lambda_function.py`` is a name 23 Lambda directories share.
``LGR-c58f276-4`` (**template / ratified**) records the defect class that causes
and ratifies the remedy used here: load the file with
``importlib.util.spec_from_file_location`` under a private alias that is never
published, then assert at import time that what was bound is neither a mock nor
vacuous. This departs from the sibling ``lambda_env`` fixture
(``test_jdbc_path.py:35``), which publishes ``sys.modules["lambda_function"]``
and pops it on teardown — safe for its example tests, but a property test holding
the module across ~100 examples is exactly the shape that record is about.

``boto3`` is stubbed for the duration of the *load* only, so the module-level
``boto3.client("s3")`` / ``("glue")`` / ``resource("dynamodb")`` resolve to
programmable fakes. No ``moto.mock_aws()`` and no real client is created — this
file is not on ``test_no_duplicate_mocking_prop.py``'s allowlist and does not need
to be.

Nothing in this file changes production code and nothing in it amends a
pre-existing assertion.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_SHARED = os.path.join(_REPO, "shared")
_SCHEMA_EXTRACT = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_extract")

# ---------------------------------------------------------------------------
# Private module loading (see the docstring's "MODULE LOADING" section)
# ---------------------------------------------------------------------------

_MISSING = object()


class _ScopedStubs:
    """Insert stub modules for a load, then remove exactly what was inserted."""

    def __init__(self, stubs: dict[str, Any]) -> None:
        self._stubs = stubs
        self._prior: dict[str, Any] = {}

    def __enter__(self) -> _ScopedStubs:
        for name, module in self._stubs.items():
            self._prior[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc: object) -> bool:
        for name, stub in self._stubs.items():
            prior = self._prior[name]
            if prior is _MISSING:
                if sys.modules.get(name) is stub:
                    del sys.modules[name]
            else:
                sys.modules[name] = prior
        return False


class _CrawlerRunningException(Exception):
    """Stand-in for ``boto3.client('glue').exceptions.CrawlerRunningException``."""


class _EntityNotFoundException(Exception):
    """Stand-in for ``boto3.client('glue').exceptions.EntityNotFoundException``."""


def _load_schema_extract():
    """Exec ``schema_extract/lambda_function.py`` under a private alias.

    Returns ``(module, s3, glue, table)``. ``boto3`` is stubbed for the load so
    the module-level clients are the returned fakes; the handles are rebound
    afterwards regardless of how ``boto3.client`` was wired, which is the same
    defensive step ``test_jdbc_path.py:104-107`` takes.
    """
    s3 = MagicMock(name="s3_client")
    glue = MagicMock(name="glue_client")
    glue.exceptions.CrawlerRunningException = _CrawlerRunningException
    glue.exceptions.EntityNotFoundException = _EntityNotFoundException

    state_table = MagicMock(name="state_table")
    dynamodb = MagicMock(name="dynamodb_resource")
    dynamodb.Table.return_value = state_table

    boto3_stub = MagicMock(name="boto3_module")
    boto3_stub.client.side_effect = lambda service, *a, **kw: glue if service == "glue" else s3
    boto3_stub.resource.return_value = dynamodb

    added = [p for p in (_SHARED, _SCHEMA_EXTRACT) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(
            "_p53_4_1_schema_extract_lambda_function", os.path.join(_SCHEMA_EXTRACT, "lambda_function.py")
        )
        assert spec is not None and spec.loader is not None, "cannot load schema_extract/lambda_function.py"
        module = importlib.util.module_from_spec(spec)
        with _ScopedStubs({"boto3": boto3_stub}):
            spec.loader.exec_module(module)
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)

    module.s3_client = s3
    module.glue_client = glue
    module.table = state_table
    module.publish_pipeline_state_change = MagicMock(name="publish_pipeline_state_change")
    return module, s3, glue, state_table


_MOD, _S3, _GLUE, _TABLE = _load_schema_extract()

# Import-time non-vacuity guards, per LGR-c58f276-4. `hasattr` alone is not a
# guard — a MagicMock has every attribute — so the loaded module is also asked
# questions with known answers. Bound to a mock, every property below would pass
# vacuously, which is the failure mode that record exists to prevent.
assert not isinstance(_MOD, MagicMock), "schema_extract resolved to a mock, not the real module"
for _attr in (
    "lambda_handler",
    "handle_jdbc_schema_extract",
    "store_schema_in_dynamodb",
    "_probe_sample_artifact_uri",
    "_sample_artifact_bucket",
    "_SAMPLE_ARTIFACT_KEY",
):
    assert hasattr(_MOD, _attr), f"the real schema_extract has no {_attr}; the private load was defeated"
assert _MOD._sample_artifact_bucket(None) == "" or os.environ.get("PROCESSED_BUCKET"), (
    "the loaded _sample_artifact_bucket does not decline an absent context; it is not the real one"
)
assert "{pipeline_id}" in _MOD._SAMPLE_ARTIFACT_KEY and _MOD._SAMPLE_ARTIFACT_KEY.endswith(".json"), (
    "the sample key template is not the shape task 2.1 writes and task 3.1 probes"
)


# ---------------------------------------------------------------------------
# Harness — the JDBC arm
# ---------------------------------------------------------------------------

RUN_ID = "run-p53-4-1"


def _lambda_context(account: str, region: str) -> MagicMock:
    """A Lambda context whose ``invoked_function_arn`` is a real string.

    ``_sample_artifact_bucket`` derives ``adi-processed-files-{account}-{region}``
    from segments 4 and 3 of this ARN (``:1508-1513``), which is why the account
    and region are generated: a hard-coded bucket name in an assertion would
    match a hard-coded bucket name in the code and prove nothing.
    """
    return MagicMock(
        invoked_function_arn=f"arn:aws:lambda:{region}:{account}:function:agentic_data_ingestion_schema_extract"
    )


def _client_error(code: str) -> Exception:
    """A botocore ``ClientError`` carrying the given S3 error code."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "probe"}}, "HeadObject")


def _glue_table(pipeline_id: str) -> dict:
    """The single matching ``GetTables`` entry the JDBC guard requires."""
    return {
        "Name": f"agentic_data_ingestion_{pipeline_id}_orders",
        "DatabaseName": f"agentic_data_ingestion_jdbc_discovery_{os.environ['AGENTIC_DATA_INGESTION_ENV']}",
        "StorageDescriptor": {"Columns": [{"Name": "id", "Type": "bigint"}, {"Name": "name", "Type": "string"}]},
        "PartitionKeys": [],
    }


def _run_jdbc_arm(pipeline_id: str, account: str, region: str, sentinel: str, probe_outcome: str) -> tuple[dict, dict]:
    """Drive the JDBC arm to completion; return ``(envelope, store_kwargs)``.

    ``probe_outcome`` programs ``head_object``:

    * ``found`` — the HEAD succeeds, so there IS a Sample_Artifact;
    * ``missing`` / ``forbidden`` — 404 / 403, both of which
      ``_probe_sample_artifact_uri:1550-1554`` classifies as absent (S3 answers
      403 rather than 404 on a missing key when the caller lacks ListBucket);
    * ``unclassifiable`` — a bare ``RuntimeError``, the WARNING arm.

    All three absent-ish outcomes are in the space because R2.6's guarantee has
    to hold for every one of them, not only the tidy 404.
    """
    _GLUE.reset_mock(side_effect=True)
    _S3.reset_mock(side_effect=True)
    _GLUE.exceptions.CrawlerRunningException = _CrawlerRunningException
    _GLUE.exceptions.EntityNotFoundException = _EntityNotFoundException

    _GLUE.start_crawler.return_value = {}
    _GLUE.get_crawler.return_value = {
        "Crawler": {"Name": "irrelevant", "State": "READY", "LastCrawl": {"Status": "SUCCEEDED"}}
    }
    _GLUE.get_tables.return_value = {"TableList": [_glue_table(pipeline_id)]}

    if probe_outcome == "found":
        _S3.head_object.side_effect = None
        _S3.head_object.return_value = {"ContentLength": 1234}
    elif probe_outcome == "missing":
        _S3.head_object.side_effect = _client_error("404")
    elif probe_outcome == "forbidden":
        _S3.head_object.side_effect = _client_error("403")
    else:
        _S3.head_object.side_effect = RuntimeError("connection reset")

    event = {
        "source_format": "jdbc",
        "run_id": RUN_ID,
        "original_pipeline_id": pipeline_id,
        "execution_arn": f"arn:aws:states:{region}:{account}:execution:Pipeline:exec-1",
    }
    with (
        patch.dict(os.environ, {}, clear=False),
        patch.object(_MOD, "store_schema_in_dynamodb", return_value="hash-1") as store,
        patch.object(_MOD, "get_previous_state", return_value={"original_s3_uri": sentinel}),
        patch("time.sleep", lambda *_: None),
    ):
        os.environ.pop("PROCESSED_BUCKET", None)
        envelope = _MOD.lambda_handler(event, _lambda_context(account, region))

    assert envelope["statusCode"] == 200, f"the JDBC arm did not complete: {envelope}"
    assert store.called, "store_schema_in_dynamodb was never reached; the row assertions would be vacuous"
    return envelope, store.call_args.kwargs


# ---------------------------------------------------------------------------
# Harness — the S3 arm
# ---------------------------------------------------------------------------


def _run_s3_arm(pipeline_id: str, source_uri: str) -> tuple[dict, tuple]:
    """Drive the streaming (S3-upload) arm; return ``(envelope, store_args)``.

    The schema extractor itself is patched — this property is about the URI keys
    in the envelope, not about CSV inference, which
    ``test_streaming_schema.py`` owns. ``store_schema_in_dynamodb`` is called
    positionally on this arm (``:2056``), unlike the JDBC arm's keyword call
    (``:1757-1762``), so the two harnesses read it differently on purpose.
    """
    _S3.reset_mock(side_effect=True)

    event = {"source_format": "csv", "run_id": RUN_ID, "original_pipeline_id": pipeline_id}
    with (
        patch.object(_MOD, "store_schema_in_dynamodb", return_value="hash-1") as store,
        patch.object(
            _MOD,
            "get_previous_state",
            return_value={"original_pipeline_id": pipeline_id, "original_s3_uri": source_uri},
        ),
        patch.object(_MOD, "extract_schema_from_csv", return_value={"properties": {"id": {"type": "integer"}}}),
    ):
        envelope = _MOD.lambda_handler(event, MagicMock())

    assert envelope["statusCode"] == 200, f"the S3 arm did not complete: {envelope}"
    assert store.called, "store_schema_in_dynamodb was never reached; the row assertions would be vacuous"
    return envelope, store.call_args.args


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#
# Everything the two URIs are built from is generated, because the deterministic
# bucket name and key are *derived* at `:1513` and `:1542` — a fixed account and
# region in the assertion would match a fixed account and region in the code and
# establish nothing about the derivation.

_SLUG = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=18).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
)
_IDENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=2, max_size=12)
_ACCOUNT = st.text(alphabet="0123456789", min_size=12, max_size=12)
_REGION = st.sampled_from(["us-west-2", "us-east-1", "eu-west-1", "ap-southeast-2"])
_ENGINE = st.sampled_from(["postgres", "mysql", "oracle", "sqlserver"])

#: `found` plus the three distinct ways the probe can conclude "absent".
_PROBE_OUTCOMES = st.sampled_from(["found", "missing", "forbidden", "unclassifiable"])


@st.composite
def _jdbc_sentinels(draw) -> str:
    """``json_s3_uri``'s value on the JDBC arm — both forms it really takes.

    ``file_validation`` writes ``original_s3_uri = jdbc://{engine}/{schema}/{table}``
    for a JDBC source, so the sentinel usually carries engine, schema and table;
    it degrades to the bare ``jdbc://pipeline/{id}`` form only when that row is
    missing (``:1735``). Both are generated because do-NOT 11 is a claim about
    this value surviving *unchanged*, which a single fixed sentinel cannot test.
    """
    if draw(st.booleans()):
        return f"jdbc://{draw(_ENGINE)}/{draw(_IDENT)}/{draw(_IDENT)}"
    return f"jdbc://pipeline/{draw(_SLUG)}"


@st.composite
def _uploaded_source_uris(draw) -> str:
    """An S3-upload pipeline's ``original_s3_uri`` — a single object, not a prefix.

    Prefix mode (``:1958``) is deliberately out of the space: it takes a
    ``ListObjectsV2`` round trip and changes which object the *schema* is read
    from, neither of which affects the URI-key claims here.
    ``test_streaming_schema.py:2789`` covers the prefix case by example.
    """
    return f"s3://adi-source-files-{draw(_ACCOUNT)}/uploads/{draw(_SLUG)}.csv"


# ---------------------------------------------------------------------------
# Property 3 — the JDBC arm
# ---------------------------------------------------------------------------
#
# Feature: p53-jdbc-source-sampling, Property 3: The URI preference is a
# preference, not a replacement.


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pipeline_id=_SLUG,
    account=_ACCOUNT,
    region=_REGION,
    sentinel=_jdbc_sentinels(),
    probe_outcome=_PROBE_OUTCOMES,
)
def test_the_jdbc_arm_resolves_the_preference_and_leaves_json_s3_uri_alone(
    pipeline_id, account, region, sentinel, probe_outcome
):
    """Property 3 at the writer, across every probe outcome.

    Three claims off one run:

    1. ``source_sample_uri == sample_s3_uri or json_s3_uri`` (R2.4, R2.5), with
       the expectation derived from the probe outcome rather than from the code.
    2. ``source_sample_uri`` is present and non-empty on **every** run,
       whichever way the probe concluded (R2.6). An absent path here is an
       uncatchable ``States.ParameterPathFailure`` downstream, not a degraded
       read.
    3. ``json_s3_uri`` is exactly the sentinel, in the envelope and on the
       stored row, whether or not a sample was found (do-NOT 11, design C3).

    **Validates: Requirements 2.4, 2.5, 2.6**
    """
    envelope, store_kwargs = _run_jdbc_arm(pipeline_id, account, region, sentinel, probe_outcome)

    expected_sample = (
        f"s3://adi-processed-files-{account}-{region}/samples/{pipeline_id}/source-sample.json"
        if probe_outcome == "found"
        else ""
    )

    assert envelope["sample_s3_uri"] == expected_sample
    assert envelope["source_sample_uri"] == (expected_sample or sentinel)

    # R2.6's presence guarantee — the reason the preference is resolved here at
    # all rather than in the payload mapping.
    assert "source_sample_uri" in envelope
    assert envelope["source_sample_uri"], "an empty source_sample_uri would leave consumer 3 with nothing to read"

    # do-NOT 11, both places it could be broken.
    assert envelope["json_s3_uri"] == sentinel
    assert store_kwargs["json_s3_uri"] == sentinel

    # The stored row keeps the "present only when there is one" shape task 3.1
    # established; `source_sample_uri` is envelope-only, because the row's
    # consumers do their own preference.
    assert store_kwargs["sample_s3_uri"] == expected_sample
    assert "source_sample_uri" not in store_kwargs


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pipeline_id=_SLUG, account=_ACCOUNT, region=_REGION, sentinel=_jdbc_sentinels())
def test_every_absent_shaped_probe_outcome_resolves_the_same_way(pipeline_id, account, region, sentinel):
    """404, 403 and an unclassifiable failure are one case, not three.

    ``_probe_sample_artifact_uri`` splits them only by log level — INFO for the
    two expected codes, WARNING for anything else (``:1550-1556``) — and the
    resolved URI must be identical for all three. A probe that leaked a partial
    URI, or raised, on the WARNING arm would pass the 404-only example test at
    ``test_jdbc_path.py:882`` and fail here.

    **Validates: Requirements 2.5, 2.6**
    """
    resolved = {
        outcome: _run_jdbc_arm(pipeline_id, account, region, sentinel, outcome)[0]
        for outcome in ("missing", "forbidden", "unclassifiable")
    }

    for outcome, envelope in resolved.items():
        assert envelope["sample_s3_uri"] == "", f"{outcome} produced a sample URI"
        assert envelope["source_sample_uri"] == sentinel, f"{outcome} did not fall back to json_s3_uri"
        assert envelope["json_s3_uri"] == sentinel, f"{outcome} disturbed json_s3_uri"


# ---------------------------------------------------------------------------
# R2.6 — the envelope guarantee, on BOTH arms
# ---------------------------------------------------------------------------


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pipeline_id=_SLUG, source_uri=_uploaded_source_uris())
def test_the_s3_arm_always_carries_source_sample_uri_and_never_changes_its_value(pipeline_id, source_uri):
    """The S3 arm echoes ``canonical_uri`` and changes no value (R2.6).

    Task 3.3 repoints the TestCode payload from
    ``$.schema_extract_result.json_s3_uri`` to ``source_sample_uri``. On this arm
    that must be a change of *path* and not of *value*, because the source-files
    grant on ``verificationSessionRole`` (``foundation-stack.ts:835-887``) is what
    covers what the payload names. Asserted as byte equality with ``json_s3_uri``
    rather than as "looks like an s3:// URI".

    The arm also carries **no** ``sample_s3_uri`` — there is no Sample_Artifact on
    this path to probe for. That asymmetry is the whole reason the state machine
    reads ``source_sample_uri`` and not ``sample_s3_uri``
    (``:1798-1806``).

    **Validates: Requirements 2.6**
    """
    envelope, store_args = _run_s3_arm(pipeline_id, source_uri)

    assert envelope["source_sample_uri"] == source_uri
    assert envelope["json_s3_uri"] == source_uri
    assert envelope["source_sample_uri"] == envelope["json_s3_uri"]
    assert "sample_s3_uri" not in envelope

    # `store_schema_in_dynamodb(run_id, original_pipeline_id, schema, canonical_uri, ...)`
    # — the fourth positional is `json_s3_uri`, and it is the same value.
    assert store_args[3] == source_uri


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pipeline_id=_SLUG,
    account=_ACCOUNT,
    region=_REGION,
    sentinel=_jdbc_sentinels(),
    probe_outcome=_PROBE_OUTCOMES,
    source_uri=_uploaded_source_uris(),
)
def test_source_sample_uri_is_present_and_non_empty_on_every_run_of_both_arms(
    pipeline_id, account, region, sentinel, probe_outcome, source_uri
):
    """R2.6's presence guarantee, stated over the pair rather than per arm.

    The arms are no longer key-identical — the JDBC arm carries
    ``sample_s3_uri`` and the S3 arm does not (``:1798-1806``) — so what has to
    hold is the narrower claim: every key any ASL path reads off
    ``$.schema_extract_result`` exists in **both**. ``source_sample_uri`` is that
    key, and this is the property that would fail if a third arm, or a future
    early return, forgot it.

    **Validates: Requirements 2.6**
    """
    jdbc_envelope, _ = _run_jdbc_arm(pipeline_id, account, region, sentinel, probe_outcome)
    s3_envelope, _ = _run_s3_arm(pipeline_id, source_uri)

    for arm, envelope in (("jdbc", jdbc_envelope), ("s3", s3_envelope)):
        assert "source_sample_uri" in envelope, f"the {arm} arm omitted source_sample_uri"
        assert isinstance(envelope["source_sample_uri"], str), f"the {arm} arm's source_sample_uri is not a string"
        assert envelope["source_sample_uri"], f"the {arm} arm's source_sample_uri is empty"

    # The keys the state machine reads on both arms, as a set relation rather
    # than a list — the claim is containment, not equality.
    shared_keys = {"statusCode", "run_id", "original_pipeline_id", "schema_hash", "json_s3_uri", "source_sample_uri"}
    assert shared_keys <= set(jdbc_envelope)
    assert shared_keys <= set(s3_envelope)
