# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``shared/target_schema_resolution.resolve_target_schema`` (p33 task 4.1).

The module under test is the one definition of "read the target schema off the
pipeline record". These tests pin the four branches that make it a *compliance*
resolver rather than a best-effort read:

1. the pair present  → ``(normalized_schema, sha256_of_raw_bytes)``
2. a key with no usable bucket → raises, naming BOTH attributes (requirement 2.7,
   C1 / ``LGR-7c9378d-1``, template/ratified)
3. no key at all → ``(None, None)`` — the ONLY input that returns the not-found
   value (requirement 2.6)
4. a key that names a missing object → **raises** (design D14 / C16), because
   the pair is written together, so a dangling key is an inconsistent record

plus read-error classification through ``raise_for_read_client_error``
(requirement 2.8, C2) and sha256 hashing over the raw bytes (C10 /
``LGR-dd80c6d-1``).

WHY CASE 4 IS THE ONE A LATER READER IS MOST LIKELY TO "SIMPLIFY"
----------------------------------------------------------------
``schema_compare.get_target_schema_from_s3`` (``lambda_function.py:369-385``)
returns ``(None, None)`` for the identical input and says so in its docstring,
and ``NoSuchKey`` sits in ``NOT_FOUND_CLIENT_ERROR_CODES``
(``shared/step_errors.py:54-62``) where the classifier returns normally for it.
Both facts make "return ``(None, None)`` here too" look like a tidy-up. It is
not: ``schema_compare`` asks "did the schema change since baseline", where a
missing object is answered by the observed-version report; this module asks
"what am I measuring compliance against", which has no honest answer when the
named object is gone. The divergence is deliberate, ``schema_compare`` is not
modified by p33 (design D5), and ``test_no_such_key_raises_rather_than_returning_the_not_found_pair``
below is the fence. Do not relax it without retiring D14.

MODULE-LOADING NOTE (a hazard this file deliberately does not have)
------------------------------------------------------------------
``patch.dict("sys.modules", ...)`` restores the dict on exit, dropping any
module first imported inside the block; re-importing ``step_errors`` afterwards
can then yield a *second* module object whose ``StepFailure`` is a different
class, at which point ``pytest.raises(StepFailure)`` silently stops matching.
This file has no stubbing at all — ``shared/`` imports cleanly — so both the
module and the tests bind one ``step_errors``. ``test_the_module_raises_the_same_StepFailure_class_these_tests_assert_on``
guards that empirically rather than by assumption.

**Validates: Requirements 2.1, 2.7, 2.8, 3.7**
"""

import hashlib
import inspect
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import target_schema_resolution as tsr
from step_errors import StepFailure, TransientStepFailure

STEP_NAME = "test_code"
PIPELINE_ID = "pipe-p33-target-schema"
TARGET_BUCKET = "agentic-data-ingestion-target-schemas"
TARGET_KEY = f"{PIPELINE_ID}/uploaded_schema_1785844048926.json"

#: A JSON-Schema payload — the shape the wizard upload produces.
SCHEMA_BYTES = json.dumps(
    {"type": "object", "properties": {"id": {"type": "integer"}, "email": {"type": "string"}}}
).encode("utf-8")

#: The same information in the custom ``fields``-array form, which the resolver
#: has to normalise so its callers see one shape (requirement 3.7).
FIELDS_FORM_BYTES = json.dumps(
    {
        "fields": [
            {"name": "customer_id", "type": "int", "required": True},
            {"name": "signup_date", "type": "date"},
        ]
    }
).encode("utf-8")

#: The pipeline record as ``create_pipeline`` writes it for an uploaded target
#: schema: the pair together, plus the by-name attributes a keyless pipeline
#: would be resolved by elsewhere (``pipelines_crud.py:761-767``).
DIRECT_PATH_RECORD = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "testing",
    "target_schema_name": "uploaded_schema_1785844048926",
    "target_schema_bucket": TARGET_BUCKET,
    "target_schema_s3_key": TARGET_KEY,
    "target_type": "New S3 Bucket",
}


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Drop the module's cached S3 client around every test.

    The cache is process-wide by design (it is a Lambda warm-start
    optimisation), so a client built inside one test's moto context would
    otherwise leak into the next test and talk to nothing.
    """
    tsr._s3_client_cache = None
    yield
    tsr._s3_client_cache = None


def _put(body: bytes, *, bucket: str = TARGET_BUCKET, key: str = TARGET_KEY) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=bucket, Key=key, Body=body)


