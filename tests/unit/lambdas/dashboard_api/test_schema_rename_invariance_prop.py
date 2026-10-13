# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — a rename changes the label and nothing else.

# Feature: p35-schema-surfaces, Property 3: A rename changes the label and nothing else

For any stored schema and any **sequence** of Display_Label writes, the schema's
id, its backing object's key, the object's **bytes**, and every referencing
pipeline's ``target_schema_name`` are byte-identical before and after — and no
``copy_object``, no ``delete_object`` and no ``put_object`` against the schema
**body** occurs. The only write the sequence is allowed to make is a
``put_object`` against the Context_Sidecar,
``schemas/uploaded/{schema_id}-metadata.json``.

**Written before any rename code exists, and that ordering is the mitigation
rather than a preference.** Design §5 risk 7: nothing type-distinguishes a
Display_Label from a schema id — both are ``str`` — and one assignment letting a
label reach ``target_schema_name`` breaks ``schema_mapping``'s by-name fallback
**six steps away, with no other symptom**.
``schema_mapping/lambda_function.py:249-250`` falls through to
``get_uploaded_json_schema(target_schema_name, pipeline_id)`` for a record with no
``target_schema_s3_key``; that function does not look the name up anywhere, it
*interpolates* it into six candidate keys (``:296`` →
``shared/target_schema_resolution.py:407``, whose preferred candidate is
``shared/{schema_name}.json``) and probes each with ``get_object``. A label there
misses all six and raises ``Could not find uploaded schema '{name}' in any
expected location`` (``:317-319``). A comment cannot fence that. Written now, the
property exists before there is any code for it to be written *from*.

WHAT IS ASSERTED, AND WHERE
---------------------------
On the **object and the pipeline attribute**, never on the handler's return
value. A handler that answered ``200`` while relocating the object would satisfy
any assertion about its response body, and a relocation is precisely what
``LGR-acca05d-2`` (**template / ratified**) rejected and what
``LGR-78ce0b7-14`` (**template / ratified**) turns into a *raise* rather than a
quiet degradation: a present ``target_schema_s3_key`` is a claim that an object
exists, so moving the body leaves every pipeline naming the old key failing its
target-schema read with ``target_schema_object_missing``.

**The absence assertions are the point, not a garnish.** A property that only
checked "the label is readable afterwards" passes against a copy-and-delete
implementation — the user gets a renamed object, which is what "rename" usually
means, and the breakage lands in a different Lambda on the next run. So the
recorded S3 calls are asserted to contain **no** relocation at all and **no**
write whose key is not the sidecar. ``_forbidden_writes`` is that check, and
``TestTheTripwireIsArmed`` below proves it is armed rather than vacuously silent.

THE THREE CLAUSES, AND WHICH ONE IS RED TODAY
---------------------------------------------
``set_display_label`` is a ``501`` stub (``handlers/schemas.py:1357``, wired by
task 4.2, implemented by task 8.5), so:

===========================================  ==========================
clause                                       against the stub
===========================================  ==========================
1. id, key, bytes, ``target_schema_name``    passes — nothing happens
   are byte-identical
2. no relocation, and no write that is not   passes — no call at all
   the sidecar
3. the label **landed**: the sidecar's       **RED** — the stub writes
   ``display_name`` is the last label        nothing
   written (R4.9, R4.12)
===========================================  ==========================

Clause 3 is not padding and it is not scope creep onto Property 4's territory.
Without it the property is **satisfied by a handler that does nothing**, and
"changes the label and nothing else" would be provable by never changing
anything — the same shape as a generator that reaches its target case zero times
and passes anyway (tasks 3.1 and 4.3 both hit that, at 0 of 200). Clause 3 is
what makes clauses 1 and 2 claims about a *rename* rather than about a no-op, and
it is derived from R4.9 ("SHALL persist the new Display_Label") and R4.12
("as a ``display_name`` key alongside ``field_contexts``"), not from any code.

``TestTheSpecificationIsSatisfiable`` proves the three clauses can hold at once,
so today's red reads as "not implemented yet" rather than as "contradictory".

THE REQUEST CONTRACT THIS FILE PINS
-----------------------------------
``PUT /api/schemas/{id}/display-label`` with body ``{"displayLabel": "<label>"}``
— camelCase, matching the ``displayLabel`` key in design §4.5's example response
and the ``{"context": …}`` precedent the sibling context ``PUT`` already uses
(``use-schemas.ts:121-123``). Nothing in the requirements or the design names the
request key, so a property written first is where it gets named. **Task 8.5 must
accept this key**, and task 8.7's ``useSetDisplayLabel`` must send it.

An **empty or whitespace-only** label is deliberately NOT generated. R4 does not
say whether one clears the label or is refused with a ``400``, so the property
does not assume an answer it would then be enforcing. That silence belongs to
whoever specifies it.

FIXTURES, REGION, AND THE ONE THING TASK 8.5 MUST NOT DO
--------------------------------------------------------
Shared fixtures only — ``mock_s3_buckets`` and ``mock_dynamodb_tables`` from
``tests/fixtures/conftest.py``. There is no ``moto.mock_aws()`` call and no boto3
client mock in this file (``tests/unit/test_no_duplicate_mocking_prop.py``), and
it does **not** set ``AWS_REGION`` (``LGR-3e45e65-1``): every client the harness
builds resolves the region through ``os.environ.get("AWS_REGION", "us-west-2")``,
which is ``schemas.py:23`` verbatim, so the seeding and the handler cannot land in
different backends.

