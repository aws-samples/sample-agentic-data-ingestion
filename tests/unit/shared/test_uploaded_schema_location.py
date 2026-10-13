# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Where an uploaded target schema lives — one definition, probed honestly.

``create_pipeline`` used to GUESS the S3 key for a target-schema id from its
prefix while ``schema_mapping.get_uploaded_json_schema`` probed six candidates,
one of which (``shared/{id}.json``) is where ``upload_schema`` actually writes.
Two definitions of one address: the reader found the object, the writer recorded
nothing, and p33's ``schema_compliance`` check reported ``no_target_schema`` for
pipelines whose schema was sitting in the bucket. Live evidence at the time of
writing — ``p32ver-ren-renc-6dbdcdf6`` and ``p32obs-ren-renb-41466288`` carry
``target_schema_name`` with a null bucket and key, while
``shared/p32ver_ren_renC.json`` and ``shared/p32obs_ren_renB.json`` exist.

These tests pin the two things that make the shared definition load-bearing:

1. :func:`uploaded_schema_candidate_keys` is the SAME list, in the SAME order,
   that ``schema_mapping`` probes — asserted against that module's source rather
   than transcribed, so the two cannot drift back apart.
2. :func:`locate_uploaded_schema` answers ``None`` **only** for a genuine
   not-found. A throttle or an ``AccessDenied`` raises classified, and does NOT
   get counted as "not at this key" six times over.

Point 2 is the whole reason this is a new function rather than a reuse of
``get_uploaded_json_schema``'s loop. That loop's per-candidate
``except Exception: continue`` swallows a throttle across all six candidates and
then reports "Could not find uploaded schema ... in any expected location" — an
infrastructure error asserting the opposite of what happened. That defect is a
known, deliberately-unfixed exception to p33 requirement 2.8, fenced by a
standing ``xfail(strict=True)`` in
``tests/unit/lambdas/schema_mapping/test_get_target_schema_characterization.py``.
It is untouched here; what these tests assert is that the new read did not
inherit it.

