# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the JDBC source path in the ``schema_extract`` Lambda
(Tasks 7.1 / 7.2 / 7.3).

Covers:
- Happy path: crawler started, polled to READY+SUCCEEDED, single matching table
  produced, columns converted to JSON Schema with correct Glue→JSON type mapping,
  and the result stored via ``store_schema_in_dynamodb``.
- Zero-match: crawler completes but ``GetTables`` returns 0 tables → the guard's
  ``ValueError`` surfaces as a permanent ``StepFailure`` (P28 R1.1 decorator).
- Multi-match: ``GetTables`` returns >1 tables → permanent ``StepFailure``
  naming the tables.
- Crawler timeout: ``GetCrawler`` stays ``RUNNING`` past the polling deadline →
  ``TransientStepFailure`` (retryable — a retried task polls a crawl that has
  likely finished by then).
- Concurrent crawler run: ``StartCrawler`` raises ``CrawlerRunningException``;
  the function catches it and falls through to the polling loop, completing
  normally once the in-flight run finishes.

Validates: Requirement 22.1 (test coverage) and 13.3 (JDBC schema_extract path).
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Shared fixture: import the schema_extract Lambda with mocked AWS ────


@pytest.fixture()
def lambda_env():
    """Import schema_extract ``lambda_function`` with fully mocked AWS clients.

    Mirrors the importlib-spec pattern used by ``file_validation``'s
    ``test_jdbc_path.py`` (Task 6.4) so the two JDBC-path test suites stay in
    lock-step. Yields ``(lf, mock_s3, mock_glue, mock_table)``.

    The fixture also pre-wires ``glue_client.exceptions.CrawlerRunningException``
    / ``EntityNotFoundException`` to real ``Exception`` subclasses so the
    production code's ``except`` clauses can match raised side-effects.
    """
    lambda_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "lambda",
            "agentic_data_ingestion_schema_extract",
        )
    )
    lambda_path = os.path.join(lambda_dir, "lambda_function.py")
    shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

    os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
    os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")

    # boto3 client/resource fakes — keyed by service name so module-level
    # ``boto3.client("s3")`` and ``boto3.client("glue")`` resolve to distinct
    # mocks the tests can program independently.
    mock_s3 = MagicMock(name="s3_client")
    mock_glue = MagicMock(name="glue_client")
    mock_table = MagicMock(name="state_table")
    mock_dynamodb = MagicMock(name="dynamodb_resource")
    mock_dynamodb.Table.return_value = mock_table

    # Concrete exception types so ``except glue_client.exceptions.X`` matches.
    class _CrawlerRunningException(Exception):
        """Stand-in for ``boto3.client('glue').exceptions.CrawlerRunningException``."""

    class _EntityNotFoundException(Exception):
        """Stand-in for ``boto3.client('glue').exceptions.EntityNotFoundException``."""

    mock_glue.exceptions.CrawlerRunningException = _CrawlerRunningException
    mock_glue.exceptions.EntityNotFoundException = _EntityNotFoundException

    def _client_router(service_name, *args, **kwargs):
        if service_name == "glue":
            return mock_glue
        return mock_s3

    mock_boto3 = MagicMock(name="boto3_module")
    mock_boto3.client.side_effect = _client_router
    mock_boto3.resource.return_value = mock_dynamodb

    sys.path.insert(0, lambda_dir)
    sys.path.insert(0, shared_dir)

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        sys.modules.pop("lambda_function", None)

        spec = importlib.util.spec_from_file_location("lambda_function", lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

        # Defensive: ensure the module-level handles point at our mocks
        # regardless of how ``boto3.client`` was wired during import.
        lf.s3_client = mock_s3
        lf.glue_client = mock_glue
        lf.table = mock_table
        # The eventbridge publishers are tuple-unpacked at module load; rebind
        # to plain MagicMocks so tests can assert on (or ignore) their calls
        # without dragging in the real shared module.
        lf.publish_pipeline_state_change = MagicMock(name="publish_pipeline_state_change")

        yield lf, mock_s3, mock_glue, mock_table

    sys.modules.pop("lambda_function", None)
    if lambda_dir in sys.path:
        sys.path.remove(lambda_dir)
    if shared_dir in sys.path:
        sys.path.remove(shared_dir)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_jdbc_event(
    pipeline_id: str = "pid-abc",
    run_id: str = "run-123",
    execution_arn: str = "arn:aws:states:us-west-2:111111111111:execution:Pipeline:exec-1",
) -> dict:
    """Build the canonical JDBC schema-extract Step Functions event."""
    return {
        "source_format": "jdbc",
        "run_id": run_id,
        "original_pipeline_id": pipeline_id,
        "execution_arn": execution_arn,
    }


def _table_payload(name: str, columns: list[dict] | None = None) -> dict:
    """Build a Glue ``GetTables`` table entry with sensible defaults."""
    return {
        "Name": name,
        "DatabaseName": "agentic_data_ingestion_jdbc_discovery_local",
        "StorageDescriptor": {
            "Columns": columns
            or [
                {"Name": "id", "Type": "bigint"},
                {"Name": "name", "Type": "string"},
            ],
        },
        "PartitionKeys": [],
    }


def _crawler_resp(state: str, last_status: str | None) -> dict:
    """Build a ``GetCrawler`` response with the supplied state machine."""
    last_crawl: dict = {}
    if last_status is not None:
        last_crawl = {"Status": last_status}
    return {"Crawler": {"Name": "irrelevant", "State": state, "LastCrawl": last_crawl}}


# ── Happy path ──────────────────────────────────────────────────────────


class TestHandleJdbcSchemaExtractHappyPath:
    """Crawler starts, polls READY+SUCCEEDED on first GetCrawler, single
    matching table → schema converted and stored.

    Validates: Requirement 13.3.
    """

    def test_starts_crawler_with_correct_name(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1") as _store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        mock_glue.start_crawler.assert_called_once_with(Name="agentic-data-ingestion-pid-abc-source-discover")

    def test_polls_get_crawler(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1"),
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        assert mock_glue.get_crawler.call_count >= 1
        mock_glue.get_crawler.assert_called_with(Name="agentic-data-ingestion-pid-abc-source-discover")

    def test_calls_get_tables_with_discovery_db_and_table_prefix(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1"),
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        mock_glue.get_tables.assert_called_once_with(
            DatabaseName=lf.DISCOVERY_DB,
            Expression="agentic_data_ingestion_pid-abc_*",
        )

    def test_glue_type_mapping_in_stored_schema(self, lambda_env):
        """Verify the canonical Glue → JSON Schema mappings flow through to
        ``store_schema_in_dynamodb``: ``bigint`` → ``integer``, ``string``
        → ``string``, ``decimal(10,2)`` → ``number``, ``timestamp`` → ``string``.
        """
        lf, _s3, mock_glue, _table = lambda_env

        columns = [
            {"Name": "id", "Type": "bigint"},
            {"Name": "name", "Type": "string"},
            {"Name": "price", "Type": "decimal(10,2)"},
            {"Name": "created_at", "Type": "timestamp"},
        ]
        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {
            "TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders", columns)]
        }

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1") as store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        store.assert_called_once()
        schema = store.call_args.kwargs["schema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        assert props["id"] == {"type": "integer"}
        assert props["name"] == {"type": "string"}
        assert props["price"] == {"type": "number"}
        assert props["created_at"] == {"type": "string"}

    def test_response_shape(self, lambda_env):
        """Response envelope mirrors the S3 path so the state machine sees an
        identical shape regardless of source format."""
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-deadbeef"),
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            result = lf.lambda_handler(_make_jdbc_event(), MagicMock())

        assert result["statusCode"] == 200
        assert result["run_id"] == "run-123"
        assert result["original_pipeline_id"] == "pid-abc"
        assert result["schema_hash"] == "hash-deadbeef"
        assert result["json_s3_uri"] == "jdbc://postgres/public/orders"
        assert result["source_format"] == "jdbc"
        assert "extracted_schema" in result
        assert "message" in result

    def test_passes_execution_arn_through_to_store(self, lambda_env):
        """``execution_arn`` from the event lands on the DDB write call as
        ``execution_arn`` so the audit trail stays linked."""
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1") as store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            lf.lambda_handler(_make_jdbc_event(execution_arn="arn:aws:states:test"), MagicMock())

        assert store.call_args.kwargs["execution_arn"] == "arn:aws:states:test"
        assert store.call_args.kwargs["run_id"] == "run-123"
        assert store.call_args.kwargs["original_pipeline_id"] == "pid-abc"


# ── Zero-match guard (Task 7.3) ─────────────────────────────────────────


class TestZeroMatch:
    """Crawler reports SUCCEEDED but ``GetTables`` returns 0 tables → the
    guard's ``ValueError`` surfaces as a permanent ``StepFailure`` via the
    ``step_handler`` decorator (P28 R1.1).

    Validates: Requirement 13.3 and P28 Requirement 1.1.
    """

    def test_zero_match_raises_step_failure(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {"TableList": []}

        with (
            patch.object(lf, "store_schema_in_dynamodb") as store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
            pytest.raises(lf.StepFailure) as excinfo,
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        exc = excinfo.value
        assert exc.step_name == "schema_extract"
        assert exc.error_type == "permanent"
        assert "Crawler completed but produced no table" in exc.message
        assert isinstance(exc.__cause__, ValueError)
        # Schema is never stored when the guard fires.
        store.assert_not_called()


# ── Multi-match guard (Task 7.3) ────────────────────────────────────────


class TestMultiMatch:
    """``GetTables`` returns >1 tables → the guard's ``ValueError`` surfaces
    as a permanent ``StepFailure`` naming the offending tables (P28 R1.1).

    Validates: Requirement 13.3 and P28 Requirement 1.1.
    """

    def test_multi_match_raises_step_failure_with_table_names(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
        mock_glue.get_tables.return_value = {
            "TableList": [
                _table_payload("agentic_data_ingestion_pid-abc_orders"),
                _table_payload("agentic_data_ingestion_pid-abc_customers"),
            ]
        }

        with (
            patch.object(lf, "store_schema_in_dynamodb") as store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
            pytest.raises(lf.StepFailure) as excinfo,
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        exc = excinfo.value
        assert exc.step_name == "schema_extract"
        assert isinstance(exc.__cause__, ValueError)
        assert "Crawler produced unexpected tables:" in exc.message
        assert "agentic_data_ingestion_pid-abc_orders" in exc.message
        assert "agentic_data_ingestion_pid-abc_customers" in exc.message
        store.assert_not_called()


# ── Crawler timeout ─────────────────────────────────────────────────────


class TestCrawlerTimeout:
    """``GetCrawler`` stays ``RUNNING`` past the polling deadline →
    ``TransientStepFailure`` (P28 R1.1: retryable — a retried task polls a
    crawl that has likely finished) with the crawler name and timeout
    duration in the message.

    Validates: Requirement 13.3 and P28 Requirement 1.1.
    """

    def test_timeout_raises_transient_step_failure_after_deadline(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        mock_glue.start_crawler.return_value = {}
        # Crawler never finishes — every poll returns RUNNING with no LastCrawl.
        mock_glue.get_crawler.return_value = _crawler_resp("RUNNING", None)

        # The production path imports ``time as _time`` INSIDE the function,
        # so patching the global ``time.time`` is both unsafe (Powertools/MagicMock
        # use it) and unreliable (we'd race against pytest's own clock). Instead,
        # inject a fake ``time`` module into ``sys.modules`` for the duration of
        # the call so the in-function ``import time as _time`` resolves to our
        # fake. The fake exposes only ``time()`` and ``sleep()`` — the two
        # attributes the production code reads.
        #
        # Sequence of ``_time.time()`` calls inside ``handle_jdbc_schema_extract``:
        #   1) ``_metrics_start = _time.time()``                  (entry)
        #   2) ``deadline = _time.time() + 840``                  (pre-loop)
        #   3) ``if _time.time() >= deadline``                    (1st iteration)
        # Returning 0.0 for the first two and 900.0 (>= 840s deadline) on the
        # third triggers the TimeoutError branch. Subsequent calls (metrics in
        # the exception handler) keep returning 900.0 via ``itertools.repeat``.
        time_values = itertools.chain([0.0, 0.0], itertools.repeat(900.0))
        fake_time = MagicMock(name="fake_time_module")
        fake_time.time.side_effect = lambda: next(time_values)
        fake_time.sleep = lambda *_: None

        with (
            patch.object(lf, "store_schema_in_dynamodb") as store,
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch.dict(sys.modules, {"time": fake_time}),
            pytest.raises(lf.TransientStepFailure) as excinfo,
        ):
            lf.lambda_handler(_make_jdbc_event(), MagicMock())

        exc = excinfo.value
        assert exc.step_name == "schema_extract"
        assert exc.error_type == "transient"
        assert isinstance(exc.__cause__, TimeoutError)
        assert "agentic-data-ingestion-pid-abc-source-discover" in exc.message
        assert "14 minutes" in exc.message
        # No table lookup or schema store happens when the loop times out.
        mock_glue.get_tables.assert_not_called()
        store.assert_not_called()


# ── Concurrent crawler run ──────────────────────────────────────────────


class TestConcurrentCrawlerRun:
    """``StartCrawler`` raises ``CrawlerRunningException``; the function
    catches it and falls through to the polling loop, completing normally
    once the in-flight run finishes.

    Validates: Requirement 13.3.
    """

    def test_concurrent_run_completes_successfully(self, lambda_env):
        lf, _s3, mock_glue, _table = lambda_env

        # Simulate the in-flight run by raising CrawlerRunningException on
        # ``StartCrawler``. The function must NOT propagate this exception.
        mock_glue.start_crawler.side_effect = mock_glue.exceptions.CrawlerRunningException("already running")
        # First poll observes the in-flight run; second poll observes it
        # completing successfully — exercises the multi-iteration loop.
        mock_glue.get_crawler.side_effect = [
            _crawler_resp("RUNNING", None),
            _crawler_resp("READY", "SUCCEEDED"),
        ]
        mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

        with (
            patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1"),
            patch.object(
                lf,
                "get_previous_state",
                return_value={"original_s3_uri": "jdbc://postgres/public/orders"},
            ),
            patch("time.sleep", lambda *_: None),
        ):
            result = lf.lambda_handler(_make_jdbc_event(), MagicMock())

        # CrawlerRunningException was caught — start_crawler was attempted
        # exactly once but the polling loop ran to completion.
        assert mock_glue.start_crawler.call_count == 1
        assert mock_glue.get_crawler.call_count == 2
        assert result["statusCode"] == 200
        assert result["source_format"] == "jdbc"


# ── p53 task 3.1: the Sample_Artifact probe ─────────────────────────────
#
# The generated Glue job writes a bounded sample of the rows it has already
# read to one deterministic key (p53 task 2.1, the sample block in
# ``pyspark_templates/glue_job_jdbc_source.py.j2``). This Lambda probes that
# key with a HEAD and records the URI as a NEW attribute, ``sample_s3_uri``.
#
# Two things these tests exist to hold down:
#
# 1. ``json_s3_uri`` is not repointed (p53 do-NOT 11 / design C3). Every test
#    below that asserts a sample also asserts the ``jdbc://`` URI is still
#    exactly what it was, because three other readers take ``json_s3_uri`` to
#    mean the canonical source URI.
# 2. A probe miss or failure is "absent", never a step failure (design C4 /
#    the error-handling section). Run 1 of a JDBC pipeline has no artifact by
#    construction, so absence is the ordinary state.
#
# The existing tests above pass a bare ``MagicMock()`` context, whose
# ``invoked_function_arn`` is not a string, so the bucket never resolves and
# the probe is skipped entirely — which is why none of them changed.

_LAMBDA_ARN = "arn:aws:lambda:us-west-2:111111111111:function:agentic_data_ingestion_schema_extract"
_EXPECTED_BUCKET = "adi-processed-files-111111111111-us-west-2"
_EXPECTED_KEY = "samples/pid-abc/source-sample.json"
_EXPECTED_URI = f"s3://{_EXPECTED_BUCKET}/{_EXPECTED_KEY}"


def _lambda_context(arn: str = _LAMBDA_ARN) -> MagicMock:
    """A Lambda context whose ``invoked_function_arn`` is a real string."""
    return MagicMock(invoked_function_arn=arn)


def _client_error(code: str) -> Exception:
    """A botocore ``ClientError`` carrying the given S3 error code."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "probe"}}, "HeadObject")


#: The sentinel ``handle_jdbc_schema_extract`` reads off the file_validation row.
_SENTINEL_URI = "jdbc://postgres/public/orders"

#: The sentinel it falls back to when that row is missing entirely
#: (``lambda_function.py``'s ``or f"jdbc://pipeline/{pipeline_id}"``).
_FALLBACK_SENTINEL_URI = "jdbc://pipeline/pid-abc"

_UNSET = object()


def _run_jdbc(lf, mock_glue, context, store=None, previous_state=_UNSET):
    """Drive the JDBC happy path to completion and return the envelope.

    ``previous_state`` defaults to the file_validation row carrying
    ``_SENTINEL_URI``; pass ``None`` to exercise the missing-row fallback.
    """
    mock_glue.start_crawler.return_value = {}
    mock_glue.get_crawler.return_value = _crawler_resp("READY", "SUCCEEDED")
    mock_glue.get_tables.return_value = {"TableList": [_table_payload("agentic_data_ingestion_pid-abc_orders")]}

    if previous_state is _UNSET:
        previous_state = {"original_s3_uri": _SENTINEL_URI}

    store_patch = patch.object(lf, "store_schema_in_dynamodb", return_value="hash-1") if store is None else store
    with (
        store_patch as store_mock,
        patch.object(lf, "get_previous_state", return_value=previous_state),
        patch("time.sleep", lambda *_: None),
    ):
        result = lf.lambda_handler(_make_jdbc_event(), context)
    return result, store_mock


class TestSampleArtifactBucketResolution:
    """``_sample_artifact_bucket`` resolves the processed-files bucket without
    an API call, or returns ``""`` so the caller can treat the sample as absent.

    Validates: p53 Requirement 1.4.
    """

    def test_prefers_the_processed_bucket_env_var(self, lambda_env, monkeypatch):
        lf, _s3, _glue, _table = lambda_env
        monkeypatch.setenv("PROCESSED_BUCKET", "adi-processed-files-999999999999-eu-west-1")

        # The env var wins over the ARN derivation, so a deploy that sets it —
        # or a bucket rename — is picked up without a code change.
        assert lf._sample_artifact_bucket(_lambda_context()) == "adi-processed-files-999999999999-eu-west-1"

    def test_derives_account_and_region_from_the_invocation_arn(self, lambda_env, monkeypatch):
        lf, _s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        assert lf._sample_artifact_bucket(_lambda_context()) == _EXPECTED_BUCKET

    def test_returns_empty_when_the_context_carries_no_usable_arn(self, lambda_env, monkeypatch):
        lf, _s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        # A bare MagicMock (what every other test in this module passes) and a
        # truncated ARN both fail to resolve rather than producing a wrong name.
        assert lf._sample_artifact_bucket(MagicMock()) == ""
        assert lf._sample_artifact_bucket(_lambda_context("arn:aws:lambda")) == ""
        assert lf._sample_artifact_bucket(None) == ""


class TestProbeSampleArtifactUri:
    """The probe is a HEAD on the key the Glue job template writes, and every
    failure mode yields ``""`` rather than raising.

    Validates: p53 Requirements 1.4, 2.6.
    """

    def test_probes_exactly_the_key_the_template_writes(self, lambda_env, monkeypatch):
        """The key is a contract shared with
        ``pyspark_templates/glue_job_jdbc_source.py.j2``; if this assertion and
        that template disagree, the probe never finds the object."""
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        uri = lf._probe_sample_artifact_uri("pid-abc", _lambda_context())

        mock_s3.head_object.assert_called_once_with(Bucket=_EXPECTED_BUCKET, Key=_EXPECTED_KEY)
        assert uri == _EXPECTED_URI

    def test_a_head_is_used_not_a_get(self, lambda_env, monkeypatch):
        """The probe decides existence, not content — the consumers read the
        body themselves, and this Lambda must not pull a 1 MiB object to learn
        one bit."""
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        lf._probe_sample_artifact_uri("pid-abc", _lambda_context())

        assert mock_s3.head_object.call_count == 1
        mock_s3.get_object.assert_not_called()

    def test_a_missing_object_is_absent_not_an_error(self, lambda_env, monkeypatch):
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("404")

        assert lf._probe_sample_artifact_uri("pid-abc", _lambda_context()) == ""

    def test_a_403_is_also_absent(self, lambda_env, monkeypatch):
        """S3 answers HeadObject on a missing key with 403 rather than 404 when
        the caller lacks ListBucket, so both codes mean "nothing to hand a
        consumer"."""
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("403")

        assert lf._probe_sample_artifact_uri("pid-abc", _lambda_context()) == ""

    def test_an_unexpected_failure_is_swallowed_too(self, lambda_env, monkeypatch):
        """Throttling, a transient S3 error, anything: the probe returns "" and
        never raises. A telemetry probe must not fail a schema extraction that
        otherwise succeeded."""
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = RuntimeError("connection reset")

        assert lf._probe_sample_artifact_uri("pid-abc", _lambda_context()) == ""

    def test_no_call_is_made_when_the_bucket_cannot_be_resolved(self, lambda_env, monkeypatch):
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        assert lf._probe_sample_artifact_uri("pid-abc", MagicMock()) == ""
        mock_s3.head_object.assert_not_called()

    def test_no_call_is_made_without_a_pipeline_id(self, lambda_env, monkeypatch):
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        assert lf._probe_sample_artifact_uri("", _lambda_context()) == ""
        mock_s3.head_object.assert_not_called()

    def test_the_returned_uri_is_never_a_jdbc_uri(self, lambda_env, monkeypatch):
        """Every consumer gates on ``s3://``. The probe's contract is that it
        returns an ``s3://`` object URI or nothing at all — it never launders a
        sentinel into the new attribute."""
        lf, mock_s3, _glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        for side_effect in (None, _client_error("404"), RuntimeError("boom")):
            mock_s3.head_object.side_effect = side_effect
            uri = lf._probe_sample_artifact_uri("pid-abc", _lambda_context())
            assert uri == "" or uri.startswith("s3://")
            assert "jdbc://" not in uri


class TestStoreSchemaRecordsTheSampleUri:
    """``store_schema_in_dynamodb`` writes ``sample_s3_uri`` only when there is
    one, and never touches ``json_s3_uri``.

    Validates: p53 Requirements 2.6, 1.4.
    """

    def test_the_attribute_is_written_when_a_sample_exists(self, lambda_env):
        lf, _s3, _glue, mock_table = lambda_env

        lf.store_schema_in_dynamodb(
            run_id="run-123",
            original_pipeline_id="pid-abc",
            schema={"type": "object", "properties": {"id": {"type": "integer"}}},
            json_s3_uri="jdbc://postgres/public/orders",
            sample_s3_uri=_EXPECTED_URI,
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["sample_s3_uri"] == _EXPECTED_URI
        # do-NOT 11: the canonical URI is untouched, not replaced.
        assert item["json_s3_uri"] == "jdbc://postgres/public/orders"

    @pytest.mark.parametrize("absent", [None, ""])
    def test_the_attribute_is_omitted_when_there_is_no_sample(self, lambda_env, absent):
        """Absent, not empty-string, so a consumer's ``.get(...) or json_s3_uri``
        preference reads naturally and presence means "there is a sample"."""
        lf, _s3, _glue, mock_table = lambda_env

        lf.store_schema_in_dynamodb(
            run_id="run-123",
            original_pipeline_id="pid-abc",
            schema={"type": "object", "properties": {}},
            json_s3_uri="jdbc://pipeline/pid-abc",
            sample_s3_uri=absent,
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "sample_s3_uri" not in item
        assert item["json_s3_uri"] == "jdbc://pipeline/pid-abc"

    def test_the_legacy_positional_call_still_works(self, lambda_env):
        """The S3 path calls this positionally with five arguments
        (``lambda_function.py``'s legacy arm). The new parameter is optional and
        trailing, so that call is unaffected."""
        lf, _s3, _glue, mock_table = lambda_env

        lf.store_schema_in_dynamodb(
            "run-123",
            "pid-abc",
            {"type": "object", "properties": {}},
            "s3://adi-processed-files-111111111111-us-west-2/converted/x.json",
            "arn:aws:states:test",
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "sample_s3_uri" not in item
        assert item["execution_id"] == "arn:aws:states:test"


class TestJdbcArmRecordsTheSampleUri:
    """End to end through ``handle_jdbc_schema_extract``: the probe result
    reaches both the stored row and the Step Functions envelope, and the
    sentinel survives either way.

    Validates: p53 Requirements 2.6, 1.4.
    """

    def test_the_uri_reaches_the_store_call_and_the_envelope(self, lambda_env, monkeypatch):
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.return_value = {"ContentLength": 512}

        result, store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert store.call_args.kwargs["sample_s3_uri"] == _EXPECTED_URI
        assert result["sample_s3_uri"] == _EXPECTED_URI
        # The sentinel is unchanged in both places — do-NOT 11.
        assert store.call_args.kwargs["json_s3_uri"] == "jdbc://postgres/public/orders"
        assert result["json_s3_uri"] == "jdbc://postgres/public/orders"

    def test_the_envelope_carries_an_empty_string_when_absent(self, lambda_env, monkeypatch):
        """Always present, empty when there is no artifact. ``sample_s3_uri`` is
        the honest "is there a sample" signal and stays that; the field task 3.3's
        payload mapping actually reads is ``source_sample_uri``, which resolves the
        preference — see ``TestJdbcArmResolvesTheSourceSampleUri`` below."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("404")

        result, store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert result["sample_s3_uri"] == ""
        assert store.call_args.kwargs["sample_s3_uri"] == ""
        assert result["json_s3_uri"] == "jdbc://postgres/public/orders"

    def test_a_probe_failure_does_not_fail_the_step(self, lambda_env, monkeypatch):
        """The extraction still returns 200 with its schema. A sampling probe
        cannot cost a run whose crawl succeeded (R1.6's posture, applied to the
        read side)."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = RuntimeError("throttled")

        result, _store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert result["statusCode"] == 200
        assert result["sample_s3_uri"] == ""
        assert result["source_format"] == "jdbc"
        assert result["extracted_schema"]["properties"]

    def test_the_probe_targets_the_pipelines_own_key(self, lambda_env, monkeypatch):
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        _run_jdbc(lf, mock_glue, _lambda_context())

        mock_s3.head_object.assert_called_once_with(Bucket=_EXPECTED_BUCKET, Key=_EXPECTED_KEY)

    def test_no_source_database_read_is_added(self, lambda_env, monkeypatch):
        """C2 / do-NOT 6, 7: this Lambda gained a HEAD on S3 and nothing else.
        No Glue connection, no query, no second crawler run."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        _run_jdbc(lf, mock_glue, _lambda_context())

        mock_glue.get_connection.assert_not_called()
        mock_glue.start_crawler.assert_called_once()
        mock_s3.get_object.assert_not_called()
        mock_s3.select_object_content.assert_not_called()


# ── p53 task 3.3: the resolved ``source_sample_uri`` ─────────────────────
#
# Task 3.3 points the TestCode payload at
# ``$.schema_extract_result.source_sample_uri`` instead of ``json_s3_uri``.
# It cannot point at ``sample_s3_uri`` directly, because that key exists on
# the JDBC arm only and Step Functions raises States.ParameterPathFailure —
# which neither Retry nor Catch can intercept
# (``orchestration-stack.ts:338-340``) — for a ``$.schema_extract_result.…``
# reference whose path is absent. ASL has no coalesce or conditional
# intrinsic, so the preference is resolved in Python and returned on BOTH
# arms.
#
# The property these tests exist to pin is therefore *presence*, not value:
# ``source_sample_uri`` is in the envelope on every run, whatever the probe
# did. The RESUME_MARKER_PATH precedent
# (``orchestration-stack.ts:332-350``) is the same shape — a value echoed so
# it "defaults to '', never absent" and is "guaranteed-present by
# construction".


class TestJdbcArmResolvesTheSourceSampleUri:
    """The JDBC arm returns ``source_sample_uri`` on every run: the
    Sample_Artifact when one exists, ``json_s3_uri`` otherwise.

    Validates: p53 Requirements 2.4, 2.5, 2.6.
    """

    def test_a_present_sample_wins(self, lambda_env, monkeypatch):
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.return_value = {"ContentLength": 512}

        result, _store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert result["source_sample_uri"] == _EXPECTED_URI
        # R2.6 / do-NOT 11: the canonical URI is untouched, and the honest
        # "is there a sample" signal is untouched too.
        assert result["json_s3_uri"] == _SENTINEL_URI
        assert result["sample_s3_uri"] == _EXPECTED_URI

    def test_an_absent_sample_falls_back_to_the_sentinel(self, lambda_env, monkeypatch):
        """R2.5: with no artifact the reader is given exactly what it is given
        today. The fallback is a value, not an absent key."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("404")

        result, _store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert result["source_sample_uri"] == _SENTINEL_URI
        assert result["json_s3_uri"] == _SENTINEL_URI
        assert result["sample_s3_uri"] == ""

    def test_a_probe_failure_also_falls_back(self, lambda_env, monkeypatch):
        """Throttling, AccessDenied, anything: the resolved URI is still a
        usable value and the step still returns 200."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = RuntimeError("throttled")

        result, _store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert result["statusCode"] == 200
        assert result["source_sample_uri"] == _SENTINEL_URI
        assert result["json_s3_uri"] == _SENTINEL_URI

    def test_it_falls_back_to_the_synthetic_sentinel_when_file_validation_is_missing(self, lambda_env, monkeypatch):
        """The other shape ``json_s3_uri`` can take. Whichever sentinel this arm
        computes, ``source_sample_uri`` echoes it rather than going absent."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("404")

        result, _store = _run_jdbc(lf, mock_glue, _lambda_context(), previous_state=None)

        assert result["json_s3_uri"] == _FALLBACK_SENTINEL_URI
        assert result["source_sample_uri"] == _FALLBACK_SENTINEL_URI

    @pytest.mark.parametrize(
        ("probe", "context_factory"),
        [
            pytest.param({"ContentLength": 1}, _lambda_context, id="sample-present"),
            pytest.param(_client_error("404"), _lambda_context, id="sample-missing"),
            pytest.param(_client_error("403"), _lambda_context, id="probe-forbidden"),
            pytest.param(RuntimeError("boom"), _lambda_context, id="probe-raised"),
            pytest.param(None, MagicMock, id="bucket-unresolvable"),
        ],
    )
    def test_the_key_is_never_absent_and_never_empty(self, lambda_env, monkeypatch, probe, context_factory):
        """The whole reason the key exists. A ``$.schema_extract_result.…`` path
        that is absent on *any* run raises States.ParameterPathFailure at state
        entry, and no Retry or Catch can intercept it — so absence on one probe
        outcome is as fatal as absence on all of them."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        if isinstance(probe, Exception):
            mock_s3.head_object.side_effect = probe
        else:
            mock_s3.head_object.return_value = probe

        result, _store = _run_jdbc(lf, mock_glue, context_factory())

        assert "source_sample_uri" in result
        assert result["source_sample_uri"]
        # It is always a URI a consumer can act on: either the sample or the
        # sentinel, never a half-resolved value.
        assert result["source_sample_uri"] in (_EXPECTED_URI, _SENTINEL_URI)
        # R2.6 holds on every one of these paths.
        assert result["json_s3_uri"] == _SENTINEL_URI

    def test_the_resolution_adds_no_aws_call(self, lambda_env, monkeypatch):
        """C2 / do-NOT 6, 7: resolving the preference is one ``or`` in Python.
        The HEAD probe is the only S3 call this arm makes, as before."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

        _run_jdbc(lf, mock_glue, _lambda_context())

        assert mock_s3.head_object.call_count == 1
        mock_s3.get_object.assert_not_called()
        mock_glue.get_connection.assert_not_called()

    def test_the_stored_row_is_unaffected_by_the_resolution(self, lambda_env, monkeypatch):
        """``source_sample_uri`` is an envelope-only key. The DynamoDB row keeps
        the ``sample_s3_uri``-present-only-when-there-is-one shape task 3.1
        established, because the row's consumers do their own preference."""
        lf, mock_s3, mock_glue, _table = lambda_env
        monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
        mock_s3.head_object.side_effect = _client_error("404")

        _result, store = _run_jdbc(lf, mock_glue, _lambda_context())

        assert "source_sample_uri" not in store.call_args.kwargs
        assert store.call_args.kwargs["sample_s3_uri"] == ""
        assert store.call_args.kwargs["json_s3_uri"] == _SENTINEL_URI
