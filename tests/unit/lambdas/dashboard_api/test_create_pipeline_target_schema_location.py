# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``create_pipeline`` records the target schema's address for BOTH id shapes.

Two defects, one fix.

**The existing-schema path never recorded the address at all.**
``create_pipeline`` wrote ``target_schema_bucket`` / ``target_schema_s3_key``
only by parsing ``schema_location``, the S3 URI the wizard echoes back from an
inline ``/schemas/upload``. The wizard performs that upload only when NO existing
schema was chosen (``CreatePipelinePage.tsx:309-313``), so selecting an existing
schema produced a record with a ``target_schema_name`` and no address —
``resolve_target_schema`` then returned ``(None, None)`` and p33's gate reported
``schema_compliance`` as ``verified: False`` / ``no_target_schema`` for a pipeline
whose schema object was sitting in the bucket. Live at the time of writing:
``p32ver-ren-renc-6dbdcdf6`` and ``p32obs-ren-renb-41466288`` both carry a name
with a null bucket and key, and ``shared/p32ver_ren_renC.json`` /
``shared/p32obs_ren_renB.json`` exist.

**The key derivation was wrong for every id shape.** The handler guessed
``schemas/uploaded/{id-minus-uploaded_}.json``,
``uploaded_schemas/target_{id}.json`` or ``schemas/{id}.json``.
``upload_schema`` writes ``shared/{name}_{timestamp}.json`` and returns
``{name}_{timestamp}`` as the id (``handlers/schemas.py:322-334``), and
``get_schemas`` derives an existing schema's id by stripping exactly ``shared/``
and ``.json`` (``:81-92``) — so ``shared/{id}.json`` addresses both shapes and
none of the three guesses matched either. The read therefore always raised into a
swallowing ``except``, which also stripped a leading ``uploaded_`` off
``target_schema_name``: live record ``testing-39eddb17`` carries
``target_schema_name=schema_1785844048926_20260804_114729`` against
``target_schema_s3_key=shared/uploaded_schema_1785844048926_20260804_114729.json``
— the name and the key one prefix apart, so its by-name fallback could never find
the object the same record already addressed.

AMENDED BY ADDITION — p35 R4.5 REVERSES THE ID SHAPE, NOT THE SUBJECT
---------------------------------------------------------------------
**Stated as a deliberate reversal** (``LGR-5906ec6-2`` and ``LGR-60c4985-6``, both
**template / ratified**): the sentence above — ``upload_schema`` writes
``shared/{name}_{timestamp}.json`` and returns ``{name}_{timestamp}`` — described
the writer *before* p35 task 8.6. R4.5 removed the second timestamp, so
``upload_schema`` now appends **exactly one** uniqueness component and the wizard's
``uploaded_schema_${Date.now()}`` is stored under itself.

``UPLOADED_SCHEMA_ID`` is therefore kept **as the legacy fixture it is**, and
``SINGLE_SUFFIX_SCHEMA_ID`` is **added** beside it for the post-R4.5 shape. Nothing
was replaced, for a reason: ``uploaded_schema_1785844048926_20260804_114729`` is a
**real live record** (``testing-39eddb17``, ``pipelines_crud.py:822-830``), and it
is the only fixture in this suite representing pre-fix data — which is exactly the
data p35 task 8.4's Scan exists to find. Deleting it would delete the coverage of
the case that motivated the Scan.

**What did not change, and this is the load-bearing half:** ``shared/{id}.json``
still addresses **both** shapes, on both the write and the read path
(``LGR-cad8032-1``, **template / ratified**), and ``target_schema_name`` still
carries the id unstripped. Every assertion below is generic over the id's shape,
so the addition is a second input to the same subject rather than a second
subject.

WHY THESE ASSERT THROUGH ``resolve_target_schema``
--------------------------------------------------
The point of writing the pair is that the consumer can read it. Asserting the two
attribute strings would pass on a record that ``resolve_target_schema`` still
rejects (a bucket with no key, a key naming nothing — both of which that module
fails by design, p33 requirement 2.7 / D14). So the assertions run the actual
consumer over the record that was actually written.

**Validates: Requirements 2.6, 2.7, 3.7**
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler — same preamble
# as test_create_pipeline_p19_target_modes.py.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)

try:
    from handlers import pipelines_crud as pipelines

    import target_schema_resolution as tsr
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

ACCOUNT_ID = "777788889999"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

#: The id shape ``get_schemas`` hands the wizard for an EXISTING uploaded
#: schema: the object basename under ``shared/``, verbatim.
EXISTING_SCHEMA_ID = "p32ver_ren_renC"

#: The id shape ``upload_schema`` returned for a fresh inline upload **before p35
#: R4.5** — the wizard's ``uploaded_schema_${Date.now()}`` with the handler's own
#: ``%Y%m%d_%H%M%S`` appended to it. Retained as the **legacy** fixture: this exact
#: value is a live record (``testing-39eddb17``), and pre-fix records are what task
#: 8.4's Scan exists to find. See the module docstring's amendment note.
UPLOADED_SCHEMA_ID = "uploaded_schema_1785844048926_20260804_114729"

