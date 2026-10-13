# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``upload_schema`` by example — the two body shapes, the name, and the label.

R8.5 makes this file a deliverable:

    THE test suite SHALL contain a test for Schema_Upload covering the JSON body
    path, the multipart path, a supplied Display_Label and an omitted one, with
    expectations derived from Requirements 4.1, 4.2, 4.5, 4.6 and 4.7.

Property 7 (``test_schema_upload_single_suffix_prop.py``) carries the universal
claims — one generated suffix, an id that is the basename of its key, uniqueness,
and a label stored verbatim. This file covers what a property cannot: the
individual statuses, the composed values themselves, and the two places where the
requirements are silent and this task had to choose.

**Nothing here calls the frontend's copy of anything.**
``tests/contracts/api_responses/test_schema_upload_contract.py`` builds an
uploaded-schema blob that "mirrors the frontend code" and never calls this
handler, which is the anti-pattern ``LGR-28b4d75-5`` (**template / ratified**)
and R8.7 forbid — a test whose oracle is its own copy of the producer. Held-
assertion inventory row 17 says to leave that file alone rather than grow the
fiction, so this file asserts against the handler instead.

WHAT IS PINNED HERE, AND WHY EACH ONE
-------------------------------------
1. **The composed id, spelled out.** Property 7 asserts the id's *shape*; these
   assert the actual strings, because the shape rule alone cannot show that
   ``uploaded_schema_1785844048926_20260804_114729`` normalises to its own head
   ``uploaded_schema_1785844048926`` — the single-suffix sibling this spec's row-4
   amendment adds to
   ``test_create_pipeline_target_schema_location.py`` — rather than to some other
   one-suffix name.
2. **The wizard's name survives the round trip.** R4.5's mechanism note names
   ``uploaded_schema_${Date.now()}`` (``CreatePipelinePage.tsx:328``) as the
   submitted value. The id returned for it is now **equal to it**, which is the
   agreement P29 R11.1 established when it made the wizard compute that name
   exactly once (``CreatePipelinePage.tsx:321-329``): the wizard's fallback id,
   used when a response omits ``schemaId``, then names the object that was
   actually written. The pre-p35 handler broke that on every upload.
3. **A name with nothing addressable in it is a ``400``, and writes nothing.**
   R4 does not say what to do with ``":"``. Property 7 found the case; this is
   where the answer is written down. Storing it under a made-up name was rejected:
   R4.6 makes the returned id an address, and inventing one the caller never
   asked for is the "second definition" state ``LGR-cad8032-1`` exists to prevent.
4. **The character set, and what it costs.** ``/`` cannot appear in an id — a
   name containing one would write outside ``shared/`` **and** break R4.6, since
   the returned id and the key's basename would disagree. ``.`` is excluded for a
   less obvious reason: ``_collect_schema_rows`` derives an uploaded id back out
   of its key by stripping exactly ``shared/`` and ``.json``, so a name carrying
   the substring ``.json`` would round-trip to a *different* id than the one that
   was returned. The cost is that a non-ASCII filename is stored under a
   transliterated-to-``_`` name — which is precisely what the Display_Label
   exists for, stored verbatim and never addressing anything (R4.7, R4.11).
5. **A label the sidecar write could not store is a ``200``, not a ``500``.**
   R4.2 makes "usable with no Display_Label at all" a first-class state, so a
   schema whose body landed **is** stored and a ``500`` would tell the caller to
   retry an upload that succeeded — producing a second object. The upload reports
   the label failure separately and the label stays recoverable through
   ``PUT /api/schemas/{id}/display-label``.

**Fixtures.** Shared fixtures only — ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and ``AWS_REGION`` is
**read, never set** (``LGR-3e45e65-1``).

**Validates: Requirements 4.1, 4.2, 4.5, 4.6, 4.7, 4.12, 8.5**
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

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

from handlers.schemas import get_schema, upload_schema

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
ACCOUNT_ID = "123456789012"
BOUNDARY = "----p35SchemaUploadBoundary"

SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_CONTENT = json.dumps(SCHEMA_BODY)

#: The name the Wizard_Inline_Upload sends — one client-generated component
#: (``CreatePipelinePage.tsx:328``).
WIZARD_NAME = "uploaded_schema_1785844048926"

