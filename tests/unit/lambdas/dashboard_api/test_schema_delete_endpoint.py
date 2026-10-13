# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``DELETE /api/schemas/{id}`` — R3.3, R3.5, and each outcome's ``checked``.

The example-level companion to Properties 4 and 5. Those two own the *predicates*
— a delete is refused whenever any pipeline references the schema by either
predicate (task 8.2), and an incomplete check never deletes (task 8.3). This file
owns the three things neither of them covers, each named by design §4.2's outcome
table or by the design's own unit-test list:

1. **R3.3** — a ``glue_catalog`` id and an ``s3_table_bucket`` id both answer
   ``400`` stating the schema is a view of a catalog object. Both properties
   generate Uploaded_Schema ids only, so this branch is unreachable from them.
2. **R3.5** — an Uploaded_Schema whose backing object is **already absent**
   answers success, so a repeated delete is not reported as a failure. Both
   properties always seed the object, deliberately, so that R3.5 cannot
   short-circuit the reference check and carry their clauses away with it.
3. **``checked`` on the outcomes that are not a ``503``.** Property 5 asserts
   ``checked`` on the ``503`` and deliberately leaves the ``409``'s contents to
   task 8.2, which in turn does not assert ``checked`` at all — so no property
   states that a ``409`` from the **index** reports the index alone, or that a
   ``200`` reports both lookups. That is the distinction ``LGR-acca05d-4``
   (**template / ratified**) requires the response semantics to state, so it is
   asserted here.

WHY ``checked`` IS WORTH ITS OWN ASSERTIONS
------------------------------------------
``checked`` is not diagnostics. Design §4.2 gives every outcome one because
"no referencing pipelines **found**" and "no referencing pipelines **exist**" are
different claims — the same distinction R1.4 and R5.3 turn on elsewhere in this
file's subject. The two that matter most:

* A ``409`` from the Query reports ``[index]`` and **not** the scan. Reporting the
  scan would claim a full-table read that never happened; design §4.2's first row
  says the scan is not run, because one referencing pipeline is already enough to
  refuse and the scan could only add more.
* A ``200`` reports ``[index, scan]``. That is what makes the delete's claim
  unqualified, and it is the only outcome for which both lookups must have run to
  the end.

EXPECTATIONS COME FROM THE REQUIREMENTS, NOT FROM THE HANDLER
------------------------------------------------------------
Both key conventions are written out here rather than imported from
``handlers.schemas``: ``shared/{id}.json`` (R4.6, ``LGR-cad8032-1``) and
``schemas/uploaded/{id}-metadata.json`` (R4.12). A test that imported the
producer's own expression could not detect the producer changing it, which is the
anti-pattern ``LGR-28b4d75-5`` (**template / ratified**) and R8.7 forbid.

