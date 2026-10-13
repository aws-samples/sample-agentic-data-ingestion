# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 8 — field context round-trips into a created pipeline, and its absence is not an error.

# Feature: p35-schema-surfaces, Property 8: Field context round-trips into a created pipeline, and its absence is not an error

For any set of field-context writes against an uploaded schema, a pipeline
subsequently created against that schema carries the stored context for each
annotated field; and for any uploaded schema with **no** sidecar, creation
succeeds and emits no ``ERROR``-level record about it.

**Validates: Requirements 2.3, 2.4, 2.6, 8.5**

WHY THIS PROPERTY EXISTS ON BOTH SIDES OF ONE OBJECT
----------------------------------------------------
D3 (``LGR-acca05d-3``, **template / ratified**) implemented a **writer** for an
object that already had a **reader**. ``handlers/pipelines_crud.py:800`` has been
reading ``metadata.get("field_contexts", {}).get(field_name, "")`` all along, and
nothing in the repository ever wrote it — verified in ``requirements.md``: the
only ``put_object`` into that bucket was the schema body, so the ``NoSuchKey``
logged on every create-pipeline run was **structural, not a race**. A property
over a writer alone would prove the writer self-consistent. What matters is that
the writer's object is at the address the reader reads and in the shape the
reader parses, because a second definition of either is exactly how
``LGR-cad8032-1`` (**template / ratified**) records a feature silently not
running.

So both halves drive **real** code: ``schemas.update_field_context`` writes,
``schemas._read_context_sidecar`` and ``schemas.get_schema`` read, and
``pipelines_crud.create_pipeline`` runs end to end against the same moto backend.
No expectation in this file is transcribed from a producer's output
(``LGR-28b4d75-5`` / R8.7); the oracle is a model replayed from the generated
writes.

WHAT THE ROUND-TRIP HALF CAN AND CANNOT OBSERVE — READ THIS BEFORE STRENGTHENING IT
-----------------------------------------------------------------------------------
**``create_pipeline`` builds ``target_schema_details`` and then discards it.**
Read end to end: it is initialised at ``pipelines_crud.py:703``, assigned at
``:791``, appended to at ``:802`` — and it is **never** written onto
``pipeline_item``, never returned, and never logged. Confirmed by grep across the
whole module: four occurrences, none of them a read. The created record carries
``target_schema_name`` and ``target_schema_s3_key`` and nothing schema-detail
shaped, which was checked against an actual created record rather than assumed.

That has a consequence for R2.4 worth stating plainly rather than papering over:
**the "created pipeline carries the stored context" clause is not observable on
the pipeline record**, because the structure that would carry it is a dead local.
This property therefore asserts the strongest thing that *is* observable, in
three parts, and does **not** pretend to have observed the fourth:

===================================================  ==============================================
claim                                                 how it is asserted here
===================================================  ==============================================
1. the written context is readable back, exactly       through ``_read_context_sidecar`` — the
   the model says                                      reader task 8.5's writer must round-trip
                                                       with, and the same
                                                       ``["field_contexts"][name]`` expression
                                                       ``pipelines_crud.py:800`` uses
2. a consumer that renders fields carries it           through ``get_schema``, whose per-field
                                                       ``context`` must equal the model
3. the pipeline path reads the address the writer       through a pass-through S3 spy over a real
   wrote, and creation succeeds                        ``create_pipeline`` call: the recorded
                                                       ``GetObject`` names
                                                       ``schemas/uploaded/{id}-metadata.json``
                                                       and the response is ``201``
4. ``target_schema_details``' ``context`` values        **NOT ASSERTED — not observable.** See
                                                       above. Reported as a finding of task 8.5,
                                                       not fixed by it: persisting that structure
                                                       is a change to another handler with its own
                                                       blast radius, and neither task 8.5 nor
                                                       design §7's C3 asks for one
===================================================  ==============================================