#: The id shape ``upload_schema`` returns **after p35 R4.5**: the wizard's single
#: client-generated component, stored under itself. Added beside the legacy value
#: rather than replacing it — ``create_pipeline``'s subject is the *address*, which
#: R4.5 leaves untouched (``shared/{id}.json``, ``LGR-cad8032-1``), so both shapes
#: must resolve identically.
SINGLE_SUFFIX_SCHEMA_ID = "uploaded_schema_1785844048926"

SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_BYTES = json.dumps(SCHEMA_BODY).encode("utf-8")


def _target_schemas_bucket() -> str:
    """The bucket ``create_pipeline`` derives, built the same way it does."""
    return f"adi-target-schemas-{ACCOUNT_ID}-{pipelines.AWS_REGION}"


@pytest.fixture
def wired_handler(mock_dynamodb_tables, mock_s3_buckets, monkeypatch):
    """Bind the handler to the shared moto DynamoDB table + S3 bucket.

    Only two things are stubbed, and neither is an AWS mock:
    ``get_aws_account_id`` (an STS call that has nothing to do with the address)
    and ``_provision_pipeline_resources`` (the S3-prefix/EventBridge unit of work
    that follows the DDB write). Every DynamoDB and S3 interaction under test is
    the real API against the shared fixtures.

    Yields the moto pipelines table so a test can read back what was written.
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_target_schemas_bucket())

    monkeypatch.setattr(pipelines, "get_aws_account_id", lambda: ACCOUNT_ID)
    monkeypatch.setattr(pipelines, "pipelines_table", mock_dynamodb_tables[PIPELINES_TABLE])
    tsr._s3_client_cache = None

    with patch.object(pipelines, "_provision_pipeline_resources"):
        yield mock_dynamodb_tables[PIPELINES_TABLE]

    tsr._s3_client_cache = None


def _put_schema(key: str, body: bytes = SCHEMA_BYTES) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=_target_schemas_bucket(), Key=key, Body=body)


def _body(target_schema: str, *, schema_location: str | None = None, name: str = "Location Test") -> str:
    payload: dict = {
        "pipeline_name": name,
        "target_schema": target_schema,
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "schema_source": "existing",
    }
    if schema_location is not None:
        payload["schema_location"] = schema_location
    return json.dumps(payload)


def _created_record(table, response: dict) -> dict:
    assert response["statusCode"] == 201, f"expected 201, got {response['statusCode']}: {response['body']}"
    pipeline_id = json.loads(response["body"])["pipeline_id"]
    item = table.get_item(Key={"pipeline_id": pipeline_id}).get("Item")
    assert item is not None, f"no pipelines-table record written for {pipeline_id}"
    return item


class TestAnExistingSchemaSelectionIsAddressable:
    """The defect: no ``schema_location``, so no address was ever recorded."""

    def test_the_record_resolves_through_resolve_target_schema(self, wired_handler):
        """The assertion that matters — the consumer finds the schema."""
        _put_schema(f"shared/{EXISTING_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(EXISTING_SCHEMA_ID), event={}))
        schema, schema_hash = tsr.resolve_target_schema(record, "test_code")

        assert schema == SCHEMA_BODY
        assert len(schema_hash) == 64

    def test_the_pair_is_written_and_names_the_object_that_exists(self, wired_handler):
        _put_schema(f"shared/{EXISTING_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(EXISTING_SCHEMA_ID), event={}))

        assert record["target_schema_bucket"] == _target_schemas_bucket()
        assert record["target_schema_s3_key"] == f"shared/{EXISTING_SCHEMA_ID}.json"

    def test_a_legacy_location_is_recorded_too(self, wired_handler):
        """The address is probed, not assumed to be under ``shared/``."""
        _put_schema(f"schemas/uploaded/{EXISTING_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(EXISTING_SCHEMA_ID), event={}))

        assert record["target_schema_s3_key"] == f"schemas/uploaded/{EXISTING_SCHEMA_ID}.json"
        assert tsr.resolve_target_schema(record, "test_code")[0] == SCHEMA_BODY


class TestTheUploadedIdShapeResolves:
    """Defect 2: the ``uploaded_`` prefix was stripped and the key guessed."""

    def test_an_uploaded_shape_id_without_a_schema_location_still_resolves(self, wired_handler):
        """The upload path normally supplies ``schema_location``; when it does
        not, the id alone has to be enough. The old derivation stripped
        ``uploaded_`` and looked under ``schemas/uploaded/``, matching nothing."""
        _put_schema(f"shared/{UPLOADED_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(UPLOADED_SCHEMA_ID), event={}))

        assert record["target_schema_s3_key"] == f"shared/{UPLOADED_SCHEMA_ID}.json"
        assert tsr.resolve_target_schema(record, "test_code")[0] == SCHEMA_BODY

    def test_target_schema_name_keeps_the_uploaded_prefix(self, wired_handler):
        """``target_schema_name`` is the object basename, so it must not be
        stripped — that is what live record ``testing-39eddb17`` got wrong."""
        _put_schema(f"shared/{UPLOADED_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(UPLOADED_SCHEMA_ID), event={}))

        assert record["target_schema_name"] == UPLOADED_SCHEMA_ID

    def test_target_schema_name_is_unstripped_even_when_nothing_resolves(self, wired_handler):
        """The degraded path has to agree with the resolved one on the name."""
        record = _created_record(wired_handler, pipelines.create_pipeline(_body(UPLOADED_SCHEMA_ID), event={}))

        assert record["target_schema_name"] == UPLOADED_SCHEMA_ID

    def test_the_post_r4_5_single_suffix_shape_resolves_the_same_way(self, wired_handler):
        """**Added by p35 task 8.6.** The new id shape, same address, same result.

        R4.5 made ``upload_schema`` append exactly one uniqueness component, so
        the id a fresh inline upload now carries is
        ``uploaded_schema_1785844048926`` rather than that value with a second
        timestamp on it. This asserts the change is invisible to
        ``create_pipeline``: the address convention is unchanged
        (``shared/{id}.json``, ``LGR-cad8032-1``, **template / ratified**), so both
        shapes must write the same pair and resolve through the same consumer.

        The legacy assertions above are kept unaltered rather than reparametrised,
        so a reader can see that the pre-fix shape still works — which it must,
        because live records carry it.

        **Validates: Requirements 4.5, 4.6, 8.6**
        """
        _put_schema(f"shared/{SINGLE_SUFFIX_SCHEMA_ID}.json")

        record = _created_record(wired_handler, pipelines.create_pipeline(_body(SINGLE_SUFFIX_SCHEMA_ID), event={}))

        assert record["target_schema_name"] == SINGLE_SUFFIX_SCHEMA_ID
        assert record["target_schema_s3_key"] == f"shared/{SINGLE_SUFFIX_SCHEMA_ID}.json"
        assert tsr.resolve_target_schema(record, "test_code")[0] == SCHEMA_BODY


class TestSchemaLocationStaysAuthoritative:
    """``LGR-724f88c-1``'s path is preserved, and its one bad shape is closed."""

    def test_an_explicit_location_is_used_without_probing(self, wired_handler):
        _put_schema("custom/prefix/explicit.json")

        record = _created_record(
            wired_handler,
            pipelines.create_pipeline(
                _body(
                    "explicit",
                    schema_location=f"s3://{_target_schemas_bucket()}/custom/prefix/explicit.json",
                ),
                event={},
            ),
        )

        assert record["target_schema_s3_key"] == "custom/prefix/explicit.json"
        assert tsr.resolve_target_schema(record, "test_code")[0] == SCHEMA_BODY

    def test_a_bucket_only_location_writes_neither_attribute(self, wired_handler):
        """``s3://bucket/`` used to be written as a key of ``""`` — a record
        claiming an object it cannot name, which ``resolve_target_schema``
        rejects (requirement 2.7). Writing neither attribute is the honest
        answer, and it is what the resolver reports as "no target schema"."""
        record = _created_record(
            wired_handler,
            pipelines.create_pipeline(
                _body("absent-schema", schema_location=f"s3://{_target_schemas_bucket()}/"), event={}
            ),
        )

        assert "target_schema_s3_key" not in record
        assert "target_schema_bucket" not in record
        assert tsr.resolve_target_schema(record, "test_code") == (None, None)


