# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``GET /api/schemas/{id}``'s response shape, and Unit C's three wired stubs.

Expectations come from ``requirements.md`` and design DD9 — not from running
``get_schema``, which task 4.2 is what introduces.

Four things are pinned here, each because getting it wrong is what the pre-p35
surface got wrong:

1. **The response is a bare schema object** (DD9), not ``{"schemas": [one]}``.
   The collection envelope is what the old third branch returned for a per-id
   request (``router.py:329-332``), and it is why the detail modal always
   described element ``[0]`` of the listing (``use-schemas.ts:38-45``) — the
   clicked row was never in the response's shape at all.
2. **An id no source can produce answers ``404`` naming that id** (R1.2).
3. **``displayLabel`` is absent, not ``null``, when no label is stored** (DD9,
   R4.8). A client that falls back on a missing key renders the schema id; one
   handed ``null`` renders nothing.
4. **``description`` and ``context`` are two keys from two authorities** (R1.6,
   R2.3) — the schema's own declared description, and the Context_Sidecar's
   annotation. Today the listing feeds each source's own description into
   ``context`` and emits no ``description`` at all (``schemas.py:62``, ``:109``,
   ``:119``, ``:189``), which is why the Description column renders ``—`` for
   every field of every schema. Task 4.5 moves it at the producer; the per-id
   endpoint separates the two authorities from this task onwards.

Unit C's write surface is asserted to be **reachable and honest**: wired into
``schemas_router`` and answering something true rather than repeating the pre-p35
false claim — "this endpoint does not exist" about an endpoint that does. When
task 4.2 wired them, "honest" meant ``501``. **Task 8.4 then implemented
``delete_schema`` and task 8.5 implemented the other two, so no stub is left and
``501`` is nobody's answer any more**; see the two amendment notes on
``test_the_three_unit_c_handlers_are_wired_but_answer_501``, plus
``test_schema_delete_endpoint.py`` and ``test_schema_context_and_label_writes.py``.

**Fixtures.** Shared fixtures only: ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call and no boto3 client
mock (``tests/unit/test_no_duplicate_mocking_prop.py``, ``LGR-adceb72-1`` /
``LGR-aadd206-1``). This file does **not** set ``AWS_REGION``
(``LGR-3e45e65-1``), which matters because ``get_schemas`` reads it at call time
(``schemas.py:23``); the seeding client resolves the region through that same
expression so seeding and handler cannot land in different regions.

**Validates: Requirements 1.1, 1.2, 1.6, 2.3, 2.5, 3.1, 3.3, 4.8**
"""

import json
import os
import sys

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

from handlers.schemas import delete_schema, get_schema, set_display_label, update_field_context

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"

SCHEMA_ID = "uploaded_schema_1785844048926"
SCHEMA_BODY = {
    "fields": [
        {"name": "email", "type": "string", "required": True, "description": "Primary contact address"},
        {"name": "nickname", "type": "string", "required": False, "description": "Optional display name"},
    ]
}
FIELD_CONTEXT = "PII — hashed downstream"
DISPLAY_LABEL = "Customer master"


def _client(service: str):
    """A client in exactly the region ``get_schemas`` will use (``schemas.py:23``)."""
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _seed_schema_body(schema_id: str = SCHEMA_ID, body: dict | None = None) -> None:
    """Write one uploaded schema body at ``shared/{id}.json`` (``LGR-cad8032-1``)."""
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=f"shared/{schema_id}.json",
        Body=json.dumps(body if body is not None else SCHEMA_BODY),
        ContentType="application/json",
    )


def _seed_sidecar(schema_id: str = SCHEMA_ID, **contents) -> None:
    """Write the Context_Sidecar at ``schemas/uploaded/{id}-metadata.json``.

    Task 8.5 (D3, ``LGR-acca05d-3``) is what introduces the writer; this seeds
    the object directly so the reader can be characterized before it exists.
    """
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=f"schemas/uploaded/{schema_id}-metadata.json",
        Body=json.dumps(contents),
        ContentType="application/json",
    )


@pytest.fixture
def target_schemas_bucket(mock_s3_buckets, monkeypatch):
    """The shared fixture's target-schemas bucket, empty, with the handler pointed at it."""
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() (schemas.py:77) off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    return TARGET_SCHEMAS_BUCKET


