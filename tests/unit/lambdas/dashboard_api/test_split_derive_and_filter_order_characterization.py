# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization — ``_split_derive_and_filter``'s four step outcomes, as a set.

WHAT THIS FILE PINS, AND WHY IT IS NOT A DUPLICATE
--------------------------------------------------
``handlers/pipelines_runs._split_derive_and_filter`` (`:730-838`) runs four steps in
one fixed order, and its own docstring is the fence:

    *"This function exists so that the order of these four steps is spelled once"*
    … *"every position below is load-bearing and each is a do-NOT"*

    1. split ``QUEUED`` into ``queued_files``;
    2. phantom-row filter, on the POST-split ``executions`` list;
    3. derive ``WAITING_APPROVAL`` from ``pending_approval_id``;
    4. status filter, last — after 2 and after 3.

**p54 task 7.2's premise — "nothing asserts the order as such today, only the
behaviour of individual steps" — is FALSE IN EFFECT, and this was measured rather
than reasoned about.** Three reorderings were applied to ``pipelines_runs.py`` one at
a time and the three existing suites were run against each (the production file was
restored byte-identical afterwards, verified by SHA-256):

===  ============================================  ==================================
M    the reordering                                caught by the existing suites?
===  ============================================  ==================================
M1   step 4 above step 3 (filter above derive)      YES — 2 failures:
                                                   ``test_run_history_ordering.py::
                                                   TestStatusFilter::
                                                   test_waiting_approval_is_selectable
                                                   _and_its_stored_status_is_not`` and
                                                   ``test_run_history_status_filter
                                                   _prop.py``'s property
M2   step 2 above step 1 (phantom on all_items)     YES — 2 failures, both in
                                                   ``test_phantom_row_predicate
                                                   _prop.py``. ``ordering.py`` passes
                                                   entirely.
M3   step 4 above step 1 (filter on all_items)      YES — 3 failures, across both
                                                   ``ordering.py`` and the status
                                                   filter property
===  ============================================  ==================================

So do-NOT 14's order **is** fenced by tests today, by the composition of three suites
none of which names the order as its subject. Detection is not the gap. What this
file adds is therefore narrower than the task text claims, and is worth stating
precisely so a later reader does not delete it as redundant:

**(a) Attribution, not detection.** A reorder today produces failures scattered
across two or three files, and for M2 *only* inside a Hypothesis property test —
whose falsifying example has to be read before anyone can say which step moved. Here
one file fails, and the failing assertion's name says which position broke. That is
the difference between "something about run history is wrong" and "step 2 moved above
step 1".

**(b) Locality, so this can gate the edit that is coming.** Every existing assertion
travels through ``get_pipeline_executions``' cursor decode, bounded page walk,
``Select="COUNT"`` query and enrichment reads, over a moto partition: **25–37 s** for
the three suites. This file calls the function directly: **0.4 s**, no table handle
touched at all. p54 task 7.4 edits inside step 1's list comprehension, and a fence
that costs half a minute is one an implementer runs at the end rather than during.
Touching no table handle also keeps this file clear of the ``mock_aws`` prohibition
(``tests/unit/test_no_duplicate_mocking_prop.py``) and of the two-caller
``state_table.query`` trap (``LGR-28b4d75-5``).

**(c) The published entry's exact key set — the one genuinely unasserted thing.**
``tests/contracts/api_responses/test_queued_files_response_contract.py:115-116``
checks the four keys as a **required subset** (``required - set(qf.keys())``), which
catches a key going *missing* and passes for a fifth key appearing.
``test_run_history_ordering.py:468`` checks two of the four *values* and never the key
set. So nothing in the tree today would notice a **widening** — and widening is
exactly the failure mode do-NOT 3 fences and that task 7.4 must avoid, since it
changes the ``status`` **value** and must leave the key set alone. The set-equality
assertion below is that fence.

