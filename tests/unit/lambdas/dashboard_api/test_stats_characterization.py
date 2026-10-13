# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests for ``get_stats`` (``handlers/stats.py:22``) — p47 task D1.

``get_stats`` has **no unit test**. Its only exercise is
``tests/integration/pipeline_flow/test_dashboard_api_integration.py``, which sits
outside the per-task gate (the default ``addopts`` excludes the integration layer)
and drives the handler through ``MagicMock`` scan stubs — so it can pin *counts*
but cannot observe how DynamoDB treats a ``FilterExpression``. That distinction is
the whole subject of R7.4, which is why this file uses the shared moto fixtures
instead.

WHAT IS BEING PINNED, AND WHY THIS IS CHARACTERIZATION RATHER THAN CORRECTION
-----------------------------------------------------------------------------
Unlike p47's other pre-change test tasks, ``get_stats`` is **not** believed to be
wrong in its output. Its defect is its **cost**: four ``_scan_all`` calls
(``:26``, ``:31``, ``:41``, ``:52``), each looping on ``LastEvaluatedKey`` with no
page cap and no deadline, two of them over the highest-volume table. So current
output is taken as the contract here, and R7 governs the cost change. D3 (cache)
and D4 (bound the reads) must not change any value this file asserts.

Four things are pinned deliberately, one per thing D3/D4 could plausibly break:

1. **The response keys, exactly** — eleven at the time of writing, twelve after
   D4. Enumerated from source, not
   from the task text. ``==`` on the key set, not ``issubset``: D3 adding a
   ``cached``/``stale`` marker or D4 adding an ``approximate`` marker is a
   contract change that should surface here and be ratified, not arrive silently.
2. **How each rate is derived, including the zero denominator.** A rate computed
   from a *truncated* count would keep its key and silently change its value once
   D4 bounds the reads, so the derivation is pinned — the numerator, the
   denominator, the ×100, and the ``0`` (int, from the ``if total > 0 else 0``
   guard) at a zero denominator.
3. **``last_updated``'s current meaning.** Today it is unambiguous:
   ``datetime.now(UTC).isoformat()`` evaluated while the response is being built,
   so it means *when these numbers were computed*. D3 introduces a cache, at which
   point the field acquires a second possible meaning (*when this was served*).
   Pinning today's meaning is what makes D3's choice a visible decision.
4. **What each filtered count matches, in row terms.** D4's whole risk is changing
   a count's meaning while keeping its name, so the two state-table counts are
   pinned against a seeded population that includes near-misses: other
   ``step_name`` values, and rows whose ``status`` is absent or differently-cased.

READ COST IS OBSERVED, NOT ASSERTED IN PROSE
--------------------------------------------
``test_state_table_filters_are_applied_after_the_read`` read ``Count`` and
``ScannedCount`` off the real ``Scan`` responses: the filtered scans examined every
row in the table and returned only the matching ones. That is the measurement R7.4
rests on, and a scripted ``scan`` stub cannot produce it — which is why the
integration test, valuable as it is, cannot stand in for this file.