THE SECOND HALF IS A METAMORPHIC NO-OP CHECK OVER A DELIBERATE TOLERANCE
-----------------------------------------------------------------------
``requirements.md`` §"Current state — already working, do NOT reimplement" says:
"**``create_pipeline`` tolerates a missing Context_Sidecar by design.** The read
at ``pipelines_crud.py:783-789`` is wrapped and logs at ``warning``, and creation
continues. Requirement 2.6 preserves that." Now that a writer exists, the
tempting cleanup is to treat the ``NoSuchKey`` as a real failure — it is no
longer structural, after all. R2.6 forbids exactly that, and this half is the
guard: with **no** sidecar, creation still answers ``201``, the read is still
**attempted** (asserted through the spy, so the branch is known to have been
reached rather than skipped), and **no ``ERROR``-level record about the sidecar is
emitted**. The log level is captured through a pass-through spy on
``pipelines_crud``'s own logger, so a one-word change from ``logger.warning`` to
``logger.error`` at ``:789`` turns this red.

FIXTURES, REGION, AND WHY THE BUCKET NAME IS NOT OVERRIDDEN FOR BOTH SIDES
--------------------------------------------------------------------------
Shared fixtures only — ``mock_dynamodb_tables`` and ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call and no boto3 client
mock (``tests/unit/test_no_duplicate_mocking_prop.py``), and this file does
**not** set ``AWS_REGION`` (``LGR-3e45e65-1``) — it reads it, through the same
``os.environ.get("AWS_REGION", "us-west-2")`` expression the handler uses.

The two sides derive their bucket differently and that is why the fixture sets
things up the way it does: ``create_pipeline`` composes
``f"adi-target-schemas-{account_id}-{AWS_REGION}"`` (``pipelines_crud.py:739``)
and does **not** consult ``TARGET_SCHEMAS_BUCKET``, while ``schemas.py`` prefers
that variable. So the bucket is **created under the name ``create_pipeline``
derives** and ``TARGET_SCHEMAS_BUCKET`` is pointed at it, which is the only
arrangement in which a writer/reader address disagreement would be a real
failure of the code rather than an artefact of the harness. ``create_bucket``
uses a ``us-east-1`` client because a region-specific endpoint rejects an
unspecified ``LocationConstraint``; moto's S3 backend is not region-partitioned,
so the object is the same object either way.

The three Lambda-layer stubs at the top are the preamble
``test_create_pipeline_target_schema_location.py`` established for importing
``handlers.pipelines_crud`` outside a Lambda; ``get_aws_account_id`` is rebound on
**both** handler modules so neither reaches STS and both agree on the account.
``_provision_pipeline_resources`` is patched out for the same reason it is there:
it is the S3-prefix/EventBridge unit of work that follows the DynamoDB write and
has nothing to do with the sidecar.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.client import BaseClient
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import event as _hypothesis_event
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Lambda-layer stubs and path — must precede the handler imports
# ---------------------------------------------------------------------------
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

_mock_audit_utils = MagicMock()
_mock_audit_utils.extract_request_context = MagicMock(return_value={})
_mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = _mock_audit_utils

_mock_env_utils = MagicMock()
_mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _mock_env_utils

SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from handlers import pipelines_crud as pipelines_handler
    from handlers import schemas as schemas_handler

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

#: The schema body every example is written against. Its ``properties`` are what
#: ``create_pipeline`` iterates (``pipelines_crud.py:797``) and what
#: ``_uploaded_schema_fields`` turns into the detail surface's field list, so
#: these three names are the ones a stored context can actually land on.
SCHEMA_PROPERTIES = ("customer_id", "email", "nickname")
SCHEMA_BODY = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "integer", "description": "Surrogate key"},
        "email": {"type": "string", "description": "Primary contact address"},
        "nickname": {"type": "string"},
    },
    "required": ["customer_id"],
}


# ---------------------------------------------------------------------------
# The two addresses, written out from the requirements rather than imported
# ---------------------------------------------------------------------------
def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention."""
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{schema_id}-metadata.json`` — R2.3 / R4.12 verbatim.

    Spelled out here rather than imported from ``schemas.py`` so the property's
    oracle is the requirement and not the code under test. It is also the
    expression ``pipelines_crud.py:785`` composes, which is the whole point: one
    address, two modules.
    """
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _region() -> str:
    """``os.environ.get("AWS_REGION", "us-west-2")`` — ``schemas.py:23`` verbatim."""
    return os.environ.get("AWS_REGION", "us-west-2")