def _s3_raising(code: str) -> MagicMock:
    """A client whose ``get_object`` raises one ClientError code.

    Error injection is the only part of these tests that does not go through
    moto: moto serves real not-found semantics but cannot be made to throttle.
    Same shape as the ratified precedent at
    ``tests/unit/lambdas/schema_compare/test_read_path_errors.py:100-116``.
    """
    client = MagicMock()
    client.get_object.side_effect = ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetObject")
    return client


class TestThePairIsResolvedTogether:
    """Requirement 2.1 / 3.7 / C1: bucket and key from the same record, no bucket parameter."""

    def test_the_signature_accepts_no_bucket(self):
        """C1 (``LGR-7c9378d-1``, template/ratified) made structural.

        The six-month divergence this module exists to prevent was possible only
        because a caller supplied the bucket. There is no parameter to supply.
        """
        params = list(inspect.signature(tsr.resolve_target_schema).parameters)
        assert params == ["pipeline_metadata", "step_name"]

    def test_pair_present_returns_the_schema_and_a_hash(self, mock_s3_buckets):
        _put(SCHEMA_BYTES)

        schema, schema_hash = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert schema == json.loads(SCHEMA_BYTES)
        assert isinstance(schema_hash, str) and len(schema_hash) == 64

    def test_hash_is_the_sha256_of_the_raw_object_bytes(self, mock_s3_buckets):
        """C10 / ``LGR-dd80c6d-1``: sha256 over the bytes, never the builtin ``hash()``."""
        _put(SCHEMA_BYTES)

        _, schema_hash = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert schema_hash == hashlib.sha256(SCHEMA_BYTES).hexdigest()

    def test_hash_is_stable_across_reads_and_moves_with_content(self, mock_s3_buckets):
        """Cross-invocation stability is what mapping reuse and drift detection rest on."""
        _put(SCHEMA_BYTES)
        first = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)[1]
        tsr._s3_client_cache = None
        second = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)[1]

        _put(FIELDS_FORM_BYTES)
        other = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)[1]

        assert first == second
        assert first != other

    def test_the_fields_array_form_is_normalized(self, mock_s3_buckets):
        """Requirement 3.7's return contract is a *normalized* schema."""
        _put(FIELDS_FORM_BYTES)

        schema, _ = tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert set(schema["properties"]) == {"customer_id", "signup_date"}
        assert schema["properties"]["customer_id"]["type"] == "integer"
        assert schema["properties"]["signup_date"]["type"] == "string"
        assert schema["required"] == ["customer_id"]


class TestNoKeyIsTheOnlyNotFoundValue:
    """Requirement 2.6: a pipeline that never had a target schema, and nothing else."""

    def test_key_absent_returns_the_none_pair_without_reading_s3(self):
        record = {k: v for k, v in DIRECT_PATH_RECORD.items() if k != "target_schema_s3_key"}
        client = MagicMock()

        with patch.object(tsr, "_s3_client", lambda: client):
            assert tsr.resolve_target_schema(record, STEP_NAME) == (None, None)

        client.get_object.assert_not_called()

    def test_a_null_key_attribute_is_absent(self):
        """DynamoDB can hold an explicit NULL; it carries no address either."""
        record = {**DIRECT_PATH_RECORD, "target_schema_s3_key": None}

        with patch.object(tsr, "_s3_client", MagicMock()):
            assert tsr.resolve_target_schema(record, STEP_NAME) == (None, None)


class TestHalfWrittenRecordIsNotAnAbsentSchema:
    """Requirement 2.7 / C1: ``create_pipeline`` writes both attributes or neither."""

    def test_key_without_bucket_raises_naming_both_attributes(self):
        record = {k: v for k, v in DIRECT_PATH_RECORD.items() if k != "target_schema_bucket"}
        client = MagicMock()

        with patch.object(tsr, "_s3_client", lambda: client), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(record, STEP_NAME)

        message = str(exc_info.value)
        assert "target_schema_bucket" in message
        assert "target_schema_s3_key" in message
        assert exc_info.value.step_name == STEP_NAME
        assert exc_info.value.error_type == "permanent"
        client.get_object.assert_not_called()

    def test_empty_bucket_string_is_rejected_like_an_absent_bucket(self):
        """``Invalid bucket name ""`` is the live failure ``LGR-7c9378d-1`` was written for."""
        record = {**DIRECT_PATH_RECORD, "target_schema_bucket": ""}

        with patch.object(tsr, "_s3_client", MagicMock()), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(record, STEP_NAME)

        assert "target_schema_bucket" in str(exc_info.value)
        assert "target_schema_s3_key" in str(exc_info.value)

    def test_an_empty_key_string_is_a_half_written_record_not_an_absent_schema(self):
        """Reachable from a ``schema_location`` of ``s3://bucket/``.

        ``pipelines_crud.py:763`` splits that into ``["bucket", ""]`` — length 2,
        so the pair is written with an empty key. Reading it as "no target
        schema" would report a malformed record as a pipeline without a schema.
        """
        record = {**DIRECT_PATH_RECORD, "target_schema_s3_key": ""}

        with patch.object(tsr, "_s3_client", MagicMock()), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(record, STEP_NAME)

        assert "target_schema_s3_key" in str(exc_info.value)


