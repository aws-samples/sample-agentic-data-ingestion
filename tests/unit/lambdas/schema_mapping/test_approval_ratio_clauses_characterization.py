# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 1.1 — characterization of `determine_approval_requirement`'s
confidence and unmapped-source-ratio clauses, pinned BEFORE W5 inserts a
refusal ahead of them.

**Function under test, named by file and line because the name is not unique in
this repository:** `determine_approval_requirement` in
`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1552`. The task
text cites the body as `:1580-1675`; the span verified by reading it in this tree
is `:1573-1674` (`try:` at `:1573`, the swallow's `return True` at `:1674`), and
every line reference below is the verified one. Exactly two of its clauses are
pinned here:

- `:1646` — `avg_confidence < CONFIDENCE_AUTO_APPROVE_THRESHOLD` requires a
  human decision (requirement 12.4).
- `:1650` — `len(unmapped_source) / total_source_fields > 0.2` requires a human
  decision (requirement 12.3).

**Expectations derive from the requirements, not from current output.** A
characterization test built from observed output ratifies whatever defect is in
it. The two boundaries asserted below come from:

- `shared/config.py`'s documented meaning of `CONFIDENCE_AUTO_APPROVE_THRESHOLD`
  (module docstring `:40-46`, constant at `:165`): "average mapping confidence
  **at or above** this value is eligible for auto-approve; below it, human
  approval is required". So *at* the threshold this clause must NOT force, and
  strictly below it must. Note the spec text cites `shared/config.py:144` for
  this constant; the constant is at `:165` in this tree — the meaning quoted is
  the same either way.
- requirement 14's stated boundary: today's approval clause "fires when the
  unmapped source proportion exceeds one fifth … i.e. when the
  Source_Field_Mappability_Ratio is below 0.8". So exactly one fifth must NOT
  force on this clause, and anything above one fifth must.

**Why this file exists at all.** Coverage of this function is real but thin on
precisely these two clauses. `LGR-9e76249-7` (template / ratified; BROAD, 80
files — read as background) names `test_schema_mapping_approval.py` as pinning
the observed run's 1-of-8 / 0.95 arithmetic, which is **one point, not the
boundary**, and it pins it in order to prove the ratio rule is *not* what fires
there. CodeGraph's "no covering tests" flag on this symbol is a false flag —
`codegraph impact` reports 16 test functions in that file — so the gap was never
coverage, it was what is asserted. This file adds the boundary and asserts
nothing about the clause ordering, which is deliberate: the comment at
`:1628-1640` states "ORDERING IS LOAD-BEARING" for the required-target-unsourced
clause sitting before these two, and `LGR-9e76249-7` is the record behind it.
Every call below passes `target_schema=None`, so that clause is inert and
untouched.

**Ledger state for these two clauses: unfenced.** `why` on
`schema_mapping/lambda_function.py:1646` returns only the broad snapshot commit
`0176ccc686` with `records: []` — the documented 634-file snapshot trap. That is
a gap in the ledger, not evidence the clauses are free to move.

Two deliberate non-assertions:

1. **The trailing `except Exception: return True` (`:1671-1674`) is not pinned as
   intended behaviour.** Every call here asserts `logger.error` was never
   reached — its call at `:1672` is the only `logger.error` in the function — so
   a `True` manufactured by the swallow fails the test instead of passing as a
   characterization. Same reasoning as `test_schema_mapping_approval_overrides_prop.py`
   (`:192`, `:389`) and as **`LGR-b2f79ef-13`** (template / ratified), which
   narrowed a blanket `except Exception` that re-labelled a classified exception
   out of `get_target_schema` (`:303-305`) in this same module — a reasoning
   precedent on this file, not a direct fence on these lines.

   **Cite `LGR-b2f79ef-13`, not `LGR-78ce0b7-13`.** The two commits carry one
   decision set and only the `b2f79ef` side survived the ledger recompile, so the
   `78ce0b7` id resolves to nothing. `LGR-7561acb-6` (template / ratified) is the
   record that documents the substitution: *"a dead twin hash, not a missing
   record. Before treating a dangling `LGR-` id as an undocumented area, try the
   twin."* Two tracked siblings in this directory still carry the dead id
   (`test_schema_mapping_approval_overrides_prop.py:192,389`); they are left
   alone because this task changes one new file, and `LGR-7561acb-6` is what
   keeps them readable.
2. **No property test.** design §6 fixes the property set for this spec at six,
   and task 1.1 is assigned none of them; task 1.3 is the one that carries
   Property 5. Adding a seventh property here would be work against the design.

Production code is unchanged by this file.

**Validates: Requirements 12.3, 12.4**
"""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)` rather than `@patch("lambda_function.…")`, so nothing
# here depends on sys.modules still pointing at this module at call time.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: The threshold the `:1646` clause compares against, read from the module under
#: test rather than restated. `shared/config.py` is its single source (P28
#: R18.2) and `tests/unit/shared/test_config.py` owns its value.
_THRESHOLD = _mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD

#: Requirement 14's statement of the `:1650` boundary: the clause fires when the
#: unmapped source proportion EXCEEDS one fifth, i.e. below 0.8 mappability.
_UNMAPPED_SOURCE_BOUNDARY = 0.2


# --- Fixtures -----------------------------------------------------------------


def _pipeline_metadata() -> dict:
    """A pipeline whose configuration reaches the quality clauses.

    `schema_mapping_enabled` true, so `:1605-1607` does not short-circuit to
    `False`.
    `always_approve_schema_changes` is irrelevant here because every call passes
    `schema_change_action=None`, but it is set explicitly so the fixture does
    not depend on the `.get(…, True)` default.
    """
    return {
        "pipeline_id": "p55-task-1-1",
        "approval_config": {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal(str(_THRESHOLD)),
            "code_generation_enabled": True,
            "always_approve_schema_changes": True,
        },
    }


def _mapping_result(*, mapped: int, unmapped_source: int, confidences: list[float] | None = None) -> dict:
    """A mapping result carrying only the axes these two clauses read.

    `unmapped_target_fields` is always empty, so the sibling target-ratio clause
    at `:1656` computes 0 and cannot be what fires. `generated_by` is a model id
    and never `FALLBACK_GENERATED_BY`, and `requires_manual_approval` is absent,
    so the P28 R3.1 fallback clause at `:1578` is inert. Every mapping entry
    carries a usable `source_field`, so `unsourced_target_field_names` returns
    nothing and the p37 R3.3 forcing clause at `:1642` is inert too.

    AMENDED for T-04: each entry's source and target now carry the SAME name
    (`field_i`), where they used to be `src_i` → `tgt_i`. T-04 added a clause
    after these two that requires every entry to be corroborated by something
    the model cannot write — here, the two names agreeing — and `src_i`/`tgt_i`
    share no name, so that clause, not the one under test, would have decided
    every `False` arm below. Matching the names keeps it inert, exactly as the
    other clauses above are kept inert, so each result is still attributable to
    the clause it names.
    """
    if confidences is None:
        confidences = [1.0] * mapped
    assert len(confidences) == mapped, "one confidence per mapping entry"

    return {
        "mappings": [
            {"source_field": f"field_{i}", "target_field": f"field_{i}", "confidence": c}
            for i, c in enumerate(confidences)
        ],
        "unmapped_source_fields": [f"orphan_{i}" for i in range(unmapped_source)],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _decide(mapping_result: dict) -> bool:
    """Call the function under test and refuse to accept an answer it did not compute.

    Two guards make a result attributable:

    - `get_pipeline_metadata` must have been read, which proves execution
      reached the quality clauses rather than returning early at `:1597-1599`
      (missing metadata defaults to `True`).
    - `logger.error` must never have been called. It appears exactly once in
      this function, at `:1672` inside the trailing `except Exception`
      (`:1671-1674`), so this is what stops the swallow's `return True` being
      recorded here as the documented behaviour of a clause.
    """
    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = _pipeline_metadata()
        result = _mod.determine_approval_requirement("p55-task-1-1", mapping_result, None)

        assert mock_get_meta.called, "the approval configuration was never read — an early return, not a clause"
        mock_logger.error.assert_not_called()

    return result


# --- Requirement 12.4: the confidence clause (`:1646`) ------------------------


def test_average_confidence_below_the_configured_threshold_requires_a_human():
    """R12.4. `shared/config.py`: below the threshold, human approval is required."""
    assert 0 < _THRESHOLD <= 1, "a threshold outside (0, 1] would make every assertion here vacuous"

    below = _THRESHOLD - 0.01
    mapping_result = _mapping_result(mapped=1, unmapped_source=0, confidences=[below])

    assert below < _THRESHOLD, "the fixture must actually sit below the gate or this test proves nothing"
    assert _decide(mapping_result) is True


def test_average_confidence_exactly_at_the_configured_threshold_does_not_require_a_human():
    """R12.4's other arm, from `shared/config.py`'s "at or above … is eligible".

    The boundary is `<`, not `<=`. Asserting only the below-threshold arm would
    leave a change from `<` to `<=` — which converts every exactly-at-threshold
    run into a human decision — invisible.
    """
    mapping_result = _mapping_result(mapped=1, unmapped_source=0, confidences=[_THRESHOLD])

    # One entry, so the average IS the threshold with no floating-point slack.
    assert sum(m["confidence"] for m in mapping_result["mappings"]) / 1 == _THRESHOLD
    assert _decide(mapping_result) is False


def test_the_confidence_clause_reads_the_average_and_not_the_lowest_entry():
    """R12.4 says "average mapping confidence", and the two readings differ.

    Both fixtures below contain an entry under the threshold. Only the one whose
    *average* falls under it requires a human. A clause rewritten to gate on the
    minimum would force a human on both.
    """
    average_above = _mapping_result(mapped=2, unmapped_source=0, confidences=[1.0, _THRESHOLD - 0.10])
    average_below = _mapping_result(mapped=2, unmapped_source=0, confidences=[1.0, _THRESHOLD - 0.30])

    assert min(m["confidence"] for m in average_above["mappings"]) < _THRESHOLD
    assert sum(m["confidence"] for m in average_above["mappings"]) / 2 > _THRESHOLD
    assert sum(m["confidence"] for m in average_below["mappings"]) / 2 < _THRESHOLD

    assert _decide(average_above) is False
    assert _decide(average_below) is True


# --- Requirement 12.3: the unmapped-source ratio clause (`:1650`) -------------


def test_unmapped_source_proportion_above_one_fifth_requires_a_human():
    """R12.3. 21 of 100 source fields unmapped — mappability 0.79, under R14's 0.8."""
    mapping_result = _mapping_result(mapped=79, unmapped_source=21)

    total_source = len(mapping_result["mappings"]) + len(mapping_result["unmapped_source_fields"])
    assert len(mapping_result["unmapped_source_fields"]) / total_source > _UNMAPPED_SOURCE_BOUNDARY
    # The confidence clause cannot be what fires: every entry is 1.0.
    assert sum(m["confidence"] for m in mapping_result["mappings"]) / 79 > _THRESHOLD

    assert _decide(mapping_result) is True


def test_unmapped_source_proportion_of_exactly_one_fifth_does_not_require_a_human():
    """R12.3's boundary, from R14: the clause fires when the proportion EXCEEDS one fifth.

    1 of 5 source fields unmapped is a mappability ratio of exactly 0.8, which
    requirement 14 places outside the approval-required region. This is the arm
    W5's strict-nesting claim (R14.7) is measured against, so a drift from `>`
    to `>=` here would silently move what reaches a human.
    """
    mapping_result = _mapping_result(mapped=4, unmapped_source=1)

    total_source = len(mapping_result["mappings"]) + len(mapping_result["unmapped_source_fields"])
    assert len(mapping_result["unmapped_source_fields"]) / total_source == _UNMAPPED_SOURCE_BOUNDARY
    assert _decide(mapping_result) is False


def test_a_mappability_ratio_of_one_half_requires_a_human_today():
    """R12.3 at the ratio W5 will refuse at — the premise of R14.7's strict nesting.

    Half the source fields unmapped is a 0.5 unmapped proportion, well above one
    fifth, so this run requires a human **before** W5 lands. That is what makes
    W5 a tightening: it converts a human decision into a refusal and never a
    human decision into a pass (do-NOT 13, R14.7). The 0.5 here is written as
    plain arithmetic on purpose — `MAPPABILITY_HARD_FAIL_THRESHOLD` does not
    exist yet, and task 1.4 owns the static assertion relating it to the `0.2`
    literal at `:1650`.
    """
    mapping_result = _mapping_result(mapped=5, unmapped_source=5)

    total_source = len(mapping_result["mappings"]) + len(mapping_result["unmapped_source_fields"])
    assert len(mapping_result["unmapped_source_fields"]) / total_source == 0.5
    assert _decide(mapping_result) is True


# --- The control the two `False` arms above are read against ------------------


def test_a_run_with_high_confidence_and_nothing_unmapped_does_not_require_a_human():
    """The local control, without which the two `False` results above prove nothing.

    Nothing unmapped on either side and every confidence at 1.0, so no clause in
    `:1578-1669` has anything to fire on. If this returned `True`, the two
    boundary `False`s would be measuring some unrelated short-circuit instead of
    the clauses named in this module's docstring.
    """
    mapping_result = _mapping_result(mapped=5, unmapped_source=0)

    assert mapping_result["generated_by"] != _mod.FALLBACK_GENERATED_BY
    assert "requires_manual_approval" not in mapping_result
    assert _decide(mapping_result) is False
