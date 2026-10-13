# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4 — a delete is refused by either predicate.

# Feature: p35-schema-surfaces, Property 4: A delete is refused whenever any pipeline references the schema, by either predicate

For any population of pipeline records — **including records whose
``target_schema_name`` and ``target_schema_s3_key`` disagree** — ``delete_schema``
responds ``409`` and performs **no** ``delete_object`` if any record's
``target_schema_s3_key`` names the schema's object **or** any record's
``target_schema_name`` equals the schema id; otherwise it deletes and responds
success. No input produces both a refusal and a deletion (R3.10).

THE DISAGREEING RECORD IS THE GENERATOR'S WHOLE JOB
---------------------------------------------------
R3.8 defines the predicate on the **key**. The pipelines table has no index on
``target_schema_s3_key`` — ``deploy-cdk/lib/data-stack.ts:80-86`` declares exactly
two GSIs, ``status-created_at-index`` and ``target_schema_name-index`` — so the
predicate the requirement states has no index of its own, and the index that
exists answers a *different* predicate which agrees with it on every record
``create_pipeline`` writes **today**.

It does not agree on the records written before the fix. The comment at
``pipelines_crud.py:822-830`` names the live one, ``testing-39eddb17``:

===========================  =================================================
``target_schema_name``       ``schema_1785844048926_20260804_114729``
``target_schema_s3_key``     ``shared/uploaded_schema_1785844048926_20260804_114729.json``
===========================  =================================================

For the schema whose id is ``uploaded_schema_1785844048926_20260804_114729``,
that record's **key names our object** while its **name does not equal our id**.
A Query on ``target_schema_name-index`` cannot see it. So a Query-only lookup
answers "no pipeline references this schema" and deletes the object out from
under a live pipeline, whose next run then *raises*
``target_schema_object_missing`` (``shared/target_schema_resolution.py:290-302``,
``LGR-78ce0b7-14``, **template / ratified**) — the exact failure D4
(``LGR-acca05d-4``, **template / ratified**) exists to prevent.

``TestTheSpecificationIsSatisfiable`` below does not merely assert that in prose:
it runs both lookups against the same seeded population and shows the Query-only
one deleting the object while Query-then-Scan refuses. That is the measurement,
and it is the reason the generator's disagreeing case is not decoration.

The generator also carries the **converse** near-misses, because R3.8's predicate
is equality on the key and not containment:

- ``shared/uploaded_{id}.json`` — contains the id as a substring and names a
  *different* schema's object. This is the live record's key read from the other
  direction, and an ``Attr(...).contains(...)`` filter would refuse on it.
- ``schemas/uploaded/{id}-metadata.json`` — the Context_Sidecar, keyed on the id
  and not the schema's object.
- ``{id}_v2`` as a ``target_schema_name`` — an id-prefixed name, which a
  ``begins_with`` key condition would match.

Each of those must **not** produce a refusal. Without them the property is
satisfiable by a handler that refuses whenever the id appears anywhere, which
would make every delete impossible while looking correct on the referenced case.

WHAT IS ASSERTED, AND WHERE
---------------------------
Two witnesses per example, deliberately:

1. **The recorded S3 calls.** A pass-through spy over
   ``botocore.client.BaseClient._make_api_call`` records ``(operation, params)``
   and then calls through, so absence-of-call assertions are possible and every
   observed call is a real call into moto's backend. Task 8.1 established this
   technique; it sits **above** moto's ``before-send`` stubber, which is why it
   sees calls from clients the handler constructs itself.
2. **The object.** The refusal case asserts the body is still there afterwards,
   the success case that it is gone. This is what still catches a delete if the
   spy were ever removed or mis-wired, and it is the half design §7's
   confirmation clause names ("and the object is still there afterwards").

``409`` alone would be satisfied by a handler that refused *and* deleted, which
is precisely what R3.10 forbids, so the status is never asserted on its own.

R3.10 IS A PROPERTY OVER THE WHOLE OUTCOME
------------------------------------------
"No input produces both a refusal and a deletion" is checked before the oracle,
as its own clause, on every example: never ``409`` together with a
``DeleteObject``, and never ``409`` together with an absent object. A handler
that deleted first and refused afterwards would satisfy every status assertion
in this file and violate the one criterion D4 was ratified for.

WHAT THIS PROPERTY DOES **NOT** COVER
-------------------------------------
**Task 8.3 owns the interrupted reference check.** Nothing here throttles the
scan or exhausts a wall-clock budget, so on every input in this file the check
*can* finish, and a ``503`` is therefore a failure of "otherwise delete and
respond success" rather than the correct answer. Property 5 is the one that makes
DD2's ``200`` an unqualified claim; keeping them apart is what stops a single
property from being satisfiable by answering ``503`` to everything.

R3.3's ``400`` for a ``glue_catalog`` / ``s3_table_bucket`` id is also out of
scope — every id generated here is an Uploaded_Schema id, which R3's scope note
establishes as the only kind that names a deletable object.

TWO MODELLING CHOICES, STATED RATHER THAN BURIED
------------------------------------------------
**Archived records count.** R3.7 says "at least one pipeline **record**
references", with no qualification by status, and ``restore_pipeline``
(``router.py:90``) can bring an archived pipeline back. So the oracle is
record-level and the generator draws ``active``, ``ACTIVE`` and ``archived``
alike. If task 8.4 concludes a refusal should ignore archived records, this
property will fail on exactly that case — which is the right place for that
conversation to happen, and it is a requirements question rather than a test bug.

**Every generated record carries ``created_at``.** Not cosmetic: the GSI's sort
key is ``created_at`` (``tests/fixtures/conftest.py:59``), so a record without it
is not in the index at all, and generating one would be generating a record the
index cannot answer for reasons unrelated to R3.8. ``create_pipeline`` always
writes it (``pipelines_crud.py:838``), so requiring it keeps the population
faithful to the table.