#: What the pre-p35 handler produced from it, and a **live** id
#: (``testing-39eddb17``). Re-submitting it must normalise to its own head.
LEGACY_TWO_SUFFIX_ID = "uploaded_schema_1785844048926_20260804_114729"


def _client(service: str):
    """A client in exactly the region the handler will use (``LGR-3e45e65-1``)."""
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _json_event(name: Any, label: Any = None, content: Any = SCHEMA_CONTENT, *, omit_label: bool = True) -> dict:
    payload: dict[str, Any] = {"name": name, "content": content}
    if not omit_label or label is not None:
        payload["displayLabel"] = label
    return {"httpMethod": "POST", "headers": {"Content-Type": "application/json"}, "body": json.dumps(payload)}


def _multipart_event(filename: str, label: str | None = None, content: str = SCHEMA_CONTENT) -> dict:
    parts = [
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/json\r\n\r\n{content}\r\n"
    ]
    if label is not None:
        parts.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="displayLabel"\r\n\r\n{label}\r\n')
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        "body": "".join(parts) + f"--{BOUNDARY}--\r\n",
    }


def _body(response: dict, expected_status: int = 200) -> dict:
    assert response["statusCode"] == expected_status, f"expected {expected_status}, got {response}"
    return json.loads(response["body"])


def _keys(prefix: str = "") -> list[str]:
    s3 = _client("s3")
    found: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET, Prefix=prefix):
        found.extend(obj["Key"] for obj in page.get("Contents", []))
    return sorted(found)


def _stored(key: str) -> Any:
    return json.loads(_client("s3").get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=key)["Body"].read())


@pytest.fixture
def upload_env(mock_s3_buckets, monkeypatch):
    """The shared moto bucket and the two addresses the handler resolves."""
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    return TARGET_SCHEMAS_BUCKET


# ---------------------------------------------------------------------------
# R4.5 / R4.6 — one suffix, and the id IS the basename
# ---------------------------------------------------------------------------
class TestTheStoredNameCarriesOneGeneratedComponent:
    """R4.5, with the composed strings spelled out rather than shape-matched."""

    def test_the_wizards_name_is_stored_under_itself_rather_than_gaining_a_second_stamp(self, upload_env):
        """The defect R4.5 exists for, closed.

        The wizard sends ``uploaded_schema_1785844048926``; the pre-p35 handler
        appended ``%Y%m%d_%H%M%S`` and stored
        ``uploaded_schema_1785844048926_20260804_114729``. Keeping the client's
        single component also restores P29 R11.1's agreement: the id returned is
        the name uploaded under, so the wizard's fallback names the object that
        exists.

        **Validates: Requirements 4.5, 4.6**
        """
        body = _body(upload_schema(_json_event(WIZARD_NAME)))

        assert body["schemaId"] == WIZARD_NAME
        assert body["location"] == f"s3://{TARGET_SCHEMAS_BUCKET}/shared/{WIZARD_NAME}.json"
        assert _keys("shared/") == [f"shared/{WIZARD_NAME}.json"]
        assert _stored(f"shared/{WIZARD_NAME}.json") == SCHEMA_BODY

    def test_the_legacy_two_suffix_name_normalises_to_its_own_single_suffix_head(self, upload_env):
        """Re-submitting the pre-fix shape stores it under one component, not three.

        ``uploaded_schema_1785844048926_20260804_114729`` →
        ``uploaded_schema_1785844048926``. The leftmost generated component is the
        one the client itself chose; the second was the handler's, and R4.5
        removes it.

        **Validates: Requirement 4.5**
        """
        body = _body(upload_schema(_json_event(LEGACY_TWO_SUFFIX_ID)))

        assert body["schemaId"] == WIZARD_NAME, (
            "the legacy double-timestamp shape must normalise to its single-suffix head"
        )

    def test_a_human_name_with_no_stamp_gains_exactly_one(self, upload_env):
        """The Schemas_Page path: the name is a filename basename (R4.2).

        **Validates: Requirements 4.2, 4.5, 4.6**
        """
        body = _body(upload_schema(_json_event("customers")))
        schema_id = body["schemaId"]

        assert schema_id.startswith("customers_")
        suffix = schema_id.removeprefix("customers_")
        assert suffix.isdigit() and len(suffix) >= 9, f"one epoch-milliseconds component, got {suffix!r}"
        assert _keys("shared/") == [f"shared/{schema_id}.json"]

    def test_a_human_name_ending_in_a_short_number_is_not_mistaken_for_a_stamp(self, upload_env):
        """``orders_2024``'s trailing digits are the user's, so it still gets a suffix.

        The floor on what counts as a generated component is what keeps a
        deliberate version or year in a filename from being read as a machine
        stamp and leaving the upload with no uniqueness component at all.

        **Validates: Requirement 4.5**
        """
        schema_id = _body(upload_schema(_json_event("orders_2024")))["schemaId"]

        assert schema_id.startswith("orders_2024_"), f"the user's trailing 2024 must survive, got {schema_id!r}"

    def test_a_second_upload_of_one_name_gets_its_own_id_and_does_not_overwrite(self, upload_env):
        """R4.7's uniqueness, at the granularity Property 7 generalises.

        The pre-p35 suffix had second resolution and there was no collision
        check, so two uploads inside one second composed one key and the second
        ``put_object`` replaced the first schema's body silently.

        **Validates: Requirement 4.7**
        """
        first = _body(upload_schema(_json_event(WIZARD_NAME)))["schemaId"]
        second = _body(upload_schema(_json_event(WIZARD_NAME)))["schemaId"]

        assert first != second
        assert _keys("shared/") == sorted([f"shared/{first}.json", f"shared/{second}.json"])