class TestAnUnresolvableIdStillCreatesThePipeline:
    """The decision: no address, no claim — but still a 201.

    An id that resolves to no object leaves BOTH attributes absent rather than
    writing a guess. That keeps the record consistent under p33 D14
    (``LGR-78ce0b7-14``): a key the record NAMES is a claim that an object
    exists, so making no claim is the writer's side of that contract. Failing the
    request instead is a stronger obligation D14 did not establish, and it would
    invert the contract nine test modules already assert.
    """

    def test_no_object_anywhere_leaves_the_pair_absent(self, wired_handler):
        record = _created_record(wired_handler, pipelines.create_pipeline(_body("never-uploaded"), event={}))

        assert "target_schema_bucket" not in record
        assert "target_schema_s3_key" not in record

    def test_the_resolver_reports_no_target_schema_rather_than_failing(self, wired_handler):
        """Requirement 2.6: an absent key is the ONE input that returns
        ``(None, None)``. A half-written pair would raise instead."""
        record = _created_record(wired_handler, pipelines.create_pipeline(_body("never-uploaded"), event={}))

        assert tsr.resolve_target_schema(record, "test_code") == (None, None)


class TestNonUploadedTargetTypesAreUnaffected:
    """``glue_*`` and ``s3_table_*`` ids name no S3 object — and must not probe."""

    @pytest.mark.parametrize(
        ("schema_id", "expected_name"),
        [("glue_analytics_users", "analytics.users"), ("s3_table_bkt_ns_tbl", "bkt.ns.tbl")],
    )
    def test_no_address_is_recorded_and_the_name_is_derived_as_before(self, wired_handler, schema_id, expected_name):
        record = _created_record(wired_handler, pipelines.create_pipeline(_body(schema_id), event={}))

        assert record["target_schema_name"] == expected_name
        assert "target_schema_s3_key" not in record
