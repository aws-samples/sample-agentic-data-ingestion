# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Display_Label pass's two bounds, as examples rather than as properties.

p36-schema-naming task 2.7. **Validates: Requirements 1.8, 1.9.**

Two tests, one per half of design §4.2's D2:

1. **The deadline returns and reports.** A slow-read stub plus a short injected
   window: `get_schemas` **returns**, returns a `200`, carries a row for *every*
   schema, and reports the labels it did not get through `sourcesFailed`.
2. **Requirement 1.9's stated shape completes with exactly the bounded read
   count.** 1000 Uploaded_Schemas of which 100 are Annotated_Schemas, with a fast
   stub: it completes, and it performs **exactly 100** sidecar `GetObject` calls.

**Why neither of these is a property, and why neither asserts a duration.**
Design §6 lists both in the "deliberately non-property cases" table, and the rule
it states is the one that shapes every assertion below: *do not assert wall-clock
seconds — that tests the CI host*. A latency assertion in CI is a flaky test
wearing a correctness costume; it fails on a loaded runner and passes on a fast
one, and neither outcome says anything about this code. The design made
Requirement 1.9 hold **by construction** — the label pass costs at most the
deadline, whatever the per-read latency `L` turns out to be, and `L` is
unmeasured (design §5 risk 3, N4) — precisely so that these tests can assert
*behaviour under a bound* instead of measuring time:

- that the request **returns** rather than running to the Lambda's own 25 s
  timeout (`compute-stack.ts:630`), which is the outcome
  `schemas.py:428-441` calls "the one outcome that makes every requirement in
  this spec unobservable at once";
- that it **degrades a label and never a schema** (Requirement 1.10), so the
  overrun is visible in the response rather than inferred from a missing name;
- that the read **count** is exactly the number of Annotated_Schemas
  (Requirement 1.8), which bounds cost without bounding clock time.

The second is also why 1000/100 is one example and not a hundred: a hundred
iterations of a thousand-object moto world would measure moto (design §6).

**How the window is shortened, and why not with a real 18-second wait.**
`_label_pass_deadline` (`schemas.py:991-1013`) derives the deadline at call time
as ``request_start + max(0, _LISTING_TIME_BUDGET_SECONDS -
_LABEL_PASS_RESERVE_SECONDS)``. Task 2.4 made both of those module-level
constants for exactly this reason, so test 1 `monkeypatch`es them down instead of
sleeping out the real 18 s window. The stub's own delay is **real** time
(`conftest.recorded_s3_calls`'s ``delay_seconds``), so it is kept small and
paired with a window smaller still: the ordering ``window << delay`` is what
makes the deadline bind on every host, fast or slow, without anything here
asserting how long anything took.

**Fixtures.** Task 1.2's shared infrastructure in this directory's `conftest.py`
— `label_world_env` for the moto backends and the two addresses,
`seed_label_world` to materialise a world, `recorded_s3_calls` as a pass-through
spy that sees calls from the client the post-pass builds itself (design §4.2),
`read_keys` for the read list, and `sidecar_key` so this file is not a second
definition of the Context_Sidecar's address (`LGR-cad8032-1`, **template /
ratified**). No `moto.mock_aws()` and no boto3 client mock
(`tests/unit/test_no_duplicate_mocking_prop.py`), and `AWS_REGION` is
deliberately not set (`LGR-3e45e65-1`, **template / ratified**).