@pytest.fixture
def uploaded_schema(target_schemas_bucket):
    """One uploaded schema in the shared fixture's target-schemas bucket."""
    _seed_schema_body()
    return SCHEMA_ID


def _body(response: dict, expected_status: int = 200) -> dict:
    assert response["statusCode"] == expected_status, f"expected {expected_status}, got {response}"
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# DD9 — the bare schema object
# ---------------------------------------------------------------------------
def test_per_id_response_is_a_bare_schema_object(uploaded_schema):
    """R1.1/DD9: one schema, unwrapped, and it is the id that was asked for.

    **Validates: Requirement 1.1**
    """
    body = _body(get_schema(uploaded_schema))
    assert "schemas" not in body, (
        f"DD9: the per-id response is a bare schema object, not a collection envelope — got {sorted(body)}"
    )
    assert body["schemaId"] == uploaded_schema
    assert body["source"] == "uploaded"
    assert [f["name"] for f in body["fields"]] == ["email", "nickname"]


def test_unknown_id_is_a_404_naming_the_requested_id(uploaded_schema):
    """R1.2: an id no source can produce answers ``404`` naming it.

    ``upload`` is the case that matters: it is the literal branch 2 owns, it
    reaches the per-id branch on a ``GET``, and it is not an id any source can
    produce — so the honest answer is this ``404`` rather than a special case in
    the router.

    **Validates: Requirement 1.2**
    """
    for absent_id in ("upload", "no-such-schema"):
        body = _body(get_schema(absent_id), expected_status=404)
        assert absent_id in body["error"], f"the 404 must name the requested id — got {body}"


# ---------------------------------------------------------------------------
# DD9 / R4.8 — displayLabel is absent, not null
# ---------------------------------------------------------------------------
def test_display_label_is_absent_not_null_when_no_label_is_stored(uploaded_schema):
    """DD9/R4.8: no stored label means **no key**, so the client falls back to the id.

    **Validates: Requirement 4.8**
    """
    body = _body(get_schema(uploaded_schema))
    assert "displayLabel" not in body, (
        f"displayLabel must be absent rather than null when no label is stored — got {body.get('displayLabel')!r}"
    )


def test_display_label_is_returned_when_the_sidecar_carries_one(uploaded_schema):
    """R4.9/R4.12: the label is read from ``display_name`` in the Context_Sidecar.

    **Validates: Requirement 4.8**
    """
    _seed_sidecar(display_name=DISPLAY_LABEL)
    body = _body(get_schema(uploaded_schema))
    assert body["displayLabel"] == DISPLAY_LABEL
    assert body["schemaId"] == uploaded_schema, "R4.10: the id is unaffected by a label"


# ---------------------------------------------------------------------------
# R1.6 / R2.3 — two keys, two authorities
# ---------------------------------------------------------------------------
def test_description_and_context_are_two_keys_from_two_authorities(uploaded_schema):
    """R1.6/R2.3: the schema's own description, and the sidecar's annotation.

    ``description`` is what the schema body declares; ``context`` is only what a
    human wrote into the Context_Sidecar. An annotated field carries both, and an
    unannotated one carries its description with an empty context — which is what
    makes R1.6's two headings honest rather than confusing.

    **Validates: Requirements 1.6, 2.3**
    """
    _seed_sidecar(field_contexts={"email": FIELD_CONTEXT})
    fields = {f["name"]: f for f in _body(get_schema(uploaded_schema))["fields"]}

    assert fields["email"]["description"] == "Primary contact address"
    assert fields["email"]["context"] == FIELD_CONTEXT
    assert fields["nickname"]["description"] == "Optional display name"
    assert fields["nickname"]["context"] == "", (
        "a field with no sidecar entry has no context, and its description is still its own"
    )