class TestTheIdIsAlwaysTheBasenameOfTheKey:
    """R4.6 and ``LGR-cad8032-1``: one address, composed once."""

    @pytest.mark.parametrize(
        ("submitted", "expected_base"),
        [
            ("shared/orders.json", "shared_orders_json"),
            ("../../etc/passwd", "etc_passwd"),
            ("my.schema.json", "my_schema_json"),
            ("Kundendaten Ünïcodé", "Kundendaten_n_cod"),
            ("  padded name  ", "padded_name"),
        ],
    )
    def test_an_unsafe_name_is_reduced_to_something_shared_id_json_can_address(
        self, submitted, expected_base, upload_env
    ):
        """A name that would escape ``shared/`` or break the round trip is reduced.

        ``/`` and ``.`` cannot appear in an id — the first would write outside
        ``shared/`` and the second would make ``_collect_schema_rows``' strip of
        ``.json`` derive a different id than the one returned. Both would leave
        the writer and the reader with two definitions of one address, which is
        exactly what ``LGR-cad8032-1`` (**template / ratified**) exists to
        prevent.

        **Validates: Requirement 4.6**
        """
        schema_id = _body(upload_schema(_json_event(submitted)))["schemaId"]

        assert schema_id.startswith(f"{expected_base}_"), (
            f"{submitted!r} must reduce to {expected_base!r} plus one suffix, got {schema_id!r}"
        )
        assert _keys("shared/") == [f"shared/{schema_id}.json"], (
            "the object must be the one the returned id names, under shared/ and nowhere else"
        )
        # The round trip the listing will perform: strip `shared/` and `.json`.
        assert _keys("shared/")[0][len("shared/") : -len(".json")] == schema_id

    def test_a_name_over_the_cap_is_truncated_and_still_addresses_its_own_object(self, upload_env):
        """S3 keys are bounded, and the sidecar's key is longer than the body's."""
        schema_id = _body(upload_schema(_json_event("a" * 300)))["schemaId"]

        assert len(schema_id) < 300
        assert _keys("shared/") == [f"shared/{schema_id}.json"]

    @pytest.mark.parametrize("submitted", [":", "***", "///", "   "])
    def test_a_name_with_nothing_addressable_is_a_400_that_writes_nothing(self, submitted, upload_env):
        """The case Property 7 discovered — R4 is silent, so this is the answer.

        Storing it under an invented name was rejected: the id is an address
        (R4.6), and inventing one the caller never submitted is the second
        definition ``LGR-cad8032-1`` forbids. A ``400`` naming the permitted
        character set is recoverable in one edit.

        **Validates: Requirement 4.6**
        """
        body = _body(upload_schema(_json_event(submitted)), expected_status=400)

        assert body["error"], "the 400 must carry a message the frontend can render"
        assert _keys() == [], f"a refused upload must write nothing — found {_keys()}"