FIXTURES AND REGION
-------------------
Shared fixtures only — ``mock_s3_buckets`` and ``mock_dynamodb_tables`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and this file does **not** set
``AWS_REGION`` (``LGR-3e45e65-1``): the unit layer pins it in
``tests/unit/conftest.py``, which is what ``delete_schema``'s own
``os.environ.get("AWS_REGION", "us-west-2")`` then resolves.

``mock_dynamodb_tables`` creates its tables in ``us-east-1`` while the handler
resolves ``us-west-2``, and moto's DynamoDB backend is region-scoped, so the
fixture binds the fixture's table onto both ``handlers.schemas`` and
``handlers.common`` — the same two bindings Properties 4 and 5 use, and the reason
the handler reaches the table through a module-level handle.

**Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.7, 3.10**
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.schemas
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Table names only — AWS_REGION and AWS_DEFAULT_REGION are deliberately absent.
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import common as common_handler
from handlers import schemas as schemas_handler

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

#: The live pre-R4.5 double-suffix id — ``testing-39eddb17``'s key names it
#: (``pipelines_crud.py:822-830``).
SCHEMA_ID = "uploaded_schema_1785844048926_20260804_114729"
SCHEMA_BODY = {"type": "object", "properties": {"email": {"type": "string"}}}


def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 / ``LGR-cad8032-1``, written out not imported."""
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{id}-metadata.json`` — R4.12's Context_Sidecar."""
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _client(service: str):
    """A client in exactly the region the handler resolves (``LGR-3e45e65-1``)."""
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _inventory() -> set[str]:
    """Every key in the target-schemas bucket — the effect-level witness."""
    keys: set[str] = set()
    for page in _client("s3").get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _parsed(response: dict[str, Any], expected_status: int) -> dict[str, Any]:
    assert response["statusCode"] == expected_status, (
        f"expected {expected_status}, got {response['statusCode']} with body {response.get('body')!r}"
    )
    return json.loads(response["body"])


@pytest.fixture
def delete_env(mock_s3_buckets, mock_dynamodb_tables, monkeypatch):
    """Shared moto backends, the env, and the two table bindings.

    Nothing is stubbed that is not an address: ``TARGET_SCHEMAS_BUCKET`` names the
    fixture's bucket and ``AWS_ACCOUNT_ID`` keeps ``get_aws_account_id`` off STS.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(schemas_handler, "pipelines_table", table, raising=False)
    monkeypatch.setattr(common_handler, "pipelines_table", table)
    return table


def _seed_body(schema_id: str = SCHEMA_ID) -> None:
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=_body_key(schema_id),
        Body=json.dumps(SCHEMA_BODY),
        ContentType="application/json",
    )


def _seed_pipeline(table, pipeline_id: str, *, name: str | None = None, key: str | None = None) -> None:
    """One pipeline record. ``created_at`` is always present.

    Not cosmetic: ``created_at`` is ``target_schema_name-index``'s sort key
    (``tests/fixtures/conftest.py``), so a record without it is not in the index
    at all, and ``create_pipeline`` always writes it (``pipelines_crud.py:838``).
    """
    item: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "pipeline_name": f"{pipeline_id} display name",
        "status": "active",
        "created_at": "2026-08-07T00:00:00Z",
    }
    if name is not None:
        item["target_schema_name"] = name
    if key is not None:
        item["target_schema_s3_key"] = key
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# R3.3 — a catalog-derived schema is a view, and is not deletable here
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("schema_id", "expected_source"),
    [
        # `_collect_schema_rows` synthesises `glue_{database}_{table}`.
        ("glue_agentic_data_ingestion_testingbucket_testingbucketns_testingbuckettn", "glue_catalog"),
        ("glue_sagemaker_sample_db_churn", "glue_catalog"),
        # ...and `s3_table_{bucket}_{namespace}_{table}`.
        ("s3_table_testingbucket_testingbucketns_testingbuckettn", "s3_table_bucket"),
    ],
)
def test_a_catalog_derived_id_is_refused_with_400_and_says_why(schema_id, expected_source, delete_env):
    """R3.3: ``400`` stating the schema is a view of a catalog object.

    Both id shapes are **synthesised identifiers** for entries owned by
    ``create_infrastructure`` and by the S3 Tables service (R3's scope note).
    Neither names a deletable object, and deleting either through a dashboard list
    action would destroy a pipeline's target — so the refusal is the point, and
    the reason is what R3.6 renders instead of a fixed message.

    **Validates: Requirement 3.3**
    """
    body = _parsed(schemas_handler.delete_schema(schema_id), 400)

    assert body["reason"] == "catalog_derived_schema"
    assert body["schemaId"] == schema_id
    assert body["source"] == expected_source
    assert "view of a catalog object" in body["error"], (
        f"R3.3 requires the response to state that the schema is a view of a catalog object; got {body['error']!r}"
    )
    assert "checked" not in body, (
        "R3.3 is decided before any lookup runs, so the response must not claim a reference check. "
        "A `checked` here would report a lookup that never happened."
    )


def test_a_catalog_derived_id_is_refused_before_any_reference_lookup(delete_env):
    """R3.3 precedes the reference check, so no pipeline read is made for it.

    Asserted at the effect level rather than by reading the body: the table is
    seeded with a record that WOULD match on the index, and the refusal must not
    depend on it. A handler that ran the lookup first and refused afterwards would
    pass the status assertion above while paying for a read it cannot use.

    **Validates: Requirement 3.3**
    """
    schema_id = "glue_db_table"
    _seed_pipeline(delete_env, "p35-cat-0", name=schema_id, key=_body_key(schema_id))

    body = _parsed(schemas_handler.delete_schema(schema_id), 400)

    assert body["reason"] == "catalog_derived_schema"
    assert "referencingPipelines" not in body, (
        "a catalog-derived refusal is about what the schema IS, not about who references it"
    )


def test_an_uploaded_id_that_merely_mentions_a_catalog_word_is_not_refused(delete_env):
    """The R3.3 test is on the **prefix**, so an id that only contains it deletes.

    ``my_glue_export`` and ``customer_s3_table_dump`` are Uploaded_Schema ids that
    contain a catalog prefix without starting with one. Refusing them would make
    R3.1 unreachable for a whole family of legitimate names, so this is the
    negative half of R3.3 and it is what fails if the test ever loosens to
    containment.

    **Validates: Requirements 3.1, 3.3**
    """
    for schema_id in ("my_glue_export", "customer_s3_table_dump"):
        _seed_body(schema_id)

        body = _parsed(schemas_handler.delete_schema(schema_id), 200)

        assert body["schemaId"] == schema_id
        assert _body_key(schema_id) not in _inventory(), f"{schema_id} is an uploaded schema and must be deleted"


# ---------------------------------------------------------------------------
# R3.5 — an already-absent object is a success, not a failure
# ---------------------------------------------------------------------------
def test_a_repeated_delete_of_an_unreferenced_schema_succeeds_twice(delete_env):
    """R3.5: the second delete is a success, so a repeat is not reported as a failure.

    Run as an actual repeat rather than as "delete an id that never existed",
    because the requirement is about the *second* request: "IF a delete is
    requested for an Uploaded_Schema whose backing object is already absent, THEN
    respond with a success status, so that a repeated delete is not reported as a
    failure."

    **Validates: Requirements 3.1, 3.5**
    """
    _seed_body()
    assert _body_key(SCHEMA_ID) in _inventory()

    first = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 200)
    assert _body_key(SCHEMA_ID) not in _inventory(), "R3.1: the first delete removes the object"

    second = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 200)

    assert second["schemaId"] == first["schemaId"] == SCHEMA_ID
    assert second["checked"] == first["checked"], (
        "the reference check runs on both requests and reports the same lookups; R3.5 changes the "
        "outcome of the delete, not the completeness of the check"
    )


def test_an_already_absent_object_is_still_refused_when_a_pipeline_references_it(delete_env):
    """R3.10 orders R3.7 before the delete, so R3.5 cannot mask a refusal.

    The sharpest interaction between the two criteria: the object is gone AND a
    pipeline record still names it. R3.5 alone would answer success; R3.10 says
    3.7 is evaluated **before** deleting, and R3.7 says refuse. Refusing is also
    the more useful answer — the pipeline's ``target_schema_s3_key`` still names a
    key that no longer resolves, which is the state
    ``resolve_target_schema`` raises ``target_schema_object_missing`` on
    (``shared/target_schema_resolution.py:290-302``, ``LGR-78ce0b7-14``), so a
    quiet success would hide exactly the condition the user needs to see.

    **Validates: Requirements 3.5, 3.7, 3.10**
    """
    _seed_pipeline(delete_env, "p35-abs-0", name="something_else", key=_body_key(SCHEMA_ID))
    assert _body_key(SCHEMA_ID) not in _inventory(), "the object is deliberately never seeded"

    body = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 409)

    assert body["reason"] == "schema_in_use"
    assert [entry["pipelineId"] for entry in body["referencingPipelines"]] == ["p35-abs-0"]


# ---------------------------------------------------------------------------
# `checked` — which lookups ran, on every outcome that is not a 503
# ---------------------------------------------------------------------------
def test_a_409_from_the_index_reports_the_index_alone_and_not_the_scan(delete_env):
    """Design §4.2, first row: the scan is not run, so it is not reported.

    A ``checked`` naming the scan here would claim a full-table read that never
    happened. It is also the assertion that distinguishes Query-then-Scan from
    Scan-always: one referencing pipeline found on the index is already enough to
    refuse, so paying for the scan would buy nothing the answer depends on.

    **Validates: Requirements 3.7, 3.10**
    """
    _seed_body()
    _seed_pipeline(delete_env, "p35-idx-0", name=SCHEMA_ID, key=_body_key(SCHEMA_ID))

    body = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 409)

    assert body["checked"] == ["target_schema_name-index"], (
        f"the Query answered, so only the index ran; got {body['checked']!r}"
    )
    assert _body_key(SCHEMA_ID) in _inventory(), "R3.7: a refusal leaves the object alone"


def test_a_409_from_the_scan_reports_both_lookups(delete_env):
    """Design §4.2, second row — **the case Query-only would have got wrong**.

    ``testing-39eddb17``'s shape: the record's ``target_schema_s3_key`` names our
    object while its ``target_schema_name`` names something else, so the index
    answers nothing and only the scan can see it. Both lookups ran, so both are
    reported.

    **Validates: Requirements 3.7, 3.8, 3.10**
    """
    _seed_body()
    _seed_pipeline(
        delete_env,
        "p35-scan-0",
        name="schema_1785844048926_20260804_114729",
        key=_body_key(SCHEMA_ID),
    )

    body = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 409)

    assert body["checked"] == ["target_schema_name-index", "target_schema_s3_key-scan"], (
        f"the Query found nothing and the scan found the record, so both ran; got {body['checked']!r}"
    )
    assert [entry["pipelineId"] for entry in body["referencingPipelines"]] == ["p35-scan-0"]
    assert _body_key(SCHEMA_ID) in _inventory()


def test_a_200_reports_both_lookups_which_is_what_makes_its_claim_unqualified(delete_env):
    """Design §4.2, third row: the delete rests on both lookups completing.

    ``[index, scan]`` on a ``200`` is the whole reason the claim "no pipeline
    record references this schema" is unqualified. A ``200`` reporting the index
    alone would mean "not found by index", which is a different and weaker claim —
    and it is the claim Query-only would have made while deleting the live
    disagreeing record's schema.

    **Validates: Requirements 3.1, 3.2, 3.10**
    """
    _seed_body()
    # A bystander that references a different schema, so the scan has something to
    # filter out rather than trivially matching nothing.
    _seed_pipeline(delete_env, "p35-ok-0", name="other_schema", key=_body_key("other_schema"))

    body = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 200)

    assert body["checked"] == ["target_schema_name-index", "target_schema_s3_key-scan"]
    assert body["referencingPipelines"] == []
    assert _body_key(SCHEMA_ID) not in _inventory(), "R3.1/R3.2: the object is gone"


def test_the_sidecar_is_not_the_schemas_object_so_a_record_naming_it_does_not_refuse(delete_env):
    """R3.8 is equality on the schema's object, and the sidecar is a different one.

    The Context_Sidecar (``schemas/uploaded/{id}-metadata.json``, R4.12) carries
    the schema id and is not the schema's body. A predicate that refused whenever
    the id appeared in a key would refuse here — and would make every delete
    impossible for any schema whose sidecar had ever been named, while still
    looking correct on the referenced case.

    **Validates: Requirements 3.1, 3.8**
    """
    _seed_body()
    _seed_pipeline(delete_env, "p35-side-0", name="other_schema", key=_sidecar_key(SCHEMA_ID))

    body = _parsed(schemas_handler.delete_schema(SCHEMA_ID), 200)

    assert body["checked"] == ["target_schema_name-index", "target_schema_s3_key-scan"]
    assert _body_key(SCHEMA_ID) not in _inventory()


# ---------------------------------------------------------------------------
# The reference check's own contract, at the function
# ---------------------------------------------------------------------------
def test_find_referencing_pipelines_returns_the_records_and_the_lookups(delete_env):
    """``_find_referencing_pipelines``' two return values, read directly.

    Asserted at the function as well as through the handler because the second
    element is a **contract**, not a by-product: it is what
    ``LGR-acca05d-4`` requires the response semantics to state, and a handler
    test cannot show that the *function* distinguishes the two lookups from each
    other.

    **Validates: Requirements 3.7, 3.8**
    """
    _seed_pipeline(delete_env, "p35-fn-0", name=SCHEMA_ID, key=_body_key(SCHEMA_ID))

    hits, checked = schemas_handler._find_referencing_pipelines(SCHEMA_ID)
    assert [str(item["pipeline_id"]) for item in hits] == ["p35-fn-0"]
    assert checked == ["target_schema_name-index"], "an index hit does not run the scan"

    # The same schema, referenced only by a record the index cannot see.
    delete_env.delete_item(Key={"pipeline_id": "p35-fn-0"})
    _seed_pipeline(delete_env, "p35-fn-1", name="disagreeing_name", key=_body_key(SCHEMA_ID))

    hits, checked = schemas_handler._find_referencing_pipelines(SCHEMA_ID)
    assert [str(item["pipeline_id"]) for item in hits] == ["p35-fn-1"]
    assert checked == ["target_schema_name-index", "target_schema_s3_key-scan"]

    # And with nothing referencing it: an empty result, both lookups complete.
    delete_env.delete_item(Key={"pipeline_id": "p35-fn-1"})
    hits, checked = schemas_handler._find_referencing_pipelines(SCHEMA_ID)
    assert hits == []
    assert checked == ["target_schema_name-index", "target_schema_s3_key-scan"], (
        "an empty result is only usable when both lookups ran — that is what `checked` reports"
    )
