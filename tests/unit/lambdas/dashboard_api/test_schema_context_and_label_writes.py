# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Context_Sidecar's two writers, by example — statuses, clears, and coexistence.

``update_field_context`` and ``set_display_label`` write **two keys into one
object**, ``schemas/uploaded/{schema_id}-metadata.json``:

    {"field_contexts": {field_name: text},   # D3, LGR-acca05d-3, R2.3
     "display_name": "<label>"}              # D2, LGR-acca05d-2, R4.12

Properties 3 and 8 cover the universal claims — a rename changes the label and
nothing else, and a context round-trips into a created pipeline. This file covers
what a property cannot: the individual statuses, the two cases where R4 and R2
are silent and task 8.5 had to choose, and the interaction between the two
writers, which is the one thing neither property watches from both ends.

WHAT IS PINNED HERE, AND WHY EACH ONE
-------------------------------------
1. **R2.5's ``400``** — a context ``PUT`` against a non-uploaded id says that only
   uploaded schemas carry editable context. A ``glue_catalog`` or
   ``s3_table_bucket`` id is a *synthesised* identifier for a catalog entry owned
   by ``create_infrastructure`` or by the S3 Tables service; it names no object
   under ``shared/`` and ``_schema_detail`` reads the sidecar only for
   ``source == "uploaded"``, so anything stored against it would be stored and
   never displayed. The same ``400`` is asserted for the label ``PUT`` — by
   inference from the same fact rather than from a requirement, which is stated
   here so a reader knows which of the two has R2.5 behind it.