class TestADanglingKeyRaises:
    """Design D14 / C16 — the fence. See this module's docstring before relaxing it."""

    def test_no_such_key_raises_rather_than_returning_the_not_found_pair(self, mock_s3_buckets):
        """The bucket exists and is readable; the object the record names is gone.

        Goes through moto rather than an injected ``ClientError`` so the
        ``NoSuchKey`` is S3's own, on the real read path.
        """
        with pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert exc_info.value.step_name == STEP_NAME
        assert exc_info.value.error_type == "permanent"
        assert exc_info.value.details["reason"] == "target_schema_object_missing"
        assert TARGET_KEY in str(exc_info.value)

    def test_a_missing_bucket_raises_too(self, mock_s3_buckets):
        """``NoSuchBucket`` is the same class of inconsistency as ``NoSuchKey``."""
        record = {**DIRECT_PATH_RECORD, "target_schema_bucket": "adi-target-schemas-not-provisioned"}

        with pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(record, STEP_NAME)

        assert exc_info.value.details["reason"] == "target_schema_object_missing"

    def test_the_raise_is_the_resolvers_own_not_the_classifiers(self):
        """The classifier RETURNS for ``NoSuchKey`` — verified here, not assumed.

        ``raise_for_read_client_error``'s not-found contract exists so the
        *caller* picks its not-found value (``step_errors.py:167-169``). This
        test pins that the classifier is unmodified and the raise above is this
        module's own decision, so a reader does not conclude the two conflict.
        """
        not_found = ClientError({"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject")

        assert tsr.raise_for_read_client_error(not_found, STEP_NAME, operation="probe") is None


class TestInfrastructureErrorsPropagateClassified:
    """Requirement 2.8 / C2: a read failure never degrades to the no-schema path."""

    def test_throttle_raises_transient_step_failure(self):
        client = _s3_raising("ThrottlingException")

        with patch.object(tsr, "_s3_client", lambda: client), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert isinstance(exc_info.value, TransientStepFailure)
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == STEP_NAME
        assert exc_info.value.details["operation"] == "resolve_target_schema"

    def test_access_denied_raises_permanent_step_failure(self):
        client = _s3_raising("AccessDenied")

        with patch.object(tsr, "_s3_client", lambda: client), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert type(exc_info.value).__name__ == "StepFailure"
        assert exc_info.value.error_type == "permanent"
        assert exc_info.value.details["error_code"] == "AccessDenied"

    def test_the_failure_is_attributed_to_the_calling_step(self):
        """``step_name`` is threaded so SFN attributes the failure to the right step."""
        client = _s3_raising("ThrottlingException")

        with patch.object(tsr, "_s3_client", lambda: client), pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, "schema_mapping")

        assert exc_info.value.step_name == "schema_mapping"


class TestUnreadableObjects:
    """Bytes that are not a JSON object fail with the location named, not with a bare decode error."""

    def test_malformed_json_raises_naming_the_location(self, mock_s3_buckets):
        _put(b"{not json")

        with pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert exc_info.value.details["reason"] == "target_schema_unparseable"
        assert TARGET_KEY in str(exc_info.value)

    def test_a_json_array_is_not_a_schema(self, mock_s3_buckets):
        _put(b'["id", "email"]')

        with pytest.raises(StepFailure) as exc_info:
            tsr.resolve_target_schema(DIRECT_PATH_RECORD, STEP_NAME)

        assert exc_info.value.details["reason"] == "target_schema_not_an_object"


def test_the_module_raises_the_same_StepFailure_class_these_tests_assert_on():
    """Identity guard for the class-duplication hazard in this file's docstring.

    If ``target_schema_resolution`` ever binds a second ``step_errors`` module
    object, ``pytest.raises(StepFailure)`` above would stop matching *silently*.
    This fails loudly instead.
    """
    assert tsr.StepFailure is StepFailure
    assert tsr.resolve_target_schema.__globals__["StepFailure"] is StepFailure
    assert issubclass(TransientStepFailure, StepFailure)