# ---------------------------------------------------------------------------
# R4.1 / R4.2 / R4.7 / R4.12 — the optional label, on both shapes
# ---------------------------------------------------------------------------
class TestTheOptionalDisplayLabelOnTheJsonPath:
    """R4.1 and R4.2: supplied, and omitted."""

    def test_a_supplied_label_is_stored_verbatim_in_the_sidecar_beside_field_contexts(self, upload_env):
        """R4.12's address, R4.7's verbatim storage.

        **Validates: Requirements 4.1, 4.7, 4.12**
        """
        body = _body(upload_schema(_json_event("customers", "  Customer master  ")))
        schema_id = body["schemaId"]

        assert body["displayLabel"] == "  Customer master  "
        assert _stored(f"schemas/uploaded/{schema_id}-metadata.json") == {"display_name": "  Customer master  "}
        # And the surface reads it back, which is what R4.8 renders.
        assert _body(get_schema(schema_id))["displayLabel"] == "  Customer master  "

    @pytest.mark.parametrize("label", [None, "", "   "])
    def test_an_omitted_or_empty_label_leaves_no_display_name_at_all(self, label, upload_env):
        """R4.2: the schema is fully usable with no Display_Label.

        Consistent with task 8.5's choice on the sibling endpoint — an empty label
        is the *absence* of one rather than a stored ``""``, so there is one
        representation of "no label" and every reader falls back to the id (R4.8).
        No sidecar is written at all, because an object carrying nothing is an
        object no listing surfaces.

        **Validates: Requirements 4.2, 4.8**
        """
        event = _json_event("customers") if label is None else _json_event("customers", label, omit_label=False)
        body = _body(upload_schema(event))
        schema_id = body["schemaId"]

        assert "displayLabel" not in body, f"an unlabelled upload must claim no label — got {body}"
        assert _keys("schemas/uploaded/") == [], f"no sidecar may be written — found {_keys('schemas/uploaded/')}"
        assert "displayLabel" not in _body(get_schema(schema_id)), (
            "R4.8: with no label stored the detail surface omits the key, so the client renders the id"
        )

    def test_two_schemas_may_carry_the_same_label(self, upload_env):
        """R4.7: uniqueness belongs to the id, never to the label.

        **Validates: Requirement 4.7**
        """
        first = _body(upload_schema(_json_event("customers", "Customer master")))
        second = _body(upload_schema(_json_event("clients", "Customer master")))

        assert first["schemaId"] != second["schemaId"]
        assert first["displayLabel"] == second["displayLabel"] == "Customer master"

    def test_a_non_string_label_is_a_400_rather_than_being_coerced(self, upload_env):
        """``str(42)`` would store a label the caller never wrote."""
        body = _body(upload_schema(_json_event("customers", 42, omit_label=False)), expected_status=400)

        assert "displayLabel" in body["error"]
        assert _keys() == [], "a rejected body must write nothing"

    def test_an_explicit_null_label_is_the_absence_of_one(self, upload_env):
        """How a JSON client spells "no value"."""
        body = _body(upload_schema(_json_event("customers", None, omit_label=False)))

        assert "displayLabel" not in body
        assert _keys("schemas/uploaded/") == []