**(d) The phantom-shaped ``QUEUED`` row, which makes step 2's position observable at
all.** The in-code note at `:788-803` records that ``_track_queued_file`` writes
``current_step="queued_trigger"`` (`pipeline_trigger/lambda_function.py:689`), so a
real placeholder never matches ``is_phantom_execution_row`` and hoisting the filter
above the split "would therefore be equivalent today, and fragile".
``QUEUED_PHANTOM_SHAPED`` below is ``QUEUED`` with neither ``current_step`` nor
``steps_status`` — the shape step 2's own argument is defensive against ("whatever
shape the QUEUED placeholder turns out to have"). p54 task 7.1 measured **zero**
``QUEUED`` rows in the live executions table — every placeholder has TTL'd out, so
the p48-era claim that no such row exists was a measurement at a moment and not a
standing property (design do-NOT 15). This fixture is the only place the shape is
exercised anywhere in the tree.

Fixturing a shape no current producer writes is the established posture in this
Lambda, not a liberty taken here: ``LGR-5aad762-2`` (**template / ratified**) keeps
``approval_data`` first in a precedence order **though no producer writes it**, on
the ground that *"a live table can hold shapes no current producer writes"* — and
``LGR-67bf5d3-6`` rejected a status denylist for the preview walk citing the same
sentence. A defensive ordering whose defensiveness is never exercised is
indistinguishable from an accident.

EXPECTATIONS ARE DERIVED FROM THE DOCSTRING AND FROM CP3, NOT FROM CURRENT OUTPUT
--------------------------------------------------------------------------------
Every assertion below is traceable to a sentence in the function's own docstring, to
the four-key list in ``ExecutionsPage.tsx:567-589``'s Queued Files table, or to CP3
as stated in p48 design §Correctness Properties. Nothing was read off a run first.

GOVERNING RECORDS
-----------------
* ``LGR-67bf5d3-3`` (**template / ratified**) — steps 3-before-4, p48 do-NOT 8:
  *"Apply the status filter **after** the ``WAITING_APPROVAL`` derivation and
  **after** the phantom-row filter"*, because *"a filter applied to the stored value
  would return nothing for the one status an operator most wants to find. No writer
  stores ``WAITING_APPROVAL`` at all."* It also records why ``status=QUEUED`` is a
  400 and names the reason as structural: *"The ``QUEUED`` split runs before the
  filter position, so by the time the filter sees the list every ``QUEUED`` row has
  already moved into ``queued_files``."*
* ``LGR-21a1b5b-2`` (**template / ratified**) — governs step 2 by its reasoning
  rather than its decision: *"the run-history read path treats every non-QUEUED row
  as a run and pages it with a server-side Limit"*, so a non-run row *"would render
  as a phantom run unless three separate readers learned to ignore it."* That is why
  a phantom row must not reach ``executions``, and it is the constraint p54 task 7.4
  works inside when it changes what a queued entry carries.

MUTATION RECORD — WHICH ASSERTION HERE FAILS FOR WHICH REORDERING
-----------------------------------------------------------------
Verified by applying each reordering to ``pipelines_runs.py``, running this file, and
restoring the production file byte-identical (SHA-256 checked before and after):

* **M1**, step 4 above step 3 → 1 failure,
  ``test_cp3_the_filter_matches_the_derived_status_and_not_the_stored_one``
  (``Right contains one more item: 'run-…-awaiting'`` — the row matched its stored
  ``RUNNING`` because the filter no longer saw the derived value).
* **M2**, step 2 above step 1 → 3 failures:
  ``test_a_queued_row_lands_in_queued_files_and_not_in_executions``,
  ``test_a_phantom_run_is_dropped_but_a_phantom_shaped_queued_row_survives``,
  ``test_queued_at_is_started_at_renamed_on_the_way_out``. All three are the same
  fact: the phantom-shaped placeholder was removed before the split could publish it.
* **M3**, step 4 above step 1 → 2 failures:
  ``test_cp3_…`` and ``test_queued_files_does_not_vary_with_the_status_filter``.

