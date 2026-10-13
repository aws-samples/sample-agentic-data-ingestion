# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 3.2 — design Property 4.

# Feature: p55-format-support-and-teardown, Property 4: The mappability outcome is a three-way partition, and refusal is a strict subset

*For any* (mapped, total) pair with total > 0, the outcome is exactly one of
refuse / approval-required / defer-to-existing, and **refuse implies
approval-required would also have fired**.

**Validates: Requirements 14.1, 14.4, 14.7**

Subject, named by file and line because neither name is unique here
-------------------------------------------------------------------
Two functions, both in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``, verified at
these lines in this tree **after** task 3.1's change:

* ``enforce_source_field_mappability`` (``:1815``) — the refusal arm. Called
  once from ``lambda_handler`` (``:2182``) at ``:2405``.
* ``determine_approval_requirement`` (``:1553``) — the other two arms. Its
  ``len(unmapped_source) / total_source_fields > 0.2`` clause is at ``:1651``.
  It was ``:1552`` / ``:1650`` before 3.1 inserted one import line and the new
  function; both figures are recorded because the spec text cites the older
  pair.

The second half is the one that matters
---------------------------------------
"Exactly one of three" is close to free: the check either raises or returns, and
the existing decision is a boolean, so three labels follow from two exhaustive
choices. What this file actually buys is stated in the assertions rather than in
the label count:

* **the check is total on this domain** — for a schema declaring at least one
  countable field it returns a ``float`` equal to *M/D*, never ``None`` and
  never both;