*p47 D4 replaced that test with its counterpart,*
``test_the_index_queries_read_only_their_own_step_partition``, *which takes the
same two figures off the four ``Query`` responses and finds the opposite: each read
now examines only its own step's partition. Same instrument, inverted reading — see
the D4 section below.*

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` from ``tests/fixtures/conftest.py``.
No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``).

The table names are set at **module scope** rather than through
``mock_lambda_env``, because ``handlers/common.py:29-37`` calls ``require_env`` for
nine tables at **import** time — before any fixture runs. This is the same
module-level ``setdefault`` that ``test_schema_delete_reference_check_prop.py:165``
uses in this directory for the same reason. ``AWS_REGION`` is deliberately not set
(``LGR-3e45e65-1``, **template / ratified**); it is moot here regardless, because
every table the handler touches is bound onto its module namespace as a **fixture
handle**, so no table is ever resolved by name at call time.

The handles are bound onto ``handlers.stats`` **and** ``handlers.common``.
``handlers/stats.py`` does ``from .common import ... state_table``, which copies
the reference into ``stats``'s own namespace — so patching ``common`` alone would
patch a name the handler never reads. Both are patched so that a future refactor
to either access shape cannot silently un-test this file.

The fixture is function-scoped and moto builds fresh tables per test, so every
test below starts with an **empty cache** and therefore exercises the computed
path unless it deliberately calls twice.

AMENDED BY p47 TASK D3 — WHAT CHANGED AND WHY
---------------------------------------------
D3 put a 60 s DynamoDB response cache in front of ``get_stats``. Two of D1's
assertions were about the *absence* of that cache and could not both survive it
and stay true; both were amended in place with the reason recorded at the
assertion, per ``LGR-60c4985-6``, rather than deleted:

* ``test_a_second_call_repeats_every_scan`` (8 scans across two calls) →
  ``test_a_second_call_repeats_no_scan_because_the_first_filled_the_cache``
  (4 scans, all attributable to the first call). D1's own docstring predicted
  this; R7.1 requires it.
* ``test_last_updated_advances_between_two_calls`` →
  ``test_two_computed_responses_still_carry_different_stamps``. The stamp is now
  reused by a hit, so the property is pinned across an expiry instead of across
  two calls — the behaviour keeps a home rather than being dropped.

``test_last_updated_is_when_the_numbers_were_computed`` kept its assertion
unchanged (a single call is always a miss) and gained the resolved decision in
its docstring: **the stamp stays compute time**, so a served hit can be up to one
TTL old. Everything else in this file passes unamended, which is the evidence
that D3 changed no value and added no response key. Nothing in section 4's
``TestFilteredCounts`` moved — the cache row's ``step_name`` is ``"cache"``, so
neither filtered count matches it.

Section 9 (``TestStatsCache``) is D3's own coverage.

AMENDED BY p47 TASK D4 — WHAT CHANGED AND WHY
---------------------------------------------
D4 replaced the two filtered state-table scans with four ``Select="COUNT"``
Queries on ``step_name-timestamp-index``, bounded every remaining read with a
page cap and a wall-clock deadline, and added a twelfth response key,
``partial_counts``. Three groups of assertions moved. Every one is amended in
place with its reason at the assertion, per ``LGR-60c4985-6``.

**1. A fixture defect, not a behaviour change.** ``state_row()`` never wrote a
``timestamp``, so *every* row D1 seeded was invisible to the sparse GSI — D2
measured it: scan 5, Query 0. Eleven count assertions would have failed for a
reason with nothing to do with correctness. ``state_row()`` now defaults a
``timestamp``, which makes the fixtures represent rows the system actually
writes; before, they represented rows no writer produces. No assertion changed.

Defaulting it removes the sparseness case from the population, so it is added
back deliberately as
``TestFilteredCounts.test_a_file_validation_row_with_no_timestamp_is_not_counted``
— under the index arm that row is **not** counted, where under the scan it was.
That is a real semantic change on a row shape no current writer produces, and it
is pinned as a visible assertion rather than left as an implicit consequence.

**2. The read-mechanism assertions, which broke by design.** D1's docstring said
so in as many words: "``Query`` is asserted absent because R7.4's index-backed arm
would introduce one — that is the change, and it should be visible as a diff to
this assertion rather than as a silent improvement." Five, each amended at the
site: ``test_get_stats_issues_four_scans_and_no_query`` →
``test_get_stats_issues_two_scans_and_four_index_queries``;
``test_a_second_call_repeats_no_scan_because_the_first_filled_the_cache``;
``test_only_the_two_state_table_scans_carry_a_filter_expression`` →
``test_the_surviving_scans_carry_no_filter_and_the_state_reads_are_queries``;
``test_state_table_filters_are_applied_after_the_read`` →
``test_the_index_queries_read_only_their_own_step_partition``, which is the
*inverse* measurement — D1 measured ``ScannedCount == whole table``, this measures
``ScannedCount == the step's partition``, and that difference is the entire cost
claim; and ``test_scan_all_paginates_past_the_one_megabyte_page_limit``, moved
onto the approvals scan so ``_scan_all``'s pagination keeps a home now that the
state table is no longer read through it. Three of ``TestStatsCache``'s own
assertions move for the same benign arithmetic reason, exactly as D3's docstring
predicted they would.

**3. The key-set tripwire fired, and was answered rather than retired.** D1 used
``==`` on the eleven-key set specifically so that "D4 adding an ``approximate``
marker is a contract change that should surface here and be ratified, not arrive
silently". It did surface. ``EXPECTED_KEYS`` and the length assertion are updated
to **twelve**, and the comparison stays ``==`` — loosening it to ``issubset``
would retire the tripwire instead of answering it. Section 10 is D4's own coverage
of the marker and the bound.

**Validates: Requirements 7.6** (sections 1–8);
**Requirements 7.1, 7.2, 7.3, 7.5** (section 9);
**Requirements 7.4** (section 10)
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key
from botocore.client import BaseClient
from botocore.exceptions import ClientError

#: Distinguishes "the caller said nothing" from "the caller said ``None``" for
#: ``state_row``'s ``timestamp``. ``None`` means *omit the attribute*, which is the
#: sparse-GSI near-miss D4 has to pin; the default means *derive one*, which is what
#: every live writer does. Two different meanings need two different values.
_OMITTED: Any = object()

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.stats
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
from handlers import stats as stats_handler

STATE_TABLE = "agentic_data_ingestion_state"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
APPROVALS_TABLE = "agentic_data_ingestion_approvals"

#: The response body's keys, enumerated from the handler's own ``stats`` literal in
#: source order — re-derived rather than taken on trust, because a key count is
#: exactly the figure that drifts.
#:
#: AMENDED BY p47 D4 — **twelve**, was eleven. ``partial_counts`` is R7.4's
#: marking: the list of keys above whose value is a *floor* rather than a total.
#: D1 pinned this set with ``==`` precisely so that this addition would surface as
#: a contract change to be ratified rather than arrive silently, so the ``==`` in
#: ``test_response_body_carries_exactly_the_twelve_keys`` stays ``==``.
#:
#: The key is **always present** (``[]`` when every count is exact) rather than
#: emitted only when something was truncated. D2 §4.2 allowed either; one answer
#: is assertable with ``==`` and two are not.
EXPECTED_KEYS: tuple[str, ...] = (
    "total_pipelines",
    "active_pipelines",
    "total_files_processed",
    "successful_files",
    "file_success_rate",
    "total_mappings",
    "successful_mappings",
    "mapping_success_rate",
    "pending_approvals",
    "total_approvals",
    "partial_counts",
    "last_updated",
)

#: The ten numeric keys. Neither ``last_updated`` (a string) nor ``partial_counts``
#: (a list of key names) is a count, and neither can be compared against ``0``.
COUNT_KEYS: tuple[str, ...] = tuple(k for k in EXPECTED_KEYS if k not in ("last_updated", "partial_counts"))


# ---------------------------------------------------------------------------
# Fixture: the three tables the handler scans, bound onto its namespace
# ---------------------------------------------------------------------------
@pytest.fixture
def stats_tables(mock_dynamodb_tables, monkeypatch) -> dict[str, Any]:
    """Bind the shared moto handles for the three tables ``get_stats`` scans.

    Returns a name→Table map for the seeders. See the module docstring for why
    both ``handlers.stats`` and ``handlers.common`` are patched.
    """
    handles = {
        "state_table": mock_dynamodb_tables[STATE_TABLE],
        "pipelines_table": mock_dynamodb_tables[PIPELINES_TABLE],
        "approvals_table": mock_dynamodb_tables[APPROVALS_TABLE],
    }
    for attribute, table in handles.items():
        monkeypatch.setattr(stats_handler, attribute, table)
        monkeypatch.setattr(common_handler, attribute, table)
    return handles


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------
def seed_state_rows(table: Any, rows: list[dict[str, Any]]) -> None:
    """Write state rows. The key is (``run_id`` pk, ``step_name`` sk).

    That key shape is the reason a per-step count is a per-**run** count: one
    ``run_id`` can carry at most one ``file_validation`` row. Seeding therefore
    varies ``run_id``, not ``step_name``, when more matching rows are wanted.
    """
    with table.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=row)


#: Base instant for the ``timestamp`` every seeded state row now carries.
_STATE_ROW_TIMESTAMP_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _stable_timestamp(run_id: str, step_name: str) -> str:
    """A deterministic ISO-8601 UTC ``timestamp`` for one seeded row.

    Derived from the row's own identity, so a given row always gets the same value
    and two different rows usually get different ones — which is what a real
    population looks like in ``step_name-timestamp-index``, whose entries are keyed
    ``(step_name, timestamp)``. No assertion in this file depends on the resulting
    order; the determinism is so that a failure reproduces, not so that a test can
    read the index in a particular sequence.
    """
    offset = sum(ord(char) for char in f"{run_id}/{step_name}") % 86_400
    return (_STATE_ROW_TIMESTAMP_BASE + timedelta(seconds=offset)).isoformat()


def state_row(
    run_id: str,
    step_name: str,
    status: str | None = None,
    *,
    timestamp: Any = _OMITTED,
    **extra: Any,
) -> dict[str, Any]:
    """One state row. ``status`` is omitted entirely when ``None``.

    Omission is a case the handler must be pinned against, not a shortcut: the two
    per-step success subsets filter ``status == "success"`` server-side, which does
    not match a *missing* attribute, so a row with no ``status`` at all counts
    toward the total and not toward the successes. That was true of the
    ``f.get("status") == "success"`` comprehension it replaced and it is still true
    of ``Attr("status").eq("success")``.

    AMENDED BY p47 D4 — **``timestamp`` now defaults**, and that one line is what
    keeps eleven count assertions in this file passing. D4's per-step counts Query
    ``step_name-timestamp-index``, which is sparse by design: a row with no
    ``timestamp`` never enters it (``data-stack.ts:66-69``). Every row this helper
    produced was therefore invisible to the index — D2 measured a population whose
    5 ``file_validation`` rows the scan counted as 5 and the Query as 0. That is a
    fixture artifact, not a behaviour change: every live writer of these two step
    names sets ``timestamp`` unconditionally (``file_validation:225``, ``:455``;
    ``schema_mapping:1585``, ``:2029``), so the rows seeded here now represent what
    the system actually writes rather than a shape no writer produces.

    Pass ``timestamp=None`` to seed the near-miss deliberately — a row absent from
    the index. Exactly one test does, and it exists to pin that consequence:
    ``TestFilteredCounts.test_a_file_validation_row_with_no_timestamp_is_not_counted``.
    """
    row: dict[str, Any] = {"run_id": run_id, "step_name": step_name}
    if status is not None:
        row["status"] = status
    if timestamp is _OMITTED:
        row["timestamp"] = _stable_timestamp(run_id, step_name)
    elif timestamp is not None:
        row["timestamp"] = timestamp
    row.update(extra)
    return row


def call_stats() -> tuple[int, dict[str, Any]]:
    """Invoke ``get_stats`` and return ``(statusCode, decoded body)``.

    The body is a JSON **string** in the envelope (``create_response``,
    ``shared/response_utils.py:32``), so every assertion below is against what a
    client actually receives rather than against the handler's internal dict.
    """
    response = stats_handler.get_stats()
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# A pass-through spy over the DynamoDB wire calls
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def recorded_dynamodb_calls(
    *, fail_scan_on_table: str | None = None
) -> Iterator[list[tuple[str, dict[str, Any], Any]]]:
    """Record ``(operation, params, response)`` for every DynamoDB call in the block.

    A **pass-through spy, not a mock**: the wrapper records and then calls the
    original, so every response is moto's own — which is what makes ``Count`` and
    ``ScannedCount`` meaningful evidence rather than something this file decided.
    It wraps ``botocore.client.BaseClient._make_api_call``, above moto's
    ``before-send`` stubber and above botocore's retry handler, which is the shape
    four files in this directory already use (see the conftest's
    ``recorded_s3_calls``) and what makes an injected ``ClientError`` deterministic.

    Args:
        fail_scan_on_table: when set, a ``Scan`` against that table raises
            ``ClientError`` instead of reaching moto. Used to pin the inner
            ``try``/``except`` at ``stats.py:51-58``.
    """
    recorded: list[tuple[str, dict[str, Any], Any]] = []
    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        if self.meta.service_model.service_name != "dynamodb":
            return original(self, operation_name, api_params)
        if (
            fail_scan_on_table is not None
            and operation_name == "Scan"
            and api_params.get("TableName") == fail_scan_on_table
        ):
            recorded.append((operation_name, dict(api_params), None))
            raise ClientError(
                {
                    "Error": {
                        "Code": "ResourceNotFoundException",
                        "Message": f"p47 D1 harness: Scan on {fail_scan_on_table!r} refused",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 400},
                },
                operation_name,
            )
        response = original(self, operation_name, api_params)
        recorded.append((operation_name, dict(api_params), response))
        return response

    BaseClient._make_api_call = recording
    try:
        yield recorded
    finally:
        BaseClient._make_api_call = original


def scans_against(recorded: list[tuple[str, dict[str, Any], Any]], table_name: str) -> list[tuple[dict, Any]]:
    """Every recorded ``Scan`` against ``table_name``, as ``(params, response)``."""
    return [
        (params, response)
        for op, params, response in recorded
        if op == "Scan" and params.get("TableName") == table_name
    ]


def queries_against(
    recorded: list[tuple[str, dict[str, Any], Any]], table_name: str, *, index_name: str | None = None
) -> list[tuple[dict, Any]]:
    """Every recorded ``Query`` against ``table_name``, as ``(params, response)``.

    Added by p47 D4, the counterpart to ``scans_against``: the two filtered state
    scans D1 measured are now four Queries, so the mechanism assertions need to
    read ``Count`` / ``ScannedCount`` off a ``Query`` response as well.
    """
    return [
        (params, response)
        for op, params, response in recorded
        if op == "Query"
        and params.get("TableName") == table_name
        and (index_name is None or params.get("IndexName") == index_name)
    ]


# ---------------------------------------------------------------------------
# Cache-row helpers (p47 D3)
# ---------------------------------------------------------------------------
#: The stats cache row's table key, taken from the handler's own constants so a
#: namespace change shows up as a failure here rather than as a silently
#: un-exercised cache. The literal is asserted once, in
#: ``TestStatsCache.test_the_cache_row_key_cannot_collide_with_a_counted_step``.
STATS_CACHE_KEY: dict[str, str] = {
    "run_id": f"{stats_handler._STATS_CACHE_NAMESPACE}#{stats_handler._STATS_CACHE_VARIANT}",
    "step_name": "cache",
}


def read_stats_cache_row(table: Any) -> dict[str, Any] | None:
    """The raw stats cache row as stored, or ``None`` when absent."""
    return table.get_item(Key=STATS_CACHE_KEY).get("Item")


def cached_stats_payload(table: Any) -> dict[str, Any]:
    """The decoded body inside the stats cache row. Fails loudly if there is none."""
    row = read_stats_cache_row(table)
    assert row is not None, "no stats cache row was written"
    return json.loads(row["data"])


def expire_stats_cache(table: Any) -> None:
    """Backdate the stats cache row's TTL so the next call recomputes.

    Rewrites only ``ttl``, leaving the stored payload in place — so a test that
    uses this is exercising *staleness*, not absence.
    """
    table.update_item(
        Key=STATS_CACHE_KEY,
        UpdateExpression="SET #ttl = :past",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )


class TableWithFailingOperation:
    """A moto table handle with exactly one operation replaced by a raise.

    Used to fail the cache ``GetItem`` / ``PutItem`` without touching the scans,
    which the ``recorded_dynamodb_calls`` spy cannot express (it keys on ``Scan``).
    Everything else delegates to the real handle, so the counts a test asserts are
    still moto's.
    """

    def __init__(self, table: Any, failing_operation: str) -> None:
        self._table = table
        self._failing_operation = failing_operation

    def __getattr__(self, name: str) -> Any:
        if name == self._failing_operation:

            def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ProvisionedThroughputExceededException",
                            "Message": f"p47 D3 harness: {name} refused",
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 400},
                    },
                    name,
                )

            return _refuse
        return getattr(self._table, name)


# ═══════════════════════════════════════════════════════════════════════════
# 1. The response shape — the exact key set (eleven at D1, twelve after D4)
# ═══════════════════════════════════════════════════════════════════════════
class TestResponseShape:
    """The envelope and the exact key set."""

    def test_response_body_carries_exactly_the_twelve_keys(self, stats_tables):
        """``==`` on the key set, not ``issubset`` — see the module docstring.

        AMENDED BY p47 D4 — was ``..._the_eleven_keys`` and ``== 11``. This is the
        tripwire firing exactly as D1 intended: "D4 adding an ``approximate``
        marker is a contract change that should surface here and be ratified, not
        arrive silently." ``partial_counts`` is that marker, so the count moves to
        twelve and the assertion **stays** ``==``. Loosening it to ``issubset``
        would have made this test pass without anyone deciding anything, which is
        the outcome D1 wrote it to prevent. Recorded per ``LGR-60c4985-6``.
        """
        status, body = call_stats()

        assert status == 200
        assert set(body) == set(EXPECTED_KEYS), (
            f"the response's key set changed: "
            f"missing={sorted(set(EXPECTED_KEYS) - set(body))} "
            f"unexpected={sorted(set(body) - set(EXPECTED_KEYS))}"
        )
        assert len(EXPECTED_KEYS) == 12

    def test_response_is_a_json_string_in_an_api_gateway_envelope(self, stats_tables):
        """The body is serialised, not a dict — ``create_response`` JSON-encodes it."""
        response = stats_handler.get_stats()

        assert set(response) == {"statusCode", "headers", "body"}
        assert isinstance(response["body"], str)
        assert response["headers"]["Content-Type"] == "application/json"

    def test_the_ten_counts_are_ints_and_the_two_rates_are_numbers(self, stats_tables):
        """Ten keys are numeric, ``last_updated`` is a string, ``partial_counts`` a list.

        Pinned because a count that arrived as a ``Decimal`` or a string would
        still satisfy the key-set assertion above while breaking every consumer.

        AMENDED BY p47 D4 — the ``count_keys`` comprehension excluded exactly three
        keys, so the twelfth key would have been asserted to be an ``int`` and
        ``partial_counts`` is a list. Excluded via ``COUNT_KEYS`` and asserted in
        its own right instead: a marker that arrived as a comma-joined string or a
        boolean would break a consumer the same way a stringified count would.
        """
        _, body = call_stats()

        for key in COUNT_KEYS:
            if key in ("file_success_rate", "mapping_success_rate"):
                continue
            assert isinstance(body[key], int), f"{key} is {type(body[key]).__name__}, expected int"
        for key in ("file_success_rate", "mapping_success_rate"):
            assert isinstance(body[key], int | float), f"{key} is {type(body[key]).__name__}"
        assert isinstance(body["last_updated"], str)
        assert isinstance(body["partial_counts"], list), (
            f"partial_counts is {type(body['partial_counts']).__name__} — R7.4's marker names "
            f"which counts are floors, so it is a list of key names, not a flag"
        )
        assert all(isinstance(name, str) for name in body["partial_counts"])


# ═══════════════════════════════════════════════════════════════════════════
# 2. The empty-table baseline (R7.3's measuring stick)
# ═══════════════════════════════════════════════════════════════════════════
class TestEmptyTables:
    """No pipelines, no state rows, no approvals."""

    def test_empty_tables_return_all_ten_counts_as_zero(self, stats_tables):
        """The all-zeros baseline R7.3 is measured against.

        R7.3 says a cold cache must not return partial counts presented as
        complete. "All zero" is what genuinely-empty looks like today, so after D3
        an all-zeros response must mean *empty* and never *not computed yet* — and
        this is the row that makes the two distinguishable.

        AMENDED BY p47 D4 — the loop skipped only ``last_updated``, so it now also
        skips ``partial_counts`` and asserts the sharper thing about it directly:
        on empty tables it is ``[]``. That is the other half of the same
        distinction. Ten zeros **with** an empty marker means "there is nothing";
        ten zeros with a populated marker would mean "I could not finish looking",
        and R7.4 exists so those two never look alike.
        """
        status, body = call_stats()

        assert status == 200
        for key in COUNT_KEYS:
            assert body[key] == 0, f"{key} == {body[key]!r} on empty tables, expected 0"
        assert body["partial_counts"] == [], (
            "empty tables produced a partial-counts marker — every read completed, "
            "so all ten zeros are totals rather than floors"
        )

    def test_empty_tables_still_carry_last_updated(self, stats_tables):
        """Nothing is elided when there is no data — every key is present."""
        _, body = call_stats()

        assert set(body) == set(EXPECTED_KEYS)
        assert body["last_updated"]


# ═══════════════════════════════════════════════════════════════════════════
# 3. The two computed rates — derivation, not just presence
# ═══════════════════════════════════════════════════════════════════════════
class TestComputedRates:
    """``successful / total * 100``, and ``0`` at a zero denominator."""

    def test_file_success_rate_is_successful_over_total_times_one_hundred(self, stats_tables):
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "file_validation", "success"),
                state_row("run-3", "file_validation", "error"),
            ],
        )

        _, body = call_stats()

        assert body["total_files_processed"] == 3
        assert body["successful_files"] == 2
        assert body["file_success_rate"] == pytest.approx(2 / 3 * 100)

    def test_mapping_success_rate_is_successful_over_total_times_one_hundred(self, stats_tables):
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "schema_mapping", "success"),
                state_row("run-2", "schema_mapping", "failed"),
                state_row("run-3", "schema_mapping", "failed"),
                state_row("run-4", "schema_mapping", "failed"),
            ],
        )

        _, body = call_stats()

        assert body["total_mappings"] == 4
        assert body["successful_mappings"] == 1
        assert body["mapping_success_rate"] == pytest.approx(25.0)

    def test_a_rate_is_a_percentage_not_a_fraction(self, stats_tables):
        """The ×100 is pinned on its own: an all-success population reads ``100``.

        Without this, a change from percent to fraction would still satisfy the
        ratio assertions above under ``approx`` on a 0/1 population.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "schema_mapping", "success"),
            ],
        )

        _, body = call_stats()

        assert body["file_success_rate"] == 100
        assert body["mapping_success_rate"] == 100

    def test_both_rates_are_int_zero_at_a_zero_denominator(self, stats_tables):
        """``if total > 0 else 0`` (``:65``, ``:68``) — no raise, and the ``0`` is an int.

        The int matters: ``0`` and ``0.0`` serialise differently
        (``"file_success_rate": 0`` vs ``0.0``), and D4's marked-as-approximate
        path must not quietly convert one to the other.
        """
        seed_state_rows(stats_tables["state_table"], [state_row("run-1", "schema_extract", "success")])

        _, body = call_stats()

        assert body["total_files_processed"] == 0
        assert body["total_mappings"] == 0
        assert body["file_success_rate"] == 0
        assert body["mapping_success_rate"] == 0
        assert isinstance(body["file_success_rate"], int)
        assert isinstance(body["mapping_success_rate"], int)

    def test_a_rate_denominator_is_the_total_and_not_the_table(self, stats_tables):
        """The denominator is the *filtered* count, not the state table's row count.

        Seeded so the two differ: 2 ``file_validation`` rows among 5. A rate over
        the table would read 20%; over the matching rows it reads 50%. D4 replacing
        a filtered scan with an index-backed read must keep the second reading.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "file_validation", "error"),
                state_row("run-3", "schema_extract", "success"),
                state_row("run-4", "code_generation", "success"),
                state_row("run-5", "quality_checks", "success"),
            ],
        )

        _, body = call_stats()

        assert body["total_files_processed"] == 2
        assert body["file_success_rate"] == pytest.approx(50.0)


# ═══════════════════════════════════════════════════════════════════════════
# 4. What each filtered count matches — the D4 anchor
# ═══════════════════════════════════════════════════════════════════════════
class TestFilteredCounts:
    """The two state-table counts, stated in terms of the rows they match.

    D4's risk is changing a count's meaning while keeping its name, so each count
    is pinned against a population carrying the near-misses that would let a
    looser replacement pass: other ``step_name`` values, an absent ``status``, and
    a differently-cased ``status``.
    """

    #: One population, shared by the assertions below so they cannot drift apart.
    POPULATION = [
        # step_name == "file_validation" — 5 rows, 2 of them status == "success"
        state_row("run-1", "file_validation", "success"),
        state_row("run-2", "file_validation", "success"),
        state_row("run-3", "file_validation", "error"),
        state_row("run-4", "file_validation", "SUCCESS"),  # wrong case — not a success
        state_row("run-5", "file_validation", None),  # no status attribute at all
        # step_name == "schema_mapping" — 3 rows, 1 of them status == "success"
        state_row("run-1", "schema_mapping", "success"),
        state_row("run-2", "schema_mapping", "pending"),
        state_row("run-3", "schema_mapping", None),
        # neither step — must not be counted by either
        state_row("run-1", "schema_extract", "success"),
        state_row("run-1", "code_generation", "success"),
        state_row("run-6", "file_validation_retry", "success"),  # not an exact match
    ]

    def test_total_files_processed_counts_every_row_whose_step_name_is_file_validation(self, stats_tables):
        """Exactly ``step_name == "file_validation"``, regardless of ``status``.

        ``file_validation_retry`` is present in the population and must **not**
        count: the filter is equality, not ``begins_with``.
        """
        seed_state_rows(stats_tables["state_table"], self.POPULATION)

        _, body = call_stats()

        assert body["total_files_processed"] == 5

    def test_successful_files_counts_only_the_exact_lowercase_success_status(self, stats_tables):
        """``f.get("status") == "success"`` — case-sensitive, and absent is not success."""
        seed_state_rows(stats_tables["state_table"], self.POPULATION)

        _, body = call_stats()

        assert body["successful_files"] == 2
        assert body["file_success_rate"] == pytest.approx(2 / 5 * 100)

    def test_total_mappings_counts_every_row_whose_step_name_is_schema_mapping(self, stats_tables):
        """The mappings count comes from the **state** table, not the mappings table.

        Worth pinning explicitly: the key reads ``total_mappings`` but the source
        is ``step_name == "schema_mapping"`` state rows — one per run — and the
        ``agentic_data_ingestion_schema_mappings`` table is never read by this
        handler at all.
        """
        seed_state_rows(stats_tables["state_table"], self.POPULATION)

        _, body = call_stats()

        assert body["total_mappings"] == 3
        assert body["successful_mappings"] == 1

    def test_a_per_step_count_is_a_per_run_count(self, stats_tables):
        """One ``run_id`` contributes at most one row per step, by the key schema.

        The state table is keyed (``run_id`` pk, ``step_name`` sk), so
        ``total_files_processed`` is "runs that recorded a ``file_validation``
        step", never "files". Two puts against the same key overwrite; the count
        stays 1. This is the meaning D4 must preserve.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success", attempt="first"),
                state_row("run-1", "file_validation", "success", attempt="second"),
            ],
        )

        _, body = call_stats()

        assert body["total_files_processed"] == 1

    def test_a_file_validation_row_with_no_timestamp_is_not_counted(self, stats_tables):
        """ADDED BY p47 D4 — the one behaviour the index arm genuinely changes.

        ``step_name-timestamp-index`` is **sparse by design**: an item with no
        ``timestamp`` attribute never enters it (``data-stack.ts:66-69``, which names
        the ``metrics_cache`` row as its example). So a ``file_validation`` state row
        written without a ``timestamp`` was counted by the old filtered scan and is
        **not** counted by the Query that replaced it. Nothing else about the two
        counts moved; this did, and it is pinned here rather than left to be
        discovered as a discrepancy.

        **Why this is an acceptable change, stated rather than assumed.** D2
        enumerated every writer that can produce a row with either of the two
        counted step names. The four ``put_item`` sites all set ``timestamp``
        unconditionally (``file_validation:225`` and ``:455``,
        ``schema_mapping:1585`` and ``:2029``); every ``update_item`` that could
        target those keys either carries
        ``ConditionExpression="attribute_exists(run_id)"`` or runs only after a
        ``put_item`` that raises on failure, so none can *create* a row; and nothing
        under ``lambda/``, ``shared/`` or ``agentcore/`` ``REMOVE``s ``timestamp``
        from a state row. **The shape below has no live producer.**

        **What remains, and is a risk rather than a certainty.** Rows written before
        the state row carried a ``timestamp`` at all are invisible to a backfilled
        GSI, and no amount of reading the working tree settles whether any exist.
        ``STATS_COUNT_SHADOW_COMPARE`` is the mitigation — one release logging both
        figures — and the *reason* that flag exists is this test.

        The row is asserted absent from the index as well as absent from the count,
        because the two fail differently: the sparseness is the cause and the
        undercount is the consequence, and a future change to either projection or
        writer could break one without touching the other.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "file_validation", "success", timestamp=None),
            ],
        )

        indexed = stats_tables["state_table"].query(
            IndexName="step_name-timestamp-index",
            KeyConditionExpression=Key("step_name").eq("file_validation"),
        )
        assert indexed["Count"] == 1, "the timestamp-less row entered the sparse index"
        assert [item["run_id"] for item in indexed["Items"]] == ["run-1"]

        _, body = call_stats()

        assert body["total_files_processed"] == 1, (
            "the timestamp-less row was counted — either the read is no longer index-backed, "
            "or the index has stopped being sparse"
        )
        assert body["successful_files"] == 1
        # Not marked partial: nothing was truncated. An uncounted invisible row and
        # a truncated walk are different failures and must not share a signal.
        assert body["partial_counts"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. The pipeline counts
# ═══════════════════════════════════════════════════════════════════════════
class TestPipelineCounts:
    """``total_pipelines`` is unfiltered; ``active_pipelines`` defaults to active."""

    def test_total_pipelines_counts_every_record_regardless_of_status(self, stats_tables):
        seed_state_rows(
            stats_tables["pipelines_table"],
            [
                {"pipeline_id": "p1", "status": "active"},
                {"pipeline_id": "p2", "status": "archived"},
                {"pipeline_id": "p3", "status": "paused"},
            ],
        )

        _, body = call_stats()

        assert body["total_pipelines"] == 3
        assert body["active_pipelines"] == 1

    def test_a_pipeline_with_no_status_attribute_counts_as_active(self, stats_tables):
        """``p.get("status", "active")`` (``:28``) — the default is a real behaviour.

        A record written before ``status`` existed is counted **active**, not
        skipped. D4 replacing this with a ``status-created_at-index`` Query would
        silently drop such records, because a record with no ``status`` attribute
        is not in that GSI at all.
        """
        seed_state_rows(
            stats_tables["pipelines_table"],
            [
                {"pipeline_id": "p1"},  # no status attribute
                {"pipeline_id": "p2", "status": "archived"},
            ],
        )

        _, body = call_stats()

        assert body["total_pipelines"] == 2
        assert body["active_pipelines"] == 1

    def test_active_is_case_sensitive(self, stats_tables):
        """``== "active"`` — ``"ACTIVE"`` is not active today."""
        seed_state_rows(
            stats_tables["pipelines_table"],
            [
                {"pipeline_id": "p1", "status": "active"},
                {"pipeline_id": "p2", "status": "ACTIVE"},
            ],
        )

        _, body = call_stats()

        assert body["total_pipelines"] == 2
        assert body["active_pipelines"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. The approvals counts and their swallowed failure
# ═══════════════════════════════════════════════════════════════════════════
class TestApprovalCounts:
    """``pending_approvals`` / ``total_approvals``, and their swallowing ``except``."""

    def test_pending_approvals_counts_only_status_pending(self, stats_tables):
        seed_state_rows(
            stats_tables["approvals_table"],
            [
                {"approval_id": "a1", "status": "pending"},
                {"approval_id": "a2", "status": "pending"},
                {"approval_id": "a3", "status": "approved"},
                {"approval_id": "a4", "status": "rejected"},
            ],
        )

        _, body = call_stats()

        assert body["total_approvals"] == 4
        assert body["pending_approvals"] == 2

    def test_an_approvals_scan_failure_reports_zero_approvals_and_keeps_the_rest(self, stats_tables):
        """The bare ``except`` swallows **any** exception and reports both counts as ``0``.

        Pinned as current behaviour, and flagged rather than endorsed: the comment
        says "Approvals table might not exist yet", but the ``except`` is bare of
        type, so a throttle or a transient error also yields
        ``pending_approvals: 0`` — indistinguishable from *genuinely none pending*.
        That is the same "a truncated total is indistinguishable from a real one"
        failure mode R7.4 is written to remove, one table over. D4 should decide
        whether this degradation is marked; today it is not.

        The nine other keys are asserted to survive, because the ``try`` is scoped
        to the approvals block and not to the whole handler.

        RESOLVED BY p47 D4 — **it is marked.** D2 §4.3 left the choice open
        (R7.4 governs the two state-table counts, not this one) and required that
        it be decided rather than silently inherited while the neighbour was fixed.
        Decided: ``0``/``0`` from a failed read is already a *floor* — the true
        value is ``>= 0`` — which is exactly what ``partial_counts`` means, so both
        keys are named there. The alternative was shipping a payload that marks six
        of its numbers honestly and two dishonestly.

        The zeros themselves are **unchanged**, which is why every assertion D1
        wrote still stands. Two are added below: the marker names both approval
        keys, and it names *only* those two — the other eight reads completed, and
        a marker that over-claimed would make the caveat useless.
        """
        seed_state_rows(
            stats_tables["pipelines_table"],
            [{"pipeline_id": "p1", "status": "active"}],
        )
        seed_state_rows(
            stats_tables["state_table"],
            [state_row("run-1", "file_validation", "success")],
        )

        with recorded_dynamodb_calls(fail_scan_on_table=APPROVALS_TABLE):
            status, body = call_stats()

        assert status == 200
        assert body["pending_approvals"] == 0
        assert body["total_approvals"] == 0
        # The rest of the response is real, not zeroed.
        assert body["total_pipelines"] == 1
        assert body["active_pipelines"] == 1
        assert body["total_files_processed"] == 1
        assert body["successful_files"] == 1
        assert body["file_success_rate"] == 100
        # p47 D4: the two zeros are floors and say so — and nothing else does.
        assert body["partial_counts"] == ["pending_approvals", "total_approvals"]

    def test_a_pipelines_scan_failure_is_a_500_not_a_zeroed_response(self, stats_tables):
        """Only the approvals read degrades. The other three propagate to ``handle_error``.

        The asymmetry is the point: three of the four scans are inside the outer
        ``try`` only, so a failure there is a ``500`` carrying ``error`` and
        ``details`` — never a 200 with zeros. D3's cache-read failure must degrade
        like the approvals read (R7.2), not like this one.
        """
        with recorded_dynamodb_calls(fail_scan_on_table=PIPELINES_TABLE):
            response = stats_handler.get_stats()

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "Error fetching statistics"
        assert "details" in body
        assert set(body) & set(EXPECTED_KEYS) == set()


# ═══════════════════════════════════════════════════════════════════════════
# 7. last_updated — format and semantics
# ═══════════════════════════════════════════════════════════════════════════
class TestLastUpdated:
    """``datetime.now(UTC).isoformat()`` (``:71``), evaluated at compute time."""

    def test_last_updated_is_utc_iso8601_with_an_explicit_offset(self, stats_tables):
        """Parseable by ``fromisoformat``, tz-aware, UTC, and suffixed ``+00:00``.

        The suffix is pinned rather than left to ``fromisoformat``'s tolerance
        because ``+00:00`` and ``Z`` are different strings to a JavaScript client,
        and the dashboard reads this field as ``last_activity``
        (``use-pipelines.ts:143``).
        """
        _, body = call_stats()

        stamp = body["last_updated"]
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert stamp.endswith("+00:00")
        assert "T" in stamp

    def test_last_updated_is_when_the_numbers_were_computed(self, stats_tables):
        """It falls inside the window in which this call ran.

        **This is the field D3 changes the meaning of.** Today it can only mean
        "when these counts were computed", because it is evaluated in the same
        expression that builds them. Once a cache is in front of the handler, a
        served response may carry a stamp from an earlier computation — so D3 has
        to choose between keeping this meaning (stamp is the cache entry's compute
        time, and can be up to the TTL old) and changing it (stamp is now, and no
        longer describes the numbers beside it). Either is defensible; picking one
        silently is not, which is why the current meaning is pinned here.

        RESOLVED BY p47 D3 — **compute time is kept.** The stamp still means
        "when these numbers were computed", and a cache hit re-serves the stored
        string rather than restamping, so it can be up to one TTL (60 s) old. The
        rejected arm was stamping the serve time, which would have made the field
        assert a freshness the numbers beside it do not have — the same
        "indistinguishable from a real one" failure family as R7.4
        (``LGR-b2f79ef-14``). Keeping compute time is also how R7.5's "staleness
        MUST be bounded **and stated**" is met without adding a response key.

        The assertion below is **unchanged** and still correct, because this test
        makes exactly one call against a fresh table, so the cache is cold and the
        response is computed. Its scope narrowed rather than its content: it now
        pins the *computed* path's stamp. The hit path's counterpart is
        ``TestStatsCache.test_a_cache_hit_reuses_the_stored_compute_time_stamp``.
        """
        before = datetime.now(UTC)
        _, body = call_stats()
        after = datetime.now(UTC)

        parsed = datetime.fromisoformat(body["last_updated"])
        assert before <= parsed <= after, f"{body['last_updated']} is outside [{before}, {after}]"

    def test_two_computed_responses_still_carry_different_stamps(self, stats_tables):
        """Two *computations* over unchanged data still produce two different stamps.

        AMENDED BY p47 D3 — was ``test_last_updated_advances_between_two_calls``,
        asserting ``first != second`` for two back-to-back handler calls. That is
        no longer true of two **calls**: the second is a cache hit and re-serves
        the first stamp, which is the whole of D3's ``last_updated`` decision (see
        the test above, and its inverse in
        ``TestStatsCache.test_a_cache_hit_reuses_the_stored_compute_time_stamp``).

        The underlying property — the stamp tracks the computation, so a fresh
        computation gets a fresh stamp — is unchanged and still worth pinning, so
        it is kept here with the cache expired in between rather than deleted.
        Same reasoning as D2 §3.3 gives for moving the ``_scan_all`` pagination
        assertion onto a surviving scan: the behaviour keeps a home. Amendment
        recorded per ``LGR-60c4985-6``.
        """
        _, first = call_stats()
        expire_stats_cache(stats_tables["state_table"])
        _, second = call_stats()

        assert first["last_updated"] != second["last_updated"]


# ═══════════════════════════════════════════════════════════════════════════
# 8. The read mechanism R7 is about
# ═══════════════════════════════════════════════════════════════════════════
class TestReadMechanism:
    """Two uncached scans and four index Queries — was four scans, two filtered."""

    def test_get_stats_issues_two_scans_and_four_index_queries(self, stats_tables):
        """The read shape of one uncached call, source by source.

        AMENDED BY p47 D4 — was ``test_get_stats_issues_four_scans_and_no_query``,
        asserting ``Scan == 4`` and ``"Query" not in operations``. D1's docstring
        wrote the amendment in advance: "``Query`` is asserted absent because
        R7.4's index-backed arm would introduce one — that is the change, and it
        should be visible as a diff to this assertion rather than as a silent
        improvement." This is that diff. Recorded per ``LGR-60c4985-6``.

        Four Queries rather than two, because each step needs its total and its
        success subset and the subset is a filter on a second bounded read of the
        same partition rather than a second read of a materialised list. Two scans
        survive, and deliberately: neither the pipelines nor the approvals count
        can use its status index without undercounting (see ``_scan_all``).

        The index name is asserted, not just the verb. A Query against the wrong
        index — ``PipelineIndex``, say — would satisfy a verb-only assertion and
        return nothing.
        """
        seed_state_rows(stats_tables["state_table"], [state_row("run-1", "file_validation", "success")])

        with recorded_dynamodb_calls() as recorded:
            call_stats()

        operations = [op for op, _, _ in recorded]
        assert operations.count("Scan") == 2
        assert operations.count("Query") == 4
        assert len(scans_against(recorded, PIPELINES_TABLE)) == 1
        assert len(scans_against(recorded, APPROVALS_TABLE)) == 1
        assert scans_against(recorded, STATE_TABLE) == [], "the state table is no longer scanned at all"
        assert len(queries_against(recorded, STATE_TABLE, index_name="step_name-timestamp-index")) == 4

    def test_a_second_call_repeats_no_read_because_the_first_filled_the_cache(self, stats_tables):
        """Two calls cost one call's worth of reads — the second is served from cache.

        AMENDED BY p47 D3 — was ``test_a_second_call_repeats_every_scan``,
        asserting ``count("Scan") == 8``. D1's own docstring anticipated this
        ("R7.1 changes this to four"), and R7.1 is the requirement: "A cache hit
        MUST serve the endpoint without any table scan." Eight was the *defect*
        being measured, so the amendment is the fix landing, not a regression.
        Recorded per ``LGR-60c4985-6``, which requires an amended assertion to
        stay legible as a deliberate change.

        AMENDED AGAIN BY p47 D4 — the arithmetic moved from four scans to two scans
        plus four Queries. The claim is unchanged and is the point: **the second
        call contributes zero reads of either kind.** Asserting only the scan count
        would now let four repeated Queries through, so both verbs are counted.
        ``TestStatsCache`` pins the hit path on its own, including that its single
        DynamoDB call is the cache ``GetItem``.
        """
        with recorded_dynamodb_calls() as recorded:
            call_stats()
            after_first = [op for op, _, _ in recorded]
            call_stats()

        operations = [op for op, _, _ in recorded]
        assert (after_first.count("Scan"), after_first.count("Query")) == (2, 4)
        assert (operations.count("Scan"), operations.count("Query")) == (2, 4)

    def test_the_surviving_scans_carry_no_filter_and_the_state_reads_are_keyed_queries(self, stats_tables):
        """Where the ``step_name`` predicate lives now, and where ``status`` lives.

        AMENDED BY p47 D4 — was
        ``test_only_the_two_state_table_scans_carry_a_filter_expression``, which
        asserted both state scans passed ``FilterExpression="step_name = :step"``.
        There are no state-table scans left; the predicate moved from a
        post-read filter to a **key condition**, which is the whole of R7.4's
        index arm. Recorded per ``LGR-60c4985-6``.

        The three claims, and why each is separate:

        1. Every state read is a Query on ``step_name-timestamp-index`` — the
           predicate is now in the key, so DynamoDB reads only the step's
           partition.
        2. Exactly **two** of the four carry a ``FilterExpression``, and it is on
           ``status``, not on ``step_name``. That is the success subset, and it runs
           server-side on the already-bounded read because the index projects ALL.
           If all four carried one, the totals would be filtered too; if none did,
           the subsets would be wrong.
        3. The two surviving scans still carry no filter, exactly as before.
        """
        with recorded_dynamodb_calls() as recorded:
            call_stats()

        state_queries = queries_against(recorded, STATE_TABLE, index_name="step_name-timestamp-index")
        assert len(state_queries) == 4
        assert scans_against(recorded, STATE_TABLE) == []

        # The spy sits on ``_make_api_call``, which is above **both** of boto3's
        # dynamodb translations — the document-to-wire serializer *and* the
        # ``ConditionExpressionBuilder`` that turns a ``Key``/``Attr`` object into an
        # expression string plus ``ExpressionAttributeValues``. Both are registered
        # on ``before-parameter-build``. D1 recorded the first (the scan's substituted
        # values arrived as plain ``str`` rather than ``{"S": ...}`` descriptors); the
        # second only becomes visible once a caller passes condition *objects*, which
        # D4 is the first read here to do. So there is no ``ExpressionAttributeValues``
        # to read at this layer, and the condition is inspected directly instead —
        # which is the better assertion anyway, since ``:v0`` and friends are boto3's
        # own numbering and not part of the contract being pinned.
        def condition_parts(condition: Any) -> tuple[str, str, Any]:
            """``(attribute name, operator, compared value)`` of one boto3 condition."""
            expression = condition.get_expression()
            attribute, value = expression["values"]
            return attribute.name, expression["operator"], value

        key_conditions = [condition_parts(params["KeyConditionExpression"]) for params, _ in state_queries]
        assert sorted(key_conditions) == [
            ("step_name", "=", "file_validation"),
            ("step_name", "=", "file_validation"),
            ("step_name", "=", "schema_mapping"),
            ("step_name", "=", "schema_mapping"),
        ], "expected one total and one success-subset Query per step, keyed on step_name equality"
        assert all(params.get("Select") == "COUNT" for params, _ in state_queries)

        filtered = [params for params, _ in state_queries if "FilterExpression" in params]
        assert len(filtered) == 2, "exactly the two success subsets filter; the two totals must not"
        for params in filtered:
            assert condition_parts(params["FilterExpression"]) == ("status", "=", "success"), (
                "the surviving filter must be the case-sensitive success subset, and it must be on "
                "status — step_name is a key condition now, not a post-read filter, and that is "
                "R7.4's index arm"
            )

        for table in (PIPELINES_TABLE, APPROVALS_TABLE):
            for params, _ in scans_against(recorded, table):
                assert "FilterExpression" not in params, f"{table} scan gained a filter"

    def test_the_index_queries_read_only_their_own_step_partition(self, stats_tables):
        """``ScannedCount`` is the step's partition, not the table. **The cost claim.**

        AMENDED BY p47 D4 — was ``test_state_table_filters_are_applied_after_the_read``,
        which asserted ``ScannedCount == len(population)`` on both filtered scans:
        each examined all 12 rows to return 2 and 1. That measurement was R7.4's
        *evidence*, and this is the same measurement after the fix, inverted. The
        old assertion cannot survive the change it justified, so it is replaced by
        its counterpart rather than deleted. Recorded per ``LGR-60c4985-6``.

        Same population, same reason for using moto rather than a scripted stub: a
        stub returns whatever it was told to and can show no relationship between
        what was examined and what matched. Here:

        - the two **total** Queries examine only their own partition — 2 rows for
          ``file_validation``, 1 for ``schema_mapping``, against a 12-row table —
          and ``Count == ScannedCount``, because a keyed read with no filter
          examines exactly what it returns;
        - the two **subset** Queries examine that same partition and return less,
          which is the ``status`` filter still being applied after the read. It is
          bounded now not because filtering got cheaper but because the set it
          filters is the partition rather than the table.

        Every one of the four is strictly less than the table, which is the claim:
        the read cost stopped being a function of the state table's total size.
        """
        population = [state_row(f"run-{n}", "schema_extract", "success") for n in range(9)] + [
            state_row("run-a", "file_validation", "success"),
            state_row("run-b", "file_validation", "error"),
            state_row("run-c", "schema_mapping", "success"),
        ]
        seed_state_rows(stats_tables["state_table"], population)

        with recorded_dynamodb_calls() as recorded:
            _, body = call_stats()

        state_queries = queries_against(recorded, STATE_TABLE, index_name="step_name-timestamp-index")
        assert len(state_queries) == 4

        totals = [(p, r) for p, r in state_queries if "FilterExpression" not in p]
        subsets = [(p, r) for p, r in state_queries if "FilterExpression" in p]
        assert (len(totals), len(subsets)) == (2, 2)

        for _params, response in totals:
            assert response["ScannedCount"] == response["Count"], (
                "an unfiltered keyed read examined more than it returned — the key condition "
                "is no longer bounding the read to one partition"
            )
        for _params, response in state_queries:
            assert response["ScannedCount"] < len(population), (
                f"a Query examined {response['ScannedCount']} of {len(population)} rows — if this "
                f"reaches the table size, the read is a function of the whole table again and "
                f"R7.4's index arm has been undone"
            )

        assert sorted(response["Count"] for _, response in totals) == [1, 2]
        assert sorted(response["Count"] for _, response in subsets) == [1, 1]
        assert body["total_files_processed"] == 2
        assert body["successful_files"] == 1
        assert body["total_mappings"] == 1
        assert body["successful_mappings"] == 1

    def test_the_cold_call_reads_the_cache_before_any_scan(self, stats_tables):
        """p47 D3: the first DynamoDB call of a cold request is the cache ``GetItem``.

        Ordering is the assertion, not just presence. A cache read issued *after*
        the scans would satisfy every count assertion in this file and save
        nothing, so the order is what makes the cache a cache.
        """
        with recorded_dynamodb_calls() as recorded:
            call_stats()

        operations = [op for op, _, _ in recorded]
        assert operations[0] == "GetItem"
        assert operations.index("GetItem") < operations.index("Scan")

    def test_scan_all_paginates_past_the_one_megabyte_page_limit(self, stats_tables):
        """``_scan_all`` follows ``LastEvaluatedKey`` until it is absent.

        Driven with four ~300 KB rows so the real 1 MB page ceiling is crossed and
        the first ``Scan`` returns a ``LastEvaluatedKey``. A single-page reader
        would report fewer than four.

        MOVED BY p47 D4 — this drove the **state table** through ``_scan_all``,
        which D4 no longer reads that way. D2 §3.3 called for the move rather than
        the deletion: ``_scan_all`` still serves the pipelines and approvals counts,
        so its pagination behaviour needs a home. It is now the approvals table, and
        the assertion is stronger than the original in one respect — the count is
        also asserted **complete**, because a paginating read that silently stops
        early would now report a floor rather than raise, and only
        ``partial_counts`` distinguishes the two.

        D1 recorded two facts here that are no longer true, and their untruth is the
        change: the loop had no page cap and no deadline, so its paging was
        unbounded by construction. It now has both. What survives unchanged is that
        the ``ExclusiveStartKey`` persists into ``scan_kwargs`` between pages.
        """
        filler = "x" * 300_000
        seed_state_rows(
            stats_tables["approvals_table"],
            [{"approval_id": f"a{n}", "status": "pending", "filler": filler} for n in range(4)],
        )

        with recorded_dynamodb_calls() as recorded:
            _, body = call_stats()

        approvals_scans = scans_against(recorded, APPROVALS_TABLE)
        paged = [params for params, _ in approvals_scans if "ExclusiveStartKey" in params]
        assert paged, "no scan carried an ExclusiveStartKey — the 1 MB ceiling was not reached"
        assert body["total_approvals"] == 4
        assert body["pending_approvals"] == 4
        assert body["partial_counts"] == [], "a multi-page read inside budget must not be marked partial"


# ═══════════════════════════════════════════════════════════════════════════
# 9. The cache (p47 task D3 — R7.1, R7.2, R7.3, R7.5)
# ═══════════════════════════════════════════════════════════════════════════
class TestStatsCache:
    """The DynamoDB response cache in front of ``get_stats``.

    Four requirements, and one hazard that is specific to *where* the cache row
    lives. The row goes into the **state table** — the same table two of the
    counting reads use — so the tests below prove it cannot be counted by either
    of those reads and cannot enter the sparse ``step_name-timestamp-index`` that
    D4's per-step Queries do use. Those are not assumptions inherited from the
    pre-existing ``metrics_cache#{period}`` row; they are re-established here for
    the key D3 actually chose.

    R7.2 is satisfied by **reuse**: ``handlers/stats.py`` imports
    ``_get_cached_metrics`` / ``_cache_metrics`` from ``handlers/metrics.py``
    rather than defining anything, which is why both degradation tests below
    exercise behaviour this file never implemented.

    AMENDED BY p47 D4 — THE THREE ASSERTIONS D3 PREDICTED WOULD MOVE
    ----------------------------------------------------------------
    D3 wrote this list for D4 and all three landed as written:

    * ``test_the_cache_row_is_scanned_but_not_counted`` →
      ``test_the_cache_row_is_read_by_neither_state_query``. The "scanned and not
      matched" property has no scan to be measured on any more, so it is replaced
      by the stronger property D3 already named: the row is not in the index at
      all, so the four Queries cannot see it. The counts-do-not-move half is kept
      verbatim, because that is the hazard, and it is now established the way it
      actually holds.
    * ``test_a_cache_read_failure_falls_through_to_computing`` and
      ``test_a_degraded_approvals_read_is_not_cached`` — both asserted ``Scan == 4``
      to prove the handler *computed* rather than served. The claim is "it did the
      work", not "it did four scans", so the claim is kept and the arithmetic
      updated to two Scans plus four Queries. Counting only scans would now let a
      served-from-cache path through, so both verbs are counted.

    One thing D3 predicted correctly and one it could not: the ``state_row()``
    ``timestamp`` default does not disturb anything here (the cache row keys on
    ``step_name="cache"``, which no seeded row uses), but the marker did add a
    twelfth key, so ``EXPECTED_KEYS`` moved under the ``==`` comparisons below.

    **Validates: Requirements 7.1, 7.2, 7.3, 7.5**
    """

    #: A population with a distinct value for every count, so a hit that returned
    #: a *different* computation's payload would be visible rather than plausible.
    POPULATION_STATE = [
        state_row("run-1", "file_validation", "success"),
        state_row("run-2", "file_validation", "success"),
        state_row("run-3", "file_validation", "error"),
        state_row("run-1", "schema_mapping", "success"),
        state_row("run-2", "schema_mapping", "pending"),
    ]
    POPULATION_PIPELINES = [
        {"pipeline_id": "p1", "status": "active"},
        {"pipeline_id": "p2", "status": "archived"},
    ]
    POPULATION_APPROVALS = [
        {"approval_id": "a1", "status": "pending"},
        {"approval_id": "a2", "status": "approved"},
    ]

    def _seed_all(self, stats_tables) -> None:
        seed_state_rows(stats_tables["state_table"], self.POPULATION_STATE)
        seed_state_rows(stats_tables["pipelines_table"], self.POPULATION_PIPELINES)
        seed_state_rows(stats_tables["approvals_table"], self.POPULATION_APPROVALS)

    # -- R7.1: a hit issues no scan ------------------------------------------
    def test_a_cache_hit_issues_no_scan_and_only_the_cache_read(self, stats_tables):
        """R7.1 — "A cache hit MUST serve the endpoint without any table scan."

        Asserted as ``Scan == 0``, and then more strictly: the hit's **only**
        DynamoDB call of any kind is one ``GetItem``. The stricter form is the one
        that matters, because the cache row is itself a read — "zero reads" would
        be false and "zero scans" alone would not notice a stray ``Query`` or a
        second lookup creeping in.
        """
        self._seed_all(stats_tables)
        call_stats()  # cold — fills the cache

        with recorded_dynamodb_calls() as recorded:
            status, _body = call_stats()

        operations = [op for op, _, _ in recorded]
        assert status == 200
        assert operations.count("Scan") == 0
        assert operations == ["GetItem"], f"a hit issued {operations}, expected exactly one GetItem"

    def test_a_cache_hit_returns_the_same_body_the_computation_produced(self, stats_tables):
        """A hit is byte-identical to the miss that filled it — every key, every value.

        Which is also the assertion that D3 added **no** response key. D1 pinned
        the key set with ``==`` so that a ``cached``/``stale`` marker would surface
        as a contract change; D3 introduced none, leaving that tripwire armed — and
        D4's ``partial_counts`` is what tripped it. This assertion is unchanged: a
        hit is still byte-identical to the miss that filled it, marker included.
        """
        self._seed_all(stats_tables)

        _, computed = call_stats()
        _, served = call_stats()

        assert served == computed
        assert set(served) == set(EXPECTED_KEYS)

    def test_a_cache_hit_reuses_the_stored_compute_time_stamp(self, stats_tables):
        """``last_updated`` on a hit is the *earlier* computation's stamp.

        The inverse of ``TestLastUpdated.test_last_updated_is_when_the_numbers_were_computed``
        and the visible half of D3's decision: the stamp describes the numbers
        beside it, so it is older than the serve. If a later change restamps on
        serve, this fails — which is the point.
        """
        self._seed_all(stats_tables)

        _, computed = call_stats()
        served_at = datetime.now(UTC)
        _, served = call_stats()

        assert served["last_updated"] == computed["last_updated"]
        assert datetime.fromisoformat(served["last_updated"]) <= served_at

    def test_an_expired_entry_recomputes_and_reflects_new_data(self, stats_tables):
        """Staleness is bounded: past the TTL the handler recomputes rather than re-serving.

        Data written *after* the first call is invisible until the entry expires,
        and visible immediately once it has — so the TTL is the only thing standing
        between a write and its appearance, which is what R7.5 asks be bounded.
        """
        self._seed_all(stats_tables)
        _, first = call_stats()
        assert first["total_files_processed"] == 3

        seed_state_rows(stats_tables["state_table"], [state_row("run-9", "file_validation", "success")])

        _, still_cached = call_stats()
        assert still_cached["total_files_processed"] == 3

        expire_stats_cache(stats_tables["state_table"])
        _, recomputed = call_stats()
        assert recomputed["total_files_processed"] == 4

    # -- R7.5: the bound -----------------------------------------------------
    def test_the_stored_ttl_is_at_most_sixty_seconds_ahead(self, stats_tables):
        """R7.5 — TTL ≤ 60 s, and the metrics helpers' 300 s is **not** inherited.

        The upper bound is the requirement. The lower bound is the regression
        guard: if the shared helper's ``ttl_seconds`` parameter were dropped and
        the default taken, this reads ``now + 300`` and fails here rather than
        silently serving five-minute-old counts.
        """
        before = int(time.time())
        call_stats()
        after = int(time.time())

        row = read_stats_cache_row(stats_tables["state_table"])
        assert row is not None, "the cold call did not write a cache row"
        ttl = int(row["ttl"])

        assert ttl <= after + 60, f"ttl is {ttl - after}s ahead — R7.5 caps staleness at 60s"
        assert ttl >= before + 1, "ttl is already in the past — the entry would never be a hit"
        assert stats_handler.STATS_CACHE_TTL_SECONDS <= 60

    # -- The row cannot corrupt what it caches --------------------------------
    def test_the_cache_row_key_cannot_collide_with_a_counted_step(self, stats_tables):
        """The row's ``step_name`` is neither of the two step names the counts select.

        This is the collision the task text names: a key whose ``step_name`` were
        ``file_validation`` or ``schema_mapping`` would corrupt the very counts it
        caches. The literals are asserted here — the one place in this file that
        does not derive them from the handler — so a namespace rename is a visible
        diff and not a silent behaviour change.
        """
        call_stats()

        row = read_stats_cache_row(stats_tables["state_table"])
        assert row is not None
        assert row["run_id"] == "stats_cache#global"
        assert row["step_name"] == "cache"
        assert row["step_name"] not in ("file_validation", "schema_mapping")
        # Not the metrics namespace either: the two payload shapes share nothing,
        # so one must never be read back as the other.
        assert not row["run_id"].startswith("metrics_cache#")

    def test_the_cache_row_is_read_by_neither_state_query(self, stats_tables):
        """The row is in the table the counts come from, and no counting read sees it.

        AMENDED BY p47 D4 — was ``test_the_cache_row_is_scanned_but_not_counted``,
        which asserted ``ScannedCount == len(POPULATION_STATE) + 1`` on both
        filtered state scans: the row was examined and not matched. There are no
        state scans left, so the "examined" half has nothing to be measured on. D3
        named the replacement in advance — the row is not in
        ``step_name-timestamp-index`` at all, so the four Queries never reach it,
        which is a stronger guarantee than being filtered out of a scan. Recorded
        per ``LGR-60c4985-6``.

        Both halves still matter and both are still asserted. The Queries'
        ``ScannedCount`` excluding the row is the mechanism; the counts not moving
        once the row exists is the property. Measured across an expiry so the second
        computation genuinely re-reads a table that now contains the cache row —
        which is what makes this a test of the hazard rather than of an absent row.
        """
        self._seed_all(stats_tables)
        _, first = call_stats()
        expire_stats_cache(stats_tables["state_table"])
        assert read_stats_cache_row(stats_tables["state_table"]) is not None, "no cache row to be a hazard"

        with recorded_dynamodb_calls() as recorded:
            _, second = call_stats()

        state_queries = queries_against(recorded, STATE_TABLE, index_name="step_name-timestamp-index")
        assert len(state_queries) == 4
        for _params, response in state_queries:
            assert response["ScannedCount"] <= len(self.POPULATION_STATE), (
                "a counting Query examined more rows than the seeded population — the cache row "
                "has entered step_name-timestamp-index and can now corrupt the counts it caches"
            )

        for key in ("total_files_processed", "successful_files", "total_mappings", "successful_mappings"):
            assert second[key] == first[key], f"{key} moved once the cache row was in the table"

    def test_the_cache_row_carries_no_timestamp_and_stays_out_of_the_sparse_gsi(self, stats_tables):
        """It must not enter ``step_name-timestamp-index`` (``data-stack.ts:66-69``).

        That index is sparse by design — a row with no ``timestamp`` never enters
        it — and the CDK comment names the ``metrics_cache`` row as its example.
        D4 replaces the two filtered scans with Queries against this index, so a
        cache row carrying a ``timestamp`` would pollute the counts D4 computes.
        This is the D3/D4 interaction, pinned from D3's side.

        Both halves are asserted because they fail differently: the missing
        attribute is the cause, the empty Query is the consequence, and a future
        change could break the second without touching the first.
        """
        call_stats()

        row = read_stats_cache_row(stats_tables["state_table"])
        assert row is not None
        assert "timestamp" not in row, "the cache row gained a timestamp and is now in the sparse GSI"

        indexed = stats_tables["state_table"].query(
            IndexName="step_name-timestamp-index",
            KeyConditionExpression=Key("step_name").eq("cache"),
        )
        assert indexed["Count"] == 0, "the cache row is present in step_name-timestamp-index"

    # -- R7.2: both failure directions degrade, neither raises ----------------
    def test_a_cache_read_failure_falls_through_to_computing(self, stats_tables, monkeypatch):
        """R7.2 — a read failure computes, exactly as the approvals read degrades.

        Not as the pipelines scan fails: D1's
        ``test_a_pipelines_scan_failure_is_a_500_not_a_zeroed_response`` records
        that asymmetry, and this is the side D3 must land on. A throttled cache
        must cost latency, never a 500.

        The reads are asserted, not just the 200 — a 200 carrying zeros would
        satisfy a status-code-only assertion and be the R7.3 failure.

        AMENDED BY p47 D4 — was ``Scan == 4``; the same claim is now two Scans plus
        four Queries. Both verbs are counted because a scan-only count would no
        longer notice a path that served without computing. Recorded per
        ``LGR-60c4985-6``.
        """
        self._seed_all(stats_tables)
        monkeypatch.setattr(
            stats_handler,
            "state_table",
            TableWithFailingOperation(stats_tables["state_table"], "get_item"),
        )

        with recorded_dynamodb_calls() as recorded:
            status, body = call_stats()

        operations = [op for op, _, _ in recorded]
        assert status == 200
        assert (operations.count("Scan"), operations.count("Query")) == (2, 4)
        assert body["total_files_processed"] == 3
        assert body["total_pipelines"] == 2
        assert body["pending_approvals"] == 1

    def test_a_cache_write_failure_is_not_fatal(self, stats_tables, monkeypatch):
        """R7.2 — a write failure logs and returns the computed response unchanged.

        A cache that cannot be filled makes every request a miss. That is the old
        cost, which is a degradation; failing the request would be a regression.
        """
        self._seed_all(stats_tables)
        monkeypatch.setattr(
            stats_handler,
            "state_table",
            TableWithFailingOperation(stats_tables["state_table"], "put_item"),
        )

        status, body = call_stats()

        assert status == 200
        assert set(body) == set(EXPECTED_KEYS)
        assert body["total_files_processed"] == 3
        assert body["successful_files"] == 2

    # -- R7.3: a cold cache answers correctly, and never caches a partial -----
    def test_the_first_cold_call_is_correct_and_stores_a_complete_payload(self, stats_tables):
        """R7.3 — a miss answers correctly, and what it stores is a whole payload.

        The stored ``data`` is asserted against the whole key contract rather than
        merely being present, because an entry holding a subset is what "partial
        counts presented as complete" would look like on the next hit.
        """
        self._seed_all(stats_tables)

        _, body = call_stats()

        assert body["total_pipelines"] == 2
        assert body["active_pipelines"] == 1
        assert body["total_files_processed"] == 3
        assert body["successful_files"] == 2
        assert body["total_mappings"] == 2
        assert body["successful_mappings"] == 1
        assert body["total_approvals"] == 2
        assert body["pending_approvals"] == 1

        stored = cached_stats_payload(stats_tables["state_table"])
        assert set(stored) == set(EXPECTED_KEYS)
        assert stored == body

    def test_all_zeros_still_means_empty_and_never_not_computed_yet(self, stats_tables):
        """R7.3's measuring stick, carried through the cache.

        D1 pinned all-zeros as what genuinely-empty looks like. The risk D3 adds is
        an entry that could be *served* before it was filled, which would make
        all-zeros ambiguous. It cannot happen here — the entry is written from the
        completed payload and never pre-created — and that is asserted from both
        ends: no row exists before the first call, and the row that appears
        afterwards carries the full zeroed payload rather than a placeholder.
        """
        assert read_stats_cache_row(stats_tables["state_table"]) is None

        _, computed = call_stats()
        _, served = call_stats()

        stored = cached_stats_payload(stats_tables["state_table"])
        assert set(stored) == set(EXPECTED_KEYS)
        for key in COUNT_KEYS:
            assert computed[key] == 0
            assert served[key] == 0
            assert stored[key] == 0
        # AMENDED BY p47 D4 — the loop skipped only ``last_updated``. The marker is
        # the other half of the same claim and is asserted rather than skipped: an
        # empty marker beside ten zeros is what makes "there is nothing" legible,
        # and it must survive the round trip through the cache intact.
        for body in (computed, served, stored):
            assert body["partial_counts"] == []

    def test_a_degraded_approvals_read_is_not_cached(self, stats_tables):
        """R7.3 — the swallowed approvals failure must not be stored and re-served.

        ``get_stats`` reports ``0``/``0`` for both approval counts when the
        approvals scan raises, and that value is indistinguishable from *genuinely
        none pending* (D1 pinned it and flagged it; D2 §4.3 leaves the *marking* to
        D4). Uncached, the misreport lasts one request. Cached, it would persist for
        the whole TTL — the exact "partial counts presented as complete" R7.3
        forbids. So the response is unchanged and the write is skipped.

        The third assertion is the one with teeth: the next call reads again, so the
        degraded payload was not merely absent from the row but genuinely not
        serving.

        AMENDED BY p47 D4 in two ways, and the second is the interesting one. The
        arithmetic moved from ``Scan == 4`` to two Scans plus four Queries — same
        claim, and both verbs counted so that a served path cannot pass. And the
        *reason* the write is skipped generalised: D3 gated it on
        ``approvals_error is None``, D4 gates it on ``partial_counts`` being empty.
        Skipping is now what happens to **any** incomplete payload rather than a
        rule about this one read, so the assertion below also pins that the
        degradation is marked in the response — which is D4's answer to the question
        D3 left open. Recorded per ``LGR-60c4985-6``.
        """
        self._seed_all(stats_tables)

        with recorded_dynamodb_calls(fail_scan_on_table=APPROVALS_TABLE):
            status, degraded = call_stats()

        assert status == 200
        assert degraded["pending_approvals"] == 0
        assert degraded["total_approvals"] == 0
        assert degraded["partial_counts"] == ["pending_approvals", "total_approvals"]
        assert read_stats_cache_row(stats_tables["state_table"]) is None, (
            "a payload whose approval counts are a swallowed failure was cached"
        )

        with recorded_dynamodb_calls() as recorded:
            _, recovered = call_stats()

        operations = [op for op, _, _ in recorded]
        assert (operations.count("Scan"), operations.count("Query")) == (2, 4)
        assert recovered["pending_approvals"] == 1
        assert recovered["total_approvals"] == 2
        assert recovered["partial_counts"] == []

    # -- R7.2: one mechanism, not two ----------------------------------------
    def test_the_cache_mechanism_is_the_metrics_one_and_not_a_second_copy(self, stats_tables):
        """R7.2 — ``stats.py`` defines no cache of its own; it imports the metrics pair.

        Identity, not similarity: the two names bound in ``handlers.stats`` are the
        *same function objects* as ``handlers.metrics``'. A structural assertion is
        the only kind that can fail when someone copies the helpers into this
        module and edits one copy — which is the divergence R7.2 names.
        """
        from handlers import metrics as metrics_handler

        assert stats_handler._get_cached_metrics is metrics_handler._get_cached_metrics
        assert stats_handler._cache_metrics is metrics_handler._cache_metrics

        source = pathlib.Path(_LAMBDA_DIR).resolve().joinpath("handlers", "stats.py").read_text(encoding="utf-8")
        for forbidden in ("def _get_cached", "def _cache_", "def _set_cache"):
            assert forbidden not in source, f"stats.py defines its own cache helper ({forbidden!r})"

    def test_the_metrics_cache_keeps_its_own_five_minute_ttl(self, stats_tables):
        """do-NOT 13 / C22 — sharing the helpers changed nothing for metrics.

        The shared write helper's ``ttl_seconds`` defaults to ``CACHE_TTL_SECONDS``,
        so a metrics-shaped call still writes 300 s while stats writes 60 s into the
        same table. Asserted from the metrics side because that is the side p47 was
        told not to perturb, and ``test_metrics_cache.py`` asserts the same figure
        through a mock — this one asserts it against a real table.
        """
        from handlers import metrics as metrics_handler

        before = int(time.time())
        metrics_handler._cache_metrics("24h", {"period": "24h"}, table=stats_tables["state_table"])

        row = stats_tables["state_table"].get_item(Key={"run_id": "metrics_cache#24h", "step_name": "cache"})["Item"]
        assert before + 295 <= int(row["ttl"]) <= before + 305
        assert metrics_handler.CACHE_TTL_SECONDS == 300


# ═══════════════════════════════════════════════════════════════════════════
# 10. The bound and the marker (p47 task D4 — R7.4)
# ═══════════════════════════════════════════════════════════════════════════
#: Roughly a third of a DynamoDB page. Four of these exceed the 1 MB response
#: ceiling, so a read over them genuinely returns a ``LastEvaluatedKey`` and the
#: bound has something real to bind on. Used rather than a stubbed pager because
#: the point of these tests is that the *bound* works against DynamoDB's own
#: pagination, not that a fake page boundary can be detected.
_THIRD_OF_A_PAGE = "x" * 300_000


def seed_multipage_file_validation(table: Any, rows: int = 4) -> None:
    """Seed enough oversized ``file_validation`` rows to force a multi-page read.

    The index projects ALL (``data-stack.ts:77``), so an index entry carries the
    whole item including the filler — which is why the Query pages here and not only
    the table scan would. That is also the RCU caveat D2 §6 names: ``Select="COUNT"``
    does not avoid reading the bytes.
    """
    seed_state_rows(
        table,
        [state_row(f"run-{n}", "file_validation", "success", filler=_THIRD_OF_A_PAGE) for n in range(rows)],
    )


class TestTheBoundBinds:
    """R7.4's second clause: when a count cannot be obtained exactly, it is marked.

    Two guards, tested separately because they fail independently and one is
    load-bearing while the other is a backstop:

    * the **wall-clock deadline**, derived at call time from the request's remaining
      budget (C23, ``LGR-14d6dd3-2`` — it "makes the bound hold by construction and
      independently of per-read latency");
    * the **page cap**, following ``_MAX_STATE_SCAN_PAGES = 100`` at
      ``cost_optimization_agent/main.py:823``.

    Both are exercised by shortening the constant rather than by asserting a
    wall-clock duration or by seeding a hundred pages — the same technique
    ``schemas.py``'s reserve comment prescribes for its own deadline.

    **Validates: Requirements 7.4**
    """

    def test_a_page_cap_that_binds_marks_the_two_file_counts_and_their_rate(self, stats_tables, monkeypatch):
        """One page of a two-page partition is a floor, and the response says which.

        The count is *not* asserted to be any particular number: how many rows fit
        in a page is DynamoDB's business and asserting it would pin the wrong thing.
        What is asserted is the pair of properties that make the answer honest — it
        is short of the truth, and it is named as short.
        """
        seed_multipage_file_validation(stats_tables["state_table"])
        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 1)

        status, body = call_stats()

        assert status == 200
        assert body["total_files_processed"] < 4, "the page cap did not bind — nothing to mark"
        assert body["partial_counts"] == [
            "total_files_processed",
            "successful_files",
            "file_success_rate",
        ], (
            "a truncated count must be named, and only truncated counts must be named — an "
            "unmarked truncated total is indistinguishable from a real one (R7.4)"
        )
        # The reads that completed are unmarked and exact.
        assert body["total_mappings"] == 0
        assert body["total_pipelines"] == 0

    def test_a_deadline_that_has_passed_marks_the_same_counts(self, stats_tables, monkeypatch):
        """The deadline is the guard that holds regardless of per-read latency.

        Driven by shortening the *budget* to zero rather than by sleeping: the
        deadline is ``started + max(0, budget - reserve)``, so a zero budget puts it
        at the instant the request began and every page boundary is past it. That
        exercises the guard without making the test's runtime part of its assertion.

        The first page is still read — deliberately, and this pins it. A count of
        zero marked partial says nothing useful, so the walk always attempts one
        page and the bound applies to *continuing*, which is also what makes the
        cost bounded rather than merely capped.
        """
        seed_multipage_file_validation(stats_tables["state_table"])
        monkeypatch.setattr(stats_handler, "_STATS_TIME_BUDGET_SECONDS", 0.0)

        with recorded_dynamodb_calls() as recorded:
            status, body = call_stats()

        assert status == 200
        assert body["total_files_processed"] > 0, "the first page must always be read"
        assert body["total_files_processed"] < 4
        assert "total_files_processed" in body["partial_counts"]

        # Exactly one page per counting read — the deadline stopped every walk at
        # its first continuation rather than stopping the request.
        state_queries = queries_against(recorded, STATE_TABLE, index_name="step_name-timestamp-index")
        assert len(state_queries) == 4
        assert all("ExclusiveStartKey" not in params for params, _ in state_queries)

    def test_a_bound_scan_marks_the_two_pipeline_counts(self, stats_tables, monkeypatch):
        """The bound is on every paginating read, not only on the two index Queries.

        D2 §4.1 required both guards on the four Queries **and** the two surviving
        scans. Asserted on the pipelines scan because it is the one whose numbers the
        dashboard tile actually shows — ``fetchStats`` maps the displayed
        ``total_pipelines`` from the API's ``active_pipelines``
        (``use-pipelines.ts:140``), so a silently truncated pipelines count is the
        most visible instance of the failure R7.4 removes.
        """
        seed_state_rows(
            stats_tables["pipelines_table"],
            [{"pipeline_id": f"p{n}", "status": "active", "filler": _THIRD_OF_A_PAGE} for n in range(4)],
        )
        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 1)

        status, body = call_stats()

        assert status == 200
        assert body["total_pipelines"] < 4, "the page cap did not bind on the pipelines scan"
        assert body["partial_counts"] == ["total_pipelines", "active_pipelines"]

    def test_a_partial_payload_is_never_cached(self, stats_tables, monkeypatch):
        """R7.3 — a floor must not be stored and re-served for a whole TTL.

        D3 established this for the swallowed approvals failure, gating the write on
        ``approvals_error is None``. D4 generalises the gate to ``partial_counts``
        being empty, so **everything in the cache is complete** and a hit therefore
        never needs marker logic of its own.

        The alternative — caching a marked-partial body, which would stay honest
        because the marker travels with it — was rejected: a bound that binds is a
        transient condition, and re-serving one floor for 60 s turns a one-request
        degradation into a sustained one.

        The second half is the assertion with teeth: once the cap is lifted the next
        call recomputes and answers exactly, which proves nothing was serving.
        """
        seed_multipage_file_validation(stats_tables["state_table"])
        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 1)

        _, truncated = call_stats()
        assert truncated["partial_counts"], "the bound did not bind — nothing to refuse to cache"
        assert read_stats_cache_row(stats_tables["state_table"]) is None, (
            "a payload carrying floors was cached and will be re-served as though exact"
        )

        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 100)
        _, complete = call_stats()

        assert complete["total_files_processed"] == 4
        assert complete["partial_counts"] == []
        assert cached_stats_payload(stats_tables["state_table"]) == complete

    def test_the_marker_lists_keys_in_response_order_across_two_different_causes(self, stats_tables, monkeypatch):
        """One list, one order, whatever the mix of causes.

        Two unrelated things go wrong at once — a page cap truncating the
        ``file_validation`` partition and the approvals scan raising — and the marker
        names all five affected keys in the response's own key order. The ordering is
        pinned because ``partial_counts`` is compared with ``==`` throughout this
        file, and a set-ordered list would make those assertions flaky rather than
        wrong, which is worse.

        This is also the case that justifies a list over a boolean (D2 §4.2): a
        single flag here would say "something is approximate" and leave a client
        unable to tell that ``total_mappings`` and ``total_pipelines`` are exact.
        """
        seed_multipage_file_validation(stats_tables["state_table"])
        seed_state_rows(stats_tables["state_table"], [state_row("run-m", "schema_mapping", "success")])
        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 1)

        with recorded_dynamodb_calls(fail_scan_on_table=APPROVALS_TABLE):
            _, body = call_stats()

        assert body["partial_counts"] == [
            "total_files_processed",
            "successful_files",
            "file_success_rate",
            "pending_approvals",
            "total_approvals",
        ]
        # The mapping counts completed in one page and are therefore exact.
        assert body["total_mappings"] == 1
        assert "total_mappings" not in body["partial_counts"]
        assert "mapping_success_rate" not in body["partial_counts"]

    def test_a_marked_rate_keeps_its_type_and_its_zero_at_a_zero_denominator(self, stats_tables, monkeypatch):
        """The marker changes what a rate *means*, never what it *is*.

        D1 pinned the ``0`` at a zero denominator as an ``int`` specifically because
        "D4's marked-as-approximate path must not quietly convert one to the other".
        So the arithmetic is untouched: a rate whose inputs are floors is still
        ``successful / total * 100`` and still an ``int`` ``0`` when the denominator
        is zero. Only its entry in ``partial_counts`` distinguishes it.

        Here the mapping counts are genuinely zero *and* complete, so the ``0`` is
        exact and unmarked, while the file counts are floors and marked. Both live in
        the same response, which is the case a single boolean could not express.
        """
        seed_multipage_file_validation(stats_tables["state_table"])
        monkeypatch.setattr(stats_handler, "_MAX_READ_PAGES", 1)

        _, body = call_stats()

        assert body["mapping_success_rate"] == 0
        assert isinstance(body["mapping_success_rate"], int)
        assert "mapping_success_rate" not in body["partial_counts"]

        assert isinstance(body["file_success_rate"], int | float)
        assert "file_success_rate" in body["partial_counts"]

    def test_the_deadline_is_derived_from_the_budget_and_shrinks_with_elapsed_time(self, stats_tables):
        """``_read_deadline`` is relative to the request's start, and never negative.

        Three properties, each of which a plausible refactor would break:

        1. It is ``started + (budget - reserve)``, so the window is what is left of
           the budget — anchoring it at *now* instead would give a slow request a
           full fresh window and make the 25 s Lambda timeout reachable again.
        2. Because it is anchored at the start, a later anchor yields a later
           deadline by exactly the same offset: the window shrinks as work ahead of
           it consumes budget rather than resetting.
        3. A budget smaller than the reserve clamps to the anchor rather than going
           negative, so a misconfiguration degrades to "read one page and mark it"
           and not to "read backwards in time".

        The budget itself is asserted to fit inside the Lambda's 25 s timeout
        (``compute-stack.ts:741``) with the reserve still in hand, because the whole
        point of the figure is that an overrun is a 200 carrying ``partial_counts``
        rather than a gateway timeout carrying no body.
        """
        anchor = 1_000.0
        expected_window = stats_handler._STATS_TIME_BUDGET_SECONDS - stats_handler._STATS_READ_RESERVE_SECONDS

        assert stats_handler._read_deadline(anchor) == anchor + expected_window
        assert stats_handler._read_deadline(anchor + 5.0) == anchor + 5.0 + expected_window

        assert stats_handler._STATS_READ_RESERVE_SECONDS > 0
        assert stats_handler._STATS_TIME_BUDGET_SECONDS + stats_handler._STATS_READ_RESERVE_SECONDS < 25.0, (
            "the budget plus its reserve must leave headroom inside the Lambda's 25s timeout, "
            "or an overrun is a gateway timeout with no body rather than a marked response"
        )

    def test_a_budget_below_the_reserve_clamps_to_the_anchor(self, stats_tables, monkeypatch):
        """Property 3 above, isolated: the deadline is never before the request began."""
        monkeypatch.setattr(stats_handler, "_STATS_TIME_BUDGET_SECONDS", 0.5)
        monkeypatch.setattr(stats_handler, "_STATS_READ_RESERVE_SECONDS", 2.0)

        assert stats_handler._read_deadline(1_000.0) == 1_000.0

    def test_the_page_cap_default_matches_the_in_tree_precedent(self, stats_tables):
        """100 pages, the figure ``cost_optimization_agent/main.py:823`` already uses.

        Pinned so that the number stays a deliberate borrowing rather than drifting
        into a second, unexplained constant for the same job.
        """
        assert stats_handler._MAX_READ_PAGES == 100


class TestShadowCompare:
    """The one-release verification behind ``STATS_COUNT_SHADOW_COMPARE`` (D2 §1.4).

    The index arm's one unclosable risk is *historical* rows: a ``file_validation``
    or ``schema_mapping`` row written before the state row carried a ``timestamp``
    is absent from a backfilled GSI, and no amount of reading the working tree
    settles whether any exist. D2's mitigation was to log both figures for one
    release and compare, rather than to keep a second read path in production.

    Which means the flag has to satisfy two opposing requirements, and both are
    asserted: turning it on must reveal a disagreement, and it must be **invisible
    in the response** — otherwise it is not a shadow read, it is a second read path
    with a switch on it.

    **Validates: Requirements 7.4**
    """

    def test_the_shadow_compare_is_off_by_default_and_costs_no_extra_read(self, stats_tables, monkeypatch):
        """Default off, because the shadow read *is* the scan R7.4 deletes.

        Asserted as the absence of any state-table Scan rather than as the absence
        of a log line: a flag that defaulted on would restore the exact cost this
        task removes, and the read count is what makes that visible.
        """
        monkeypatch.delenv("STATS_COUNT_SHADOW_COMPARE", raising=False)
        seed_state_rows(stats_tables["state_table"], [state_row("run-1", "file_validation", "success")])

        with recorded_dynamodb_calls() as recorded:
            call_stats()

        assert scans_against(recorded, STATE_TABLE) == [], (
            "the shadow compare ran without being asked — its scan is the cost D4 removes"
        )

    def test_enabling_it_scans_and_agrees_without_changing_the_response(self, stats_tables, monkeypatch):
        """On: one shadow scan per step, and a response identical to the unflagged one.

        The identical-response half is the load-bearing assertion. The shadow scans
        run *after* the payload is final precisely so that they cannot consume budget
        a counted read needed, and this is what pins that ordering: if the scans ran
        earlier they could push a later read past the deadline and turn the flag into
        something that changes numbers.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "file_validation", "error"),
                state_row("run-1", "schema_mapping", "success"),
            ],
        )

        monkeypatch.delenv("STATS_COUNT_SHADOW_COMPARE", raising=False)
        _, unflagged = call_stats()
        expire_stats_cache(stats_tables["state_table"])

        monkeypatch.setenv("STATS_COUNT_SHADOW_COMPARE", "true")
        with recorded_dynamodb_calls() as recorded:
            _, flagged = call_stats()

        assert len(scans_against(recorded, STATE_TABLE)) == 2, "one shadow scan per counted step"
        for key in COUNT_KEYS:
            assert flagged[key] == unflagged[key], f"the shadow compare changed {key}"
        assert flagged["partial_counts"] == unflagged["partial_counts"] == []

    def test_a_shadow_mismatch_is_logged_as_a_mismatch(self, stats_tables, monkeypatch):
        """The disagreement the flag exists to find, produced deliberately.

        A ``file_validation`` row with no ``timestamp`` is exactly the historical
        shape D2 could not rule out: the scan counts it, the index cannot see it. So
        seeding one makes the two figures differ by one, and the warning has to say
        so — a mismatch logged as agreement would make the whole verification
        worthless.
        """
        seed_state_rows(
            stats_tables["state_table"],
            [
                state_row("run-1", "file_validation", "success"),
                state_row("run-2", "file_validation", "success", timestamp=None),
            ],
        )
        monkeypatch.setenv("STATS_COUNT_SHADOW_COMPARE", "1")
        warnings: list[str] = []
        monkeypatch.setattr(stats_handler, "logger", _RecordingLogger(warnings))

        _, body = call_stats()

        assert body["total_files_processed"] == 1, "the index arm counted the timestamp-less row"
        mismatches = [line for line in warnings if "MISMATCH" in line and "file_validation" in line]
        assert len(mismatches) == 1, f"expected one mismatch warning, got {warnings}"
        assert "index=1" in mismatches[0]
        assert "scan=2" in mismatches[0]

    def test_a_shadow_failure_cannot_fail_the_request(self, stats_tables, monkeypatch):
        """A lost measurement is not a lost response.

        The shadow scan is refused at the wire, which is the same injection D1 uses
        for the approvals failure. The request must still be a 200 with every count
        intact and — the sharper claim — with ``partial_counts`` still empty: a
        failure in the *verification* must not be reported as a truncation of the
        thing being verified.
        """
        seed_state_rows(stats_tables["state_table"], [state_row("run-1", "file_validation", "success")])
        monkeypatch.setenv("STATS_COUNT_SHADOW_COMPARE", "yes")

        # The payload is complete before the shadow scan runs, so the cache write has
        # already happened; the refused Scan can only affect the comparison.
        with recorded_dynamodb_calls(fail_scan_on_table=STATE_TABLE):
            status, body = call_stats()

        assert status == 200
        assert body["total_files_processed"] == 1
        assert body["successful_files"] == 1
        assert body["partial_counts"] == [], (
            "a failed shadow comparison was reported as a truncated count — the two are "
            "different conditions and must not share a signal"
        )


class _RecordingLogger:
    """Captures ``logger.warning`` / ``.info`` text without a logging-config dependency.

    ``handlers/common.py:30`` is an ``aws_lambda_powertools`` ``Logger``, whose
    propagation to the stdlib root is not something a test should assume, so
    ``caplog`` is not a reliable instrument here. Substituting the module's ``logger``
    name is, and it is the same ``monkeypatch.setattr(stats_handler, ...)`` technique
    the table handles already use.
    """

    def __init__(self, warnings: list[str]) -> None:
        self.warnings = warnings
        self.infos: list[str] = []

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(str(message))

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.infos.append(str(message))

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(str(message))

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        pass
