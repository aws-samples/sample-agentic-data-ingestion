# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — an incomplete reference check never deletes.

# Feature: p35-schema-surfaces, Property 5: An incomplete reference check never deletes

For any point at which the paginated scan is interrupted — throttled, or the
wall-clock budget exhausted — ``delete_schema`` responds **503** with
``reason: "reference_check_incomplete"``, performs **no** ``delete_object``, and
reports **only the lookups that completed**.

Model-based: the model is **"deleted ⟹ every lookup in ``checked`` completed"**.
The harness knows, by construction, which lookups it interrupted, so the model is
checked against ground truth rather than against the handler's own account of
itself: a ``200`` whose ``checked`` omits a lookup that did not finish is the
violation.

WHY THIS IS A SEPARATE PROPERTY FROM PROPERTY 4
-----------------------------------------------
Task 8.2 owns the two predicates on a check that **can** finish, and asserts
``status != 503`` there with a pointer here. This file owns only the interrupted
check, and it is the property that makes DD2's ``200`` an **unqualified** claim.
Design §4.2's fourth row is the one carrying DD2's weight: a ``200`` on an
unfinished check would be a silent claim the code cannot support — R1.4's failure
class, and ``LGR-78ce0b7-14``'s (**template / ratified**) as well — while a
``409`` would be worse, asserting references that were never found. ``503`` says
"ask again", is honestly retryable when the cause is throttling, and satisfies
R3.10 by construction because no delete is issued on any path where the check did
not finish.

Keeping the two apart is also what stops a single property from being satisfiable
by answering ``503`` to everything: Property 4 forbids ``503`` on every input it
generates, and this one requires it on inputs Property 4 never produces.

THE INTERRUPTION MECHANISM, AND WHY THIS ONE
--------------------------------------------
``delete_schema`` is a ``501`` stub, so there is nothing to interrupt yet and task
8.4 has not chosen how its budget is measured. The interruption therefore has to
be produced from **outside** the handler, in a way that does not decide 8.4's
implementation for it.

**Primary: throttling, produced by generalising task 8.1's pass-through spy from
S3 to DynamoDB.** The spy wraps ``botocore.client.BaseClient._make_api_call``, so
it sees ``Query`` and ``Scan`` whether task 8.4 reaches the table through the
``Table`` resource, the low-level client or a paginator, and it can fail the
**Nth** call of either. Throttling was chosen as the primary mechanism because it
is the one interruption that is **independent of every choice 8.4 has left to
make**: it needs no clock, no budget constant and no particular pagination
idiom. ``ProvisionedThroughputExceededException``, ``ThrottlingException`` and
``RequestLimitExceeded`` are all real DynamoDB throttles, and raising from inside
the ``_make_api_call`` wrapper is *above* botocore's own retry handler, so the
failure is deterministic rather than absorbed by a retry.

**Secondary: the wall-clock budget, produced by an advancing clock rather than by
a real wait.** For these examples the spy passes every ``Scan`` through and then
moves ``time.monotonic`` / ``time.time`` forward by 30 or 300 seconds per page, so
a handler that measures a budget against either gives up at a page boundary. This
one **does** touch a choice 8.4 owns, and the limits are stated rather than
hidden: the harness drives ``time.monotonic``, ``time.monotonic_ns``, ``time.time``
and ``time.time_ns``, and does **not** drive ``time.perf_counter`` (Hypothesis
uses it) or ``datetime.datetime.now()`` (a C-level clock this cannot reach). If
task 8.4 measures its budget with a clock this harness does not drive, the
budget-mode examples fail on a correct implementation — **the fix is then to widen
``_InterruptedLookups``'s clock, not to loosen the property.** The failure message
on that clause says so.

**Pagination is genuine, not injected.** ``LastEvaluatedKey`` is not fabricated —
the fixture seeds ~2.5 MB of padding records and moto applies DynamoDB's real 1 MB
page limit *before* the ``FilterExpression``
(``moto/dynamodb/models/table.py:1103-1149``), exactly as the service does. So a
full scan of the seeded table really does take three pages, and "interrupted at
the second page" is a real mid-pagination interruption rather than a mocked one.
``TestTheHarnessIsArmed`` measures the page count rather than assuming it.

WHAT IS PINNED, AND WHAT IS DELIBERATELY LEFT FREE FOR TASK 8.4
---------------------------------------------------------------
**Pinned** — because the requirements, design §4.2's table or task 8.3's own text
name them:

* no ``DeleteObject`` / ``DeleteObjects``, and the object is still there afterwards
  (R3.10);