def _bucket() -> str:
    """The bucket ``create_pipeline`` derives (``pipelines_crud.py:739``), built its way."""
    return f"adi-target-schemas-{ACCOUNT_ID}-{pipelines_handler.AWS_REGION}"


def _client(service: str):
    return boto3.client(service, region_name=_region())


# ---------------------------------------------------------------------------
# Two pass-through spies — calls and log levels
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _recorded_s3_calls():
    """Record every S3 API call made inside the block, then call through.

    A spy, not a mock: the wrapper appends ``(operation, params)`` and returns
    ``original(...)``, so every call reaches moto's backend and every response is
    moto's. It wraps ``BaseClient._make_api_call``, above moto's ``before-send``
    stubber, so it sees calls from clients the handler constructs itself — which
    is the only way to assert that a read was **attempted**.
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


@contextlib.contextmanager
def _recorded_error_logs(module):
    """Record ``logger.error`` / ``logger.exception`` on one module, then call through.

    R2.6 is a statement about a **log level**, so the level is what has to be
    observed. Powertools' ``Logger`` is not wired into ``caplog`` by default
    (it does not propagate), so this shadows the two error-level methods as
    instance attributes and restores whatever was there before — including
    restoring *absence*, since the originals are resolved through delegation
    rather than living in ``__dict__``.

    ``warning`` is deliberately **not** captured. Pinning "it must be a warning"
    would freeze a level R2.6 does not specify; R2.6 says only that an absent
    sidecar must not be logged as an error, and that is exactly what is asserted.
    """
    recorded: list[tuple[str, str]] = []
    names = ("error", "exception")
    originals = {name: getattr(module.logger, name) for name in names}
    was_own = {name: name in module.logger.__dict__ for name in names}

    def _spy(level: str, original):
        def wrapper(message, *args, **kwargs):
            try:
                rendered = str(message) % args if args else str(message)
            except (TypeError, ValueError):
                rendered = f"{message} {args}"
            recorded.append((level, rendered))
            return original(message, *args, **kwargs)

        return wrapper

    for name in names:
        setattr(module.logger, name, _spy(name.upper(), originals[name]))
    try:
        yield recorded
    finally:
        for name in names:
            if was_own[name]:
                setattr(module.logger, name, originals[name])
            else:
                module.logger.__dict__.pop(name, None)


#: Words that make an ``ERROR`` record be *about the sidecar*. R2.6 forbids
#: reporting the sidecar's absence as an error; it says nothing about unrelated
#: errors, so the filter is on the subject rather than on the count.
#: ``pipelines_crud.py:789``'s message is "Failed to load schema metadata from
#: S3: …", so ``metadata`` is the word a level change would surface under.
_SIDECAR_WORDS = ("metadata", "sidecar", "field_contexts", "-metadata.json")


def _error_logs_about_the_sidecar(records: list[tuple[str, str]]) -> list[str]:
    return [message for level, message in records if any(word in message.lower() for word in _SIDECAR_WORDS)]


def _sidecar_reads(calls: list[tuple[str, dict[str, Any]]], schema_id: str) -> list[str]:
    """Every recorded read against the Context_Sidecar's address."""
    key = _sidecar_key(schema_id)
    return [
        operation
        for operation, params in calls
        if params.get("Key") == key and operation in ("GetObject", "HeadObject")
    ]


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def roundtrip_env(mock_dynamodb_tables, mock_s3_buckets, monkeypatch):
    """Both handlers bound to one moto backend, agreeing on one bucket.

    See the module docstring for why the bucket is created under the name
    ``create_pipeline`` derives rather than under the fixture's own, and why
    ``create_bucket`` uses a ``us-east-1`` client.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=_bucket())
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", _bucket())
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setattr(pipelines_handler, "get_aws_account_id", lambda: ACCOUNT_ID)
    monkeypatch.setattr(schemas_handler, "get_aws_account_id", lambda: ACCOUNT_ID)
    monkeypatch.setattr(pipelines_handler, "pipelines_table", mock_dynamodb_tables[PIPELINES_TABLE])
    tsr._s3_client_cache = None
    with patch.object(pipelines_handler, "_provision_pipeline_resources"):
        yield mock_dynamodb_tables[PIPELINES_TABLE]
    tsr._s3_client_cache = None


def _wipe_bucket() -> None:
    """Empty the bucket so each example starts clean.

    Hypothesis reuses one fixture across every example; without this, a sidecar
    written by an earlier example would make the no-sidecar half pass — or fail —
    for the wrong reason. Runs in the harness, outside the recorders' windows.
    """
    s3 = _client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=_bucket()):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=_bucket(), Delete={"Objects": keys})


def _seed_body(schema_id: str) -> None:
    _client("s3").put_object(Bucket=_bucket(), Key=_body_key(schema_id), Body=json.dumps(SCHEMA_BODY))


def _sidecar_object(schema_id: str) -> dict[str, Any] | None:
    s3 = _client("s3")
    try:
        return json.loads(s3.get_object(Bucket=_bucket(), Key=_sidecar_key(schema_id))["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _context_event(text: str) -> dict[str, Any]:
    """``PUT /api/schemas/{id}/fields/{name}/context`` with ``{"context": …}``.

    The key ``use-schemas.ts:121-123`` already sends, and the one R2.3 names.
    """
    return {"httpMethod": "PUT", "body": json.dumps({"context": text})}


def _create_pipeline(schema_id: str, name: str = "Property 8"):
    return pipelines_handler.create_pipeline(
        json.dumps(
            {
                "pipeline_name": name,
                "target_schema": schema_id,
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "schema_source": "existing",
            }
        ),
        event={},
    )


# ---------------------------------------------------------------------------
# Generators and the model
# ---------------------------------------------------------------------------
class Scenario(NamedTuple):
    """One uploaded schema and a **sequence** of field-context writes.

    An empty ``writes`` is not a degenerate case to be filtered out — it is the
    property's second half: no write means no sidecar object at all, which is the
    state R2.6's tolerance is about.
    """

    schema_id: str
    writes: tuple[tuple[str, str], ...]


#: Both live id shapes. The double-suffix form is the pre-R4.5 shape live record
#: ``testing-39eddb17`` carries; the single-suffix form is what task 8.6 will
#: produce. The sidecar's address is derived from the id, so a round trip must be
#: invariant over both — and R4.5 must not be something this property is
#: sensitive to.
_SCHEMA_IDS = st.sampled_from(
    (
        "uploaded_schema_1785844048926",
        "uploaded_schema_1785844048926_20260804_114729",
        "schema_1785844048926_20260804_114729",
        "p32ver_ren_renC",
        "customers_jsonschema",
    )
)

#: Field names: the schema's own three, plus one that is **not** in the body.
#: The off-schema name is deliberate — R2.3 stores what was submitted, and the
#: readers look a stored context up **by field name**, so a context for a field
#: the body does not declare must round-trip through the sidecar and appear on
#: **no** field of the detail surface. An implementation that validated the name
#: against the body would pass every on-schema example.
_FIELD_NAMES = st.sampled_from((*SCHEMA_PROPERTIES, "shipping_zip"))

#: Context texts. The empty and whitespace-only entries are the **clear**, which
#: is a write like any other and has to be in the model. Free-form text is drawn
#: from a utf-8-encodable alphabet with surrogates excluded, because a lone
#: surrogate cannot be encoded into a JSON body at all — generating one would
#: test Python's codec rather than the round trip.
_CONTEXT_TEXTS = st.one_of(
    st.sampled_from(
        (
            "PII — hashed downstream",
            "  padded context  ",
            "line one\nline two",
            "Kundendaten Ünïcodé",
            'quotes "and" \\backslashes\\',
            "",
            "   ",
            "a" * 400,
        )
    ),
    st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), min_size=0, max_size=32),
)


def _model(writes: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """The ``field_contexts`` the sidecar must hold after replaying ``writes``.

    The whole oracle, and it is derived from the requirements rather than from the
    handler: R2.3 persists **the submitted text**, so the last write for a field
    wins and it is stored **verbatim** — no trimming, which is why
    ``"  padded context  "`` is expected with its padding. An empty or
    whitespace-only submission **clears** the field, which is the choice task 8.5
    made where R2 is silent, argued in the block comment above
    ``update_field_context``: ``""`` and absent are already indistinguishable to
    both readers (``_schema_detail`` and ``pipelines_crud.py:800`` each
    ``.get(field_name, "")``), so absence gets exactly one representation.
    """
    contexts: dict[str, str] = {}
    for field_name, text in writes:
        if text.strip():
            contexts[field_name] = text
        else:
            contexts.pop(field_name, None)
    return contexts


#: The cases this property is about, as **explicit** inputs rather than as draws
#: that might happen. Promoted after the survey below made the distribution
#: measurable: ``one_of(sampled_from(...), text(...))`` biases toward its first
#: branch and ``sampled_from`` toward its first elements, so a coverage claim left
#: to the seed is not a claim. The pool stays — it is what gives the random half
#: its variety — and these are guaranteed.
_ID_A = "uploaded_schema_1785844048926_20260804_114729"
_CRITICAL_SCENARIOS = (
    # The second half: no write at all, so NO sidecar object exists. R2.6.
    Scenario(_ID_A, ()),
    # The ordinary case.
    Scenario(_ID_A, (("email", "PII — hashed downstream"),)),
    # Two writes to the SAME field — the last one wins.
    Scenario(_ID_A, (("email", "first"), ("email", "second"))),
    # A clear of a field that was set: the sidecar exists, the field does not.
    Scenario(_ID_A, (("email", "PII — hashed downstream"), ("email", "   "))),
    # A clear of a field that was never set — a no-op that must not fail.
    Scenario(_ID_A, (("nickname", ""),)),
    # Verbatim storage, R2.3: the padding survives.
    Scenario(_ID_A, (("email", "  padded context  "),)),
    # A context for a field the schema body does not declare.
    Scenario(_ID_A, (("shipping_zip", "not in the body"),)),
    # Every declared field annotated at once.
    Scenario(_ID_A, tuple((name, f"context for {name}") for name in SCHEMA_PROPERTIES)),
)


def _with_critical_examples(test):
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


@st.composite
def roundtrip_scenarios(draw) -> Scenario:
    """One uploaded schema id and 0–4 field-context writes."""
    schema_id = draw(_SCHEMA_IDS)
    writes = draw(st.lists(st.tuples(_FIELD_NAMES, _CONTEXT_TEXTS), min_size=0, max_size=4))
    return Scenario(schema_id, tuple(writes))


#: Every case the survey observed, accumulated across examples. A module-level
#: accumulator because Hypothesis's statistics are per-example and cannot carry a
#: cross-example claim, and because a failing property short-circuits — the
#: survey keeps the coverage claim measurable even in a window where the property
#: is red.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached.

    Tasks 3.1 and 4.3 both found generators that reached their target case **0
    times out of 200 and passed anyway**. These events, plus the aggregate
    assertion below, are what make that visible here rather than later.
    """

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    event(f"{len(scenario.writes)} write(s)")
    model = _model(scenario.writes)
    if not scenario.writes:
        event("NO write at all — no sidecar object exists (R2.6's tolerance)")
    else:
        event("at least one write")
    if model:
        event("at least one context survives into the sidecar")
    if scenario.writes and not model:
        event("writes happened but every one of them cleared (an empty field_contexts)")
    if any(not text.strip() for _, text in scenario.writes):
        event("a clear (empty or whitespace-only context)")
    seen: set[str] = set()
    for field_name, _ in scenario.writes:
        if field_name in seen:
            event("two writes to the SAME field (last one wins)")
            break
        seen.add(field_name)
    if any(text != text.strip() and text.strip() for _, text in scenario.writes):
        event("a context with leading or trailing whitespace, stored verbatim (R2.3)")
    if any(name not in SCHEMA_PROPERTIES for name, _ in scenario.writes):
        event("a context for a field the schema body does not declare")
    if any("\n" in text for _, text in scenario.writes):
        event("a multi-line context")
    if len({name for name, _ in scenario.writes} & set(SCHEMA_PROPERTIES)) >= 2:
        event("two or more declared fields annotated")


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no AWS, no handler, nothing
# but the generator and the model.
@_with_critical_examples
@given(scenario=roundtrip_scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(scenario):
    """Every scenario is a usable schema id and a replayable write sequence.

    **Validates: Requirement 8.5**
    """
    _report_generated_cases(scenario)

    assert scenario.schema_id, "a scenario must name a schema"
    assert not scenario.schema_id.startswith(("glue_", "s3_table_")), (
        f"an id this property writes against must be an Uploaded_Schema, not catalog-derived; "
        f"drew {scenario.schema_id!r} — a catalog id is R2.5's 400 and belongs in the example tests"
    )
    assert len(scenario.writes) <= 4
    model = _model(scenario.writes)
    assert all(text.strip() for text in model.values()), (
        f"the model must never carry an empty context — an empty write is a clear; got {model}"
    )


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once in 200 draws.

    The two entries that matter most are the **no-write** case and the **clear**.
    The first is the property's whole second half — a generator that stopped
    producing it would leave R2.6's tolerance untested while the property still
    passed. The second is what distinguishes a writer that stores ``""`` from one
    that removes the key, which is the choice task 8.5 had to make.

    Every required case is guaranteed by ``_CRITICAL_SCENARIOS`` rather than left
    to the draw, which is what makes this deterministic instead of
    seed-dependent.

    **Validates: Requirement 8.5**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        "NO write at all — no sidecar object exists (R2.6's tolerance)",
        "at least one write",
        "at least one context survives into the sidecar",
        "writes happened but every one of them cleared (an empty field_contexts)",
        "a clear (empty or whitespace-only context)",
        "two writes to the SAME field (last one wins)",
        "a context with leading or trailing whitespace, stored verbatim (R2.3)",
        "a context for a field the schema body does not declare",
        "a multi-line context",
        "two or more declared fields annotated",
        "0 write(s)",
        "1 write(s)",
        "3 write(s)",
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=roundtrip_scenarios())
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto bucket and runs a real
    # `create_pipeline`, which routinely exceeds Hypothesis's 200ms default. The
    # property asserts a round trip, not latency.
    deadline=None,
    # One fixture serves every example by design; `_wipe_bucket` runs at the
    # start of each one, so nothing an earlier example wrote is visible later.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded for the reason Property 3 records: it re-runs
    # hundreds of further examples to annotate a failure whose cause the
    # assertion message already states verbatim. Shrinking is kept.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_field_context_round_trips_and_its_absence_is_not_an_error(scenario, roundtrip_env):
    """Property 8, both halves, over one generated write sequence.

    Ordered so a failure localises: the writes are made and read back first, then
    the pipeline is created and the sidecar's absence-tolerance is checked. See
    the module docstring's table for the one clause that is **not** asserted and
    why it cannot be.

    **Validates: Requirements 2.3, 2.4, 2.6, 8.5**
    """
    _report_generated_cases(scenario)

    schema_id = scenario.schema_id
    expected = _model(scenario.writes)

    _wipe_bucket()
    _seed_body(schema_id)

    # ── The writes ────────────────────────────────────────────────────────
    with _recorded_s3_calls() as write_calls:
        for field_name, text in scenario.writes:
            response = schemas_handler.update_field_context(schema_id, field_name, _context_event(text))
            assert response["statusCode"] == 200, (
                f"R2.3: a context write against an uploaded schema must be stored, not refused. "
                f"field={field_name!r} text={text!r} → {response}"
            )

    # The writer touched exactly one object, and it is the sidecar (R4.10's rule
    # applies to this writer too — it shares the read-modify-write with the
    # rename). Stated here as well as in Property 3 because a context write is
    # the *other* caller of that function.
    written_keys = {params.get("Key") for operation, params in write_calls if operation == "PutObject"}
    assert written_keys <= {_sidecar_key(schema_id)}, (
        f"a context write may write only the Context_Sidecar; it also wrote {sorted(written_keys - {_sidecar_key(schema_id)})}"
    )

    # ── Clause 1: read back through the reader the writer must agree with ──
    sidecar = schemas_handler._read_context_sidecar(schema_id)
    if scenario.writes:
        assert sidecar.get("field_contexts") == expected, (
            f"R2.3: the sidecar must hold exactly the replayed writes, verbatim.\n"
            f"  writes:   {scenario.writes}\n  expected: {expected}\n  stored:   {sidecar.get('field_contexts')!r}"
        )
        assert _sidecar_object(schema_id) is not None, "a write must leave the sidecar object at its address"
    else:
        # No write means no object at all — not an empty one. R2.6's subject is
        # an absent sidecar, so the property must actually produce that state.
        assert sidecar == {}, f"no write happened, so there is nothing to read; got {sidecar!r}"
        assert _sidecar_object(schema_id) is None, (
            "the no-write case must leave NO sidecar object — otherwise the second half is "
            "testing a present-but-empty sidecar, which is a different claim"
        )

    # ── Clause 2: a rendering consumer carries it ─────────────────────────
    detail = json.loads(schemas_handler.get_schema(schema_id)["body"])
    for field in detail["fields"]:
        assert field["context"] == expected.get(field["name"], ""), (
            f"R2.4: field {field['name']!r} must carry the stored context, or '' when it has none.\n"
            f"  expected: {expected.get(field['name'], '')!r}\n  got:      {field['context']!r}"
        )
    # A context written for a field the body does not declare is stored and
    # surfaces on no field — the readers look up by name, they do not invent one.
    assert {field["name"] for field in detail["fields"]} == set(SCHEMA_PROPERTIES)

    # ── Clause 3: the pipeline path reads that address, and creation works ──
    with _recorded_error_logs(pipelines_handler) as error_logs, _recorded_s3_calls() as create_calls:
        created = _create_pipeline(schema_id)

    assert created["statusCode"] == 201, (
        f"R2.6: creating a pipeline against this schema must succeed whether or not a sidecar "
        f"exists. writes={scenario.writes} → {created}"
    )
    assert _sidecar_reads(create_calls, schema_id), (
        f"create_pipeline must READ the Context_Sidecar at {_sidecar_key(schema_id)!r} — that read is "
        f"the consumer D3 exists to feed (`pipelines_crud.py:785-800`). Recorded keys: "
        f"{sorted({params.get('Key') for _, params in create_calls if params.get('Key')})}"
    )
    # The record carries the id, never a label or a context (R4.11), and it
    # addresses the body rather than the sidecar.
    record = roundtrip_env.get_item(Key={"pipeline_id": json.loads(created["body"])["pipeline_id"]}).get("Item")
    assert record is not None, "create_pipeline answered 201 but wrote no record"
    assert record["target_schema_name"] == schema_id
    assert record["target_schema_s3_key"] == _body_key(schema_id)

    # ── The metamorphic no-op: absence is not an error ────────────────────
    offending = _error_logs_about_the_sidecar(error_logs)
    if not scenario.writes:
        assert not offending, (
            f"R2.6: with no Context_Sidecar, creation must treat its absence as 'no context "
            f"recorded' and must NOT log it as an error. `requirements.md` §Current state records "
            f"that tolerance as deliberate — `pipelines_crud.py:783-789` logs at `warning` and "
            f"creation continues — and this is the guard on it. ERROR records about the sidecar:\n"
            f"  {offending}"
        )
    else:
        assert not offending, (
            f"a sidecar that EXISTS and was just written must not produce an ERROR about the "
            f"metadata read either: {offending}"
        )