FIXTURES, REGION, AND THE HANDLE TASK 8.4 MUST USE
--------------------------------------------------
Shared fixtures only — ``mock_s3_buckets`` and ``mock_dynamodb_tables`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and this file does **not** set
``AWS_REGION`` (``LGR-3e45e65-1``): the unit layer pins it to ``us-west-2`` in
``tests/unit/conftest.py:31-33``, which is what ``schemas.py:23``'s
``os.environ.get("AWS_REGION", "us-west-2")`` then resolves, so the seeding and
the handler cannot land in different backends.

**The DynamoDB half carries the asymmetry task 8.1 recorded.**
``mock_dynamodb_tables`` creates its tables in ``us-east-1``
(``tests/fixtures/conftest.py:214``) while the handler resolves ``us-west-2``, and
moto's DynamoDB backend is region-scoped. The fixture below therefore binds the
fixture's table handle onto **both** ``handlers.schemas`` and
``handlers.common``, with ``raising=False`` on the first because
``handlers/schemas.py`` has no such attribute today. Two bindings rather than one
because either access shape is reasonable for task 8.4 —
``from .common import pipelines_table`` reads the name out of ``schemas``'s own
namespace, while ``common.pipelines_table`` reads it out of ``common``'s — and a
property that only patched one would silently test nothing under the other.
**Task 8.4 must reach the pipelines table through a module-level handle**, as
``pipelines_crud`` does: a resource constructed inline in the resolved region
would find no table and raise ``ResourceNotFoundException``, which is loud but
puts R3.8's assertion out of this property's reach.

**Validates: Requirements 3.7, 3.8, 3.10, 8.9**
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, NamedTuple

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
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

from handlers import common as common_handler
from handlers import schemas as schemas_handler

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
ACCOUNT_ID = "123456789012"

#: Upper bound on the generated population, and the number of deterministic
#: pipeline ids the harness reclaims before each example. Seeding reuses
#: ``p35-del-{n}`` rather than drawing unique ids so that clearing the previous
#: example is a fixed set of keyed deletes instead of a scan — a scan here would
#: be the harness performing the very operation the subject is being measured on.
_MAX_RECORDS = 6


# ---------------------------------------------------------------------------
# The two addresses, written out from the requirements rather than imported
# ---------------------------------------------------------------------------
def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention.

    Spelled out here rather than imported so the property's oracle is the
    requirement and not the code under test. This is "the schema's object" that
    R3.8's predicate is defined on.
    """
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{schema_id}-metadata.json`` — R4.12's Context_Sidecar.

    Present here only as a **near miss**: it carries the schema id and it is not
    the schema's object, so a record naming it does not reference the schema.
    """
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _client(service: str):
    """A client in exactly the region the handler will use (``LGR-3e45e65-1``).

    ``os.environ.get("AWS_REGION", "us-west-2")`` is ``schemas.py:23`` verbatim.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


# ---------------------------------------------------------------------------
# The pass-through spy, and the delete oracle
# ---------------------------------------------------------------------------
#: Every spelling of "remove an object". ``DeleteObjects`` is the batch form and
#: ``AbortMultipartUpload`` is not a delete of a stored object, so the pair below
#: is the whole surface a refusal must not touch. Listing only ``DeleteObject``
#: would let a batch delete through under a different name.
_DELETING_OPERATIONS = ("DeleteObject", "DeleteObjects")


@contextlib.contextmanager
def _recorded_s3_calls():
    """Record every S3 API call made inside the block, then call through.

    A spy, not a mock: the wrapper appends ``(operation_name, params)`` and
    returns ``original(...)``, so every call still reaches moto's backend and
    every response is moto's. It wraps ``BaseClient._make_api_call``, which sits
    **above** moto's ``before-send`` stubber, so it observes calls from clients
    the handler constructs itself — the only way to assert the *absence* of a
    call the handler was never asked to make.
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


def _delete_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Every recorded call that removes an object, whatever it names.

    Not filtered to the schema's own key on purpose. A refusal has no business
    deleting *any* object in the target-schemas bucket, and a handler that
    deleted the sidecar while refusing the body would be reported here rather
    than passing a narrower check.
    """
    offending: list[str] = []
    for operation, params in calls:
        if operation == "DeleteObject":
            offending.append(f"DeleteObject(Key={params.get('Key')!r})")
        elif operation == "DeleteObjects":
            keys = [entry.get("Key") for entry in (params.get("Delete") or {}).get("Objects", [])]
            offending.append(f"DeleteObjects(Keys={keys!r})")
    return offending


# ---------------------------------------------------------------------------
# The fixture — shared moto backends, bound to the handler
# ---------------------------------------------------------------------------
SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_BYTES = (json.dumps(SCHEMA_BODY, sort_keys=True, indent=2) + "\n").encode("utf-8")
OTHER_SCHEMA_BYTES = (json.dumps({"type": "object", "properties": {"sku": {"type": "string"}}}) + "\n").encode("utf-8")


@pytest.fixture
def delete_env(mock_s3_buckets, mock_dynamodb_tables, monkeypatch):
    """The shared moto backends, plus the env and the bindings the handler needs.

    Nothing is stubbed that is not an address: ``TARGET_SCHEMAS_BUCKET`` names the
    fixture's bucket and ``AWS_ACCOUNT_ID`` keeps ``get_aws_account_id``
    (``schemas.py:77``) off STS. Every S3 and DynamoDB interaction under test is
    the real API against the shared fixtures.

    Both table bindings are deliberate — see the module docstring. ``raising=False``
    on the ``handlers.schemas`` one because that attribute does not exist until
    task 8.4 adds it; setting an attribute nothing reads yet is harmless, and
    monkeypatch removes it afterwards.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(schemas_handler, "pipelines_table", table, raising=False)
    monkeypatch.setattr(common_handler, "pipelines_table", table)
    return table


# ---------------------------------------------------------------------------
# The population model — one record kind per row of R3.8's truth table
# ---------------------------------------------------------------------------
class RecordSpec(NamedTuple):
    """One pipeline record, described by what it does to the two predicates.

    ``references`` is the oracle's verdict for this record alone, written down
    beside the record's shape rather than computed from it, so the expectation is
    stated by the requirement and not derived from the same expression the
    subject will use.
    """

    kind: str
    #: ``None`` means the attribute is absent from the record entirely.
    name: str | None
    key: str | None
    status: str
    references: bool
    #: True when this record's key names the schema's object.
    by_key: bool
    #: True when this record's name equals the schema id.
    by_name: bool


def _legacy_name(schema_id: str) -> str:
    """The ``target_schema_name`` the pre-fix code wrote for this id.

    The old code stripped a leading ``uploaded_`` before writing the name, which
    is what put ``testing-39eddb17``'s name and key one prefix apart
    (``pipelines_crud.py:822-830``). Reproduced rather than approximated: for
    ``uploaded_schema_1785844048926_20260804_114729`` this returns the live
    record's own ``schema_1785844048926_20260804_114729``.

    For an id that never carried the prefix the strip is a no-op, which would make
    the record *agree* with its key and stop it being the shape its row is for, so
    the fallback prefixes instead. Either way the name disagrees with the key,
    which is the only property the row needs to carry.
    """
    if schema_id.startswith("uploaded_"):
        return schema_id[len("uploaded_") :]
    return f"legacy_{schema_id}"


def _record_kinds(schema_id: str, other_id: str) -> list[RecordSpec]:
    """Every record shape this property is about, for one schema id.

    Rows 1-4 reference the schema; rows 5-10 do not. The middle two of the first
    group are the disagreeing pair, and row 2 is the live ``testing-39eddb17``
    shape read against the schema its key actually names.
    """
    body = _body_key(schema_id)
    return [
        # 1. The consistent shape `create_pipeline` writes today: name and key
        #    agree (`pipelines_crud.py:804` → `:837`). BOTH predicates fire on
        #    this one record.
        RecordSpec("agreeing", schema_id, body, "active", True, True, True),
        # 2. THE LIVE DISAGREEING RECORD. `testing-39eddb17`'s key names our
        #    object while its name names something else, so a Query on
        #    `target_schema_name-index` cannot see it and only the Scan can.
        #    This is the row Query-only deletes through.
        RecordSpec("key-only (disagreeing)", _legacy_name(schema_id), body, "active", True, True, False),
        # 3. The other direction: the name equals our id while the key addresses
        #    a different object. The Query answers this one in a page.
        RecordSpec(
            "name-only (key elsewhere)", schema_id, f"shared/uploaded_{schema_id}.json", "active", True, False, True
        ),
        # 4. Name only, with NO key at all — the unresolvable branch at
        #    `pipelines_crud.py:831`, and the single input for which
        #    `resolve_target_schema` returns `(None, None)`.
        RecordSpec("name-only (no key attribute)", schema_id, None, "active", True, False, True),
        # 5. A key that CONTAINS the id and names a different schema's object.
        #    `Attr(...).contains(...)` refuses here; `eq` does not. This is the
        #    live record's key seen from the other side.
        RecordSpec(
            "near miss: key contains the id, names another object",
            f"uploaded_{schema_id}",
            f"shared/uploaded_{schema_id}.json",
            "active",
            False,
            False,
            False,
        ),
        # 6. The Context_Sidecar: keyed on the id, not the schema's object.
        RecordSpec(
            "near miss: the -metadata sidecar key", other_id, _sidecar_key(schema_id), "active", False, False, False
        ),
        # 7. A suffixed key. `begins_with` on the key would match it.
        RecordSpec(
            "near miss: key with a suffix", other_id, f"{_body_key(schema_id)}.bak", "active", False, False, False
        ),
        # 8. An id-prefixed NAME. `begins_with` on the index would match it.
        RecordSpec(
            "near miss: name prefixed by the id",
            f"{schema_id}_v2",
            f"shared/{schema_id}_v2.json",
            "active",
            False,
            False,
            False,
        ),
        # 9. Another schema entirely, consistently addressed.
        RecordSpec("unrelated", other_id, _body_key(other_id), "active", False, False, False),
        # 10. A record with no name at all — absent from the index by
        #     construction, and no reference either.
        RecordSpec("unrelated (no name attribute)", None, _body_key(other_id), "archived", False, False, False),
    ]


def _item(spec: RecordSpec, index: int) -> dict[str, Any]:
    """A DynamoDB item for one spec, on a deterministic reclaimable id.

    ``created_at`` is always present: it is the GSI's sort key
    (``tests/fixtures/conftest.py:59``), so an item without it is not in
    ``target_schema_name-index`` at all — see the module docstring.
    """
    item: dict[str, Any] = {
        "pipeline_id": f"p35-del-{index}",
        "pipeline_name": f"{spec.kind} #{index}",
        "status": spec.status,
        "created_at": f"2026-08-07T00:00:{index:02d}Z",
    }
    if spec.name is not None:
        item["target_schema_name"] = spec.name
    if spec.key is not None:
        item["target_schema_bucket"] = TARGET_SCHEMAS_BUCKET
        item["target_schema_s3_key"] = spec.key
    return item


class Population(NamedTuple):
    """One schema under delete, one bystander schema, and the table's contents."""

    schema_id: str
    other_id: str
    records: tuple[RecordSpec, ...]

    @property
    def referencing(self) -> tuple[RecordSpec, ...]:
        return tuple(spec for spec in self.records if spec.references)

    @property
    def is_referenced(self) -> bool:
        """The oracle, straight from R3.7 and R3.8, as a disjunction."""
        return any(spec.by_key or spec.by_name for spec in self.records)


# ---------------------------------------------------------------------------
# Seeding and reading the world
# ---------------------------------------------------------------------------
def _wipe_bucket() -> None:
    """Empty the target-schemas bucket so each example starts from a clean slate.

    Hypothesis reuses one fixture across every example, so without this an object
    an earlier example deleted (or left behind) would decide a later example's
    outcome. This runs in the harness, outside the recorder's window, so its own
    ``delete_objects`` is not one of the calls under assertion.
    """
    s3 = _client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=TARGET_SCHEMAS_BUCKET, Delete={"Objects": keys})


def _put(key: str, body: bytes) -> None:
    _client("s3").put_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=key, Body=body)


def _inventory() -> set[str]:
    """Every key in the bucket — the effect-level witness."""
    s3 = _client("s3")
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _seed(table, population: Population) -> set[str]:
    """Put the world in place and return the "before" key set.

    The pipeline ids are reclaimed rather than accumulated: every
    ``p35-del-{n}`` up to the generator's maximum is deleted first, so a shorter
    population cannot inherit a longer one's records.
    """
    _wipe_bucket()
    _put(_body_key(population.schema_id), SCHEMA_BYTES)
    _put(_body_key(population.other_id), OTHER_SCHEMA_BYTES)

    for index in range(_MAX_RECORDS):
        table.delete_item(Key={"pipeline_id": f"p35-del-{index}"})
    for index, spec in enumerate(population.records):
        table.put_item(Item=_item(spec, index))

    return _inventory()


def _response_body(response: dict[str, Any]) -> Any:
    """The parsed body of a handler response, or the raw value if not JSON."""
    body = response.get("body")
    if not isinstance(body, str):
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _strings_in(value: Any) -> list[str]:
    """Every string anywhere in a parsed JSON body, keys included.

    The requirements do not name the key the referencing pipelines arrive under —
    design §4.2 says only "the pipeline ids and names" — so the property asserts
    the **substance** (which ids the response names) and leaves task 8.4 free to
    choose the shape. Transcribing a key name here would be pinning a contract
    the requirements never state.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in [*_strings_in(key), *_strings_in(item)]]
    if isinstance(value, list):
        return [text for item in value for text in _strings_in(item)]
    return []


def _named_pipeline_ids(response: dict[str, Any], candidates: list[str]) -> set[str]:
    """Which of ``candidates`` the response names anywhere in its body."""
    haystack = _strings_in(_response_body(response))
    return {candidate for candidate in candidates if any(candidate in text for text in haystack)}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#: Both id shapes that reach this endpoint. The double-suffix form is the **live**
#: pre-R4.5 shape — ``testing-39eddb17``'s key names
#: ``uploaded_schema_1785844048926_20260804_114729`` — and the single-suffix form
#: is what task 8.6 will produce. A delete must be correct for both.
_SCHEMA_IDS = st.sampled_from(
    (
        "uploaded_schema_1785844048926_20260804_114729",
        "uploaded_schema_1785844048926",
        "customers_jsonschema",
        "p32ver_ren_renC",
    )
)

_STATUSES = st.sampled_from(("active", "ACTIVE", "archived"))


@st.composite
def populations(draw) -> Population:
    """A schema id, a bystander, and a subset of the ten record kinds.

    Drawn as a **subset of kinds** rather than as free-form records: the ten kinds
    are the rows of R3.8's truth table, and a free-form draw would spend most of
    its examples on records that differ from each other only in a string while
    reaching the disagreeing row rarely. ``unique_by`` keeps a kind from being
    drawn twice, and the empty population is reachable (``min_size=0``) because
    "no pipeline record exists at all" is the case a delete must succeed on.

    The status is re-drawn per record, so an archived record referencing the
    schema is reachable — see the module docstring on why the oracle counts it.

    **One in four draws is forced into the Query-blind shape**, and that weighting
    is the task's "the disagreeing-record case is the generator's whole job" made
    operational. Measured first without it: the population in which the *only*
    reference is a disagreeing record — the one a Query-only lookup deletes — was
    reached in **2.18%** of 200 draws, because it requires the disagreeing kind to
    be drawn *and* every name-matching kind to be absent. Three of the explicit
    examples guarantee it, so the coverage assertion held either way; the
    weighting is what gives the *random* half of the property real power in the
    region D4's ratification block is about, rather than exercising it four times.
    """
    schema_id = draw(_SCHEMA_IDS)
    other_id = draw(_SCHEMA_IDS)
    if other_id == schema_id:
        other_id = f"{other_id}_bystander"

    kinds = _record_kinds(schema_id, other_id)
    chosen = draw(st.lists(st.sampled_from(kinds), min_size=0, max_size=_MAX_RECORDS, unique_by=lambda spec: spec.kind))

    if draw(st.integers(min_value=0, max_value=3)) == 0:
        # The Query-blind shape: no record's `target_schema_name` equals the id,
        # so the index answers nothing, and at least one record's key names the
        # schema's object. Composed by filtering rather than by `.filter()` so no
        # example is discarded for it.
        chosen = [spec for spec in chosen if not spec.by_name]
        if not any(spec.by_key for spec in chosen):
            disagreeing = next(spec for spec in kinds if spec.kind == "key-only (disagreeing)")
            chosen = [disagreeing, *chosen][:_MAX_RECORDS]

    records = tuple(spec._replace(status=draw(_STATUSES)) for spec in chosen)
    return Population(schema_id, other_id, records)


#: The live pre-R4.5 double-suffix id, and a bystander, for the explicit examples.
_ID_A = "uploaded_schema_1785844048926_20260804_114729"
_ID_B = "customers_jsonschema"


def _population_of(schema_id: str, other_id: str, *kinds: str) -> Population:
    """Build a population by naming the kinds it should contain."""
    by_kind = {spec.kind: spec for spec in _record_kinds(schema_id, other_id)}
    missing = [kind for kind in kinds if kind not in by_kind]
    assert not missing, f"unknown record kind(s): {missing}"
    return Population(schema_id, other_id, tuple(by_kind[kind] for kind in kinds))


#: The cases this property exists for, as **explicit** inputs rather than as draws
#: that might happen. Task 8.1 promoted its required cases for exactly this
#: reason: over 200 examples one required case was reached 0 times in one run and
#: ~25 in the next, purely on the Hypothesis seed. A coverage claim that depends
#: on the seed is not a claim.
_CRITICAL_POPULATIONS = (
    # The whole point of the property: the ONLY reference is the live disagreeing
    # record, so a Query-only lookup finds nothing and deletes.
    _population_of(_ID_A, _ID_B, "key-only (disagreeing)"),
    # The same, surrounded by near misses a sloppier predicate would trip on —
    # so the refusal has to come from the disagreeing record and not from them.
    _population_of(
        _ID_A,
        _ID_B,
        "key-only (disagreeing)",
        "near miss: key contains the id, names another object",
        "near miss: the -metadata sidecar key",
        "unrelated",
    ),
    # Both predicates firing on the same population, from two different records.
    _population_of(_ID_A, _ID_B, "key-only (disagreeing)", "name-only (no key attribute)"),
    # Both predicates firing on ONE record — today's consistent shape.
    _population_of(_ID_A, _ID_B, "agreeing"),
    # Name-only, key elsewhere: the Query hits and the scan is not needed.
    _population_of(_ID_A, _ID_B, "name-only (key elsewhere)"),
    # NOT referenced, and every record is a near miss. This is the population a
    # containment or begins_with predicate refuses and the requirement deletes.
    _population_of(
        _ID_A,
        _ID_B,
        "near miss: key contains the id, names another object",
        "near miss: the -metadata sidecar key",
        "near miss: key with a suffix",
        "near miss: name prefixed by the id",
        "unrelated",
        "unrelated (no name attribute)",
    ),
    # The empty table: nothing references anything, so the delete must happen.
    Population(_ID_A, _ID_B, ()),
    # The single-suffix id shape task 8.6 will produce, referenced.
    _population_of("uploaded_schema_1785844048926", _ID_B, "key-only (disagreeing)"),
    # An ARCHIVED record is the only reference — the modelling choice stated in
    # the module docstring, made explicit so it is reviewed rather than drawn.
    Population(
        _ID_A,
        _ID_B,
        (_record_kinds(_ID_A, _ID_B)[0]._replace(status="archived"),),
    ),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_POPULATIONS`` as explicit examples, in order."""
    for population in reversed(_CRITICAL_POPULATIONS):
        test = example(population=population)(test)
    return test


#: Every case ``_report_generated_cases`` observed, accumulated across the survey.
#: A module-level accumulator, because Hypothesis's statistics are per-example and
#: cannot carry a cross-example claim — and because **the property short-circuits**
#: while ``delete_schema`` is a ``501`` stub: it fails on its first example, so its
#: own statistics measure nothing. The survey keeps the coverage claim measurable
#: during exactly the window in which the property is expected to be red.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(population: Population) -> None:
    """Record which of the cases this property is about the example reached.

    Reported as Hypothesis events as well as accumulated, so
    ``--hypothesis-show-statistics`` gives the measured frequency of the
    disagreeing case and of both predicates firing.
    """

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    event(f"population size {len(population.records)}")
    event("referenced (refusal expected)" if population.is_referenced else "unreferenced (deletion expected)")

    by_key = [spec for spec in population.records if spec.by_key]
    by_name = [spec for spec in population.records if spec.by_name]
    disagreeing = [spec for spec in population.records if spec.by_key and not spec.by_name]

    if disagreeing:
        event("a disagreeing record: its KEY names the schema, its NAME does not (testing-39eddb17)")
    if disagreeing and not by_name:
        event("the ONLY reference is a disagreeing record — a Query-only lookup deletes here")
    if by_key and by_name:
        event("both predicates fire on the population")
    if any(spec.by_key and spec.by_name for spec in population.records):
        event("both predicates fire on ONE record (the consistent shape)")
    if any(spec.by_name and not spec.by_key for spec in population.records):
        event("a name-only reference (the Query answers it)")
    if any(spec.kind == "name-only (no key attribute)" for spec in population.records):
        event("a referencing record with no target_schema_s3_key at all")
    if any(spec.kind.startswith("near miss") for spec in population.records):
        event("a near-miss record that must NOT cause a refusal")
    if any(spec.references and spec.status == "archived" for spec in population.records):
        event("an archived record is a reference")
    if not population.records:
        event("an empty population (no pipeline records at all)")


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no AWS, no handler, nothing
# but the generator and the reporter.
@_with_critical_examples
@given(population=populations())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(population):
    """Every population, explicit or drawn, is a coherent set of record shapes.

    The per-example half of the survey. ``phases`` is limited to the explicit
    examples plus generation: there is no failure to shrink or explain and no
    reuse-database entry to replay.

    **Validates: Requirement 8.9**
    """
    _report_generated_cases(population)

    assert population.other_id != population.schema_id, "the bystander must be a different schema"
    assert len(population.records) <= _MAX_RECORDS, (
        f"the harness reclaims exactly {_MAX_RECORDS} pipeline ids, so a longer population would "
        f"leak records into the next example; drew {len(population.records)}"
    )
    assert len({spec.kind for spec in population.records}) == len(population.records), (
        f"each record kind may appear at most once: {[spec.kind for spec in population.records]}"
    )
    for spec in population.records:
        # The oracle written on the spec must agree with R3.7's disjunction. This
        # is the one place the two forms are compared, so a mislabelled row in
        # `_record_kinds` fails here rather than silently weakening the property.
        assert spec.references == (spec.by_key or spec.by_name), (
            f"record kind {spec.kind!r} labels itself references={spec.references} but "
            f"by_key={spec.by_key} / by_name={spec.by_name}"
        )
        if spec.by_key:
            assert spec.key == _body_key(population.schema_id), (
                f"{spec.kind!r} claims to reference by key but its key is {spec.key!r}"
            )
        else:
            assert spec.key != _body_key(population.schema_id), (
                f"{spec.kind!r} claims NOT to reference by key while naming the schema's own object"
            )
        if spec.by_name:
            assert spec.name == population.schema_id, (
                f"{spec.kind!r} claims to reference by name but its name is {spec.name!r}"
            )
        else:
            assert spec.name != population.schema_id, (
                f"{spec.kind!r} claims NOT to reference by name while carrying the schema id"
            )


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once in 200 draws.

    Task 8.1's assertion, reused for the same reason. Tasks 3.1 and 4.3 both found
    generators that reached their target case **0 times out of 200 and passed
    anyway**, caught only by someone reading ``--hypothesis-show-statistics`` by
    hand. Here the coverage claim is a test, so it fails when the generator drifts
    away from the cases rather than going quiet.

    The entry that matters most is "the ONLY reference is a disagreeing record".
    That is the population a Query-only lookup deletes, so a generator that
    stopped producing it would leave the property passing while testing nothing
    about the gap D4's ratification block names.

    Every required case is guaranteed by ``_CRITICAL_POPULATIONS`` rather than
    left to the draw, which is what makes this assertion deterministic instead of
    seed-dependent.

    **Validates: Requirement 8.9**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        "referenced (refusal expected)",
        "unreferenced (deletion expected)",
        "a disagreeing record: its KEY names the schema, its NAME does not (testing-39eddb17)",
        "the ONLY reference is a disagreeing record — a Query-only lookup deletes here",
        "both predicates fire on the population",
        "both predicates fire on ONE record (the consistent shape)",
        "a name-only reference (the Query answers it)",
        "a referencing record with no target_schema_s3_key at all",
        "a near-miss record that must NOT cause a refusal",
        "an archived record is a reference",
        "an empty population (no pipeline records at all)",
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(population=populations())
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto bucket and up to six DynamoDB
    # records, which routinely exceeds Hypothesis's 200ms default. The property
    # asserts an outcome, not latency.
    deadline=None,
    # One fixture serves every example by design; `_seed` wipes the bucket and
    # reclaims the six pipeline ids at the start of each one, so nothing an
    # earlier example wrote is visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` is excluded deliberately, on task 8.1's measurement: it
    # re-ran ~400 further examples to annotate a failure whose cause the
    # assertion message already states, at 72s of that file's 78s. This property
    # is expected to be RED from task 8.2 until task 8.4 and every checkpoint in
    # the plan runs the full suite, so that is a standing cost for no
    # information. Shrinking is kept — a minimal counter-example is what is
    # worth having when this fails for a real reason after 8.4 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_delete_is_refused_whenever_any_pipeline_references_the_schema(population, delete_env):
    """Property 4: a delete is refused by either predicate, and never both.

    Clause order is deliberate. R3.10 is checked **first**, because a handler that
    deleted and then refused must be reported as violating the criterion D4 was
    ratified for rather than as returning the wrong status. The predicate clauses
    come second, and each asserts the call **and** the object.

    **Validates: Requirements 3.7, 3.8, 3.10, 8.9**
    """
    _report_generated_cases(population)

    table = delete_env
    before = _seed(table, population)
    body_key = _body_key(population.schema_id)
    assert body_key in before, "the harness must seed the object the delete is about"

    with _recorded_s3_calls() as calls:
        response = schemas_handler.delete_schema(population.schema_id)

    status = response["statusCode"]
    after = _inventory()
    deleted = body_key not in after
    deletes = _delete_calls(calls)
    referencing_ids = [f"p35-del-{index}" for index, spec in enumerate(population.records) if spec.references]
    non_referencing_ids = [f"p35-del-{index}" for index, spec in enumerate(population.records) if not spec.references]
    shapes = [spec.kind for spec in population.records]

    # -- R3.10: a refusal and a deletion are never both observable -------------
    assert not (status == 409 and deleted), (
        f"R3.10: the response refused with 409 and the object is gone. The check must run "
        f"BEFORE the delete, so these two can never both be observable for one request.\n"
        f"  schema:  {population.schema_id!r}\n  records: {shapes}"
    )
    assert not (status == 409 and deletes), (
        f"R3.10: the response refused with 409 and still issued a delete.\n"
        f"  delete calls: {deletes}\n  records: {shapes}"
    )

    # -- 503 belongs to task 8.3, and nothing here interrupts the check --------
    assert status != 503, (
        f"nothing in this property throttles the lookup or exhausts a budget, so the reference "
        f"check can always finish and a 503 is not the correct answer here. Property 5 (task 8.3) "
        f"owns the interrupted case.\n  body: {_response_body(response)!r}\n  records: {shapes}"
    )

    if population.is_referenced:
        # -- R3.7 / R3.8: refuse, name them, and leave the object alone --------
        assert status == 409, (
            f"R3.7/R3.8: {len(population.referencing)} pipeline record(s) reference "
            f"{population.schema_id!r}, so the delete must be refused with 409. Got {status}.\n"
            f"  `delete_schema` is a 501 stub until task 8.4 — this is the expected red.\n"
            f"  referencing records: "
            f"{[(spec.kind, 'by_key' if spec.by_key else '', 'by_name' if spec.by_name else '') for spec in population.referencing]}\n"
            f"  body: {_response_body(response)!r}"
        )
        assert not deletes, (
            f"R3.7: a refused delete must perform no delete_object. `LGR-78ce0b7-14` makes the key "
            f"a referencing pipeline names a claim that the object exists, so removing it turns a "
            f"list action into a pipeline-breaking one.\n  delete calls: {deletes}"
        )
        assert not deleted, (
            f"R3.7: the object {body_key!r} is gone after a refusal. The call-level check passed, "
            f"so it was removed by something other than a recorded delete.\n  keys now: {sorted(after)}"
        )
        named = _named_pipeline_ids(response, referencing_ids + non_referencing_ids)
        assert named & set(referencing_ids), (
            f"R3.7: the refusal must NAME the referencing pipelines so the user can act on them "
            f"(R3.9 renders what this names). It named none of {referencing_ids}.\n"
            f"  body: {_response_body(response)!r}"
        )
        assert not (named & set(non_referencing_ids)), (
            f"R3.7: the refusal named pipelines that do not reference this schema — "
            f"{sorted(named & set(non_referencing_ids))}. A user acting on that list would edit "
            f"the wrong pipelines.\n  body: {_response_body(response)!r}"
        )
    else:
        # -- R3.1: no reference, so the delete happens and succeeds ------------
        assert 200 <= status < 300, (
            f"R3.1: no pipeline record references {population.schema_id!r} — every record is a "
            f"near miss or a bystander — so the object must be deleted and the response must "
            f"succeed. Got {status}.\n"
            f"  `delete_schema` is a 501 stub until task 8.4 — this is the expected red.\n"
            f"  records: {shapes}\n  body: {_response_body(response)!r}"
        )
        assert deleted, (
            f"R3.1: the response claimed success while {body_key!r} is still there. A success is a "
            f"claim the object is gone (`LGR-78ce0b7-14` read the other way round).\n"
            f"  keys now: {sorted(after)}\n  records: {shapes}"
        )
        # The bystander schema is never in scope for this request.
        assert _body_key(population.other_id) in after, (
            f"a delete of {population.schema_id!r} removed {population.other_id!r}'s object too.\n"
            f"  keys now: {sorted(after)}"
        )