**This file changes no production code.** That was true when task 7.2 landed it and it
is still true — the amendment 7.4 made here is to an assertion, not to a handler.

The SHA-256 7.2 recorded to prove the mutation runs had been reverted cleanly —
``be36dab69e1ffb5cd35cc4407ed3067a0e12b73fc89df39be7816caa9d5e3657`` — is
**superseded**: p54 task 7.4 has since edited ``pipelines_runs.py`` on purpose (step
1's comprehension now reads ``queue_outcome``). It is left here because it is the
evidence for M1–M3 above, which were measured against that revision. Do not re-check
the current file against it.

Validates: p54 Requirement 9.8
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Path and import-time env — must precede importing the handler, because
# ``handlers/common.py`` calls ``require_env`` at import time. Same block the
# sibling suites in this directory use; ``AWS_REGION`` is deliberately absent
# because ``tests/unit/conftest.py`` assigns it (not ``setdefault``) for the
# whole layer (``LGR-3e45e65-1``).
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import pipelines_runs as runs_handler

# ---------------------------------------------------------------------------
# The one input page. Five rows, each present for a named reason, ordered here
# newest-last for readability only — the function does not sort ``executions``.
# ---------------------------------------------------------------------------

#: ``QUEUED`` **and phantom-shaped** — no ``current_step``, no ``steps_status``.
#: The row that makes step 2's position observable rather than merely equivalent.
#: See (b) in the module docstring: no live placeholder has this shape today, and
#: task 7.1 measured zero live ``QUEUED`` rows at all.
QUEUED_PHANTOM_SHAPED = "queued-0000000000000000"

#: ``QUEUED`` in the shape ``_track_queued_file`` actually writes — ``current_step``
#: present (``pipeline_trigger/lambda_function.py:689``). Included so the assertions
#: hold for the real shape as well as the defensive one.
QUEUED_LIVE_SHAPED = "queued-1111111111111111"

#: A Phantom_Execution_Row: neither ``current_step`` nor ``steps_status``, which is
#: exactly what ``is_phantom_execution_row`` (``handlers/common.py:87-107``) tests.
#: Stored ``SUCCEEDED`` so its removal cannot be attributed to the status filter.
PHANTOM_RUN = "run-2026-08-27-10-00-00-phantom0"

#: Stored ``RUNNING`` **with** ``pending_approval_id`` — the row CP3 is about. Its
#: ``current_step`` is set so it survives step 2; step 3 overwrites it with
#: ``pending_approval_type``.
AWAITING_APPROVAL_RUN = "run-2026-08-27-11-00-00-awaiting"

#: Stored ``RUNNING`` **without** ``pending_approval_id``. Present so CP3's negative
#: half is not vacuous: ``status=RUNNING`` must return *this* row, which is what
#: distinguishes "the derivation moved the other row out" from "the filter is broken".
PLAIN_RUNNING_RUN = "run-2026-08-27-12-00-00-running0"

#: The published shape of a queued entry: ``ExecutionsPage.tsx:567-589``'s four
#: columns, and do-NOT 3's subject. ``queued_at`` is ``started_at`` **renamed on the
#: way out** — the rename is why do-NOT 3 says a synthesised ``executions`` entry
#: would have to invent ``started_at`` back.
PUBLISHED_QUEUED_KEYS = {"file_name", "source_key", "queued_at", "status"}

#: A ``QUEUED`` row carrying ``queue_outcome: "lost_to_dlq"`` — the Lost_File shape p54
#: task 7.4 gives the ``failed_dlq`` reader a producer for. Added by 7.4, used only by
#: ``test_the_status_value_is_the_state_that_is_true``, and appended to a fresh page
#: there rather than added to :func:`_page` so the other seven assertions in this file
#: are byte-identical to what 7.2 landed.
LOST_TO_DLQ_PLACEHOLDER = "queued-3333333333333333"


def _page() -> list[dict[str, Any]]:
    """A fresh raw index page carrying all five shapes.

    **A factory, not a module constant, and that is load-bearing.** Step 3 mutates
    the caller's dicts in place (``exec_item["status"] = "WAITING_APPROVAL"`` at
    ``pipelines_runs.py:816``), so a page reused across two calls would have
    ``AWAITING_APPROVAL_RUN`` already storing ``WAITING_APPROVAL`` on the second.
    CP3's negative half would then pass for the wrong reason — the row would miss
    ``status=RUNNING`` because a previous call rewrote it, not because the derivation
    runs before the filter.
    """
    return [
        {
            "pipeline_id": "pipeline-p54-t72",
            "run_id": QUEUED_PHANTOM_SHAPED,
            "status": "QUEUED",
            "file_name": "deferred_second.csv",
            "source_key": "uploads/deferred_second.csv",
            "started_at": "2026-08-27T09:00:00.000000+00:00",
        },
        {
            "pipeline_id": "pipeline-p54-t72",
            "run_id": QUEUED_LIVE_SHAPED,
            "status": "QUEUED",
            "file_name": "deferred_third.csv",
            "source_key": "uploads/deferred_third.csv",
            "started_at": "2026-08-27T09:30:00.000000+00:00",
            "current_step": "queued_trigger",
        },
        {
            "pipeline_id": "pipeline-p54-t72",
            "run_id": PHANTOM_RUN,
            "status": "SUCCEEDED",
            "started_at": "2026-08-27T10:00:00.000000+00:00",
        },
        {
            "pipeline_id": "pipeline-p54-t72",
            "run_id": AWAITING_APPROVAL_RUN,
            "status": "RUNNING",
            "started_at": "2026-08-27T11:00:00.000000+00:00",
            "current_step": "code_generation",
            "steps_status": {"code_generation": "running"},
            "pending_approval_id": "appr-7c1f",
            "pending_approval_type": "code_review",
        },
        {
            "pipeline_id": "pipeline-p54-t72",
            "run_id": PLAIN_RUNNING_RUN,
            "status": "RUNNING",
            "started_at": "2026-08-27T12:00:00.000000+00:00",
            "current_step": "run_pipeline",
            "steps_status": {"run_pipeline": "running"},
        },
    ]


def _run_ids(executions: list[dict[str, Any]]) -> list[str]:
    return [e["run_id"] for e in executions]


class TestTheFourStepOutcomes:
    """The four outcomes, one assertion each, from one input page.

    Read together they pin the order: outcome 2 fails if step 2 moves above step 1,
    and outcome 4 (CP3) fails if step 4 moves above step 3. Steps 1 and 3 are pinned
    by outcomes 1 and 3 being simultaneously true of the same page.
    """

    # -- Outcome 1: step 1, the QUEUED split ------------------------------
    def test_a_queued_row_lands_in_queued_files_and_not_in_executions(self):
        """Step 1. Both halves of the split, on both placeholder shapes.

        The docstring's step 1: *"split ``QUEUED`` into ``queued_files``"*. Asserted
        in both directions because a split that copies rather than moves satisfies
        the first half alone — and a ``QUEUED`` row reaching ``executions`` is the
        Phantom_Execution_Row problem ``LGR-21a1b5b-2`` describes, a non-run row
        rendering as a run.
        """
        executions, queued_files = runs_handler._split_derive_and_filter(_page(), None)

        assert [q["file_name"] for q in queued_files] == ["deferred_second.csv", "deferred_third.csv"], (
            "both QUEUED rows are published, sorted by queued_at"
        )
        assert QUEUED_PHANTOM_SHAPED not in _run_ids(executions)
        assert QUEUED_LIVE_SHAPED not in _run_ids(executions)

    # -- Outcome 2: step 2's POSITION, post-split --------------------------
    def test_a_phantom_run_is_dropped_but_a_phantom_shaped_queued_row_survives(self):
        """Step 2, and specifically **where** it runs.

        Two halves, and the second is the one no existing test makes. The docstring:
        *"this runs on the POST-split ``executions`` list, never on ``all_items`` …
        a filter applied after the split cannot remove a row from ``queued_files``,
        whatever shape the QUEUED placeholder turns out to have."*

        ``QUEUED_PHANTOM_SHAPED`` carries neither ``current_step`` nor
        ``steps_status``, so it **would** match ``is_phantom_execution_row``. If step
        2 ran on ``all_items``, or if the split moved below it, that row would be
        gone from ``queued_files`` and an operator's deferred file would be invisible
        — which is R9's whole subject. This assertion is what fails in that case.
        """
        executions, queued_files = runs_handler._split_derive_and_filter(_page(), None)

        assert PHANTOM_RUN not in _run_ids(executions), (
            "a row no state-recording writer has touched must not render as a run"
        )
        # The published entry carries no ``run_id`` (do-NOT 3), so the phantom-shaped
        # placeholder is identified by the file it names.
        assert "deferred_second.csv" in [q["file_name"] for q in queued_files], (
            "the phantom predicate runs post-split, so it cannot reach queued_files — "
            f"{QUEUED_PHANTOM_SHAPED} matches is_phantom_execution_row and must survive anyway"
        )

    # -- Outcome 3: step 3, the derivation --------------------------------
    def test_a_running_row_with_a_pending_approval_reads_waiting_approval(self):
        """Step 3. The stored value is ``RUNNING``; the published one is not.

        ``LGR-67bf5d3-3``: *"No writer stores ``WAITING_APPROVAL`` at all."* So the
        string can only appear here, and its appearance is the derivation. The
        ``current_step`` rewrite is asserted alongside it because step 3 does both in
        one pass and a partial derivation would leave the row labelled with the step
        it was executing rather than what it is waiting for.
        """
        executions, _ = runs_handler._split_derive_and_filter(_page(), None)
        derived = {e["run_id"]: e for e in executions}

        assert derived[AWAITING_APPROVAL_RUN]["status"] == "WAITING_APPROVAL"
        assert derived[AWAITING_APPROVAL_RUN]["current_step"] == "code_review"
        assert derived[PLAIN_RUNNING_RUN]["status"] == "RUNNING", (
            "a row with no pending approval keeps its stored status"
        )

    # -- Outcome 4: CP3, step 4 after step 3 ------------------------------
    def test_cp3_the_filter_matches_the_derived_status_and_not_the_stored_one(self):
        """**CP3.** The observable consequence of step 4 running after step 3.

        p48 design §Correctness Properties, Property 3: for a row with
        ``pending_approval_id`` set, ``status=WAITING_APPROVAL`` includes it and
        ``status=RUNNING`` — its stored value — does not.

        Both halves, because either alone is satisfiable by a wrong implementation.
        A filter above the derivation returns **nothing** for ``WAITING_APPROVAL``
        *and* returns this row for ``RUNNING``; asserting only the first half passes
        for an implementation that filters the stored value on a page that happens
        to hold no approval-pending rows. ``PLAIN_RUNNING_RUN`` is why the negative
        half is not vacuous: ``status=RUNNING`` must still return something.

        Each call gets a fresh page — see ``_page``'s docstring on why.
        """
        waiting, _ = runs_handler._split_derive_and_filter(_page(), "WAITING_APPROVAL")
        running, _ = runs_handler._split_derive_and_filter(_page(), "RUNNING")

        assert _run_ids(waiting) == [AWAITING_APPROVAL_RUN], (
            "the derived status matches — this is the half that fails if step 4 moves above step 3"
        )
        assert _run_ids(running) == [PLAIN_RUNNING_RUN], (
            "the stored status must not match once it is derived away, and the plain "
            "RUNNING row must still match so the assertion is not vacuous"
        )


class TestThePublishedQueuedEntryShape:
    """do-NOT 3's subject: four keys and only four.

    ``ExecutionsPage.tsx:567-589`` renders exactly ``file_name``, ``source_key``,
    ``queued_at``, ``status``. p54 task 7.4 changes the ``status`` **value** — from an
    unconditional ``"queued"`` to ``"queued"`` or ``"failed_dlq"`` — and this class is
    what pins the key set while that value moves.
    """

    def test_the_entry_carries_exactly_the_four_published_keys(self):
        """No fifth key, and none of the four missing.

        Asserted as set equality rather than as four ``in`` checks, because the
        failure this guards against is a *widening*: a fifth key would satisfy every
        containment check. ``run_id`` in particular must stay absent —
        ``LGR-67bf5d3-3`` records that a queued entry *"has no ``run_id`` and no
        ``started_at`` in its published shape"*, which is the structural reason
        ``status=QUEUED`` is refused with a 400 rather than merged into
        ``executions``.
        """
        _, queued_files = runs_handler._split_derive_and_filter(_page(), None)

        assert queued_files, "the page carries two QUEUED rows"
        for entry in queued_files:
            assert set(entry) == PUBLISHED_QUEUED_KEYS

    def test_queued_at_is_started_at_renamed_on_the_way_out(self):
        """The rename, which is why the two shapes cannot be merged.

        The raw row stores ``started_at``; the published entry spells it
        ``queued_at``. ``started_at`` being present on the raw placeholder is itself
        load-bearing — it is why these rows appear in ``RunsByTimeIndex`` and reach
        this function at all.
        """
        page = _page()
        _, queued_files = runs_handler._split_derive_and_filter(page, None)
        by_file = {q["file_name"]: q for q in queued_files}

        assert by_file["deferred_second.csv"]["queued_at"] == "2026-08-27T09:00:00.000000+00:00"
        assert "started_at" not in by_file["deferred_second.csv"]

    def test_the_status_value_is_the_state_that_is_true(self):
        """**Updated by p54 task 7.4 — deliberately, and this is the record of it.**

        Before 7.4 this method was ``test_the_status_value_is_queued_today`` and
        asserted ``{q["status"] for q in queued_files} == {"queued"}``, with its own
        docstring naming itself as *"the one that has to be updated deliberately
        rather than discovered to be stale"*. 7.4 made the published ``status`` the
        state that is true, so the old assertion is now false by design rather than
        by regression, and it is **amended in place with the reason at the assertion**
        — this repo's convention for a deliberate reversal (``LGR-9e76249-10``,
        ``LGR-60c4985-6``, ``LGR-78ce0b7-8``: change it on purpose and keep it legible
        as a reversal instead of deleting the coverage).

        The two published values, from
        ``_split_derive_and_filter``'s step-1 comprehension:

        * ``queue_outcome`` absent → ``"queued"`` (a Deferred_File). Absence carries
          meaning here — the only thing that writes an outcome is the loss path — so
          this is not absence collapsed into a value (do-NOT 7).
        * ``queue_outcome == "lost_to_dlq"`` → ``"failed_dlq"`` (a Lost_File), the
          literal ``ExecutionsPage.tsx``'s branch was written for and had no producer
          for until 7.4.

        **The stored ``status`` stays ``"QUEUED"`` on both rows** — asserted below,
        because that is do-NOT 19 / ``LGR-21a1b5b-2``: the split, the phantom filter,
        the ``StatusIndex`` GSI, the DynamoDB Stream and the run-history reader must
        all behave exactly as before, which they do only if the lost fact rides on a
        separate attribute. The three sibling classes in this file are what verify
        that, and they are unmodified.

        ``LOST_TO_DLQ_PLACEHOLDER`` is a shape **no current producer writes** — its
        producer is task 7.7's DLQ handler. Fixturing such a shape is the established
        posture in this Lambda: ``LGR-5aad762-2`` (**template / ratified**) keeps
        ``approval_data`` first in a precedence order though nothing writes it, on the
        ground that *"a live table can hold shapes no current producer writes"*.
        """
        page = _page()
        page.append(
            {
                "pipeline_id": "pipeline-p54-t72",
                "run_id": LOST_TO_DLQ_PLACEHOLDER,
                # The row's own status is UNCHANGED. This is the do-NOT 19 half.
                "status": "QUEUED",
                "file_name": "customer_data_7.csv",
                "source_key": "mynewtesting/customer_data_7.csv",
                "started_at": "2026-08-27T08:00:00.000000+00:00",
                "current_step": "queued_trigger",
                "queue_outcome": "lost_to_dlq",
            }
        )

        executions, queued_files = runs_handler._split_derive_and_filter(page, None)
        by_file = {q["file_name"]: q for q in queued_files}

        assert by_file["customer_data_7.csv"]["status"] == "failed_dlq", (
            "a file whose redelivery budget ran out publishes the lost state"
        )
        assert by_file["deferred_second.csv"]["status"] == "queued"
        assert by_file["deferred_third.csv"]["status"] == "queued", (
            "no queue_outcome attribute means the file is still coming back"
        )
        assert LOST_TO_DLQ_PLACEHOLDER not in _run_ids(executions), (
            "the lost row is still a QUEUED row to every reader of `status`, so it is "
            "still split out of executions — do-NOT 19"
        )
        assert set(by_file["customer_data_7.csv"]) == PUBLISHED_QUEUED_KEYS, (
            "the value moved; the key set did not (do-NOT 3)"
        )

    def test_an_unrecognised_queue_outcome_does_not_claim_a_loss(self):
        """A value the one producer did not write is not evidence of loss.

        Added by p54 task 7.4 alongside the amendment above, because the comparison it
        introduces is exact and a reader will ask what a near-miss does. Exact rather
        than normalised follows ``LGR-ad78946-1`` — *"normalising removes the only
        signal that the deployed value is not the intended one"* — and the arm it
        falls to is ``"queued"`` rather than ``"failed_dlq"`` because claiming a loss
        on an unrecognised value would be the invented fact R9.5's three-state
        discipline exists to prevent.
        """
        page = _page()
        page.append(
            {
                "pipeline_id": "pipeline-p54-t72",
                "run_id": "queued-2222222222222222",
                "status": "QUEUED",
                "file_name": "near_miss.csv",
                "source_key": "uploads/near_miss.csv",
                "started_at": "2026-08-27T08:30:00.000000+00:00",
                "current_step": "queued_trigger",
                "queue_outcome": "LOST_TO_DLQ",
            }
        )

        _, queued_files = runs_handler._split_derive_and_filter(page, None)
        by_file = {q["file_name"]: q for q in queued_files}

        assert by_file["near_miss.csv"]["status"] == "queued"


class TestTheUnfilteredPathIsUnchangedByTheFilterArgument:
    """``status=None`` means no filter — the byte-equivalence claim in the docstring.

    *"``status`` of ``None`` means no filter and the returned ``executions`` are
    exactly what this function returned before task 4.3 … which is what keeps do-NOT
    3 (``get_queued_files``, which calls with defaults) and CP6 untouched."*
    """

    def test_queued_files_does_not_vary_with_the_status_filter(self):
        """Step 1 runs above step 4, so the filter cannot reach ``queued_files``.

        ``LGR-67bf5d3-3``'s structural reason for the ``status=QUEUED`` 400, stated
        as an assertion: *"by the time the filter sees the list every ``QUEUED`` row
        has already moved into ``queued_files``."* If step 4 moved above step 1, a
        ``status=RUNNING`` request would empty this array.
        """
        _, unfiltered = runs_handler._split_derive_and_filter(_page(), None)
        _, filtered = runs_handler._split_derive_and_filter(_page(), "RUNNING")

        assert filtered == unfiltered