# ---------------------------------------------------------------------------
# The metamorphic pair, as an example — one schema, with and without a sidecar
# ---------------------------------------------------------------------------
def test_a_sidecar_changes_nothing_about_whether_a_pipeline_can_be_created(roundtrip_env):
    """The no-op stated as a *pair*, which the property cannot state per example.

    The property asserts ``201`` in both branches across different examples. This
    asserts it for **one** schema, twice, and compares the two records key for
    key — so "the sidecar's presence is irrelevant to creation" is observed
    directly rather than inferred from two independent runs. Everything except
    the per-creation identifiers must be identical.

    That is the metamorphic form of R2.6: adding a Context_Sidecar is a no-op on
    the pipeline record, and removing one is too.

    **Validates: Requirement 2.6**
    """
    schema_id = "uploaded_schema_1785844048926"
    _wipe_bucket()
    _seed_body(schema_id)

    with _recorded_error_logs(pipelines_handler) as without_logs:
        without = _create_pipeline(schema_id, name="Without sidecar")
    assert without["statusCode"] == 201, without
    assert _sidecar_object(schema_id) is None, "the first half must run with no sidecar at all"

    assert schemas_handler.update_field_context(schema_id, "email", _context_event("PII"))["statusCode"] == 200
    assert _sidecar_object(schema_id) == {"field_contexts": {"email": "PII"}}

    with _recorded_error_logs(pipelines_handler) as with_logs:
        with_sidecar = _create_pipeline(schema_id, name="With sidecar")
    assert with_sidecar["statusCode"] == 201, with_sidecar

    #: Per-creation values: a new id, a new name, new timestamps. Everything else
    #: is the schema's, so everything else must match.
    volatile = {"pipeline_id", "pipeline_name", "created_at", "updated_at"}

    def _comparable(response) -> dict[str, Any]:
        item = roundtrip_env.get_item(Key={"pipeline_id": json.loads(response["body"])["pipeline_id"]}).get("Item")
        assert item is not None
        return {key: value for key, value in item.items() if key not in volatile}

    assert _comparable(without) == _comparable(with_sidecar), (
        "R2.6: whether a Context_Sidecar exists must make no difference to the pipeline record. "
        "It does not carry the contexts either way — `target_schema_details` is built at "
        "`pipelines_crud.py:791-802` and discarded, which is the finding recorded in this module's "
        "docstring, not something this test asserts away."
    )
    assert not _error_logs_about_the_sidecar(without_logs), (
        f"the absent-sidecar creation logged an ERROR about it: {_error_logs_about_the_sidecar(without_logs)}"
    )
    assert not _error_logs_about_the_sidecar(with_logs), (
        f"the present-sidecar creation logged an ERROR about it: {_error_logs_about_the_sidecar(with_logs)}"
    )


