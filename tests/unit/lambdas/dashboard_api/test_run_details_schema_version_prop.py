#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4 — an unresolved schema version is absent, never a substitute.

Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved schema
version is absent, never a substitute

p54 R4.1 / R4.4, design 4.4 and *Correctness Properties* Property 4:

    *For any* hash-to-version map and any run schema hash, the Run_Detail_Read
    response carries ``schema_version`` with the mapped ordinal exactly when the
    hash is a key of the map, and carries no ``schema_version`` key at all
    otherwise — never ``''``, never ``0``, never ``null``.

WHY THIS IS A PROPERTY AND NOT A TWELFTH EXAMPLE
------------------------------------------------
``test_run_details_schema_version.py`` already holds 11 examples: three on the
resolve path, five omission routes, a no-partition-key guard and two read-cost
assertions. Each asserts **one outcome for one input**. Three things are only
sayable generatively, and each is a clause of the requirement that no example
states:

1. **The biconditional, as a biconditional.** The examples assert
   "this hash resolves to 3" and "that hash is absent" at five separate points.
   The requirement is an *iff*: presence and membership move together on **every**
   input. The two halves fail in opposite directions — a defaulting
   implementation is present-when-absent, an over-eager guard is absent-when-
   present — and only sweeping the joint space of (map × run hash) exercises both
   against one oracle.

2. **The ordinal, not merely the presence.** ``KEY in body`` is satisfied by
   ``body[KEY] == 1`` for every input. The examples pin the ordinal at exactly
   two points (1 and 3). Here the map's size, the run hash's position in it and
   the page split are drawn independently, so a substitute constant, an
   off-by-one, or a "length of the map" reading is caught wherever it sits.

3. **Prefix-colliding keys.** Nothing in the tree exercises them. The derivation
   is a dict membership test, which is exact — but the *only* thing making it
   exact is that it is a dict lookup, and a future substring or ``startswith``
   reading would keep every existing example green. ``_prefix_family`` below
   draws hash families like ``abc`` / ``abcd`` / ``abcde`` and a run hash that is
   a strict prefix or extension of a real key, which is absent under the
   requirement and present under a substring reading.

WHAT ``0`` ACTUALLY IS HERE, MEASURED
-------------------------------------
``_schema_hash_versions`` **cannot produce ``0``**. Its one assignment is
``versions[schema_hash] = len(versions) + 1`` (``pipelines_runs.py:503``) against
a dict that starts empty, so the first ordinal is ``1`` and every value is
``>= 1``. Verified by running the rule, not by reading it.

That matters for what this file can and cannot test. The ``0`` R4.4 forbids is
**not** reachable through the map's values — it is reachable only through a
*defaulted assignment*, ``_hash_versions.get(hash, 0)`` in place of the
membership test. No generator over map contents can produce it, so:

* the property asserts **membership** and, where present, an ``int >= 1`` equal
  to the drawn ordinal — never truthiness. ``0`` and ``None`` are both falsy, so
  ``not body.get(KEY)`` would *pass* on exactly the two outputs R4.4 forbids;
* the invariant that the producer's own values are all ``>= 1`` is asserted here
  too (``test_the_derived_map_never_carries_a_zero_ordinal``), because it is the
  reason ``0`` in a response could only ever have come from a default;
* the defaulted-assignment mutations that *do* produce ``''``, ``0`` and ``None``
  were run against this file as mutation tests rather than encoded as inputs. All
  were caught. Recorded in the task report, not simulated here — a test that
  injects the defect it is meant to detect asserts nothing about the real code.

WHAT IS GENERATED
-----------------
* **The map, via its producer.** Never patched in. The ``PipelineIndex`` pages
  are seeded with generated rows and the **real** ``_schema_hash_versions``
  derives the map from them, because a fixture that handed the derivation a
  ready-made map could not detect the producer drifting — the output would be its
  own oracle (``LGR-28b4d75-5``, **template / ratified**: expectations must not be
  transcribed past the producer). The oracle instead comes from the *draw*: the
  distinct hashes are drawn as an **ordered unique list**, so ``distinct[i]``'s
  ordinal is ``i + 1`` by construction of the draw and nothing recomputes
  ``_schema_hash_versions``' rule.