**The DynamoDB half carries an asymmetry worth stating.**
``mock_dynamodb_tables`` creates its tables in ``us-east-1``
(``tests/fixtures/conftest.py:214``) while the handler's expression resolves
``us-west-2``, and moto's DynamoDB backend is region-scoped like Glue's. The
fixture below therefore binds ``schemas.pipelines_table`` to the fixture's table
handle — the same move ``test_create_pipeline_target_schema_location.py:139``
makes for ``pipelines_crud`` — so that a wrong implementation's write lands where
this property reads it. **Task 8.4 / 8.5 must reach the pipelines table through a
module-level handle**, as ``pipelines_crud`` does, rather than constructing a
resource inline: an inline resource in the resolved region would find no table
and raise ``ResourceNotFoundException``, which is at least loud, but it would
move R4.11's assertion out of this property's reach.

The recorder is a **pass-through spy**, not a mock: ``_recorded_s3_calls`` wraps
``botocore.client.BaseClient._make_api_call``, appends ``(operation, params)``
and then calls the original, so every observed call is a real call into moto's
backend. It sits above moto's ``before-send`` stubber, which is why it sees
everything regardless of how the handler constructs its client.

**Validates: Requirements 4.10, 4.11, 4.12, 8.8**
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, NamedTuple

import boto3
import pytest
from botocore.client import BaseClient
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import event as _hypothesis_event
from hypothesis import strategies as st

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

from handlers import schemas as schemas_handler

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
ACCOUNT_ID = "123456789012"


# ---------------------------------------------------------------------------
# The two addresses, written out from the requirements rather than imported
# ---------------------------------------------------------------------------
def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention.

    Spelled out here rather than imported so the property's oracle is the
    requirement and not the code under test. ``upload_schema`` writes here and
    ``get_schemas`` derives an id by stripping exactly ``shared/`` and ``.json``
    (``schemas.py:85``), which is why this key is the schema's identity and why
    R4.10 freezes it.
    """
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{schema_id}-metadata.json`` — R4.12 verbatim.

    Keyed on the **frozen** id, so the label's own address cannot go stale when
    the label changes. This is the one key a rename may write.
    """
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _client(service: str):
    """A client in exactly the region the handler will use (``LGR-3e45e65-1``).

    ``os.environ.get("AWS_REGION", "us-west-2")`` is ``schemas.py:23`` verbatim.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


# ---------------------------------------------------------------------------
# The pass-through spy, and the forbidden-call oracle
# ---------------------------------------------------------------------------
#: Anything that moves or removes an object. R4.10 forbids all of it — "SHALL NOT
#: copy, move or delete **any** object" — not merely against the body, because
#: a copy-and-delete of the body is exactly the rejected implementation
#: (``LGR-acca05d-2``). ``UploadPartCopy`` and ``DeleteObjects`` are the
#: multipart and batch spellings of the same two intentions; leaving them out
#: would let the forbidden operation through under a different name.
_RELOCATING_OPERATIONS = ("CopyObject", "UploadPartCopy", "DeleteObject", "DeleteObjects")

#: Anything that creates or replaces an object's bytes.
_WRITING_OPERATIONS = ("PutObject", "CreateMultipartUpload")


@contextlib.contextmanager
def _recorded_s3_calls():
    """Record every S3 API call made inside the block, then call through.

    A spy, not a mock: the wrapper appends ``(operation_name, params)`` and
    returns ``original(...)``, so every call still reaches moto's backend and
    every response is moto's. It wraps ``BaseClient._make_api_call``, which sits
    **above** moto's ``before-send`` stubber, so it observes calls from clients
    the handler constructs itself — which is the only way to assert the
    *absence* of a call the handler was never asked to make.
    """
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        if self.meta.service_model.service_name == "s3":
            recorded.append((operation_name, dict(api_params)))
        return original(self, operation_name, api_params)

    BaseClient._make_api_call = recording
    try:
        yield recorded
    finally:
        BaseClient._make_api_call = original


def _named_key(params: dict[str, Any]) -> str:
    """Whatever a call names, for a readable failure message."""
    for field in ("Key", "CopySource", "Delete"):
        if field in params:
            return f"{field}={params[field]!r}"
    return "<no key in params>"


def _forbidden_writes(calls: list[tuple[str, dict[str, Any]]], sidecar_key: str) -> list[str]:
    """Every recorded call R4.10 / R4.11 / R4.12 forbid a rename from making.

    Two rules, and the second is the tighter one:

    1. **No relocation, anywhere.** R4.10's "SHALL NOT copy, move or delete any
       object".
    2. **The only permitted write is the sidecar.** Not "not the body" —
       *only the sidecar*. That is what catches R4.11's failure as well as
       R4.10's: an implementation that derived a key from the label would write
       ``shared/{label}.json``, which is neither the body nor the sidecar, and a
       "not the body" rule would wave it through.

    A ``get_object`` / ``head_object`` / ``list_objects_v2`` is not forbidden —
    reading the existing sidecar in order to preserve ``field_contexts`` beside
    the new ``display_name`` is the correct implementation (R4.12).
    """
    offending: list[str] = []
    for operation, params in calls:
        if operation in _RELOCATING_OPERATIONS:
            offending.append(f"{operation}({_named_key(params)})")
        elif operation in _WRITING_OPERATIONS and params.get("Key") != sidecar_key:
            offending.append(f"{operation}(Key={params.get('Key')!r})")
    return offending


# ---------------------------------------------------------------------------
# The fixture — shared moto backends, bound to the handler
# ---------------------------------------------------------------------------
SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
#: ``sort_keys`` + a trailing newline so the bytes are deliberate rather than
#: incidental: the assertion is byte-identity, and the reason it matters is that
#: these bytes are hashed downstream (``sha256`` at
#: ``shared/target_schema_resolution.py:310``, ``schema_mapping:305``,
#: ``schema_compare:384``) and compared as ``target_schema_version`` to detect
#: drift (``LGR-eee8ac8-4``). A rename that changed one byte would present as
#: target-schema drift and trigger a remap and an approval.
SCHEMA_BYTES = (json.dumps(SCHEMA_BODY, sort_keys=True, indent=2) + "\n").encode("utf-8")

OTHER_SCHEMA_BYTES = (json.dumps({"type": "object", "properties": {"sku": {"type": "string"}}}) + "\n").encode("utf-8")


@pytest.fixture
def rename_env(mock_s3_buckets, mock_dynamodb_tables, monkeypatch):
    """The shared moto backends, plus the env and the binding the handler needs.

    Nothing is stubbed that is not an address: ``TARGET_SCHEMAS_BUCKET`` names
    the fixture's bucket and ``AWS_ACCOUNT_ID`` keeps
    ``get_aws_account_id`` (``schemas.py:77``) off STS. Every S3 and DynamoDB
    interaction under test is the real API against the shared fixtures.

    ``raising=False`` on the ``pipelines_table`` binding is deliberate:
    ``handlers/schemas.py`` has no such attribute **today** — task 8.4 adds one
    for ``_find_referencing_pipelines`` — and the binding has to be in place
    before the code that would use it, or the first implementation to touch the
    pipelines table does so in a region this property cannot see. Setting an
    attribute nothing reads yet is harmless; monkeypatch removes it afterwards.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setattr(schemas_handler, "pipelines_table", mock_dynamodb_tables[PIPELINES_TABLE], raising=False)
    return mock_dynamodb_tables[PIPELINES_TABLE]


