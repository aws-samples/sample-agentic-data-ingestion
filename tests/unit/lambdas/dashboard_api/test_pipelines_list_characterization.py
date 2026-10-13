# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests for ``get_pipelines`` / ``enrich_pipeline`` — p47 task D5.

``pipelines_crud.py:190`` and ``:251``. R8.7 claims ``enrich_pipeline`` has no
covering tests; **the disk check confirms it** — zero references to the name
anywhere under ``tests/``, which is unsurprising since it is a closure defined
inside ``get_pipelines`` and cannot be imported. It is the one zero-coverage claim
in p47 that survived verification.

``get_pipelines`` itself is *not* uncovered, and this file extends rather than
duplicates what exists:

* ``test_read_path_errors.py::TestGetPipelineSchemaHistoryErrors`` (this
  directory) already pins that a throttled enrichment yields a minimal entry and
  a 200 page. It asserts two fields of that entry; it does not pin the stub's
  shape, and it drives the handler through ``MagicMock`` tables, so it cannot
  observe how DynamoDB treats a ``Limit`` beside a ``FilterExpression``.
* ``tests/contracts/api_responses/test_pipelines_response_contract.py`` asserts
  the six envelope keys are *present* and that five entry fields are strings.
* ``test_api_contract_conformance_prop.py`` validates the envelope against
  ``PAGINATED_PIPELINES_SCHEMA``.
* ``tests/integration/pipeline_flow/test_dashboard_api_integration.py`` drives the
  endpoint, outside the per-task gate.

None of them pin the entry's **key set**, the envelope's **arithmetic**, the
failure stub's **shape**, or the **read count** — which are the four things D6,
D7, D8, D9 and D10 can each break. So this file uses the shared moto fixtures and
D1's pass-through spy rather than scripted stubs, for the same reason D1 gave: a
stub returns what it was told to and can show no relationship between what was
examined and what matched.

WHAT IS BEING PINNED, AND WHERE THIS IS *NOT* CHARACTERIZATION OF CORRECTNESS
-----------------------------------------------------------------------------
Like D1's ``get_stats``, the headline defect here is **cost**: a full-table scan
per page (``:224``) plus five reads per pipeline on the page (``:255``, ``:258``,
``:263``, ``:280``, ``:309``), fanned out over ``ThreadPoolExecutor(max_workers=10)``
(``:357``). So the shape and the envelope are taken as the contract.

**Unlike ``get_stats``, three of the outputs are genuinely wrong**, and this file
says so at each site rather than ratifying them by assertion (task D5's own
instruction, and the reason D1's four findings changed D4's plan). Each is marked
``DEFECT`` in its test name and docstring, with the correct value stated:

1. **``mapping_count`` silently reads 0** once ≥50 newer state rows exist for the
   pipeline. ``Limit=50`` is applied before the ``FilterExpression`` (``:269``),
   ``LastEvaluatedKey`` is discarded, and 0 is indistinguishable from "no
   mappings". Measured: a pipeline with a ``schema_mapping`` row carrying
   ``mapping_count: 7`` reports ``0``.
   **FIXED by task D6** — ``get_pipeline_mapping_count`` now paginates
   newest-first under a page cap and a wall-clock deadline. The test below asserts
   ``7`` and keeps this history so the read's shape stays explicable.
2. **``all_files`` silently reads ``[]``** for the same reason (``:285``). The
   comment at ``:277`` names the mechanism — "Limit applies BEFORE
   FilterExpression, so we need a higher limit" — but a higher limit is a bigger
   window, not a bound. Measured: the same pipeline reports ``file_count: 3`` and
   ``all_files: []`` **in one response**, because ``get_pipeline_file_count``
   (``:97``) passes no ``Limit`` at all. That internal contradiction is the
   sharpest available evidence and is asserted as such.
   **FIXED by task D6** — ``get_pipeline_recent_files`` paginates the same way.
   The test below now asserts the two agree at three. Worth carrying forward:
   ``get_pipeline_file_count``, the read that supplied the oracle, follows no
   ``LastEvaluatedKey`` either, so it is only correct while a pipeline's partition
   fits in one 1 MB page. D6 recorded that in the handler docstring and left it —
   it is read #2, outside D6's three.