* **Repeats and blank-hash rows**, interleaved so that first-appearance order is
  preserved: a repeat and a row carrying no ``schema_hash`` both contribute no
  new ordinal, which is what makes the number an ordinal of first appearance
  rather than a row count.
* **The page split** — one page, or two to four pages with ``LastEvaluatedKey``,
  so the multi-page walk is exercised rather than assumed.
* **Whether the walk is truncated.** When it is, the effective map is the prefix
  the first page established, and the oracle uses that prefix — so the
  biconditional is asserted against the map the response was actually built from.
  This is the case where a naive implementation reaches for a substitute, and on
  this surface truncation is expressed as the *same* omission as an unknown hash
  (design 4.4: run detail has no ``pagination`` envelope and must not gain one).
* **The run's hash**, as one of six relations to that map: a key of it, a hash
  absent from it, a strict prefix of a key, a strict extension of a key, blank,
  or no ``schema_extract`` row at all.
* **The extract row's spelling**, over the six variants that canonicalise to
  ``StepName.SCHEMA_EXTRACT``. Every one verified by running ``from_any`` over it;
  ``ExtractSchema`` and ``schema_extraction`` do **not** canonicalise and are in
  the near-miss pool instead.

WHAT IS NOT GENERATED, AND WHY
------------------------------
* **A raising walk.** ``LGR-8e34f37-3``'s omit-on-failure path is deterministic
  and is pinned by ``test_a_walk_that_raises_omits_the_key_and_keeps_the_200`` in
  the sibling file. A generator over exception types would be a second spelling
  of one guard.
* **The page cap.** ``_MAX_SCHEMA_VERSION_PAGES = 100`` needs 100 seeded pages to
  bind and lands on the same omission the deadline route already covers, so it
  buys a slower test and no new outcome.
* **Whitespace-only hashes.** ``" "`` is truthy, so a row carrying it addresses a
  map key no producer writes; generating it would manufacture a counterexample
  about an input that does not exist. Same ground as the sibling property file.

FIXTURES
--------
**No fixture at all, deliberately.** The table handles are patched with
``patch.object`` over **freshly built** ``MagicMock``s inside each example, the
shape ``test_run_details_reuse_fact_prop.py`` established: a function-scoped
fixture is invoked **once** for a whole ``@given`` run, so a harness carrying
``return_value`` or ``call_count`` state between examples can make a property pass
vacuously. Building per example closes that by construction and needs no
``HealthCheck.function_scoped_fixture`` suppression.

``state_table.query`` serves **two** callers with different jobs — the run's own
base-table read and the ``PipelineIndex`` walk — so it dispatches on ``IndexName``
rather than serving one ``return_value`` to both. A shared ``return_value`` would
let the walk see the run's own rows and pass for the wrong reason. Same
``LGR-28b4d75-5`` ground as above, one level down.

No ``moto.mock_aws()`` is created here (``tests/unit/test_no_duplicate_mocking_prop.py``).

**Validates: Requirements 4.1, 4.4**
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path and import-time env — ``handlers/common.py`` calls ``require_env`` at
# import time, so these must precede the handler import.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _var, _value in {
    "AGENTIC_DATA_INGESTION_ENV": "local",
    "DYNAMODB_TABLE": "test-state-table",
    "MAPPINGS_TABLE": "test-mappings-table",
    "PIPELINES_TABLE": "test-pipelines-table",
    "GENERATED_CODE_TABLE": "test-generated-code-table",
    "TEST_RESULTS_TABLE": "test-results-table",
    "APPROVALS_TABLE_NAME": "test-approvals-table",
    "EXECUTIONS_TABLE": "test-executions-table",
    "AWS_REGION": "us-west-2",
}.items():
    os.environ.setdefault(_var, _value)