# ---------------------------------------------------------------------------
# Seeding and reading the world
# ---------------------------------------------------------------------------
def _wipe_bucket() -> None:
    """Empty the target-schemas bucket so each example starts from a clean slate.

    Hypothesis reuses one fixture across every example, so without this a
    sidecar written by an earlier example could make clause 3 pass for the
    wrong reason. This runs in the harness, outside the recorder's window, so it
    is not one of the calls under assertion.
    """
    s3 = _client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=TARGET_SCHEMAS_BUCKET, Delete={"Objects": keys})


def _put(key: str, body: bytes) -> None:
    _client("s3").put_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=key, Body=body)


def _get(key: str) -> bytes | None:
    s3 = _client("s3")
    try:
        return s3.get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def _inventory() -> dict[str, bytes]:
    """Every key in the bucket and its bytes — the effect-level backstop.

    The call-level assertion is the primary one, per the task. This is the
    second, independent witness: a relocation shows up here as a key that
    appeared or vanished, and a body rewrite as bytes that moved, without
    depending on the spy having been installed correctly.
    """
    s3 = _client("s3")
    inventory: dict[str, bytes] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        for obj in page.get("Contents", []):
            inventory[obj["Key"]] = s3.get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=obj["Key"])["Body"].read()
    return inventory


def _pipeline_records(schema_id: str, other_id: str) -> list[dict[str, Any]]:
    """Three pipeline records, each a reference this property must see survive.

    - ``by-name-and-key``: the consistent shape ``create_pipeline`` writes today
      (``pipelines_crud.py:762-767`` writes bucket and key together or neither).
    - ``disagreeing``: the **live** ``testing-39eddb17`` shape
      (``pipelines_crud.py:822-830``) — ``target_schema_name`` and
      ``target_schema_s3_key`` one prefix apart. It is here because an
      implementation that decided a rename should "fix up the references" would
      do the most damage to exactly this record, and because R4.11's by-name
      fallback is reached only by a record whose key is *absent* or unusable.
    - ``by-name-only``: no ``target_schema_s3_key`` at all, which is the single
      input for which ``resolve_target_schema`` returns ``(None, None)``
      (``shared/target_schema_resolution.py:240-249``) and therefore the only
      record that actually reaches ``schema_mapping``'s by-name branch — the
      six-step path R4.11 is about.
    - ``unrelated``: references ``other_id``. A rename of one schema must not
      touch another schema's pipeline either.
    """
    return [
        {
            "pipeline_id": f"p35-consistent-{schema_id}",
            "pipeline_name": "consistent",
            "target_schema_name": schema_id,
            "target_schema_bucket": TARGET_SCHEMAS_BUCKET,
            "target_schema_s3_key": _body_key(schema_id),
            "status": "ACTIVE",
            "created_at": "2026-08-07T00:00:00Z",
        },
        {
            "pipeline_id": f"p35-disagreeing-{schema_id}",
            "pipeline_name": "disagreeing",
            "target_schema_name": schema_id,
            "target_schema_bucket": TARGET_SCHEMAS_BUCKET,
            "target_schema_s3_key": f"shared/uploaded_{schema_id}.json",
            "status": "ACTIVE",
            "created_at": "2026-08-07T00:00:01Z",
        },
        {
            "pipeline_id": f"p35-by-name-only-{schema_id}",
            "pipeline_name": "by name only",
            "target_schema_name": schema_id,
            "status": "ACTIVE",
            "created_at": "2026-08-07T00:00:02Z",
        },
        {
            "pipeline_id": f"p35-unrelated-{other_id}",
            "pipeline_name": "unrelated",
            "target_schema_name": other_id,
            "target_schema_bucket": TARGET_SCHEMAS_BUCKET,
            "target_schema_s3_key": _body_key(other_id),
            "status": "ACTIVE",
            "created_at": "2026-08-07T00:00:03Z",
        },
    ]