The expected row key comes from `display_label_row_key`, task 1.3's single
definition read out of the **consumer** (`use-schemas.ts`'s `normalizeSchema`),
rather than being transcribed here (Requirement 4.7, `LGR-28b4d75-5`,
**template / ratified**).
"""

import json
import time
from typing import Any

from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    named_key,
    read_keys,
    recorded_s3_calls,
    seed_label_world,
    sidecar_key,
)
from tests.unit.lambdas.dashboard_api.test_schemas_listing_label_prop import display_label_row_key

# `label_world_env` is a fixture in this directory's `conftest.py`, so pytest
# resolves it by name in the signatures below rather than by import.


# ---------------------------------------------------------------------------
# Reaching the handler
# ---------------------------------------------------------------------------
# Lazily, for the reason `conftest.py`'s `_schemas_handler` gives:
# `handlers/common.py:29-37` calls `require_env` for nine table names at
# **import** time, so binding the module at module scope would decide this file's
# import-time env before `mock_lambda_env` has run.


def _handler():
    """The `handlers.schemas` module itself — test 1 patches two of its globals."""
    from handlers import schemas as schemas_handler

    return schemas_handler


def _get_schemas() -> dict[str, Any]:
    return _handler().get_schemas()


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])["schemas"]


def _uploaded_rows(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["schemaId"]: row for row in _rows(response) if row.get("source") == "uploaded"}


def _label_failures(response: dict[str, Any]) -> list[dict[str, str]]:
    """The `sourcesFailed` entries the Display_Label pass contributed.

    Filtered to ``source == "uploaded"`` rather than asserted over the whole list:
    the Glue and S3 Tables sources are enumerated in the same request and may
    legitimately report their own entries in a moto world, and neither is this
    task's subject.
    """
    body = json.loads(response["body"])
    return [entry for entry in body["sourcesFailed"] if entry.get("source") == "uploaded"]


# ---------------------------------------------------------------------------
# Test 1 — the deadline returns and reports
# ---------------------------------------------------------------------------
#: One stubbed sidecar read costs this much real time. Large **relative to the
#: window below**, small in absolute terms: the deadline has to bind on a fast
#: host, and the test still has to finish quickly.
_SLOW_READ_SECONDS = 0.5

#: The whole listing budget, shortened from 20 s. With the reserve at zero the
#: label pass's window is ``0.05 - (however long the enumeration took)``, so it is
#: **at most** 50 ms and may be 0 — either way it is an order of magnitude below
#: one stubbed read, which is what makes the deadline bind deterministically
#: without this test asserting a single duration. Both constants exist at module
#: level so this patch is possible (task 2.4).
_SHORT_BUDGET_SECONDS = 0.05
_NO_RESERVE_SECONDS = 0.0

#: Three Annotated_Schemas, every one carrying a truthy label, so that "no row
#: carries a label" below is a statement about the deadline rather than about the
#: world. The padded and non-ASCII values are there because a Display_Label is
#: stored verbatim; nothing in this file trims one.
_SLOW_WORLD = LabelWorld(
    schema_ids=("uploaded_schema_1700000000010", "customers_v3", "orders-2027"),
    sidecars={
        "uploaded_schema_1700000000010": {"display_name": "Customer Master"},
        "customers_v3": {"display_name": "顧客マスタ"},
        "orders-2027": {"display_name": "  padded label  ", "field_contexts": {"order_id": "the order's id"}},
    },
)


def test_the_deadline_returns_a_complete_listing_and_reports_the_shortfall(label_world_env, monkeypatch):
    """A slow sidecar read cannot stop `GET /api/schemas` answering (R1.9, R1.10).

    **Validates: Requirements 1.8, 1.9**

    Four assertions, and not one of them is a duration:

    1. `get_schemas` **returns** — reaching the next line at all is the claim,
       because the failure this guards against is a request that never comes back
       inside the Lambda's 25 s timeout.
    2. It returns a `200`. An overrun degrades the page's least important field;
       it is not an error response.
    3. Every seeded schema still has a row (Requirement 1.10: a label failure
       degrades a **label**, never a schema).
    4. The shortfall is **reported**, as one aggregated `sourcesFailed` entry, so
       "I could not look" is distinguishable from "nobody named this".
    """
    schemas_handler = _handler()
    monkeypatch.setattr(schemas_handler, "_LISTING_TIME_BUDGET_SECONDS", _SHORT_BUDGET_SECONDS)
    monkeypatch.setattr(schemas_handler, "_LABEL_PASS_RESERVE_SECONDS", _NO_RESERVE_SECONDS)

    seed_label_world(_SLOW_WORLD)
    with recorded_s3_calls(delay_seconds=_SLOW_READ_SECONDS) as calls:
        # Assertion 1 is the fact that this call returns. A pass that ignored its
        # deadline would sit here for `ceil(3/10) x 0.5 s` and, at the bound
        # Requirement 1.8 permits rather than this world's three schemas, past the
        # Lambda's own timeout.
        response = _get_schemas()

    # Not an assertion — cleanup. The abandoned reads are still asleep in the
    # pool's threads (`shutdown(wait=False, cancel_futures=True)`, which is the
    # point), and they will each call moto when they wake. Draining them here
    # keeps that off the moto backend's teardown rather than racing it.
    time.sleep(_SLOW_READ_SECONDS)

    rows = _uploaded_rows(response)

    # -- 3: a row for every schema -------------------------------------------
    missing = sorted(schema_id for schema_id in _SLOW_WORLD.schema_ids if schema_id not in rows)
    assert not missing, (
        f"Requirement 1.10: a Display_Label that could not be resolved degrades the **label**, "
        f"never the schema — but {missing} {'has' if len(missing) == 1 else 'have'} no row at all "
        f"after the deadline bound the label pass.\n"
        f"  seeded: {sorted(_SLOW_WORLD.schema_ids)}\n"
        f"  listed: {sorted(rows)}"
    )

    # -- the labels are the thing that was lost -------------------------------
    # Stated as a consequence rather than as a fifth claim: the window is at most
    # 50 ms and one read costs 500 ms, so no read can have been consumed. If this
    # ever fails, the deadline stopped binding — read it as the harness having
    # gone stale, not as the feature being wrong.
    label_key = display_label_row_key()
    labelled = sorted(schema_id for schema_id, row in rows.items() if label_key in row)
    assert not labelled, (
        f"the injected window is {_SHORT_BUDGET_SECONDS}s and one stubbed sidecar read costs "
        f"{_SLOW_READ_SECONDS}s, so no label should have been resolved — yet {labelled} carried "
        f"{label_key!r}. The deadline is no longer binding, so the rest of this test is no longer "
        f"about the deadline."
    )

    # -- 4: the shortfall is reported -----------------------------------------
    failures = _label_failures(response)
    assert len(failures) == 1, (
        f"Requirement 1.10: the abandoned Display_Label reads must be reported through "
        f"`sourcesFailed`, as **one** aggregated entry — `SchemaManagementPage.tsx:448-451` "
        f"renders one <li> per entry, so one entry per abandoned read would read as the schemas "
        f"page having broken. Got {len(failures)}: {failures}\n"
        f"  recorded S3 calls: "
        f"{', '.join(f'{op}({named_key(params)})' for op, params in calls) or '<none>'}"
    )
    entry = failures[0]
    annotated_count = len(_SLOW_WORLD.annotated_ids)
    assert str(annotated_count) in entry["scope"], (
        f"the one entry must name how many labels are missing, so a user can tell a shortfall of "
        f"3 from a shortfall of 300. Expected {annotated_count} in scope {entry['scope']!r}"
    )
    assert "deadline" in entry["error"], (
        f"the entry must say the reads were abandoned at the deadline rather than that they "
        f"failed, because those are different facts about the same missing label. Got "
        f"{entry['error']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Requirement 1.9's stated shape, and its exact read count
# ---------------------------------------------------------------------------
#: Requirement 1.9's world, verbatim: 1000 Uploaded_Schemas of which 100 are
#: Annotated_Schemas. Every tenth schema is annotated, which is a choice about
#: nothing — the ids are opaque and the listing enumerates them by key — beyond
#: making the annotated set trivially re-derivable in the assertion below.
_LARGE_WORLD_SCHEMAS = 1000
_LARGE_WORLD_ANNOTATED_EVERY = 10


def _requirement_1_9_world() -> LabelWorld:
    """1000 Uploaded_Schemas, 100 of them Annotated_Schemas.

    The ids are `uploaded_schema_{epoch_ms}`, the shape `_unique_schema_id`
    produces and the one D1 keeps opaque, so this world is the one the product
    actually creates at scale.
    """
    schema_ids = tuple(f"uploaded_schema_{1_700_000_000_000 + index}" for index in range(_LARGE_WORLD_SCHEMAS))
    sidecars = {
        schema_id: {"display_name": f"Schema {position}"}
        for position, schema_id in enumerate(schema_ids)
        if position % _LARGE_WORLD_ANNOTATED_EVERY == 0
    }
    return LabelWorld(schema_ids=schema_ids, sidecars=sidecars)


def test_the_requirement_1_9_shape_completes_with_exactly_one_read_per_annotated_schema(label_world_env):
    """1000 Uploaded_Schemas, 100 Annotated: it completes, with exactly 100 reads.

    **Validates: Requirements 1.8, 1.9**

    The bound, not the clock. Requirement 1.8 says the read count may not exceed
    the number of Annotated_Schemas and that a schema outside that set is not read
    at all; at this shape those two clauses plus "every annotated sidecar **is**
    read" pin the count to exactly 100 out of 1000 — 900 schemas that are never
    touched. That is the cost claim Requirement 1.9's latency claim rests on, and
    it is checkable without a stopwatch.

    A fast stub deliberately: the deadline is left at its real 18 s window, so
    this is the case where it does **not** bind, and what remains under
    observation is the read count and the labels arriving.
    """
    world = _requirement_1_9_world()
    assert len(world.annotated_ids) == 100, f"harness fault: built {len(world.annotated_ids)} Annotated_Schemas"

    seed_label_world(world)
    with recorded_s3_calls() as calls:
        response = _get_schemas()

    rows = _uploaded_rows(response)

    # -- it completes, with every row ----------------------------------------
    assert len(rows) == _LARGE_WORLD_SCHEMAS, (
        f"Requirement 1.9's shape must come back whole: expected {_LARGE_WORLD_SCHEMAS} uploaded rows, got {len(rows)}"
    )
    assert not _label_failures(response), (
        f"nothing about this world is unreadable and the deadline is not shortened, so the label "
        f"pass must resolve every label rather than report a shortfall: {_label_failures(response)}"
    )

    # -- exactly one read per Annotated_Schema, and none outside them ---------
    reads = read_keys(calls)
    expected = sorted(sidecar_key(schema_id) for schema_id in world.annotated_ids)
    assert sorted(reads) == expected, (
        f"Requirement 1.8: the listing must read exactly the {len(expected)} Annotated_Schema "
        f"sidecars and nothing else. It performed {len(reads)} per-object read(s).\n"
        f"  never read:  {sorted(set(expected) - set(reads))[:10]}\n"
        f"  read but not an Annotated_Schema's sidecar: {sorted(set(reads) - set(expected))[:10]}\n"
        f"  duplicate reads: {sorted({key for key in reads if reads.count(key) > 1})[:10]}"
    )
    assert len(reads) == len(world.annotated_ids), (
        f"Requirement 1.8 bounds the **count**, so a duplicated read counts twice: "
        f"{len(reads)} read(s) for {len(world.annotated_ids)} Annotated_Schema(s)"
    )

    # -- and the labels actually arrived -------------------------------------
    # Without this the read count could be met by a pass that read every sidecar
    # and dropped the results, which would satisfy the bound while leaving item
    # 1's defect exactly where it was.
    label_key = display_label_row_key()
    labelled = {schema_id for schema_id, row in rows.items() if label_key in row}
    assert labelled == set(world.expected_labels), (
        f"expected the {len(world.expected_labels)} stored Display_Labels to reach the response; "
        f"{len(labelled)} row(s) carried {label_key!r}.\n"
        f"  missing: {sorted(set(world.expected_labels) - labelled)[:10]}\n"
        f"  unexpected: {sorted(labelled - set(world.expected_labels))[:10]}"
    )