#: The property tag. Unlike the sibling property files it is **split across two
#: string literals**, because the tag is 115 characters and this module's line
#: limit is 120 — an indented single-line literal is 121. The concatenation is
#: verified byte-exact against the required tag; to grep for it, match on
#: ``Property 4: An unresolved schema version`` rather than the whole sentence.
PROPERTY_TAG = (
    "Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved schema version "
    "is absent, never a substitute"
)

RUN_ID = "run-schema-version-prop-001"
PIPELINE_ID = "pipe-schema-version-prop-001"

#: The attribute under test. R4.3 is about the SPELLING; this file is about R4.1
#: and R4.4, which are about the VALUE and its absence.
KEY = "schema_version"

#: Distinguishes "absent" from "present holding None" in one expression. ``None``
#: cannot be the default of a ``.get`` used to test for absence here, because
#: ``None`` is one of the three values R4.4 forbids the key from carrying.
_MISSING = object()

#: Spellings that all canonicalise to ``StepName.SCHEMA_EXTRACT``. Verified by
#: running ``from_any`` over each — ``_normalize`` strips ``_``/``-`` and
#: lowercases, and the PascalCase form arrives through ``SFN_STATE_NAMES``.
_EXTRACT_SPELLINGS: tuple[str, ...] = (
    "schema_extract",
    "SchemaExtract",
    "schema-extract",
    "SCHEMA_EXTRACT",
    "Schema_Extract",
    "schemaextract",
)

#: Step names that are NOT the extract step. The last two are the near-misses:
#: ``ExtractSchema`` and ``schema_extraction`` both raise out of ``from_any`` and
#: pass through ``_canonical_or_raw`` unchanged, so a row of either shape must not
#: be mistaken for the run's extract row.
_NOT_EXTRACT: tuple[str, ...] = (
    "schema_mapping",
    "schema_compare",
    "create_code",
    "test_code",
    "file_validation",
    "ExtractSchema",
    "schema_extraction",
)


def _state_row(step_name: str, seq: int, **extra: Any) -> dict[str, Any]:
    """A state-table row in the shape the ``run_id`` base-table query returns.

    ``original_pipeline_id`` is carried because the injection read keys on
    ``original_pipeline_id or pipeline_id``; without it that read never fires and
    an unrelated branch is skipped, which would make these examples pass for a
    reason other than the one they name.
    """
    row: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-schema-version-prop-001",
        "status": "success",
        "timestamp": f"2026-08-28T13:{seq:02d}:00+00:00",
        "step_sequence": seq,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Hash-shaped, and long enough that a strict prefix of one is still a plausible
#: hash. Drawn unique in ``_partition`` so no two distinct hashes collide.
_hashes = st.from_regex(r"[0-9a-f]{8,16}", fullmatch=True)


@st.composite
def _prefix_family(draw: st.DrawFn) -> list[str]:
    """A family of hashes that are prefixes of one another.

    ``abc`` / ``abcd`` / ``abcde`` — distinct keys where every one is a prefix of
    the next. A dict lookup separates them; a substring or ``startswith`` reading
    does not, and every existing example would stay green under that reading.
    """
    stem = draw(st.from_regex(r"[0-9a-f]{6,10}", fullmatch=True))
    extensions = draw(st.lists(st.sampled_from("0123456789abcdef"), min_size=1, max_size=3, unique=True))
    family = [stem]
    grown = stem
    for ch in extensions:
        grown = grown + ch
        family.append(grown)
    return family


