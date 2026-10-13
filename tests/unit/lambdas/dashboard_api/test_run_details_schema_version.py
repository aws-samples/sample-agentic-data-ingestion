# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``get_run_details`` derives ``schema_version``, or omits it.

p54 task 3.2 (R4.1, R4.3, R4.4). The subject is one block near the foot of
``handlers/pipelines_runs.py``'s ``get_run_details``, and the thing worth testing
about it is **what it does not emit**.

``schema_version`` has **no producer anywhere**. Task 3.2 re-ran p54 C-2's
negative search over the whole tree — including ``mcp/`` and
``pyspark_templates/``, which the original search did not cover — and confirmed
it: zero occurrences under ``mcp/``, ``pyspark_templates/``, ``shared/`` and
``deploy-cdk/``; and under ``lambda/`` only ``execution_updater:346-357``
(a *reader* whose two sources are themselves producerless),
``job_completion``/``schema_compare``'s unrelated ``target_schema_version`` on
the **pipelines** table, and ``dashboard_api``'s own read-path derivations. The
``schema_extract`` row — the one this run's version is *about* — carries
``schema_hash`` and a three-key ``summary`` of ``field_count`` / ``schema_hash``
/ ``is_new_schema``. No ordinal.

So the number is derived, by ``_schema_hash_versions`` walking ``PipelineIndex``
oldest-first and numbering each hash by its first appearance. That function had
one caller (``get_pipeline_executions``); this block is its second.

**The rule under test is R4.4: an unresolved version is ABSENT.** Never ``''``,
never ``0``, never ``null``. ``LGR-44d601e-1`` (**template / ratified**) is the
rule — a value is cleared by *removing* the key, "precisely so no reader has to
distinguish ``\"\"`` from absence" — and ``LGR-7561acb-7`` applies the same
reasoning to ``nullable``, rejecting a third spelling of two facts. Five distinct
routes reach that outcome and each gets an assertion here, because they are five
different pieces of code and a test that covers one proves nothing about the
others:

1. no ``schema_extract`` row at all;
2. a ``schema_extract`` row carrying no ``schema_hash``;
3. a hash the partition does not know;
4. a walk **truncated** by the deadline, so the map is short;
5. the walk **raising**.

Route 4 is the one with a design decision behind it. In
``get_pipeline_executions`` a short map is named in
``pagination.partial_counts`` via ``_SCHEMA_VERSION_DERIVED_KEY_ORDER``. This
response has **no ``pagination`` envelope and must not gain one** — so a
truncated walk is expressed as the same omission as an unknown hash, and
``test_a_truncated_walk_omits_rather_than_inventing_an_envelope`` asserts both
halves of that: the key absent, and no envelope key invented.

Route 5's degrade is deliberately *indistinguishable* from routes 1–4 in the
body, which is why no marker separates them. From the response, "the walk could
not run" and "the hash is not in the map" mean the same thing: no version is
established for this run. What route 5 additionally pins is
``LGR-8e34f37-3``'s omit-on-failure shape — the assignment is inside the block's
own ``try``, so a raise leaves the key off the body and the response stays 200.

**Not duplicated here.** ``test_run_history_ordering.py``'s
``TestSchemaVersionWalkIsBounded`` already pins the *executions* surface's
behaviour, including its ``partial_counts`` marker; that stays there. This file
is only about the run-detail surface, where the marker does not exist.

**The projection fences are not touched and not re-asserted here.**
``test_run_details_keyed_executions_lookup.py::test_the_injection_read_asks_only_for_what_it_uses``
owns the injection read's three-name projection (p54 do-NOT 2) and must keep
passing unmodified; this block reads neither executions row.

No moto: the module-level table objects are replaced with ``MagicMock``s, the
pattern every sibling reader test in this directory uses
(``test_run_details_lineage.py``, ``test_run_details_load_outcome.py``). One
wrinkle those files do not have — ``state_table.query`` is called for **two**
different things here, the run's own base-table read and the ``PipelineIndex``
walk, so the fixture dispatches on ``IndexName`` rather than serving one
``return_value`` to both. A shared ``return_value`` would let the walk see the
run's own rows and pass for the wrong reason (``LGR-28b4d75-5``: a fixture must
not be able to fake past the producer).

``AWS_REGION`` is deliberately **not** set here — ``tests/unit/conftest.py`` pins
it by assignment (``LGR-3e45e65-1``, **template / ratified**).