# ---------------------------------------------------------------------------
# The live record, on its own, as a readable test
# ---------------------------------------------------------------------------
def test_the_live_disagreeing_record_alone_refuses_the_delete(delete_env):
    """``testing-39eddb17``'s shape, and nothing else in the table.

    The population every clause of D4's ratification block is about, written out
    as one test so it reads as a scenario rather than as an example id inside a
    property. One record, whose ``target_schema_s3_key`` names our object while
    its ``target_schema_name`` names something else.

    **Expected RED until task 8.4**, on the status: the stub answers ``501``.

    **Validates: Requirements 3.7, 3.8, 8.9**
    """
    schema_id = _ID_A
    population = _population_of(schema_id, _ID_B, "key-only (disagreeing)")
    _seed(delete_env, population)

    record = delete_env.get_item(Key={"pipeline_id": "p35-del-0"})["Item"]
    # The shape, pinned, so a later edit to `_record_kinds` cannot quietly turn
    # this into an agreeing record and leave the test passing for a Query-only
    # implementation.
    assert record["target_schema_s3_key"] == _body_key(schema_id)
    assert record["target_schema_name"] != schema_id

    with _recorded_s3_calls() as calls:
        response = schemas_handler.delete_schema(schema_id)

    assert response["statusCode"] == 409, (
        f"R3.8: pipeline p35-del-0's `target_schema_s3_key` names {_body_key(schema_id)!r}, so the "
        f"delete must be refused. Got {response['statusCode']}. `delete_schema` is a 501 stub "
        f"until task 8.4 — the expected red. A Query on `target_schema_name-index` cannot see this "
        f"record, which is why the lookup cannot be Query-only."
    )
    assert not _delete_calls(calls)
    assert _body_key(schema_id) in _inventory()