def _seed(table, scenario: Scenario) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """Put the world in place and return the "before" snapshot of both halves."""
    _wipe_bucket()
    _put(_body_key(scenario.schema_id), SCHEMA_BYTES)
    _put(_body_key(scenario.other_id), OTHER_SCHEMA_BYTES)
    if scenario.seed_sidecar:
        # The realistic case once task 8.5's context write exists: the sidecar is
        # already there carrying `field_contexts`, and the label joins it.
        _put(
            _sidecar_key(scenario.schema_id),
            json.dumps({"field_contexts": {"email": "primary contact"}}).encode("utf-8"),
        )

    records = _pipeline_records(scenario.schema_id, scenario.other_id)
    for record in records:
        table.put_item(Item=record)

    return _inventory(), {record["pipeline_id"]: record for record in records}


def _read_back(table, pipeline_ids: list[str]) -> dict[str, dict[str, Any]]:
    return {
        pipeline_id: table.get_item(Key={"pipeline_id": pipeline_id}).get("Item") or {} for pipeline_id in pipeline_ids
    }


def _display_label_event(schema_id: str, label: str) -> dict[str, Any]:
    """``PUT /api/schemas/{id}/display-label`` with ``{"displayLabel": …}``."""
    return {
        "httpMethod": "PUT",
        "path": f"/api/schemas/{schema_id}/display-label",
        "body": json.dumps({"displayLabel": label}),
    }


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
class Scenario(NamedTuple):
    """One stored schema, one bystander schema, and a sequence of labels."""

    schema_id: str
    other_id: str
    labels: list[str]
    seed_sidecar: bool


#: Both id shapes that reach this endpoint. The double-suffix form is the
#: **live** pre-R4.5 shape (``testing-39eddb17``'s
#: ``schema_1785844048926_20260804_114729``); the single-suffix form is what
#: task 8.6 will produce. A rename must be invariant over both, and R4.5 is a
#: change to the writer that this property must not be sensitive to.
_SCHEMA_IDS = st.one_of(
    st.sampled_from(
        (
            "uploaded_schema_1785844048926",
            "uploaded_schema_1785844048926_20260804_114729",
            "schema_1785844048926_20260804_114729",
            "p32ver_ren_renC",
            "customers_jsonschema",
        )
    ),
)


def _labels(schema_id: str, other_id: str) -> st.SearchStrategy[str]:
    """Labels, weighted toward the ones that would break something.

    The first two entries are the whole reason this property exists. Nothing
    type-distinguishes a Display_Label from a schema id, so a label that **is**
    an id is the input under which a wrong implementation looks right: writing
    it into ``target_schema_name`` produces a record that still resolves (for
    ``schema_id``) or resolves to the **wrong schema** (for ``other_id``) — and
    the second is silent until ``schema_mapping`` runs, six steps later.

    The key-shaped entries are R4.11's other half: a label that reaches a key
    derivation. ``shared/{id}.json`` and ``orders.json`` are what an
    implementation composing a key out of a label would produce, and the
    traversal string is what makes such a key dangerous rather than merely wrong.

    Free-form text is drawn from a utf-8-encodable alphabet with surrogates
    excluded: a lone surrogate cannot be encoded into a JSON object body at all,
    so generating one would test Python's codec rather than the rename.
    """
    hostile = [
        schema_id,  # a label that IS this schema's id — R4.11's trap, benign-looking
        other_id,  # a label that is ANOTHER schema's id — the six-step trap
        _body_key(schema_id),  # a label shaped like the body's own key
        _sidecar_key(schema_id),  # a label shaped like the sidecar's key
        "shared/evil.json",
        "../../etc/passwd",
        "orders.json",
        "Customer master",
        "Kundendaten Ünïcodé",
        "  padded label  ",
        "a" * 200,
        "customers",
    ]
    # `Zs` is excluded so a whitespace-only draw is rare rather than routine —
    # an empty-after-strip label is out of scope (see the module docstring), and
    # filtering it out at the test level discards whole examples. The
    # leading/trailing-whitespace case is covered by the explicit hostile entry
    # above instead, which is deterministic rather than hoped for.
    free_form = st.text(
        alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc", "Zs")),
        min_size=1,
        max_size=24,
    ).filter(lambda value: value.strip() != "")
    return st.one_of(st.sampled_from(hostile), free_form)


#: The live pre-R4.5 double-suffix id (``testing-39eddb17``'s shape) and a
#: bystander, used by the explicit examples below.
_ID_A = "uploaded_schema_1785844048926_20260804_114729"
_ID_B = "customers_jsonschema"

