# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 1.4 — `MAPPABILITY_HARD_FAIL_THRESHOLD` nests STRICTLY inside the
region that already requires a human decision (R14.7).

This is the assertion that being a module constant buys, and it is the reason
W5-D3 chose a constant over an accessor: the relationship is checkable without
running a pipeline, without an invocation, and without a deployed environment.
A value read per invocation could satisfy it in one account and violate it in
another, and nothing would say so.

**What is compared.** Two regions over *r* = mapped source fields / total
source fields:

* **refusal** — *r* ≤ ``MAPPABILITY_HARD_FAIL_THRESHOLD`` (p55 R14.1), owned by
  ``shared/config.py``.
* **approval required** — *r* < 0.8, which is not written anywhere as ``0.8``.
  It is *implied* by the clause
  ``len(unmapped_source) / total_source_fields > 0.2`` in
  ``determine_approval_requirement``
  (``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1650``,
  verified at that line in this tree). An unmapped proportion strictly above
  one fifth is a mappability ratio strictly below four fifths.

R14.7 requires the first to be a **strict subset** of the second, so W5 can
only ever convert a human decision into a refusal and never a human decision
into a pass (do-NOT 13). This file derives the 0.8 from that clause's own
literal rather than restating it, so moving the literal moves this test's
expectation with it — which is the point. P28 R18.2 keeps
``MAPPABILITY_HARD_FAIL_THRESHOLD``'s own value stated in exactly two places,
the constant and ``test_config.py``; 0.8 is stated in neither, because it is
not a value anyone chose — it is arithmetic on a value someone chose.

**Read by AST, not by substring.** ``LGR-b46a5bb-4`` (template / ratified) is
the record: a repository-level invariant expressed as substring matching over
source was rejected there because it also forbids *documenting* the fence — it
failed against a docstring that named the forbidden construct on purpose.
``determine_approval_requirement`` has exactly that hazard. Its comment block
at ``:1628-1640`` discusses ``> 0.2`` in prose, and there is a **second**
structurally identical clause on the target side at ``:1656``
(``len(unmapped_target) / total_target_fields > 0.2``) which this test must not
read. Matching the division expression as a syntax tree distinguishes all three
by construction; a regex for ``0.2`` cannot.

**The operator is asserted, not assumed.** The derived region is *r* < 0.8 only
because the clause is ``>`` and not ``>=``. Task 1.1's
``test_unmapped_source_proportion_of_exactly_one_fifth_does_not_require_a_human``
pins that boundary from the behavioural side; this file pins the same fact
statically, so the two cannot drift apart silently. A change to ``>=`` would
make the approval-required region *r* ≤ 0.8 and this test says so rather than
quietly deriving a wrong bound.

**No production code and no consumer.** Nothing reads
``MAPPABILITY_HARD_FAIL_THRESHOLD`` yet — p55 task 3.1 is the consumer. This
file asserts a relationship between two numbers that already exist.

**Validates: Requirements 14.6, 14.7**
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from config import MAPPABILITY_HARD_FAIL_THRESHOLD

#: Lower-cased alias used inside the assertions below. The relationship R14.7
#: states reads naturally as `threshold < boundary`, and ruff's SIM300 treats an
#: upper-case name on the left of a comparison as a Yoda condition — so the
#: alias keeps the assertion in the requirement's own direction without adding
#: a lint finding to a tree that already carries a style backlog
#: (`LGR-65ce737-1`).
_refusal_threshold = MAPPABILITY_HARD_FAIL_THRESHOLD

_SCHEMA_MAPPING_SOURCE = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_schema_mapping",
        "lambda_function.py",
    )
)

#: The function whose clause implies the approval-required region.
_FUNCTION = "determine_approval_requirement"


def _unmapped_source_ratio_clause() -> ast.Compare:
    """The single `len(unmapped_source) / total_source_fields <op> <literal>` comparison.

    Located by tree shape inside ``determine_approval_requirement`` only, so
    neither the prose at ``:1628-1640`` nor the target-side sibling at
    ``:1656`` can be mistaken for it. Raises rather than skipping if the shape
    is not found exactly once — a clause this test cannot read is a clause the
    nesting relationship is no longer being checked against, which is the
    posture F5 takes for the same reason.
    """
    with open(_SCHEMA_MAPPING_SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_SCHEMA_MAPPING_SOURCE)

    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _FUNCTION
    ]
    assert len(functions) == 1, (
        f"expected exactly one `def {_FUNCTION}` in {_SCHEMA_MAPPING_SOURCE}, found {len(functions)}"
    )

    matches = [node for node in ast.walk(functions[0]) if _is_unmapped_source_ratio_comparison(node)]
    assert len(matches) == 1, (
        f"expected exactly one `len(unmapped_source) / total_source_fields` comparison inside "
        f"`{_FUNCTION}`, found {len(matches)} at lines {[m.lineno for m in matches]}. The "
        f"approval-required region R14.7 nests inside is derived from that clause, so this test "
        f"cannot answer without it."
    )
    return matches[0]


