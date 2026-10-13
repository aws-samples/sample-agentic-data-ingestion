# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: only the three healthy CloudFormation states read as healthy.

`classify_stack_status` in `scripts/deploy_prereq_lib.sh` reduces a stack status
to `ok`, `missing` or `warn`, and `deploy.sh` lets a deploy report success only on
`ok` — both other verdicts set `MISSING=1` and feed the same `exit 1`. So the
property that matters is a *closed* one: **anything outside the three healthy
states must not classify as ok.**

Stated as a property rather than only as a list of bad states because the risk is
a state nobody enumerated. CloudFormation's status vocabulary grows (IMPORT_* and
REVIEW_IN_PROGRESS were both later additions), and a check that read an unknown
status as healthy would report a half-deployed system as complete. The enumerated
cases in `test_deploy_stack_verification.py` pin the states that exist today;
this generalises over the ones that do not.

Input domain, deliberately narrow: these strings come from
`aws cloudformation describe-stacks --query 'Stacks[0].StackStatus' --output text`,
which prints an upper-snake token, `None`, or nothing — plus the `MISSING`
sentinel `deploy.sh` substitutes when the call fails. Generating arbitrary Unicode
would test the shell's argv handling rather than the classification, so the
alphabet is the one the producer actually emits.

Driven through the same real shell function as the enumerated cases, via the
`tests/unit/conftest.py` harness — imported directly rather than as a fixture,
because Hypothesis rejects function-scoped fixtures under `@given`. Deterministic
per example: the function is a `case` over its one argument.

**Validates:** any state outside the healthy set fails the completeness check.
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from tests.unit.conftest import call_prereq

FN = "classify_stack_status"

# The healthy set, from the requirement rather than from the library's `case`
# arms: a stack that finished creating, finished updating, or finished rolling an
# update back. Transcribing the implementation would make this test agree with
# whatever the implementation does, which is the failure mode `LGR-28b4d75-5`
# names.
HEALTHY_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"})

# The sentinel for a stack describe-stacks could not find.
ABSENT_SENTINEL = "MISSING"

VERDICTS = frozenset({"ok", "missing", "warn"})

# CloudFormation's actual status grammar: a resource action, optionally prefixed
# by an import/update qualifier, joined to an outcome. Composed rather than
# sampled from a fixed list so the property also covers combinations this
# vocabulary permits but no deploy has produced yet.
_ACTIONS = ["CREATE", "UPDATE", "DELETE", "ROLLBACK", "REVIEW", "IMPORT", "UPDATE_ROLLBACK", "IMPORT_ROLLBACK"]
_OUTCOMES = ["IN_PROGRESS", "COMPLETE", "FAILED", "COMPLETE_CLEANUP_IN_PROGRESS"]

cfn_statuses = st.builds(lambda a, o: f"{a}_{o}", st.sampled_from(_ACTIONS), st.sampled_from(_OUTCOMES))

# Anything else the producer could hand over: an upper-snake token that is not a
# real status, an empty string, or the literal `None` that `--output text` prints
# for an empty query result.
other_output = st.one_of(
    st.just(""),
    st.just("None"),
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=40),
)

statuses = st.one_of(cfn_statuses, other_output, st.just(ABSENT_SENTINEL))


def _classify(status: str) -> str:
    result = call_prereq(FN, status)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@settings(max_examples=60)
@given(status=statuses)
def test_only_a_healthy_state_classifies_as_ok(status):
    """`ok` if and only if the status is one of the three healthy states.

    Both directions in one assertion, because they constrain each other: the
    forward direction stops a working deploy being reported as incomplete, and
    the reverse — the one this file exists for — stops a `ROLLBACK_COMPLETE` or
    `UPDATE_IN_PROGRESS` stack passing as a working deployment.
    """
    verdict = _classify(status)

    assert (verdict == "ok") == (status in HEALTHY_STATUSES), (
        f"{status!r} classified as {verdict!r}; healthy states are {sorted(HEALTHY_STATUSES)}"
    )


@settings(max_examples=40)
@given(status=statuses)
def test_classification_always_yields_exactly_one_known_verdict(status):
    """The function is total: every input gets one of three tokens, on one line.

    `deploy.sh` dispatches on this value with a `case`, whose default arm is the
    failing `warn` branch — so an empty or multi-line result would still fail
    closed, but it would print a nonsense message. Totality is what keeps the
    three arms exhaustive rather than incidentally so.
    """
    result = call_prereq(FN, status)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"{status!r} produced {lines!r}"
    assert lines[0] in VERDICTS, f"{status!r} produced unknown verdict {lines[0]!r}"


@settings(max_examples=40)
@given(status=statuses)
def test_absence_is_distinguished_from_unhealthy(status):
    """Only the absent sentinel reads as `missing`; every other failure is `warn`.

    The two verdicts share an exit code but not a message, and the messages carry
    different fixes — an absent stack is fixed by re-running deploy.sh, an
    unhealthy one needs looking at. Collapsing them would tell an operator with a
    `ROLLBACK_COMPLETE` stack to re-run, which does not help.
    """
    verdict = _classify(status)

    assert (verdict == "missing") == (status == ABSENT_SENTINEL), f"{status!r} classified as {verdict!r}"


@settings(max_examples=40)
@given(status=statuses)
def test_no_state_outside_the_healthy_set_can_pass_the_check(status):
    """The completeness check's actual contract, stated once.

    `deploy.sh` reports success only when every stack classified `ok`. So for any
    status the producer can emit that is not a healthy state, the verdict must be
    one that sets `MISSING=1`.
    """
    assume(status not in HEALTHY_STATUSES)

    assert _classify(status) in {"missing", "warn"}
