# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the SQS redelivery budget against the approval hold (p54 R10.2).

**These are the first assertions of any kind on this queue.** Before p54, a grep
across BOTH CDK suites (``deploy-cdk/test/`` and ``tests/cdk/``) found zero hits
for ``visibilityTimeout``, ``maxReceiveCount``, ``RedrivePolicy``,
``PipelineTriggerQueue`` or ``PipelineTriggerDLQ``. That is how a 25-minute
redelivery budget came to sit behind a 72-hour human approval gate with three
in-code comments reassuring the reader — one of them naming the wrong constant,
one naming the wrong count, and one describing the lock's own TTL as though it
covered the file queued behind the lock.

## The inequality, and why it is a contract test rather than a property test

Three numbers have to agree and they live in two languages that cannot import
each other:

- ``deploy-cdk/lib/orchestration-stack.ts`` — ``visibilityTimeout`` and
  ``maxReceiveCount`` on ``PipelineTriggerQueue``. Their **product** is the
  Redelivery_Budget: each "receive" is one retry after the visibility timeout,
  so the count is the budget's length.
- the same file's exported ``APPROVAL_TIMEOUT_SECONDS`` — the ``TimeoutSeconds``
  on all three ``waitForTaskToken`` gates, and therefore the longest a run can
  hold the per-pipeline lock. ``LGR-69ca78b-2`` owns that number and
  ``LGR-5d68eeb-4`` records that nothing releases a parked approval before it
  elapses, so the gate really is what terminates the wait.

p54 R10.2 states the budget MUST NOT be shorter than the hold. There is exactly
one deployed value for each constant, so this is configuration arithmetic and
not input-driven behaviour — generating a hundred of them would mean generating
configurations that do not exist. It is therefore a contract assertion, on the
model of ``test_approval_window_cross_language.py``, whose *"same
regex-the-CDK-literal technique"* this file reuses for the same reason.

## R10.2 is UNSATISFIED, and the xfail below is how that is recorded

Stated as arithmetic rather than as an argument:

===========================================  ==============
covering 72 h at a 30 s visibility timeout   8640 receives
SQS's documented ceiling on max receives     1000 receives
so the reachable budget is                   30000 s ≈ 8 h 20 min
of a hold that can run to                    259200 s = 72 h
===========================================  ==============

**Full coverage is unreachable by geometry alone**, and that is not a defect in
the values chosen — it is the reason p54's ratified arm is (d) + (a) rather than
(a) alone. (a) raised ``maxReceiveCount`` from 50 to 1000, taking coverage from
0.6% to 11.6% — a 20x reduction in exposure and not a fix. (d) makes the residual
**visible** instead of silent: ``QueueLossRecorderFunction`` writes a durable
Lost_File record and ``ExecutionsPage`` renders it as "Failed (DLQ)". The arm
that satisfies R10.2 *structurally* is (c), a durable pending-files table, and
``LGR-21a1b5b-2`` already decides it goes in its own table.

Raising ``visibilityTimeout`` is deliberately NOT the lever, and the test below
pins that too: at 900 s every deferral idles up to 15 minutes even when the lock
clears in seconds, regressing the 6–7 minute serialisation the ``mynewtesting``
pipeline is observed doing. It is also read by the trigger Lambda's timeout ratio
(``queueVisibilityTimeout >= lambdaTimeout * 6``).

The inequality is carried as an ``xfail(strict=True)``: it fails today by
construction, and strictness means that **closing the gap breaks this test**,
forcing whoever closes it to delete the marker and the arithmetic above rather
than leaving a stale "known gap" behind. Every other assertion here is green and
must stay green.