def _is_unmapped_source_ratio_comparison(node: ast.AST) -> bool:
    """True for `len(unmapped_source) / total_source_fields <op> <number>`.

    Deliberately structural. ``unmapped_source`` and ``total_source_fields``
    are matched as names, which is what excludes the target-side clause whose
    names are ``unmapped_target`` and ``total_target_fields``.
    """
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return False

    left = node.left
    if not isinstance(left, ast.BinOp) or not isinstance(left.op, ast.Div):
        return False

    numerator, denominator = left.left, left.right
    if not (
        isinstance(numerator, ast.Call)
        and isinstance(numerator.func, ast.Name)
        and numerator.func.id == "len"
        and len(numerator.args) == 1
        and isinstance(numerator.args[0], ast.Name)
        and numerator.args[0].id == "unmapped_source"
    ):
        return False

    return isinstance(denominator, ast.Name) and denominator.id == "total_source_fields"


def _approval_required_below() -> float:
    """The mappability ratio strictly below which a human decision is required.

    Derived, never restated: an unmapped proportion above ``p`` is a
    mappability ratio below ``1 - p``.
    """
    clause = _unmapped_source_ratio_clause()

    assert isinstance(clause.ops[0], ast.Gt), (
        f"the unmapped-source clause at line {clause.lineno} is no longer a strict `>`. "
        f"The approval-required region derived here (r < 1 - literal) depends on that: with "
        f"`>=` the region becomes r <= 1 - literal, and task 1.1's exactly-one-fifth boundary "
        f"test changes meaning too. Re-derive both together, deliberately."
    )

    literal = clause.comparators[0]
    assert isinstance(literal, ast.Constant) and isinstance(literal.value, int | float), (
        f"the unmapped-source clause at line {clause.lineno} no longer compares against a "
        f"numeric literal, so the approval-required region cannot be read statically."
    )

    unmapped_proportion = float(literal.value)
    assert 0.0 < unmapped_proportion < 1.0, (
        f"an unmapped proportion of {unmapped_proportion} at line {clause.lineno} is outside "
        f"(0, 1) and would make every assertion in this file vacuous."
    )
    return 1.0 - unmapped_proportion


# ---------------------------------------------------------------------------
# R14.7 — the refusal region is a STRICT SUBSET of the approval-required region
# ---------------------------------------------------------------------------


def test_hard_fail_threshold_is_strictly_below_the_approval_required_boundary():
    """R14.7, read against the `:1650` literal rather than a second copy of 0.8.

    Equality would be a violation, not a near miss: at *r* exactly equal to the
    boundary the approval clause does NOT fire (it is a strict `>` on the
    unmapped proportion), so a threshold sitting there would refuse a run that
    nothing currently sends to a human — W5 turning a pass into a refusal,
    which is a widening and not the tightening do-NOT 13 permits.
    """
    boundary = _approval_required_below()

    assert _refusal_threshold < boundary, (
        f"MAPPABILITY_HARD_FAIL_THRESHOLD ({_refusal_threshold}) must be strictly below the mappability "
        f"ratio at which a human decision is already required ({boundary}, implied by the "
        f"unmapped-source clause in {_FUNCTION}). R14.7."
    )


def test_every_refused_ratio_already_required_a_human():
    """The subset half of R14.7: W5 never converts a pass into a refusal.

    The refusal region is *r* ≤ threshold, so its supremum is the threshold
    itself. If the supremum is inside *r* < boundary then so is every point
    below it, which is the whole region — stated here as the two witnesses that
    would fail first if the relationship inverted.
    """
    boundary = _approval_required_below()

    # The worst case in the refusal region: the threshold itself.
    assert _refusal_threshold < boundary
    # And a ratio comfortably inside it.
    assert _refusal_threshold / 2 < boundary


def test_the_subset_is_proper_so_some_runs_still_reach_a_human():
    """The *strict* half of R14.7: the refusal region is not the whole region.

    Without this, a threshold equal to the boundary would satisfy "subset" on a
    non-strict reading and swallow every run that currently requires a human
    decision — R12's characterization tests would still pass, because they
    assert what `determine_approval_requirement` returns, and W5 raises before
    it is called. The witness is the midpoint of the gap: a ratio that requires
    a human today and must keep requiring one after W5.
    """
    boundary = _approval_required_below()
    witness = (_refusal_threshold + boundary) / 2

    assert _refusal_threshold < witness < boundary, (
        f"no ratio sits strictly between the refusal threshold ({_refusal_threshold}) and the approval "
        f"boundary ({boundary}), so W5 would refuse every run that currently reaches a human. "
        f"R14.7 requires a proper subset."
    )


def test_the_unmapped_source_clause_is_read_from_the_function_it_lives_in():
    """The located clause is the source-side one, and it is the only one located.

    The target-side clause at `:1656` is structurally identical apart from its
    two names. If this test ever started reading that one, every assertion
    above would still pass while measuring the wrong requirement — R14.7 is a
    claim about the SOURCE-side region — so the identification itself is
    asserted rather than left to the locator's docstring.
    """
    clause = _unmapped_source_ratio_clause()

    assert isinstance(clause.left, ast.BinOp)
    assert isinstance(clause.left.right, ast.Name)
    assert clause.left.right.id == "total_source_fields"

    numerator = clause.left.left
    assert isinstance(numerator, ast.Call)
    assert numerator.args[0].id == "unmapped_source"

    # The literal's VALUE is deliberately not pinned here. It is the approval
    # boundary — a product decision — and R14.7 constrains only the nesting, so
    # a boundary that moved while staying above the refusal threshold is not a
    # violation of this requirement. Task 1.1's
    # `test_unmapped_source_proportion_of_exactly_one_fifth_does_not_require_a_human`
    # is what pins the current value, from the behavioural side.
    assert isinstance(clause.comparators[0], ast.Constant)