* **the refusal arm is decided by the ratio and by nothing else** — refuse
  happens exactly when *r* ≤ ``MAPPABILITY_HARD_FAIL_THRESHOLD``, so no property
  of the mapping other than that ratio can move a run into or out of it (R14.1,
  and R14.4's "above the threshold nothing changes");
* **refuse implies a human was already required** — the executable form of
  do-NOT 13 and R14.7. W5 can convert a human decision into a refusal and
  **never** into a pass.

How the generator is constrained, and why
-----------------------------------------
The subset half does **not** hold for every conceivable ``mapping_result``, and
pinning that honestly is the whole reason this section exists. The production
docstring at ``:1815`` carries the argument in full; restated with its names:
let *D* be the declared source fields, *M* the declared fields the check counts
as mapped, *K* = ``len(mappings)`` and *U* = ``len(unmapped_source_fields)``.
Refusal needs *M/D* ≤ 0.5. Whenever the model's own lists do not
**under-report** — *U* ≥ *D* − *M* and *K* ≤ *M* — the approval clause computes
*U/(K+U)* ≥ 1 − *M/D* ≥ 0.5 > 0.2 and fires.

Those two inequalities are premises, not decoration. The implication genuinely
fails for a ``mappings`` list inflated to *K* ≥ 4·*U* — the clause not firing
needs exactly that — so a generator free to draw that shape would find a real
counterexample to a claim the design does not make, and "fixing" it by weakening
the assertion would delete R14.7 from the suite. So the generator draws only
shapes satisfying both premises, and does so **by construction** rather than by
``assume()``:

* every ``mappings`` entry names a distinct declared source field and a target
  field, so *K* = *M* exactly and *K* ≤ *M* holds. (*M* ≤ *K* is forced anyway —
  the check counts entries — so the premise ``K ≤ M`` is really the statement
  "no source field mapped twice, none undeclared".)
* ``unmapped_source_fields`` carries every declared-and-unmapped field, plus
  0–3 names the schema does not declare. So *U* = (*D* − *M*) + *e* ≥ *D* − *M*,
  and drawing *e* > 0 exercises the premise as the **inequality** it is rather
  than only at equality.

Both premises hold **exactly** on the fallback path, where
``generate_simple_mapping`` removes each matched source field from the pool and
assigns the remainder to ``unmapped_source_fields``. On the Bedrock path
``mappings`` is model output parsed as returned (``:806-826``), so the premises
are an assumption about the model's accounting — which is what the production
docstring says, and which this file therefore does not pretend to have removed.

What is deliberately not asserted here
--------------------------------------
The *upper* boundary of the middle arm — that approval is required for all
*r* < 0.8 — is not restated. It is owned twice already and neither owner is
this file: ``tests/unit/shared/test_config_mappability_nesting.py`` (task 1.4)
derives it **statically** from the ``> 0.2`` literal by tree shape, and task
1.1's ``test_unmapped_source_proportion_of_exactly_one_fifth_does_not_require_a_human``
pins the same boundary behaviourally. A third copy of ``0.8`` here would be a
restatement of arithmetic on a value someone else chose, and 1.4's locator
asserts **exactly one** ``len(unmapped_source) / total_source_fields``
comparison exists, so nothing in this file may reproduce that expression's
shape. The message contents (R14.2), the denominator decision, the
zero-source-field case (R14.5), the persisted row (R14.3) and the permanent
classification (R14.8) belong to tasks 3.1 and 3.3.

Ledger status
-------------
``why`` on the subject's line reports ``uncommitted_line`` — 3.1's function is
not committed yet, so no record governs it and the file's recent history is
shown instead. The governing records for the surrounding decision:
``LGR-9e76249-7`` (**template / ratified**; BROAD, 80 files — background) for
the clause ordering the approval clauses sit in, and ``LGR-60c4985-7`` for the
permanent-``StepFailure``-terminates-the-execution mechanism the refusal reuses.
``LGR-b46a5bb-4`` (**template / ratified**) is why 1.4 reads that clause as a
syntax tree rather than by substring, and is the reason this file does not add a
second reader for it. ``LGR-1e135d2-11`` (**template / ratified**) governs what
happens if this property ever fails: ``.hypothesis/examples`` is not committed,
so the counterexample must be pinned as an example test in the tree rather than
left in the cache. Patching is ``patch.object(_mod, …)`` throughout, per
``LGR-b397ee2-2`` and ``LGR-c58f276-4``.
"""

import os
import sys
from decimal import Decimal
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
# share, so it cross-contaminates between test files. The fixtures below are
# deliberately the same SHAPES as `test_mappability_hard_fail.py`'s and are
# re-declared rather than imported for exactly that reason — importing them
# would bind helpers that close over *that* module's `_mod`, a different object
# from this module's, and every patch here would then target the wrong one.
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

#: Read from the module under test rather than restated. `shared/config.py` is
#: its single source (P28 R18.2); `tests/unit/shared/test_config.py` owns its
#: value and `test_config_mappability_nesting.py` owns its relationship to the
#: `> 0.2` clause.
_THRESHOLD = _mod.MAPPABILITY_HARD_FAIL_THRESHOLD

# Non-vacuity guard: every arm below is arithmetic on this number, and a
# threshold of 0 or 1 would collapse the partition to two arms.
assert 0 < _THRESHOLD < 1, f"a refusal threshold of {_THRESHOLD} would make this whole file vacuous"

#: The largest declared-field count the generator draws. Twelve puts ratios on
#: both sides of the threshold and on the exact boundary (1/2, 2/4, 3/6, …)
#: while keeping every example a pure in-process call.
_MAX_DECLARED_FIELDS = 12

#: The three arms, named once so a label typo cannot pass as a fourth outcome.
_REFUSE = "refuse"
_APPROVAL_REQUIRED = "approval-required"
_DEFER = "defer-to-existing"
_OUTCOMES = (_REFUSE, _APPROVAL_REQUIRED, _DEFER)


# --- Fixtures (the shapes task 3.1 established) --------------------------------


def _schema(field_count: int, prefix: str = "src") -> dict:
    """A normalised source schema declaring *field_count* fields.

    `lambda_handler` runs `normalize_schema_format` before the check, so the
    shape reaching production always carries `properties`.
    """
    return {"type": "object", "properties": {f"{prefix}_{i}": {"type": "string"} for i in range(field_count)}}


def _mapping_result(declared: int, mapped: int, extra_unmapped: int) -> dict:
    """A mapping result satisfying both premises of the subset argument.

    *K* = *M* = ``mapped``: each entry names a distinct declared source field
    **and** a target field, which is what makes it count towards the numerator.
    *U* = (*D* − *M*) + ``extra_unmapped``: every declared-and-unmapped field is
    named, plus optional names the schema does not declare.

    `confidence` is 1.0 on every entry and `unmapped_target_fields` is empty, so
    the confidence clause and the target-side ratio clause cannot fire;
    `generated_by` is a model id and never `FALLBACK_GENERATED_BY`. For
    ``mapped >= 1`` the only clause left that can require a human is the
    unmapped-**source** one, so a `True` from `determine_approval_requirement`
    is attributable to it rather than to a coincidence.

    AMENDED for T-04: each entry's `target_field` is now its own `source_field`
    name (`src_i`), where it used to be `tgt_i`. T-04 added a clause that sends
    any entry whose source and target names do not agree (and that no operator
    mapping hint names) to a human, which `src_i` → `tgt_i` would have tripped
    on every entry — so the sentence above would have stopped being true. The
    SOURCE names are untouched, because those are what the mappability check
    counts against `_schema(declared)`; no target schema is in play here, so the
    target names carry no other meaning.
    """
    return {
        "mappings": [
            {"source_field": f"src_{i}", "target_field": f"src_{i}", "confidence": 1.0, "transformation": "direct"}
            for i in range(mapped)
        ],
        "unmapped_source_fields": [f"src_{i}" for i in range(mapped, declared)]
        + [f"ghost_{j}" for j in range(extra_unmapped)],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _pipeline_metadata() -> dict:
    """A pipeline whose configuration reaches the quality clauses.

    `schema_mapping_enabled` true, so `determine_approval_requirement` does not
    short-circuit to `False` before the ratio clauses.
    """
    return {
        "pipeline_id": "p55-task-3-2",
        "approval_config": {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal(str(_mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD)),
            "always_approve_schema_changes": True,
        },
    }


def _decide(mapping_result: dict) -> bool:
    """What the pre-existing criteria say about this mapping.

    The same two attribution guards task 1.1 uses, for the same reason: without
    them a `True` could be an early return on missing metadata or the trailing
    `except Exception`'s `return True`, either of which would satisfy the subset
    assertion below while proving nothing about the approval clauses.
    """
    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = _pipeline_metadata()
        decision = _mod.determine_approval_requirement("p55-task-3-2", mapping_result, None)
        assert mock_get_meta.called, "the approval configuration was never read — an early return, not a clause"
        mock_logger.error.assert_not_called()
    return decision


class _Observation(NamedTuple):
    """One run's outcome, plus the two facts the assertions read."""

    outcome: str
    ratio: float | None
    refusal: object | None
    #: `determine_approval_requirement`'s verdict on the same mapping. On the
    #: two non-refused arms this is the decision production actually reaches;
    #: on the refused arm it is a COUNTERFACTUAL, because production never calls
    #: that function for a refused run — `test_mappability_hard_fail.py`'s
    #: `test_a_refused_run_never_reaches_the_mapping_write_or_the_approval_decision`
    #: pins that. The counterfactual is the whole point of R14.7: "would also
    #: have fired" is a claim about a decision that is deliberately never made.
    counterfactual_approval: bool


def _observe(declared: int, mapped: int, extra_unmapped: int) -> _Observation:
    """Run the check, then ask the pre-existing criteria about the same mapping."""
    mapping_result = _mapping_result(declared, mapped, extra_unmapped)

    refusal: object | None = None
    ratio: float | None = None
    with patch.object(_mod, "logger"):
        try:
            ratio = _mod.enforce_source_field_mappability(
                mapping_result, _schema(declared), run_id="r-p55", pipeline_id="p-p55"
            )
        except _mod.StepFailure as raised:
            refusal = raised

    approval = _decide(mapping_result)

    if refusal is not None:
        outcome = _REFUSE
    elif approval:
        outcome = _APPROVAL_REQUIRED
    else:
        outcome = _DEFER
    return _Observation(outcome, ratio, refusal, approval)


@st.composite
def _honest_mapping(draw) -> tuple[int, int, int]:
    """(declared, mapped, extra_unmapped), constrained to the subset argument's premises.

    `declared >= 1` because Property 4 is stated for total > 0 — the zero-field
    case is R14.5 and belongs to task 3.3. `mapped` spans the whole closed range
    so both the empty mapping and the fully-mapped schema are drawn.
    `extra_unmapped` exercises *U* ≥ *D* − *M* as an inequality; it is capped at
    3 because its only effect is to push the unmapped proportion further above
    the approval clause's boundary, and one, two or three ghosts do that as well
    as thirty would.
    """
    declared = draw(st.integers(min_value=1, max_value=_MAX_DECLARED_FIELDS))
    mapped = draw(st.integers(min_value=0, max_value=declared))
    extra_unmapped = draw(st.integers(min_value=0, max_value=3))
    return declared, mapped, extra_unmapped


# --- Property 4 ---------------------------------------------------------------


@given(case=_honest_mapping())
@settings(max_examples=300, deadline=None)
def test_the_outcome_is_a_three_way_partition_and_refusal_is_a_strict_subset(case: tuple[int, int, int]):
    """Feature: p55-format-support-and-teardown, Property 4: The mappability outcome
    is a three-way partition, and refusal is a strict subset.

    *For any* (mapped, total) pair with total > 0, the outcome is exactly one of
    refuse / approval-required / defer-to-existing, and refuse implies
    approval-required would also have fired.

    **Validates: Requirements 14.1, 14.4, 14.7**
    """
    declared, mapped, extra_unmapped = case
    ratio = mapped / declared

    observed = _observe(declared, mapped, extra_unmapped)

    # (1) The check is TOTAL on this domain, and its two exits are exclusive.
    # `None` is reserved for an undefined ratio (R14.5), which cannot arise for
    # a schema declaring at least one field — so a `None` here would mean the
    # zero-field arm had swallowed a real run.
    assert (observed.refusal is None) != (observed.ratio is None), (
        f"the check neither raised nor returned a ratio for {mapped}/{declared}, or did both"
    )
    if observed.ratio is not None:
        assert observed.ratio == pytest.approx(ratio)

    # (2) Exactly one arm. Entailed by (1) plus the booleanness of the existing
    # decision, and asserted anyway so a fourth outcome cannot be introduced
    # without this failing.
    assert observed.outcome in _OUTCOMES
    assert [observed.outcome == name for name in _OUTCOMES].count(True) == 1

    # (3) The refusal arm is decided by the ratio and by NOTHING else — the
    # `<=` boundary of R14.1, and R14.4's "above the threshold nothing changes"
    # read as the contrapositive.
    assert (observed.outcome == _REFUSE) == (ratio <= _THRESHOLD), (
        f"{mapped}/{declared} = {ratio} against threshold {_THRESHOLD} produced {observed.outcome}"
    )

    # (4) THE HALF THAT MATTERS. do-NOT 13 and R14.7: every refused run already
    # required a human decision, so W5 only ever converts a human decision into
    # a refusal and never a pass into a refusal.
    if observed.outcome == _REFUSE:
        assert observed.counterfactual_approval is True, (
            f"{mapped}/{declared} was refused but the pre-existing criteria would have "
            f"auto-approved it. W5 must only ever TIGHTEN (R14.7, do-NOT 13)."
        )


def test_all_three_arms_of_the_partition_are_reachable():
    """Non-vacuity for Property 4: each arm has a witness.

    Without this, a generator that only ever produced one arm would satisfy
    every assertion above while checking a third of the claim — and the
    property's assertion (3) is conditional on the arm, so a partition
    collapsed to two arms would pass silently. One witness each, chosen so the
    arithmetic is visible in the test rather than in a comment.
    """
    # r = 0.25, at or below the threshold.
    refused = _observe(declared=4, mapped=1, extra_unmapped=0)
    assert refused.outcome == _REFUSE

    # r = 0.6: above the threshold, and 2 unmapped of 5 accounted-for fields is
    # 40% — above the approval clause's one fifth, so a human is still required.
    to_a_human = _observe(declared=5, mapped=3, extra_unmapped=0)
    assert to_a_human.outcome == _APPROVAL_REQUIRED
    assert to_a_human.ratio == pytest.approx(0.6)

    # r = 1.0: nothing unmapped, so no clause fires and the existing criteria
    # decide exactly as they did before W5 (R14.4).
    deferred = _observe(declared=5, mapped=5, extra_unmapped=0)
    assert deferred.outcome == _DEFER
    assert deferred.ratio == pytest.approx(1.0)


def test_the_premises_the_generator_respects_are_the_ones_the_design_states():
    """The generator's constraint, asserted rather than only documented.

    The subset half is claimed **under** two premises, and a later reader
    relaxing the generator would weaken Property 4 without touching a single
    assertion. So the shapes `_mapping_result` produces are checked against both
    premises directly: *K* ≤ *M* (no source field mapped twice, none
    undeclared) and *U* ≥ *D* − *M* (every unmapped declared field named).

    The counter-shape is named too, because "constrained" means nothing without
    it: a `mappings` list inflated to *K* ≥ 4·*U* breaks the implication, and
    the design does not claim otherwise.
    """
    for declared, mapped, extra in ((1, 0, 0), (4, 1, 0), (5, 3, 2), (12, 12, 3), (7, 4, 1)):
        result = _mapping_result(declared, mapped, extra)
        declared_names = set(_schema(declared)["properties"])

        counted_as_mapped = {
            entry["source_field"] for entry in result["mappings"] if entry["source_field"] in declared_names
        }
        k = len(result["mappings"])
        u = len(result["unmapped_source_fields"])
        m = len(counted_as_mapped)

        assert k <= m, f"premise K <= M violated for ({declared}, {mapped}, {extra}): K={k}, M={m}"
        assert u >= declared - m, f"premise U >= D - M violated for ({declared}, {mapped}, {extra}): U={u}, M={m}"

    # The shape deliberately NOT generated: four duplicate entries for one
    # declared field, one declared field unmapped. M = 1 of D = 2 so the check
    # refuses, while the approval clause sees U/(K+U) = 1/5 = 0.2, which is not
    # `> 0.2` and does not fire. A generator free to draw this would produce a
    # true counterexample to a claim the design does not make.
    #
    # AMENDED for T-04: the four entries are now literal duplicates,
    # `src_0 → src_0`, where they used to fan out to `tgt_0`…`tgt_3`. With the
    # old target names T-04's corroboration clause fired (`src_0` does not agree
    # with `tgt_i`) and the assertion below failed — but NOT because the premise
    # had become unnecessary, which is what its message would have claimed. Four
    # corroborated duplicates are still drawable, still refused, and still
    # auto-approved by every other criterion, so the counterexample stands and
    # the premise is still load-bearing. Only the target names moved; K, U, D
    # and M are unchanged.
    inflated = {
        "mappings": [{"source_field": "src_0", "target_field": "src_0", "confidence": 1.0} for _ in range(4)],
        "unmapped_source_fields": ["src_1"],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }
    assert len(inflated["mappings"]) >= 4 * len(inflated["unmapped_source_fields"])
    with patch.object(_mod, "logger"), pytest.raises(_mod.StepFailure):
        _mod.enforce_source_field_mappability(inflated, _schema(2))
    assert _decide(inflated) is False, (
        "the K >= 4*U shape is supposed to be the one where the pre-existing criteria do NOT "
        "require a human — if it now does, the premise this generator respects has become "
        "unnecessary and Property 4 can be widened deliberately."
    )
