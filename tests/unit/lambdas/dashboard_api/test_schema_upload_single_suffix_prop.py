# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
# Feature: p35-schema-surfaces, Property 7: A stored id carries exactly one generated suffix, and a label carries none

For any submitted schema name and any optional Display_Label, the id
``upload_schema`` returns is **byte-identical to the basename of the key it
wrote**, matches the single-suffix shape, and is unique; and the Display_Label is
stored **verbatim with no uniqueness transformation**, so two schemas may carry
the same label.

THE DEFECT THIS IS ABOUT
------------------------
The wizard sends ``uploaded_schema_${Date.now()}``
(``CreatePipelinePage.tsx:328``) and the pre-p35 handler unconditionally appended
its own ``%Y%m%d_%H%M%S`` (``schemas.py:322-323``), so every inline upload landed
as ``uploaded_schema_1785844048926_20260804_114729`` — two independent
timestamps, neither aware of the other. That id is a **live record**, which is why
this spec's row-4 amendment keeps it as the legacy fixture in
``test_create_pipeline_target_schema_location.py`` and *adds* a single-suffix
sibling rather than replacing it.

WHAT "EXACTLY ONE SUFFIX" IS ASSERTED TO MEAN, AND WHY THAT AND NOT SOMETHING ELSE
---------------------------------------------------------------------------------
R4.5: "WHILE a schema name is being composed for storage, THE Schema_Upload SHALL
append exactly one uniqueness suffix, so that a stored name carries no more than
one generated component."

The oracle here is **trailing**: strip one generated component off the end of the
id and the remainder must not end with another. It is deliberately not "the id
contains at most one run of digits". A user is entitled to a name like
``orders_1785844048926_eu`` — the digits in the middle are *theirs*, not
generated, and a rule that forbade them would be a rule about names rather than
about suffixes. The doubling R4.5 exists to stop happens at the end, because
that is where an ``append`` puts things.

The two generated shapes are enumerated **from the requirement text**, not from
the implementation: ``%Y%m%d_%H%M%S`` is the stamp R4.5 names as the one the
handler appended, and an epoch-milliseconds run is what ``Date.now()`` gives the
wizard in the same requirement's mechanism note. ``_ONE_GENERATED_SUFFIX`` is
this file's own regex; nothing is imported from ``handlers.schemas`` to score it,
so the property cannot be satisfied by the code agreeing with itself.

"FOR ANY SUBMITTED NAME" HAS TWO OUTCOMES, AND THE FIRST RUN IS WHY
------------------------------------------------------------------
The property as designed said "for any submitted schema name … the id
``upload_schema`` returns is byte-identical to the basename of the key it wrote".
Its first run falsified that with ``submitted_name=':'``: a name containing
nothing an id can be composed from is answered ``400``, so there is no id and no
key for the identity to hold between. That is the handler being right and the
property's domain being unstated.

Rather than narrow the generator — which would have discarded exactly the input
class most likely to write outside ``shared/`` — the property carries both
outcomes, and ``_ADDRESSABLE`` decides which one is **owed**: a name with at
least one ASCII alphanumeric must be stored; only a name with none may be
refused, and a refusal must write nothing. Without that guard a handler could
satisfy every clause by refusing everything.