2. **A ``404`` for an id nothing backs.** Not a requirement either. The
   alternative — write the sidecar for any well-shaped id — was rejected because a
   ``200`` would be a claim that the schema now carries a label, and for a mistyped
   id that claim is false and leaves an orphan ``-metadata.json`` no listing can
   ever surface (``_collect_schema_rows`` enumerates ``shared/*.json``).
   ``LGR-78ce0b7-14`` (**template / ratified**) is the rule: a response is a claim.
3. **An empty value CLEARS rather than being refused**, and it clears by
   **removing the key** rather than by storing ``""``. R4 does not say which, and
   Property 3 deliberately does not generate an empty label so as not to enforce
   an answer it was not given — so the choice is task 8.5's, and this is where it
   is written down and fenced. Three reasons, all in the block comment above
   ``update_field_context``: R4.2 makes "usable with no Display_Label at all" a
   first-class state and R4.8 says what to show then, so refusing would make that
   state unreachable once a label had been set; a stored ``""`` is a name that
   names nothing (``LGR-78ce0b7-14``); and ``_schema_detail``'s ``if
   display_label:`` would *tolerate* ``""``, which is the tolerant-consumer shape
   ``LGR-28b4d75-5`` (**template / ratified**) rejects — absence gets one
   representation.
4. **Verbatim storage, R4.7 and R2.3.** ``"  padded label  "`` keeps its padding.
   ``.strip()`` is used only to decide *whether this is a clear*, never on the
   stored value. Property 3 pins the padded label as an explicit example; this
   pins the same rule for a context and for the response echo.
5. **The two keys coexist, in both orders.** R4.12 says the label lives
   "alongside ``field_contexts``". Property 3 asserts one direction — a label write
   preserves a seeded ``field_contexts``. The other direction is asserted here: a
   **context** write must preserve a ``display_name``. ``put_object`` replaces
   rather than merges, so a writer that skipped the read would fail exactly one of
   these two, and Property 3 alone would only catch one of them.
6. **A malformed body is a ``400``, and a broken sidecar is a ``500`` naming it** —
   R2.1's "report the failure to the user" needs a status and a reason the
   frontend can render, which is why the ``500`` carries ``details`` rather than a
   fixed message. R3.6's lesson on the sibling endpoint.

**Fixtures.** Shared fixtures only — ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and ``AWS_REGION`` is **read,
never set** (``LGR-3e45e65-1``), through the same
``os.environ.get("AWS_REGION", "us-west-2")`` expression ``schemas.py:23`` uses,
so the seeding client and the handler cannot land in different backends.

**Validates: Requirements 2.1, 2.3, 2.5, 4.7, 4.9, 4.12**
"""

from __future__ import annotations

import json
import os
import sys

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path and import-time env — must precede importing handlers.schemas
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

from handlers.schemas import get_schema, set_display_label, update_field_context

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
SCHEMA_ID = "uploaded_schema_1785844048926"
SCHEMA_BODY = {
    "fields": [
        {"name": "email", "type": "string", "required": True, "description": "Primary contact address"},
        {"name": "nickname", "type": "string", "required": False},
    ]
}


def _client(service: str):
    """A client in exactly the region the handler will use (``schemas.py:23``)."""
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _sidecar_key(schema_id: str = SCHEMA_ID) -> str:
    """``schemas/uploaded/{id}-metadata.json`` — R2.3 / R4.12 verbatim."""
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _body_key(schema_id: str = SCHEMA_ID) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention."""
    return f"shared/{schema_id}.json"


def _sidecar(schema_id: str = SCHEMA_ID) -> dict | None:
    s3 = _client("s3")
    try:
        return json.loads(s3.get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_sidecar_key(schema_id))["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _put_raw_sidecar(body: bytes, schema_id: str = SCHEMA_ID) -> None:
    _client("s3").put_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_sidecar_key(schema_id), Body=body)


def _context_event(text) -> dict:
    return {"httpMethod": "PUT", "body": json.dumps({"context": text})}


def _label_event(label) -> dict:
    return {"httpMethod": "PUT", "body": json.dumps({"displayLabel": label})}


def _body(response: dict, expected_status: int = 200) -> dict:
    assert response["statusCode"] == expected_status, f"expected {expected_status}, got {response}"
    return json.loads(response["body"])


@pytest.fixture
def uploaded_schema(mock_s3_buckets, monkeypatch):
    """One uploaded schema body in the shared fixture's target-schemas bucket."""
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=_body_key(),
        Body=json.dumps(SCHEMA_BODY),
        ContentType="application/json",
    )
    return SCHEMA_ID


# ---------------------------------------------------------------------------
# R2.5 — only uploaded schemas carry editable context
# ---------------------------------------------------------------------------
class TestOnlyUploadedSchemasAreEditable:
    """R2.5, and the same answer for the label by inference from the same fact."""

    @pytest.mark.parametrize(
        ("schema_id", "expected_source"),
        [
            ("glue_agentic_data_ingestion_testingbucket_testingbucketns_testingbuckettn", "glue_catalog"),
            ("s3_table_testingbucket_testingbucketns_testingbuckettn", "s3_table_bucket"),
        ],
    )
    def test_a_context_put_against_a_catalog_derived_id_is_a_400_saying_why(
        self, schema_id, expected_source, uploaded_schema
    ):
        """R2.5: ``400`` stating that only uploaded schemas carry editable context.

        **Validates: Requirement 2.5**
        """
        body = _body(update_field_context(schema_id, "email", _context_event("x")), expected_status=400)

        assert body["reason"] == "not_an_uploaded_schema"
        assert body["source"] == expected_source
        assert "uploaded" in body["error"].lower(), (
            f"the 400 must say that only uploaded schemas carry editable context — got {body['error']!r}"
        )
        assert _sidecar(schema_id) is None, "a refused write must write nothing"

    @pytest.mark.parametrize(
        "schema_id",
        [
            "glue_agentic_data_ingestion_testingbucket_testingbucketns_testingbuckettn",
            "s3_table_testingbucket_testingbucketns_testingbuckettn",
        ],
    )
    def test_a_label_put_against_a_catalog_derived_id_is_a_400_too(self, schema_id, uploaded_schema):
        """The same ``400``, by **inference** rather than by requirement.

        R2.5 speaks about context. Nothing in R4 says what a label write against a
        Glue mirror should do — but ``_schema_detail`` reads the sidecar only for
        ``source == "uploaded"``, so a label stored against a catalog-derived id
        could never be rendered, and a ``200`` would claim it had been set.
        Refusing is the honest answer, and the inference is recorded here rather
        than left to look like a requirement.

        **Validates: Requirement 2.5**
        """
        body = _body(set_display_label(schema_id, _label_event("Customer master")), expected_status=400)

        assert body["reason"] == "not_an_uploaded_schema"
        assert _sidecar(schema_id) is None, "a refused rename must write nothing"


class TestAnIdNothingBacksIsA404:
    """No object under ``shared/`` means nothing to annotate or rename."""

    def test_a_context_put_for_an_unknown_id_is_a_404_naming_it(self, uploaded_schema):
        body = _body(update_field_context("no_such_schema", "email", _context_event("x")), expected_status=404)

        assert "no_such_schema" in body["error"]
        assert _sidecar("no_such_schema") is None, (
            "a 404 must leave no orphan sidecar — an object with no body is invisible to the "
            "listing, which enumerates shared/*.json"
        )

    def test_a_label_put_for_an_unknown_id_is_a_404_naming_it(self, uploaded_schema):
        body = _body(set_display_label("no_such_schema", _label_event("Customer master")), expected_status=404)

        assert "no_such_schema" in body["error"]
        assert _sidecar("no_such_schema") is None


# ---------------------------------------------------------------------------
# The context writer — R2.3's shape, verbatim, and the clear
# ---------------------------------------------------------------------------
class TestTheContextWriterWritesTheShapeTheReaderReads:
    """R2.3: ``{"field_contexts": {field_name: text}}``, the shape ``pipelines_crud.py:800`` reads."""

    def test_the_stored_shape_is_exactly_what_the_reader_expects(self, uploaded_schema):
        """**Validates: Requirement 2.3**"""
        assert (
            _body(update_field_context(SCHEMA_ID, "email", _context_event("PII — hashed downstream")))["context"]
            == "PII — hashed downstream"
        )

        assert _sidecar() == {"field_contexts": {"email": "PII — hashed downstream"}}, (
            "the object must carry field_contexts and nothing else — this is the shape "
            "pipelines_crud.py:800 already read before any writer existed (LGR-acca05d-3)"
        )

    def test_the_text_is_stored_verbatim_including_its_padding(self, uploaded_schema):
        """R2.3 persists **the submitted text**; ``.strip()`` decides only whether this is a clear."""
        update_field_context(SCHEMA_ID, "email", _context_event("  padded context  "))

        assert _sidecar()["field_contexts"]["email"] == "  padded context  "

    def test_a_second_write_to_the_same_field_replaces_it_and_keeps_the_others(self, uploaded_schema):
        update_field_context(SCHEMA_ID, "email", _context_event("first"))
        update_field_context(SCHEMA_ID, "nickname", _context_event("what they go by"))
        update_field_context(SCHEMA_ID, "email", _context_event("second"))

        assert _sidecar()["field_contexts"] == {"email": "second", "nickname": "what they go by"}

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t", None])
    def test_an_empty_context_clears_the_field_rather_than_storing_an_empty_string(self, empty, uploaded_schema):
        """The choice task 8.5 made where R2 is silent — cleared, and by key removal.

        ``""`` and absent are **already** indistinguishable to both readers
        (``_schema_detail`` and ``pipelines_crud.py:800`` each
        ``.get(field_name, "")``), so storing ``""`` would add a second
        representation of one state and every future reader would have to know
        it. ``None`` is included because that is how a JSON client spells "no
        value".
        """
        update_field_context(SCHEMA_ID, "email", _context_event("PII"))
        assert _sidecar()["field_contexts"] == {"email": "PII"}

        body = _body(update_field_context(SCHEMA_ID, "email", _context_event(empty)))

        assert body["context"] == ""
        assert "cleared" in body["message"].lower()
        assert _sidecar()["field_contexts"] == {}, (
            f"an empty context ({empty!r}) must REMOVE the key, not store '' — got {_sidecar()}"
        )

    def test_clearing_a_field_that_was_never_set_is_not_an_error(self, uploaded_schema):
        """A no-op stays a ``200``: nothing was there, and nothing is there now."""
        body = _body(update_field_context(SCHEMA_ID, "nickname", _context_event("")))

        assert body["context"] == ""
        assert _sidecar() == {"field_contexts": {}}

    def test_the_detail_surface_reads_back_what_was_written(self, uploaded_schema):
        """The round trip through the reader, stated as one example (Property 8 generalises it)."""
        update_field_context(SCHEMA_ID, "email", _context_event("PII — hashed downstream"))

        fields = {field["name"]: field for field in _body(get_schema(SCHEMA_ID))["fields"]}
        assert fields["email"]["context"] == "PII — hashed downstream"
        assert fields["nickname"]["context"] == "", "a field with no context reads back as '', never as None"
        # R1.6's two authorities stay two: the sidecar filled `context` and left
        # the schema's own `description` alone.
        assert fields["email"]["description"] == "Primary contact address"


# ---------------------------------------------------------------------------
# The label writer — R4.7's verbatim storage, R4.9's persistence, and the clear
# ---------------------------------------------------------------------------
class TestTheLabelWriter:
    """R4.9, R4.12 — ``display_name`` in the same object, verbatim."""

    def test_the_label_is_stored_and_read_back_by_the_detail_surface(self, uploaded_schema):
        """R4.9: the label persists and the surfaces subsequently display it.

        **Validates: Requirements 4.9, 4.12**
        """
        assert _body(set_display_label(SCHEMA_ID, _label_event("Customer master")))["displayLabel"] == "Customer master"

        assert _sidecar() == {"display_name": "Customer master"}
        assert _body(get_schema(SCHEMA_ID))["displayLabel"] == "Customer master"

    def test_the_label_is_stored_verbatim_with_no_uniqueness_transformation(self, uploaded_schema):
        """R4.7: two schemas may carry the same label, and padding survives.

        Uniqueness is a property of the **id**, because the id is an address
        (R4.6, ``LGR-cad8032-1``). A label is a presentation value with no
        addressing role (R4.11), so nothing may be appended to it to make it
        unique.

        **Validates: Requirement 4.7**
        """
        second_id = "customers_jsonschema"
        _client("s3").put_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(second_id), Body=json.dumps(SCHEMA_BODY))

        set_display_label(SCHEMA_ID, _label_event("  Customer master  "))
        set_display_label(second_id, _label_event("  Customer master  "))

        assert _sidecar()["display_name"] == "  Customer master  ", "R4.7: stored verbatim, padding included"
        assert _sidecar(second_id)["display_name"] == "  Customer master  ", (
            "R4.7: two schemas may carry the same Display_Label — nothing is appended to make it unique"
        )

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_an_empty_label_clears_it_and_the_detail_surface_falls_back_to_the_id(self, empty, uploaded_schema):
        """The other half of task 8.5's silence-filling choice, and why it matters.

        R4.2 makes "usable with no Display_Label at all" a first-class state and
        R4.8 says the schema **id** is what to display then. Refusing an empty
        label would make that state unreachable once a label had been set, with no
        way back short of editing S3 by hand — so an empty label clears.

        The clear removes the key, so ``displayLabel`` is **absent** from the
        detail response rather than ``null`` (DD9): a client that falls back on a
        missing key renders the id, while one handed ``null`` renders nothing.
        """
        set_display_label(SCHEMA_ID, _label_event("Customer master"))
        assert _body(get_schema(SCHEMA_ID))["displayLabel"] == "Customer master"

        body = _body(set_display_label(SCHEMA_ID, _label_event(empty)))

        assert body["cleared"] is True
        assert "displayLabel" not in body, "a cleared label is absent from the response, not null"
        assert _sidecar() == {}, f"an empty label ({empty!r}) must REMOVE display_name, not store '' — got {_sidecar()}"
        assert "displayLabel" not in _body(get_schema(SCHEMA_ID)), (
            "R4.8: with no label stored the detail surface omits the key, so the client renders the id"
        )

    def test_the_schema_body_is_byte_identical_after_a_rename(self, uploaded_schema):
        """R4.10, as one example. Property 3 is the general statement.

        Restated here because it is the clause with the most distant failure: the
        body's bytes are hashed as ``target_schema_version``
        (``LGR-eee8ac8-4``), so one changed byte presents as target-schema drift
        and triggers a remap and an approval for a reason unrelated to the schema.
        """
        before = _client("s3").get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key())["Body"].read()

        set_display_label(SCHEMA_ID, _label_event("Customer master"))

        after = _client("s3").get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key())["Body"].read()
        assert after == before


# ---------------------------------------------------------------------------
# R4.12's "alongside field_contexts" — both directions
# ---------------------------------------------------------------------------
class TestTheTwoKeysCoexist:
    """One object, two writers, and ``put_object`` **replaces** rather than merges.

    Property 3 asserts one direction: a label write preserves a seeded
    ``field_contexts``. The other direction has no property watching it, and a
    writer that skipped the read would fail exactly one of the two — so both are
    asserted here, in both orders.
    """

    def test_a_label_write_preserves_field_contexts(self, uploaded_schema):
        """**Validates: Requirement 4.12**"""
        update_field_context(SCHEMA_ID, "email", _context_event("PII"))
        set_display_label(SCHEMA_ID, _label_event("Customer master"))

        assert _sidecar() == {"field_contexts": {"email": "PII"}, "display_name": "Customer master"}

    def test_a_context_write_preserves_the_display_label(self, uploaded_schema):
        """The direction Property 3 does not watch.

        **Validates: Requirement 4.12**
        """
        set_display_label(SCHEMA_ID, _label_event("Customer master"))
        update_field_context(SCHEMA_ID, "email", _context_event("PII"))

        assert _sidecar() == {"display_name": "Customer master", "field_contexts": {"email": "PII"}}

    def test_clearing_one_leaves_the_other(self, uploaded_schema):
        update_field_context(SCHEMA_ID, "email", _context_event("PII"))
        set_display_label(SCHEMA_ID, _label_event("Customer master"))

        set_display_label(SCHEMA_ID, _label_event(""))
        assert _sidecar() == {"field_contexts": {"email": "PII"}}

        set_display_label(SCHEMA_ID, _label_event("Customer master"))
        update_field_context(SCHEMA_ID, "email", _context_event(""))
        assert _sidecar() == {"field_contexts": {}, "display_name": "Customer master"}

    def test_both_reach_the_detail_surface_together(self, uploaded_schema):
        update_field_context(SCHEMA_ID, "email", _context_event("PII"))
        set_display_label(SCHEMA_ID, _label_event("Customer master"))

        detail = _body(get_schema(SCHEMA_ID))
        assert detail["displayLabel"] == "Customer master"
        assert {field["name"]: field["context"] for field in detail["fields"]} == {"email": "PII", "nickname": ""}


# ---------------------------------------------------------------------------
# R2.1 — report the failure, with a reason, rather than a fixed message
# ---------------------------------------------------------------------------
class TestFailuresAreReportedWithAReason:
    """R2.1's server half: a status and a reason the frontend can render.

    R2.1's other half — "leave the entered text recoverable" — is the frontend's
    (task 8.7). What the handler owes it is a distinguishable answer, which is why
    the ``500`` carries ``details`` instead of a fixed message. R3.6 records the
    same lesson for the sibling delete endpoint.
    """

    @pytest.mark.parametrize(
        ("event", "handler", "args"),
        [
            ({"body": "{not json"}, update_field_context, (SCHEMA_ID, "email")),
            ({"body": "{}"}, update_field_context, (SCHEMA_ID, "email")),
            ({"body": json.dumps({"context": 42})}, update_field_context, (SCHEMA_ID, "email")),
            ({"body": json.dumps(["not", "an", "object"])}, update_field_context, (SCHEMA_ID, "email")),
            ({"body": "{not json"}, set_display_label, (SCHEMA_ID,)),
            ({"body": "{}"}, set_display_label, (SCHEMA_ID,)),
            ({"body": json.dumps({"displayLabel": 42})}, set_display_label, (SCHEMA_ID,)),
        ],
    )
    def test_a_malformed_body_is_a_400_naming_what_was_wrong(self, event, handler, args, uploaded_schema):
        body = _body(handler(*args, event), expected_status=400)

        assert body["schemaId"] == SCHEMA_ID
        assert body["error"], "the 400 must carry a message the frontend can render"
        assert _sidecar() is None, "a rejected body must write nothing"

    def test_a_sidecar_that_is_not_a_json_object_is_a_500_naming_it_rather_than_being_clobbered(self, uploaded_schema):
        """A broken existing sidecar is refused, not silently replaced.

        The alternative — start from ``{}`` and write the new key — would answer
        ``200`` while **deleting** whatever contexts a user had entered, which is
        the same silent-loss failure the strict read in
        ``_apply_to_context_sidecar`` exists to prevent. Refusing is loud and
        recoverable.
        """
        _put_raw_sidecar(json.dumps(["not", "an", "object"]).encode("utf-8"))

        body = _body(set_display_label(SCHEMA_ID, _label_event("Customer master")), expected_status=500)

        assert "details" in body, "R2.1: the failure must be reported with a reason, not a fixed message"
        assert _sidecar() == ["not", "an", "object"], "the broken object is left exactly as it was"

    def test_a_sidecar_that_is_not_valid_json_is_a_500_rather_than_a_silent_overwrite(self, uploaded_schema):
        _put_raw_sidecar(b"{ this is not json")

        body = _body(update_field_context(SCHEMA_ID, "email", _context_event("PII")), expected_status=500)

        assert "details" in body
        assert (
            _client("s3").get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_sidecar_key())["Body"].read()
            == b"{ this is not json"
        )

    def test_a_field_contexts_that_is_not_an_object_is_a_500_rather_than_being_replaced(self, uploaded_schema):
        _put_raw_sidecar(json.dumps({"field_contexts": "oops", "display_name": "Customer master"}).encode("utf-8"))

        body = _body(update_field_context(SCHEMA_ID, "email", _context_event("PII")), expected_status=500)

        assert "details" in body
        assert _sidecar() == {"field_contexts": "oops", "display_name": "Customer master"}
