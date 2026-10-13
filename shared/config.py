# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single source for the Python side of behavioral thresholds, windows, and TTLs
(P28 R18.2).

Every behavioral threshold that gates pipeline behavior — the advertised
approval window, the confidence auto-approve gate, and lock/record TTLs — is
defined here exactly once and imported by consumers. No divergent literals.

What this file does NOT own (p34 R6, amending P28 R18.1's "single source"
claim in the two places it overreached):

1. **The approval gate window is owned by the CDK, not by this file.** The
   three ``waitForTaskToken`` gates carry ``APPROVAL_TIMEOUT_SECONDS`` in
   ``deploy-cdk/lib/orchestration-stack.ts``, and *that* timeout is what
   abandons the execution. ``APPROVAL_WINDOW_HOURS`` below mirrors it so the
   deadline we advertise to an approver is the deadline they actually get;
   the mirror is asserted by
   ``tests/contracts/test_approval_window_cross_language.py`` because
   TypeScript and Python cannot import each other.
2. **Docs are not generated from here.** The prose in ``docs/`` states these
   numbers by hand and has to be updated by hand; this module governs code,
   not documentation. (The original docstring claimed to be the source "for
   docs" too, which was never mechanically true.)

Constants (no environment override; change here, redeploy):
    APPROVAL_WINDOW_HOURS
        The ADVERTISED human-approval window in hours — the deadline written
        onto the approval record as ``timeout_at``, stated in the approval
        notification, and acted on by the timeout sweeper. Mirrors the gate
        (see 1. above).
    TASK_TOKEN_TTL_DAYS / TASK_TOKEN_TTL_SECONDS
        DynamoDB TTL backstop on stored Step Functions task tokens.
        DELIBERATELY LONGER than ``APPROVAL_WINDOW_HOURS`` — a token must
        outlive the longest legitimate approval wait rather than race it.
        The advertised deadline and this backstop are two different numbers
        on purpose; see the comment on the constant.
    APPROVAL_RECORD_RETENTION_DAYS
        DynamoDB TTL (auto-cleanup) on approval records, counted from
        creation. Retention/housekeeping — distinct from the approval
        *timeout* (``DEFAULT_TIMEOUT_HOURS`` below).
    CONFIDENCE_AUTO_APPROVE_THRESHOLD
        Schema-mapping confidence gate: average mapping confidence at or
        above this value is eligible for auto-approve; below it, human
        approval is required. Value 0.85 by the recorded p28b R4 decision
        (safer gate, accepting more human approvals), superseding the
        crystallized live 0.8 that P28 R18 had preserved pending that
        decision.
    FALLBACK_CONFIDENCE_CAP
        Ceiling applied to fallback (substring-match) mapping confidences.
        Derived strictly below ``CONFIDENCE_AUTO_APPROVE_THRESHOLD`` so a
        fallback mapping can never auto-approve on confidence (R3.2).
    MAPPABILITY_HARD_FAIL_THRESHOLD
        Source→target mappability floor (p55 R14): a run at or below this
        mapped-source-field ratio is REFUSED rather than routed to a human.
        A constant and NOT an accessor on purpose, and strictly below the
        ratio at which a human decision is already required — both reasons
        are on the constant itself, which is the only place the value
        appears. Its strict-nesting relationship to the approval clause is
        asserted by
        ``tests/unit/shared/test_config_mappability_nesting.py``.
    LOCK_TTL_SECONDS
        Pipeline-trigger lock TTL backstop (11 days) — frees a lock whose
        holder vanished without any release path running. It is sized above
        the state machine's own execution timeout (``EXECUTION_TIMEOUT_SECONDS``
        in ``deploy-cdk/lib/orchestration-stack.ts``, 10 days), so a lock can
        only expire after its execution has already ended one way or another.
        It was 24 h until 2026-10-11, with a note saying long approval waits
        were "covered because completion/failure paths release the lock
        explicitly". That was backwards: TTL deletion does not wait for a
        release path, so a lock whose execution was still parked at a 72 h
        gate was deleted at hour 24 and the next trigger ran beside it.
        SCOPE, added by p54 task 7.6 because every clause above is true only
        **of the lock row itself**: it says nothing about the uploaded file
        queued BEHIND that lock. The file waits in SQS on a redelivery budget
        of ``maxReceiveCount × visibilityTimeout`` = 1000 × 30 s = **30,000 s
        (8 h 20 min)** (``deploy-cdk/lib/orchestration-stack.ts``), while the
        lock can be held for up to 259,200 s (72 h) by a run parked at an
        approval gate (``LGR-69ca78b-2`` owns that window; ``LGR-5d68eeb-4``
        records that nothing releases a parked approval before it). So "long
        approval waits are covered" holds for the LOCK and NOT for the file —
        once the budget is exhausted the message is moved to the DLQ and the
        file is lost. p54 R10.2 requires the budget not be shorter than the
        hold; it is shorter, covering **11.6%** of it, a shortfall of 229,200 s
        (63 h 40 min).
        Those are p54 task 7.7's own numbers: ``LGR-e5e9edd-1`` raised
        ``maxReceiveCount`` from 50 to 1000 — SQS's documented ceiling — which
        took coverage from 0.6% to 11.6% and cut exposure 20x without closing
        it. Covering the full hold at a 30 s visibility timeout would need
        8,640 receives, so the residual is **unreachable by geometry** rather
        than a tuning choice. That is why the ratified arm is (d) + (a) and not
        (a) alone: (d) makes the residual VISIBLE via the Lost_File record
        rather than closing it, and ``LGR-e5e9edd-2`` owns how — a
        ``queue_outcome`` attribute, with the executions row keeping status
        ``QUEUED``. The inequality itself is asserted, not described, by
        ``tests/contracts/test_pipeline_trigger_redelivery_budget_cross_language.py``,
        which parses both literals out of the CDK source and carries R10.2 as
        an ``xfail(strict=True)`` — so closing the gap breaks that test on
        purpose. When these numbers move, they move there first; this docstring
        mirrors them and is not their source.
        This constant is NOT the lever: it is a crash backstop on the lock
        row, and it stays independent of the two queue constants and of
        ``QUEUED_RECORD_TTL_SECONDS`` below (p54 do-NOT 16 / R8.13).
    QUEUED_RECORD_TTL_SECONDS
        TTL on the transient "queued file" tracking records the trigger
        writes while a file waits for a concurrency slot.
    SKIPPED_RUN_RECORD_TTL_SECONDS
        TTL on Skipped_Run_Records — the executions-table rows that make a
        scheduled load which never happened visible in the run history.
    SKIPPED_RUN_NOTIFY_WINDOW_SECONDS
        Collapse window for contention skips: skips of the same pipeline for
        the same reason inside one window share one record and produce one
        notification.
    LOST_FILE_RECORD_TTL_SECONDS
        TTL on Lost_File records — the executions-table rows the queue-loss
        recorder writes for a file whose SQS redelivery budget ran out. A
        FOURTH constant on purpose: p54 R8.13 / do-NOT 16 forbid coupling it
        to any of the three above, and the 1 h queued TTL is now SHORTER than
        the redelivery budget, so it cannot serve. See the constant.