Validates: Requirements 4.1, 4.3, 4.4
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path / env setup — copied from test_run_details_lineage.py, which covers a
# sibling concern in the same function.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(LAMBDA_DIR), os.path.abspath(SHARED_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")

RUN_ID = "run-2026-08-27-11-02-04-b1c2d3e4"
PIPELINE_ID = "pipe-schema-version-001"

#: The hash this run's ``schema_extract`` row carries. Spelled as a constant so a
#: test that means "a DIFFERENT hash" has to say so.
RUN_HASH = "a1b2c3d4e5f60718"

#: The attribute under test. Named once because R4.3 is about the SPELLING — the
#: run-detail cell and both list surfaces must read one key
#: (``ExecutionsPage.tsx:383``, ``PipelineOverviewPage.tsx:542``,
#: ``pipelines_runs.py:1154``all read ``schema_version``).
KEY = "schema_version"


def _parse(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return response["statusCode"], json.loads(response["body"])


def _state_row(step_name: str, *, sequence: int, **extra: Any) -> dict[str, Any]:
    """A genuine Step_Row as the base-table query returns it.

    ``original_pipeline_id`` is carried because the injection read keys on
    ``original_pipeline_id or pipeline_id``; without it that read never fires and
    an unrelated branch is skipped, which would make these tests pass for a
    reason other than the one they name.
    """
    row: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "status": "success",
        "timestamp": f"2026-08-27T11:{sequence:02d}:00+00:00",
        "step_sequence": sequence,
    }
    row.update(extra)
    return row


def _extract_rows(*hashes: str) -> list[dict[str, Any]]:
    """``PipelineIndex`` rows as ``_schema_hash_versions`` sees them.

    It projects ``schema_hash`` only, and numbers each hash by first appearance
    in the order the page yields — so the position of a hash in this list IS its
    version number, and a repeat costs no new number.
    """
    return [{"schema_hash": h} for h in hashes]


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    # Defaults are the "nothing to say" answers, and every one is a *successful*
    # call returning nothing — a different thing from a call that raises, which
    # is the distinction the error-path test rests on.
    mock_approvals.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}
    mock_executions.query.return_value = {"Items": []}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    # ``state_table.query`` serves TWO callers with different jobs: the run's own
    # base-table read (no ``IndexName``) and ``_schema_hash_versions``' walk of
    # ``PipelineIndex``. Routed on ``IndexName``, which is what a real table does
    # — the two reads genuinely address different things.
    def _dispatch_query(*, IndexName=None, **_kw):
        seeded = _patch_tables.seeded["walk" if IndexName == "PipelineIndex" else "base"]
        if isinstance(seeded, Exception):
            raise seeded
        return seeded

    mock_state.query.side_effect = _dispatch_query
    _patch_tables.seeded = {"base": {"Items": []}, "walk": {"Items": []}}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Log fetching is covered elsewhere and would reach CloudWatch.
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.state = mock_state
    _patch_tables.runs = _runs
    _patch_tables.get_run_details = _runs.get_run_details
    yield


def _set_state_rows(rows: list[dict[str, Any]]) -> None:
    _patch_tables.seeded["base"] = {"Items": rows}


def _set_walk(rows: list[dict[str, Any]], *, last_key: dict[str, Any] | None = None) -> None:
    """Seed the ``PipelineIndex`` walk with one page.

    ``last_key`` present models a partition with **more to read** — which is what
    makes the deadline reachable at all: ``_schema_hash_versions`` checks the
    clock only *after* establishing there is a continuation, so the first page is
    always read and the bound applies to *continuing*.
    """
    page: dict[str, Any] = {"Items": rows}
    if last_key is not None:
        page["LastEvaluatedKey"] = last_key
    _patch_tables.seeded["walk"] = page


# ---------------------------------------------------------------------------
# R4.1 / R4.3 — the version resolves, under the one agreed key name
# ---------------------------------------------------------------------------