#: The cases this property exists for, as **explicit** inputs rather than as
#: draws that might happen.
#:
#: These started as pool entries in ``_labels`` and were promoted after the
#: coverage survey caught the distribution being unreliable: over 200 examples
#: the whitespace-padded label was reached **0 times in one run and ~25 in the
#: next**, purely on the Hypothesis seed, because ``one_of(sampled_from(...),
#: text(...))`` biases toward its first branch and ``sampled_from`` toward its
#: first elements. A coverage claim that depends on the seed is not a claim, and
#: the case that went missing was one of the ones R4.11 is about.
#:
#: So the pool stays — it is what gives the random half its variety — and these
#: seven are guaranteed by ``@example``. ``Phase.explicit`` runs them before any
#: generated example, in both the property and the survey.
_CRITICAL_SCENARIOS = (
    # A label that IS this schema's own id. Benign-looking: writing it into
    # `target_schema_name` leaves a record that still resolves, so an
    # implementation that does the forbidden thing passes every smoke test.
    Scenario(_ID_A, _ID_B, [_ID_A, "Customer master"], False),
    # A label that is ANOTHER schema's id — the six-step trap. This one resolves
    # to the WRONG schema, silently, until `schema_mapping` runs.
    Scenario(_ID_A, _ID_B, ["Customer master", _ID_B], False),
    # A label shaped like the body's own key: contains '/' and ends '.json',
    # which is what an implementation composing a key from a label produces.
    Scenario(_ID_A, _ID_B, [_body_key(_ID_A), "shared/evil.json"], False),
    Scenario(_ID_A, _ID_B, ["orders.json", "Customer master"], True),
    # Leading and trailing whitespace — stored verbatim per R4.7, not trimmed.
    Scenario(_ID_A, _ID_B, ["  padded label  ", "Customer master"], False),
    # The same label written twice in a row: a rename must be idempotent.
    Scenario(_ID_A, _ID_B, ["Customer master", "Customer master"], True),
    # The longest sequence, over a sidecar that already exists.
    Scenario(_ID_A, _ID_B, ["one", "two", "three", "four", "five"], True),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_SCENARIOS`` as explicit examples, in order."""
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


@st.composite
def rename_scenarios(draw) -> Scenario:
    """A stored schema plus a **sequence** of at least two label writes.

    ``min_size=2`` is deliberate and it is the case the task singles out: a
    rename-then-rename-again is where an implementation that derives anything
    from the *current* label diverges, because the second write's starting point
    is the first write's output rather than the original state. A single write
    cannot distinguish those, so it is covered by its own example test
    (``test_a_single_rename_is_the_same_shape_as_a_sequence``) rather than by
    diluting the sequence generator — the measured alternative was a
    ``min_size=1`` list, in which Hypothesis's bias toward the minimum made
    single-write examples the majority.

    ``other_id`` is forced distinct from ``schema_id`` by construction rather
    than by ``.filter``, so no example is discarded for it.
    """
    schema_id = draw(_SCHEMA_IDS)
    other_id = draw(_SCHEMA_IDS)
    if other_id == schema_id:
        other_id = f"{other_id}_bystander"
    labels = draw(st.lists(_labels(schema_id, other_id), min_size=2, max_size=5))
    return Scenario(schema_id, other_id, labels, draw(st.booleans()))


#: Every case ``_report_generated_cases`` observed, accumulated across the survey
#: below. A module-level accumulator, because Hypothesis's own statistics are
#: per-example and cannot carry a cross-example claim — and because **the
#: property short-circuits.** While ``set_display_label`` is a ``501`` stub the
#: property fails on its first example, so Hypothesis stops generating after a
#: handful and its statistics measure nothing. The survey is what keeps the
#: coverage claim measurable during exactly the window in which the property is
#: expected to be red.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached.

    Coverage is *reported*, not assumed — ``pytest --hypothesis-show-statistics``
    shows how often each case was actually reached. Tasks 3.1 and 4.3 both found
    generators that reached their target case **0 times out of 200 and passed
    anyway**; these events are what make that visible here rather than
    discovered later.
    """

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    event(f"sequence length {len(scenario.labels)}")
    if len(scenario.labels) >= 2:
        event("a sequence of two or more label writes (rename-then-rename-again)")
    if scenario.schema_id in scenario.labels:
        event("a label that IS this schema's own id (R4.11)")
    if scenario.other_id in scenario.labels:
        event("a label that is ANOTHER schema's id (the six-step trap)")
    if any("/" in label for label in scenario.labels):
        event("a label containing '/' — a label shaped like a key (R4.11)")
    if any(label.endswith(".json") for label in scenario.labels):
        event("a label ending in '.json'")
    if any(label != label.strip() for label in scenario.labels):
        event("a label with leading or trailing whitespace")
    if any(first == second for first, second in zip(scenario.labels, scenario.labels[1:], strict=False)):
        event("two consecutive writes of the SAME label (idempotence)")
    if scenario.seed_sidecar:
        event("a Context_Sidecar already exists (the label joins field_contexts)")
    else:
        event("no Context_Sidecar exists yet (the label creates it)")


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no AWS, no handler, nothing
# but the generator and the reporter.
@_with_critical_examples
@given(scenario=rename_scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(scenario):
    """Every scenario, explicit or drawn, is a multi-write sequence over usable labels.

    The per-example half of the survey. ``phases`` is limited to the explicit
    examples plus generation: there is no failure to shrink or explain and no
    reuse-database entry to replay, so the remaining phases would only add time.

    **Validates: Requirement 8.8**
    """
    _report_generated_cases(scenario)

    assert len(scenario.labels) >= 2, (
        f"the sequence generator must produce a rename-then-rename-again; drew {scenario.labels}"
    )
    assert scenario.other_id != scenario.schema_id, "the bystander schema must be a different schema"
    assert all(label.strip() for label in scenario.labels), (
        f"an empty-after-strip label is out of scope for this property; drew {scenario.labels}"
    )


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once in 200 draws.

    This is the assertion tasks 3.1 and 4.3 wished they had had. Both found
    generators that reached their target case **0 times out of 200 and passed
    anyway** — the first was caught by reading
    ``--hypothesis-show-statistics`` output by hand, which is a thing a person
    has to remember to do. Here the coverage claim is a test, so it fails when
    the generator drifts away from the cases rather than going quiet.

    The two entries that matter most are the id-shaped labels. Nothing
    type-distinguishes a Display_Label from a schema id, so a label that *is* an
    id is the input under which a wrong implementation looks correct — and for
    another schema's id, silently so, six steps away in ``schema_mapping``. A
    generator that stopped producing them would leave the property passing while
    testing nothing about R4.11.

    Every required case is guaranteed by ``_CRITICAL_SCENARIOS`` rather than left
    to the draw, which is what makes this assertion deterministic. The first
    version of it was seed-dependent and would have failed intermittently in CI —
    see the comment on ``_CRITICAL_SCENARIOS``.

    **Validates: Requirement 8.8**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        "a sequence of two or more label writes (rename-then-rename-again)",
        "a label that IS this schema's own id (R4.11)",
        "a label that is ANOTHER schema's id (the six-step trap)",
        "a label containing '/' — a label shaped like a key (R4.11)",
        "a label ending in '.json'",
        "a label with leading or trailing whitespace",
        "two consecutive writes of the SAME label (idempotence)",
        "a Context_Sidecar already exists (the label joins field_contexts)",
        "no Context_Sidecar exists yet (the label creates it)",
        "sequence length 2",
        "sequence length 5",
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=rename_scenarios())
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto S3 bucket and four DynamoDB records,
    # which routinely exceeds Hypothesis's 200ms default. The property asserts
    # byte-identity, not latency.
    deadline=None,
    # One fixture serves every example by design; `_seed` wipes the bucket and
    # overwrites the four records at the start of each one, so nothing an
    # earlier example wrote is visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` is excluded deliberately. This property is expected to be
    # RED from task 8.1 until task 8.5, and on every one of those runs the
    # explain phase re-ran ~400 further examples to annotate a failure whose
    # cause the assertion message already states verbatim — measured at 72s of
    # the file's 78s. Every checkpoint in this plan runs the full suite, so that
    # is a standing cost for no information. Shrinking is kept: a minimal
    # counter-example is the thing worth having when this fails for a real
    # reason after 8.5 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_rename_changes_the_label_and_nothing_else(scenario, rename_env):
    """Property 3: a rename changes the label and nothing else.

    Three clauses, asserted in this order on purpose. The two invariance clauses
    come first so that a copy-and-delete implementation names *itself* in the
    failure rather than being hidden behind a label that did land; clause 3 comes
    last so that today's ``501`` stub reports "the label never landed" — the
    expected red — instead of masking the guards.

    **Validates: Requirements 4.10, 4.11, 4.12, 8.8**
    """
    _report_generated_cases(scenario)

    table = rename_env
    before_inventory, before_records = _seed(table, scenario)
    body_key = _body_key(scenario.schema_id)
    sidecar_key = _sidecar_key(scenario.schema_id)

    with _recorded_s3_calls() as calls:
        for label in scenario.labels:
            schemas_handler.set_display_label(scenario.schema_id, _display_label_event(scenario.schema_id, label))

    # -- Clause 1a: the schema's id and its object's key are byte-identical -----
    after_inventory = _inventory()
    assert body_key in after_inventory, (
        f"R4.10: the schema's backing object must still be addressed by {body_key!r} after "
        f"{len(scenario.labels)} label write(s). Keys present now: {sorted(after_inventory)}"
    )

    # -- Clause 1b: the object's BYTES are byte-identical ----------------------
    # Byte-identity rather than JSON equality: these bytes are the schema's
    # identity downstream (sha256 → `target_schema_version`, LGR-eee8ac8-4), so a
    # re-serialisation that preserved meaning would still present as drift.
    assert after_inventory[body_key] == SCHEMA_BYTES, (
        f"R4.10/R4.12: the schema body's bytes changed. A rename must leave them byte-identical, "
        f"because sha256 over these bytes is `target_schema_version`.\n"
        f"  before: {SCHEMA_BYTES!r}\n  after:  {after_inventory[body_key]!r}"
    )

    # -- Clause 1c: no other object moved, appeared or vanished ----------------
    # The effect-level backstop, independent of the spy. Only the sidecar is
    # allowed to differ.
    changed = {
        key
        for key in set(before_inventory) | set(after_inventory)
        if before_inventory.get(key) != after_inventory.get(key)
    }
    assert changed <= {sidecar_key}, (
        f"R4.10: a rename may change no object but the Context_Sidecar {sidecar_key!r}. "
        f"These changed, appeared or vanished: {sorted(changed - {sidecar_key})}"
    )

    # -- Clause 1d: every referencing pipeline's target_schema_name is unchanged
    after_records = _read_back(table, list(before_records))
    for pipeline_id, before in before_records.items():
        after = after_records[pipeline_id]
        assert after.get("target_schema_name") == before["target_schema_name"], (
            f"R4.11: pipeline {pipeline_id}'s `target_schema_name` must keep carrying the schema "
            f"**id**, never the Display_Label. A label here misses all six candidate keys in "
            f"`uploaded_schema_candidate_keys` and raises in `schema_mapping` six steps later, "
            f"with no other symptom.\n"
            f"  before: {before['target_schema_name']!r}\n  after:  {after.get('target_schema_name')!r}"
        )
        assert after.get("target_schema_s3_key") == before.get("target_schema_s3_key"), (
            f"R4.10/R4.11: pipeline {pipeline_id}'s `target_schema_s3_key` must be untouched — a "
            f"present key is a claim that an object exists (`LGR-78ce0b7-14`), so rewriting it is "
            f"how a cosmetic rename turns into a raise on the next run.\n"
            f"  before: {before.get('target_schema_s3_key')!r}\n  after:  {after.get('target_schema_s3_key')!r}"
        )

    # -- Clause 2: the absence of the mutating calls, not just the right one ----
    forbidden = _forbidden_writes(calls, sidecar_key)
    assert not forbidden, (
        f"R4.10: a rename issued a relocating or non-sidecar write. `LGR-acca05d-2` rejected "
        f"rename-by-copy-and-delete and `LGR-78ce0b7-14` is why: it breaks every pipeline whose "
        f"`target_schema_s3_key` names the old key. The only permitted write is "
        f"{sidecar_key!r}.\n  forbidden calls: {forbidden}\n"
        f"  all recorded S3 calls: {[operation for operation, _ in calls]}"
    )

    # -- Clause 3: the label LANDED (R4.9, R4.12) ------------------------------
    # Without this the property is satisfied by a handler that does nothing, and
    # clauses 1 and 2 stop being claims about a rename. RED until task 8.5.
    sidecar_bytes = after_inventory.get(sidecar_key)
    assert sidecar_bytes is not None, (
        f"R4.9/R4.12: after {len(scenario.labels)} Display_Label write(s) the Context_Sidecar "
        f"{sidecar_key!r} does not exist, so no label was persisted. `set_display_label` is a "
        f"501 stub until task 8.5 — this is the expected red, and it is what stops clauses 1 "
        f"and 2 from being satisfied by a handler that changes nothing.\n"
        f"  labels written: {scenario.labels}\n  keys present: {sorted(after_inventory)}"
    )
    sidecar = json.loads(sidecar_bytes)
    assert sidecar.get("display_name") == scenario.labels[-1], (
        f"R4.12: the sidecar's `display_name` must be the last label written, stored verbatim "
        f"with no uniqueness transformation (R4.7).\n"
        f"  labels written: {scenario.labels}\n  display_name:   {sidecar.get('display_name')!r}"
    )


# ---------------------------------------------------------------------------
# The tripwire, proved armed
# ---------------------------------------------------------------------------
class TestTheTripwireIsArmed:
    """``_forbidden_writes`` catches the calls it exists to catch.

    Clause 2 passes today because ``set_display_label`` makes no call at all, so
    on its own it is indistinguishable from a spy that records nothing and an
    oracle that forbids nothing. These tests do the forbidden things
    **themselves** and require the check to report each one, so the guard is
    known-good before there is any code for it to guard.
    """

    def test_the_spy_records_real_calls_and_lets_them_through(self, rename_env):
        """A recorded call is still a real call: the object is really written."""
        with _recorded_s3_calls() as calls:
            _put("shared/spy_check.json", b"{}")
            body = _get("shared/spy_check.json")

        assert body == b"{}", "the spy must call through, not stand in for moto"
        assert ("PutObject", {"Bucket": TARGET_SCHEMAS_BUCKET, "Key": "shared/spy_check.json", "Body": b"{}"}) in [
            (operation, {k: v for k, v in params.items() if k in ("Bucket", "Key", "Body")})
            for operation, params in calls
        ], f"the PutObject was not recorded; observed: {[operation for operation, _ in calls]}"

    @pytest.mark.parametrize("operation", ["CopyObject", "DeleteObject", "PutObject"])
    def test_a_relocation_or_a_body_write_is_reported(self, operation, rename_env):
        """Each of the three calls the task names is caught, by name."""
        schema_id = "uploaded_schema_1785844048926"
        _put(_body_key(schema_id), SCHEMA_BYTES)
        s3 = _client("s3")

        with _recorded_s3_calls() as calls:
            if operation == "CopyObject":
                s3.copy_object(
                    Bucket=TARGET_SCHEMAS_BUCKET,
                    Key=_body_key("renamed"),
                    CopySource={"Bucket": TARGET_SCHEMAS_BUCKET, "Key": _body_key(schema_id)},
                )
            elif operation == "DeleteObject":
                s3.delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(schema_id))
            else:
                s3.put_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(schema_id), Body=b"{}")

        forbidden = _forbidden_writes(calls, _sidecar_key(schema_id))
        assert any(entry.startswith(operation) for entry in forbidden), (
            f"_forbidden_writes must report {operation}; it reported {forbidden}"
        )

    def test_the_sidecar_write_is_permitted_and_a_read_is_not_forbidden(self, rename_env):
        """The correct write, and the read that precedes it, both pass.

        A guard that forbade the sidecar write, or the ``get_object`` that reads
        the existing sidecar in order to preserve ``field_contexts`` beside the
        new ``display_name`` (R4.12), would make the property unsatisfiable.
        """
        schema_id = "uploaded_schema_1785844048926"
        sidecar_key = _sidecar_key(schema_id)

        with _recorded_s3_calls() as calls:
            _get(sidecar_key)
            _put(sidecar_key, json.dumps({"display_name": "Customer master"}).encode("utf-8"))

        assert _forbidden_writes(calls, sidecar_key) == [], (
            "the sidecar write and the read before it are the correct implementation"
        )