UNIQUENESS IS ASSERTED ON THE BUCKET, NOT ON THE RESPONSE
---------------------------------------------------------
Every example uploads the **same submitted name two or three times**, which is
the case the pre-p35 handler got silently wrong: its suffix had
second-resolution, so two uploads inside one second composed the same key and the
second ``put_object`` **overwrote the first schema's body** with no error and no
second id. So the assertions are (a) the ids are distinct and (b) the bucket ends
up holding one ``shared/*.json`` object per upload. A handler that answered with
distinct ids while writing one object would pass (a) alone.

R4.6's identity is asserted the same way — against the **key that is actually in
the bucket** rather than against the ``location`` string the handler also
composed, because two values the handler derives separately can agree with each
other and still both be wrong. ``LGR-cad8032-1`` (**template / ratified**) is the
record: a second definition of one address meant the reader found the object
while the writer recorded nothing, and the feature silently did not run.

BOTH BODY SHAPES, BECAUSE R8.5 REQUIRES BOTH
--------------------------------------------
Each example runs against either the JSON body (``{name, content,
displayLabel}``) or ``multipart/form-data`` (a ``file`` part plus an optional
``displayLabel`` part). They are different parsers reaching one writer, and the
label was the half that did not exist on either before this task.

Generated names exclude ``"``, ``;``, CR and LF — not because the handler cannot
take them, but because a multipart ``Content-Disposition`` header cannot express
them, so generating them would compare two shapes that were handed different
input. The JSON path's tolerance for them is pinned by example in
``test_schema_upload_name_and_label.py``.

FIXTURES AND REGION
-------------------
Shared fixtures only — ``mock_s3_buckets`` from ``tests/fixtures/conftest.py``.
No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and ``AWS_REGION`` is
**read, never set** (``LGR-3e45e65-1``), through the same
``os.environ.get("AWS_REGION", "us-west-2")`` expression ``schemas.py`` uses — so
the seeding client and the handler cannot land in different moto backends.

**Validates: Requirements 4.5, 4.6, 4.7, 4.12, 8.5**
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, NamedTuple

import boto3
import pytest
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

from handlers.schemas import upload_schema

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"
ACCOUNT_ID = "123456789012"

SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_CONTENT = json.dumps(SCHEMA_BODY)


# ---------------------------------------------------------------------------
# The oracles, written from the requirements rather than imported
# ---------------------------------------------------------------------------
#: One **generated** component at the end of an id, in either shape this system
#: produces: R4.5's ``%Y%m%d_%H%M%S`` stamp, or an epoch-milliseconds run of the
#: kind ``Date.now()`` gives the wizard. The floor of 9 digits is what keeps a
#: human name ending in a short number (``orders_2024``) from being read as a
#: machine stamp — see the module docstring.
_ONE_GENERATED_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<suffix>\d{8}_\d{6}|\d{9,})$")


def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention.

    Spelled here rather than imported so the property's oracle is the requirement
    and not the code under test.
    """
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{schema_id}-metadata.json`` — R4.12 verbatim."""
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _basename(key: str) -> str:
    """The id an object key claims, derived the way the listing derives it.

    ``_collect_schema_rows`` strips exactly ``shared/`` and ``.json``
    (``schemas.py:568``). Using the same derivation here is what makes R4.6's
    "byte-identical" a statement about a round trip rather than about string
    slicing: the id the writer returned has to be the id the reader will produce
    from the object it wrote.
    """
    assert key.startswith("shared/") and key.endswith(".json"), (
        f"R4.6/LGR-cad8032-1: an uploaded schema must be written to shared/{{id}}.json — got {key!r}"
    )
    return key[len("shared/") : -len(".json")]


#: Whether a submitted name contains anything a schema id can be composed from.
#:
#: **This came out of the property rather than into it.** The first run's
#: counter-example was ``submitted_name=':'``, which the handler answers ``400``:
#: nothing in ``':'`` can appear in an id, so there is no name to store and R4.6's
#: identity has nothing to hold between. The property's original wording — "for
#: any submitted schema name, the id it returns…" — silently assumed every name
#: produces an id. It does not, and narrowing the generator to hide that would
#: have thrown away the input class most likely to write outside ``shared/``.
#:
#: So the property carries **both** outcomes and this predicate is the oracle
#: that decides which one is owed: a name with at least one ASCII alphanumeric
#: must be stored, and only a name with none may be refused. Without it a handler
#: could satisfy the property by refusing everything.
_ADDRESSABLE = re.compile(r"[A-Za-z0-9]")


def _generated_suffix_depth(schema_id: str) -> int:
    """How many generated components the id carries at its **end**.

    ``0`` for ``customers``, ``1`` for ``uploaded_schema_1785844048926``, ``2``
    for the live pre-fix ``uploaded_schema_1785844048926_20260804_114729``. R4.5
    permits no more than one.
    """
    depth = 0
    remaining = schema_id
    while (match := _ONE_GENERATED_SUFFIX.match(remaining)) is not None:
        depth += 1
        remaining = match.group("base")
    return depth


def _client(service: str):
    """A client in exactly the region the handler will use (``LGR-3e45e65-1``)."""
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


# ---------------------------------------------------------------------------
# The two request shapes R8.5 requires
# ---------------------------------------------------------------------------
_MULTIPART_BOUNDARY = "----p35SchemaUploadBoundary"


def _json_event(name: str, label: str | None) -> dict[str, Any]:
    """``POST /api/schemas/upload`` with ``{name, content, displayLabel?}``."""
    payload: dict[str, Any] = {"name": name, "content": SCHEMA_CONTENT}
    if label is not None:
        payload["displayLabel"] = label
    return {
        "httpMethod": "POST",
        "path": "/api/schemas/upload",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _multipart_event(name: str, label: str | None) -> dict[str, Any]:
    """The same upload as ``multipart/form-data``: a ``file`` part, plus a label part.

    The filename carries the submitted name with a ``.json`` extension, because
    that is the only place this shape can express one — ``_parse_multipart_body``
    derives the schema name from the filename minus its extension.
    """
    parts = [
        f"--{_MULTIPART_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}.json"\r\n'
        f"Content-Type: application/json\r\n"
        f"\r\n"
        f"{SCHEMA_CONTENT}\r\n"
    ]
    if label is not None:
        parts.append(
            f'--{_MULTIPART_BOUNDARY}\r\nContent-Disposition: form-data; name="displayLabel"\r\n\r\n{label}\r\n'
        )
    body = "".join(parts) + f"--{_MULTIPART_BOUNDARY}--\r\n"
    return {
        "httpMethod": "POST",
        "path": "/api/schemas/upload",
        "headers": {"Content-Type": f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"},
        "body": body,
    }


_EVENT_BUILDERS = {"json": _json_event, "multipart": _multipart_event}


# ---------------------------------------------------------------------------
# Reading the world
# ---------------------------------------------------------------------------
def _wipe_bucket() -> None:
    """Empty the target-schemas bucket so each example starts from a clean slate.

    Hypothesis reuses one fixture across every example, so without this the
    uniqueness clause would be measuring the accumulated history of the run
    rather than the example.
    """
    s3 = _client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=TARGET_SCHEMAS_BUCKET, Delete={"Objects": keys})


def _keys(prefix: str) -> list[str]:
    s3 = _client("s3")
    found: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=TARGET_SCHEMAS_BUCKET, Prefix=prefix):
        found.extend(obj["Key"] for obj in page.get("Contents", []))
    return sorted(found)


def _json_object(key: str) -> Any:
    return json.loads(_client("s3").get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=key)["Body"].read())


def _response(raw: dict[str, Any]) -> dict[str, Any]:
    return json.loads(raw["body"])


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
class Scenario(NamedTuple):
    """One submitted name, one optional label, uploaded ``repeats`` times."""

    submitted_name: str
    label: str | None
    repeats: int
    shape: str


#: Names weighted toward the ones that would break something.
#:
#: The first three are R4.5's own subject matter: a name with no suffix, the
#: wizard's single-suffix shape, and the live double-suffix shape the pre-p35
#: handler produced. The path-shaped and ``.json``-bearing entries are R4.6's:
#: an id that is *not* the basename of its key is the state ``LGR-cad8032-1``
#: exists to prevent, and a name containing ``/`` or the substring ``.json`` is
#: how a writer gets there.
_HOSTILE_NAMES = (
    "customers",
    "uploaded_schema_1785844048926",
    "uploaded_schema_1785844048926_20260804_114729",
    "schema_1785844048926_20260804_114729",
    "orders_2024",
    "shared/orders.json",
    "../../etc/passwd",
    "my.schema.json",
    "Kundendaten Ünïcodé",
    "p32ver_ren_renC",
    "1785844048926",
    "  padded name  ",
    "a" * 300,
    # Nothing a schema id can be composed from. Promoted into the pool after the
    # first run's counter-example (`':'`) — see `_ADDRESSABLE`.
    ":",
    "***",
    "///",
)

#: Labels. ``None`` is the key **omitted** — R4.2's "usable with no Display_Label
#: at all" — and ``""`` is present-but-empty, which task 8.5 settled as the
#: absence of a label rather than a stored ``""``.
_HOSTILE_LABELS = (
    None,
    "",
    "Customer master",
    "  padded label  ",
    "uploaded_schema_1785844048926",
    "shared/evil.json",
    "Kundendaten Ünïcodé",
)


@st.composite
def upload_scenarios(draw) -> Scenario:
    """A submitted name, an optional label, and **two or three uploads of it**.

    ``repeats`` starts at 2 deliberately: one upload cannot distinguish a handler
    that composes a unique id from one that composes the same id twice and
    overwrites, and overwriting is exactly what the second-resolution stamp did.
    The single-upload case is covered by its own example test in
    ``test_schema_upload_name_and_label.py`` rather than by diluting this
    generator — Hypothesis biases toward the minimum, so a ``min_value=1`` would
    have made the majority of examples the case that cannot see the defect.

    Free-form names exclude ``"``, ``;``, CR and LF so both body shapes can carry
    the same input (see the module docstring), and exclude surrogates because a
    lone surrogate cannot be encoded into a request body at all — generating one
    would test Python's codec rather than the upload.
    """
    free_form_name = st.text(
        alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc"), exclude_characters='";'),
        min_size=1,
        max_size=40,
    )
    return Scenario(
        submitted_name=draw(st.one_of(st.sampled_from(_HOSTILE_NAMES), free_form_name)),
        label=draw(st.sampled_from(_HOSTILE_LABELS)),
        repeats=draw(st.integers(min_value=2, max_value=3)),
        shape=draw(st.sampled_from(sorted(_EVENT_BUILDERS))),
    )


#: The cases this property exists for, as **explicit** inputs rather than as
#: draws that might happen. Task 8.1 measured the hazard: over 200 examples a
#: ``one_of(sampled_from(...), text(...))`` reached one target case 0 times in one
#: run and ~25 in the next, purely on the Hypothesis seed. A coverage claim that
#: depends on the seed is not a claim.
_CRITICAL_SCENARIOS = (
    # The live pre-fix double-suffix shape, on both body shapes. This is the
    # input R4.5 is written about.
    Scenario("uploaded_schema_1785844048926_20260804_114729", "Customer master", 2, "json"),
    Scenario("uploaded_schema_1785844048926_20260804_114729", None, 2, "multipart"),
    # The wizard's single-suffix shape: one generated component already, so the
    # handler must not add a second.
    Scenario("uploaded_schema_1785844048926", None, 3, "json"),
    Scenario("uploaded_schema_1785844048926", "Customer master", 2, "multipart"),
    # No suffix at all — the Schemas_Page path, where the name is a human
    # filename and the uniqueness component has to come from the handler.
    Scenario("customers", "  padded label  ", 2, "json"),
    Scenario("customers", "  padded label  ", 2, "multipart"),
    # R4.6's trap: a name that would escape `shared/` or break the round trip.
    Scenario("shared/orders.json", "Customer master", 2, "json"),
    Scenario("../../etc/passwd", None, 2, "multipart"),
    Scenario("my.schema.json", None, 2, "json"),
    # A label that IS an id — nothing type-distinguishes the two (R4.11), and
    # this is the input under which a wrong implementation looks right.
    Scenario("customers", "uploaded_schema_1785844048926", 2, "json"),
    # An empty label present in the body: the absence of a label, not a stored "".
    Scenario("customers", "", 2, "json"),
    Scenario("customers", "", 2, "multipart"),
    # A human name ending in a short number, which must NOT be read as a stamp.
    Scenario("orders_2024", None, 2, "json"),
    # Over the length cap, and a name that is nothing but a stamp.
    Scenario("a" * 300, None, 2, "json"),
    Scenario("1785844048926", None, 2, "json"),
    # Nothing addressable at all — the refusal branch, which the first run of
    # this property discovered rather than assumed (see `_ADDRESSABLE`).
    Scenario(":", None, 2, "json"),
    Scenario("***", "Customer master", 2, "multipart"),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_SCENARIOS`` as explicit examples, in order."""
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


#: Every case ``_report_generated_cases`` observed, accumulated across the survey
#: below. Module-level because Hypothesis's own statistics are per-example and
#: cannot carry a cross-example claim.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached.

    Tasks 3.1 and 4.3 both found generators that reached their target case **0
    times out of 200 and passed anyway**. These events are what make that visible
    here rather than discovered later.
    """

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    event(f"body shape: {scenario.shape}")
    event(f"{scenario.repeats} uploads of one submitted name")
    depth = _generated_suffix_depth(scenario.submitted_name)
    event(f"submitted name carries {min(depth, 2)} generated suffix(es)")
    if scenario.label is None:
        event("no Display_Label key at all (R4.2)")
    elif not scenario.label.strip():
        event("an empty Display_Label (the absence of one)")
    else:
        event("a Display_Label was supplied")
        if scenario.repeats >= 2:
            event("two or more schemas carrying the SAME Display_Label (R4.7)")
        if scenario.label != scenario.label.strip():
            event("a Display_Label with leading or trailing whitespace (R4.7 verbatim)")
    if "/" in scenario.submitted_name:
        event("a submitted name containing '/' (R4.6 — it must not escape shared/)")
    if ".json" in scenario.submitted_name:
        event("a submitted name containing '.json' (R4.6 — the listing strips it)")
    if len(scenario.submitted_name) > 200:
        event("a submitted name over the length cap")
    if _ADDRESSABLE.search(scenario.submitted_name):
        event("a submitted name that must be stored")
    else:
        event("a submitted name with nothing addressable in it (must be refused)")


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def upload_env(mock_s3_buckets, monkeypatch):
    """The shared moto bucket, plus the two addresses the handler resolves.

    Nothing is stubbed that is not an address: ``TARGET_SCHEMAS_BUCKET`` names the
    fixture's bucket and ``AWS_ACCOUNT_ID`` keeps ``get_aws_account_id`` off STS.
    Every S3 interaction under test is the real API against the shared fixture.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    return TARGET_SCHEMAS_BUCKET


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no AWS, no handler.
@_with_critical_examples
@given(scenario=upload_scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(scenario):
    """Every scenario is a repeated upload of one name, on one of the two shapes.

    **Validates: Requirement 8.5**
    """
    _report_generated_cases(scenario)

    assert scenario.repeats >= 2, (
        f"a single upload cannot distinguish a unique id from an overwrite; drew {scenario.repeats}"
    )
    assert scenario.shape in _EVENT_BUILDERS, f"unknown body shape {scenario.shape!r}"
    assert scenario.submitted_name, "an empty submitted name is a 400, not a naming question"


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once.

    Every required case is guaranteed by ``_CRITICAL_SCENARIOS`` rather than left
    to the draw, which is what makes this assertion deterministic rather than
    seed-dependent.

    **Validates: Requirement 8.5**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        "body shape: json",
        "body shape: multipart",
        "submitted name carries 0 generated suffix(es)",
        "submitted name carries 1 generated suffix(es)",
        "submitted name carries 2 generated suffix(es)",
        "no Display_Label key at all (R4.2)",
        "an empty Display_Label (the absence of one)",
        "a Display_Label was supplied",
        "two or more schemas carrying the SAME Display_Label (R4.7)",
        "a Display_Label with leading or trailing whitespace (R4.7 verbatim)",
        "a submitted name containing '/' (R4.6 — it must not escape shared/)",
        "a submitted name containing '.json' (R4.6 — the listing strips it)",
        "a submitted name over the length cap",
        "a submitted name that must be stored",
        "a submitted name with nothing addressable in it (must be refused)",
        "2 uploads of one submitted name",
        "3 uploads of one submitted name",
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=upload_scenarios())
@settings(
    max_examples=100,
    # Each example wipes a moto bucket and performs two or three real uploads,
    # which routinely exceeds Hypothesis's 200ms default. The property asserts
    # identity and uniqueness, not latency.
    deadline=None,
    # One fixture serves every example by design; `_wipe_bucket` clears the
    # bucket at the start of each one, so nothing an earlier example wrote is
    # visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` is excluded: every assertion message below names the
    # offending value verbatim, so the explain phase would re-run several hundred
    # further examples to add nothing. Shrinking is kept.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_stored_id_carries_exactly_one_generated_suffix_and_a_label_carries_none(scenario, upload_env):
    """Property 7: one suffix on the id, none on the label, and the id IS the basename.

    **Validates: Requirements 4.5, 4.6, 4.7, 4.12, 8.5**
    """
    _report_generated_cases(scenario)
    _wipe_bucket()

    build_event = _EVENT_BUILDERS[scenario.shape]
    ids: list[str] = []

    for attempt in range(scenario.repeats):
        raw = upload_schema(build_event(scenario.submitted_name, scenario.label))
        body = _response(raw)

        # -- Clause 0: a name that cannot be stored is refused, and writes nothing
        # The outcome the first run's counter-example (`':'`) surfaced. A refusal
        # is only owed for a name with nothing addressable in it — otherwise a
        # handler could satisfy every clause below by never storing anything.
        if raw["statusCode"] != 200:
            assert not _ADDRESSABLE.search(scenario.submitted_name), (
                f"R4.1/R4.2: {scenario.submitted_name!r} contains characters a schema id can be "
                f"composed from, so it must be stored rather than refused — got {raw}"
            )
            assert raw["statusCode"] == 400, f"an unstorable name is a 400 naming why, not {raw['statusCode']}"
            assert body.get("error"), "the 400 must carry a message the frontend can render"
            assert _keys("shared/") == [] and _keys("schemas/uploaded/") == [], (
                f"a refused upload must write nothing — found {_keys('')}"
            )
            return

        schema_id = body["schemaId"]

        # -- Clause 1: the id is the basename of the key that was written -------
        # Read from the bucket, not from the response's `location`: two values the
        # handler derives separately can agree and both be wrong, and one
        # definition of this address is `LGR-cad8032-1`'s whole subject.
        written = [key for key in _keys("shared/") if _basename(key) == schema_id]
        assert written == [_body_key(schema_id)], (
            f"R4.6: the id {schema_id!r} must be byte-identical to the basename of the object it "
            f"names. Objects under shared/ now: {_keys('shared/')}"
        )
        assert body["location"] == f"s3://{TARGET_SCHEMAS_BUCKET}/{_body_key(schema_id)}", (
            f"the reported location must name the object that was written — got {body['location']!r}"
        )
        assert _json_object(_body_key(schema_id)) == SCHEMA_BODY, (
            "the stored body must be the submitted schema and nothing else"
        )

        # -- Clause 2: exactly one generated suffix ----------------------------
        depth = _generated_suffix_depth(schema_id)
        assert depth == 1, (
            f"R4.5: a stored id carries exactly one generated suffix; {schema_id!r} carries {depth} "
            f"(submitted name was {scenario.submitted_name!r})"
        )

        # -- Clause 3: the id is unique, and nothing was overwritten -----------
        assert schema_id not in ids, (
            f"R4.7: upload {attempt + 1} of {scenario.submitted_name!r} reused the id {schema_id!r} "
            f"— a second upload in the same second used to overwrite the first schema's body"
        )
        ids.append(schema_id)
        assert len(_keys("shared/")) == len(ids), (
            f"R4.7: {len(ids)} uploads must leave {len(ids)} objects under shared/; found {_keys('shared/')}"
        )

        # -- Clause 4: the label is stored verbatim, or not at all -------------
        if scenario.label is not None and scenario.label.strip():
            assert body.get("displayLabel") == scenario.label, (
                f"R4.7: the response must echo the label verbatim — got {body.get('displayLabel')!r} "
                f"for {scenario.label!r}"
            )
            sidecar = _json_object(_sidecar_key(schema_id))
            assert sidecar.get("display_name") == scenario.label, (
                f"R4.12/R4.7: display_name must be the submitted label verbatim, with no uniqueness "
                f"transformation and no trimming — got {sidecar!r} for {scenario.label!r}"
            )
        else:
            assert "displayLabel" not in body, f"R4.2: an upload with no Display_Label must claim none — got {body!r}"
            assert _sidecar_key(schema_id) not in _keys("schemas/uploaded/"), (
                f"R4.2: an unlabelled upload must leave no sidecar; found {_keys('schemas/uploaded/')}"
            )

    # -- Clause 4, the part one upload cannot show -------------------------
    # R4.7: uniqueness is a property of the id because the id is an address; a
    # label has no addressing role, so nothing may be appended to it to make it
    # unique. Every upload in this example carried the SAME label, so this is the
    # assertion that two schemas may share one.
    if scenario.label is not None and scenario.label.strip():
        stored = {schema_id: _json_object(_sidecar_key(schema_id))["display_name"] for schema_id in ids}
        assert set(stored.values()) == {scenario.label}, (
            f"R4.7: {len(ids)} schemas submitted with one label must all carry it verbatim — got {stored!r}"
        )
        assert len(set(stored)) == len(ids), "each label must sit on its own schema's sidecar"