class TestTheRecordersAreArmed:
    """Both spies catch what they exist to catch.

    The second half of Property 8 asserts an **absence** — no ``ERROR`` about the
    sidecar — and the third clause asserts a **presence** through recorded calls.
    Neither claim means anything if the recorder is silent, so each is shown to
    fire against a deliberate provocation before it is trusted to stay quiet.
    """

    def test_the_s3_spy_records_real_calls_and_lets_them_through(self, roundtrip_env):
        with _recorded_s3_calls() as calls:
            _client("s3").put_object(Bucket=_bucket(), Key="shared/spy_check.json", Body=b"{}")
            body = _client("s3").get_object(Bucket=_bucket(), Key="shared/spy_check.json")["Body"].read()

        assert body == b"{}", "the spy must call through, not stand in for moto"
        assert [operation for operation, _ in calls] == ["PutObject", "GetObject"], (
            f"observed: {[operation for operation, _ in calls]}"
        )

    def test_the_log_spy_reports_an_error_about_the_sidecar(self, roundtrip_env):
        """A ``logger.error`` naming the metadata read is caught, and the level restored."""
        with _recorded_error_logs(pipelines_handler) as records:
            pipelines_handler.logger.error("Failed to load schema metadata from S3: %s", "NoSuchKey")
            pipelines_handler.logger.error("something else entirely")

        assert _error_logs_about_the_sidecar(records) == ["Failed to load schema metadata from S3: NoSuchKey"], (
            f"the filter must catch the sidecar message and only it; got {records}"
        )
        assert "error" not in pipelines_handler.logger.__dict__, (
            "the spy must restore the logger to its original state, including restoring absence"
        )

    def test_a_warning_about_the_sidecar_is_not_an_error(self, roundtrip_env):
        """R2.6 forbids an ERROR, not a diagnostic. A ``warning`` passes.

        This is the assertion that keeps the second half from over-reaching: the
        tolerance ``requirements.md`` §Current state preserves logs at ``warning``
        and continues, and a property that failed on any mention of the sidecar
        would forbid that.
        """
        with _recorded_error_logs(pipelines_handler) as records:
            pipelines_handler.logger.warning("Failed to load schema metadata from S3: NoSuchKey")

        assert records == [], "a warning must not be recorded as an error-level report"