# ---------------------------------------------------------------------------
# The tripwire, proved armed
# ---------------------------------------------------------------------------
class TestTheTripwireIsArmed:
    """``_delete_calls`` catches the calls it exists to catch.

    The absence assertions pass today because ``delete_schema`` makes no call at
    all, so on their own they are indistinguishable from a spy that records
    nothing and an oracle that forbids nothing. These tests do the forbidden
    things **themselves** and require the check to report each one, so the guard
    is known-good before there is any code for it to guard.
    """

    def test_the_spy_records_real_calls_and_lets_them_through(self, delete_env):
        """A recorded call is still a real call: the object is really removed."""
        _put(_body_key("spy_check"), b"{}")

        with _recorded_s3_calls() as calls:
            _client("s3").delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("spy_check"))

        assert _body_key("spy_check") not in _inventory(), "the spy must call through, not stand in for moto"
        assert _delete_calls(calls) == [f"DeleteObject(Key={_body_key('spy_check')!r})"]

    def test_a_batch_delete_is_reported_too(self, delete_env):
        """``DeleteObjects`` is the same intention under another name."""
        _put(_body_key("batch_check"), b"{}")

        with _recorded_s3_calls() as calls:
            _client("s3").delete_objects(
                Bucket=TARGET_SCHEMAS_BUCKET, Delete={"Objects": [{"Key": _body_key("batch_check")}]}
            )

        reported = _delete_calls(calls)
        assert len(reported) == 1 and reported[0].startswith("DeleteObjects"), (
            f"a batch delete must be reported; got {reported}"
        )

    def test_reads_are_not_forbidden(self, delete_env):
        """The reads a correct reference check makes are not delete calls.

        A guard that reported ``head_object`` / ``get_object`` /
        ``list_objects_v2`` would make the refusal case unsatisfiable, because
        R3.5's already-absent branch needs a read to distinguish absent from
        present.
        """
        _put(_body_key("read_check"), b"{}")

        with _recorded_s3_calls() as calls:
            s3 = _client("s3")
            s3.head_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("read_check"))
            s3.get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("read_check"))
            s3.list_objects_v2(Bucket=TARGET_SCHEMAS_BUCKET, Prefix="shared/")

        assert _delete_calls(calls) == [], "a reference check reads; only a removal is forbidden"


