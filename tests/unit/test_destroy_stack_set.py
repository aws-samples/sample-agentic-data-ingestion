# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Teardown_Tool's stack set, compared against the CDK entrypoint (p55 task 2.3, W1).

`stack_deletion_order` in `scripts/destroy_lib.sh` is a function rather than an
inline list in `destroy.sh` for the reason design decision W1-D2 gives: the order
is derivable from `deploy-cdk/bin/agentic_data_ingestion.ts`, and a transcribed
list goes stale *silently* the first time a stack is added — the drift
`LGR-28b4d75-5` records, "a test written from the producer's output cannot detect
the producer drifting". Being a function is what makes this file possible: it can
compare the library's stack set against the producer.

**This is a name-set comparison, not a behaviour claim over source text**, which
is why it does not run into `LGR-b46a5bb-4`. That record rejects asserting
*behaviour* by substring-matching output or source — a script that merely mentions
a stack name proves nothing. Here the source is read to enumerate the *set of
stacks the app declares*, and the assertion is that two sets are equal. A stack
name present in one and absent from the other is a real defect either way round:
a stack the app creates and the teardown cannot name is undeletable, and a stack
the teardown names and the app never creates is a delete request at a
non-existent stack.

`TestFixturesStack` is constructed inside the `adi:deployTestFixtures` context
gate, so a plain `cdk deploy --all` does not create it. It is still in the set,
and `test_test_fixtures_is_the_context_gated_construction` below pins that the
gate is what makes it optional — being *optionally deployed* is a fact about a
past `cdk deploy`, not about the vocabulary this teardown needs. Omitting it
would make an opt-in stack undeletable by the only tool built to delete it.

**Validates: Requirements 1.6**
"""

import re

from tests.unit.conftest import REPO_ROOT, call_destroy_lib

CDK_ENTRYPOINT = REPO_ROOT / "deploy-cdk" / "bin" / "agentic_data_ingestion.ts"

# `new FoundationStack(`, `new EdgeWafStack(` — the logical name is the capture.
# `[A-Za-z0-9]*` deliberately admits no `.`, so a hypothetical `new cdk.Stack(`
# is not mistaken for a declared application stack.
_STACK_CONSTRUCTION = re.compile(r"\bnew ([A-Za-z][A-Za-z0-9]*)Stack\(")

# The context key that gates TestFixtures. Read as a literal so that renaming the
# key without revisiting the teardown is a failure here rather than a surprise
# during a teardown.
_TEST_FIXTURES_GATE = "adi:deployTestFixtures"


def _without_comments(source: str) -> str:
    """Drop `//` line comments and `/* … */` blocks.

    A commented-out construction is not a declared stack. Without this, deleting
    a stack by commenting it out would fail this test with the *opposite* advice
    — "add it to the teardown order" — for a stack that no longer exists.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _declared_stack_names() -> list[str]:
    """The logical names of every stack `bin/agentic_data_ingestion.ts` constructs."""
    source = _without_comments(CDK_ENTRYPOINT.read_text())
    return _STACK_CONSTRUCTION.findall(source)


def _deletion_order() -> list[str]:
    result = call_destroy_lib("stack_deletion_order")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_the_entrypoint_is_where_it_is_expected_to_be():
    """**Validates: Requirements 1.6**

    A missing file would make `_declared_stack_names` raise rather than return an
    empty set, but stating it separately means a moved entrypoint reports itself
    as a moved entrypoint instead of as a stack-set mismatch.
    """
    assert CDK_ENTRYPOINT.is_file(), f"CDK entrypoint not found at {CDK_ENTRYPOINT}"


def test_deletion_order_covers_exactly_the_stacks_the_app_declares():
    """**Validates: Requirements 1.6**

    The assertion this file exists for. When a tenth stack lands in
    `bin/agentic_data_ingestion.ts`, this fails and names it — rather than the
    teardown silently leaving it standing, which is the drift W1-D2 rejected an
    inline list to avoid.
    """
    declared = set(_declared_stack_names())
    ordered = set(_deletion_order())

    assert declared, "no `new *Stack(` constructions found — the regex or the entrypoint moved"
    assert ordered == declared, (
        f"stack set drift: declared but never deleted {sorted(declared - ordered)}; "
        f"deleted but never declared {sorted(ordered - declared)}"
    )


def test_the_app_declares_nine_stacks_and_the_order_names_each_once():
    """**Validates: Requirements 1.6**

    Nine, not eight. design's `## Components and Interfaces` table says "the
    eight stack logical names" while design §4.1's own enumeration lists nine;
    the nine-entry reading is the one taken, because it is what §4.1 enumerates
    and the only reading under which the set comparison above is satisfiable —
    `TestFixturesStack` is constructed, inside its context gate.

    The length check is what rules out a duplicate entry masking a missing one: a
    set comparison alone passes if a name appears twice and another is absent
    twice over.
    """
    declared = _declared_stack_names()
    ordered = _deletion_order()

    assert len(declared) == 9, f"expected 9 declared stacks, found {len(declared)}: {declared}"
    assert len(ordered) == 9, f"expected 9 ordered stacks, found {len(ordered)}: {ordered}"
    assert len(set(ordered)) == len(ordered), f"duplicate entry in deletion order: {ordered}"
    assert len(set(declared)) == len(declared), f"a stack is constructed twice: {declared}"


def test_test_fixtures_is_the_context_gated_construction():
    """**Validates: Requirements 1.6**

    Pins *which* stack is optional and *what* makes it optional, so the set above
    is not merely nine names that happen to agree. `TestFixturesStack` is the
    only construction inside the `adi:deployTestFixtures` gate, and the gate is
    read from the same source rather than assumed.

    If the gate were ever extended to a second stack, the deletion order would
    still be correct — it names every declared stack regardless of gating — but
    the reason recorded in `destroy_lib.sh` for emitting a possibly-absent name
    would need to name both. This test is what surfaces that.
    """
    source = _without_comments(CDK_ENTRYPOINT.read_text())

    assert _TEST_FIXTURES_GATE in source, f"the {_TEST_FIXTURES_GATE} context gate is gone"
    # The gate and the construction are adjacent: the `if (…) { new TestFixturesStack(…) }`
    # block is the whole of it, so the construction must follow the last mention
    # of the gate key and no other construction may.
    tail = source[source.rindex(_TEST_FIXTURES_GATE) :]
    gated = _STACK_CONSTRUCTION.findall(tail)

    assert gated == ["TestFixtures"], f"expected TestFixtures alone behind the gate, found {gated}"


def test_the_gated_stack_is_still_in_the_deletion_order():
    """**Validates: Requirements 1.6**

    Stated as its own case because the tempting simplification — "it is
    context-gated, so leave it out" — is wrong for a reason no other test here
    carries. Whether the stack exists was decided by a context flag supplied to a
    past `cdk deploy`; this library cannot know it, and `destroy.sh` finds out by
    enumerating. Absence is a recorded non-event; a name the tool does not have
    is an undeletable stack.
    """
    assert "TestFixtures" in _deletion_order()