class TestTheVersionResolves:
    def test_the_run_carries_the_ordinal_of_its_own_hash(self):
        """R4.1. Third distinct hash in ascending order ⇒ ``schema_version: 3``.

        The number is an ordinal of **first appearance**, not a page length and
        not a row count — which is why the fixture repeats ``RUN_HASH`` after it
        first appears and the expected number does not move.
        """
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows("older-hash-1", "older-hash-2", RUN_HASH, RUN_HASH))

        status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert status == 200
        assert body[KEY] == 3, "the ordinal of RUN_HASH's first appearance, not the number of rows read"

    def test_the_key_is_spelled_the_way_both_run_lists_spell_it(self):
        """R4.3. One key, three surfaces.

        ``ExecutionsPage.tsx:383`` and ``PipelineOverviewPage.tsx:542`` both read
        ``item.schema_version``; the run-detail cell (task 4.2) reads
        ``runDetail.schema_version``. If this response ever spelled it
        differently the two list surfaces would keep working and only the new
        cell would go blank, which is the failure mode this asserts against.
        """
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows(RUN_HASH))

        _status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert KEY in body
        assert not any(k.startswith("schema_ver") and k != KEY for k in body), "no second spelling of one fact"

    def test_a_legacy_pascalcase_extract_row_still_resolves(self):
        """The row pick compares in canonical space (P28 R8.2).

        ``SchemaExtract`` is **not** in ``step_names._LEGACY_ALIASES`` — that map
        holds ``CodeGeneration``, ``CodeTesting``, ``schema_mapping_approval``,
        ``code_approval``, ``infrastructure`` and ``first_run``. The tolerance
        comes from ``_normalize``, which strips ``_`` and lowercases before
        lookup, so ``SchemaExtract`` and ``schema_extract`` both reach
        ``StepName.SCHEMA_EXTRACT`` through the *canonical* name. Asserted rather
        than assumed, because a reader who checked the alias table would
        conclude the tolerance was absent.
        """
        _set_state_rows([_state_row("SchemaExtract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows(RUN_HASH))

        _status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert body[KEY] == 1


# ---------------------------------------------------------------------------
# R4.4 — the five routes to absence. No ``''``, no ``0``, no ``null``.
# ---------------------------------------------------------------------------


class TestAnUnresolvedVersionIsAbsent:
    """Every assertion here is ``KEY not in body``, and that is the point.

    ``body[KEY] == 0`` and ``body[KEY] is None`` are the *prohibited* shapes, not
    weaker forms of the same thing: a reader handed ``0`` renders ``v0``, and one
    handed ``null`` has to know that ``null`` means unknown while ``0`` would
    have meant zero. ``LGR-44d601e-1`` removes the key so no reader must make
    that distinction.
    """

    def test_a_run_with_no_schema_extract_row_omits_the_key(self):
        """Route 1. A Skipped_Run, or one that failed before extraction."""
        _set_state_rows([_state_row("file_validation", sequence=1)])
        _set_walk(_extract_rows("some-other-hash"))

        status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert status == 200
        assert KEY not in body

    def test_an_extract_row_with_no_hash_omits_the_key(self):
        """Route 2. The row exists; it establishes no hash."""
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash="")])
        _set_walk(_extract_rows("some-other-hash"))

        _status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert KEY not in body

    def test_a_hash_the_partition_does_not_know_omits_the_key(self):
        """Route 3. The walk completed and simply does not carry this hash."""
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows("unrelated-hash-1", "unrelated-hash-2"))

        _status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        # Membership, not truthiness. ``0`` and ``None`` are the two shapes a
        # defaulted implementation produces and both are falsy, so a ``not
        # body.get(KEY)`` check would pass on exactly the outputs R4.4 forbids.
        assert KEY not in body

    def test_a_truncated_walk_omits_rather_than_inventing_an_envelope(self, monkeypatch):
        """Route 4, and the design decision behind it.

        The walk is cut short by the deadline before it reaches the page carrying
        this run's hash. On the **executions** surface that is reported by naming
        ``schema_version`` in ``pagination.partial_counts``. This response has no
        such envelope and must not gain one, so the truncation is expressed as
        the same omission as an unknown hash — asserted here in both halves.

        The deadline is forced by pushing it into the past rather than by
        sleeping: ``_schema_hash_versions`` compares ``time.monotonic()`` against
        the value handed to it, so a deadline already behind the clock stops the
        walk after its first page. That is the shape
        ``_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS``' own docstring recommends —
        "a test that needs this to bind shortens it rather than asserting a
        wall-clock duration".
        """
        monkeypatch.setattr(_patch_tables.runs, "_schema_version_walk_deadline", lambda _s: float("-inf"))
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        # A continuation token, so there IS more to read and the clock is checked.
        # The page yielded does not carry this run's hash.
        _set_walk(_extract_rows("first-page-hash"), last_key={"pipeline_id": PIPELINE_ID, "timestamp": "x"})

        status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert status == 200
        # The walk RAN and stopped, rather than never having been attempted —
        # without this the three assertions below would pass on a block that did
        # nothing at all.
        walk_calls = [c for c in _patch_tables.state.query.call_args_list if c.kwargs.get("IndexName")]
        assert len(walk_calls) == 1, "one page read, then the deadline stopped the continuation"
        assert KEY not in body, "a truncated walk leaves the key absent — R4.4"
        assert "pagination" not in body, "run detail has no pagination envelope and must not gain one"
        assert "partial_counts" not in body, "and no bare marker key either"

    def test_a_walk_that_raises_omits_the_key_and_keeps_the_200(self):
        """Route 5. ``LGR-8e34f37-3``'s omit-on-failure shape.

        The assignment lives inside the block's own ``try``, so a raising walk
        leaves the key off the body rather than defaulting it, and the rest of
        the response still goes out. ``body[KEY] == 0`` after a raise is the
        prohibited shape; absence is the correct one.
        """
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _patch_tables.seeded["walk"] = RuntimeError("PipelineIndex unavailable")

        status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert status == 200, "bookkeeping about a run must never fail the run"
        assert KEY not in body
        # The rest of the body is intact — the degrade is scoped to one key.
        assert body["run_id"] == RUN_ID
        assert body["steps"], "the steps the run really has are still reported"

    def test_the_walk_is_not_attempted_without_a_partition_key(self):
        """A run whose rows carry no ``pipeline_id`` cannot address a partition.

        ``PipelineIndex`` is hash-keyed on ``pipeline_id``, so an empty value
        would be a query for a partition that cannot exist. It is guarded rather
        than issued-and-discarded, so this asserts the call count, not just the
        absent key.
        """
        row = _state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)
        row["pipeline_id"] = ""
        row["original_pipeline_id"] = ""
        _set_state_rows([row])
        _set_walk(_extract_rows(RUN_HASH))

        _status, body = _parse(_patch_tables.get_run_details(RUN_ID))

        assert KEY not in body
        walk_calls = [c for c in _patch_tables.state.query.call_args_list if c.kwargs.get("IndexName")]
        assert walk_calls == [], "no partition key, no query"