3. **``schema_version`` is not a version.** It was ``len(schema_history)``, and
   ``schema_history`` has already been sliced to the list view's ``limit=3``.
   Measured: a pipeline with 12 schema versions reports ``schema_version: 3`` while
   ``schema_history[-1]["version"]`` correctly reads ``12``. The truth is in the
   payload beside the misleading key.
   **FIXED by task D7** — ``schema_version`` is now that neighbour, so the two
   numbers cannot disagree. D5 also warned that D7's cap would destroy the oracle
   ("D7 must either keep a count read or state that the version numbers become
   relative") and D7 took the second arm, because a ``Select="COUNT"`` counts rows
   and this read de-duplicates by hash. §6 carries the measurement for both halves:
   the small-row case is fixed outright, and the multi-page case reports ordinals
   with ``schema_version`` and ``schema_history`` named in ``partial_fields``.

A fourth finding is about ordering rather than a value, and is pinned in section 3:
an unenriched stub carries no ``updated_at``, so the re-sort at ``:378`` — whose
comment reads "Sort to maintain original order" — moves it to the **end** of the
page. R8.5's isolation holds (200, entry present); the position does not.

WHY EACH SECTION EXISTS, PER LATER TASK
---------------------------------------
* §1 **the 22-key entry shape**, ``==`` not ``issubset``. D6 caches three reads and
  D8 bounds the page read; either could drop or rename a key. Following D1's
  tripwire convention: an added or removed key must surface here and be ratified.
  **The task text said 20 keys; the source said 21** when this file was written —
  re-derived from the literal rather than trusted. **D6 made it 22**, adding
  ``partial_fields``, and this is the ratification: the tripwire fired and the
  addition was accepted here rather than by loosening the assertion.
* §2 **the envelope**, six keys plus the arithmetic. D8 rewrites how the page is
  selected, so this is the contract that says the client's view survived. The
  fields pinned are the ones ``use-pipelines.ts:120-126`` actually reads — all six,
  each behind a fallback, which is why a *missing* key degrades silently in the UI
  and must fail here instead. **D8 made it seven**, adding ``partial_counts``, and
  this file is that ratification: every arithmetic assertion is unchanged, because
  D8 marked the values rather than re-deriving them.
* §3 **the failure stub**, six keys. D8 and D9 both touch the fan-out. R8.5.
* §4 **five reads, counted.** D6 caches three of them, D7 caps the schema-history
  walk. Without a current number those changes are unmeasurable. **D6 moved the
  warm figure**: a second request in one process now costs the page scan and
  nothing else, where it used to cost the scan plus three.
* §5 **``Limit`` before ``Filter``**, enforced by the query engine rather than by a
  stub. This is the same filter-after-read semantics D1 measured for stats. **D6
  did not change those semantics** — a first page of 50 still examines 50 rows and
  matches none. What changed is that the read no longer stops there.
* §6 **the schema-history page walk.** D5 measured 4 pages to satisfy ``limit=3``,
  bounded at 10 (``max_pages``). **D7 capped it and the section now measures 1**, with
  D5's figure kept beside it as a counterfactual against the *unbounded* entry point,
  which R8.3 and design §2.6 both say to leave alone. The section also carries the
  version-number decision the cap forced, and the measurement that ruled out a count
  read.
* §7 **what a stopped walk reports** — added by D6, the other half of its own fix.
  The two reads it made correct are now *paginated*, so they need a bound, and a
  bound that reports ``0`` unlabelled would be the same defect one layer along.
  Both bounds (deadline, page cap) and the not-cached rule are pinned here.
  **D7 extended the same marker to two more keys** (``schema_version``,
  ``schema_history``) for the same reason, and its cases live in §6 beside the walk
  they describe rather than here.
* §9 **the fan-out's own bound** — added by D9, the last of the four. Each read was
  bounded and their *sum* was not, so ``as_completed`` now carries the enrichment
  deadline's remaining window. What is pinned: the page still answers with every
  member, an abandoned future ships the same stub a failed read does (one literal,
  compared directly), ``page_order`` covers it, the third marker names it, and the
  executor is **not** a context manager — ``shutdown(wait=True)`` would block until
  the overrun the deadline exists to prevent.
* §8 **the page read's own bound** — added by D8, the same argument one level up.
  The page read now follows ``LastEvaluatedKey``, so it needs a page cap and a
  deadline, and the deadline is a *share* of the budget rather than enrichment's own
  so a slow walk cannot starve the fan-out. The envelope marker, the two caps being
  separate constants, the two markers' independence, and the partition property
  across a multi-scan-page table are pinned here. D5's two page-read DEFECTs
  (silent truncation, stubs sorting last) are **fixed** and their tests now assert
  the fixed behaviour in §4 and §3 respectively, with D5's measured figures kept in
  the docstrings.

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` from ``tests/fixtures/conftest.py``.
No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``).

Table names are set at **module scope**: ``handlers/common.py:29-37`` resolves nine
tables through ``require_env`` at **import** time, before any fixture runs. Same
reason D1's file does it, and ``test_schema_delete_reference_check_prop.py:165`` in
this directory. ``AWS_REGION`` is deliberately unset (``LGR-3e45e65-1``) and moot,
since every table is bound onto the handler's namespace as a fixture handle.

**The enrichment cache is a process-global dict, and clearing it is load-bearing.**
``_get_cached`` / ``_set_cache`` (``pipelines_logs.py:318-333``) is a module-level
``dict`` with a 300 s TTL — *not* the DynamoDB cache ``handlers/metrics.py`` owns.
It therefore survives across tests in one pytest process, so a second test reusing
a ``pipeline_id`` would silently measure 3 reads instead of 5. The fixture clears
it on both sides. Worth stating for D6: R8.2 asks for "the same mechanism as #1 and
#2", and that mechanism is per-container in-process memory with no size bound,
which is a different staleness and eviction story from R7.2's DynamoDB rows.
**D6 used it for all three remaining reads**, so the fixture's clearing is now
load-bearing for five cache keys per pipeline rather than two, and a warm second
request issues no per-pipeline read at all.

``get_pipelines`` writes no audit entry — verified by the spy, which records no
``PutItem`` — so unlike the contract test this file does not stub
``write_audit_entry``.

**Validates: Requirements 8.7**
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.client import BaseClient

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing the handler
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

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
from handlers import pipelines_crud as crud
from handlers import pipelines_logs as logs_handler

STATE_TABLE = "agentic_data_ingestion_state"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
EXECUTIONS_TABLE = "agentic_data_ingestion_executions"

#: The handler's own source, read once for the three assertions that pin a literal or
#: a comment. Source text is the only instrument for a comment (R8.4) and the same one
#: ``test_sfn_states_contract.py`` uses for its own source-level claims.
HANDLER_SOURCE: str = (
    pathlib.Path(_LAMBDA_DIR).resolve().joinpath("handlers", "pipelines_crud.py").read_text(encoding="utf-8")
)

#: One enriched entry's keys, enumerated from the ``return`` literal at
#: ``pipelines_crud.py:328-353`` in source order.
#:
#: **Twenty-two.** D5 re-derived twenty-one against a task text that said twenty,
#: because a key count is exactly the figure that drifts — the same correction D1
#: had to make for ``get_stats``. **D6 added the twenty-second**,
#: ``partial_fields``, and this is where that addition is ratified: the ``==``
#: tripwire fired, the key was accepted, and the count moved. It is deliberately
#: last, matching its position in the return literal.
ENRICHED_KEYS: tuple[str, ...] = (
    "pipeline_key",
    "pipeline_id",
    "pipeline_name",
    "target_schema_name",
    "ai_model",
    "pipeline_description",
    "status",
    "pipeline_status",
    "running_count",
    "approval_count",
    "created_at",
    "updated_at",
    "file_count",
    "mapping_count",
    "all_files",
    "schema_version",
    "schema_history",
    "current_schema_hash",
    "glue_job_metadata",
    "source_type",
    "source_config",
    "partial_fields",
)

#: The unenriched stub's keys, from the literal at ``:366-375``. Six, and a strict
#: subset of ``ENRICHED_KEYS`` — asserted, not assumed, in ``TestFailureIsolation``.
STUB_KEYS: tuple[str, ...] = (
    "pipeline_key",
    "pipeline_id",
    "pipeline_name",
    "status",
    "schema_version",
    "schema_history",
)

#: The envelope's keys, from ``create_response``'s payload at ``:380-390``.
#: The first six are read by ``fetchPipelines`` (``use-pipelines.ts:120-126``),
#: each behind a ``??`` fallback — so a dropped key does not break the UI, it makes
#: it quietly wrong. That is why this is asserted with ``==`` here.
#:
#: **Seven since task D8**, which added ``partial_counts``: the page read now
#: paginates under a page cap and a deadline, and a walk stopped by either returns
#: a *prefix* of the filtered set, which makes ``total_count`` a floor and the page
#: a selection over a subset. This is the ratification of that addition — the
#: tripwire fired and the key was accepted here rather than the assertion being
#: loosened, exactly as D6's ``partial_fields`` was accepted in ``ENRICHED_KEYS``.
#: Nothing renders it: ``fetchPipelines`` drops unknown keys, so it is
#: additive-safe on the wire, and ``PAGINATED_PIPELINES_SCHEMA``
#: (``tests/contracts/api_responses/schemas.py:104``) sets no
#: ``additionalProperties: false``, so the conformance property still validates.
#:
#: **Eight since task D9**, which added ``enrichment_abandoned``: the fan-out now
#: has a deadline of its own, and a pipeline whose future was outstanding when it
#: fired ships as the same six-key stub an enrichment *failure* produces. Neither
#: existing marker can say so — ``partial_counts`` is about the page set being a
#: prefix (it is not; the walk completed) and ``partial_fields`` names keys **of an
#: entry** whose value is a floor (an abandoned entry omits those keys entirely).
#: So it is a third condition with a third key, ratified here the same way: the
#: tripwire fired and the key was accepted rather than the assertion loosened. It
#: holds ``pipeline_id``\\ s where the other two hold key *names*, which is what
#: keeps the three vocabularies unmistakable for each other.
ENVELOPE_KEYS: tuple[str, ...] = (
    "pipelines",
    "total_count",
    "page",
    "page_size",
    "total_pages",
    "has_more",
    "partial_counts",
    "enrichment_abandoned",
)

#: Six of the 22 enriched keys are read **nowhere** in ``dashboard-react/src``.
#: Verified by grep across the whole source tree, tests excluded. Recorded as an
#: observation for D6/D8 rather than as a licence to remove them: two of them
#: (``running_count``, ``approval_count``) are the *derivation* behind ``status``,
#: so read #5 earns its place even though its two counts do not travel.
#: ``glue_job_metadata`` is an unbounded map on a hot list payload.
#:
#: ``partial_fields`` joins the list as D6's own addition, and knowingly:
#: ``normalizePipeline`` (``use-pipelines.ts:64-99``) rebuilds each entry field by
#: field and drops unknown keys, so the marker is additive-safe on the wire and
#: invisible in the UI. Same position D4's ``partial_counts`` is in against
#: ``fetchStats``, and the same named frontend follow-up.
KEYS_NO_FRONTEND_READER: tuple[str, ...] = (
    "pipeline_status",
    "running_count",
    "approval_count",
    "current_schema_hash",
    "glue_job_metadata",
    "partial_fields",
)


# ---------------------------------------------------------------------------
# Fixture: the three tables the listing reads, bound onto the handler namespace
# ---------------------------------------------------------------------------
@pytest.fixture
def list_tables(mock_dynamodb_tables, monkeypatch) -> Iterator[dict[str, Any]]:
    """Bind shared moto handles for the three tables the listing reads.

    ``handlers.pipelines_crud`` **and** ``handlers.common`` are both patched, for
    the reason D1's file records: ``pipelines_crud`` does
    ``from .common import ... state_table``, which copies the reference into its own
    namespace, so patching ``common`` alone would patch a name the handler never
    reads. Both, so a future refactor to either access shape cannot silently
    un-test this file.

    The process-global enrichment cache is cleared on **both** sides. See the
    module docstring: it is a module-level dict, not a per-request or per-table
    thing, so a leak in either direction makes the read counts in §4 meaningless.
    """
    logs_handler._cache.clear()
    handles = {
        "state_table": mock_dynamodb_tables[STATE_TABLE],
        "pipelines_table": mock_dynamodb_tables[PIPELINES_TABLE],
        "executions_table": mock_dynamodb_tables[EXECUTIONS_TABLE],
    }
    for attribute, table in handles.items():
        monkeypatch.setattr(crud, attribute, table)
        monkeypatch.setattr(common_handler, attribute, table)
    yield handles
    logs_handler._cache.clear()


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------
def pipeline_record(pipeline_id: str, **extra: Any) -> dict[str, Any]:
    """One pipelines-table item. ``updated_at`` defaults so the sort is defined.

    ``get_pipelines`` sorts on ``updated_at`` (``:238``) and re-sorts the enriched
    page on it again (``:378``), so a record without one participates in neither
    ordering in a defined way. Every live writer sets it.
    """
    record: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "pipeline_name": f"Pipeline {pipeline_id}",
        "status": "active",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    record.update(extra)
    return record


def state_row(run_id: str, step_name: str, pipeline_id: str, timestamp: str, **extra: Any) -> dict[str, Any]:
    """One state row, keyed (``run_id`` pk, ``step_name`` sk).

    ``timestamp`` is **required**, not defaulted, and that is deliberate: every one
    of the four state reads goes through ``PipelineIndex``, whose sort key is
    ``timestamp`` (``tests/fixtures/conftest.py:48``), so the index is **sparse** —
    a row without one is invisible to all four. D4 lost eleven assertions to
    exactly that fixture artifact on the sibling ``step_name-timestamp-index``.
    Making it a positional argument means the trap cannot be fallen into silently.

    The key shape also means one ``run_id`` carries at most one row per step, so
    seeding more matching rows means varying ``run_id``, not ``step_name``.
    """
    row: dict[str, Any] = {
        "run_id": run_id,
        "step_name": step_name,
        "pipeline_id": pipeline_id,
        "timestamp": timestamp,
    }
    row.update(extra)
    return row


def seed(table: Any, rows: list[dict[str, Any]]) -> None:
    """Write rows through a batch writer."""
    with table.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=row)


def seed_fully_enriched_pipeline(tables: dict[str, Any], pipeline_id: str = "pipe-1") -> None:
    """One pipeline with a non-default value for every enrichment-derived key.

    Every value is distinct from the ``.get(key, default)`` fallback beside it in
    the return literal, so a change that started returning the default instead of
    the read value is visible rather than plausible.
    """
    tables["pipelines_table"].put_item(
        Item=pipeline_record(
            pipeline_id,
            pipeline_name="Customer Data",
            target_schema_name="customer_schema",
            ai_model="anthropic.claude-3-haiku-20240307-v1:0",
            pipeline_description="A described pipeline",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-05T00:00:00Z",
            glue_job_metadata={"job_name": "adi-customer"},
            source_type="JDBC",
            source_config={"jdbc_table": "customers"},
        )
    )
    seed(
        tables["state_table"],
        [
            state_row(
                "run-1",
                "schema_extract",
                pipeline_id,
                "2026-01-02T00:00:00Z",
                schema_hash="hash-aaa",
                json_s3_uri="s3://bucket/schemas/first.json",
                extracted_schema='{"properties": {"id": {}}}',
            ),
            state_row(
                "run-2",
                "schema_extract",
                pipeline_id,
                "2026-01-03T00:00:00Z",
                schema_hash="hash-bbb",
                json_s3_uri="s3://bucket/schemas/second.json",
            ),
            state_row(
                "run-1",
                "file_validation",
                pipeline_id,
                "2026-01-02T00:01:00Z",
                status="success",
                original_s3_uri="s3://bucket/incoming/first.csv",
            ),
            state_row(
                "run-2",
                "file_validation",
                pipeline_id,
                "2026-01-03T00:01:00Z",
                status="success",
                original_s3_uri="s3://bucket/incoming/second.csv",
            ),
            state_row("run-1", "schema_mapping", pipeline_id, "2026-01-02T00:02:00Z", mapping_count=7),
        ],
    )
    tables["executions_table"].put_item(
        Item={
            "pipeline_id": pipeline_id,
            "run_id": "run-2",
            "status": "RUNNING",
            "started_at": "2026-01-03T00:00:00Z",
        }
    )


def call_pipelines(**kwargs: Any) -> tuple[int, dict[str, Any]]:
    """Invoke ``get_pipelines`` and return ``(statusCode, decoded body)``.

    The body is a JSON **string** in the envelope (``create_response``,
    ``shared/response_utils.py``), so every assertion below is against what a
    client receives rather than against the handler's internal dict.
    """
    response = crud.get_pipelines(**kwargs)
    return response["statusCode"], json.loads(response["body"])


def one_entry(**kwargs: Any) -> dict[str, Any]:
    """The single enriched entry of a one-pipeline page. Fails loudly otherwise."""
    status, body = call_pipelines(**kwargs)
    assert status == 200, body
    assert len(body["pipelines"]) == 1, f"expected exactly one entry, got {len(body['pipelines'])}"
    return body["pipelines"][0]


# ---------------------------------------------------------------------------
# A pass-through spy over the DynamoDB wire calls, and read classifiers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def recorded_dynamodb_calls() -> Iterator[list[tuple[str, dict[str, Any], Any]]]:
    """Record ``(operation, params, response)`` for every DynamoDB call in the block.

    A **pass-through spy, not a mock** — the wrapper records and then calls the
    original, so every ``Count`` / ``ScannedCount`` / ``LastEvaluatedKey`` below is
    moto's own rather than something this file decided. Reused from D1
    (``test_stats_characterization.py``) rather than reinvented; it wraps
    ``BaseClient._make_api_call``, above moto's ``before-send`` stubber and above
    botocore's retry handler.

    D4's finding applies and shapes §4's classifiers: this layer sits above **both**
    of boto3's dynamodb translations, including the ``ConditionExpressionBuilder``
    registered on ``before-parameter-build``. A caller passing ``Key``/``Attr``
    objects therefore yields **no** ``ExpressionAttributeValues`` here, so
    conditions are inspected structurally through ``get_expression()`` instead —
    which is the better assertion anyway, since ``:v0`` and friends are boto3's own
    numbering and not part of any contract.

    The listing fans out over a ``ThreadPoolExecutor``, so this patches a shared
    class attribute observed from several threads. It is safe for the same reason
    D1's is: the list is only appended to, under the GIL, and every assertion below
    is about the *multiset* of calls rather than their interleaving. Tests that need
    a per-pipeline figure seed exactly one pipeline.
    """
    recorded: list[tuple[str, dict[str, Any], Any]] = []
    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        if self.meta.service_model.service_name != "dynamodb":
            return original(self, operation_name, api_params)
        response = original(self, operation_name, api_params)
        recorded.append((operation_name, dict(api_params), response))
        return response

    BaseClient._make_api_call = recording
    try:
        yield recorded
    finally:
        BaseClient._make_api_call = original


def condition_parts(condition: Any) -> tuple[str, str, Any]:
    """``(attribute name, operator, compared value)`` of one boto3 condition.

    D4's helper, reused. Only meaningful for a single comparison — the two
    ``FilterExpression``\\ s built with ``&`` are inspected by their string form
    instead, since a conjunction has conditions rather than values in its operands.
    """
    expression = condition.get_expression()
    attribute, value = expression["values"]
    return attribute.name, expression["operator"], value


def queried_pipeline_id(params: dict[str, Any]) -> str | None:
    """The ``pipeline_id`` a ``PipelineIndex`` query keyed on, or ``None``."""
    key_condition = params.get("KeyConditionExpression")
    if key_condition is None:
        return None
    name, operator, value = condition_parts(key_condition)
    return value if name == "pipeline_id" and operator == "=" else None


def classify_reads(recorded: list[tuple[str, dict[str, Any], Any]]) -> dict[str, list[tuple[dict, Any]]]:
    """Group the recorded calls into the six reads one page issues.

    Each of the five per-pipeline reads is distinguishable by its parameters alone,
    which is what makes the read count in §4 an assertion about *which* reads ran
    rather than only how many:

    ===================  ==========================================================
    ``page_scan``        ``Scan`` on the pipelines table (``:224``)
    ``schema_history``   ``PipelineIndex``, no ``Limit``, no ``Select`` (``:143``)
    ``file_count``       ``PipelineIndex``, ``Select="COUNT"`` (``:97``)
    ``mapping_count``    ``PipelineIndex``, ``Limit=50``, ``ProjectionExpression`` (``:263``)
    ``recent_files``     ``PipelineIndex``, ``Limit=50``, no projection (``:280``)
    ``executions``       ``Query`` on the executions base table, ``Limit=5`` (``:309``)
    ===================  ==========================================================

    Anything unclassified lands in ``other`` and is asserted empty, so a sixth read
    appearing cannot hide inside a total.
    """
    groups: dict[str, list[tuple[dict, Any]]] = {
        "page_scan": [],
        "schema_history": [],
        "file_count": [],
        "mapping_count": [],
        "recent_files": [],
        "executions": [],
        "other": [],
    }
    for operation, params, response in recorded:
        table = params.get("TableName")
        entry = (params, response)
        if operation == "Scan" and table == PIPELINES_TABLE:
            groups["page_scan"].append(entry)
        elif operation == "Query" and table == EXECUTIONS_TABLE:
            groups["executions"].append(entry)
        elif operation == "Query" and table == STATE_TABLE and params.get("IndexName") == "PipelineIndex":
            if params.get("Select") == "COUNT":
                groups["file_count"].append(entry)
            elif params.get("Limit") is None:
                groups["schema_history"].append(entry)
            elif "ProjectionExpression" in params:
                groups["mapping_count"].append(entry)
            else:
                groups["recent_files"].append(entry)
        else:
            groups["other"].append(entry)
    return groups


# ═══════════════════════════════════════════════════════════════════════════
# 1. The enrichment return shape — exactly 21 keys
# ═══════════════════════════════════════════════════════════════════════════
class TestEnrichedEntryShape:
    """``enrich_pipeline``'s return literal (``:328-353``), key by key.

    D6 caches three of the five reads and D8 bounds the page read. Either can drop
    a key it stops needing or add one it starts producing, and either would pass
    every existing test on disk — the contract test asserts five fields are
    *present*, the conformance property validates the envelope. So the key set is
    pinned with ``==``, following D1's tripwire convention.
    """

    def test_an_entry_carries_exactly_the_twenty_two_keys(self, list_tables):
        """``==`` on the key set, not ``issubset``.

        D5 wrote this at 21 against a task text that said 20, re-derived from the
        literal rather than trusted. **D6 made it 22** by adding ``partial_fields``,
        which is exactly what the tripwire is for: it fired, and the addition was
        ratified by amending this assertion and its docstring rather than by
        loosening it to ``issubset``, which would have retired the tripwire instead
        of answering it. ``partial_fields`` is last, matching the literal's order.
        """
        seed_fully_enriched_pipeline(list_tables)

        entry = one_entry()

        assert set(entry) == set(ENRICHED_KEYS), (
            f"the enriched entry's key set changed: "
            f"missing={sorted(set(ENRICHED_KEYS) - set(entry))} "
            f"unexpected={sorted(set(entry) - set(ENRICHED_KEYS))}"
        )
        assert len(ENRICHED_KEYS) == 22
        assert list(entry) == list(ENRICHED_KEYS), "the keys are emitted in the literal's source order"

    def test_every_enrichment_derived_value_arrives_rather_than_its_default(self, list_tables):
        """Each key reads its seeded value, not the ``.get(key, default)`` fallback.

        The population gives every key a value distinct from its own default, so a
        change that started returning the default — a dropped read, a cache miss
        treated as empty — is visible rather than plausible. This is the assertion
        D6's three new caches must keep true on a **miss**; its counterpart on a hit
        is D6's own to write.
        """
        seed_fully_enriched_pipeline(list_tables, "pipe-1")

        entry = one_entry()

        assert entry["pipeline_key"] == "pipe-1"
        assert entry["pipeline_id"] == "pipe-1"
        assert entry["pipeline_name"] == "Customer Data"
        assert entry["target_schema_name"] == "customer_schema"
        assert entry["ai_model"] == "anthropic.claude-3-haiku-20240307-v1:0"
        assert entry["pipeline_description"] == "A described pipeline"
        assert entry["pipeline_status"] == "active"
        assert entry["created_at"] == "2026-01-01T00:00:00Z"
        assert entry["updated_at"] == "2026-01-05T00:00:00Z"
        assert entry["glue_job_metadata"] == {"job_name": "adi-customer"}
        assert entry["source_type"] == "JDBC"
        assert entry["source_config"] == {"jdbc_table": "customers"}
        # Read-derived, one per enrichment read.
        assert entry["file_count"] == 2
        assert entry["mapping_count"] == 7
        assert [f["file_key"] for f in entry["all_files"]] == ["second.csv", "first.csv"]
        assert [h["hash"] for h in entry["schema_history"]] == ["hash-aaa", "hash-bbb"]
        assert entry["current_schema_hash"] == "hash-bbb"
        assert entry["running_count"] == 1
        assert entry["approval_count"] == 0
        # D6: every read completed inside its bound, so nothing is a floor. This is
        # the empty-marker half of the contract; the non-empty half is asserted by
        # `TestPartialFields`.
        assert entry["partial_fields"] == []

    def test_a_bare_pipeline_record_gets_every_default(self, list_tables):
        """The other half of the same contract: what a record with nothing set reads.

        Six keys default to ``""``, two to a container, one to ``"active"``, one to
        ``"S3_UPLOAD"``, ``current_schema_hash`` to ``None``, and the four
        read-derived numbers to ``0`` / ``[]``. The ``S3_UPLOAD`` and ``{}`` pair is
        P17 Requirement 6.7's backward-compatibility default (comment at
        ``:348-350``) and is the one default with a stated requirement behind it.

        ``None`` for ``current_schema_hash`` is pinned as ``None`` specifically:
        ``""`` would serialise differently and the frontend does not read this key
        at all, so nothing downstream would notice a change.
        """
        list_tables["pipelines_table"].put_item(Item={"pipeline_id": "bare"})

        entry = one_entry()

        assert entry["pipeline_name"] == "bare", "the name falls back to the id, not to an empty string"
        assert entry["target_schema_name"] == ""
        assert entry["ai_model"] == ""
        assert entry["pipeline_description"] == ""
        assert entry["created_at"] == ""
        assert entry["updated_at"] == ""
        assert entry["status"] == "active"
        assert entry["pipeline_status"] == "active"
        assert entry["glue_job_metadata"] == {}
        assert entry["source_type"] == "S3_UPLOAD"
        assert entry["source_config"] == {}
        assert entry["current_schema_hash"] is None
        assert (entry["file_count"], entry["mapping_count"]) == (0, 0)
        assert (entry["all_files"], entry["schema_history"]) == ([], [])
        assert entry["schema_version"] == 0
        # D6: an empty partition is read to its end, so these zeros are **totals**,
        # not floors — which is the distinction `partial_fields` exists to carry.
        assert entry["partial_fields"] == []

    def test_status_is_the_derived_live_status_and_pipeline_status_is_the_record(self, list_tables):
        """Two keys, two meanings — and ``status`` is the *derived* one.

        ``status`` is ``live_status`` (``:305``, ``:322``, ``:324``), overwritten from
        the executions read; ``pipeline_status`` is the record's own value (``:336``).
        A pipeline whose record says ``active`` reports ``status: "running"`` while a
        run is in flight. ``PipelineListPage.tsx:52`` renders ``item.status``, so the
        derived one is the one on screen, which is why collapsing the two would be a
        visible regression and not a tidy-up.
        """
        seed_fully_enriched_pipeline(list_tables, "pipe-1")

        entry = one_entry()

        assert entry["status"] == "running"
        assert entry["pipeline_status"] == "active"
        assert entry["status"] != entry["pipeline_status"]

    def test_an_awaiting_approval_run_outranks_a_running_one(self, list_tables):
        """``approval_required`` wins over ``running`` (``:321-324``), in that order.

        Both counts are non-zero in the population, so the precedence is pinned
        rather than inferred from a single-state case. ``formatting.ts:48`` maps this
        literal, so the string is a wire value.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        for run_id, item in (
            ("run-1", {"status": "RUNNING"}),
            ("run-2", {"status": "WAITING_APPROVAL"}),
        ):
            list_tables["executions_table"].put_item(
                Item={"pipeline_id": "pipe-1", "run_id": run_id, "started_at": "2026-01-01", **item}
            )

        entry = one_entry()

        assert entry["running_count"] == 1
        assert entry["approval_count"] == 1
        assert entry["status"] == "approval_required"

    def test_a_pending_approval_id_counts_as_awaiting_approval_whatever_the_status(self, list_tables):
        """``ex.get("pending_approval_id")`` is the second half of the ``or`` at ``:316``.

        A run with a pending approval id and *any* status counts as awaiting
        approval — including a status the branch below would otherwise call
        ``RUNNING``, which is why the two arms are pinned separately.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        list_tables["executions_table"].put_item(
            Item={
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "status": "RUNNING",
                "pending_approval_id": "appr-1",
                "started_at": "2026-01-01",
            }
        )

        entry = one_entry()

        assert entry["approval_count"] == 1
        assert entry["running_count"] == 0, "the elif at :318 is not reached once the id matched"
        assert entry["status"] == "approval_required"

    def test_an_all_files_element_carries_exactly_three_keys(self, list_tables):
        """The nested shape at ``:293``, pinned on its own.

        ``use-pipelines.ts:81`` passes ``all_files`` through **unnormalised** —
        ``(raw.all_files as Pipeline['all_files']) ?? []`` — so unlike
        ``schema_history`` there is no mapping layer to absorb a rename. A changed
        key here reaches the component as ``undefined``.

        ``status`` is a **literal** ``"success"`` rather than the row's own value,
        which is safe only because the query filters on exactly that
        (``:283``). D6 caching this read must not decouple the two.
        """
        seed_fully_enriched_pipeline(list_tables)

        entry = one_entry()

        assert entry["all_files"], "no files to inspect"
        for element in entry["all_files"]:
            assert set(element) == {"file_key", "processed_at", "status"}
            assert element["status"] == "success"
        newest = entry["all_files"][0]
        assert newest["file_key"] == "second.csv", "the basename, not the whole s3 uri"
        assert newest["processed_at"] == "2026-01-03T00:01:00Z"

    def test_a_schema_history_element_carries_exactly_six_keys(self, list_tables):
        """The nested shape from ``get_pipeline_schema_history`` (``:162-170``, ``:184``).

        Five keys are built in the literal and ``version`` is assigned afterwards, so
        the sixth is easy to lose in a refactor of the version numbering D7 touches.
        ``use-pipelines.ts:15-42`` reads ``version``, ``timestamp``, ``hash``,
        ``extracted_schema`` and ``file_name`` — five of the six — so this is the
        nested shape with the most frontend surface.
        """
        seed_fully_enriched_pipeline(list_tables)

        entry = one_entry()

        assert entry["schema_history"], "no schema history to inspect"
        for element in entry["schema_history"]:
            assert set(element) == {
                "hash",
                "timestamp",
                "extracted_schema",
                "file_name",
                "json_s3_uri",
                "version",
            }
        assert entry["schema_history"][0]["file_name"] == "first.json", "the basename of json_s3_uri"
        assert entry["schema_history"][0]["version"] == 1
        assert [h["timestamp"] for h in entry["schema_history"]] == [
            "2026-01-02T00:00:00Z",
            "2026-01-03T00:00:00Z",
        ], "oldest to newest (:173), which is why current_schema_hash reads [-1]"

    def test_a_schema_history_entry_with_no_json_uri_reports_unknown(self, list_tables):
        """``file_name`` falls back to the literal ``"unknown"`` (``:161``), not to ``""``.

        Pinned because ``use-pipelines.ts:60`` uses ``file_name`` as the
        ``source_schema`` fallback when ``extracted_schema`` will not parse, so this
        string is rendered.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [state_row("run-1", "schema_extract", "pipe-1", "2026-01-02T00:00:00Z", schema_hash="h1")],
        )

        entry = one_entry()

        assert entry["schema_history"][0]["file_name"] == "unknown"
        assert entry["schema_history"][0]["json_s3_uri"] == ""
        assert entry["schema_history"][0]["extracted_schema"] == ""

    def test_six_of_the_twenty_two_keys_have_no_frontend_reader(self, list_tables):
        """An **observation**, asserted so it stays true or gets noticed.

        ``pipeline_status``, ``running_count``, ``approval_count``,
        ``current_schema_hash``, ``glue_job_metadata`` and — since D6 —
        ``partial_fields`` appear nowhere in ``dashboard-react/src`` outside tests.
        This is recorded rather than acted on: removing a key from a public response
        is a contract change, and two of the six are the derivation behind
        ``status``, which *is* rendered.

        It is asserted from the response side only — that these keys are present —
        because the grep that established the other half is not something a unit
        test should re-run. The figure and its date live in the module docstring.
        """
        seed_fully_enriched_pipeline(list_tables)

        entry = one_entry()

        for key in KEYS_NO_FRONTEND_READER:
            assert key in entry, f"{key} left the payload — if deliberate, this test is the ratification point"
        assert set(KEYS_NO_FRONTEND_READER) < set(ENRICHED_KEYS)


# ═══════════════════════════════════════════════════════════════════════════
# 2. The pagination envelope — D8's contract
# ═══════════════════════════════════════════════════════════════════════════
class TestPaginationEnvelope:
    """The six keys around the page, and the arithmetic behind each.

    **D8 rewrites how the page is selected**, so this is the contract that says the
    rewrite preserved the client's view. Every one of the six is read by
    ``fetchPipelines`` (``use-pipelines.ts:120-126``) — and every one is read behind
    a ``??`` fallback, so a dropped key does not error in the UI, it makes the
    pager quietly wrong. That is why presence is asserted with ``==`` and each
    value's derivation is asserted separately.

    ``total_count`` is the figure with the most at risk. Today it is
    ``len(all_pipelines)`` **after** the status filter and the search
    (``:240``), which is only knowable because the whole table was read. R8.1 asks
    for the opposite, and risk 5.6 anticipates that a bounded read may not be able
    to produce an exact total — so if D8 cannot, this is where it has to say so.
    """

    @staticmethod
    def _seed_seven_active_and_one_archived(tables: dict[str, Any]) -> None:
        """Seven active pipelines with distinct ``updated_at``, plus one archived.

        Seven over a page size of three gives three pages with a short last page, so
        ``total_pages``' ceiling division and ``has_more``'s boundary are both
        exercised rather than assumed. The archived record is the near-miss for the
        default filter.
        """
        for n in range(7):
            tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z")
            )
        tables["pipelines_table"].put_item(
            Item=pipeline_record("pipe-archived", status="archived", updated_at="2026-02-01T00:00:00Z")
        )

    def test_the_envelope_carries_exactly_the_eight_keys(self, list_tables):
        """``==``, for the same tripwire reason as the entry's key set.

        **Six when D5 wrote this; seven since D8 added ``partial_counts``; eight
        since D9 added ``enrichment_abandoned``.** The rename is deliberate each
        time — a test whose name states a count must move with the count, or the
        next reader trusts the name over the tuple.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        status, body = call_pipelines()

        assert status == 200
        assert set(body) == set(ENVELOPE_KEYS), (
            f"the envelope's key set changed: "
            f"missing={sorted(set(ENVELOPE_KEYS) - set(body))} "
            f"unexpected={sorted(set(body) - set(ENVELOPE_KEYS))}"
        )
        assert body["partial_counts"] == [], "a table that fits in one page read completely"
        assert body["enrichment_abandoned"] == [], "D9: every future was consumed inside the fan-out deadline"

    def test_the_envelope_field_types_are_what_the_frontend_casts_to(self, list_tables):
        """``use-pipelines.ts`` casts four to ``number`` and one to ``boolean``.

        A count arriving as a ``Decimal`` or a string would satisfy the key-set
        assertion above and break the pager silently, since the cast is unchecked.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        _, body = call_pipelines()

        assert isinstance(body["pipelines"], list)
        for key in ("total_count", "page", "page_size", "total_pages"):
            assert isinstance(body[key], int), f"{key} is {type(body[key]).__name__}, expected int"
        assert isinstance(body["has_more"], bool)

    def test_three_pages_of_three_partition_seven_records_without_overlap(self, list_tables):
        """The whole pager, walked: every record once, in ``updated_at`` order.

        Asserted as a partition rather than page by page — the union is the full
        descending sort and the pages are disjoint. That is the property R8.1's
        rewrite has to preserve, and it is the one a per-page assertion would miss:
        two bounded reads that each return a correct-looking page can still drop or
        duplicate a record at the boundary.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        pages = []
        for page in (1, 2, 3):
            _, body = call_pipelines(page=page, page_size=3)
            assert body["total_count"] == 7
            assert body["total_pages"] == 3
            assert body["page"] == page
            assert body["page_size"] == 3
            pages.append([entry["pipeline_id"] for entry in body["pipelines"]])

        assert [len(page) for page in pages] == [3, 3, 1], "the last page is short, not padded"
        walked = [pipeline_id for page in pages for pipeline_id in page]
        assert walked == [f"pipe-{n}" for n in reversed(range(7))], (
            "the walk must be the full descending updated_at order, exactly once each"
        )
        assert len(set(walked)) == 7

    def test_has_more_is_page_less_than_total_pages_and_is_false_on_the_last(self, list_tables):
        """``page < total_pages`` (``:388``) — a comparison, not a count of remaining rows.

        Pinned at the boundary in both directions, because that is where an
        off-by-one in a rewritten pager lands: page 2 of 3 is ``True`` even though it
        is followed by a single record, and page 3 of 3 is ``False``.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        assert call_pipelines(page=1, page_size=3)[1]["has_more"] is True
        assert call_pipelines(page=2, page_size=3)[1]["has_more"] is True
        assert call_pipelines(page=3, page_size=3)[1]["has_more"] is False

    def test_total_pages_is_a_ceiling_division_and_is_one_when_a_single_page_holds_all(self, list_tables):
        """``(total + size - 1) // size`` (``:241``), pinned at an exact multiple too.

        Six records over a page size of three is the case a naive ``total // size + 1``
        gets wrong, so it is included alongside the seven-record remainder case.
        """
        for n in range(6):
            list_tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z")
            )

        assert call_pipelines(page=1, page_size=3)[1]["total_pages"] == 2
        assert call_pipelines(page=1, page_size=6)[1]["total_pages"] == 1
        assert call_pipelines(page=1, page_size=20)[1]["total_pages"] == 1

    def test_an_empty_table_gives_zero_pages_and_a_defined_envelope(self, list_tables):
        """No records: ``total_pages`` is ``0``, not ``1``, and ``has_more`` is ``False``.

        Zero pages with ``page: 1`` is arithmetically consistent and is what the
        frontend's ``?? 1`` fallback would *mask* if the key vanished — so the value
        matters more than it looks. ``has_more`` is ``1 < 0``.
        """
        status, body = call_pipelines()

        assert status == 200
        assert body["pipelines"] == []
        assert body["total_count"] == 0
        assert body["total_pages"] == 0
        assert body["has_more"] is False
        assert body["partial_counts"] == [], (
            "D8: an empty table is a *complete* read of nothing, not a truncated one — "
            "the zero must not be marked as a floor or the distinction is lost"
        )
        assert body["enrichment_abandoned"] == [], "D9: an empty page has no future to abandon"
        assert set(body) == set(ENVELOPE_KEYS)

    def test_a_page_past_the_end_echoes_the_requested_page_and_returns_nothing(self, list_tables):
        """``page`` is the **request** echoed back, not clamped to ``total_pages``.

        Page 99 of 3 returns an empty list with ``page: 99`` and
        ``total_count: 7`` — so ``total_count`` is not "how many are in this
        response", and a client cannot infer emptiness from the counts alone. Pinned
        because a rewritten pager that clamped instead would look tidier and would
        change an observable value.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        _, body = call_pipelines(page=99, page_size=3)

        assert body["pipelines"] == []
        assert body["page"] == 99
        assert body["total_count"] == 7
        assert body["total_pages"] == 3
        assert body["has_more"] is False

    def test_page_and_page_size_are_clamped_at_the_boundaries(self, list_tables):
        """``max(1, int(page))`` and ``min(100, max(1, int(page_size)))`` (``:211-212``).

        The 100 ceiling is the only bound on the response size that exists today, and
        it bounds the **fan-out** as well: 100 pipelines on a page is 100 × 5 reads.
        A rewrite that moved the clamp would change the worst case, so both ends are
        pinned.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        _, zeroth = call_pipelines(page=0, page_size=3)
        assert zeroth["page"] == 1, "page 0 clamps up to 1"

        _, negative = call_pipelines(page=-5, page_size=3)
        assert negative["page"] == 1

        _, huge = call_pipelines(page=1, page_size=9999)
        assert huge["page_size"] == 100, "page_size clamps down to 100"

        _, tiny = call_pipelines(page=1, page_size=0)
        assert tiny["page_size"] == 1, "page_size clamps up to 1"

    def test_the_default_page_is_one_of_twenty(self, list_tables):
        """``page=1, page_size=20`` (``:190``) — the signature's own defaults.

        The 20 is the figure R8's worst case is computed from
        (20 × (10+1+1+1+1) = 280), so it is pinned rather than left implicit.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        _, body = call_pipelines()

        assert body["page"] == 1
        assert body["page_size"] == 20

    def test_archived_records_are_excluded_by_default_and_reachable_by_filter(self, list_tables):
        """``Attr("status").ne("archived")`` by default, ``eq(status_filter)`` when given.

        Both arms change ``total_count``, which is the point: the total is
        post-filter, so it is a different number per request rather than a property
        of the table. A bounded read that produced a table-wide total would satisfy
        every *presence* assertion and be wrong here.
        """
        self._seed_seven_active_and_one_archived(list_tables)

        _, default = call_pipelines()
        assert default["total_count"] == 7
        assert "pipe-archived" not in [entry["pipeline_id"] for entry in default["pipelines"]]

        _, archived = call_pipelines(status_filter="archived")
        assert archived["total_count"] == 1
        assert [entry["pipeline_id"] for entry in archived["pipelines"]] == ["pipe-archived"]

        _, active = call_pipelines(status_filter="active")
        assert active["total_count"] == 7

    def test_search_matches_id_or_name_case_insensitively_and_narrows_the_total(self, list_tables):
        """``search`` is a substring test over ``pipeline_id`` **or** ``pipeline_name``.

        Applied in memory at ``:228-235``, after the scan — which is half of why
        risk 5.6 says R8.1 may be partly unachievable. Both fields and both cases are
        pinned so a rewrite that moved search into a ``FilterExpression`` (which is
        case-sensitive and has no ``or`` over two attributes for free) shows the
        difference here.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-alpha", pipeline_name="Orders Feed"))
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-beta", pipeline_name="Customers"))

        _, by_id = call_pipelines(search="ALPHA")
        assert [entry["pipeline_id"] for entry in by_id["pipelines"]] == ["pipe-alpha"]
        assert by_id["total_count"] == 1, "the total is the searched count, not the table count"

        _, by_name = call_pipelines(search="customers")
        assert [entry["pipeline_id"] for entry in by_name["pipelines"]] == ["pipe-beta"]

        _, no_match = call_pipelines(search="nothing-matches-this")
        assert no_match["pipelines"] == []
        assert no_match["total_count"] == 0
        assert no_match["total_pages"] == 0

    def test_a_pipelines_table_failure_is_a_5xx_and_carries_no_envelope_key(self, list_tables, monkeypatch):
        """The outer ``except`` (``:392-394``) routes to ``handle_error``, not to an empty page.

        The asymmetry with §3 is the point and mirrors what D1 pinned for
        ``get_stats``: a *per-pipeline* failure degrades to a stub, while a failure of
        the page read itself is a 500. An empty 200 would be indistinguishable from
        "no pipelines", which is the failure family R7.4 exists to remove one endpoint
        over.
        """
        real = list_tables["pipelines_table"]

        class RefusingScan:
            def __getattr__(self, name: str) -> Any:
                if name == "scan":

                    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                        raise RuntimeError("p47 D5: pipelines scan refused")

                    return _refuse
                return getattr(real, name)

        monkeypatch.setattr(crud, "pipelines_table", RefusingScan())

        response = crud.get_pipelines()

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert set(body) & set(ENVELOPE_KEYS) == set(), "a failed page read must not look like an empty page"


# ---------------------------------------------------------------------------
# A state table that refuses reads for selected pipelines (section 3)
# ---------------------------------------------------------------------------
class StateTableRefusingPipelines:
    """A moto state-table handle whose ``query`` raises for named ``pipeline_id``\\ s.

    Selective rather than wholesale, because the property under test is
    **isolation**: a page containing one failing pipeline and two succeeding ones is
    the only population that can distinguish "the failure was contained" from "the
    page happened not to fail". Everything else delegates to the real handle, so the
    surviving entries are enriched from moto's own data.

    The predicate keys on the query's own ``KeyConditionExpression`` rather than on
    call order, which is what makes it deterministic under the fan-out. All four
    per-pipeline state reads key on ``pipeline_id``, so refusing that value refuses
    every one of them — matching a real per-pipeline throttle rather than a single
    unlucky read.
    """

    def __init__(self, table: Any, refuse: set[str]) -> None:
        self._table = table
        self._refuse = refuse

    def query(self, **kwargs: Any) -> Any:
        pipeline_id = queried_pipeline_id(kwargs)
        if pipeline_id in self._refuse:
            raise RuntimeError(f"p47 D5: state read refused for {pipeline_id}")
        return self._table.query(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)


# ═══════════════════════════════════════════════════════════════════════════
# 3. The per-future failure-isolation stub (R8.5)
# ═══════════════════════════════════════════════════════════════════════════
class TestFailureIsolation:
    """``:359-375`` — one pipeline's enrichment failure must not fail the page.

    R8.5 requires this preserved, and D8 and D9 both touch the fan-out.
    ``test_read_path_errors.py::test_list_view_enrichment_degrades_per_pipeline_on_throttle``
    already pins the 200 and two of the stub's fields through ``MagicMock`` tables.
    This section pins the **shape** — which is what a rewritten fan-out would change
    — and the two consequences that file does not reach: the ``status`` key means
    something different in a stub, and the stub loses its place in the page.

    Note what the ``except`` catches: ``future.result()`` re-raises whatever the
    worker raised, and the clause is bare ``Exception``. So the three inner
    ``try``/``except`` blocks inside ``enrich_pipeline`` already swallow their own
    failures — only the schema-history read and ``get_pipeline_file_count`` can reach
    this outer catch, and only the former can, since the latter swallows too.
    **The stub path is therefore reachable through exactly one read today.** D6
    caching the other three must not quietly widen that.

    **Still exactly one read after D7**, verified rather than assumed: the read is now
    ``get_pipeline_schema_history_preview``, it is called outside any ``try`` in
    ``enrich_pipeline``, and it raises whatever the query raises. D7's completeness
    flag travels as a return value precisely so that marking a bounded walk did not
    require a ``try`` here — a ``try`` would have retired the stub path silently while
    every test in this section kept passing on the *other* pipelines.

    **Still exactly one read after D9, and one non-read condition beside it.** D9
    bounded the fan-out itself, so the same six-key stub is now emitted from two
    places: this ``except``, and the ``except TimeoutError`` that ships every future
    still outstanding at the deadline. The R8.5 boundary — *which enrichment read
    can produce a stub* — did not move; what moved is that a stub no longer implies a
    failed read. D9 made the literal one function (``crud._unenriched_stub``) so the
    two causes cannot drift into two shapes, and named the deadline case on the
    envelope (``enrichment_abandoned``) so a client can tell them apart. §9 asserts
    both halves; every test in this section is unchanged.
    """

    @staticmethod
    def _seed_three(tables: dict[str, Any]) -> None:
        """Three pipelines, ``pipe-2`` newest — so a stub's lost position is visible."""
        for n in range(3):
            tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z")
            )
            seed(
                tables["state_table"],
                [
                    state_row(
                        f"run-{n}",
                        "schema_extract",
                        f"pipe-{n}",
                        "2026-01-02T00:00:00Z",
                        schema_hash=f"hash-{n}",
                        json_s3_uri=f"s3://bucket/schemas/{n}.json",
                    )
                ],
            )

    def test_a_single_pipelines_failure_leaves_the_page_at_200_and_full_length(self, list_tables, monkeypatch):
        """R8.5, stated as the page-level property: the page survives intact.

        Three in, three out, ``200`` — the failing pipeline is still *present*, which
        is what makes this degradation rather than omission. A rewrite that dropped
        the entry would still return 200 and would fail here.
        """
        self._seed_three(list_tables)
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-2"}))

        status, body = call_pipelines()

        assert status == 200
        assert len(body["pipelines"]) == 3
        assert {entry["pipeline_id"] for entry in body["pipelines"]} == {"pipe-0", "pipe-1", "pipe-2"}
        assert body["total_count"] == 3

    def test_the_stub_carries_exactly_six_keys_and_they_are_a_subset_of_the_enriched_set(
        self, list_tables, monkeypatch
    ):
        """The stub's shape (``:366-375``), pinned as a set and as a strict subset.

        The subset relation is asserted rather than assumed because it is what lets a
        client parse both shapes with one type: every key in a stub also means the
        same thing in an enriched entry — with the single exception ``status``, which
        the next test pins separately. If a later fan-out rewrite emits a key here
        that the enriched form lacks, that one type stops holding.
        """
        self._seed_three(list_tables)
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-2"}))

        _, body = call_pipelines()
        stub = next(entry for entry in body["pipelines"] if entry["pipeline_id"] == "pipe-2")

        assert set(stub) == set(STUB_KEYS), (
            f"the stub's key set changed: "
            f"missing={sorted(set(STUB_KEYS) - set(stub))} "
            f"unexpected={sorted(set(stub) - set(STUB_KEYS))}"
        )
        assert len(STUB_KEYS) == 6
        assert set(STUB_KEYS) < set(ENRICHED_KEYS), "a stub key that no enriched entry carries"

    def test_the_stubs_values_are_the_record_and_two_neutral_zeros(self, list_tables, monkeypatch):
        """What an unenriched entry says: identity from the record, ``0`` and ``[]``.

        ``schema_version: 0`` and ``schema_history: []`` are **neutral rather than
        accurate** — this pipeline has one schema version and the stub reports none.
        That is the documented cost of degrading, and it is pinned so nobody later
        reads a zero here as a fact about the pipeline. The other fifteen enrichment
        keys are simply absent, which is the more honest signal, and is why the
        subset assertion above matters.
        """
        self._seed_three(list_tables)
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-2"}))

        _, body = call_pipelines()
        stub = next(entry for entry in body["pipelines"] if entry["pipeline_id"] == "pipe-2")

        assert stub["pipeline_key"] == "pipe-2"
        assert stub["pipeline_id"] == "pipe-2"
        assert stub["pipeline_name"] == "Pipeline pipe-2"
        assert stub["schema_version"] == 0
        assert stub["schema_history"] == []
        assert "file_count" not in stub, "a stub reports absence, not a zero it cannot vouch for"
        assert "mapping_count" not in stub
        assert "all_files" not in stub

    def test_a_stubs_status_is_the_record_status_while_an_enriched_status_is_derived(self, list_tables, monkeypatch):
        """The one key whose **meaning** differs between the two shapes.

        ``status`` in a stub is ``item_str(pipeline, "status", "active")`` (``:371``) —
        the record's own value. In an enriched entry it is ``live_status``, derived
        from the executions read. So a pipeline with a run in flight reads
        ``"running"`` when enrichment succeeds and ``"active"`` when it fails, and
        ``PipelineListPage.tsx:52`` renders whichever arrives.

        This is characterized, not endorsed: a degraded entry silently claims a
        quieter state than the pipeline is in. Flagged for D8/D9 rather than fixed
        here, because changing it is a response-value change and this task is tests
        only.
        """
        self._seed_three(list_tables)
        for pipeline_id in ("pipe-1", "pipe-2"):
            list_tables["executions_table"].put_item(
                Item={
                    "pipeline_id": pipeline_id,
                    "run_id": "run-live",
                    "status": "RUNNING",
                    "started_at": "2026-01-05T00:00:00Z",
                }
            )
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-2"}))

        _, body = call_pipelines()
        by_id = {entry["pipeline_id"]: entry for entry in body["pipelines"]}

        assert by_id["pipe-1"]["status"] == "running", "enriched: the derived live status"
        assert by_id["pipe-2"]["status"] == "active", (
            "stub: the record's own status — the same running pipeline reads quieter when its enrichment failed"
        )

    def test_a_stub_keeps_the_position_its_record_earned(self, list_tables, monkeypatch):
        """**D5's DEFECT (ordering), FIXED by task D8.** A stub no longer sorts last.

        *What D5 measured.* ``pipe-2`` is the newest record, so it heads the page
        when enrichment succeeds. When its enrichment failed, the stub carried no
        ``updated_at`` (the six-key literal omits it), the re-sort read that key off
        each entry, ``item_str`` yielded ``""``, and ``reverse=True`` sorted the
        empty string last: ``["pipe-1", "pipe-0", "pipe-2"]``. Under a comment
        reading "Sort to maintain original order". R8.5 held — 200, entry present —
        but the newest pipeline appearing last *only when a read failed* is a wrong
        answer that looks like a right one, so D5 reported it rather than ratifying
        it and asserted **both** orders "so either resolution is a legible diff".

        *The resolution, and whose it was.* **D8's**, not D9's: the defect is in how
        the page is ordered, which is part of page selection, and D9 changes neither
        the page order nor the stub literal. The re-sort now sorts by the position
        each record held in the already-sorted page (``page_order``) instead of
        re-deriving the order from the enriched entry. The healthy order is
        byte-identical — it *is* that order — and the degraded order now matches it.

        This test is the diff D5 designed for: its second assertion changed, and the
        first did not.
        """
        self._seed_three(list_tables)

        _, healthy = call_pipelines()
        assert [entry["pipeline_id"] for entry in healthy["pipelines"]] == ["pipe-2", "pipe-1", "pipe-0"], (
            "the healthy order is newest-first, and D8 must not have moved it"
        )

        logs_handler._cache.clear()
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-2"}))
        _, degraded = call_pipelines()

        assert [entry["pipeline_id"] for entry in degraded["pipelines"]] == ["pipe-2", "pipe-1", "pipe-0"], (
            "D8: the degraded order must equal the healthy order — a failed enrichment degrades the "
            "entry's fields, never its position (D5 measured ['pipe-1', 'pipe-0', 'pipe-2'] here)"
        )
        stub = next(entry for entry in degraded["pipelines"] if entry["pipeline_id"] == "pipe-2")
        assert set(stub) == set(STUB_KEYS), "the stub is still a stub — only where it sorts changed"

    def test_every_pipeline_failing_still_yields_a_full_page_of_stubs(self, list_tables, monkeypatch):
        """The degenerate case: isolation is per-future, so all three degrade separately.

        Pinned because a rewritten fan-out that hoisted the ``try`` outside the loop
        would satisfy the single-failure test above and lose the page here.
        """
        self._seed_three(list_tables)
        monkeypatch.setattr(
            crud,
            "state_table",
            StateTableRefusingPipelines(list_tables["state_table"], {"pipe-0", "pipe-1", "pipe-2"}),
        )

        status, body = call_pipelines()

        assert status == 200
        assert len(body["pipelines"]) == 3
        for entry in body["pipelines"]:
            assert set(entry) == set(STUB_KEYS)

    def test_the_three_inner_catches_swallow_without_reaching_the_stub_path(self, list_tables, monkeypatch):
        """Only the schema-history read can produce a stub today.

        The executions read's own ``except`` (``:325``) is exercised by refusing that
        table alone: the entry stays **fully enriched**, 21 keys, with ``status``
        falling back to the record's value and both counts at ``0``. So a failure
        there is invisible in the shape and visible only in the numbers.

        This is the boundary D6 must not move. Caching the three inner reads is fine;
        letting one of them raise past its ``except`` would convert a silently
        degraded field into a whole-entry stub, changing the response shape for a
        failure mode that used to keep it.

        **D6 kept the boundary and stopped the degradation being silent.** The three
        reads moved into module-level functions that *raise*, with each ``except``
        left at the same place inside ``enrich_pipeline`` — so the entry is still
        21 enriched keys plus D6's marker, never a stub. What the failure now also
        does is name itself: ``partial_fields`` carries the two counts **and**
        ``status``, because ``status`` is derived from them and is the one of the
        three the UI renders. Asserted below, so a later change that stops marking
        it has to say so here.
        """
        seed_fully_enriched_pipeline(list_tables, "pipe-1")
        real = list_tables["executions_table"]

        class RefusingQuery:
            def __getattr__(self, name: str) -> Any:
                if name == "query":

                    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                        raise RuntimeError("p47 D5: executions query refused")

                    return _refuse
                return getattr(real, name)

        monkeypatch.setattr(crud, "executions_table", RefusingQuery())

        entry = one_entry()

        assert set(entry) == set(ENRICHED_KEYS), "an inner failure must not produce a stub"
        assert entry["status"] == "active", "live_status keeps its pre-read value"
        assert (entry["running_count"], entry["approval_count"]) == (0, 0)
        assert entry["file_count"] == 2, "the reads that succeeded are unaffected"
        assert entry["partial_fields"] == ["running_count", "approval_count", "status"], (
            "D6: the failure is still swallowed, but the three keys it makes unreliable are named — "
            "`status` among them, because it is derived from the two counts and is what is rendered"
        )
        assert entry["mapping_count"] == 7, "the other two reads completed, so they are not marked"
        assert len(entry["all_files"]) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 4. The five reads, counted — D6's and D7's measuring stick
# ═══════════════════════════════════════════════════════════════════════════
class TestReadCount:
    """One scan for the page, then five reads per pipeline on it.

    Without a current number, D6 (cache three of the five) and D7 (cap the
    schema-history walk) are unmeasurable. The reads are identified individually
    rather than only totalled — see ``classify_reads`` — so a change that swapped one
    read for another while keeping the count cannot pass.

    Two things are asserted that the arithmetic alone would not catch: that nothing
    lands in ``other`` (a sixth read cannot hide inside a total), and that the page
    read is **one** scan whose ``ScannedCount`` is the whole table regardless of the
    page requested. That second one is R8.1's evidence.
    """

    def test_one_pipeline_costs_one_scan_and_five_reads(self, list_tables):
        """The per-pipeline figure R8's worst case is built from.

        Five, one per row of R8's table, each identified. The cache is cold — the
        fixture clears it — so this is the miss path, which is the path a cold Lambda
        container takes on every first request.
        """
        seed_fully_enriched_pipeline(list_tables)

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()

        reads = classify_reads(recorded)
        assert reads["other"] == [], f"an unclassified read appeared: {[p for p, _ in reads['other']]}"
        assert len(reads["page_scan"]) == 1
        assert len(reads["schema_history"]) == 1
        assert len(reads["file_count"]) == 1
        assert len(reads["mapping_count"]) == 1
        assert len(reads["recent_files"]) == 1
        assert len(reads["executions"]) == 1
        assert len(recorded) == 6, "one page scan plus five per-pipeline reads"

    def test_the_read_count_is_linear_in_the_page_size_not_the_table_size(self, list_tables):
        """N+1: three pipelines on the page cost 1 + 3×5 = 16 reads.

        Linearity is the property, and it is asserted across two page sizes over the
        **same** five-pipeline table so table size is held constant. R8.2's caching
        changes the constant; R8.1's bounded page changes neither — which is why both
        need pinning separately.
        """
        for n in range(5):
            seed_fully_enriched_pipeline(list_tables, f"pipe-{n}")

        with recorded_dynamodb_calls() as recorded:
            _, body = call_pipelines(page=1, page_size=3)
        assert len(body["pipelines"]) == 3
        assert len(recorded) == 1 + 3 * 5

        logs_handler._cache.clear()
        with recorded_dynamodb_calls() as recorded:
            _, body = call_pipelines(page=1, page_size=5)
        assert len(body["pipelines"]) == 5
        assert len(recorded) == 1 + 5 * 5

    def test_the_page_read_scans_the_whole_table_whatever_page_is_asked_for(self, list_tables):
        """**R8.1's evidence.** Page 2 of 2 still examines every record.

        ``ScannedCount`` off moto's own ``Scan`` response, which a scripted stub
        cannot produce — the same instrument and the same reason D1 gave. Both pages
        examine all five records to return two or three, so the cost of page *N* is
        independent of *N* and proportional to the table.

        The filter is asserted to be present and applied *after* the read
        (``Count < ScannedCount`` with an archived record in the population), because
        that is the second half of the same claim: a ``FilterExpression`` does not
        reduce what DynamoDB reads.
        """
        for n in range(5):
            list_tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z")
            )
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-archived", status="archived"))

        for page in (1, 2):
            logs_handler._cache.clear()
            with recorded_dynamodb_calls() as recorded:
                call_pipelines(page=page, page_size=3)

            scans = classify_reads(recorded)["page_scan"]
            assert len(scans) == 1, "one scan per request, no pagination of the page read"
            params, response = scans[0]
            assert "FilterExpression" in params
            assert response["ScannedCount"] == 6, (
                f"page {page} examined {response['ScannedCount']} records — if this ever stops being "
                f"the whole table, R8.1's bounded read has landed and this assertion is the diff"
            )
            assert response["Count"] == 5 < response["ScannedCount"], (
                "the status filter is applied after the read, so it does not reduce the cost"
            )

    def test_the_page_read_follows_last_evaluated_key_to_the_end_of_the_filtered_set(self, list_tables):
        """**D5's DEFECT (truncation), FIXED by task D8.** The page read paginates.

        *What D5 measured.* The page read was a single ``scan`` with no pagination
        loop, so ``total_count``, ``total_pages`` and the page contents were all
        computed from however much fit in one 1 MB page. Driven with four ~300 KB
        records: the response carried a ``LastEvaluatedKey``, no second ``Scan`` was
        issued, and ``total_count`` read **3 of 4** — presented as the total. The
        same "a truncated total is indistinguishable from a real one" failure R7.4
        removed from ``/api/stats``.

        *After D8.* The walk follows ``LastEvaluatedKey`` under
        ``_MAX_PAGE_SCAN_PAGES`` and ``_page_scan_deadline``, so the seeded four are
        all found, the intermediate responses still carry a ``LastEvaluatedKey``
        (which is how we know the ceiling was genuinely crossed and the test is not
        passing vacuously), the final one does not, and ``partial_counts`` is empty
        because the walk completed.

        The seed count is asserted exactly here, unlike D5's "fewer than seeded":
        after the fix the answer is the seeded number regardless of how DynamoDB
        chose to page it, which is the stronger assertion the fix makes available.
        """
        filler = "x" * 300_000
        for n in range(4):
            list_tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z", filler=filler)
            )

        with recorded_dynamodb_calls() as recorded:
            _, body = call_pipelines()

        scans = classify_reads(recorded)["page_scan"]
        assert len(scans) > 1, "the 1 MB ceiling was not reached; the seed is too small"
        assert all(response.get("LastEvaluatedKey") for _p, response in scans[:-1]), (
            "every scan but the last must have resumed from a LastEvaluatedKey"
        )
        assert scans[-1][1].get("LastEvaluatedKey") is None, "the last scan reached the end of the table"
        assert body["total_count"] == 4, "D8: total_count is the whole filtered set, not one page of it"
        assert body["total_pages"] == 1
        assert [entry["pipeline_id"] for entry in body["pipelines"]] == ["pipe-3", "pipe-2", "pipe-1", "pipe-0"], (
            "the records past the first 1 MB page used to be invisible entirely"
        )
        assert body["partial_counts"] == [], "the walk completed, so nothing is a floor"

    def test_a_second_request_in_the_same_process_costs_the_page_scan_and_nothing_else(self, list_tables):
        """R8.2 satisfied: all five per-pipeline reads are cached.

        **This is D5's read-count assertion, amended by D6 with its reason.** D5
        pinned the warm figure at 1 + 3 and named it "D6's baseline: after D6 this
        must be the page scan plus zero". It is. Each of the five reads is still
        asserted individually rather than only totalled, so a regression that
        uncaches one of them says which.

        The cache is a process-global dict with a 300 s TTL, so "the same process"
        means the same warm Lambda container. A cold container still pays the full
        five, which is why §4's first test describes production worst case and this
        one describes the best case — the gap between 6 and 1 is what D6 bought.

        The remaining scan is the page read, which is task D8's.
        """
        seed_fully_enriched_pipeline(list_tables)

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()
        assert len(recorded) == 6

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()

        reads = classify_reads(recorded)
        assert reads["schema_history"] == [], "cached before D6"
        assert reads["file_count"] == [], "cached before D6"
        assert reads["mapping_count"] == [], "cached by D6 — R8.2"
        assert reads["recent_files"] == [], "cached by D6 — R8.2"
        assert reads["executions"] == [], "cached by D6 — R8.2"
        assert len(reads["page_scan"]) == 1
        assert len(recorded) == 1, "the page scan alone; every per-pipeline read is served from cache"

    def test_a_cached_entry_is_identical_to_the_one_that_filled_the_cache(self, list_tables):
        """A hit must not be a different answer from the miss it replaced.

        The counterpart D5 named and left to D6: it pinned that a *miss* reads every
        seeded value rather than its default, and said "its counterpart on a hit is
        D6's own to write". Asserted as whole-payload equality rather than key by
        key, so a cached value that round-tripped through the cache differently —
        a tuple that came back a list, a Decimal that came back a float — fails
        here rather than in whatever renders it.
        """
        seed_fully_enriched_pipeline(list_tables)

        miss = one_entry()
        hit = one_entry()

        assert hit == miss, "the cached entry differs from the computed one"
        assert hit["mapping_count"] == 7
        assert [f["file_key"] for f in hit["all_files"]] == ["second.csv", "first.csv"]
        assert (hit["running_count"], hit["approval_count"]) == (1, 0)
        assert hit["status"] == "running", "the live status survives the executions cache"
        assert hit["partial_fields"] == []

    def test_a_cached_value_is_keyed_per_pipeline(self, list_tables):
        """Three caches, three keys — a warm entry must not answer for another pipeline.

        The failure mode this rules out is the cheap one: a cache key that omitted
        ``pipeline_id`` would make the second pipeline on a page inherit the first's
        mapping count and file preview, which reads as plausible data rather than as
        a bug. Driven by giving two pipelines deliberately different values and
        warming the cache with one of them first.
        """
        seed_fully_enriched_pipeline(list_tables, "pipe-a")
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-b", updated_at="2026-01-04T00:00:00Z"))
        seed(
            list_tables["state_table"],
            [
                state_row("b-run-1", "schema_mapping", "pipe-b", "2026-01-02T00:00:00Z", mapping_count=42),
                state_row(
                    "b-run-1",
                    "file_validation",
                    "pipe-b",
                    "2026-01-02T00:01:00Z",
                    status="success",
                    original_s3_uri="s3://bucket/incoming/b-only.csv",
                ),
            ],
        )

        _status, first = call_pipelines(search="pipe-a")
        assert first["pipelines"][0]["mapping_count"] == 7

        _status, both = call_pipelines()
        by_id = {entry["pipeline_id"]: entry for entry in both["pipelines"]}

        assert by_id["pipe-a"]["mapping_count"] == 7, "served from cache, unchanged"
        assert by_id["pipe-b"]["mapping_count"] == 42, "a cold pipeline did not inherit the warm one's count"
        assert [f["file_key"] for f in by_id["pipe-b"]["all_files"]] == ["b-only.csv"]

    def test_the_worker_pool_is_ten_wide_and_its_comment_agrees(self, list_tables):
        """R8.4, resolved by task D10 — the pool comment now states the pool's width.

        This pinned the **disagreement** before D10: ``max_workers=10`` against a
        comment reading "max 5 concurrent", so R8.4 surfaced here whichever way it
        was resolved. D10 resolved it in favour of the code — the width is unchanged
        at ten and the comment was corrected — so what is pinned now is the
        agreement, in both directions: lowering the pool to five fails the first
        assertion, and reverting the comment to "max 5 concurrent" fails the third.

        Asserted against the source text rather than by observing concurrency, which
        is what makes it deterministic: a thread-count observation on a 10-wide pool
        over a 3-item page proves nothing.

        Same instrument ``test_sfn_states_contract.py`` uses for source-level claims,
        and the same reason: no synthesised artifact carries a comment.
        """
        assert "ThreadPoolExecutor(max_workers=10)" in HANDLER_SOURCE
        assert "max 10 concurrent" in HANDLER_SOURCE, (
            "the pool comment no longer states the pool's width — R8.4 chose the code (ten) as the "
            "correct half, so update this pin to the new wording rather than deleting it"
        )
        assert "max 5 concurrent" not in HANDLER_SOURCE, (
            "the stale comment is back; the pool has been ten wide throughout and D10 corrected the "
            "comment to match it, not the other way round"
        )

    def test_the_listing_writes_nothing(self, list_tables):
        """A read endpoint: no ``PutItem``, no ``UpdateItem``, no audit entry.

        Pinned because D6 adds caching, and the mechanism R8.2 names is an in-process
        dict — not the DynamoDB rows R7.2 uses. If D6 reaches for the DynamoDB cache
        instead, a write appears on this path and this assertion is where the choice
        surfaces to be ratified. It also confirms why this file, unlike the contract
        test, need not stub ``write_audit_entry``.
        """
        seed_fully_enriched_pipeline(list_tables)

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()

        writes = [op for op, _, _ in recorded if op not in ("Scan", "Query")]
        assert writes == [], f"the listing issued {writes}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Limit before Filter — where two values are silently wrong
# ═══════════════════════════════════════════════════════════════════════════
#: Enough newer non-matching state rows to consume the ``Limit=50`` window whole.
#:
#: Sixty is not an extreme figure. Each run writes a row per step
#: (``schema_extract``, ``schema_mapping``, ``code_generation``, ``file_validation``,
#: ``quality_checks``, …), so 50 rows is roughly six runs of one pipeline. **Every
#: pipeline in routine use crosses this.**
_WINDOW_FILLERS = 60


def seed_pipeline_behind_a_full_window(tables: dict[str, Any], pipeline_id: str = "pipe-1") -> None:
    """A pipeline whose matching rows sit behind 60 newer non-matching ones.

    ``PipelineIndex`` is sorted on ``timestamp`` and all four state reads pass
    ``ScanIndexForward=False``, so the newest rows are read first. The fillers carry
    2026-06 timestamps and the rows that *should* match carry 2026-01 ones, which puts
    the matches beyond the 50-item window by construction rather than by luck.

    The fillers use ``code_generation``, a step name none of the four reads filters
    on, so they are near-misses for every read rather than for one.
    """
    tables["pipelines_table"].put_item(Item=pipeline_record(pipeline_id, updated_at="2026-06-30T00:00:00Z"))
    rows = [
        state_row(f"noise-{n}", "code_generation", pipeline_id, f"2026-06-01T00:{n:02d}:00Z", status="success")
        for n in range(_WINDOW_FILLERS)
    ]
    rows += [
        state_row(
            f"fv-{n}",
            "file_validation",
            pipeline_id,
            f"2026-01-01T00:{n:02d}:00Z",
            status="success",
            original_s3_uri=f"s3://bucket/incoming/old-{n}.csv",
        )
        for n in range(3)
    ]
    rows.append(state_row("sm-1", "schema_mapping", pipeline_id, "2026-01-01T00:00:00Z", mapping_count=7))
    seed(tables["state_table"], rows)


class TestLimitAppliedBeforeFilter:
    """The comment at ``:277`` admits it; this measures what it costs.

    "Limit applies BEFORE FilterExpression, so we need a higher limit." True, and a
    higher limit is a **bigger window, not a bound** — which is the distinction the
    two reads at ``:263`` and ``:280`` get wrong. Both pass ``Limit=50``, neither
    follows ``LastEvaluatedKey``, so both report *nothing found* when the answer sits
    at position 51.

    Measured with the shared moto fixture rather than a stub, so the semantics are
    enforced by the query engine. Same instrument D1 used for the two filtered stats
    scans, and the reading is the same shape: ``ScannedCount`` is what DynamoDB
    examined and ``Count`` is what survived the filter.

    **Two of these three tests pinned defects, not contracts** — their assertions
    were the current wrong values, marked ``DEFECT``, with the correct value stated
    in the docstring. D5's argument was that D6 caching these reads without fixing
    the truncation would cache a wrong answer for 300 s, which is strictly worse.

    **D6 fixed them and this section now asserts the correct values**, with each
    defect's history kept in the docstring so the reads stay explicable. The
    mechanism test below is unamended in substance: a first page of 50 still
    examines 50 rows and matches none. What changed is that the read continues past
    it, so the tests that read a *value* moved and the test that reads the
    *mechanism* did not.
    """

    def test_the_first_page_examines_exactly_fifty_rows_and_matches_none(self, list_tables):
        """The mechanism, measured: ``ScannedCount == 50``, ``Count == 0``, ``LastEvaluatedKey`` set.

        This is the assertion D5 said would stay true whatever D6 decided about the
        values, and it did — it describes what a ``Limit`` beside a
        ``FilterExpression`` does, which no fix here changes. The ``Limit=50`` is
        still passed, now as a *page size* rather than as the whole read.

        **Amended by D6 in one respect**: the walk no longer stops at that page, so
        the last assertion is the new half of the same claim — the
        ``LastEvaluatedKey`` this read leaves is now followed, and the following page
        carries an ``ExclusiveStartKey`` continuing it. The old name said "and match
        none" of the read as a whole; it is now scoped to the first page, which is
        what it always measured.
        """
        seed_pipeline_behind_a_full_window(list_tables)

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()

        reads = classify_reads(recorded)
        for name in ("mapping_count", "recent_files"):
            params, response = reads[name][0]
            assert params["Limit"] == 50
            assert "ExclusiveStartKey" not in params, "the first page starts at the top of the partition"
            assert response["ScannedCount"] == 50, (
                f"{name} examined {response['ScannedCount']} rows — the Limit is applied to the read"
            )
            assert response["Count"] == 0, f"{name} matched nothing inside its first page"
            assert response.get("LastEvaluatedKey"), f"{name} left a LastEvaluatedKey"

            assert len(reads[name]) > 1, (
                f"{name} stopped at one page — D6's walk must follow the LastEvaluatedKey rather than "
                f"discarding it, which is the whole defect"
            )
            continuation, _ = reads[name][1]
            assert continuation["ExclusiveStartKey"] == response["LastEvaluatedKey"], (
                f"{name}'s second page continues the first — one walk, not two reads"
            )

        _params, counted = reads["file_count"][0]
        assert counted["ScannedCount"] == _WINDOW_FILLERS + 4, "the COUNT read passes no Limit"
        assert counted["Count"] == 3

    def test_all_files_and_file_count_agree_at_three_behind_a_full_window(self, list_tables):
        """**DEFECT 2, FIXED by D6.** The response no longer contradicts itself.

        **What D5 measured:** ``file_count: 3`` and ``all_files: []`` in one payload,
        describing the same three ``file_validation`` success rows. The count read
        (``:97``) passed no ``Limit`` and found them; the preview read passed
        ``Limit=50``, discarded its ``LastEvaluatedKey``, and did not. The
        contradiction was the sharpest available evidence because it needed no
        external oracle — the payload disproved itself.

        **What D6 changed:** ``get_pipeline_recent_files`` paginates newest-first
        until it has three matches or the partition ends. Here it takes two pages,
        and the two keys agree. Asserted as agreement rather than as a literal count
        so the oracle stays inside the payload, which is what made this the strongest
        of D5's four findings.

        The history is kept because it explains why the read has a page loop and a
        completeness flag in it. And one caveat travels forward: ``file_count``, the
        oracle here, follows no ``LastEvaluatedKey`` of its own, so the agreement
        holds only while the partition fits in one 1 MB page. That read is #2 and
        outside D6's three; it is recorded in the handler docstring.
        """
        seed_pipeline_behind_a_full_window(list_tables)

        entry = one_entry()

        assert entry["file_count"] == 3, "the unbounded COUNT read sees all three"
        assert len(entry["all_files"]) == entry["file_count"], (
            "the preview and the count describe the same rows and must agree — D5 measured 0 against 3"
        )
        assert [f["file_key"] for f in entry["all_files"]] == ["old-2.csv", "old-1.csv", "old-0.csv"], (
            "the three newest matching files, from beyond the 50-row first page"
        )
        assert entry["partial_fields"] == [], "the walk completed, so nothing is a prefix"

    def test_mapping_count_reads_seven_for_a_pipeline_behind_a_full_window(self, list_tables):
        """**DEFECT 1, FIXED by D6.** Correct is ``7``; D5 measured ``0``.

        **What D5 measured:** the ``schema_mapping`` row carries ``mapping_count: 7``
        and sits at position 61 of the pipeline's index partition, so ``Limit=50``
        never reached it and the entry reported ``0`` — indistinguishable from "no
        mappings". Unlike ``all_files`` there was no second read to contradict it,
        which made it the worse of the two: the wrong value was a plausible one.
        ``use-pipelines.ts:100`` passes it through, so it was a rendered zero.

        **What D6 changed:** ``get_pipeline_mapping_count`` follows
        ``LastEvaluatedKey`` under a page cap and a deadline, so the row is found on
        the second page. The oracle query is kept — the value's correctness should
        not rest on a literal written in this test.

        **The second flaw D5 found is fixed by the same change.** The read took
        ``Items[0]``, the newest match *inside the window*, and called it "the latest
        schema mapping step" — so even a non-empty answer was only as fresh as the
        window. The walk is newest-first and stops at the first page carrying a
        match, so ``Items[0]`` is now the newest matching row in the partition. That
        is asserted directly, by seeding a *newer* mapping row than the one behind
        the window and requiring the newer count to win.
        """
        seed_pipeline_behind_a_full_window(list_tables)

        oracle = list_tables["state_table"].query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq("pipe-1"),
            FilterExpression=Attr("step_name").eq("schema_mapping"),
        )
        assert oracle["Count"] == 1, "the fixture must contain exactly the row that used to be missed"
        assert int(oracle["Items"][0]["mapping_count"]) == 7

        entry = one_entry()

        assert entry["mapping_count"] == 7, (
            "the walk reaches position 61 — D5 measured 0 here, which was indistinguishable from a "
            "pipeline with no mappings"
        )
        assert entry["partial_fields"] == [], "the walk found its answer, so the count is a total"

    def test_the_newest_mapping_row_wins_even_when_an_older_one_is_behind_the_window(self, list_tables):
        """The second half of defect 1: ``Items[0]`` must be the newest match overall.

        D5 named this as a flaw the truncation hid — "the newest match inside the
        window need not be the newest overall". Seeded both ways round: a
        ``mapping_count: 99`` row *inside* the first page and the original
        ``mapping_count: 7`` row beyond it. The newest of the two is the one inside,
        so the walk must stop on page one and report 99. A read that paginated to
        the end and took the last match would report 7 and pass the previous test.
        """
        seed_pipeline_behind_a_full_window(list_tables)
        list_tables["state_table"].put_item(
            Item=state_row("sm-newest", "schema_mapping", "pipe-1", "2026-07-01T00:00:00Z", mapping_count=99)
        )

        entry = one_entry()

        assert entry["mapping_count"] == 99, "the newest schema_mapping row, not the newest reachable one"

    def test_both_reads_are_correct_when_the_matches_fit_inside_the_window(self, list_tables):
        """The control. Same population minus the fillers: both read correctly.

        Written by D5 so the two ``DEFECT`` tests could not be read as the reads
        being unconditionally broken — the fault was the unfollowed
        ``LastEvaluatedKey``, not the query. **It is unamended after D6**, which is
        the point of having it: it states the behaviour the fix had to preserve
        rather than merely move, and a fix that traded the truncated case for the
        untruncated one would fail here.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row(
                    f"fv-{n}",
                    "file_validation",
                    "pipe-1",
                    f"2026-01-01T00:{n:02d}:00Z",
                    status="success",
                    original_s3_uri=f"s3://bucket/incoming/{n}.csv",
                )
                for n in range(3)
            ]
            + [state_row("sm-1", "schema_mapping", "pipe-1", "2026-01-01T00:00:00Z", mapping_count=7)],
        )

        entry = one_entry()

        assert entry["file_count"] == 3
        assert len(entry["all_files"]) == 3
        assert entry["mapping_count"] == 7

    def test_the_preview_stops_at_three_files_despite_asking_for_five(self, list_tables):
        """``Limit=50  # Scan up to 50 items to find 5 matching files`` yields **three**.

        The comment at ``:276`` says "last 5", the ``Limit``'s own comment says "find 5
        matching files", and the loop breaks at ``len(recent_files) >= 3`` (``:295``).
        Three is what a client gets. Pinned because it is the kind of disagreement that
        gets "fixed" toward the comment during a rewrite, which would change the
        payload size on a hot path.

        Also pinned: the newest three, in descending order, since the break makes the
        result order-dependent on ``ScanIndexForward=False``.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row(
                    f"fv-{n}",
                    "file_validation",
                    "pipe-1",
                    f"2026-01-01T00:{n:02d}:00Z",
                    status="success",
                    original_s3_uri=f"s3://bucket/incoming/{n}.csv",
                )
                for n in range(8)
            ],
        )

        entry = one_entry()

        assert entry["file_count"] == 8
        assert [f["file_key"] for f in entry["all_files"]] == ["7.csv", "6.csv", "5.csv"], (
            "three newest, not five — the break at :295 wins over both comments"
        )

    def test_a_folder_marker_row_is_skipped_but_still_counted(self, list_tables):
        """``file_key and not file_key.endswith("/")`` (``:291``) excludes it from the preview.

        The row still counts toward ``file_count``, because that read has no such
        predicate — a second, smaller instance of the same count-versus-list
        divergence, and one that is *deliberate* rather than a truncation artifact.
        Pinned so the two are not confused when D6 reconciles them: this asymmetry is
        intended, the ``Limit`` one is not.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row(
                    "fv-marker",
                    "file_validation",
                    "pipe-1",
                    "2026-01-01T00:01:00Z",
                    status="success",
                    original_s3_uri="s3://bucket/incoming/folder/",
                ),
                state_row(
                    "fv-real",
                    "file_validation",
                    "pipe-1",
                    "2026-01-01T00:00:00Z",
                    status="success",
                    original_s3_uri="s3://bucket/incoming/real.csv",
                ),
            ],
        )

        entry = one_entry()

        assert entry["file_count"] == 2, "both rows match the count read's filter"
        assert [f["file_key"] for f in entry["all_files"]] == ["real.csv"], "the folder marker is skipped"


# ═══════════════════════════════════════════════════════════════════════════
# 6. get_pipeline_schema_history's page walk — D7's target
# ═══════════════════════════════════════════════════════════════════════════
class TestSchemaHistoryPageWalk:
    """The list view asks for 3 versions and — since D7 — reads 3 versions' worth.

    R8.3 / D7. D5 pinned the walk paging until ``LastEvaluatedKey`` was absent or
    ``max_pages = 10`` was reached, de-duplicating, sorting, and **then** slicing to
    the caller's ``limit``, so ``limit=3`` bounded the output and not the read. Four
    round-trips for three items, measured.

    **D7 stopped the walk at three distinct hashes instead**, and the diff lands in
    ``test_satisfying_a_three_version_request_costs_one_round_trip`` below —
    ``4 → 1``, on the same population, with D5's counterfactual kept beside it.

    The unbounded walk is untouched for the other caller (design §2.6 —
    "keep the existing bound for other callers; do not remove ``max_pages``"), which
    is why the ``max_pages = 10`` source pin further down is unamended.

    **The section's other half is now the version numbers**, because capping the walk
    is what put them at risk. D5's DEFECT #3 said D7 "must keep a count read or state
    that version numbers become relative"; a count read cannot answer a de-duplicated
    count, so the numbers are relative when the walk could not establish the total,
    ``schema_version`` reports the newest entry's own version rather than a page
    length, and the marker says which. Each of those three is asserted below.
    """

    @staticmethod
    def _seed_versions(tables: dict[str, Any], count: int, *, oversized: bool) -> None:
        """``count`` distinct schema versions, optionally ~300 KB each.

        Oversized rows are how a real multi-page walk is produced: ``PipelineIndex``
        projects enough to carry ``extracted_schema``, so four of these cross the 1 MB
        response ceiling and DynamoDB genuinely returns a ``LastEvaluatedKey``. A
        stubbed pager could fake the boundary but not the relationship between page
        count and payload size, which is what D7 is trading against.
        """
        tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        filler = "x" * 300_000 if oversized else ""
        seed(
            tables["state_table"],
            [
                state_row(
                    f"run-{n}",
                    "schema_extract",
                    "pipe-1",
                    f"2026-01-{n + 1:02d}T00:00:00Z",
                    schema_hash=f"hash-{n:03d}",
                    json_s3_uri=f"s3://bucket/schemas/{n}.json",
                    extracted_schema=filler,
                )
                for n in range(count)
            ],
        )

    def test_the_list_view_requests_three_versions(self, list_tables):
        """``get_pipeline_schema_history(pipeline_id, limit=3)`` (``:255``).

        Asserted through the response — twelve versions in, three out — rather than by
        reading the call, because that is what a client observes. The unbounded walk's
        default is 10, so the 3 is the list view's own choice and the figure R8.3
        argues from. **D7 made it the walk's stopping condition as well as the slice
        width** (``_SCHEMA_HISTORY_PREVIEW_LIMIT``), so the two can no longer drift
        apart — but the observable asserted here is unchanged.
        """
        self._seed_versions(list_tables, 12, oversized=False)

        entry = one_entry()

        assert len(entry["schema_history"]) == 3
        assert [h["hash"] for h in entry["schema_history"]] == ["hash-009", "hash-010", "hash-011"], (
            "the three newest, oldest-first within the slice"
        )

    def test_satisfying_a_three_version_request_costs_one_round_trip(self, list_tables):
        """**D7's measurement, after.** Twelve oversized versions ⇒ one page, three items.

        **Amended from D5's ``…_costs_four_round_trips``, and the amendment is the
        point of the task** (``LGR-60c4985-6`` — the change stays legible as a
        deliberate reversal rather than reading as a regression). D5 asserted 4 on
        this population, with "every page after the first carries an
        ``ExclusiveStartKey``" and "the walk ran to exhaustion, not to the cap". Both
        of those described the defect; neither is true now, so both are replaced
        rather than relaxed:

        * the count is **1**, and asserted exactly — the whole claim is that the walk
          stops once it has the three versions it was asked for, and "at most four"
          would pass on the unfixed code;
        * **no** page carries an ``ExclusiveStartKey``, since there is only one;
        * the single page **does** carry a ``LastEvaluatedKey`` — that is what makes
          this a genuine early stop rather than a smaller population. D5's
          exhaustion assertion is inverted here, deliberately.

        The counterfactual at the old bound is measured in
        ``test_the_unbounded_walk_still_costs_four_round_trips_on_the_same_population``
        below, so the 4 → 1 improvement is a measurement on one population rather than
        a claim about two.

        Still asserted as three items returned: capping the read must not change the
        answer's width.
        """
        self._seed_versions(list_tables, 12, oversized=True)

        with recorded_dynamodb_calls() as recorded:
            entry = one_entry()

        pages = classify_reads(recorded)["schema_history"]
        assert len(pages) == 1, (
            f"the walk took {len(pages)} pages to answer a limit=3 request; D5 measured 4 here and D7 caps it at "
            f"the first page that carries three distinct hashes"
        )
        assert [p for p, _ in pages if "ExclusiveStartKey" in p] == [], (
            "one page means no continuation — D5's 'every page but the first' assertion is gone with the pages"
        )
        assert pages[0][1].get("LastEvaluatedKey"), (
            "the partition was NOT exhausted — which is what makes this an early stop rather than a small seed"
        )
        assert len(entry["schema_history"]) == 3, "one page read, three items returned"

    def test_the_unbounded_walk_still_costs_four_round_trips_on_the_same_population(self, list_tables):
        """The counterfactual: D5's figure, still reproducible at the old bound.

        Same seed, same instrument, calling ``get_pipeline_schema_history`` — the
        entry point R8.3 and design §2.6 both say to leave alone — instead of the
        list view's. It still walks to exhaustion in four pages, which is what makes
        "4 → 1" a measured improvement on one population rather than two numbers from
        two runs.

        It also pins the constraint itself: if a later change tightens the unbounded
        walk too, this fails and says so. D6 measured its own fix the same way.
        """
        self._seed_versions(list_tables, 12, oversized=True)

        with recorded_dynamodb_calls() as recorded:
            history = crud.get_pipeline_schema_history("pipe-1", limit=3)

        pages = classify_reads(recorded)["schema_history"]
        assert len(pages) == 4, f"the unbounded walk took {len(pages)} pages; D5 measured 4 and D7 must not change it"
        assert sum(1 for params, _ in pages if "ExclusiveStartKey" in params) == len(pages) - 1, (
            "every page but the first continues the previous one — one walk, not four reads"
        )
        assert not pages[-1][1].get("LastEvaluatedKey"), "the unbounded walk still runs to exhaustion"
        assert [h["hash"] for h in history] == ["hash-009", "hash-010", "hash-011"], (
            "and it returns the same three versions the bounded walk does — only the reading differs"
        )

    def test_the_bounded_walk_returns_the_same_versions_as_the_unbounded_one(self, list_tables):
        """Capping the read changes the numbering and nothing else about the slice.

        The property the cap rests on: the walk is newest-first and de-duplicates on
        first occurrence, so the first three distinct hashes are the three hashes with
        the newest occurrences — exactly what "sort ascending, take the last three"
        selects. Asserted against the unbounded walk on one population rather than
        against a transcribed expectation, and asserted for ``hash``, ``timestamp``
        and ``extracted_schema`` so a cap that silently dropped a field would fail
        here.

        The seed includes a duplicate of the **oldest** hash at the newest timestamp,
        which is the case where "first N distinct newest-first" and "last N by
        timestamp" could diverge if the de-duplication kept the wrong occurrence.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row("run-1", "schema_extract", "pipe-1", "2026-01-01T00:00:00Z", schema_hash="hash-a"),
                state_row("run-2", "schema_extract", "pipe-1", "2026-01-02T00:00:00Z", schema_hash="hash-b"),
                state_row("run-3", "schema_extract", "pipe-1", "2026-01-03T00:00:00Z", schema_hash="hash-c"),
                state_row("run-4", "schema_extract", "pipe-1", "2026-01-04T00:00:00Z", schema_hash="hash-d"),
                state_row("run-5", "schema_extract", "pipe-1", "2026-01-05T00:00:00Z", schema_hash="hash-a"),
            ],
        )

        bounded, absolute = crud.get_pipeline_schema_history_preview("pipe-1", deadline=float("inf"), limit=3)
        logs_handler._cache.clear()
        unbounded = crud.get_pipeline_schema_history("pipe-1", limit=3)

        assert absolute, "one small page exhausts this partition, so the numbers are real"
        assert bounded == unbounded, "the bounded walk returned a different slice, not just a different numbering"

    def test_no_page_of_the_walk_carries_a_limit(self, list_tables):
        """The caller's ``limit`` never reaches DynamoDB — ``query_kwargs`` has no ``Limit``.

        ``:132-137`` builds the kwargs and ``:151`` adds only ``ExclusiveStartKey``. So
        the walk cannot be shortened by the caller today, which is precisely why R8.3
        is a code change rather than a parameter change.

        Stated as the counterpart to §5: the two ``Limit=50`` reads truncate silently
        because they bound and do not paginate; this read is complete because it
        paginates and does not bound. Neither is the shape R8 wants.
        """
        self._seed_versions(list_tables, 12, oversized=True)

        with recorded_dynamodb_calls() as recorded:
            call_pipelines()

        for params, _response in classify_reads(recorded)["schema_history"]:
            assert "Limit" not in params
        assert "max_pages = 10" in HANDLER_SOURCE, (
            "the ceiling R8.3 tightens is no longer spelled this way — update the pin, do not drop it"
        )

    def test_schema_version_is_the_newest_versions_number_not_the_page_length(self, list_tables):
        """**DEFECT 3, FIXED by D7.** Twelve versions ⇒ ``schema_version: 12``.

        **Amended from D5's ``…_is_the_returned_page_length_not_the_version_number``,
        which asserted ``3`` and marked it ``DEFECT``**, with the reason kept here per
        ``LGR-60c4985-6``.

        **What D5 measured:** ``schema_version`` was ``len(schema_history)`` computed
        *after* the slice to three, so a pipeline with twelve versions reported ``3``
        and every pipeline past its third revision reported ``3`` for ever. The true
        figure sat in the same payload — ``schema_history[-1]["version"]`` read ``12``,
        assigned from ``total_versions`` — so the key's own neighbour disproved it.
        ``use-pipelines.ts:82`` reads ``raw.schema_version ?? schemaHistory.length``,
        making both candidate values the same wrong number, so the fallback hid
        nothing.

        **What D7 did:** ``schema_version`` is now that neighbour —
        ``schema_history[-1]["version"]`` — so the two numbers agree by construction
        instead of contradicting each other. Both are asserted, and their *equality*
        is asserted, because the contradiction is the thing that was wrong.

        **Why this population is fixed rather than merely marked.** The rows here are
        small (``oversized=False``), so all twelve fit in one 1 MB page and the walk
        exhausts the partition on page one — ``absolute`` is True, ``total_versions``
        is real, and ``partial_fields`` is empty. That is the common case. The
        multi-page case, where the cap does cost the absolute numbers, is
        ``test_a_capped_walk_reports_relative_versions_and_says_so`` below.
        """
        self._seed_versions(list_tables, 12, oversized=False)

        entry = one_entry()

        assert entry["schema_version"] == 12, (
            "D5 measured 3 here and pinned it as a DEFECT — the value was a page length after the limit=3 slice"
        )
        assert entry["schema_history"][-1]["version"] == 12, "unchanged: the figure that was already correct"
        assert entry["schema_version"] == entry["schema_history"][-1]["version"], (
            "the two numbers in the payload must not be able to disagree again"
        )
        assert entry["current_schema_hash"] == "hash-011", "the newest hash was always right"
        assert entry["partial_fields"] == [], "one page exhausted the partition, so nothing here is a floor"

    def test_a_capped_walk_reports_relative_versions_and_says_so(self, list_tables):
        """The cost of D7's cap, marked rather than hidden — D5's own second arm.

        D5's DEFECT #3 ended "D7 must either keep a count read or state that the
        version numbers become relative". A count read cannot keep them (see
        ``test_a_count_read_cannot_answer_the_deduplicated_version_count`` below), so
        this is what the second arm looks like on the wire: the three newest versions
        are still correct and still the right three, their ``version`` numbers are
        ordinals within what was read, ``schema_version`` is a floor, and
        ``partial_fields`` names both keys.

        Twelve **oversized** versions, so the partition genuinely needs more than one
        page and the walk stops with the total unknown. Contrast the small-row
        population one test up, which is fixed outright.

        The marker is what makes this acceptable under ``LGR-b2f79ef-14``: an
        absolute-looking number that is actually relative is the same
        "could not check reported as checked" failure as a floor reported as a total.
        ``schema_history`` is named beside ``schema_version`` because it is where the
        ``version`` numbers live, and a derived value is marked when its inputs are —
        D6's rule, applied to a second pair of keys.
        """
        self._seed_versions(list_tables, 12, oversized=True)

        entry = one_entry()

        assert entry["partial_fields"] == ["schema_version", "schema_history"], (
            "the walk could not establish the total, and neither key may be presented as if it had"
        )
        assert [h["hash"] for h in entry["schema_history"]] == ["hash-009", "hash-010", "hash-011"], (
            "the right three versions — the cap costs the numbering, not the selection"
        )
        assert [h["version"] for h in entry["schema_history"]] == [1, 2, 3], (
            "ordinals within what was read; the real numbers are 10, 11, 12 and this walk cannot know that"
        )
        assert entry["schema_version"] == 3, "a floor — at least three versions exist — and partial_fields says so"
        assert entry["current_schema_hash"] == "hash-011", "unaffected: the newest hash needs no total"

    def test_a_relative_answer_is_not_cached(self, list_tables):
        """D6's rule, applied to read #1: only a walk that established the total caches.

        Without this, one capped walk would pin relative numbering into a 300 s
        per-container dict, and a hit would need marker logic to stay honest — which
        is exactly the complexity D6 avoided by never caching a partial answer. The
        cost is one page per request for a pipeline with a multi-page schema history,
        which is the whole point of the cap.

        Driven by calling twice and counting: a cached relative answer would issue no
        second read.
        """
        self._seed_versions(list_tables, 12, oversized=True)

        first = one_entry()
        assert first["partial_fields"] == ["schema_version", "schema_history"]

        with recorded_dynamodb_calls() as recorded:
            second = one_entry()

        assert len(classify_reads(recorded)["schema_history"]) == 1, (
            "the relative answer was cached — a hit would then serve ordinals as if they were version numbers"
        )
        assert second == first, "and the re-read must produce the same answer it did the first time"

    def test_a_count_read_cannot_answer_the_deduplicated_version_count(self, list_tables):
        """Why D7 took the second arm: the evidence that killed the count read.

        D5 offered a ``Select="COUNT"`` read as the way to keep absolute numbers under
        a capped walk. It cannot, and this is measured against moto's own query engine
        rather than argued, because it is the load-bearing premise of D7's decision.

        Two independent failures, one test each half:

        1. **It counts rows, not versions.** Four extract rows sharing one
           ``schema_hash`` are one version — ``test_duplicate_hashes_collapse_to_one_version``
           already pins that — and a COUNT over them returns ``4``. DynamoDB has no
           distinct-count, so the number is not the one the oracle needs.
        2. **It is paginated by the same ceiling it was meant to dodge.** The 1 MB
           limit applies to data *scanned*, before ``Select`` and before
           ``FilterExpression``, so a COUNT over D5's twelve oversized versions
           returns a ``LastEvaluatedKey`` — the same four round-trips, plus the walk's.

        Asserted here rather than in the handler's docstring alone so that a future
        reader who wants to "just add a count read" finds the measurement instead of
        the assertion that it was considered.
        """
        # (1) rows are not versions
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row(f"run-{n}", "schema_extract", "pipe-1", f"2026-01-0{n + 1}T00:00:00Z", schema_hash="hash-a")
                for n in range(4)
            ],
        )
        counted = list_tables["state_table"].query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq("pipe-1"),
            FilterExpression=Attr("step_name").eq("schema_extract") & Attr("schema_hash").exists(),
            Select="COUNT",
        )
        assert counted["Count"] == 4
        assert one_entry()["schema_version"] == 1, (
            "four rows, one version — so a row count is not a version count and cannot restore total_versions"
        )

        # (2) and the count read is itself multi-page on the population D7 caps
        logs_handler._cache.clear()
        self._seed_versions(list_tables, 12, oversized=True)
        counted_oversized = list_tables["state_table"].query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq("pipe-1"),
            FilterExpression=Attr("step_name").eq("schema_extract") & Attr("schema_hash").exists(),
            Select="COUNT",
        )
        assert counted_oversized.get("LastEvaluatedKey"), (
            "a COUNT over this partition stopped early too — it costs the round-trips it was supposed to save"
        )

    def test_versions_are_numbered_from_the_full_history_not_from_the_slice(self, list_tables):
        """``start_version = total_versions - len(recent) + 1``.

        Five versions sliced to three are numbered 3, 4, 5 — not 1, 2, 3. This is the
        behaviour the previous tests rely on for their oracle, and D5 pinned it in its
        own right because D7's cap put it at risk.

        **Unamended, and that is the finding.** These five rows are small, so one page
        exhausts the partition and the walk reports ``absolute`` — the arithmetic is
        untouched and the numbers stay real. D7's cap only changes the numbering when
        the partition needs more than one page, which is the case
        ``test_a_capped_walk_reports_relative_versions_and_says_so`` covers.
        """
        self._seed_versions(list_tables, 5, oversized=False)

        entry = one_entry()

        assert [h["version"] for h in entry["schema_history"]] == [3, 4, 5]

    def test_duplicate_hashes_collapse_to_one_version(self, list_tables):
        """Dedup by ``schema_hash``, newest occurrence kept.

        Four rows across three distinct hashes give three versions numbered 1..3, so
        ``schema_version`` counts *unique schemas* rather than extract runs. D5 pinned
        it because a capped walk sees fewer rows and can therefore find fewer
        duplicates, changing the numbering for a reason unrelated to the cap.

        **It is also the assertion that killed D7's count-read option**, and is cited
        as such by ``test_a_count_read_cannot_answer_the_deduplicated_version_count``:
        a ``Select="COUNT"`` over this population returns 4 where the version count is
        3, so no count read can restore ``total_versions`` for a de-duplicating walk.
        Unamended — one small page exhausts this partition, so the numbers stay
        absolute.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row("run-1", "schema_extract", "pipe-1", "2026-01-01T00:00:00Z", schema_hash="hash-a"),
                state_row("run-2", "schema_extract", "pipe-1", "2026-01-02T00:00:00Z", schema_hash="hash-a"),
                state_row("run-3", "schema_extract", "pipe-1", "2026-01-03T00:00:00Z", schema_hash="hash-b"),
                state_row("run-4", "schema_extract", "pipe-1", "2026-01-04T00:00:00Z", schema_hash="hash-c"),
            ],
        )

        entry = one_entry()

        assert [h["hash"] for h in entry["schema_history"]] == ["hash-a", "hash-b", "hash-c"]
        assert [h["version"] for h in entry["schema_history"]] == [1, 2, 3]
        assert entry["schema_version"] == 3, "three unique schemas from four extract rows"

    def test_a_row_without_a_schema_hash_is_excluded_by_the_filter(self, list_tables):
        """``Attr("schema_hash").exists()`` (``:135``) — a partial extract row is not a version.

        The near-miss for this read, seeded alongside a real version so the filter is
        shown to discriminate rather than to reject everything.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))
        seed(
            list_tables["state_table"],
            [
                state_row("run-1", "schema_extract", "pipe-1", "2026-01-01T00:00:00Z", schema_hash="hash-a"),
                state_row("run-2", "schema_extract", "pipe-1", "2026-01-02T00:00:00Z"),
            ],
        )

        entry = one_entry()

        assert [h["hash"] for h in entry["schema_history"]] == ["hash-a"]
        assert entry["schema_version"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 7. What a stopped walk reports — D6's bound, and the marker that names it
# ═══════════════════════════════════════════════════════════════════════════
class TestPartialFields:
    """The other half of D6: the two walks are bounded, and a bound is never silent.

    §5 shows the walks reach the answer. This section shows what happens when they
    are not allowed to — because "paginate until you find a match" on its own would
    reintroduce the unbounded read R8 exists to remove, and because ``0`` for
    "could not establish" is the same defect D6 was sent to fix, one layer along
    (``LGR-b2f79ef-14``: an unmarked "could not check" reported as "checked and
    found none").

    Both bounds are driven by **shortening the budget or the cap**, never by
    asserting a wall-clock duration — the convention ``stats.py`` and
    ``schemas.py`` both state at their own deadline constants. Either bound must
    produce the same observable outcome, so both are tested: a wrong value is not
    what is being ruled out here, an *unlabelled* one is.
    """

    def test_the_deadline_stops_the_walk_and_both_reads_say_so(self, list_tables, monkeypatch):
        """A budget of zero: the first page runs, the walk stops, two keys are named.

        The deadline is checked after the ``LastEvaluatedKey`` test, so a walk that
        finishes naturally is complete even past its deadline — D4's shape, kept, and
        the reason the full-window fixture is needed to make this bind at all.

        The payload here is the *honest* form of the contradiction D5 measured:
        ``file_count: 3`` beside ``all_files: []``, except that ``all_files`` now
        arrives with a label saying it is a prefix rather than an answer.
        """
        monkeypatch.setattr(crud, "_LIST_TIME_BUDGET_SECONDS", 0.0)
        seed_pipeline_behind_a_full_window(list_tables)

        entry = one_entry()

        assert entry["partial_fields"] == ["mapping_count", "all_files"], (
            "a bound stopped both walks and neither value may be reported as a total"
        )
        assert entry["mapping_count"] == 0, "0 as a floor — `partial_fields` is what says so"
        assert entry["all_files"] == []
        assert entry["file_count"] == 3, "the unbounded COUNT read is unaffected by the deadline"
        assert set(entry) == set(ENRICHED_KEYS), "a stopped walk degrades a field, never the entry's shape"

    def test_the_page_cap_stops_the_walk_and_reports_the_same_way(self, list_tables, monkeypatch):
        """One page allowed, with the deadline far away: the cap is the backstop.

        Two bounds, one observable outcome. The cap exists because the deadline
        cannot bound a *single* pathological page, and it is asserted separately so a
        change that removed one of them would surface here rather than in whichever
        production request hit the other.
        """
        monkeypatch.setattr(crud, "_MAX_ENRICHMENT_READ_PAGES", 1)
        seed_pipeline_behind_a_full_window(list_tables)

        with recorded_dynamodb_calls() as recorded:
            entry = one_entry()

        reads = classify_reads(recorded)
        assert len(reads["mapping_count"]) == 1, "the cap allowed exactly one page"
        assert len(reads["recent_files"]) == 1
        assert entry["partial_fields"] == ["mapping_count", "all_files"]
        assert (entry["mapping_count"], entry["all_files"]) == (0, [])

    def test_a_stopped_walk_is_not_cached(self, list_tables, monkeypatch):
        """The rule that keeps a transient bound from lasting 300 s.

        D3 established that a degraded payload is not cached and D4 generalised it to
        anything carrying a marker; D6 applies the same rule per read. Without it, one
        throttled or slow request would pin a wrong ``mapping_count`` into a
        per-container dict for a full TTL — turning the one-request misreport D5
        measured into a sustained one, which is the argument this whole task rests on.

        Driven by binding the deadline, then lifting it: the second request must
        re-read and get the right answer. A cached floor would make it read 0 again
        and issue no query.
        """
        monkeypatch.setattr(crud, "_LIST_TIME_BUDGET_SECONDS", 0.0)
        seed_pipeline_behind_a_full_window(list_tables)

        first = one_entry()
        assert first["partial_fields"] == ["mapping_count", "all_files"]

        monkeypatch.setattr(crud, "_LIST_TIME_BUDGET_SECONDS", 20.0)
        with recorded_dynamodb_calls() as recorded:
            second = one_entry()

        reads = classify_reads(recorded)
        assert len(reads["mapping_count"]) > 1, "the floor was not cached, so the walk ran again"
        assert second["mapping_count"] == 7
        assert len(second["all_files"]) == 3
        assert second["partial_fields"] == []

    def test_a_completing_walk_over_an_empty_partition_is_not_marked(self, list_tables):
        """``0`` from an exhausted partition is a **total**, and must not be marked.

        The distinction the marker exists to carry, asserted from the other side: if
        every zero were marked the marker would say nothing, and if no zero were
        marked it would be the defect again. A pipeline with no state rows at all
        reads its partition to the end, so its zeros are answers.
        """
        list_tables["pipelines_table"].put_item(Item=pipeline_record("pipe-1"))

        entry = one_entry()

        assert (entry["mapping_count"], entry["all_files"]) == (0, [])
        assert entry["partial_fields"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 8. The page read's bound, and the envelope marker that names it — D8
# ═══════════════════════════════════════════════════════════════════════════
def seed_oversized_pipelines(tables: dict[str, Any], count: int) -> list[str]:
    """``count`` pipeline records padded so the set exceeds one 1 MB scan page.

    The padding is what makes these tests non-vacuous: without it the page read
    completes in one call and neither bound can be observed. ~300 KB each, so four
    records already cross the ceiling. ``updated_at`` ascends with the index, so the
    expected order is always ``reversed(range(count))``.

    Returns the seeded ids in **descending ``updated_at`` order** — the order the
    listing must produce.
    """
    filler = "x" * 300_000
    for n in range(count):
        tables["pipelines_table"].put_item(
            Item=pipeline_record(f"pipe-{n:02d}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z", filler=filler)
        )
    return [f"pipe-{n:02d}" for n in reversed(range(count))]


class TestPageReadBound:
    """The page read paginates, is bounded twice, and never truncates silently.

    §4 pinned the old shape: one ``Scan``, ``LastEvaluatedKey`` discarded, a table
    over 1 MB reduced to whatever fit — with ``has_more: False`` claiming that was
    everything. That is fixed in ``TestReadCount`` above. This section is the other
    half, and the same argument D6's §7 makes one layer along: a walk that follows
    ``LastEvaluatedKey`` needs a bound, and a bound that reports a floor as a total
    is the defect moved rather than removed (``LGR-b2f79ef-14``).

    Both bounds are driven by **shortening the budget or the cap**, never by
    asserting a wall-clock duration — the convention every deadline constant in this
    tree states. Both must produce the same observable outcome, so both are tested.
    """

    def test_the_page_scan_deadline_stops_the_walk_and_the_envelope_says_so(self, list_tables, monkeypatch):
        """A page-scan share of zero: one page runs, and all four derived keys are named.

        ``_PAGE_SCAN_TIME_SHARE_SECONDS`` is shortened rather than the whole budget,
        so enrichment keeps its window — which is the point of the split and is
        asserted here by the entries arriving fully enriched beside a partial
        envelope.

        ``pipelines`` is in the marker, not only the counts. A truncated read makes
        the page a *selection over a subset*: the entries returned are the newest of
        what was read, which is not necessarily the newest that exist.
        """
        monkeypatch.setattr(crud, "_PAGE_SCAN_TIME_SHARE_SECONDS", 0.0)
        seed_oversized_pipelines(list_tables, 4)

        with recorded_dynamodb_calls() as recorded:
            status, body = call_pipelines()

        assert status == 200
        assert len(classify_reads(recorded)["page_scan"]) == 1, "the deadline allowed exactly one page"
        assert body["partial_counts"] == ["pipelines", "total_count", "total_pages", "has_more"], (
            "every value derived from a prefix of the filtered set must be named"
        )
        assert body["total_count"] < 4, "a floor — `partial_counts` is what says so"
        assert body["pipelines"], "the page still answers with what it read"
        for entry in body["pipelines"]:
            assert entry["partial_fields"] == [], (
                "the page read's bound must not consume enrichment's window — that is why the "
                "page scan has a share of the budget rather than the enrichment deadline"
            )

    def test_the_page_scan_page_cap_stops_the_walk_and_reports_the_same_way(self, list_tables, monkeypatch):
        """One page allowed, deadline far away: the cap is the backstop.

        Two bounds, one observable outcome. The cap exists because a deadline cannot
        bound a *single* pathological page, and it is asserted separately so removing
        either one surfaces here rather than in whichever request met the other.
        """
        monkeypatch.setattr(crud, "_MAX_PAGE_SCAN_PAGES", 1)
        seed_oversized_pipelines(list_tables, 4)

        with recorded_dynamodb_calls() as recorded:
            _status, body = call_pipelines()

        assert len(classify_reads(recorded)["page_scan"]) == 1, "the cap allowed exactly one page"
        assert body["partial_counts"] == ["pipelines", "total_count", "total_pages", "has_more"]
        assert body["total_count"] < 4

    def test_the_page_cap_is_its_own_constant_and_not_the_enrichment_one(self, list_tables, monkeypatch):
        """The two caps bound different resources and must not be one constant.

        ``_MAX_ENRICHMENT_READ_PAGES`` bounds round-trips *per pipeline* and is paid
        up to ``page_size`` times; ``_MAX_PAGE_SCAN_PAGES`` bounds bytes accumulated
        in one list before the sort. They hold the same number today for unrelated
        reasons, which is exactly the situation in which sharing a constant lets a
        later change to one silently move the other. Asserted by shortening the
        enrichment cap and observing that the page read is unaffected.
        """
        monkeypatch.setattr(crud, "_MAX_ENRICHMENT_READ_PAGES", 1)
        seed_oversized_pipelines(list_tables, 4)

        _status, body = call_pipelines()

        assert body["partial_counts"] == [], "the enrichment cap must not bound the page read"
        assert body["total_count"] == 4

    def test_the_page_scan_deadline_is_a_share_that_leaves_enrichment_a_window(self):
        """The scan's deadline is strictly earlier than enrichment's, by a stated margin.

        A pure arithmetic assertion on the two derivations rather than a timing test.
        Handed ``_list_read_deadline``'s instant, one slow page walk could spend the
        whole budget and turn every entry on the page partial for want of time rather
        than for want of data — so the ordering of these two instants is the property,
        and it is asserted from the same anchor both are derived from.
        """
        anchor = 1000.0

        scan_deadline = crud._page_scan_deadline(anchor)
        enrichment_deadline = crud._list_read_deadline(anchor)

        assert scan_deadline < enrichment_deadline, "the page read must not be able to starve enrichment"
        assert scan_deadline == anchor + crud._PAGE_SCAN_TIME_SHARE_SECONDS
        assert enrichment_deadline - scan_deadline == pytest.approx(
            crud._LIST_TIME_BUDGET_SECONDS - crud._LIST_READ_RESERVE_SECONDS - crud._PAGE_SCAN_TIME_SHARE_SECONDS
        )

    def test_the_two_markers_are_independent_and_share_no_key_name(self, list_tables, monkeypatch):
        """``partial_counts`` is the envelope's, ``partial_fields`` is an entry's.

        The disjointness is what lets a client read either without knowing which
        level it came from, so it is asserted rather than left to inspection. The
        independence is asserted in the direction the other tests do not cover: a
        **complete** page read carrying entries whose enrichment was bounded.
        """
        assert "partial_counts" not in ENRICHED_KEYS, "the two vocabularies must not share a key name"
        assert "partial_fields" not in ENVELOPE_KEYS
        assert set(ENVELOPE_KEYS).isdisjoint(ENRICHED_KEYS), (
            "the envelope and an entry share no key name at all, so a marked key names its own level"
        )

        monkeypatch.setattr(crud, "_MAX_ENRICHMENT_READ_PAGES", 1)
        seed_pipeline_behind_a_full_window(list_tables)

        _status, body = call_pipelines()

        assert body["partial_counts"] == [], "the page read completed"
        assert body["pipelines"][0]["partial_fields"] == ["mapping_count", "all_files"], (
            "while this entry's enrichment did not — the two markers move independently"
        )

    @pytest.mark.parametrize("page_size", [1, 2, 3, 4, 7])
    def test_the_partition_property_survives_a_multi_page_page_read(self, list_tables, page_size):
        """The pager is still a partition when the page read spans several scan pages.

        D5 asserted this over a table that fitted in one page and named the hazard it
        rules out: "two bounded reads that each return a correct-looking page can
        still drop or duplicate a record at the boundary". Now that the page read is
        itself several reads, that hazard is real rather than hypothetical — a walk
        that mishandled ``ExclusiveStartKey`` would lose or repeat a record at a
        *scan* page boundary, and every individual page of results would still look
        plausible.

        Swept across page sizes rather than fixed at one, because the two boundaries
        are independent: the scan's page breaks fall wherever DynamoDB puts them, and
        the pager's fall on multiples of ``page_size``. The union must be the full
        descending order at every one of them.
        """
        expected = seed_oversized_pipelines(list_tables, 7)

        walked: list[str] = []
        total_pages = None
        for page in range(1, (7 // page_size) + 2):
            logs_handler._cache.clear()
            _status, body = call_pipelines(page=page, page_size=page_size)
            assert body["total_count"] == 7, "every page agrees on the total"
            assert body["partial_counts"] == [], "the walk completed on every page"
            total_pages = body["total_pages"]
            walked.extend(entry["pipeline_id"] for entry in body["pipelines"])

        assert total_pages == (7 + page_size - 1) // page_size
        assert walked == expected, "the union of the pages must be the full descending updated_at order"
        assert len(set(walked)) == 7, "no record appears twice"


# ═══════════════════════════════════════════════════════════════════════════
# 9. The fan-out's own bound, and the third marker that names it — D9
# ═══════════════════════════════════════════════════════════════════════════
@contextlib.contextmanager
def enrichment_held_open(blocked_ids: set[str]) -> Iterator[None]:
    """Hold the named pipelines' first enrichment read open for the whole block.

    The instrument §9 needs and the only one available: to observe the fan-out's
    deadline binding, at least one future must be **unable** to finish, and no
    amount of seeded data can arrange that — the reads' own deadlines make each of
    them fast. So one read blocks on an ``Event`` this helper releases on exit.

    It patches ``crud.get_pipeline_schema_history_preview``, which
    ``enrich_pipeline`` resolves as a module global at call time. That is the same
    read whose *exception* reaches the per-future catch, which keeps §9's two causes
    of a stub — a failed read and an abandoned future — reachable through one
    symbol, so the tests below compare like with like.

    The patch is undone **on exiting this block** rather than at test teardown, via
    its own ``MonkeyPatch`` context. A test that provokes both causes in turn needs
    the second call to run against the real read, and a test-scoped patch would
    leave the blocking one installed with its event already set — the same failed-read
    stub arriving for the wrong reason, which is exactly what the comparison is
    supposed to establish.

    On release the blocked worker **raises instead of continuing**, deliberately:
    its future has already been abandoned, so its value is discarded either way,
    and returning would let a thread touch moto tables after the test that owns
    them has finished. Nobody retrieves the exception, which ``concurrent.futures``
    is content with. If the deadline somehow did *not* bind, that raise turns the
    pipeline into a failed-read stub and the ``enrichment_abandoned`` assertions
    below fail loudly rather than passing for the wrong reason.
    """
    release = threading.Event()
    original = crud.get_pipeline_schema_history_preview

    def blocking(pipeline_id: str, deadline: float, *args: Any, **kwargs: Any) -> Any:
        if pipeline_id in blocked_ids:
            release.wait(timeout=30)
            raise RuntimeError("p47 D9: released after the fan-out had already abandoned this future")
        return original(pipeline_id, deadline, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(crud, "get_pipeline_schema_history_preview", blocking)
        try:
            yield
        finally:
            release.set()


def shorten_the_fan_out_window(monkeypatch: Any, seconds: float = 0.6) -> None:
    """Leave the fan-out ``seconds`` of budget, keeping D8's share ordering intact.

    The convention every deadline in this tree states: **drive the bound by
    shortening a constant, never by asserting a wall-clock duration.** Nothing
    below asserts how long anything took; the observable is which futures were
    abandoned.

    Both constants move, not just the budget. Shortening ``_LIST_TIME_BUDGET_SECONDS``
    alone would push the page-scan share (8 s) past the enrichment deadline and
    invert the ordering D8 established — harmless here, since these tables complete
    in one scan page, but a test that quietly depends on an inverted configuration
    is not measuring the shipped one.

    ``_FAN_OUT_COLLECT_GRACE_SECONDS`` is deliberately **not** shortened: it is the
    slack that lets a read which stopped at the deadline still be collected, so a
    test that removed it would be measuring a configuration this endpoint never
    ships. Every call below therefore waits ``seconds`` plus that grace, which is why
    ``seconds`` is small — and a pipeline whose read is held open cannot finish
    inside either.
    """
    monkeypatch.setattr(crud, "_LIST_TIME_BUDGET_SECONDS", crud._LIST_READ_RESERVE_SECONDS + seconds)
    monkeypatch.setattr(crud, "_PAGE_SCAN_TIME_SHARE_SECONDS", seconds / 2)


class TestFanOutDeadline:
    """``as_completed`` is bounded, and an abandoned pipeline says so — task D9.

    D6 and D7 bounded each enrichment read and D8 bounded the page read, but the
    **executor** had no bound: ``as_completed`` without a timeout waits
    indefinitely, so twenty pipelines each finishing just inside their own window
    still sum to a page the listing has no say over. D9 bounds it on
    ``read_deadline`` — the instant already derived from this request's remaining
    budget, reused rather than duplicated (C23 / ``LGR-14d6dd3-2``).

    **What is bounded is how long the handler waits, not how long the work runs.**
    ``cancel_futures`` cannot cancel a future that has started, so a worker inside a
    DynamoDB call runs to completion (on Lambda, until the container freezes on
    return). These tests therefore assert what the *response* says, which is the
    only thing the bound actually promises.

    One consequence deliberately left alone: a future that completes just after the
    deadline has its value discarded, but its inner reads may already have written
    to the enrichment cache. That is safe under D6's rule rather than in spite of it
    — only *complete* answers are ever cached, so a discarded entry can only have
    left whole values behind.
    """

    @staticmethod
    def _seed_three(tables: dict[str, Any]) -> None:
        """Three pipelines, ``pipe-2`` newest — so an abandoned entry's position shows."""
        for n in range(3):
            tables["pipelines_table"].put_item(
                Item=pipeline_record(f"pipe-{n}", updated_at=f"2026-01-{n + 1:02d}T00:00:00Z")
            )
            seed(
                tables["state_table"],
                [
                    state_row(
                        f"run-{n}",
                        "schema_extract",
                        f"pipe-{n}",
                        "2026-01-02T00:00:00Z",
                        schema_hash=f"hash-{n}",
                        json_s3_uri=f"s3://bucket/schemas/{n}.json",
                    )
                ],
            )

    def test_the_deadline_stops_the_fan_out_and_the_envelope_names_what_it_dropped(self, list_tables, monkeypatch):
        """One future cannot finish: the page still answers, and says which entry is short.

        The page-level property R8.5 establishes for a failed read, one level up:
        three in, three out, ``200``. The abandoned pipeline is **present** rather
        than omitted, so this is degradation and not silent loss.

        *That this test terminates at all is the ``shutdown(wait=True)`` trap being
        avoided.* A ``with ThreadPoolExecutor(...)`` block would raise ``TimeoutError``
        inside and then block on ``__exit__`` until the held read released — the whole
        overrun the deadline exists to prevent. The pin on that is the source-level
        one below; this is the behavioural half.
        """
        self._seed_three(list_tables)
        shorten_the_fan_out_window(monkeypatch)

        with enrichment_held_open({"pipe-1"}):
            status, body = call_pipelines()

        assert status == 200
        assert len(body["pipelines"]) == 3
        assert {entry["pipeline_id"] for entry in body["pipelines"]} == {"pipe-0", "pipe-1", "pipe-2"}
        assert body["enrichment_abandoned"] == ["pipe-1"], (
            "the fan-out deadline bound, and the one future it could not consume is named"
        )
        assert body["partial_counts"] == [], (
            "the *page read* completed — the fan-out's bound must not be reported as the page set being a prefix"
        )

        by_id = {entry["pipeline_id"]: entry for entry in body["pipelines"]}
        assert set(by_id["pipe-1"]) == set(STUB_KEYS), "an abandoned pipeline ships the unenriched stub"
        for enriched_id in ("pipe-0", "pipe-2"):
            assert set(by_id[enriched_id]) == set(ENRICHED_KEYS), "one slow pipeline must not degrade the others"
            assert by_id[enriched_id]["partial_fields"] == [], (
                "and their reads had a window of their own — the fan-out bound is not an enrichment bound"
            )

    def test_the_named_ids_are_exactly_the_entries_that_carry_no_enrichment(self, list_tables, monkeypatch):
        """The marker and the payload agree, which is the property a client relies on.

        Asserted as an equality over the whole page rather than by looking up the one
        id: a marker naming an entry that *is* enriched, or an unenriched entry the
        marker does not name, are both failures and only one of them is visible from
        the id alone. This is the direction that distinguishes D9's signal from
        guessing "a stub means the clock ran out" — a failed read produces the same
        stub and is deliberately **not** named here.
        """
        self._seed_three(list_tables)
        shorten_the_fan_out_window(monkeypatch)

        with enrichment_held_open({"pipe-0", "pipe-2"}):
            _status, body = call_pipelines()

        stub_ids = {entry["pipeline_id"] for entry in body["pipelines"] if set(entry) == set(STUB_KEYS)}
        assert stub_ids == {"pipe-0", "pipe-2"}
        assert set(body["enrichment_abandoned"]) == stub_ids, (
            "every unenriched entry on this page is unenriched *because of the deadline*, so the two sets are equal"
        )

    def test_an_abandoned_pipeline_keeps_the_position_its_record_earned(self, list_tables, monkeypatch):
        """D8's ``page_order`` covers an abandoned entry, verified rather than assumed.

        The re-sort keys on the position each record held in the already-sorted page,
        looked up by ``pipeline_id`` — which the stub carries. So the property D8
        established for a failed-read stub holds for D9's abandoned one for free, and
        this is the test that says the "for free" was checked: ``pipe-2`` is the
        newest record and heads the page whether or not its enrichment happened.

        The abandoned-id list is asserted in page order for the same reason. Which
        futures are outstanding at the deadline is not deterministic in general, so
        the marker is sorted on the same anchor rather than on completion order.
        """
        self._seed_three(list_tables)
        shorten_the_fan_out_window(monkeypatch)

        with enrichment_held_open({"pipe-2", "pipe-0"}):
            _status, body = call_pipelines()

        assert [entry["pipeline_id"] for entry in body["pipelines"]] == ["pipe-2", "pipe-1", "pipe-0"], (
            "D9 changes neither the page order nor the stub literal — an abandoned entry degrades its fields"
        )
        assert body["enrichment_abandoned"] == ["pipe-2", "pipe-0"], "named in page order, not completion order"

    def test_an_abandoned_stub_and_a_failed_read_stub_are_one_shape(self, list_tables, monkeypatch):
        """Two causes, one literal — because D9 made it one function.

        The stub's key set is pinned with ``==`` and asserted a strict subset of the
        enriched set, which is what lets a client parse both forms with one type. Two
        copies of that literal would be two shapes one refactor apart, so the two
        causes are compared here directly rather than each against ``STUB_KEYS``.
        """
        self._seed_three(list_tables)
        shorten_the_fan_out_window(monkeypatch)
        with enrichment_held_open({"pipe-1"}):
            _status, abandoned_body = call_pipelines()
        abandoned = next(e for e in abandoned_body["pipelines"] if e["pipeline_id"] == "pipe-1")

        logs_handler._cache.clear()
        monkeypatch.setattr(crud, "state_table", StateTableRefusingPipelines(list_tables["state_table"], {"pipe-1"}))
        _status, failed_body = call_pipelines()
        failed = next(e for e in failed_body["pipelines"] if e["pipeline_id"] == "pipe-1")

        assert abandoned == failed, "one stub literal, two causes — the shapes must be identical, values included"
        assert failed_body["enrichment_abandoned"] == [], (
            "and the envelope is what tells them apart: a failed read is not an abandoned future"
        )

    def test_the_executor_is_not_a_context_manager_and_abandons_rather_than_joins(self):
        """The trap ``LGR-14d6dd3-2`` records, pinned at the source level.

        ``Executor.__exit__`` calls ``shutdown(wait=True)``, so a ``with`` block would
        return only after every submitted read finished — the deadline would raise on
        time and the handler would overrun anyway. Source text is the only instrument
        for "this is deliberately not the idiomatic form", the same reason R8.4's
        comment assertion uses it, and the failure message is where the next reader
        who reaches for a ``with`` block finds out why.
        """
        assert "shutdown(wait=False, cancel_futures=True)" in HANDLER_SOURCE, (
            "the fan-out must abandon outstanding reads rather than join them — `shutdown(wait=True)`, "
            "which `Executor.__exit__` calls, blocks until the very overrun the deadline exists to prevent"
        )
        assert "with ThreadPoolExecutor" not in HANDLER_SOURCE, (
            "the executor is deliberately not a context manager (C23 / LGR-14d6dd3-2); if this must change, "
            "the deadline needs another way to stop the handler waiting"
        )
        assert "ThreadPoolExecutor(max_workers=10)" in HANDLER_SOURCE, (
            "D9 changed how the fan-out is awaited, not how wide it is — the width is R8.4/D10's"
        )

    def test_the_fan_out_reuses_the_enrichment_deadline_rather_than_deriving_a_second(self):
        """One budget with named shares, not three independent timers.

        C23's property is that the bound holds "by construction and independently of
        per-read latency", which requires the instant to come from the request's
        remaining budget. ``_list_read_deadline`` already is that instant and is
        already threaded into every enrichment read, so a third *anchor* would be a
        third thing to keep consistent with the Lambda's 25 s ceiling. Asserted as
        the absence of a third derivation, which is the form a later addition trips.
        """
        assert sorted(name for name in vars(crud) if name.endswith("_deadline")) == [
            "_list_read_deadline",
            "_page_scan_deadline",
        ], "a third deadline derivation appeared; D9's bound is meant to reuse the enrichment one"
        assert "collect_until = read_deadline + _FAN_OUT_COLLECT_GRACE_SECONDS" in HANDLER_SOURCE, (
            "the fan-out's wait must be the enrichment deadline plus its named share of the reserve"
        )
        assert "as_completed(futures, timeout=max(0.0, collect_until - time.monotonic()))" in HANDLER_SOURCE, (
            "and it must be passed to `as_completed` as a remaining window computed at that moment"
        )

    def test_the_collection_grace_is_a_slice_of_the_reserve_and_stays_inside_the_budget(self):
        """The grace is bounded by the reserve it comes out of — arithmetic, not timing.

        It exists because a read stopped *by* the deadline returns just after it, so
        without slack the fan-out would abandon exactly the pipelines whose reads had
        marked their floors, and a stub says less than a marked entry. That makes it a
        share of the window already held back for "everything after the last read
        returns" — so the property to pin is that it is a *slice* of that reserve and
        that the whole handler still finishes inside the self-imposed budget, which is
        itself inside the Lambda's 25 s.
        """
        assert 0 < crud._FAN_OUT_COLLECT_GRACE_SECONDS < crud._LIST_READ_RESERVE_SECONDS, (
            "the grace is carved out of the reserve; it must not exceed it or the reserve stops covering the encode"
        )
        anchor = 1000.0
        collect_until = crud._list_read_deadline(anchor) + crud._FAN_OUT_COLLECT_GRACE_SECONDS
        assert collect_until < anchor + crud._LIST_TIME_BUDGET_SECONDS, (
            "the latest instant the handler can still be waiting must be inside the listing's own budget"
        )
        assert crud._list_read_deadline(anchor) < collect_until, (
            "and strictly later than the reads' deadline, or the marking path is unreachable"
        )

    def test_the_three_markers_hold_disjoint_vocabularies(self, list_tables, monkeypatch):
        """``enrichment_abandoned`` holds ids where the other two hold key names.

        D8 asserted the two key-name vocabularies share no name. The third marker is
        a different *kind* of value, so the disjointness that matters for it is
        against those vocabularies' contents rather than against the key sets: a
        client reading a list of strings must never have to guess whether it is
        holding pipeline ids or field names.
        """
        assert "enrichment_abandoned" not in ENRICHED_KEYS, "the third marker is the envelope's, not an entry's"
        assert set(ENVELOPE_KEYS).isdisjoint(ENRICHED_KEYS)

        self._seed_three(list_tables)
        shorten_the_fan_out_window(monkeypatch)
        with enrichment_held_open({"pipe-1"}):
            _status, body = call_pipelines()

        named_ids = set(body["enrichment_abandoned"])
        assert named_ids, "the deadline bound, so there is something to compare"
        assert named_ids.isdisjoint(crud._ENVELOPE_DERIVED_KEY_ORDER), (
            "an id must never be mistakable for one of `partial_counts`' key names"
        )
        assert named_ids.isdisjoint(ENRICHED_KEYS), (
            "nor for one of `partial_fields`' — the three vocabularies are read without a discriminator"
        )
        assert named_ids <= {entry["pipeline_id"] for entry in body["pipelines"]}, (
            "and every id names a member of *this* page"
        )