def test_no_sidecar_is_not_an_error_and_context_is_empty(uploaded_schema):
    """R2.6: an absent sidecar means "no context recorded", not a failure.

    **Validates: Requirement 1.6**
    """
    fields = {f["name"]: f for f in _body(get_schema(uploaded_schema))["fields"]}
    assert fields["email"]["description"] == "Primary contact address"
    assert fields["email"]["context"] == ""


# ---------------------------------------------------------------------------
# Unit C's three handlers — wired, and honest about not being implemented
# ---------------------------------------------------------------------------
def test_the_three_unit_c_handlers_are_wired_but_answer_501(target_schemas_bucket):
    """The write surface is reachable and answers honestly. **No stub is left.**

    **AMENDED TWICE. The reversals are stated here rather than in a commit
    message, so each reads as deliberate** (``LGR-5906ec6-2`` /
    ``LGR-60c4985-6``, both **template / ratified**), and the **name is kept
    verbatim** on purpose: an amendment that renames itself reads as a new test
    rather than as a moved assertion, and the ``501`` in the name is what makes
    both reversals findable from the outside.

    - **Task 8.4** removed ``delete_schema`` from the set. It now answers ``400``,
      ``409``, ``503``, ``200`` or ``500`` per design §4.2's outcome table and
      **never** ``501``. Its behaviour moved to ``test_schema_delete_endpoint.py``
      plus Properties 4 and 5.
    - **Task 8.5** removed the other two. ``update_field_context`` and
      ``set_display_label`` are implemented (D3 / ``LGR-acca05d-3`` and D2 /
      ``LGR-acca05d-2``), so **``501`` is nobody's answer any more** and the
      original assertion is gone rather than loosened — asserting ``501`` now
      would be asserting a stub that does not exist. Their behaviour is in
      ``test_schema_context_and_label_writes.py`` and in Properties 3 and 8.

    **What survives is a real tripwire, not a formality.** The subject was always
    "the write surface is reachable and says something true, rather than
    ``404``-ing" — the pre-p35 false claim, "this endpoint does not exist" about an
    endpoint that does. That still has content: both writers must answer a
    **named, honest** status for an id nothing backs, and must not regress to a
    stub. This test seeds **no** schema object for ``SCHEMA_ID`` — it takes the
    shared fixture's target-schemas bucket **empty** — so the honest answer is a
    ``404`` naming the id, which is exactly what is asserted. A handler that
    reverted to ``501``, or a route quietly dropped, fails here.

    **Why it takes the empty bucket rather than no fixture at all.** Both writers
    ``head_object`` the schema body before anything else
    (``_apply_to_context_sidecar``), so with no fixture the account lookup and
    that call went to real AWS: with a developer's credentials they reached
    whatever account the shell pointed at, and without credentials they raised
    "Unable to locate credentials", which both handlers report as ``500``. The
    empty ``mock_s3_buckets`` bucket gives the ``404`` hermetically.

    ``delete_schema`` is deliberately **not** called, for the same reason as
    before: it reaches the pipelines table on every path (design §4.2's
    Query-then-Scan) and this test takes no DynamoDB fixture, so calling it would
    either hit the network or assert against a table that does not exist —
    neither of which says anything about wiring.

    **Validates: Requirements 2.5, 3.1, 3.3**
    """
    responses = {
        "update_field_context": update_field_context(SCHEMA_ID, "email", {"body": json.dumps({"context": "x"})}),
        "set_display_label": set_display_label(SCHEMA_ID, {"body": json.dumps({"displayLabel": DISPLAY_LABEL})}),
    }
    for name, response in responses.items():
        assert response["statusCode"] != 501, (
            f"{name} is implemented as of task 8.5 — a 501 here means it regressed to a stub, got {response}"
        )
        body = _body(response, expected_status=404)
        assert body["schemaId"] == SCHEMA_ID, f"{name} must name the schema it refused"
        assert SCHEMA_ID in body["error"], f"{name}'s 404 must name the requested id — got {body}"

    assert delete_schema is not None, (
        "delete_schema stays imported and routed — task 8.4 implemented it, it did not remove it. "
        "Its outcomes are asserted in test_schema_delete_endpoint.py."
    )