# ---------------------------------------------------------------------------
# The block adds no reads beyond the one walk, and touches no fenced projection
# ---------------------------------------------------------------------------


class TestTheReadCost:
    def test_the_derivation_costs_exactly_one_extra_query(self):
        """One ``PipelineIndex`` query per run, and only when it can answer.

        The hash comes off ``items``, which ``get_run_details`` already holds, so
        the walk is the only added operation. Stated as a test because the
        per-run (rather than per-page) cost profile is the one open question
        design 4.4 records about this derivation.
        """
        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows(RUN_HASH))

        _patch_tables.get_run_details(RUN_ID)

        walk_calls = [c for c in _patch_tables.state.query.call_args_list if c.kwargs.get("IndexName")]
        assert len(walk_calls) == 1
        assert walk_calls[0].kwargs["IndexName"] == "PipelineIndex"
        assert walk_calls[0].kwargs["ProjectionExpression"] == "schema_hash"

    def test_neither_executions_read_was_widened(self):
        """p54 do-NOT 2, from this file's side.

        The projection fence is owned and asserted by
        ``test_run_details_keyed_executions_lookup.py``; this is the weaker,
        local claim that the ``schema_version`` derivation did not reach for
        either executions row — it reads ``items`` and ``PipelineIndex`` only.
        """
        from handlers import pipelines_runs as _runs

        _set_state_rows([_state_row("schema_extract", sequence=2, schema_hash=RUN_HASH)])
        _set_walk(_extract_rows(RUN_HASH))

        _patch_tables.get_run_details(RUN_ID)

        calls = _runs.executions_table.get_item.call_args_list
        projections = [c.kwargs.get("ProjectionExpression", "") for c in calls]
        # The attribute has no writer, so projecting it would return nothing.
        assert all(KEY not in p for p in projections), "neither executions read was widened"