class TestTheMultipartPathCarriesTheSameThings:
    """R8.5's second shape — a different parser reaching one writer."""

    def test_the_filename_supplies_the_name_and_a_form_field_supplies_the_label(self, upload_env):
        """**Validates: Requirements 4.1, 4.2, 4.5, 4.6, 4.12**"""
        body = _body(upload_schema(_multipart_event("customers.json", "Customer master")))
        schema_id = body["schemaId"]

        assert schema_id.startswith("customers_")
        assert _keys("shared/") == [f"shared/{schema_id}.json"]
        assert _stored(f"shared/{schema_id}.json") == SCHEMA_BODY
        assert body["displayLabel"] == "Customer master"
        assert _stored(f"schemas/uploaded/{schema_id}-metadata.json") == {"display_name": "Customer master"}

    def test_a_multipart_upload_with_no_label_part_stores_no_display_name(self, upload_env):
        """R4.2 on the shape that had no way to carry a label before this task."""
        body = _body(upload_schema(_multipart_event("customers.json")))

        assert "displayLabel" not in body
        assert _keys("schemas/uploaded/") == []

    def test_a_multipart_label_is_stored_verbatim_including_its_padding(self, upload_env):
        """R4.7: only the single line break the boundary needs is removed.

        A part's body ends with the ``\\r\\n`` that belongs to the delimiter. That
        one break is dropped and nothing else, so a label's own leading and
        trailing whitespace survives — the same verbatim rule the JSON path and
        ``set_display_label`` follow.
        """
        body = _body(upload_schema(_multipart_event("customers.json", "  Customer master  ")))

        assert body["displayLabel"] == "  Customer master  "

    def test_the_label_part_is_read_even_when_it_precedes_the_file_part(self, upload_env):
        """Field order is a client's choice, not a contract."""
        reordered = {
            "httpMethod": "POST",
            "headers": {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
            "body": (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="displayLabel"\r\n\r\nCustomer master\r\n'
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; filename="customers.json"; name="file"\r\n'
                f"Content-Type: application/json\r\n\r\n{SCHEMA_CONTENT}\r\n"
                f"--{BOUNDARY}--\r\n"
            ),
        }

        body = _body(upload_schema(reordered))

        assert body["schemaId"].startswith("customers_"), (
            "the file part's name must come from `name=file`, not from the `name=` inside `filename=`"
        )
        assert body["displayLabel"] == "Customer master"

    def test_a_snake_case_label_field_is_accepted_too(self, upload_env):
        """A hand-written form is as likely to spell it ``display_label``."""
        event = {
            "httpMethod": "POST",
            "headers": {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
            "body": (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="customers.json"\r\n'
                f"Content-Type: application/json\r\n\r\n{SCHEMA_CONTENT}\r\n"
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="display_label"\r\n\r\nCustomer master\r\n'
                f"--{BOUNDARY}--\r\n"
            ),
        }

        assert _body(upload_schema(event))["displayLabel"] == "Customer master"

    def test_a_multipart_body_with_no_file_part_is_a_400(self, upload_env):
        """Unchanged from the pre-p35 form, restated because the parser was rewritten."""
        event = {
            "httpMethod": "POST",
            "headers": {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
            "body": (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="displayLabel"\r\n\r\nCustomer master\r\n'
                f"--{BOUNDARY}--\r\n"
            ),
        }

        assert _body(upload_schema(event), expected_status=400)["error"]
        assert _keys() == []

    def test_invalid_json_in_the_uploaded_file_is_a_400(self, upload_env):
        body = _body(upload_schema(_multipart_event("customers.json", content="{not json")), expected_status=400)

        assert "Invalid JSON" in body["error"]
        assert _keys() == []


class TestABodyThatStoredButALabelThatDidNotIsStillAnUpload:
    """R4.2: the schema is stored and usable, so the upload is not a failure."""

    def test_a_failed_label_write_reports_itself_and_leaves_the_schema_stored(self, upload_env, monkeypatch):
        """A ``500`` here would tell the caller to retry an upload that succeeded.

        The retry would produce a **second** object — one more schema than the user
        asked for, which is worse than a schema with no label. So this degrades to
        exactly the state R4.2 already makes first-class, names the reason, and
        leaves the label recoverable through
        ``PUT /api/schemas/{id}/display-label``.
        """
        import handlers.schemas as schemas_handler

        def _explode(schema_id, apply_change):
            raise RuntimeError("sidecar unavailable")

        monkeypatch.setattr(schemas_handler, "_apply_to_context_sidecar", _explode)

        body = _body(upload_schema(_json_event("customers", "Customer master")))
        schema_id = body["schemaId"]

        assert "displayLabel" not in body, "the response must not claim a label it did not store"
        assert "sidecar unavailable" in body["displayLabelError"]
        assert _keys("shared/") == [f"shared/{schema_id}.json"], "the schema itself is stored"
        assert _stored(f"shared/{schema_id}.json") == SCHEMA_BODY


class TestTheUnchangedRefusals:
    """The two ``400``s the JSON path already had, restated after the rewrite."""

    def test_a_missing_name_is_a_400(self, upload_env):
        assert _body(upload_schema(_json_event("")), expected_status=400)["error"] == "Schema name is required"

    def test_a_missing_content_is_a_400(self, upload_env):
        body = _body(upload_schema(_json_event("customers", content="")), expected_status=400)
        assert body["error"] == "Schema content is required"

    def test_invalid_json_content_is_a_400(self, upload_env):
        body = _body(upload_schema(_json_event("customers", content="{not json")), expected_status=400)
        assert "Invalid JSON" in body["error"]
        assert _keys() == []