**Validates: Requirements 2.7, 2.8, 3.7**
"""

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import target_schema_resolution as tsr
from step_errors import StepFailure, TransientStepFailure

STEP_NAME = "create_pipeline"
TARGET_BUCKET = "agentic-data-ingestion-target-schemas"
SCHEMA_ID = "uploaded_schema_1785844048926_20260804_114729"

SCHEMA_BYTES = json.dumps({"type": "object", "properties": {"id": {"type": "integer"}}}).encode("utf-8")

_SCHEMA_MAPPING_SOURCE = (
    Path(__file__).resolve().parents[3] / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py"
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    tsr._s3_client_cache = None
    yield
    tsr._s3_client_cache = None


def _put(key: str, body: bytes = SCHEMA_BYTES, *, bucket: str = TARGET_BUCKET) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=bucket, Key=key, Body=body)


def _s3_raising(code: str) -> MagicMock:
    """A client whose ``head_object`` raises one ClientError code.

    Error injection is the only part of these tests that does not go through
    moto: moto serves real not-found semantics but cannot be made to throttle.
    Same shape as ``tests/unit/shared/test_target_schema_resolution.py``.
    """
    client = MagicMock()
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": code, "Message": f"simulated {code}"}}, "HeadObject"
    )
    return client


class TestTheCandidateListIsOneDefinition:
    """The writer and the reader compose the address from the same list."""

    def test_the_six_legacy_candidates_are_in_the_documented_order(self):
        assert tsr.uploaded_schema_candidate_keys("acme") == [
            "shared/acme.json",
            "schemas/uploaded/acme.json",
            "uploaded_schemas/target_acme.json",
            "schemas/acme.json",
            "acme.json",
        ]

    def test_a_pipeline_id_prepends_the_pipeline_scoped_candidate(self):
        keys = tsr.uploaded_schema_candidate_keys("acme", "pipe-1")

        assert keys[0] == "pipe-1/acme.json"
        assert keys[1:] == tsr.uploaded_schema_candidate_keys("acme")

    def test_shared_is_where_upload_schema_writes_so_it_leads_the_fallbacks(self):
        """``upload_schema`` writes ``shared/{name}_{timestamp}.json`` and returns
        ``{name}_{timestamp}`` as the id, so ``shared/{id}.json`` must be the
        first candidate that is not pipeline-scoped."""
        assert tsr.uploaded_schema_candidate_keys(SCHEMA_ID)[0] == f"shared/{SCHEMA_ID}.json"

    def test_schema_mapping_no_longer_defines_its_own_copy_of_the_list(self):
        """The alignment, asserted against the other caller's source.

        Transcribing the list into this test would let the two drift apart
        silently, which is the failure mode being fixed. ``schema_mapping`` must
        CALL the shared definition and must not rebuild the keys inline.
        """
        source = _SCHEMA_MAPPING_SOURCE.read_text()

        assert "uploaded_schema_candidate_keys(schema_name, pipeline_id)" in source
        assert not re.search(r'f"uploaded_schemas/target_\{schema_name\}\.json"', source)
        assert not re.search(r'f"shared/\{schema_name\}\.json"', source)


class TestLocateReturnsTheFirstExistingCandidate:
    def test_an_object_under_shared_is_found(self, mock_s3_buckets):
        _put(f"shared/{SCHEMA_ID}.json")

        assert tsr.locate_uploaded_schema(TARGET_BUCKET, SCHEMA_ID, step_name=STEP_NAME) == (f"shared/{SCHEMA_ID}.json")

    def test_a_legacy_location_is_still_found(self, mock_s3_buckets):
        _put("schemas/uploaded/legacy.json")

        assert (
            tsr.locate_uploaded_schema(TARGET_BUCKET, "legacy", step_name=STEP_NAME) == "schemas/uploaded/legacy.json"
        )

    def test_the_pipeline_scoped_candidate_wins_over_shared(self, mock_s3_buckets):
        _put("shared/dup.json")
        _put("pipe-9/dup.json")

        located = tsr.locate_uploaded_schema(TARGET_BUCKET, "dup", "pipe-9", step_name=STEP_NAME)

        assert located == "pipe-9/dup.json"

    def test_no_object_anywhere_returns_none(self, mock_s3_buckets):
        assert tsr.locate_uploaded_schema(TARGET_BUCKET, "absent", step_name=STEP_NAME) is None

    def test_the_located_key_resolves_through_resolve_target_schema(self, mock_s3_buckets):
        """The pair this produces has to satisfy the actual consumer.

        ``locate_uploaded_schema`` exists to supply the address
        ``resolve_target_schema`` reads, so the round trip is the assertion that
        matters — not the string.
        """
        _put(f"shared/{SCHEMA_ID}.json")
        key = tsr.locate_uploaded_schema(TARGET_BUCKET, SCHEMA_ID, step_name=STEP_NAME)

        schema, schema_hash = tsr.resolve_target_schema(
            {"target_schema_bucket": TARGET_BUCKET, "target_schema_s3_key": key}, "test_code"
        )

        assert schema == json.loads(SCHEMA_BYTES)
        assert len(schema_hash) == 64


class TestInfrastructureErrorsDoNotPresentAsNotFound:
    """Requirement 2.8: the defect the fallback chain still carries, not inherited."""

    def test_a_throttle_raises_transient_rather_than_returning_none(self):
        with pytest.raises(TransientStepFailure) as exc_info:
            tsr.locate_uploaded_schema(TARGET_BUCKET, SCHEMA_ID, s3_client=_s3_raising("ThrottlingException"))

        assert exc_info.value.error_type == "transient"

    def test_access_denied_raises_permanent_rather_than_returning_none(self):
        with pytest.raises(StepFailure) as exc_info:
            tsr.locate_uploaded_schema(TARGET_BUCKET, SCHEMA_ID, s3_client=_s3_raising("AccessDenied"))

        assert exc_info.value.error_type == "permanent"

    def test_a_throttle_is_not_swallowed_once_per_candidate(self):
        """The sharp edge: six candidates must not mean six swallows.

        ``get_uploaded_json_schema`` continues past every candidate and reports
        not-found; this read stops at the first one.
        """
        client = _s3_raising("ThrottlingException")

        with pytest.raises(TransientStepFailure):
            tsr.locate_uploaded_schema(TARGET_BUCKET, SCHEMA_ID, s3_client=client)

        assert client.head_object.call_count == 1