class TestTheSpecificationIsSatisfiable:
    """All three clauses can hold at once, so today's red means "unimplemented".

    Design §5 risk 9: a reviewer who sees red and reverts has undone the only
    thing that would have caught the defect. These two tests are what separate
    "the subject is not written yet" from "the property cannot be satisfied" —
    the second would be a reason to revisit the requirements, and it is not the
    case here.

    ``_rename_via_sidecar`` below is a **discrimination probe, not a reference
    implementation.** It exists so the property's oracle can be shown to accept
    the prescribed shape and reject the rejected one, and the property itself
    never calls it — it calls ``schemas_handler.set_display_label``. Task 8.5
    still owns the real handler, with the validation, the ``404`` for an unknown
    id and the error handling the requirements ask for, none of which is here.
    """

    @staticmethod
    def _rename_via_sidecar(schema_id: str, label: str) -> None:
        """R4.12's shape: read the sidecar, add ``display_name``, put it back."""
        existing = _get(_sidecar_key(schema_id))
        sidecar = json.loads(existing) if existing else {}
        sidecar["display_name"] = label
        _put(_sidecar_key(schema_id), json.dumps(sidecar).encode("utf-8"))

    @staticmethod
    def _rename_by_copy_and_delete(schema_id: str, label: str) -> None:
        """The implementation ``LGR-acca05d-2`` rejected, for contrast only."""
        s3 = _client("s3")
        s3.copy_object(
            Bucket=TARGET_SCHEMAS_BUCKET,
            Key=_body_key(label),
            CopySource={"Bucket": TARGET_SCHEMAS_BUCKET, "Key": _body_key(schema_id)},
        )
        s3.delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(schema_id))

    def test_a_sidecar_write_satisfies_every_clause(self, rename_env):
        """The prescribed shape passes all three clauses over a sequence."""
        scenario = Scenario("uploaded_schema_1785844048926", "customers_jsonschema", ["First", "Second"], True)
        before_inventory, before_records = _seed(rename_env, scenario)
        body_key = _body_key(scenario.schema_id)
        sidecar_key = _sidecar_key(scenario.schema_id)

        with _recorded_s3_calls() as calls:
            for label in scenario.labels:
                self._rename_via_sidecar(scenario.schema_id, label)

        after_inventory = _inventory()
        after_records = _read_back(rename_env, list(before_records))

        assert after_inventory[body_key] == SCHEMA_BYTES
        assert _forbidden_writes(calls, sidecar_key) == []
        assert json.loads(after_inventory[sidecar_key])["display_name"] == "Second"
        assert all(
            after_records[pid].get("target_schema_name") == before["target_schema_name"]
            for pid, before in before_records.items()
        )
        # R4.12's "alongside `field_contexts`" — the seeded context survives.
        assert json.loads(after_inventory[sidecar_key])["field_contexts"] == {"email": "primary contact"}
        # The sidecar was seeded, so the key set is identical: nothing appeared
        # and nothing vanished, only the sidecar's bytes moved.
        assert set(before_inventory) == set(after_inventory)

    def test_the_rejected_copy_and_delete_is_caught_by_both_witnesses(self, rename_env):
        """The rejected shape fails the call check **and** the inventory check.

        Two independent witnesses, deliberately: the call-level assertion is the
        one the task names, and the inventory is what still catches a relocation
        if the spy were ever removed or mis-wired.
        """
        scenario = Scenario("uploaded_schema_1785844048926", "customers_jsonschema", ["Renamed"], False)
        before_inventory, _ = _seed(rename_env, scenario)

        with _recorded_s3_calls() as calls:
            self._rename_by_copy_and_delete(scenario.schema_id, "Renamed")

        forbidden = _forbidden_writes(calls, _sidecar_key(scenario.schema_id))
        assert [entry.split("(")[0] for entry in forbidden] == ["CopyObject", "DeleteObject"], (
            f"the call-level witness must name both halves of the rejected implementation: {forbidden}"
        )

        after_inventory = _inventory()
        assert _body_key(scenario.schema_id) not in after_inventory, (
            "the rejected implementation removes the object the pipelines address — this is the "
            "state `LGR-78ce0b7-14` turns into a raise on the next run"
        )
        changed = {
            key
            for key in set(before_inventory) | set(after_inventory)
            if before_inventory.get(key) != after_inventory.get(key)
        }
        assert changed - {_sidecar_key(scenario.schema_id)}, "the inventory witness must see the relocation too"