* no success status while any lookup was interrupted (R1.4's could-not-check
  distinction, R5.3's the same);
* ``503`` when the interrupted check would otherwise have concluded "nothing
  references this" — design §4.2's fourth row;
* ``reason == "reference_check_incomplete"`` on that ``503`` — stated in task 8.3's
  text, so it is pinned;
* a ``checked`` field, reporting **only** completed lookups — design §4.2 gives
  every outcome a ``checked``, and task 8.4's text says the second return element
  "is not diagnostics — it is what distinguishes 'checked and found nothing' from
  'could not check'".

**Left free:**

* **Where in the body ``reason`` and ``checked`` live.** Both are looked up
  anywhere in the parsed body, at any nesting depth.
* **How the entries of ``checked`` are spelled.** They are matched by substring
  (``index`` / ``query`` for the Query, ``scan`` / ``target_schema_s3_key`` for the
  Scan) rather than compared to design §4.2's literal
  ``"target_schema_name-index"``, and ``checked`` may be a list, a mapping or a
  single string.
* **``retryable: true``.** Design §4.2 and task 8.4 both name it, but task 8.3
  does not, so it is asserted **once**, in its own example test, rather than as a
  clause of the property.
* **The status when the *Query* rather than the Scan is interrupted.** Task 8.3's
  text is about "the paginated scan", so this file requires only that a
  Query-interrupted request makes no claim — not success, not ``409`` — and leaves
  the choice between ``503`` and a ``500`` to task 8.4. When a ``checked`` is
  present it must still be empty, because no lookup completed.
* **Whether the scan stops early on its first hit.** A referencing record can be
  found on page 1, and one hit is already enough to refuse (design §4.2's second
  row), so on a *referenced* population an interruption that never fires and a
  ``409`` are both accepted. The interruption is *required* to fire only on an
  unreferenced population, where nothing can justify stopping early — and that is
  also the clause a Query-only implementation fails.
* **Everything about a ``409``'s body**, including its ``checked``. Design §4.2's
  second row legitimately reports the scan as run when the scan produced a hit,
  and in budget mode the harness cannot tell "stopped at the hit" from "ran out of
  clock". A ``409`` claims references *were* found, which is not the claim this
  property is about; task 8.2 owns the ``409``'s contents.
* **The budget's own mechanism and duration.** Nothing here asserts a number of
  pages, a deadline constant, or where the budget is read from.

WHAT THIS PROPERTY DOES **NOT** COVER
-------------------------------------
R3.3's ``400`` for a ``glue_catalog`` / ``s3_table_bucket`` id, R3.5's
already-absent success, and the whole of Property 4 (a check that completes). Every
id generated here is an Uploaded_Schema id whose object is present, so R3.5's
branch cannot short-circuit the reference check and take the ``503`` clause with it.

FIXTURES, REGION, AND THE HANDLE TASK 8.4 MUST USE
--------------------------------------------------
Shared fixtures only — ``mock_s3_buckets`` and ``mock_dynamodb_tables`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and this file does **not** set
``AWS_REGION`` (``LGR-3e45e65-1``): the unit layer pins it to ``us-west-2`` in
``tests/unit/conftest.py:31-33``, which is what both readers then resolve —
``handlers/common.py:32`` freezes it at import time and ``handlers/schemas.py:442``
re-reads it per call, and ``LGR-3e45e65-1`` exists because those two disagreeing
is a full-suite-only failure.

``mock_dynamodb_tables`` creates its tables in ``us-east-1``
(``tests/fixtures/conftest.py:213``) while the handler resolves ``us-west-2``, and
moto's DynamoDB backend is region-scoped, so the fixture below binds the fixture's
table onto **both** ``handlers.schemas`` (``raising=False``, the attribute does not
exist yet) and ``handlers.common`` — either access shape is reasonable for task
8.4, and patching only one would silently test nothing under the other. **Task 8.4
must reach the pipelines table through a module-level handle**, as
``pipelines_crud`` does.

A bounded scan on this table is the fix ``LGR-dd80c6d-1`` (**backfilled**) already
applied to the stats and approvals handlers; ``LGR-acca05d-4`` (**template /
ratified**) is what requires the response to state which lookups ran, which is
what ``checked`` discharges.

**Validates: Requirements 3.10, 1.4, 5.3**
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from typing import Any, NamedTuple

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.client import BaseClient
from botocore.exceptions import ClientError
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

#: The two lookups design §4.2 prescribes, as the harness's own labels. These are
#: ground truth about what the *harness* did, never a claim about how task 8.4
#: spells its ``checked`` entries — see ``_checked_lookups``.
LOOKUP_INDEX = "query on target_schema_name-index"
LOOKUP_SCAN = "scan filtered on target_schema_s3_key"

#: Upper bound on the generated population, and the number of deterministic
#: pipeline ids the harness reclaims before each example. Reclaiming a fixed set
#: of keyed deletes rather than accumulating ids keeps the per-example teardown
#: off the very operation the subject is being measured on.
_MAX_RECORDS = 5

#: Padding that makes a full scan of this table genuinely paginate. moto applies
#: DynamoDB's 1 MB page limit before the ``FilterExpression``
#: (``moto/dynamodb/models/table.py:1103-1149``), so ten ~250 KB records produce
#: three pages whose second and third are mostly empty after filtering — which is
#: precisely the real service's behaviour, and the state a budget has to survive.
#: ``TestTheHarnessIsArmed`` measures the page count rather than trusting this.
_PADDING_RECORDS = 10
_PADDING_BYTES = 250_000
_PADDING_NAME = "p35-inc-padding-is-not-a-schema-id"

#: Real DynamoDB throttles. Raised from inside the ``_make_api_call`` wrapper,
#: which sits above botocore's retry handler, so the failure is deterministic.
_THROTTLE_CODES = (
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
)

#: Every spelling of "remove an object" — ``DeleteObjects`` is the batch form, and
#: listing only ``DeleteObject`` would let it through under another name.
_DELETING_OPERATIONS = ("DeleteObject", "DeleteObjects")


# ---------------------------------------------------------------------------
# The two addresses, written out from the requirements rather than imported
# ---------------------------------------------------------------------------
def _body_key(schema_id: str) -> str:
    """``shared/{id}.json`` — R4.6 and ``LGR-cad8032-1``'s one convention.

    Spelled out rather than imported so the oracle is the requirement and not the
    code under test. This is "the schema's object" R3.8's predicate is defined on.
    """
    return f"shared/{schema_id}.json"


def _sidecar_key(schema_id: str) -> str:
    """``schemas/uploaded/{schema_id}-metadata.json`` — R4.12's Context_Sidecar.

    Here only as a near miss: it carries the id and is not the schema's object.
    """
    return f"schemas/uploaded/{schema_id}-metadata.json"


def _client(service: str):
    """A client in exactly the region the handler will use (``LGR-3e45e65-1``).

    ``os.environ.get("AWS_REGION", "us-west-2")`` is ``schemas.py:442`` verbatim —
    the call-time read — and ``handlers/common.py:32`` is the import-time one.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


# ---------------------------------------------------------------------------
# The interruption plan
# ---------------------------------------------------------------------------
class Interruption(NamedTuple):
    """How, and where, this example interrupts the reference check.

    ``target`` is the DynamoDB operation to interrupt — ``"Query"`` for the index
    lookup, ``"Scan"`` for the paginated one. ``calls_allowed`` is how many calls
    of that operation pass through before the interruption begins, so ``0`` is
    "interrupted before it ever returned a page" and ``2`` is "interrupted on the
    third page", genuinely partway through.
    """

    mode: str  #: "throttle" | "budget"
    target: str  #: "Query" | "Scan"
    calls_allowed: int
    #: Only meaningful for ``mode == "throttle"``.
    error_code: str = _THROTTLE_CODES[0]
    #: Only meaningful for ``mode == "budget"`` — simulated seconds per page.
    seconds_per_page: float = 30.0

    @property
    def label(self) -> str:
        if self.mode == "budget":
            return f"budget exhausted mid-scan (+{self.seconds_per_page:g}s per page)"
        where = "before its first page" if self.calls_allowed == 0 else f"on page {self.calls_allowed + 1}"
        return f"{self.target} throttled {where} with {self.error_code}"

    @property
    def lookup(self) -> str:
        return LOOKUP_INDEX if self.target == "Query" else LOOKUP_SCAN


class _Recorder:
    """What the harness observed, and what it interrupted.

    The ``interrupted`` set is ground truth: it is what *this* object did to the
    handler, not what the handler said about itself. The model
    "deleted ⟹ every lookup in ``checked`` completed" is checked against it.
    """

    def __init__(self, plan: Interruption) -> None:
        self.plan = plan
        self.s3_calls: list[tuple[str, dict[str, Any]]] = []
        self.operations: list[str] = []
        self.query_calls = 0
        self.scan_calls = 0
        self.raised = 0
        self.simulated_seconds = 0.0
        #: True once a ``Scan`` returned a page with no ``LastEvaluatedKey`` — the
        #: only observable evidence that the paginated lookup ran to the end.
        #: ``"LastEvaluatedKey" in response`` is form-independent, so this reads
        #: the same whether task 8.4 uses the ``Table`` resource or the client.
        self.scan_ran_to_completion = False
        self.query_ran_to_completion = False

    @property
    def interrupted(self) -> frozenset[str]:
        """The lookups this example prevented from completing.

        Empty when the handler never reached the interruption point — which
        happens on the ``501`` stub (it makes no call at all) and legitimately on a
        referenced population whose hit is on page 1.
        """
        if self.plan.mode == "throttle":
            return frozenset({self.plan.lookup}) if self.raised else frozenset()
        # Budget mode interrupts nothing by force: the handler abandons the scan
        # itself, or it does not, and the difference is whether the scan was ever
        # seen to run out of pages.
        if self.scan_calls and not self.scan_ran_to_completion:
            return frozenset({LOOKUP_SCAN})
        return frozenset()


@contextlib.contextmanager
def _interrupted_lookups(plan: Interruption):
    """Record every S3 and pipelines-table call, and interrupt the planned one.

    A spy, not a mock. Every call that is not interrupted is passed through to
    moto and its real response returned, so absence-of-call assertions are
    possible *and* every observed call is a real call — task 8.1's technique,
    generalised from S3 to DynamoDB.

    ``BaseClient._make_api_call`` sits above moto's ``before-send`` stubber and
    above botocore's retry handler, which is what makes this work for clients the
    handler constructs itself and makes a raised throttle deterministic rather
    than absorbed by a retry.

    In ``budget`` mode nothing is raised: the clock is moved forward instead, so a
    handler measuring a wall-clock budget runs out of it. The clocks driven are
    ``time.monotonic``, ``time.monotonic_ns``, ``time.time`` and ``time.time_ns``;
    ``time.perf_counter`` is deliberately left alone because Hypothesis measures
    with it, and ``datetime.datetime.now()`` cannot be reached from here.
    """
    recorder = _Recorder(plan)
    original_call = BaseClient._make_api_call
    real_monotonic, real_monotonic_ns = time.monotonic, time.monotonic_ns
    real_time, real_time_ns = time.time, time.time_ns

    def _throttle(operation_name: str) -> ClientError:
        return ClientError(
            {
                "Error": {
                    "Code": plan.error_code,
                    "Message": (
                        f"p35 task 8.3 harness: {operation_name} on {PIPELINES_TABLE} "
                        f"interrupted after {plan.calls_allowed} call(s)"
                    ),
                },
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            operation_name,
        )

    def recording(self, operation_name, api_params):
        service = self.meta.service_model.service_name
        if service == "s3":
            recorder.s3_calls.append((operation_name, dict(api_params)))
            return original_call(self, operation_name, api_params)

        is_lookup = (
            service == "dynamodb"
            and operation_name in ("Query", "Scan")
            and api_params.get("TableName") == PIPELINES_TABLE
        )
        if not is_lookup:
            return original_call(self, operation_name, api_params)

        recorder.operations.append(operation_name)
        if operation_name == "Query":
            recorder.query_calls += 1
            seen = recorder.query_calls
        else:
            recorder.scan_calls += 1
            seen = recorder.scan_calls

        if plan.mode == "throttle" and operation_name == plan.target and seen > plan.calls_allowed:
            recorder.raised += 1
            raise _throttle(operation_name)

        response = original_call(self, operation_name, api_params)
        exhausted = "LastEvaluatedKey" not in response
        if operation_name == "Scan":
            recorder.scan_ran_to_completion = exhausted
            if plan.mode == "budget":
                recorder.simulated_seconds += plan.seconds_per_page
        else:
            recorder.query_ran_to_completion = exhausted
        return response

    def _offset_monotonic() -> float:
        return real_monotonic() + recorder.simulated_seconds

    def _offset_time() -> float:
        return real_time() + recorder.simulated_seconds

    def _offset_monotonic_ns() -> int:
        return real_monotonic_ns() + int(recorder.simulated_seconds * 1_000_000_000)

    def _offset_time_ns() -> int:
        return real_time_ns() + int(recorder.simulated_seconds * 1_000_000_000)

    BaseClient._make_api_call = recording
    if plan.mode == "budget":
        time.monotonic = _offset_monotonic
        time.monotonic_ns = _offset_monotonic_ns
        time.time = _offset_time
        time.time_ns = _offset_time_ns
    try:
        yield recorder
    finally:
        BaseClient._make_api_call = original_call
        time.monotonic, time.monotonic_ns = real_monotonic, real_monotonic_ns
        time.time, time.time_ns = real_time, real_time_ns


def _delete_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Every recorded call that removes an object, whatever it names.

    Not filtered to the schema's own key: an interrupted check has no business
    deleting *any* object in the target-schemas bucket, and one that removed the
    sidecar while refusing the body would be reported here rather than slipping
    past a narrower check.
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
# Reading the response without pinning a shape the requirements never state
# ---------------------------------------------------------------------------
def _response_body(response: dict[str, Any]) -> Any:
    body = response.get("body")
    if not isinstance(body, str):
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _find_key(value: Any, wanted: str) -> Any:
    """The first value stored under ``wanted`` anywhere in a parsed body.

    Design §4.2 shows ``reason`` and ``checked`` in the body without fixing their
    nesting, so both are looked up at any depth. ``None`` when absent — and
    ``None`` is distinguishable from a present-but-empty ``checked`` because an
    empty list is returned as an empty list.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key == wanted:
                return item
        for item in value.values():
            found = _find_key(item, wanted)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, wanted)
            if found is not None:
                return found
    return None


def _checked_entries(body: Any) -> list[str] | None:
    """``checked`` as a list of strings, or ``None`` when the field is absent.

    Accepts a list, a mapping (its keys) or a bare string, because task 8.4 is
    free to choose among those. It is *not* free to omit the field on a ``503``:
    that is what the property asserts.
    """
    raw = _find_key(body, "checked")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    if isinstance(raw, list):
        return [str(entry) for entry in raw]
    return [str(raw)]


def _checked_lookups(entries: list[str]) -> frozenset[str]:
    """Which of the two prescribed lookups the ``checked`` entries claim.

    Matched by substring rather than compared to design §4.2's literal
    ``"target_schema_name-index"`` / ``"target_schema_s3_key-scan"``, so the
    property does not pin a spelling the requirements never state. An entry this
    cannot classify is ignored here and surfaced by the emptiness clause, which
    counts entries rather than classifying them.
    """
    claimed: set[str] = set()
    for entry in entries:
        text = entry.lower()
        if "scan" in text or "target_schema_s3_key" in text:
            claimed.add(LOOKUP_SCAN)
        elif "index" in text or "query" in text or "target_schema_name" in text:
            claimed.add(LOOKUP_INDEX)
    return frozenset(claimed)


# ---------------------------------------------------------------------------
# The population — every shape here is Query-blind, so the Scan always runs
# ---------------------------------------------------------------------------
SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_BYTES = (json.dumps(SCHEMA_BODY, sort_keys=True, indent=2) + "\n").encode("utf-8")
OTHER_SCHEMA_BYTES = (json.dumps({"type": "object", "properties": {"sku": {"type": "string"}}}) + "\n").encode("utf-8")


class RecordSpec(NamedTuple):
    """One pipeline record, described by what it does to R3.8's predicate.

    ``references`` is the oracle's verdict for this record alone, written beside
    the record's shape rather than computed from it. Every kind in this file has a
    ``target_schema_name`` that does **not** equal the schema id, because a name
    match would let the Query answer and the Scan would never run — and the Scan
    is this property's whole subject.
    """

    kind: str
    name: str | None
    key: str | None
    references: bool


def _legacy_name(schema_id: str) -> str:
    """The ``target_schema_name`` the pre-fix code wrote for this id.

    The old code stripped a leading ``uploaded_``, which is what put live record
    ``testing-39eddb17``'s name and key one prefix apart
    (``pipelines_crud.py:822-830``): for
    ``uploaded_schema_1785844048926_20260804_114729`` this returns the record's own
    ``schema_1785844048926_20260804_114729``. For an id that never carried the
    prefix the strip is a no-op, which would make the record *agree* with its key,
    so the fallback prefixes instead.
    """
    if schema_id.startswith("uploaded_"):
        return schema_id[len("uploaded_") :]
    return f"legacy_{schema_id}"


def _record_kinds(schema_id: str, other_id: str) -> list[RecordSpec]:
    """The Query-blind record shapes this property draws from."""
    return [
        # The live disagreeing record: only the Scan can see it, and it is the one
        # record here that R3.8 says references the schema.
        RecordSpec("key-only (disagreeing)", _legacy_name(schema_id), _body_key(schema_id), True),
        # Near misses. R3.8's predicate is equality on the key, so none of these
        # references the schema — and a handler that refused on any of them would
        # make every delete impossible while looking correct.
        RecordSpec(
            "near miss: key contains the id, names another object",
            f"uploaded_{schema_id}",
            f"shared/uploaded_{schema_id}.json",
            False,
        ),
        RecordSpec("near miss: the -metadata sidecar key", other_id, _sidecar_key(schema_id), False),
        RecordSpec("near miss: key with a suffix", other_id, f"{_body_key(schema_id)}.bak", False),
        RecordSpec("unrelated", other_id, _body_key(other_id), False),
    ]


class Scenario(NamedTuple):
    """One schema under delete, a bystander, the table's contents, and the plan."""

    schema_id: str
    other_id: str
    records: tuple[RecordSpec, ...]
    plan: Interruption

    @property
    def is_referenced(self) -> bool:
        """The oracle, straight from R3.8: some record's key names our object."""
        return any(spec.references for spec in self.records)


def _item(spec: RecordSpec, index: int) -> dict[str, Any]:
    """A DynamoDB item for one spec, on a deterministic reclaimable id.

    ``created_at`` is always present because it is the GSI's sort key
    (``tests/fixtures/conftest.py:59``), so an item without it is not in
    ``target_schema_name-index`` at all — a record the index cannot answer for
    reasons unrelated to this property. ``create_pipeline`` always writes it
    (``pipelines_crud.py:838``).
    """
    item: dict[str, Any] = {
        "pipeline_id": f"p35-inc-{index}",
        "pipeline_name": f"{spec.kind} #{index}",
        "status": "active",
        "created_at": f"2026-08-07T00:00:{index:02d}Z",
    }
    if spec.name is not None:
        item["target_schema_name"] = spec.name
    if spec.key is not None:
        item["target_schema_bucket"] = TARGET_SCHEMAS_BUCKET
        item["target_schema_s3_key"] = spec.key
    return item


# ---------------------------------------------------------------------------
# The fixture — shared moto backends, the handler bindings, and the padding
# ---------------------------------------------------------------------------
@pytest.fixture
def interrupted_delete_env(mock_s3_buckets, mock_dynamodb_tables, monkeypatch):
    """The shared moto backends, the env, the table bindings, and the padding.

    Nothing is stubbed that is not an address: ``TARGET_SCHEMAS_BUCKET`` names the
    fixture's bucket and ``AWS_ACCOUNT_ID`` keeps ``get_aws_account_id``
    (``shared/env_utils.py:13``, called at ``schemas.py:539``) off STS.

    Both table bindings are deliberate — see the module docstring. ``raising=False``
    on the ``handlers.schemas`` one because that attribute does not exist until
    task 8.4 adds it.

    The padding is seeded here, once per test function rather than per example:
    it is what makes a full scan of this table take three real pages, and it is
    constant across examples so no example can inherit another's pagination.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(schemas_handler, "pipelines_table", table, raising=False)
    monkeypatch.setattr(common_handler, "pipelines_table", table)

    filler = "p" * _PADDING_BYTES
    for index in range(_PADDING_RECORDS):
        table.put_item(
            Item={
                "pipeline_id": f"p35-inc-pad-{index:02d}",
                "pipeline_name": f"padding {index}",
                "status": "archived",
                "created_at": f"2026-01-01T00:00:{index:02d}Z",
                "target_schema_name": _PADDING_NAME,
                "target_schema_s3_key": f"shared/{_PADDING_NAME}-{index}.json",
                "filler": filler,
            }
        )
    return table


# ---------------------------------------------------------------------------
# Seeding and reading the world
# ---------------------------------------------------------------------------
def _wipe_bucket() -> None:
    """Empty the target-schemas bucket so each example starts clean.

    Hypothesis reuses one fixture across every example, so without this an object
    an earlier example left behind would decide a later one's outcome. It runs in
    the harness, outside the recorder's window, so its own ``delete_objects`` is
    never one of the calls under assertion.
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


def _seed(table, scenario: Scenario) -> set[str]:
    """Put the world in place and return the "before" key set.

    The schema's object is **always** present, so R3.5's already-absent success
    cannot short-circuit the reference check and carry the ``503`` clause away
    with it. The padding is not touched — it belongs to the fixture.
    """
    _wipe_bucket()
    _put(_body_key(scenario.schema_id), SCHEMA_BYTES)
    _put(_body_key(scenario.other_id), OTHER_SCHEMA_BYTES)

    for index in range(_MAX_RECORDS):
        table.delete_item(Key={"pipeline_id": f"p35-inc-{index}"})
    for index, spec in enumerate(scenario.records):
        table.put_item(Item=_item(spec, index))
    return _inventory()


def _scan_page_count(table, schema_id: str) -> int:
    """How many pages a full filtered scan of the seeded table really takes.

    Used by the harness tripwires to *measure* the pagination the interruption
    points depend on, rather than assuming the padding is enough.
    """
    pages = 0
    kwargs: dict[str, Any] = {"FilterExpression": Attr("target_schema_s3_key").eq(_body_key(schema_id))}
    while True:
        page = table.scan(**kwargs)
        pages += 1
        if "LastEvaluatedKey" not in page:
            return pages
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#: Both id shapes that reach this endpoint. The double-suffix form is the live
#: pre-R4.5 shape — ``testing-39eddb17``'s key names
#: ``uploaded_schema_1785844048926_20260804_114729`` — and the single-suffix form
#: is what task 8.6 will produce.
_SCHEMA_IDS = st.sampled_from(
    (
        "uploaded_schema_1785844048926_20260804_114729",
        "uploaded_schema_1785844048926",
        "customers_jsonschema",
    )
)

_ID_A = "uploaded_schema_1785844048926_20260804_114729"
_ID_B = "customers_jsonschema"


@st.composite
def interruptions(draw) -> Interruption:
    """An interruption point, weighted towards the Scan.

    ``calls_allowed`` is capped at 2 because the seeded table pages three times —
    a higher value would plan an interruption the scan never reaches, which is
    indistinguishable from a handler that stopped early. The Query is only ever
    interrupted before its first page: it answers in one page for these
    populations, so ``calls_allowed >= 1`` would never fire.
    """
    mode = draw(st.sampled_from(("throttle", "throttle", "throttle", "budget")))
    if mode == "budget":
        return Interruption(
            mode="budget",
            target="Scan",
            calls_allowed=0,
            seconds_per_page=draw(st.sampled_from((30.0, 300.0))),
        )
    target = draw(st.sampled_from(("Scan", "Scan", "Scan", "Query")))
    calls_allowed = 0 if target == "Query" else draw(st.integers(min_value=0, max_value=2))
    return Interruption(
        mode="throttle",
        target=target,
        calls_allowed=calls_allowed,
        error_code=draw(st.sampled_from(_THROTTLE_CODES)),
    )


@st.composite
def scenarios(draw) -> Scenario:
    """A Query-blind population and an interruption point.

    Drawn as a subset of the five record kinds rather than as free-form records:
    the kinds are the rows of R3.8's truth table that a Query cannot answer, and a
    free-form draw would spend most examples on records differing only in a
    string. ``min_size=0`` keeps the empty table reachable — "no pipeline record
    exists at all" is the population on which an *uninterrupted* check would
    delete, so it is the sharpest input for a ``503``.
    """
    schema_id = draw(_SCHEMA_IDS)
    other_id = draw(_SCHEMA_IDS)
    if other_id == schema_id:
        other_id = f"{other_id}_bystander"

    kinds = _record_kinds(schema_id, other_id)
    chosen = draw(st.lists(st.sampled_from(kinds), min_size=0, max_size=_MAX_RECORDS, unique_by=lambda spec: spec.kind))
    return Scenario(schema_id, other_id, tuple(chosen), draw(interruptions()))


def _scenario_of(schema_id: str, other_id: str, plan: Interruption, *kinds: str) -> Scenario:
    """Build a scenario by naming the record kinds it should contain."""
    by_kind = {spec.kind: spec for spec in _record_kinds(schema_id, other_id)}
    missing = [kind for kind in kinds if kind not in by_kind]
    assert not missing, f"unknown record kind(s): {missing}"
    return Scenario(schema_id, other_id, tuple(by_kind[kind] for kind in kinds), plan)


#: The cases this property exists for, as **explicit** inputs rather than as draws
#: that might happen. Tasks 8.1 and 8.2 both promoted their required cases for the
#: same reason: over 200 examples a required case was reached 0 times on one seed
#: and ~25 on the next, so a coverage claim that depends on the seed is not a
#: claim.
_CRITICAL_SCENARIOS = (
    # The sharpest input in the file: nothing references the schema, so an
    # uninterrupted check would delete — and the check did not finish, so a 200
    # would be exactly the unqualified claim DD2 forbids.
    Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 0)),
    # The same, interrupted genuinely partway through pagination.
    Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 1, error_code="ThrottlingException")),
    Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 2, error_code="RequestLimitExceeded")),
    # Unreferenced but full of near misses, so a refusal cannot come from them.
    _scenario_of(
        _ID_A,
        _ID_B,
        Interruption("throttle", "Scan", 1),
        "near miss: key contains the id, names another object",
        "near miss: the -metadata sidecar key",
        "near miss: key with a suffix",
        "unrelated",
    ),
    # The wall-clock budget rather than a throttle, at both magnitudes.
    Scenario(_ID_A, _ID_B, (), Interruption("budget", "Scan", 0, seconds_per_page=30.0)),
    Scenario(_ID_A, _ID_B, (), Interruption("budget", "Scan", 0, seconds_per_page=300.0)),
    # The Query interrupted before its first page: NO lookup completed, so
    # `checked` must claim none of them.
    Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Query", 0)),
    _scenario_of(_ID_A, _ID_B, Interruption("throttle", "Query", 0), "key-only (disagreeing)"),
    # A referenced population, interrupted. 409 (the hit was found) and 503 (the
    # check did not finish) are both defensible; deleting is not.
    _scenario_of(_ID_A, _ID_B, Interruption("throttle", "Scan", 1), "key-only (disagreeing)"),
    _scenario_of(_ID_A, _ID_B, Interruption("budget", "Scan", 0), "key-only (disagreeing)"),
    # The single-suffix id shape task 8.6 will produce.
    Scenario("uploaded_schema_1785844048926", _ID_B, (), Interruption("throttle", "Scan", 0)),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_SCENARIOS`` as explicit examples, in order."""
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
#: Accumulated across the survey. A module-level accumulator because Hypothesis's
#: statistics are per-example and cannot carry a cross-example claim — and because
#: **the property short-circuits** while ``delete_schema`` is a ``501`` stub: it
#: fails on its first example, so its own statistics measure nothing. The survey
#: keeps the coverage claim measurable during exactly the window in which the
#: property is expected to be red.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached."""

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    plan = scenario.plan
    event(f"population size {len(scenario.records)}")
    event("referenced (409 or 503, never a delete)" if scenario.is_referenced else "unreferenced (503 required)")

    if plan.mode == "budget":
        event("interrupted by the wall-clock budget, not by a throttle")
        event(f"budget: +{plan.seconds_per_page:g}s per scan page")
    else:
        event("interrupted by a throttle")
        event(f"throttle code {plan.error_code}")
        if plan.target == "Query":
            event("the QUERY is interrupted — no lookup completed, so `checked` must claim none")
        elif plan.calls_allowed == 0:
            event("the scan is interrupted before its first page — only the index completed")
        else:
            event("the scan is interrupted genuinely partway through pagination")
            event(f"the scan is interrupted on page {plan.calls_allowed + 1}")

    if not scenario.records:
        event("an empty population — an uninterrupted check would delete here")
    if any(spec.kind.startswith("near miss") for spec in scenario.records):
        event("a near-miss record that must not turn the 503 into a 409")
    if any(spec.references for spec in scenario.records):
        event("the live disagreeing record is present (testing-39eddb17)")


@_with_critical_examples
@given(scenario=scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_interruptions_the_property_needs(scenario):
    """Every scenario is Query-blind, and every plan can fire.

    The per-example half of the survey. ``phases`` is limited to the explicit
    examples plus generation: there is no failure to shrink or explain and no
    reuse-database entry to replay. No AWS and no handler — the generator and the
    reporter only.

    **Validates: Requirement 3.10**
    """
    _report_generated_cases(scenario)

    assert scenario.other_id != scenario.schema_id, "the bystander must be a different schema"
    assert len(scenario.records) <= _MAX_RECORDS, (
        f"the harness reclaims exactly {_MAX_RECORDS} pipeline ids, so a longer population would leak "
        f"records into the next example; drew {len(scenario.records)}"
    )
    assert len({spec.kind for spec in scenario.records}) == len(scenario.records), (
        f"each record kind may appear at most once: {[spec.kind for spec in scenario.records]}"
    )
    for spec in scenario.records:
        # Query-blindness is the invariant that makes the Scan run at all. A kind
        # whose name equalled the id would be answered by the index, the Scan
        # would never be reached, and the interruption would never fire.
        assert spec.name != scenario.schema_id, (
            f"record kind {spec.kind!r} carries the schema id as its target_schema_name, so the Query "
            f"would answer and the Scan — this property's subject — would never run"
        )
        assert spec.references == (spec.key == _body_key(scenario.schema_id)), (
            f"record kind {spec.kind!r} labels itself references={spec.references} while its key is "
            f"{spec.key!r} against R3.8's {_body_key(scenario.schema_id)!r}"
        )

    plan = scenario.plan
    assert plan.mode in ("throttle", "budget")
    assert plan.target in ("Query", "Scan")
    assert plan.calls_allowed <= 2, (
        f"the seeded table pages three times, so an interruption planned after {plan.calls_allowed} "
        f"calls could never fire and would be indistinguishable from a handler that stopped early"
    )
    if plan.target == "Query":
        assert plan.calls_allowed == 0, "the Query answers in one page here, so only page 1 can be interrupted"
    if plan.mode == "budget":
        assert plan.seconds_per_page >= 30.0, (
            "the budget must be exhausted by a single page — design §4.2 puts the real budget well "
            "inside the 25s Lambda timeout (compute-stack.ts:630)"
        )


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once in 200 draws.

    Tasks 8.1 and 8.2 both used this assertion for the same reason. Tasks 3.1 and
    4.3 each found generators that reached their target case **0 times out of 200
    and passed anyway**, caught only by someone reading
    ``--hypothesis-show-statistics`` by hand. Here the coverage claim is a test, so
    it fails when the generator drifts away from the cases rather than going quiet.

    The entries that matter most are the three interruption *points* — before the
    first page, partway through pagination, and at the Query — because a generator
    that produced only one of them would leave the property passing while saying
    nothing about "for any point at which the paginated scan is interrupted".

    Every required case is guaranteed by ``_CRITICAL_SCENARIOS`` rather than left
    to the draw, which is what makes this deterministic instead of seed-dependent.

    **Validates: Requirement 3.10**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the whole "
        "module has to run, in definition order"
    )

    required = (
        "unreferenced (503 required)",
        "referenced (409 or 503, never a delete)",
        "interrupted by a throttle",
        "interrupted by the wall-clock budget, not by a throttle",
        "the scan is interrupted before its first page — only the index completed",
        "the scan is interrupted genuinely partway through pagination",
        "the scan is interrupted on page 2",
        "the scan is interrupted on page 3",
        "the QUERY is interrupted — no lookup completed, so `checked` must claim none",
        "an empty population — an uninterrupted check would delete here",
        "a near-miss record that must not turn the 503 into a 409",
        "the live disagreeing record is present (testing-39eddb17)",
        *(f"throttle code {code}" for code in _THROTTLE_CODES),
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=scenarios())
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto bucket and up to five DynamoDB
    # records, which routinely exceeds Hypothesis's 200ms default. The property
    # asserts an outcome, not latency.
    deadline=None,
    # One fixture serves every example by design; `_seed` wipes the bucket and
    # reclaims the five pipeline ids at the start of each one, and the padding is
    # constant, so nothing an earlier example wrote decides a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    # `Phase.explain` is excluded deliberately, on task 8.1's measurement: it
    # re-ran ~400 further examples to annotate a failure whose cause the assertion
    # message already states, at 72s of that file's 78s. This property is expected
    # to be RED from task 8.3 until task 8.4 and every checkpoint in the plan runs
    # the full suite, so that is a standing cost for no information. Shrinking is
    # kept — a minimal counter-example is what is worth having when this fails for
    # a real reason after 8.4 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_an_incomplete_reference_check_never_deletes(scenario, interrupted_delete_env):
    """Property 5: an interrupted check refuses, and never deletes.

    Clause order is deliberate. The **model** — "deleted ⟹ every lookup in
    ``checked`` completed" — is checked first, against the harness's own ground
    truth about what it interrupted, because a handler that deleted after an
    unfinished check must be reported as violating R3.10 rather than as returning
    the wrong status code.

    **Validates: Requirements 3.10, 1.4, 5.3**
    """
    _report_generated_cases(scenario)

    table = interrupted_delete_env
    before = _seed(table, scenario)
    body_key = _body_key(scenario.schema_id)
    assert body_key in before, "the harness must seed the object the delete is about"

    with _interrupted_lookups(scenario.plan) as recorder:
        response = schemas_handler.delete_schema(scenario.schema_id)

    status = response["statusCode"]
    body = _response_body(response)
    after = _inventory()
    deleted = body_key not in after
    deletes = _delete_calls(recorder.s3_calls)
    interrupted = recorder.interrupted
    entries = _checked_entries(body)
    shapes = [spec.kind for spec in scenario.records]
    context = (
        f"\n  schema:   {scenario.schema_id!r}"
        f"\n  records:  {shapes}"
        f"\n  plan:     {scenario.plan.label}"
        f"\n  observed: {recorder.operations} "
        f"(raised={recorder.raised}, simulated={recorder.simulated_seconds:g}s, "
        f"scan_ran_out_of_pages={recorder.scan_ran_to_completion})"
        f"\n  status:   {status}"
        f"\n  body:     {body!r}"
    )

    # -- The model: deleted ⟹ nothing was interrupted -------------------------
    assert not (deleted and interrupted), (
        f"MODEL VIOLATION — 'deleted ⟹ every lookup in `checked` completed'. The object is gone and "
        f"{sorted(interrupted)} did not complete, so the delete rests on a claim the code cannot "
        f"support (R3.10, R1.4).{context}"
    )
    assert not (interrupted and deletes), (
        f"R3.10: {sorted(interrupted)} did not complete and the handler still issued a delete. No "
        f"delete may be issued on any path where the check did not finish.\n  delete calls: "
        f"{deletes}{context}"
    )
    # `checked`'s contents are asserted on every outcome EXCEPT a `409`. A `409`
    # is justified by hits the lookup actually found, so design §4.2's second row
    # legitimately reports `[index, scan]` for a scan that produced a hit and then
    # stopped — and in budget mode the harness cannot distinguish "stopped at the
    # hit" from "ran out of clock", because both look like a scan that did not run
    # out of pages. A `409` makes no claim about references never found, which is
    # what this property is about, and task 8.2 owns the `409`'s contents.
    if interrupted and entries is not None and status != 409:
        claimed = _checked_lookups(entries)
        assert not (claimed & interrupted), (
            f"`checked` reports {sorted(claimed & interrupted)} as checked, and the harness prevented "
            f"exactly that from completing. `checked` must report **only the lookups that completed** — "
            f"it is not diagnostics, it is what distinguishes 'checked and found nothing' from 'could "
            f"not check'.\n  checked: {entries}{context}"
        )

    if not interrupted:
        # -- The interruption never fired. Two very different reasons ----------
        if scenario.is_referenced:
            # This can be legitimate: a hit on the first page is already enough to
            # refuse (design §4.2's second row), so the scan may stop before the
            # interruption point. It is legitimate only if a lookup ran at all and
            # it refused — nothing about `checked` is asserted, because that is
            # Property 4's outcome and task 8.2 owns it.
            assert recorder.operations, (
                f"the handler performed no Query and no Scan, so it ran no reference check at all.\n"
                f"  `delete_schema` is a 501 stub until task 8.4 — this is the expected red.{context}"
            )
            assert not deletes and not deleted, (
                f"R3.7: a delete was issued for a schema a pipeline record references, with the "
                f"lookup never interrupted. A Query-only lookup cannot see a record whose "
                f"target_schema_name disagrees with its target_schema_s3_key — live record "
                f"testing-39eddb17 — which is exactly how it deletes.\n"
                f"  delete calls: {deletes}{context}"
            )
            assert status == 409, (
                f"R3.7: the lookup ran and was not interrupted, so it saw the referencing record — "
                f"whether it completed or stopped at the hit — and design §4.2's second row says "
                f"refuse with 409. Got {status}.{context}"
            )
            return
        if scenario.plan.mode == "budget" and recorder.scan_ran_to_completion:
            pytest.fail(
                f"the handler consumed the whole paginated scan across a simulated "
                f"{recorder.simulated_seconds:g}s without giving up, so it has no wall-clock budget "
                f"the harness can observe. Design §4.2 requires one, because the dashboard API "
                f"Lambda's timeout is 25s (compute-stack.ts:630) and a scan that overran it would be "
                f"killed mid-request rather than answering anything.\n"
                f"  If task 8.4 does have a budget but measures it with a clock this harness does "
                f"not drive (time.perf_counter, datetime.datetime.now), widen "
                f"`_interrupted_lookups`'s clock rather than loosening this clause.{context}"
            )
        pytest.fail(
            f"the reference check was never interrupted on an UNREFERENCED population, so the "
            f"handler cannot have run the lookup this property is about.\n"
            f"  `delete_schema` is a 501 stub until task 8.4 — this is the expected red.\n"
            f"  After 8.4 this same failure means one of two things, and the observed calls above "
            f"say which: no DynamoDB call at all means the reference check does not exist; a Query "
            f"and no Scan means Query-only, which design §4.2 rejects because it deletes the object "
            f"out from under live record testing-39eddb17.{context}"
        )

    # -- An interrupted check makes no claim ----------------------------------
    assert not 200 <= status < 300, (
        f"R1.4/R5.3: {sorted(interrupted)} did not complete, so the handler cannot claim success. "
        f"'Checked and found nothing' and 'could not check' are the distinction this endpoint turns "
        f"on, and a success asserts the first.{context}"
    )
    assert not deleted, (
        f"R3.10: the object {body_key!r} is gone after an interrupted check. The call-level check "
        f"passed, so it was removed by something other than a recorded delete.\n"
        f"  keys now: {sorted(after)}{context}"
    )
    assert _body_key(scenario.other_id) in after, (
        f"a delete of {scenario.schema_id!r} removed the bystander {scenario.other_id!r}'s object.\n"
        f"  keys now: {sorted(after)}{context}"
    )

    if scenario.plan.target == "Query":
        # Task 8.3's text is about the paginated scan, so the status for an
        # interrupted Query is left to task 8.4 — but no lookup completed, so if a
        # `checked` is present it must claim nothing.
        assert status != 409, (
            f"R3.7: the Query never returned, so no referencing pipeline was ever found — a 409 "
            f"would assert references that were not found, which design §4.2 calls the worse of the "
            f"two wrong answers.{context}"
        )
        if entries is not None:
            assert entries == [], (
                f"the Query was interrupted before its first page, so **no** lookup completed and "
                f"`checked` must be empty. It reported {entries}.{context}"
            )
        return

    # -- The scan was interrupted: design §4.2's fourth row -------------------
    if scenario.is_referenced and status == 409:
        # A hit was found before the interruption. Refusing on it is right, and
        # the earlier clauses have already established that nothing was deleted.
        return

    assert status == 503, (
        f"design §4.2's fourth row: the paginated scan was interrupted "
        f"({scenario.plan.label}) and the check could not establish that no pipeline references "
        f"{scenario.schema_id!r}, so the request must be refused with 503 — 'ask again'. Got "
        f"{status}.\n"
        f"  If the scan RAN OUT OF PAGES here (scan_ran_out_of_pages=True above) while a budget "
        f"mode simulated {recorder.simulated_seconds:g}s, the handler consumed the whole scan "
        f"without noticing the clock: either it has no wall-clock budget at all — which design §4.2 "
        f"requires, because the dashboard API Lambda's timeout is 25s "
        f"(compute-stack.ts:630) — or it measures one with a clock this harness does not drive "
        f"(time.perf_counter, datetime.datetime.now). In the second case widen "
        f"`_interrupted_lookups`'s clock rather than loosening this clause.{context}"
    )
    assert _find_key(body, "reason") == "reference_check_incomplete", (
        f'the 503 must carry reason: "reference_check_incomplete" so the caller can tell it apart '
        f"from any other 503 — task 8.3 pins this string, and task 8.7's frontend renders 'ask "
        f"again' on it rather than a generic error.\n  reason: {_find_key(body, 'reason')!r}{context}"
    )
    assert entries is not None, (
        f"the 503 must carry `checked`. It is not diagnostics: it is what distinguishes 'checked and "
        f"found nothing' from 'could not check', which is what `LGR-acca05d-4` requires the response "
        f"semantics to state.{context}"
    )
    assert entries, (
        f"`checked` is empty while the Query completed. It must report the lookups that DID "
        f"complete, not only omit the ones that did not.{context}"
    )
    assert LOOKUP_SCAN not in _checked_lookups(entries), (
        f"`checked` claims the scan among the completed lookups, and the harness interrupted it "
        f"({scenario.plan.label}).\n  checked: {entries}{context}"
    )


# ---------------------------------------------------------------------------
# The one field the property leaves out, asserted once
# ---------------------------------------------------------------------------
def test_the_503_is_marked_retryable(interrupted_delete_env):
    """``retryable: true`` on the incomplete-check ``503``.

    Design §4.2's fourth row and task 8.4's text both name it; task 8.3's text
    does not, so it is asserted here rather than as a clause of the property.
    Throttling and a budget overrun are both transient, which is the whole reason
    ``503`` was chosen over ``409``.

    **Expected RED until task 8.4**, on the status: the stub answers ``501``.

    **Validates: Requirement 3.10**
    """
    scenario = Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 0))
    _seed(interrupted_delete_env, scenario)

    with _interrupted_lookups(scenario.plan) as recorder:
        response = schemas_handler.delete_schema(scenario.schema_id)

    body = _response_body(response)
    assert response["statusCode"] == 503, (
        f"`delete_schema` is a 501 stub until task 8.4 — the expected red. Observed calls: "
        f"{recorder.operations}, body: {body!r}"
    )
    assert _find_key(body, "retryable") is True, (
        f"the incomplete-check 503 is retryable — the cause is a throttle or a budget overrun, both "
        f"transient. Body: {body!r}"
    )


# ---------------------------------------------------------------------------
# The harness, proved armed
# ---------------------------------------------------------------------------
class TestTheHarnessIsArmed:
    """The spy, the pagination and the clock do what the property assumes.

    Every absence assertion in this file passes today because ``delete_schema``
    makes no call at all, so on their own they are indistinguishable from a spy
    that records nothing, an interruption that never fires and an oracle that
    forbids nothing. These tests do each of those things **themselves** and
    require the harness to report them, so the guard is known-good before there is
    any code for it to guard.
    """

    def test_a_full_scan_of_the_seeded_table_really_takes_three_pages(self, interrupted_delete_env):
        """The pagination the interruption points depend on is measured, not assumed.

        Without this, ``calls_allowed=2`` would silently plan an interruption the
        scan never reaches, and "interrupted partway through pagination" would be
        a claim rather than a fact. moto applies the 1 MB limit before the
        ``FilterExpression`` (``moto/dynamodb/models/table.py:1103-1149``), so
        these pages are mostly empty after filtering — exactly as the service's are.
        """
        _seed(interrupted_delete_env, Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 0)))

        pages = _scan_page_count(interrupted_delete_env, _ID_A)

        assert pages >= 3, (
            f"a full filtered scan took {pages} page(s); the property plans interruptions on pages 1, "
            f"2 and 3, so the padding ({_PADDING_RECORDS} x {_PADDING_BYTES} bytes) is no longer "
            f"enough to reach them"
        )

    def test_the_spy_records_real_calls_and_lets_them_through(self, interrupted_delete_env):
        """A recorded call is still a real call: the object is really removed."""
        _put(_body_key("spy_check"), b"{}")

        with _interrupted_lookups(Interruption("throttle", "Scan", 99)) as recorder:
            _client("s3").delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("spy_check"))

        assert _body_key("spy_check") not in _inventory(), "the spy must call through, not stand in for moto"
        assert _delete_calls(recorder.s3_calls) == [f"DeleteObject(Key={_body_key('spy_check')!r})"]

    def test_a_batch_delete_is_reported_too(self, interrupted_delete_env):
        """``DeleteObjects`` is the same intention under another name."""
        _put(_body_key("batch_check"), b"{}")

        with _interrupted_lookups(Interruption("throttle", "Scan", 99)) as recorder:
            _client("s3").delete_objects(
                Bucket=TARGET_SCHEMAS_BUCKET, Delete={"Objects": [{"Key": _body_key("batch_check")}]}
            )

        reported = _delete_calls(recorder.s3_calls)
        assert len(reported) == 1 and reported[0].startswith("DeleteObjects"), (
            f"a batch delete must be reported; got {reported}"
        )

    @pytest.mark.parametrize("code", _THROTTLE_CODES)
    def test_the_first_scan_can_be_throttled(self, code, interrupted_delete_env):
        """``calls_allowed=0`` fails the scan before it returns anything."""
        _seed(interrupted_delete_env, Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 0)))

        with (
            _interrupted_lookups(Interruption("throttle", "Scan", 0, error_code=code)) as recorder,
            pytest.raises(ClientError) as raised,
        ):
            _scan_page_count(interrupted_delete_env, _ID_A)

        assert raised.value.response["Error"]["Code"] == code
        assert recorder.interrupted == frozenset({LOOKUP_SCAN})
        assert recorder.scan_calls == 1, f"the throttle must land on the first page; saw {recorder.scan_calls}"

    def test_a_later_scan_page_can_be_throttled(self, interrupted_delete_env):
        """``calls_allowed=2`` lets two pages through and fails the third.

        This is what makes "for any point at which the paginated scan is
        interrupted" a real range rather than a single point.
        """
        _seed(interrupted_delete_env, Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Scan", 2)))

        with _interrupted_lookups(Interruption("throttle", "Scan", 2)) as recorder, pytest.raises(ClientError):
            _scan_page_count(interrupted_delete_env, _ID_A)

        assert recorder.scan_calls == 3, f"two pages must pass through before the throttle; saw {recorder.scan_calls}"
        assert not recorder.scan_ran_to_completion, "the scan must not have run out of pages before the throttle"
        assert recorder.interrupted == frozenset({LOOKUP_SCAN})

    def test_the_query_can_be_throttled_and_the_scan_left_alone(self, interrupted_delete_env):
        """Targeting the Query leaves the Scan untouched, and vice versa."""
        _seed(interrupted_delete_env, Scenario(_ID_A, _ID_B, (), Interruption("throttle", "Query", 0)))
        table = interrupted_delete_env

        with _interrupted_lookups(Interruption("throttle", "Query", 0)) as recorder:
            with pytest.raises(ClientError):
                table.query(
                    IndexName="target_schema_name-index",
                    KeyConditionExpression=Key("target_schema_name").eq(_ID_A),
                )
            # The scan is not the target, so it still works.
            pages = _scan_page_count(table, _ID_A)

        assert pages >= 3
        assert recorder.interrupted == frozenset({LOOKUP_INDEX})
        assert recorder.query_calls == 1 and recorder.scan_calls >= 3

    def test_the_budget_clock_advances_only_in_budget_mode(self, interrupted_delete_env):
        """A page of scan moves the clock forward, and only when planned.

        The clocks driven are ``time.monotonic`` and ``time.time``. If task 8.4
        measures its budget with ``time.perf_counter`` or ``datetime.now()``, this
        test still passes and the property's budget-mode examples fail — which is
        why the property's failure message names that possibility explicitly.
        """
        _seed(interrupted_delete_env, Scenario(_ID_A, _ID_B, (), Interruption("budget", "Scan", 0)))
        table = interrupted_delete_env

        with _interrupted_lookups(Interruption("budget", "Scan", 0, seconds_per_page=30.0)) as recorder:
            started = time.monotonic()
            wall_started = time.time()
            pages = _scan_page_count(table, _ID_A)
            elapsed = time.monotonic() - started
            wall_elapsed = time.time() - wall_started

        assert pages >= 3
        assert elapsed >= 60.0, f"three pages at +30s each must read as at least 60s elapsed; got {elapsed:g}s"
        assert wall_elapsed >= 60.0, f"time.time must advance with time.monotonic; got {wall_elapsed:g}s"
        assert recorder.simulated_seconds >= 90.0
        assert time.monotonic() - started < 60.0, "the clock must be restored when the block exits"

        with _interrupted_lookups(Interruption("throttle", "Scan", 99)) as recorder:
            started = time.monotonic()
            _scan_page_count(table, _ID_A)
            assert time.monotonic() - started < 5.0, "throttle mode must not touch the clock"
        assert recorder.simulated_seconds == 0.0

    def test_reads_are_not_forbidden(self, interrupted_delete_env):
        """The reads a correct reference check makes are not delete calls.

        A guard that reported ``head_object`` / ``get_object`` /
        ``list_objects_v2`` would make R3.5's already-absent branch unsatisfiable,
        because distinguishing absent from present needs a read.
        """
        _put(_body_key("read_check"), b"{}")

        with _interrupted_lookups(Interruption("throttle", "Scan", 99)) as recorder:
            s3 = _client("s3")
            s3.head_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("read_check"))
            s3.get_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key("read_check"))
            s3.list_objects_v2(Bucket=TARGET_SCHEMAS_BUCKET, Prefix="shared/")

        assert _delete_calls(recorder.s3_calls) == [], "a reference check reads; only a removal is forbidden"


# ---------------------------------------------------------------------------
# Satisfiability, and what a downgraded interruption costs — measured
# ---------------------------------------------------------------------------
class _ReferenceCheckIncomplete(Exception):
    """The probe's own signal. Task 8.4 owns the real one.

    Carries the lookups that **did** complete, because that is what the response's
    ``checked`` may report and nothing more.
    """

    def __init__(self, completed: list[str]) -> None:
        super().__init__("the reference check could not be finished inside its budget")
        self.completed = completed


class TestTheSpecificationIsSatisfiable:
    """The property can be satisfied, and not by downgrading the interruption.

    Design §5 risk 9: a reviewer who sees red and reverts has undone the only
    thing that would have caught the defect. These tests separate "the subject is
    not written yet" from "the property cannot be satisfied".

    The probe below is a **discrimination probe, not a reference
    implementation.** It exists so the oracle can be shown to accept the
    prescribed shape and reject the rejected one; the property itself never calls
    it — it calls ``schemas_handler.delete_schema``. Task 8.4 still owns the real
    handler, with R3.3's ``400``, R3.5's already-absent success, the ``409``'s
    pipeline names and the real budget, none of which is here.
    """

    #: Deliberately small, and deliberately not a proposal for task 8.4's real
    #: budget: it only has to be smaller than the 30s a budget-mode page simulates.
    _BUDGET_SECONDS = 5.0

    @classmethod
    def _find_referencing_pipelines(cls, table, schema_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Design §4.2's Query-then-Scan, bounded, raising rather than truncating."""
        try:
            hits: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {
                "IndexName": "target_schema_name-index",
                "KeyConditionExpression": Key("target_schema_name").eq(schema_id),
            }
            while True:
                page = table.query(**kwargs)
                hits.extend(page.get("Items", []))
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _THROTTLE_CODES:
                raise _ReferenceCheckIncomplete([]) from exc
            raise
        if hits:
            return hits, [LOOKUP_INDEX]

        started = time.monotonic()
        hits = []
        scan_kwargs: dict[str, Any] = {"FilterExpression": Attr("target_schema_s3_key").eq(_body_key(schema_id))}
        while True:
            try:
                page = table.scan(**scan_kwargs)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _THROTTLE_CODES:
                    raise _ReferenceCheckIncomplete([LOOKUP_INDEX]) from exc
                raise
            hits.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return hits, [LOOKUP_INDEX, LOOKUP_SCAN]
            if time.monotonic() - started > cls._BUDGET_SECONDS:
                raise _ReferenceCheckIncomplete([LOOKUP_INDEX])
            scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @classmethod
    def _delete_via(cls, table, schema_id: str, *, downgrade: bool) -> tuple[int, dict[str, Any]]:
        """Refuse, delete, or answer 503 — with ``downgrade`` the rejected variant.

        ``downgrade=True`` is design §4.2's explicitly forbidden shape: it catches
        the incompleteness and treats it as an empty result, so "could not check"
        becomes "no pipeline references this schema".
        """
        try:
            hits, checked = cls._find_referencing_pipelines(table, schema_id)
        except _ReferenceCheckIncomplete as incomplete:
            if not downgrade:
                return 503, {
                    "reason": "reference_check_incomplete",
                    "checked": incomplete.completed,
                    "retryable": True,
                }
            hits, checked = [], [LOOKUP_INDEX, LOOKUP_SCAN]
        if hits:
            return 409, {
                "pipelines": [
                    {"pipelineId": str(item["pipeline_id"]), "pipelineName": str(item.get("pipeline_name", ""))}
                    for item in hits
                ],
                "checked": checked,
            }
        _client("s3").delete_object(Bucket=TARGET_SCHEMAS_BUCKET, Key=_body_key(schema_id))
        return 200, {"deleted": schema_id, "checked": checked}

    @pytest.mark.parametrize("scenario", _CRITICAL_SCENARIOS, ids=lambda s: s.plan.label.replace(" ", "-"))
    def test_the_prescribed_shape_satisfies_the_property_on_every_critical_scenario(
        self, scenario, interrupted_delete_env
    ):
        """Query-then-Scan with a budget answers every scenario the property is about.

        The property's own clauses, against the probe rather than the handler: no
        delete, no success, ``503`` with the pinned ``reason`` and a ``checked``
        that omits the interrupted lookup.
        """
        _seed(interrupted_delete_env, scenario)

        with _interrupted_lookups(scenario.plan) as recorder:
            status, body = self._delete_via(interrupted_delete_env, scenario.schema_id, downgrade=False)

        interrupted = recorder.interrupted
        assert interrupted, f"the probe must reach the interruption point for {scenario.plan.label}"
        assert not _delete_calls(recorder.s3_calls)
        assert _body_key(scenario.schema_id) in _inventory()
        assert not 200 <= status < 300, f"an interrupted check cannot claim success; got {status}"

        if scenario.is_referenced and status == 409:
            # A hit found before the interruption. Refusing on it is right.
            return
        assert status == 503, f"{scenario.plan.label} must produce a 503; got {status} with {body!r}"
        assert body["reason"] == "reference_check_incomplete"
        assert body["retryable"] is True
        entries = _checked_entries(body)
        assert entries is not None
        assert not (_checked_lookups(entries) & interrupted), (
            f"`checked` {entries} claims a lookup the harness interrupted ({sorted(interrupted)})"
        )
        if scenario.plan.target == "Query":
            assert entries == [], "no lookup completed, so `checked` must be empty"
        else:
            assert entries == [LOOKUP_INDEX], "the Query completed and the Scan did not"

    def test_downgrading_the_interruption_to_an_empty_result_deletes_the_object(self, interrupted_delete_env):
        """The rejected shape, and exactly what it costs.

        This is the measurement behind design §4.2's "deliberately NOT caught here
        and NOT downgraded to an empty list": an empty list means "no pipeline
        references this schema", and an unfinished scan cannot support that claim.
        Run against a population that **does** reference the schema by key — the
        live ``testing-39eddb17`` shape — so the downgrade deletes the object out
        from under a pipeline whose record still names the key, which is the state
        ``resolve_target_schema`` raises ``target_schema_object_missing`` on
        (``shared/target_schema_resolution.py:290-302``, ``LGR-78ce0b7-14``).

        It also proves the oracle **discriminates**: the same clauses that pass on
        the prescribed shape fail here, so the property's red is unimplemented
        rather than contradictory.
        """
        scenario = _scenario_of(_ID_A, _ID_B, Interruption("throttle", "Scan", 0), "key-only (disagreeing)")
        _seed(interrupted_delete_env, scenario)

        with _interrupted_lookups(scenario.plan) as recorder:
            status, body = self._delete_via(interrupted_delete_env, _ID_A, downgrade=True)

        assert recorder.interrupted == frozenset({LOOKUP_SCAN})
        assert status == 200 and _body_key(_ID_A) not in _inventory(), (
            "the downgrade deletes the object here, which is the failure the 503 exists to prevent"
        )
        assert LOOKUP_SCAN in _checked_lookups(_checked_entries(body) or []), (
            "and it reports the scan as checked, which is the claim it cannot support — the exact "
            "shape the property's `checked` clause rejects"
        )
        orphan = interrupted_delete_env.get_item(Key={"pipeline_id": "p35-inc-0"})["Item"]
        assert orphan["target_schema_s3_key"] == _body_key(_ID_A), (
            "and the pipeline record still names the key that no longer resolves"
        )

        # The same scenario, answered by the prescribed shape: refused, object
        # intact. Re-seeded so the two are compared on identical input.
        _seed(interrupted_delete_env, scenario)
        with _interrupted_lookups(scenario.plan):
            status, body = self._delete_via(interrupted_delete_env, _ID_A, downgrade=False)
        assert status == 503 and body["reason"] == "reference_check_incomplete"
        assert _body_key(_ID_A) in _inventory()


# ---------------------------------------------------------------------------
# Today's answer, pinned — REMOVED BY TASK 8.4, on its own instruction
# ---------------------------------------------------------------------------
# `test_the_stub_answers_501_and_deletes_nothing` lived here while
# `delete_schema` was a `501` stub. It recorded *why* the clauses above were red —
# the stub performed no lookup at all, so the interruption could never fire — and
# its docstring was its own expiry note: "Task 8.4 will make this test fail, and
# that failure is the signal to delete this test, not to loosen it."
#
# Task 8.4 landed the handler, so it is gone. The interruption now fires against
# real Query and Scan calls, which is what the property was written to measure.
# `test_schema_delete_endpoint.py` owns the example-level outcomes (R3.3's `400`,
# R3.5's already-absent success, and each outcome's `checked`).