Environment-overridable values (read at call time; misconfiguration raises
``ValueError`` immediately — honest failure over a silent fallback):
    default_timeout_hours()          env DEFAULT_TIMEOUT_HOURS, default 72
        Default approval timeout in hours (= ``APPROVAL_WINDOW_HOURS``). The
        ``DEFAULT_TIMEOUT_HOURS`` environment-variable override on the
        approval Lambda keeps working unchanged, and a per-pipeline
        ``approvals.timeout_hours`` in ``pipeline_config`` still takes
        precedence over both.
    complex_mapping_threshold()      env COMPLEX_MAPPING_THRESHOLD, default 0.7
        Below this average confidence, the complex-mapping agent (P13) is
        eligible to assist. Read per invocation, matching the pre-R18
        behavior of the in-handler ``os.environ`` read.

Related configuration that intentionally does NOT live here:
    Model-call budgets (``MODEL_CALL_BUDGET``, ``MODEL_CALL_BUDGET_TABLE``)
    and per-call Converse parameters are owned by ``shared/model_config.py``,
    next to the budget-enforcement code — exactly one home per value, never
    two. ``model_config`` also owns its own budget-item TTL outright as of
    p34 R6: it used to derive that TTL from the approval window defined
    here, but a housekeeping TTL on a call counter has nothing to do with
    how long an approver is given, and shortening the window must not reap
    budget counters as a side effect.