# ---------------------------------------------------------------------------
# The single-write case, and the stub's current answer
# ---------------------------------------------------------------------------
def test_a_single_rename_is_the_same_shape_as_a_sequence(rename_env):
    """One write, asserted the same way — the case the sequence generator drops.

    ``rename_scenarios`` draws ``min_size=2`` so every generated example is a
    rename-then-rename-again, which is where a naive implementation diverges.
    A single write is still the ordinary case, so it is covered here rather than
    left to Hypothesis's shrinking.

    **Expected RED until task 8.5**, on the same clause as the property: the
    label never lands.

    **Validates: Requirements 4.10, 4.11, 4.12, 8.8**
    """
    scenario = Scenario(
        "uploaded_schema_1785844048926_20260804_114729", "customers_jsonschema", ["Customer master"], False
    )
    before_inventory, before_records = _seed(rename_env, scenario)
    sidecar_key = _sidecar_key(scenario.schema_id)

    with _recorded_s3_calls() as calls:
        schemas_handler.set_display_label(
            scenario.schema_id, _display_label_event(scenario.schema_id, "Customer master")
        )

    after_inventory = _inventory()
    after_records = _read_back(rename_env, list(before_records))

    assert after_inventory[_body_key(scenario.schema_id)] == SCHEMA_BYTES
    assert _forbidden_writes(calls, sidecar_key) == []
    assert all(
        after_records[pid].get("target_schema_name") == before["target_schema_name"]
        for pid, before in before_records.items()
    )
    assert set(before_inventory) <= set(after_inventory)

    sidecar_bytes = after_inventory.get(sidecar_key)
    assert sidecar_bytes is not None, (
        f"R4.9/R4.12: one Display_Label write persisted nothing to {sidecar_key!r}. "
        f"`set_display_label` is a 501 stub until task 8.5 — the expected red."
    )
    assert json.loads(sidecar_bytes)["display_name"] == "Customer master"


# ---------------------------------------------------------------------------
# DELETED BY TASK 8.5 — `test_the_stub_writes_nothing_which_is_why_the_label_never_lands`
# ---------------------------------------------------------------------------
# It pinned the stub's answer — `501` and not one S3 call — as the *reason* the
# three clauses above were red, so that the red read as "the subject does not
# exist yet" rather than as a defect in the property. Its own docstring was its
# expiry note: "Task 8.5 will make this test fail, and **that failure is the
# signal to delete this test**, not to loosen it."
#
# Task 8.5 landed `set_display_label`, so it answered `200` and wrote the
# sidecar. Deleted rather than amended, per its own instruction and per
# `LGR-5906ec6-2` / `LGR-60c4985-6` (both **template / ratified**), which require
# a reversal to be stated where the assertion was so it reads as deliberate. It
# was also the only assertion in this file about a handler's **return value**,
# and the property must make none — so there is nothing here to carry forward.
# The behaviour it stood in for is now asserted positively by clause 3 of
# `test_a_rename_changes_the_label_and_nothing_else` and by
# `test_a_single_rename_is_the_same_shape_as_a_sequence`, both green, and the
# handler's status codes belong to `test_schema_context_and_label_writes.py`.