@st.composite
def _partition(draw: st.DrawFn) -> tuple[list[list[dict[str, Any]]], list[str], bool]:
    """Draw the ``PipelineIndex`` pages, the ordered distinct hashes, and truncation.

    Returns ``(pages, distinct, truncated)`` where:

    * ``pages`` is the sequence of query responses the walk will read, each a
      ``{"Items": [...]}`` with a ``LastEvaluatedKey`` on all but the last;
    * ``distinct`` is the distinct hashes in **first-appearance order**, so
      ``distinct[i]``'s expected ordinal is ``i + 1`` — generator-side truth, not
      a re-derivation of the producer's rule;
    * ``truncated`` asks for the walk to be cut off after its first page.

    Repeats and rows carrying no ``schema_hash`` are interleaved *after* the hash
    they follow, so first appearance order — and therefore every ordinal — is
    fixed by the draw regardless of how the noise falls.
    """
    # Either an independent set of hashes, or a prefix-colliding family. The
    # second is the case nothing in the tree exercises.
    if draw(st.booleans()):
        distinct = draw(st.lists(_hashes, min_size=0, max_size=6, unique=True))
    else:
        distinct = draw(_prefix_family())

    rows: list[dict[str, Any]] = []
    for index, schema_hash in enumerate(distinct):
        rows.append({"schema_hash": schema_hash})
        # Noise that must not move any ordinal: a repeat of an already-seen hash,
        # a blank hash, and a row with no ``schema_hash`` attribute at all.
        for kind in draw(st.lists(st.sampled_from(("repeat", "blank", "absent")), max_size=2)):
            if kind == "repeat":
                rows.append({"schema_hash": distinct[draw(st.integers(min_value=0, max_value=index))]})
            elif kind == "blank":
                rows.append({"schema_hash": ""})
            else:
                rows.append({"run_id": "unrelated"})

    # Split into pages. ``max(1, ...)`` keeps a single empty page for an empty
    # partition, which is a real state: a pipeline with no extract rows yet.
    page_count = draw(st.integers(min_value=1, max_value=4))
    size = max(1, (len(rows) + page_count - 1) // page_count) if rows else 1
    chunks = [rows[i : i + size] for i in range(0, len(rows), size)] or [[]]
    pages = [{"Items": chunk} for chunk in chunks]
    for page in pages[:-1]:
        page["LastEvaluatedKey"] = {"pipeline_id": PIPELINE_ID, "timestamp": "cursor"}

    truncated = draw(st.booleans())
    return pages, distinct, truncated


_RELATIONS = ("in_map", "not_in_map", "strict_prefix", "strict_extension", "blank_hash", "no_extract_row")


@st.composite
def _run_hash(draw: st.DrawFn, effective: list[str]) -> tuple[str | None, int | None, str]:
    """Draw the run's own hash as one of six relations to the effective map.

    ``effective`` is the map's key list — the distinct hashes the walk actually
    reaches, which is the whole list or its first-page prefix under truncation.

    Returns ``(schema_hash, expected_ordinal, relation)``. ``schema_hash`` is
    ``None`` for "no extract row at all". ``expected_ordinal`` is ``None`` for
    every relation the requirement says must be **absent**.
    """
    relation = draw(st.sampled_from(_RELATIONS if effective else ("not_in_map", "blank_hash", "no_extract_row")))

    if relation == "in_map":
        index = draw(st.integers(min_value=0, max_value=len(effective) - 1))
        return effective[index], index + 1, relation
    if relation == "strict_prefix":
        key = draw(st.sampled_from(effective))
        # A strict, non-empty prefix that is not itself a key.
        candidates = [key[:n] for n in range(1, len(key)) if key[:n] not in effective]
        if not candidates:
            return draw(st.from_regex(r"zz[0-9a-f]{8}", fullmatch=True)), None, "not_in_map"
        return draw(st.sampled_from(candidates)), None, relation
    if relation == "strict_extension":
        key = draw(st.sampled_from(effective))
        grown = key + draw(st.sampled_from("0123456789abcdef"))
        while grown in effective:
            grown = grown + "0"
        return grown, None, relation
    if relation == "blank_hash":
        return "", None, relation
    if relation == "no_extract_row":
        return None, None, relation
    # not_in_map — the ``zz`` prefix is outside the ``[0-9a-f]`` hash alphabet, so
    # it can collide with nothing drawn above.
    fresh = draw(st.from_regex(r"zz[0-9a-f]{8}", fullmatch=True))
    return fresh, None, relation


@st.composite
def _scenario(draw: st.DrawFn) -> dict[str, Any]:
    """One complete example: the partition, the run's rows, and the oracle."""
    pages, distinct, truncated = draw(_partition())

    # The EFFECTIVE map is what the walk actually reaches. Truncation stops it
    # after page one, so the map is the prefix that page established — and the
    # biconditional is asserted against that, not against the full partition.
    if truncated and len(pages) > 1:
        first_page_hashes = [
            h for h in (row.get("schema_hash", "") for row in pages[0]["Items"]) if h and isinstance(h, str)
        ]
        effective: list[str] = []
        for h in first_page_hashes:
            if h not in effective:
                effective.append(h)
    else:
        truncated = False
        effective = list(distinct)

    schema_hash, expected_ordinal, relation = draw(_run_hash(effective))

    other_steps = draw(st.lists(st.sampled_from(_NOT_EXTRACT), unique=True, max_size=4))
    rows = [_state_row(name, seq) for seq, name in enumerate(other_steps, start=1)]
    if schema_hash is not None:
        rows.append(
            _state_row(
                draw(st.sampled_from(_EXTRACT_SPELLINGS)),
                len(rows) + 1,
                schema_hash=schema_hash,
            )
        )
    if not rows:
        # Zero state rows is a 404 and a different requirement's subject
        # (``test_run_details_not_found_prop.py`` owns it).
        rows = [_state_row("file_validation", 1)]

    return {
        "pages": pages,
        "state_rows": rows,
        "truncated": truncated,
        "effective": effective,
        "run_hash": schema_hash,
        "expected_ordinal": expected_ordinal,
        "relation": relation,
    }


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def _invoke(
    state_rows: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    *,
    truncated: bool,
) -> tuple[int, dict[str, Any], int]:
    """Call ``get_run_details`` over freshly built table mocks.

    Returns ``(status, body, walk_call_count)``. The call count is returned so an
    example can assert the walk actually ran rather than that a block did nothing.

    ``handlers/pipelines_runs.py`` copies the table handles into its own namespace
    with ``from .common import …``, so both modules are patched or the handler
    reads names that were never replaced.
    """
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    state = MagicMock()
    approvals = MagicMock()
    executions = MagicMock()
    pipelines = MagicMock()
    dynamo = MagicMock()

    remaining = list(pages)

    def _dispatch_query(*, IndexName: str | None = None, **_kw: Any) -> dict[str, Any]:
        """Route the run's base-table read and the ``PipelineIndex`` walk apart.

        A shared ``return_value`` would let the walk see the run's own state rows
        and resolve a version for the wrong reason (``LGR-28b4d75-5``).
        """
        if IndexName != "PipelineIndex":
            return {"Items": state_rows}
        return remaining.pop(0) if remaining else {"Items": []}

    state.query.side_effect = _dispatch_query
    approvals.query.return_value = {"Items": []}
    executions.query.return_value = {"Items": []}
    executions.get_item.return_value = {}
    pipelines.get_item.return_value = {}
    dynamo.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    handles = (
        ("state_table", state),
        ("approvals_table", approvals),
        ("executions_table", executions),
        ("pipelines_table", pipelines),
        ("dynamodb", dynamo),
    )
    with ExitStack() as stack:
        for module in (_common, _runs):
            for attribute, handle in handles:
                stack.enter_context(patch.object(module, attribute, handle))
        stack.enter_context(patch.object(_runs, "get_step_logs", lambda *a, **kw: []))
        if truncated:
            # A deadline already behind the clock stops the walk after its first
            # page — ``_schema_hash_versions`` checks the clock only *after*
            # establishing there is a continuation. The shape
            # ``_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS``' own docstring
            # recommends: shorten the deadline rather than assert a duration.
            stack.enter_context(patch.object(_runs, "_schema_version_walk_deadline", lambda _started: float("-inf")))
        response = _runs.get_run_details(RUN_ID)

    walk_calls = [c for c in state.query.call_args_list if c.kwargs.get("IndexName") == "PipelineIndex"]
    return response["statusCode"], json.loads(response["body"]), len(walk_calls)


# ===========================================================================
# Property 4
# ===========================================================================


class TestAnUnresolvedSchemaVersionIsAbsent:
    """Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved
    schema version is absent, never a substitute."""

    @given(scenario=_scenario())
    @settings(max_examples=250, deadline=None)
    def test_present_with_the_mapped_ordinal_exactly_when_the_hash_is_a_key(
        self,
        scenario: dict[str, Any],
    ) -> None:
        """Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved
        schema version is absent, never a substitute.

        The biconditional, over the joint space of (derived map × run hash ×
        page split × truncation):

        * the key is **present** exactly when the run's hash is a key of the map
          the walk actually built, and then carries that key's ordinal;
        * the key is **absent** in every other case — asserted by **membership**,
          because ``0`` and ``None`` are both falsy and a truthiness check would
          pass on precisely the two outputs R4.4 forbids;
        * no substitute is ever carried: not ``''``, not ``0``, not ``None``;
        * and nothing invents a ``pagination`` envelope to report a short map.

        **Validates: Requirements 4.1, 4.4**
        """
        status, body, walk_calls = _invoke(scenario["state_rows"], scenario["pages"], truncated=scenario["truncated"])

        # The derivation never changes the response's own outcome.
        assert status == 200

        expected = scenario["expected_ordinal"]
        observed = body.get(KEY, _MISSING)
        context = (
            f"relation={scenario['relation']}, run_hash={scenario['run_hash']!r}, "
            f"effective_map_keys={scenario['effective']!r}, truncated={scenario['truncated']}, "
            f"pages={len(scenario['pages'])}, walk_calls={walk_calls}"
        )

        if expected is not None:
            # ── The forward half: a key of the map resolves to ITS ordinal ──
            assert KEY in body, f"the hash IS a key of the map but the version was omitted — {context}"
            assert observed == expected, f"expected ordinal {expected}, got {observed!r} — {context}"
            # An ordinal, not a placeholder that happens to compare equal.
            assert isinstance(observed, int) and not isinstance(observed, bool), (
                f"the version must be an integer ordinal, got {type(observed).__name__} — {context}"
            )
            assert observed >= 1, (
                f"ordinals are ones-based (``len(versions) + 1`` on an empty dict), "
                f"so {observed!r} cannot be one — {context}"
            )
        else:
            # ── The reverse half: MEMBERSHIP, not truthiness ────────────────
            assert observed is _MISSING, (
                f"an unresolved schema version must be ABSENT, not carried as {observed!r}. "
                f"``LGR-44d601e-1`` clears a value by removing the key precisely so no reader "
                f"has to distinguish a substitute from absence — {context}"
            )
            # Spelled out, because these three are the named prohibitions and a
            # reader of a failure should see which one landed.
            assert body.get(KEY, _MISSING) not in ("", 0, None), f"a substitute value was carried — {context}"

        # A short map is expressed as omission on this surface. Run detail is a
        # single object; ``partial_counts`` is a property of a page (design 4.4).
        assert "pagination" not in body, f"run detail must not gain a pagination envelope — {context}"
        assert "partial_counts" not in body, f"nor a bare marker key — {context}"

        # One fact, one key. A second spelling would let the two list surfaces
        # keep working while only the run-detail cell went blank (R4.3).
        assert not any(k.startswith("schema_ver") and k != KEY for k in body), (
            f"a second spelling of one fact appeared — {context}"
        )

    @given(scenario=_scenario())
    @settings(max_examples=150, deadline=None)
    def test_a_hash_the_map_does_not_carry_is_never_substituted(
        self,
        scenario: dict[str, Any],
    ) -> None:
        """Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved
        schema version is absent, never a substitute.

        The same property restricted to the arm R4.4 names explicitly, so the
        absent case is the whole sweep rather than a slice of it: the run's hash
        is replaced with one **guaranteed** outside the map — a fresh hash, a
        strict prefix of a key, and a strict extension of a key, each drawn from
        the map that was actually built.

        The prefix and extension cases are the ones nothing else in the tree
        covers. Under the requirement all three are absent. Under a substring or
        ``startswith`` reading of the lookup, two of them would resolve — and
        every example in ``test_run_details_schema_version.py`` would still pass.

        The walk is asserted to have run, so a block that did nothing at all
        cannot satisfy this vacuously.

        **Validates: Requirements 4.1, 4.4**
        """
        effective = scenario["effective"]
        rows = [r for r in scenario["state_rows"] if "schema_hash" not in r]

        # Every shape that must NOT resolve, built against the real map.
        outsiders = ["zz" + "0" * 8]
        if effective:
            longest = max(effective, key=len)
            if len(longest) > 1 and longest[:-1] not in effective:
                outsiders.append(longest[:-1])
            extension = longest + "f"
            while extension in effective:
                extension += "0"
            outsiders.append(extension)

        for outsider in outsiders:
            state_rows = [*rows, _state_row("schema_extract", len(rows) + 1, schema_hash=outsider)]
            status, body, walk_calls = _invoke(state_rows, scenario["pages"], truncated=scenario["truncated"])

            context = (
                f"outsider={outsider!r}, effective_map_keys={effective!r}, "
                f"truncated={scenario['truncated']}, walk_calls={walk_calls}"
            )
            assert status == 200, f"bookkeeping about a run must never fail the run — {context}"
            assert outsider not in effective, f"the harness built a hash that IS in the map — {context}"
            assert walk_calls >= 1, f"the walk did not run, so this example asserts nothing — {context}"
            assert body.get(KEY, _MISSING) is _MISSING, (
                f"a hash the map does not carry was answered with {body.get(KEY)!r} instead of "
                f"omission (R4.4) — {context}"
            )

    @given(scenario=_scenario())
    @settings(max_examples=100, deadline=None)
    def test_the_derived_map_never_carries_a_zero_ordinal(
        self,
        scenario: dict[str, Any],
    ) -> None:
        """Feature: p54-reused-and-skipped-run-honesty, Property 4: An unresolved
        schema version is absent, never a substitute.

        The reason ``0`` in a response could only ever be a **default**.

        ``_schema_hash_versions``' one assignment is
        ``versions[schema_hash] = len(versions) + 1`` against a dict that starts
        empty, so ordinals are ones-based and no partition — however many rows,
        repeats, blank hashes or pages it has — can put ``0`` in the map. Asserted
        over the real producer rather than read off the line, because it is the
        premise the other two properties rest on: if the map cannot carry ``0``,
        then a ``0`` reaching a response came from a defaulted lookup, which is
        exactly the substitution R4.4 forbids.

        Also asserts the ordinals are a contiguous ``1..n`` over first appearance,
        which is what makes "the mapped ordinal" in the property statement a
        well-defined number rather than an arbitrary label.

        **Validates: Requirements 4.1**
        """
        from handlers import pipelines_runs as _runs

        state = MagicMock()
        remaining = list(scenario["pages"])
        state.query.side_effect = lambda **_kw: remaining.pop(0) if remaining else {"Items": []}

        with patch.object(_runs, "state_table", state):
            versions, _complete = _runs._schema_hash_versions(PIPELINE_ID, float("inf"))

        assert 0 not in versions.values(), (
            f"the producer emitted a zero ordinal, which would make ``0`` an ambiguous "
            f"answer rather than an impossible one: {versions!r}"
        )
        assert all(isinstance(v, int) and v >= 1 for v in versions.values()), f"non-ordinal value in {versions!r}"
        assert sorted(versions.values()) == list(range(1, len(versions) + 1)), (
            f"ordinals are not a contiguous 1..n over first appearance: {versions!r}"
        )