Validates: p54 Requirements 10.2 (recorded as unsatisfied, with the shortfall
stated numerically), 10.4 (the lock's concurrency guarantee is untouched — no
constant here moves it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATION_STACK = _PROJECT_ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts"

#: SQS's documented ceiling on "Maximum receives" — the valid range is 1 to 1000.
#: Restated here because it is the fact that makes the shortfall structural
#: rather than a tuning choice, so a reader of the failure needs it in front of
#: them. It is not a value this repo controls.
SQS_MAX_RECEIVE_COUNT_CEILING = 1000

#: The construct whose two literals are the budget. Anchoring the parse inside
#: this block matters: ``visibilityTimeout`` and ``maxReceiveCount`` both appear
#: in the surrounding comment prose (in backticks), so a file-wide regex would
#: happily match a sentence describing the defect instead of the value causing
#: it.
_QUEUE_CONSTRUCT = "new sqs.Queue(this, 'PipelineTriggerQueue', {"


def _source() -> str:
    return _ORCHESTRATION_STACK.read_text(encoding="utf-8")


def _trigger_queue_block() -> str:
    """Just the ``PipelineTriggerQueue`` construct's own properties."""
    source = _source()
    start = source.find(_QUEUE_CONSTRUCT)
    assert start != -1, (
        f"{_QUEUE_CONSTRUCT!r} not found in deploy-cdk/lib/orchestration-stack.ts — "
        "the pipeline trigger queue was renamed or moved, and the redelivery "
        "budget can no longer be checked against the approval gate."
    )
    end = source.find("\n    });", start)
    assert end != -1, "could not find the end of the PipelineTriggerQueue construct"
    return source[start:end]


def _one_int(pattern: str, haystack: str, what: str) -> int:
    matches = re.findall(pattern, haystack)
    assert len(matches) == 1, (
        f"expected exactly one {what} in the PipelineTriggerQueue construct, found "
        f"{matches!r} — the parse is ambiguous and the assertions below would be "
        f"checking an arbitrary one of them."
    )
    return int(matches[0])


def visibility_timeout_seconds() -> int:
    """``visibilityTimeout: cdk.Duration.seconds(N)`` on the trigger queue."""
    return _one_int(
        r"visibilityTimeout:\s*cdk\.Duration\.seconds\((\d+)\)",
        _trigger_queue_block(),
        "visibilityTimeout",
    )


def max_receive_count() -> int:
    """``maxReceiveCount: N`` in the trigger queue's ``deadLetterQueue``."""
    return _one_int(r"maxReceiveCount:\s*(\d+)", _trigger_queue_block(), "maxReceiveCount")


def approval_timeout_seconds() -> int:
    """The gate's ``TimeoutSeconds`` — the longest the pipeline lock can be held.

    Matches the exported constant's declaration, not a use site, so a stray
    reference elsewhere in the file cannot satisfy it. Same regex as
    ``test_approval_window_cross_language.py``, deliberately: if the two ever
    disagree about how to read this number, one of them is wrong.
    """
    match = re.search(
        r"export\s+const\s+APPROVAL_TIMEOUT_SECONDS\s*(?::\s*number\s*)?=\s*(\d+)\s*;",
        _source(),
    )
    assert match is not None, (
        "APPROVAL_TIMEOUT_SECONDS declaration not found in "
        "deploy-cdk/lib/orchestration-stack.ts — the approval gate window moved "
        "or was renamed, so the lock-hold bound is unknown and R10.2 cannot be "
        "evaluated."
    )
    return int(match.group(1))


def redelivery_budget_seconds() -> int:
    """The budget: the product, not either factor.

    Spelled as a function because attributing it to ``visibilityTimeout`` alone
    is the exact mistake the queue's comment made for as long as it existed.
    """
    return max_receive_count() * visibility_timeout_seconds()


class TestTheBudgetIsTheProductOfTwoParsedLiterals:
    """The parse itself holds, before anything is concluded from it."""

    def test_the_cdk_source_is_readable(self):
        """A moved or renamed CDK file must fail loudly, not skip silently."""
        assert _ORCHESTRATION_STACK.is_file(), f"CDK source not found at {_ORCHESTRATION_STACK}"

    def test_both_factors_parse_to_positive_integers(self):
        """Guards against the whole file passing vacuously on a failed parse."""
        assert visibility_timeout_seconds() > 0
        assert max_receive_count() > 0
        assert approval_timeout_seconds() > 0

    def test_the_budget_is_eight_hours_twenty_minutes(self):
        """1000 x 30 s = 30000 s, and the number is asserted rather than described.

        This is the assertion that would have caught the original defect: the
        queue's comment claimed a ~15-minute visibility timeout while the value
        was 30 s, and nothing anywhere compared the claim to the literal.
        """
        assert redelivery_budget_seconds() == 30_000, (
            f"the redelivery budget is now {redelivery_budget_seconds()} s "
            f"({max_receive_count()} receives x {visibility_timeout_seconds()} s). If that "
            f"is intended, update this assertion, the arithmetic in this module's "
            f"docstring, and the comment on PipelineTriggerQueue in "
            f"deploy-cdk/lib/orchestration-stack.ts — all three state the same number."
        )

    def test_max_receive_count_is_at_the_sqs_ceiling(self):
        """1000 is the top of SQS's documented 1-1000 range for Maximum receives.

        At the ceiling, so a reader looking for more budget from this factor
        learns here that there is none, and a value ABOVE it would be rejected at
        deploy time rather than here.
        """
        assert max_receive_count() == SQS_MAX_RECEIVE_COUNT_CEILING, (
            f"maxReceiveCount is {max_receive_count()}; SQS's valid range is 1-"
            f"{SQS_MAX_RECEIVE_COUNT_CEILING}. Above the ceiling this fails at deploy; "
            f"below it, budget is being left on the table for no stated reason."
        )

    def test_visibility_timeout_stays_short_enough_not_to_idle_a_deferral(self):
        """30 s, and NOT the lever for more budget.

        The two factors multiply, so raising this also raises the budget — which
        is why the constraint has to be written down rather than left to
        judgement. A deferred file waits out the full visibility timeout on every
        retry even when the lock clears immediately, so a large value converts
        the observed 6-7 minute serialisation into a fixed-granularity queue. The
        60 s bound below is generous relative to the deployed 30 s; it exists to
        catch a jump to 900 s ("the value the old comment thought was set"), not
        to pin the exact number.

        It is also read by the trigger Lambda's own timeout ratio
        (``queueVisibilityTimeout >= lambdaTimeout * 6``) in the same file, so
        moving it moves two things.
        """
        assert visibility_timeout_seconds() <= 60, (
            f"visibilityTimeout is {visibility_timeout_seconds()} s. Every deferral now idles "
            f"up to that long even when the pipeline lock clears in seconds. If this "
            f"is a deliberate trade for redelivery budget, say so here and re-measure "
            f"the serialisation latency — do not raise it silently."
        )


class TestR102IsUnsatisfiedAndTheShortfallIsStated:
    """R10.2 as a requirement, and the size of the gap as a number."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "p54 R10.2 is UNSATISFIED and recorded as such. The budget reaches "
            "30000 s (1000 receives x 30 s) against a 259200 s approval hold — "
            "11.6% covered, a 63 h 40 min shortfall. Covering 72 h at 30 s would "
            "need 8640 receives and SQS caps maxReceiveCount at 1000, so full "
            "coverage is UNREACHABLE by geometry: closing this needs arm (c), a "
            "durable pending-files table (LGR-21a1b5b-2 puts it in its own "
            "table). The ratified arm is (d)+(a): (a) cut exposure 20x, (d) makes "
            "the residual visible via QueueLossRecorderFunction instead of "
            "silent. strict=True on purpose — when the gap IS closed this test "
            "XPASSes and fails, which is the prompt to delete the marker and the "
            "arithmetic that goes with it."
        ),
    )
    def test_the_redelivery_budget_is_not_shorter_than_the_approval_hold(self):
        """R10.2: Redelivery_Budget >= Lock_Hold_Window.

        The one assertion in this file that is expected to fail. It is here so
        the requirement is *asserted* rather than merely commented — a comment
        cannot notice when someone shortens the budget again.
        """
        assert redelivery_budget_seconds() >= approval_timeout_seconds()

    def test_the_shortfall_has_not_grown(self):
        """The gap may shrink freely; growing it is what must fail.

        A "no worse than" bound rather than an equality, so raising the budget or
        (per do-NOT 21, which forbids it) shortening the gate does not break this
        — but halving the budget does. This is the guard that keeps the xfail
        above from being the only thing watching these numbers, since an xfail
        stays green whether the shortfall is one second or seventy hours.
        """
        shortfall = approval_timeout_seconds() - redelivery_budget_seconds()
        assert shortfall <= 229_200, (
            f"the R10.2 shortfall is now {shortfall} s, worse than the 229200 s "
            f"(63 h 40 min) recorded when p54 task 7.7 measured it. Something reduced "
            f"the redelivery budget or lengthened the approval hold."
        )

    def test_the_approval_hold_is_still_seventy_two_hours(self):
        """The other side of the inequality has NOT been touched.

        p54 do-NOT 21 forbids moving the approval window to close R10.2 from the
        cheap end: ``LGR-69ca78b-2`` owns the number, ``LGR-5d68eeb-4``
        re-derived it, and ``test_approval_window_cross_language.py`` asserts
        ``shared/config.APPROVAL_WINDOW_HOURS`` agrees with it — so moving it
        means superseding a record and moving two numbers in lockstep, for a
        reason that has nothing to do with how long an approver should get.
        Asserted here so a future attempt to "fix" the inequality by shrinking
        the gate fails in the file that owns the inequality.
        """
        assert approval_timeout_seconds() == 259_200