"""

import os

# ── Approval window + its TTL backstop (P28 R18.1; window amended p34 R6) ──

#: The ADVERTISED human-approval window, in hours: what ``timeout_at`` on the
#: approval record is computed from, what the approval notification tells the
#: approver, and what the timeout sweeper acts on.
#:
#: 72 hours because THE GATE OWNS THIS NUMBER. The three ``waitForTaskToken``
#: approval gates carry ``APPROVAL_TIMEOUT_SECONDS`` in
#: ``deploy-cdk/lib/orchestration-stack.ts`` (p34 R6), and that timeout is
#: what actually terminates the wait — ``Catch[States.ALL]`` →
#: ``RecordFailure``. Advertising anything longer promises an approver time
#: the gate will not give them: told 7 days, they act on day 4 and find the
#: run already abandoned. When the gate constant moves, this moves with it,
#: never the other way round.
#:
#: The agreement is asserted rather than hoped for. TypeScript cannot import
#: Python and Python cannot import TypeScript, so
#: ``tests/contracts/test_approval_window_cross_language.py`` parses
#: ``APPROVAL_TIMEOUT_SECONDS`` out of the CDK source and asserts it equals
#: this constant — the same regex-the-CDK-literal technique
#: ``tests/integration/pipeline_flow/test_test_failure_gating.py`` uses for
#: the ``TestResultChoice`` literal.
APPROVAL_WINDOW_HOURS = 72

#: Fallback default for ``default_timeout_hours()`` — the advertised window.
DEFAULT_TIMEOUT_HOURS_DEFAULT = APPROVAL_WINDOW_HOURS

#: TTL backstop on stored Step Functions task tokens, in days.
#:
#: DELIBERATELY NOT ``APPROVAL_WINDOW_HOURS``, and deliberately longer than
#: it. A stored token must OUTLIVE the longest legitimate approval wait, so a
#: TTL equal to the window would race the very window it exists to survive —
#: a token reaped a moment early turns a decision that arrived in time into a
#: callback that can never be delivered. This is pure housekeeping with no
#: behavioral deadline attached, so being generous costs one small DynamoDB
#: row per approval and removes the race.
#:
#: This is why the advertised deadline and the TTL backstop are now two
#: different numbers: p34 R6 shortened the gate, and therefore the advertised
#: window, but had no reason to shorten the backstop.
TASK_TOKEN_TTL_DAYS = 7

#: DynamoDB TTL for stored Step Functions task tokens (see above).
TASK_TOKEN_TTL_SECONDS = TASK_TOKEN_TTL_DAYS * 24 * 3600

#: Auto-cleanup retention for approval records (DynamoDB TTL), in days.
APPROVAL_RECORD_RETENTION_DAYS = 30

# ── Confidence thresholds (P28 R18.2, R3.2) ──────────────────────────

#: Schema-mapping auto-approve confidence gate. Average confidence below
#: this requires human approval. See module docstring for value provenance.
CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.85

#: Fallback (substring-match) mapping confidences are capped strictly below
#: the auto-approve threshold BY CONSTRUCTION (R3.2 — no more 0.8-meets-0.8).
FALLBACK_CONFIDENCE_CAP = CONFIDENCE_AUTO_APPROVE_THRESHOLD - 0.05

#: Source→target mappability floor (p55 R14). A run whose mapped-source-field
#: ratio is at or below this value is REFUSED, not routed to a human.
#:
#: DELIBERATELY a constant and not an accessor. shared/config.py's two
#: accessors exist for named reasons that do not apply here — default_timeout_
#: hours() preserves a pre-existing operator override, complex_mapping_
#: threshold() matches a pre-R18 in-handler os.environ read. There is neither
#: here. A threshold that decides whether a run is REFUSED must not be
#: silently movable per environment: two accounts would then refuse
#: differently on identical data with no signal.
#:
#: STRICTLY BELOW the 0.8 implied by the >0.2 unmapped-source approval clause
#: (schema_mapping/lambda_function.py:1650), so the hard-fail region is a
#: strict SUBSET of the approval-required region and W5 can only ever turn a
#: human decision into a refusal, never into a pass (R14.7, do-NOT 13).
MAPPABILITY_HARD_FAIL_THRESHOLD = 0.5

#: Fallback default for ``complex_mapping_threshold()``.
COMPLEX_MAPPING_THRESHOLD_DEFAULT = 0.7

# ── Lock / transient-record TTLs (P28 R18.2) ─────────────────────────

#: Pipeline-trigger lock TTL backstop (safety net for stuck executions). Must
#: exceed the state machine's EXECUTION_TIMEOUT_SECONDS (10 days); see the
#: module docstring and tests/cdk/stacks/test_lock_ttl_exceeds_execution_timeout.py.
LOCK_TTL_SECONDS = 11 * 24 * 60 * 60

#: TTL on transient queued-file tracking records written by the trigger.
QUEUED_RECORD_TTL_SECONDS = 3600

#: TTL on Processed_File_Ledger rows (p30 R9.1/R9.4). 30 days.
#:
#: Both directions of this number are load-bearing and neither failure is loud:
#: too short silently re-enables duplicate ingestion (an accidental re-upload a
#: month later would be reprocessed), too long keeps rows — and their skips —
#: alive long after anyone would expect a re-upload to be treated as a
#: duplicate. 30 days is chosen as the outer bound of "someone re-uploaded the
#: file they already sent"; beyond that a resubmission is more likely to be
#: intentional, and the manual Run Now override covers the deliberate case
#: inside the window. Storage cost is negligible: one small row per distinct
#: content per pipeline, on a PAY_PER_REQUEST table.
PROCESSED_FILE_LEDGER_TTL_SECONDS = 30 * 24 * 60 * 60

#: TTL on Skipped_Run_Records (p30 R10.1/R10.2). 30 days.
#:
#: Deliberately NOT ``QUEUED_RECORD_TTL_SECONDS``. A queued tracker describes a
#: file that is about to be processed, so an hour is generous; a Skipped_Run_
#: Record describes a load that never happened, and R10's whole purpose is that
#: "a silent miss does not become a data gap I discover later" — a record that
#: expired an hour after the miss would be gone before anyone noticed the gap.
#:
#: Deliberately NOT sharing ``PROCESSED_FILE_LEDGER_TTL_SECONDS`` either, even
#: though the two currently hold the same number. The ledger's TTL is tuned to
#: "how long is a re-upload still plausibly accidental"; this one is tuned to
#: "how long should a missed load stay auditable". Those change for unrelated
#: reasons, and coupling them would silently retune one when the other moved.
SKIPPED_RUN_RECORD_TTL_SECONDS = 30 * 24 * 60 * 60

#: Collapse window for contention Skipped_Run_Records (p30 R10.1). 1 hour.
#:
#: A contention skip is unbounded in a way a duplicate-content skip is not: a
#: pipeline scheduled every five minutes against a busy fleet produces 288
#: skips a day, all identical. One record and one notification each would bury
#: the pipeline's real runs under skip rows and train the operator to ignore
#: the alert — the two failures R10 exists to prevent, reintroduced from the
#: other side. Skips of the same pipeline for the same reason inside one window
#: therefore share ONE record (carrying ``skip_count``) and produce ONE
#: notification, capping both at 24/day per pipeline per reason.
#:
#: One hour rather than one day because the record's timestamp is what an
#: operator reads as "when did this pipeline stop loading": an hour bounds that
#: answer usefully while still collapsing 12 five-minute skips into one row.
SKIPPED_RUN_NOTIFY_WINDOW_SECONDS = 3600

#: TTL on Lost_File records — the executions-table rows the queue-loss recorder
#: writes for a file whose SQS redelivery budget ran out (p54 R10.3). 30 days.
#:
#: **This is the FOURTH TTL constant on this table's transient rows, and it is
#: deliberately a fourth rather than a reuse of any of the three above.** p54
#: R8.13 / do-NOT 16 make that explicit, and the reasoning is the one
#: ``SKIPPED_RUN_RECORD_TTL_SECONDS`` states two constants up: these numbers
#: change for unrelated reasons, so coupling them silently retunes one when the
#: other moves. Each of the three is wrong for this row for its own reason:
#:
#: * ``QUEUED_RECORD_TTL_SECONDS`` (1 h) — wrong by an order of magnitude, and
#:   this is the arithmetic that makes the point. It is sized for "a file that
#:   is about to be processed", and the redelivery budget it was sized against
#:   was 25 min. p54 task 7.7 raised that budget to 8 h 20 min
#:   (``maxReceiveCount`` 1000 × ``visibilityTimeout`` 30 s in
#:   ``deploy-cdk/lib/orchestration-stack.ts``), so the Queued_Placeholder now
#:   normally EXPIRES BEFORE the loss it was describing. That is why the
#:   recorder ``put_item``s a complete row rather than ``update_item``-ing the
#:   placeholder: there is usually no placeholder left to annotate, and an
#:   update against a missing item would create ``{pipeline_id, run_id,
#:   queue_outcome}`` with no ``started_at`` — a row ``RunsByTimeIndex`` cannot
#:   index (invariant ``LGR-7d19eca-2``) and therefore an invisible record,
#:   which is the exact failure R10.3 exists to prevent.
#: * ``PROCESSED_FILE_LEDGER_TTL_SECONDS`` (30 days) — same number, different
#:   question. That one answers "how long is a re-upload still plausibly
#:   accidental".
#: * ``SKIPPED_RUN_RECORD_TTL_SECONDS`` (30 days) — the closest relative, and
#:   the one whose *reasoning* this constant borrows without borrowing its
#:   value: a record of something that never happened must stay auditable long
#:   enough for someone to notice the gap. But a skip is a decision the system
#:   made and a loss is a decision nothing made, so the two are free to diverge.
#:
#: 30 days satisfies R10.3's "readable for longer than the window in which the
#: loss occurred" with three orders of magnitude of margin over the 8 h 20 min
#: budget, and it comfortably outlives the DLQ's own 14-day message retention —
#: which is the whole reason the record cannot live in the DLQ.
LOST_FILE_RECORD_TTL_SECONDS = 30 * 24 * 60 * 60


# ── Environment readers (honest-ValueError pattern, as model_config) ──


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; empty/absent → default; garbage → ValueError."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Read a float env var; empty/absent → default; garbage → ValueError."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}") from exc


def default_timeout_hours() -> int:
    """Advertised approval window in hours: env ``DEFAULT_TIMEOUT_HOURS``, default 72.

    The ``DEFAULT_TIMEOUT_HOURS`` environment-variable override keeps working
    — this reads the same name the approval handler always read. Only the
    default moved, from 168 h to ``APPROVAL_WINDOW_HOURS`` (72 h), so the
    window we advertise matches the gate that enforces it (p34 R6).
    """
    return _env_int("DEFAULT_TIMEOUT_HOURS", DEFAULT_TIMEOUT_HOURS_DEFAULT)


def complex_mapping_threshold() -> float:
    """Complex-mapping-agent eligibility gate: env ``COMPLEX_MAPPING_THRESHOLD``, default 0.7.

    Read at call time (per invocation), matching the pre-R18 in-handler
    ``os.environ`` read so tests and deployed overrides behave identically.
    """
    return _env_float("COMPLEX_MAPPING_THRESHOLD", COMPLEX_MAPPING_THRESHOLD_DEFAULT)