# ---------------------------------------------------------------------------
# Satisfiability, and what Query-only gets wrong — measured, not asserted
# ---------------------------------------------------------------------------
class TestTheSpecificationIsSatisfiable:
    """The property can be satisfied, and only by a lookup that scans.

    Design §5 risk 9: a reviewer who sees red and reverts has undone the only
    thing that would have caught the defect. These tests separate "the subject is
    not written yet" from "the property cannot be satisfied".

    The two probes below are **discrimination probes, not reference
    implementations.** They exist so the oracle can be shown to accept the
    prescribed lookup and reject the rejected one; the property itself never
    calls them — it calls ``schemas_handler.delete_schema``. Task 8.4 still owns
    the real handler, with the wall-clock budget, ``ReferenceCheckIncomplete``,
    R3.3's ``400``, R3.5's already-absent success and the ``checked`` key, none of
    which is here.
    """

    @staticmethod
    def _query_pages(table, schema_id: str) -> list[dict[str, Any]]:
        """Every page of the Query on ``target_schema_name-index``."""
        hits: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": "target_schema_name-index",
            "KeyConditionExpression": Key("target_schema_name").eq(schema_id),
        }
        while True:
            page = table.query(**kwargs)
            hits.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return hits
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @staticmethod
    def _scan_pages(table, expected_key: str) -> list[dict[str, Any]]:
        """Every page of the Scan filtered on ``target_schema_s3_key``.

        ``eq``, not ``contains``: R3.8's predicate is that the key **names** the
        schema's object, and ``contains`` would match
        ``shared/uploaded_{id}.json``, which names a different one.
        """
        hits: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"FilterExpression": Attr("target_schema_s3_key").eq(expected_key)}
        while True:
            page = table.scan(**kwargs)
            hits.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return hits
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @classmethod
    def _delete_via(cls, table, schema_id: str, *, scan: bool) -> tuple[int, list[str]]:
        """A minimal delete: refuse if the lookup finds anything, else delete.

        With ``scan=True`` this is design §4.2's Query-then-Scan; with
        ``scan=False`` it is the rejected Query-only. Returns the status it would
        answer and the pipeline ids it found.
        """
        hits = cls._query_pages(table, schema_id)
        if not hits and scan:
            hits = cls._scan_pages(table, _body_key(schema_id))
        if hits:
            return 409, sorted(str(item["pipeline_id"]) for item in hits)
        _client("s3").delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(schema_id))
        return 200, []

    @pytest.mark.parametrize("population", _CRITICAL_POPULATIONS, ids=lambda p: f"{len(p.records)}-records")
    def test_query_then_scan_satisfies_the_property_on_every_critical_population(self, population, delete_env):
        """The prescribed lookup answers every population the property is about.

        Same clauses as the property, against the probe rather than the handler:
        refuse and keep the object when referenced, delete and succeed when not.
        """
        _seed(delete_env, population)

        with _recorded_s3_calls() as calls:
            status, found = self._delete_via(delete_env, population.schema_id, scan=True)

        present = _body_key(population.schema_id) in _inventory()
        if population.is_referenced:
            assert status == 409, f"{[s.kind for s in population.records]} is referenced"
            assert present and not _delete_calls(calls)
            expected = {f"p35-del-{i}" for i, spec in enumerate(population.records) if spec.references}
            assert set(found) <= expected and set(found), (
                f"the lookup must find referencing records and only those: found {found}, "
                f"referencing {sorted(expected)}"
            )
        else:
            assert 200 <= status < 300, f"{[s.kind for s in population.records]} is not referenced"
            assert not present, "an unreferenced schema's object must be gone"

    def test_query_only_deletes_the_schema_the_live_disagreeing_record_addresses(self, delete_env):
        """The rejected lookup, and exactly what it costs.

        This is the measurement behind "Do NOT implement Query-only": on the live
        ``testing-39eddb17`` population the Query finds nothing, so the object is
        deleted out from under a pipeline that addresses it — and the pipeline
        record is still there afterwards, still naming a key that no longer
        resolves. ``resolve_target_schema`` raises ``target_schema_object_missing``
        on exactly that state (``shared/target_schema_resolution.py:290-302``,
        ``LGR-78ce0b7-14``).
        """
        population = _population_of(_ID_A, _ID_B, "key-only (disagreeing)")
        _seed(delete_env, population)

        query_only_status, query_only_found = self._delete_via(delete_env, _ID_A, scan=False)

        assert query_only_status == 200 and query_only_found == [], (
            "the Query cannot see a record whose name disagrees with its key — that is the gap"
        )
        assert _body_key(_ID_A) not in _inventory(), (
            "Query-only deletes the object here, which is the failure D4 exists to prevent"
        )
        orphan = delete_env.get_item(Key={"pipeline_id": "p35-del-0"})["Item"]
        assert orphan["target_schema_s3_key"] == _body_key(_ID_A), (
            "and the pipeline record still names the key that no longer resolves"
        )

        # The same population, answered by the prescribed lookup: refused, object
        # intact. Re-seeded so the two are compared on identical input.
        _seed(delete_env, population)
        scan_status, scan_found = self._delete_via(delete_env, _ID_A, scan=True)
        assert scan_status == 409 and scan_found == ["p35-del-0"]
        assert _body_key(_ID_A) in _inventory()


# ---------------------------------------------------------------------------
# Today's answer, pinned — REMOVED BY TASK 8.4, on its own instruction
# ---------------------------------------------------------------------------
# `test_the_stub_answers_501_and_deletes_nothing` lived here while
# `delete_schema` was a `501` stub, so that the red clauses above read as "the
# subject does not exist yet" rather than as a defect in the property. Its
# docstring was its own expiry note: "Task 8.4 will make this test fail, and that
# failure is the signal to delete this test, not to loosen it."
#
# Task 8.4 landed the handler, so it is gone. The property above is now the
# statement about the delete, which is what it was always for; nothing about the
# handler's return value in isolation is asserted in this file any more.
# `test_schema_delete_endpoint.py` owns the example-level outcomes (R3.3's `400`,
# R3.5's already-absent success, and each outcome's `checked`).
