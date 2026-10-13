# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T7 — every permanence claim the report makes is true.

**Validates: Requirements 1.9, 1.10, 2.7, 3.8, 3.9, Property 5**

Spec: `.kiro/specs/p41-converter-literal-fidelity/`, task 9.1. Defect **D4**:
`initcap` is asserted `permanently_inexpressible`, which
`test_code/lambda_function.py:507-509` renders to the customer as "no
single-frame pandas equivalent exists, so the proxy will never …" and whose
next-step text (`:1325`) reads "this construct is permanently outside the
proxy, so there is nothing to fix here". `initcap` is `.str.title()` —
single-frame and elementwise — so that is a false statement to the customer
about the system's own limits. *Measured*: 6 of 20 refusals, the single largest
refused construct (`bugfix.md` 1.9).

══════════════════════════════════════════════════════════════════════
TWO OPPOSITE ORACLE RULES IN THIS FILE, ONE PER CLASS. READ BEFORE EDITING.
══════════════════════════════════════════════════════════════════════
`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs both rules inside `tests/unit/lambdas/test_code/`, and this
header plus the two class docstrings are what keep them apart.

**Rule A — `TestPermanenceImpliesNoSingleFrameEquivalent`: the oracle is
`bugfix.md` 2.7's SEMANTICS, and current classifier output must NOT be
consulted.** The claim is that a construct expressible as a single-frame
elementwise pandas operation must not be classified
`permanently_inexpressible`. Today the classifier answers
`permanently_inexpressible` for `initcap`; taking that as the oracle is exactly
the mistake `LGR-28b4d75-5` names, because the answer under test IS the defect.

**Rule B — `TestTheConservativeDefaultIsUnchanged`: current output IS the
oracle, legitimately.** Everything in that class is a *preservation* claim —
`bugfix.md` 3.8 (the conservative default is not inverted) and 3.9 (the
genuinely permanent keep reading permanent). There is no better oracle for
"unchanged" than the current answers, and they were captured by calling the
real classifier against the working tree before this file was written.

EXPECTED RESULT ON THE FIRST RUN: **RED on the three `initcap` assertions in
Rule A, GREEN on the other 56 cases.** The red is the evidence D4 exists
(`tasks.md` 9.1, and `bugfix.md`'s note that no existing test fails on D4, so a
green `make test-fast` at HEAD is not evidence for this fix). **Do not adjust
the assertions to make them pass.** The expected-red window closes at task 9.2,
which removes `"initcap"` from `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS` — one
line in `code_interpreter_runner.py`, no change to this file.

WHAT THIS FILE DELIBERATELY DOES NOT DO. It does **not** implement `initcap`,
anywhere. D4's scope is making the reason *truthful*, not making it unnecessary
(`bugfix.md` "Out of scope", `design.md` §10). After 9.2 the reason reads
"not yet expressible", which is true: there is no rule yet, and one is possible.

WHY THE MEMBER LIST IS READ FROM THE MODULE AND NEVER TRANSCRIBED. Every
member-level assertion parametrises over
`sorted(_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS)` as imported. A name added to
the set with no entry in `_P41_MEMBERSHIP_JUSTIFICATION` below therefore
**fails the audit** rather than going unnoticed — which is the failure mode that
put `initcap` there in the first place: the set's comment (`:131`) lists it as
one of "p32 3.7's list (groupBy, agg, spark., UDFs, initcap)" and states no
membership criterion at all.

WHAT KIND OF EVIDENCE `_P41_MEMBERSHIP_JUSTIFICATION` IS — stated plainly
because it is not an execution result. It is a **derivation** over the member
list from Spark semantics and pandas' single-frame API surface, with the
reasoning recorded per member, following `design.md`'s "Bug Condition — D4"
section, which reasons through the same members. **No member's claim is
verified by running pandas**: pandas is not installed in
`agentic_data_ingestion/.venv` and must not become a dependency
(`bugfix.md` 3.14, `design.md` §10). So this file is evidence about the
*classification*, never about a pandas execution.
"""

import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
from code_interpreter_runner import (
    _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS,
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    classify_conversion_refusal,
)

#: The two verdicts `_P41_MEMBERSHIP_JUSTIFICATION` may carry.
#:
#: `_P41_PERMANENT` means: **no single-frame elementwise pandas equivalent
#: exists.** The criterion, stated once so every entry below is checkable
#: against it — an operation qualifies as *expressible* here iff it can be
#: written as a pandas expression over the single frame the proxy holds,
#: producing each row's value from that row alone. A construct fails the
#: criterion (and is therefore permanent) when it needs a second frame, a row
#: set other than the current row, a change of frame shape, or arbitrary user
#: code with no static translation.
#:
#: `_P41_EXPRESSIBLE` means the construct passes that criterion, so
#: `permanently_inexpressible` would be a **false** statement about it, whatever
#: the converter's current coverage. "Expressible" is emphatically **not**
#: "implemented" — `initcap` is expressible and stays unimplemented by this
#: spec.
_P41_PERMANENT = "no-single-frame-elementwise-equivalent"
_P41_EXPRESSIBLE = "single-frame-elementwise"

#: Membership justification, one entry per name, **derived** (see the module
#: docstring for what kind of evidence that is). Keyed by the name as
#: `ConversionError` reports it.
#:
#: `initcap` keeps an entry after it leaves the set, deliberately: the entry is
#: the record of *why* it left, and it is what makes a future re-addition fail
#: `test_no_member_of_the_permanent_set_is_expressible` instead of quietly
#: restoring the untruth.
_P41_MEMBERSHIP_JUSTIFICATION: dict[str, tuple[str, str]] = {
    # --- change of frame shape: many rows collapse into fewer -------------
    "groupBy": (
        _P41_PERMANENT,
        "collapses rows into groups; the result is a differently shaped frame, not a value per input row",
    ),
    "agg": (
        _P41_PERMANENT,
        "reduces a row set to one row per group, so no row's value is a function of that row alone",
    ),
    # --- needs a second frame the proxy does not have ---------------------
    "join": (_P41_PERMANENT, "combines two frames; the proxy holds exactly one"),
    "crossJoin": (_P41_PERMANENT, "combines two frames and multiplies rows; the proxy holds exactly one"),
    "union": (_P41_PERMANENT, "appends another frame's rows; the proxy holds exactly one"),
    "unionAll": (_P41_PERMANENT, "appends another frame's rows; the proxy holds exactly one"),
    "unionByName": (_P41_PERMANENT, "appends another frame's rows by name; the proxy holds exactly one"),
    # --- reads rows other than the current one ----------------------------
    "Window": (_P41_PERMANENT, "a window spec computes over a row set, not over the current row"),
    "over": (_P41_PERMANENT, "applies a function over a window's row set rather than one row"),
    "partitionBy": (_P41_PERMANENT, "defines the row set a window computes over"),
    "row_number": (_P41_PERMANENT, "an ordinal within a partition, so it depends on the other rows' order"),
    "rank": (_P41_PERMANENT, "a rank within a partition, so it depends on the other rows"),
    "dense_rank": (_P41_PERMANENT, "a rank within a partition, so it depends on the other rows"),
    "lag": (_P41_PERMANENT, "reads a preceding row's value"),
    "lead": (_P41_PERMANENT, "reads a following row's value"),
    # --- no static translation exists at all ------------------------------
    "rdd": (_P41_PERMANENT, "escapes the DataFrame API; there is no column expression to translate"),
    "udf": (_P41_PERMANENT, "arbitrary user code registered with Spark; nothing static to rewrite"),
    # --- entry points, not column expressions -----------------------------
    # `design.md`'s D4 section: permanent "in the sense that a Spark entry point
    # has no pandas equivalent". §11 records the separate untruth this leaves
    # when a SCAFFOLD writes `spark.conf.set(...)` — owned by `bugfix.md` 2.11
    # and out of scope here. Task 9.2 keeps both names in the set.
    "spark": (_P41_PERMANENT, "a Spark session, not a transform of a frame"),
    "sc": (_P41_PERMANENT, "a Spark context, not a transform of a frame"),
    # --- the one member that fails the criterion: D4's whole subject -------
    "initcap": (
        _P41_EXPRESSIBLE,
        "`.str.title()` — single-frame and elementwise, so it is unimplemented, not inexpressible (bugfix.md 1.9, 2.7)",
    ),
}

#: Names OUTSIDE the permanent set whose conservative default must not move
#: (`bugfix.md` 3.8). The first four are 1.10's correction to the framing this
#: spec was commissioned with — the framing grouped them with `initcap`, and
#: **only `initcap` was ever in the set**; all four already read
#: `not_yet_expressible` and must keep doing so.
_P41_FRAMING_NAMES_ALREADY_TRUTHFUL = ["length", "substring", "split", "coalesce"]

#: A wider sample of names outside the set, each classified by calling the real
#: classifier against the working tree before this file was written (Rule B:
#: current output IS the oracle). Included so the default is pinned across the
#: kinds of name that actually reach it — measured refusals (`set`, `cache`,
#: `isNotNull`, `rlike`), converter vocabulary (`col`, `trim`,
#: `regexp_replace`), the two module aliases task 8.2 stopped naming (`F`,
#: `functions`), a name no vocabulary knows (`posexplode`), and a name that
#: cannot exist.
#:
#: `expr` is here rather than among the permanent names on purpose, and it is a
#: recorded discrepancy rather than a decision of this task: `bugfix.md` 3.9's
#: prose reads "`expr`, aggregations, window functions … all stay permanent",
#: but `expr` is **not** a member of `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS` and
#: classifies `not_yet_expressible` today. 3.9's reproduction for `expr` was of
#: the converter's *refusal*, not of the runner's classification. Adding it to
#: the set would be a production change no requirement in this spec asks for, so
#: this pins what is, and task 9.1's notes record the discrepancy.
_P41_NAMES_OUTSIDE_THE_SET = [
    "expr",
    "rlike",
    "set",
    "cache",
    "isNotNull",
    "col",
    "trim",
    "regexp_replace",
    "F",
    "functions",
    "posexplode",
    "zzz_not_a_construct",
]


def p41_justified_verdict(name: str) -> str:
    """Return the derived membership verdict for *name*, or fail legibly.

    A member of the permanent set with no entry is an audit failure, not a
    lookup error: the whole point of reading the set from the module is that a
    name added without a stated reason cannot pass silently.
    """
    entry = _P41_MEMBERSHIP_JUSTIFICATION.get(name)
    assert entry is not None, (
        f"'{name}' is in _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS but has no entry in "
        "_P41_MEMBERSHIP_JUSTIFICATION. Membership means 'no single-frame elementwise pandas "
        "equivalent exists' (p41 2.7) — state the reason here, or the name does not belong in "
        "the set. This is the check that would have caught `initcap`."
    )
    return entry[0]


#: The permanent set as the module holds it, read once at import. Every
#: member-level assertion parametrises over this rather than over a transcribed
#: list, so a change to the set moves these tests.
_P41_SET_MEMBERS = sorted(_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS)

#: The names this file derives as genuinely permanent, from the justification
#: table rather than from the set. Used by Rule B to assert 3.9 — a removal of
#: one of these from the set would go red here even though the set is read from
#: the module.
_P41_DERIVED_PERMANENT_NAMES = sorted(
    name for name, (verdict, _) in _P41_MEMBERSHIP_JUSTIFICATION.items() if verdict == _P41_PERMANENT
)


class TestPermanenceImpliesNoSingleFrameEquivalent:
    """RULE A — the oracle is `bugfix.md` 2.7's semantics, never current output.

    Property 5: *for all C in refusableConstructs,
    `classify_conversion_refusal(C) = "permanently_inexpressible"` IMPLIES C has
    no single-frame pandas equivalent* (`bugfix.md`, `design.md` Correctness
    Properties). Asserted as the contrapositive over the set's own membership:
    an expressible construct must not be in the set, and every name in the set
    must carry a derived reason why it is not expressible.

    **The three assertions naming `initcap` are RED until task 9.2.** That is
    the expected-red window, and it is what makes the defect a recorded
    observation rather than an argument.
    """

    def test_initcap_classifies_as_not_yet_expressible(self):
        """D4's headline claim, stated exactly as `tasks.md` 9.1 states it.

        `initcap` is `.str.title()`: one frame, one row at a time. The reason
        the customer reads must therefore be "the proxy cannot yet express
        this" — a defect in this system, and the thing to fix — not "there is
        nothing to fix here".
        """
        assert classify_conversion_refusal("initcap") == NOT_VERIFIED_REASON_NOT_YET, (
            "`initcap` reads `permanently_inexpressible`, which the report renders as 'nothing to "
            "fix, ever' about something expressible as `.str.title()` (p41 1.9, 2.7). Task 9.2 "
            "removes it from _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS; it then falls to the "
            "conservative default, which is true."
        )

    def test_initcap_is_absent_from_the_permanent_set(self):
        """The mechanism behind the assertion above, pinned separately.

        Stated as its own case so a future reader can tell a classification
        change from a set change — the classifier keys on membership
        (`code_interpreter_runner.py:198-200`; the citation read `:171-173` when
        task 9.1 wrote this file, before task 9.2's +27 comment lines) and
        nothing else, so these two
        assertions fail together today and must be fixed by moving the
        membership, never by special-casing the name in the classifier.
        """
        assert "initcap" not in _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS, (
            "`initcap` is still a member. D4's fix is removing the member (task 9.2), not "
            "inverting the default (3.8) and not implementing `initcap` (out of scope)."
        )

    @pytest.mark.parametrize("construct", _P41_SET_MEMBERS)
    def test_no_member_of_the_permanent_set_is_expressible(self, construct):
        """The whole rule, over every member the module actually holds.

        Members are enumerated from `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS`
        rather than transcribed, so adding a name to the set adds a case here
        and it fails unless the addition carries a derived reason.

        RED for the `initcap` case only. The other cases are the 19 names
        `design.md`'s D4 section reasons through, and they stay green.
        """
        assert p41_justified_verdict(construct) == _P41_PERMANENT, (
            f"'{construct}' is asserted permanently inexpressible, but this file derives it as "
            f"{_P41_MEMBERSHIP_JUSTIFICATION[construct][1]}. A construct with a single-frame "
            "elementwise pandas equivalent must not carry `permanently_inexpressible` (p41 2.7): "
            "the report renders that reason as 'there is nothing to fix here'."
        )

    def test_every_member_carries_a_membership_justification(self):
        """The audit that the set's own comment never had (`:131`).

        Separate from the case above so a *missing* reason and a *wrong* reason
        read differently in the failure output.
        """
        unjustified = set(_P41_SET_MEMBERS) - set(_P41_MEMBERSHIP_JUSTIFICATION)

        assert not unjustified, (
            f"Members of _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS with no stated membership reason: "
            f"{sorted(unjustified)}. Membership means 'no single-frame elementwise pandas "
            "equivalent exists', not 'this converter has no rule' (p41 2.7, task 9.2's comment)."
        )


class TestTheConservativeDefaultIsUnchanged:
    """RULE B — current classifier output IS the oracle. Preservation only.

    `bugfix.md` 3.8: a name outside the permanent set continues to default to
    `not_yet_expressible`. The classifier's docstring states why — "Defaulting
    the other way would let a real converter gap read as 'nothing to fix'" — and
    2.7 splits the set **without** touching that default. `design.md` §10 makes
    inverting it an explicit do-NOT.

    `bugfix.md` 3.9: the genuinely permanent keep reading permanent, so removing
    one member does not quietly demote the rest.

    Every expectation here was captured by calling the real classifier against
    the working tree before this file was written, which is legitimate *because*
    the claim is "unchanged". **GREEN throughout, before and after task 9.2** —
    a red here means 9.2 changed more than the one line it was scoped to.
    """

    @pytest.mark.parametrize("construct", _P41_FRAMING_NAMES_ALREADY_TRUTHFUL)
    def test_the_four_framing_names_already_read_not_yet_and_keep_doing_so(self, construct):
        """`bugfix.md` 1.10's correction, pinned so the split cannot disturb it.

        The framing this spec was commissioned with grouped `length`,
        `substring`, `split` and `coalesce` with `initcap` as five names keyed
        off `_UNSUPPORTED_FUNCTIONS`. The classification does not key on that
        set, and of the five **only `initcap`** was ever in the permanent one.
        So the measured impact of the false permanence claim is 6 of 20
        refusals, all `initcap` — not 9 of 20 — and `coalesce`'s 3 refusals
        already read truthfully.
        """
        assert classify_conversion_refusal(construct) == NOT_VERIFIED_REASON_NOT_YET

    @pytest.mark.parametrize("construct", _P41_NAMES_OUTSIDE_THE_SET)
    def test_a_name_outside_the_set_falls_to_the_conservative_default(self, construct):
        """3.8, over the kinds of name that actually reach the default."""
        assert construct not in _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS
        assert classify_conversion_refusal(construct) == NOT_VERIFIED_REASON_NOT_YET

    @pytest.mark.parametrize("construct", _P41_DERIVED_PERMANENT_NAMES)
    def test_the_genuinely_permanent_keep_reading_permanent(self, construct):
        """3.9 — the reason still reaches the constructs it is true of.

        Parametrised over the justification table's permanent names rather than
        over the set, on purpose: the set is read from the module, so a *removal*
        of `lag` or `join` would silently shrink a set-driven sweep. Driving this
        from the derived list makes such a removal red.

        `spark` and `sc` are included: `design.md` D4 keeps them, permanent in
        the sense that a Spark entry point has no pandas equivalent.
        """
        assert classify_conversion_refusal(construct) == NOT_VERIFIED_REASON_PERMANENT

    def test_the_two_reasons_stay_distinct_strings(self):
        """The split is worthless if the two reasons collapse into one value.

        `LGR-b2f79ef-8`'s house rule, which D4 is an instance of violating:
        "could not check" and "checked and failed" must not collapse into one
        value. Here the two are "we have no rule yet" and "no rule can exist",
        and the whole of 2.7 is that they stay tellable apart.
        """
        assert NOT_VERIFIED_REASON_PERMANENT == "permanently_inexpressible"
        assert NOT_VERIFIED_REASON_NOT_YET == "not_yet_expressible"
        assert NOT_VERIFIED_REASON_PERMANENT != NOT_VERIFIED_REASON_NOT_YET
