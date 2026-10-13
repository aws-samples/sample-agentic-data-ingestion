# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Requirement-derived tests — the storage tile searches for buckets that exist.

Feature: p39-metrics-truth, task 4.2. Subjects: ``_get_s3_storage_bytes`` and
``_deploy_bucket_search_tokens`` in ``handlers/metrics.py`` (task 4.1).

WHERE THE EXPECTATIONS COME FROM
--------------------------------
**Every expectation here is written from ``R1.2``-``R1.5``, and none of it from
what this function returned before p39.** The pre-4.1 output for a populated
account was ``0`` — the SEARCH looked for the bare token
``agentic_data_ingestion``, which is the *snake* naming form this repo uses for
Lambdas, Glue jobs and state machines, while every bucket carries the *kebab*
form (``adi-<purpose>-<account>-<region>``, ``foundation-stack.ts:67-121``). The
filter matched zero series and ~135 MB of real storage was published as a byte
count of zero. **That zero appears nowhere below as a baseline**
(`LGR-28b4d75-5`, **template / ratified**: a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle). Where it appears at all it is asserted *against* — ``R1.2`` and ``R1.3``
both name it explicitly as the wrong answer, so "not zero" is part of the
requirement rather than a memory of the defect.

WHAT IS ASSERTED, CLAUSE BY CLAUSE
----------------------------------
=========  =====================================================================
``R1.2``   at least one deployed bucket reporting a ``StandardStorage`` datapoint
           in the window yields a total **greater than zero**
``R1.3``   a successful query with no datapoints reports Not_Established — an
           ``_absent`` token, not a byte count of zero
``R1.4``   a raising client reports Not_Established with an ``_incomplete``
           token, and records the failure at WARNING or above
``R1.5``   the bucket-name filter is derived from the deploy-injected identifier,
           asserted as a **data dependency** — see the next section
=========  =====================================================================

Also covered, because task 4.1 introduced them and neither is named by a clause
of its own: the two arms in which no filter can be built — ``PROCESSED_BUCKET``
unset, and a value not in the deploy naming form — report ``_absent`` **and
issue no query at all**. A guessed prefix there would be the hand-written
identifier ``R1.5`` exists to remove.

THE ``R1.5`` STRUCTURAL GUARD, AND WHY IT IS NOT A SUBSTRING SEARCH
-------------------------------------------------------------------
Task 4.2's text asks for "a structural guard that ``metrics.py`` contains no
hand-written bucket-name substring". **A source-text search over the file cannot
express that**, and task 4.1 left the reason in the module at
``metrics.py:1310-1331``: the prose there has to quote the real bucket names to
explain the defect, and this module legitimately hand-writes
``agentic_data_ingestion`` in the ``AWS/Lambda`` (``:767``, ``:779``, ``:791``),
``AWS/States`` (``:837``, ``:855``, ``:873``) and ``AWS/DynamoDB`` (``:916``,
``:928``) SEARCHes, where it is the *correct* identifier for those resources. A
file-wide substring guard therefore cannot distinguish the fix from the
documentation of the fix, nor either from an unrelated correct literal — it would
fail on the fence that exists to keep the fence. That is the trap
`LGR-b46a5bb-4` (**template / ratified**) records verbatim for the
``threading.Thread`` scan: substring matching "forbids documenting the fence, and
failed for exactly that reason". ``test_metrics_polarity.py`` (task 2.3) solved
the same problem for ``R3.2`` with an AST walk that excludes docstrings.

What ``R1.5`` actually asserts is a **data dependency**: "the filter SHALL be
derived from the same identifier that names the buckets at deploy time, so that a
rename of the deployment prefix cannot silently empty the series." So the guard
observes the expression the module *sends to CloudWatch*, under two synthetic
``PROCESSED_BUCKET`` values, and asserts three things about it:

1. **It changes with the identifier** (:func:`test_the_search_expression_changes_with_the_deploy_bucket_name`).
   Two deploy-form values sharing no segment produce two different expressions,
   so a rename cannot leave the filter behind.
2. **Every term it adds came from that identifier**
   (:func:`test_every_deploy_derived_term_is_a_segment_of_the_bucket_name`). The
   structural vocabulary is identified *without a hardcoded allowlist*, as the
   tokens the two expressions share; everything else must be a segment of the
   bucket name it was built from.
3. **Neither of the repo's two naming forms appears in it** unless the bucket
   name carried it (:func:`test_the_search_expression_carries_no_repo_naming_form_of_its_own`).
   This is the text-level half, and it is **scoped to the S3 SEARCH expression**
   rather than to the file — which is exactly what lets the eight correct
   literals elsewhere in the module, and the fence's prose, stay untouched. It is
   here because guard 2 cannot catch a hand-written constant on its own: a
   literal is present in *both* expressions, so the intersection classifies it as
   structural. The two guards are complementary and
   :func:`test_the_historical_defect_is_caught_by_the_naming_form_guard_only`
   demonstrates precisely that, against the pre-4.1 expression.

**Token composition is deliberately not pinned.** Task 4.3 is confirming
read-only against the live account whether the deploy suffix should be AND-ed
single segments or one concatenated string, and may change
``_deploy_bucket_search_tokens``' return value. Nothing here asserts the token
list, the term count, the join character or the expression string. Guard 1 needs
only that the expression differ across two wholly different names; guard 2 needs
only that each term be *a* segment; guard 3 is a substring check. All three hold
under either composition, under a term list that grows or shrinks, and under a
different join.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
* **No assertion on which datapoint is selected.** ``R1.2`` asks for "a total
  greater than zero". That the read is ``ScanBy="TimestampDescending"`` over a
  two-day window and takes the most recent daily sum is task 4.1's choice, not a
  requirement; :func:`test_the_total_is_a_reported_daily_sum` asserts only that
  the answer is one of the sums CloudWatch reported and not an aggregate of
  several, which is what makes it a *total* rather than a fabrication.
* **No Hypothesis.** Tasks 21.1-21.6 own the property tests; ``R1.2``/``R1.3``
  are two of Property 1's clauses. What is here is example coverage of the
  requirement clauses plus the structural guard.
* **No live AWS.** Task 4.3 owns the live confirmation, read-only.

MOCKING
-------
CloudWatch is outside the shared fixtures' scope (they cover DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` is patched with ``MagicMock`` —
the established pattern for these read paths (`test_query_not_scan.py` and the
task-1 characterization files). No ``mock_aws()`` is created here and no
fixture-covered service is stubbed by hand. ``PROCESSED_BUCKET`` is set through
``monkeypatch.setenv`` so it cannot leak between tests;
``_get_s3_storage_bytes`` reads it at call time, so no module reload is needed.
``AWS_REGION`` is deliberately **not** set — the unit layer pins it by
assignment in ``tests/unit/conftest.py`` (`LGR-3e45e65-1`, **template /
ratified**).

**Expected result: green on the first run.** Task 4.1 has landed, so the
expected-red window this task's entry names is closed. A red here is a real
finding about the read behind the test, not a test to adjust.

**Validates: Requirements 1.2, 1.3, 1.4, 1.5**

_Requirements: R1.2, R1.3, R1.4, R1.5_
"""

import os
import pathlib
import re
import sys
from datetime import UTC, datetime
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_metrics_absence.py and
# the task-1 characterization files repeat it — under xdist --dist=loadfile any
# file in this directory may be the first one imported on a worker.
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# handlers.common resolves nine table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

METRICS_PATH = pathlib.Path(os.path.abspath(metrics_mod.__file__))
METRICS_SOURCE = METRICS_PATH.read_text()

# ═══════════════════════════════════════════════════════════════════════════
# Vocabulary
# ═══════════════════════════════════════════════════════════════════════════

#: R4.2's two suffixes, written out literally because the requirement fixes these
#: words. The source *name* comes from the module's own SOURCE_* constant,
#: because no requirement fixes that.
ABSENT = "_absent"
INCOMPLETE = "_incomplete"

END = datetime(2025, 4, 2, tzinfo=UTC)

#: A deploy-form bucket name in the live shape (`foundation-stack.ts:80-83`
#: builds `adi-processed-files-${account}-${region}`), used where the case under
#: test is about the *outcome* rather than about the filter.
DEPLOY_BUCKET = "adi-processed-files-444455556666-us-west-2"

#: Two synthetic deploy-form names sharing **no segment** with each other and
#: neither carrying either of this repo's naming forms. Guard 1 needs the first
#: property (a rename must move the filter); guard 3 needs the second (anything
#: recognisably hand-written in the expression cannot have come from here).
RENAMED_BUCKET_A = "zzz-widgets-999999999999-eu-north-1"
RENAMED_BUCKET_B = "qqq-gadgets-111111111111-ap-south-2"

#: The two identifier forms this repo actually deploys under. Both are checked
#: because Group A was a *form* error rather than a typo: the snake form is
#: correct for Lambdas, Glue jobs and state machines and appears eight times in
#: this module for exactly that reason, while buckets carry the kebab form.
#: `2b45b72912` had to teach the Glue IAM ARNs to carry both.
SNAKE_NAMING_FORM = "agentic_data_ingestion"
KEBAB_NAMING_FORM = "agentic-data-ingestion"
DEPLOY_BUCKET_PREFIX = "adi-"

#: Values from which no deploy scope can be recovered, so no filter can be built.
#: ``None`` means the variable is absent altogether. The two names are chosen to
#: be unusable under *any* reading of the deploy form — neither carries a
#: twelve-digit account segment at all — rather than to probe task 4.1's segment
#: index rules, which task 4.3 may revise.
UNUSABLE_BUCKET_VALUES = [
    None,
    "",
    "   ",
    "some-random-bucket",
    "not-a-deploy-bucket-name",
]

REMEDIATION_R15 = (
    "R1.5: the bucket-name filter must be derived from the same identifier that names the buckets at "
    "deploy time (PROCESSED_BUCKET, injected at compute-stack.ts:645 from foundation.processedBucket."
    "bucketName), so a rename of the deployment prefix cannot silently empty the series. Every term in "
    "the S3 SEARCH expression must be a segment of that value. Do not put a bucket-name literal back "
    "into the expression in any form — not the deploy prefix, not the account id, not a purpose "
    "segment. See metrics.py:1310-1331."
)


class StorageRun(NamedTuple):
    """One call to ``_get_s3_storage_bytes`` and what it did on the way.

    ``queries`` is the ``MetricDataQueries`` list of every ``get_metric_data``
    call, so a test can assert both what was asked and — for the two no-filter
    arms — that nothing was asked at all.
    """

    outcome: Any
    queries: list[list[dict[str, Any]]]
    logger: MagicMock

    @property
    def search_expression(self) -> str:
        """The one ``SEARCH(...)`` expression the read issued.

        Located by looking for ``SEARCH(`` rather than by the query ``Id``, so a
        rename of the ids leaves this working. Fails loudly rather than
        returning an empty string: a guard scanning nothing passes for free.
        """
        assert len(self.queries) == 1, f"expected exactly one get_metric_data call, got {len(self.queries)}"
        expressions = [q["Expression"] for q in self.queries[0] if "SEARCH(" in q.get("Expression", "")]
        assert len(expressions) == 1, (
            f"expected exactly one SEARCH expression in the S3 storage read, got {len(expressions)}: {expressions!r}"
        )
        return expressions[0]


def _cw_response(values: list[float] | None, *, series_id: str = "s3sum") -> dict[str, Any]:
    """A ``get_metric_data`` response carrying ``values`` on the reduction series.

    ``series_id`` is parameterised only so the "reduction series absent from the
    response" case is reachable; every other caller takes the default.
    """
    return {
        "MetricDataResults": [
            {
                "Id": series_id,
                "Label": "SUM(s3_raw)",
                "Timestamps": [END for _ in values or []],
                "Values": list(values or []),
                "StatusCode": "Complete",
            }
        ]
    }


def run_storage(
    monkeypatch: pytest.MonkeyPatch,
    bucket_name: str | None,
    *,
    response: dict[str, Any] | None = None,
    values: list[float] | None = None,
    side_effect: Exception | None = None,
) -> StorageRun:
    """Call ``_get_s3_storage_bytes`` with ``PROCESSED_BUCKET`` set to ``bucket_name``.

    ``bucket_name=None`` deletes the variable. ``monkeypatch.setenv`` is used so
    the value cannot leak into another test; the function reads the variable at
    call time, so no reload is needed.
    """
    if bucket_name is None:
        monkeypatch.delenv(metrics_mod._DEPLOY_BUCKET_ENV, raising=False)
    else:
        monkeypatch.setenv(metrics_mod._DEPLOY_BUCKET_ENV, bucket_name)

    mock_cw = MagicMock()
    if side_effect is not None:
        mock_cw.get_metric_data.side_effect = side_effect
    else:
        mock_cw.get_metric_data.return_value = response if response is not None else _cw_response(values)

    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "logger") as mock_logger,
    ):
        outcome = metrics_mod._get_s3_storage_bytes(END)

    queries = [call.kwargs["MetricDataQueries"] for call in mock_cw.get_metric_data.call_args_list]
    return StorageRun(outcome=outcome, queries=queries, logger=mock_logger)


def assert_absent(outcome: Any) -> None:
    """An established nothing from the bucket-size source (``R1.3``, ``R4.2``).

    Value ``None`` **and** the ``_absent`` token, asserted together because
    either alone is satisfiable by a defect: a ``None`` under an unsuffixed token
    claims a measurement was obtained, and an ``_absent`` token beside a number
    contradicts itself inside one record.
    """
    source = metrics_mod.SOURCE_S3_BUCKET_SIZE
    assert outcome.value is None, f"expected Not_Established from {source}, got value {outcome.value!r}"
    assert outcome.determined_from == f"{source}{ABSENT}", (
        f"an established nothing from {source} must carry '{source}{ABSENT}' (R4.2), got {outcome.determined_from!r}"
    )


def assert_incomplete(outcome: Any) -> None:
    """An unreadable bucket-size source — "we could not look" (``R1.4``, ``R4.2``).

    Kept apart from :func:`assert_absent`: an operator acts differently on "there
    is nothing there" than on "the read failed", so the two must not share a
    token.
    """
    source = metrics_mod.SOURCE_S3_BUCKET_SIZE
    assert outcome.value is None, f"expected Not_Established from {source}, got value {outcome.value!r}"
    assert outcome.determined_from == f"{source}{INCOMPLETE}", (
        f"an unreadable {source} must carry '{source}{INCOMPLETE}' (R4.2), got {outcome.determined_from!r}"
    )


def assert_recorded_at_warning(run: StorageRun, what: str) -> None:
    """The failure reached the log at WARNING or above.

    "or above" is ``R1.4``'s own wording, so ``error`` and ``exception`` satisfy
    it too. A Not_Established token with no log is a silent failure.
    """
    assert run.logger.warning.called or run.logger.error.called or run.logger.exception.called, (
        f"{what} must be recorded at WARNING level or above (R1.4)"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Detectors for the R1.5 data-dependency guard
# ═══════════════════════════════════════════════════════════════════════════
# Pure functions over strings so they can be shown to work — the negative
# controls at the bottom drive them directly. A guard whose detector is never
# exercised on a violation is a guard nobody knows can fail.

_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def expression_tokens(expression: str) -> set[str]:
    """Every alphanumeric run in ``expression``.

    Splitting on non-alphanumerics rather than on whitespace is what makes this
    composition-agnostic: ``999999999999-eu-north-1`` and
    ``999999999999 eu north 1`` yield the same set, so task 4.3 may change the
    term composition without touching these guards.
    """
    return set(_ALNUM_RUN.findall(expression))


def bucket_name_segments(bucket_name: str) -> set[str]:
    """Every alphanumeric run in ``bucket_name`` — what a filter may be built of."""
    return set(_ALNUM_RUN.findall(bucket_name))


def structural_tokens(expression_a: str, expression_b: str) -> set[str]:
    """The vocabulary two expressions share: namespace, metric name, statistic, period.

    Identified as the intersection rather than from a hardcoded allowlist, so a
    change to the period, the statistic or the dimension list is not a violation
    and needs no edit here. The *cost* of that choice is that a hand-written
    constant is also shared and so also lands here — which is why
    :func:`test_the_search_expression_carries_no_repo_naming_form_of_its_own`
    exists, and why
    :func:`test_the_historical_defect_is_caught_by_the_naming_form_guard_only`
    pins the division of labour between the two.
    """
    return expression_tokens(expression_a) & expression_tokens(expression_b)


def undeclared_terms(expression: str, bucket_name: str, structural: set[str]) -> set[str]:
    """Terms in ``expression`` that are neither structural nor from ``bucket_name``.

    Empty is the passing answer for ``R1.5``: every term the module contributes
    to the filter must be traceable to the deploy-injected identifier.
    """
    return expression_tokens(expression) - structural - bucket_name_segments(bucket_name)


def hand_written_naming_forms(expression: str, bucket_name: str) -> list[str]:
    """Repo naming forms present in ``expression`` but absent from ``bucket_name``.

    The text-level half of the guard, **scoped to the S3 SEARCH expression**. The
    scoping is the whole point: the same substrings appear legitimately elsewhere
    in ``metrics.py`` — eight times as ``FunctionName``/``StateMachineArn``/
    ``TableName`` filters, and in the fence's own prose — so a file-wide search
    cannot tell a correct literal from the defect (`LGR-b46a5bb-4`).
    """
    return [
        form
        for form in (SNAKE_NAMING_FORM, KEBAB_NAMING_FORM, DEPLOY_BUCKET_PREFIX)
        if form in expression and form not in bucket_name
    ]


# ═══════════════════════════════════════════════════════════════════════════
# 1. R1.2 — datapoints in the window mean a positive total
# ═══════════════════════════════════════════════════════════════════════════


class TestDatapointsPresent:
    """``R1.2``: a deployed bucket reporting ``StandardStorage`` yields a total > 0.

    This is the whole of Group A. The clause is trivially satisfiable by a
    correct filter and was **unreachable** under the old one — no bucket name
    contains the token it searched for, so no series matched, so there were never
    any datapoints to reduce.
    """

    def test_datapoints_present_report_a_positive_total(self, monkeypatch):
        """``R1.2`` verbatim: at least one datapoint means a total greater than zero."""
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[141_557_760.0])

        assert run.outcome.value is not None, "a reported datapoint must not report Not_Established (R1.2)"
        assert run.outcome.value > 0, f"expected a total greater than zero (R1.2), got {run.outcome.value!r}"
        assert run.outcome.determined_from == metrics_mod.SOURCE_S3_BUCKET_SIZE, (
            f"a measurement carries the bare source token (R4.2), got {run.outcome.determined_from!r}"
        )

    def test_a_reported_datapoint_is_never_published_as_zero(self, monkeypatch):
        """``R1.2`` stated negatively — and this is the half that was the defect.

        ~135 MB of real storage was published as ``0`` for as long as the filter
        searched for an identifier no bucket carries. Asserted separately from
        the clause above because ``0`` is the one wrong answer this requirement
        was written about, and because ``> 0`` alone would be satisfied by a
        function that had merely stopped reading anything.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[141_557_760.0])

        assert run.outcome.value != 0, (
            "a bucket reporting a StandardStorage datapoint must not be published as zero bytes (R1.2)"
        )

    def test_the_total_is_a_reported_daily_sum(self, monkeypatch):
        """The answer comes from the series, not from arithmetic over the window.

        Two daily sums are offered. The reported total must be **one of them**,
        never their sum — a two-day window summed twice would double-count
        storage that never grew. *Which* one is task 4.1's choice
        (``ScanBy="TimestampDescending"``, most recent first) and is deliberately
        not pinned: ``R1.2`` asks for "a total", not for a selection rule.
        """
        daily_sums = [141_557_760.0, 100_000_000.0]
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=daily_sums)

        assert run.outcome.value in {int(v) for v in daily_sums}, (
            f"expected one of the reported daily sums {daily_sums!r}, got {run.outcome.value!r}"
        )
        assert run.outcome.value != int(sum(daily_sums)), (
            "the total must be one window's storage, not an aggregate across days"
        )

    def test_the_read_asks_for_standard_storage_bucket_size(self, monkeypatch):
        """``R1.2``'s premise: the datapoint kind the requirement names is what is read.

        ``BucketSizeBytes`` / ``StandardStorage`` are CloudWatch's own vocabulary
        (``R1.1``), not bucket identifiers, so pinning them constrains nothing
        task 4.3 may change.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[141_557_760.0])

        expression = run.search_expression
        assert "BucketSizeBytes" in expression, f"the storage read must query BucketSizeBytes, got {expression!r}"
        assert "StandardStorage" in expression, f"the storage read must select StandardStorage, got {expression!r}"


# ═══════════════════════════════════════════════════════════════════════════
# 2. R1.3 — an established nothing is not a byte count of zero
# ═══════════════════════════════════════════════════════════════════════════


class TestNoDatapoints:
    """``R1.3``: no datapoints for any deployed bucket reports Not_Established.

    ``BucketSizeBytes`` is a daily metric, so a young or unmeasured deployment
    has **no series** rather than an empty one. Reporting ``0`` there asserts the
    buckets were measured and found empty — a claim the read cannot support, and
    the collapse `LGR-4854b3d-1` (**template / ratified**) forbids: a three-state
    value is never collapsed to one of its ends.
    """

    def test_an_empty_series_reports_not_established(self, monkeypatch):
        """``R1.3``: a successful query with no datapoints is an established nothing."""
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[])

        assert_absent(run.outcome)

    def test_an_empty_series_is_never_reported_as_zero_bytes(self, monkeypatch):
        """``R1.3`` verbatim: "rather than a byte count of zero"."""
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[])

        assert run.outcome.value != 0, "no datapoints must not be reported as zero bytes (R1.3)"

    def test_a_response_without_the_reduction_series_reports_not_established(self, monkeypatch):
        """A successful call that established no total is the same fact as an empty one.

        The reduction series being missing from the response altogether is a
        different code path from its ``Values`` being empty, and ``R1.3`` does
        not distinguish them — both are "the query returned no datapoints".
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, response=_cw_response([141.0], series_id="s3_raw_only"))

        assert_absent(run.outcome)

    def test_the_empty_case_still_issued_the_query(self, monkeypatch):
        """An established nothing means the read happened and found nothing.

        This is what separates ``R1.3``'s ``_absent`` from the ``_absent`` of a
        filter that could never be built: same token, and only the presence of a
        query tells them apart. Without this assertion a function that stopped
        querying entirely would satisfy every other clause in this class.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, values=[])

        assert len(run.queries) == 1, (
            f"R1.3's Not_Established must follow an actual query; {len(run.queries)} were issued"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. R1.4 — a failed read says so, and says it out loud
# ═══════════════════════════════════════════════════════════════════════════


class TestRaisingClient:
    """``R1.4``: a raising query reports Not_Established and logs at WARNING or above."""

    def test_a_raising_client_reports_not_established(self, monkeypatch):
        """``R1.4``: an unreadable source carries the ``_incomplete`` token.

        Distinct from ``R1.3``'s ``_absent`` on purpose — "we could not look" and
        "we looked and there is nothing" are different facts, and collapsing them
        is what `LGR-78ce0b7-8` (**template / ratified**) refuses in the same
        words for a missing versus an empty record.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, side_effect=RuntimeError("CloudWatch unavailable"))

        assert_incomplete(run.outcome)

    def test_a_raising_client_is_never_reported_as_zero_bytes(self, monkeypatch):
        """A failed read must not publish the strongest claim it could make.

        The pre-p39 shape of this defect across the handler was a failure
        returning the source's zero — which downstream reads as a measurement.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, side_effect=RuntimeError("CloudWatch unavailable"))

        assert run.outcome.value != 0, "a failed storage query must not be reported as zero bytes (R1.4)"

    def test_a_raising_client_is_recorded_at_warning(self, monkeypatch):
        """``R1.4``: the failure is recorded at WARNING level or above."""
        run = run_storage(monkeypatch, DEPLOY_BUCKET, side_effect=RuntimeError("CloudWatch unavailable"))

        assert_recorded_at_warning(run, "a failed S3 storage query")

    def test_a_raising_client_does_not_propagate(self, monkeypatch):
        """``R4.12``, reached through ``R1.4``: Not_Established is a return value.

        Implicit in every assertion above — they could not run otherwise — and
        stated once so the intent is not left to inference.
        """
        run = run_storage(monkeypatch, DEPLOY_BUCKET, side_effect=RuntimeError("CloudWatch unavailable"))

        assert isinstance(run.outcome, metrics_mod.MetricOutcome)


# ═══════════════════════════════════════════════════════════════════════════
# 4. No deploy-derived filter — absent, and no query at all
# ═══════════════════════════════════════════════════════════════════════════


class TestNoFilterCanBeBuilt:
    """Both arms task 4.1 introduced: ``PROCESSED_BUCKET`` unset, and a value not in the deploy form.

    They are the same fact — there is no identifier to build a filter from — and
    the requirement they serve is ``R1.5``: a fallback to a literal prefix here
    would be the hand-written identifier ``R1.5`` exists to remove, and would
    re-create Group A the first time the deploy prefix changed.

    Neither arm is reachable in a deployed Lambda (``compute-stack.ts:645``
    always injects the variable), so what they guard is a misconfigured or
    half-deployed function — which is exactly when a fabricated number is most
    expensive.
    """

    @pytest.mark.parametrize("bucket_name", UNUSABLE_BUCKET_VALUES)
    def test_an_unusable_bucket_name_reports_not_established(self, monkeypatch, bucket_name):
        """No identifier means no series was established."""
        run = run_storage(monkeypatch, bucket_name)

        assert_absent(run.outcome)

    @pytest.mark.parametrize("bucket_name", UNUSABLE_BUCKET_VALUES)
    def test_an_unusable_bucket_name_issues_no_query(self, monkeypatch, bucket_name):
        """``R1.5``: never a guessed prefix — so there is nothing to ask CloudWatch.

        The load-bearing assertion of this class. A function that fell back on a
        hardcoded ``adi-`` would still return a plausible outcome here; only the
        absence of a query shows that no prefix was invented.
        """
        run = run_storage(monkeypatch, bucket_name)

        assert run.queries == [], (
            f"PROCESSED_BUCKET={bucket_name!r} yields no filter, so no CloudWatch query may be issued; "
            f"got {run.queries!r}\n\n{REMEDIATION_R15}"
        )

    @pytest.mark.parametrize("bucket_name", UNUSABLE_BUCKET_VALUES)
    def test_an_unusable_bucket_name_is_recorded_at_warning(self, monkeypatch, bucket_name):
        """A tile silently reporting nothing because of a missing variable is a silent failure."""
        run = run_storage(monkeypatch, bucket_name)

        assert_recorded_at_warning(run, f"an unusable {metrics_mod._DEPLOY_BUCKET_ENV} of {bucket_name!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. R1.5 — the structural guard, as a data dependency
# ═══════════════════════════════════════════════════════════════════════════


def _renamed_expressions(monkeypatch) -> tuple[str, str]:
    """The S3 SEARCH expression under each of the two synthetic deploy names."""
    return (
        run_storage(monkeypatch, RENAMED_BUCKET_A, values=[1.0]).search_expression,
        run_storage(monkeypatch, RENAMED_BUCKET_B, values=[1.0]).search_expression,
    )


class TestFilterIsDerivedFromTheDeployIdentifier:
    """``R1.5``, asserted as a data dependency rather than as file text.

    See this module's docstring for why a source-text search over ``metrics.py``
    cannot express this requirement, and ``metrics.py:1310-1331`` for task 4.1's
    statement of the same point.
    """

    def test_the_search_expression_changes_with_the_deploy_bucket_name(self, monkeypatch):
        """``R1.5`` verbatim: a rename of the deployment prefix cannot silently empty the series.

        Two deploy-form names sharing no segment must produce two different
        expressions. A filter that ignored the variable — a hardcoded prefix, or
        the old hand-written token — produces the same expression for both, which
        is precisely the failure mode: the buckets are renamed, the filter is
        not, and the series goes quietly empty.
        """
        expression_a, expression_b = _renamed_expressions(monkeypatch)

        assert expression_a != expression_b, (
            f"the S3 SEARCH expression is identical for {RENAMED_BUCKET_A!r} and {RENAMED_BUCKET_B!r}, so it is "
            f"not derived from {metrics_mod._DEPLOY_BUCKET_ENV} at all:\n  {expression_a!r}\n\n{REMEDIATION_R15}"
        )

    def test_every_deploy_derived_term_is_a_segment_of_the_bucket_name(self, monkeypatch):
        """``R1.5``: the filter is built of the identifier's own parts and nothing else.

        The structural vocabulary — namespace, metric name, statistic, period —
        is whatever the two expressions share, so nothing about the query's shape
        is pinned. Every remaining term must be an alphanumeric run of the bucket
        name it was built from, which holds whether task 4.3 sends AND-ed
        segments or one concatenated string.
        """
        expression_a, expression_b = _renamed_expressions(monkeypatch)
        structural = structural_tokens(expression_a, expression_b)

        for expression, bucket_name in ((expression_a, RENAMED_BUCKET_A), (expression_b, RENAMED_BUCKET_B)):
            undeclared = undeclared_terms(expression, bucket_name, structural)
            assert not undeclared, (
                f"the S3 SEARCH expression for {bucket_name!r} carries {sorted(undeclared)!r}, which is neither "
                f"structural CloudWatch vocabulary nor a segment of that bucket name:\n  {expression!r}\n\n"
                f"{REMEDIATION_R15}"
            )

    def test_the_filter_contributes_at_least_one_deploy_derived_term(self, monkeypatch):
        """Non-vacuity: the previous test passes trivially against no filter at all.

        An expression with no bucket-derived term would match every bucket in the
        account, which is not this deployment's storage — and would satisfy
        "every derived term is a segment" by having none.
        """
        expression_a, expression_b = _renamed_expressions(monkeypatch)
        structural = structural_tokens(expression_a, expression_b)

        derived = expression_tokens(expression_a) - structural
        assert derived, (
            f"the S3 SEARCH expression for {RENAMED_BUCKET_A!r} carries no term derived from it, so it does not "
            f"filter to this deployment's buckets:\n  {expression_a!r}\n\n{REMEDIATION_R15}"
        )
        assert derived <= bucket_name_segments(RENAMED_BUCKET_A), (
            f"derived terms {sorted(derived)!r} are not all segments of {RENAMED_BUCKET_A!r}"
        )
        assert expression_tokens(expression_b) - structural, (
            f"the S3 SEARCH expression for {RENAMED_BUCKET_B!r} carries no term derived from it:\n  {expression_b!r}"
        )

    def test_the_search_expression_carries_no_repo_naming_form_of_its_own(self, monkeypatch):
        """The text-level half of the guard, **scoped to the expression, not the file**.

        Under a bucket name carrying neither of this repo's naming forms nor the
        deploy prefix, the expression must carry none of them either. This is the
        one guard that catches a hand-written constant: a literal appears in
        *both* expressions, so the intersection above classifies it as
        structural — see
        :func:`test_the_historical_defect_is_caught_by_the_naming_form_guard_only`.

        Scoping it to the expression is what keeps the eight correct
        ``agentic_data_ingestion`` literals elsewhere in this module — and the
        fence's own prose — out of its way (`LGR-b46a5bb-4`).
        """
        for expression, bucket_name in zip(
            _renamed_expressions(monkeypatch), (RENAMED_BUCKET_A, RENAMED_BUCKET_B), strict=True
        ):
            hand_written = hand_written_naming_forms(expression, bucket_name)
            assert not hand_written, (
                f"the S3 SEARCH expression built from {bucket_name!r} hand-writes {hand_written!r}, which that "
                f"bucket name does not contain:\n  {expression!r}\n\n{REMEDIATION_R15}"
            )

    def test_the_guard_reads_the_real_s3_storage_search(self, monkeypatch):
        """A guard pointed at the wrong query, or at nothing, passes for free."""
        run = run_storage(monkeypatch, RENAMED_BUCKET_A, values=[1.0])

        expression = run.search_expression
        assert "AWS/S3" in expression, f"expected the AWS/S3 namespace, got {expression!r}"
        assert "BucketSizeBytes" in expression, f"expected the BucketSizeBytes metric, got {expression!r}"


# ═══════════════════════════════════════════════════════════════════════════
# Negative controls — a guard that cannot fail is worthless
# ═══════════════════════════════════════════════════════════════════════════

#: The pre-4.1 expression, verbatim in shape: a bare hand-written snake-form
#: token where the deploy-derived filter belongs. This is Group A.
HISTORICAL_DEFECT_EXPRESSION = (
    'SEARCH(\'{AWS/S3,BucketName,StorageType} MetricName="BucketSizeBytes" '
    "agentic_data_ingestion StorageType=\"StandardStorage\"', 'Average', 86400)"
)


def _correct_expression(bucket_name: str) -> str:
    """A filter derived from ``bucket_name``, in the shape task 4.1 ships."""
    segments = [seg for seg in bucket_name.split("-") if seg]
    bucket_filter = " ".join([segments[0], *segments[2:]])
    return (
        'SEARCH(\'{AWS/S3,BucketName,StorageType} MetricName="BucketSizeBytes" '
        f"{bucket_filter} StorageType=\"StandardStorage\"', 'Average', 86400)"
    )


def test_the_historical_defect_is_caught_by_the_naming_form_guard_only():
    """Why both guards exist, demonstrated against the expression that was the defect.

    The pre-4.1 expression hand-writes ``agentic_data_ingestion``. Because it is
    a *constant*, it appears under every ``PROCESSED_BUCKET`` value, so the
    intersection that identifies structural vocabulary absorbs it and
    :func:`undeclared_terms` reports nothing. The naming-form check catches it.

    Asserting both halves — that one detector misses it and the other catches it
    — is what keeps a later reader from deleting either as redundant.
    """
    structural = structural_tokens(HISTORICAL_DEFECT_EXPRESSION, HISTORICAL_DEFECT_EXPRESSION)

    assert undeclared_terms(HISTORICAL_DEFECT_EXPRESSION, RENAMED_BUCKET_A, structural) == set(), (
        "the intersection detector is expected to be blind to a hand-written constant — if it is not, this "
        "control's explanation of why the naming-form guard exists is stale"
    )
    assert hand_written_naming_forms(HISTORICAL_DEFECT_EXPRESSION, RENAMED_BUCKET_A) == [SNAKE_NAMING_FORM], (
        "the naming-form guard must catch the hand-written snake-form token that was Group A"
    )


def test_the_naming_form_guard_permits_a_form_the_bucket_name_carries():
    """A live deployment's own prefix is not a hand-written literal.

    The real buckets *are* named ``adi-…``, so an expression built from
    ``adi-processed-files-…`` legitimately contains ``adi``. The guard compares
    against the bucket name rather than forbidding the substring outright —
    otherwise it would fail on a correct filter in the account it was written
    for.
    """
    expression = _correct_expression(DEPLOY_BUCKET)

    assert hand_written_naming_forms(expression, DEPLOY_BUCKET) == []


def test_the_naming_form_guard_ignores_the_modules_prose_and_its_other_searches():
    """The scoping is load-bearing: a file-wide substring guard would fail on correct code.

    ``metrics.py`` contains the snake naming form as the *correct* identifier in
    the ``AWS/Lambda``, ``AWS/States`` and ``AWS/DynamoDB`` SEARCHes, and quotes
    it again in the fence at ``:1310-1331`` that explains the defect. So a
    file-wide check flags the module, while the same check scoped to the S3
    expression does not — which is the whole reason this guard reads the
    expression the module *sends* rather than the text it *is*
    (`LGR-b46a5bb-4`, **template / ratified**).

    If the first assertion ever fails, the naming form has left this module
    entirely and the scoping rationale wants revisiting — not the guard.
    """
    assert SNAKE_NAMING_FORM in METRICS_SOURCE, (
        f"{SNAKE_NAMING_FORM!r} no longer appears anywhere in {METRICS_PATH.name}; a file-wide substring guard "
        "would now be harmless, so re-read this file's docstring before relying on the scoped form"
    )
    assert hand_written_naming_forms(_correct_expression(RENAMED_BUCKET_A), RENAMED_BUCKET_A) == []


@pytest.mark.parametrize(
    ("expression", "bucket_name", "expected"),
    [
        # A purpose segment left in — one bucket, not the deployment.
        (
            "SEARCH('{AWS/S3,BucketName} MetricName=\"BucketSizeBytes\" zzz widgets 999999999999', 'Average', 86400)",
            "zzz-widgets-999999999999-eu-north-1",
            set(),
        ),
        # A term from somewhere other than this bucket name — a second env var,
        # a stale constant, a copied literal.
        (
            "SEARCH('{AWS/S3,BucketName} MetricName=\"BucketSizeBytes\" zzz sourcefiles', 'Average', 86400)",
            "zzz-widgets-999999999999-eu-north-1",
            {"sourcefiles"},
        ),
    ],
)
def test_undeclared_terms_flags_a_term_that_did_not_come_from_the_bucket_name(expression, bucket_name, expected):
    """The derived-term detector accepts segments and rejects anything else.

    The first case shows a term that *is* a segment passing even though it is a
    purpose segment — narrowing the filter to one bucket is a composition
    question task 4.3 owns, not an ``R1.5`` violation. The second shows a term
    from outside the identifier being caught.
    """
    structural = structural_tokens(_correct_expression(bucket_name), _correct_expression(RENAMED_BUCKET_B))

    assert undeclared_terms(expression, bucket_name, structural) == expected


def test_the_rename_guard_flags_an_expression_that_ignores_the_variable():
    """Guard 1's detector: a constant expression is identical across two names.

    Driven directly rather than through the module, so the assertion in
    :func:`TestFilterIsDerivedFromTheDeployIdentifier.test_the_search_expression_changes_with_the_deploy_bucket_name`
    is known to be capable of failing.
    """
    assert HISTORICAL_DEFECT_EXPRESSION == HISTORICAL_DEFECT_EXPRESSION
    assert _correct_expression(RENAMED_BUCKET_A) != _correct_expression(RENAMED_BUCKET_B)


def test_the_guard_goes_red_against_a_hand_written_filter(monkeypatch):
    """End-to-end non-vacuity: the guard fails when the real function is fed a constant.

    The controls above drive the detectors on synthetic strings. This one drives
    the **real** ``_get_s3_storage_bytes``, with only its token source replaced by
    one that ignores the bucket name — which is Group A, expressed as the
    smallest possible regression. Both module-level guards are shown to fail on
    it: the expression no longer changes with the identifier, and it carries a
    naming form the bucket name does not.

    Patching ``_deploy_bucket_search_tokens`` rather than editing the module keeps
    two concurrent tasks' hands off ``metrics.py`` — and it is the honest seam
    anyway, since task 4.3 owns that function's return value and nothing else.
    """
    with patch.object(metrics_mod, "_deploy_bucket_search_tokens", lambda _name: [SNAKE_NAMING_FORM]):
        expression_a = run_storage(monkeypatch, RENAMED_BUCKET_A, values=[1.0]).search_expression
        expression_b = run_storage(monkeypatch, RENAMED_BUCKET_B, values=[1.0]).search_expression

    assert expression_a == expression_b, (
        "a filter that ignores the bucket name should produce one expression for both names — if it does not, "
        "this control no longer demonstrates what the rename guard catches"
    )
    assert hand_written_naming_forms(expression_a, RENAMED_BUCKET_A) == [SNAKE_NAMING_FORM], (
        "the naming-form guard must fail against a hand-written filter reaching the real SEARCH expression"
    )


def test_expression_tokens_is_indifferent_to_term_composition():
    """The reason task 4.3 can change the token composition without touching this file.

    AND-ed single segments and one concatenated string tokenize identically, so
    every guard above is stated over a set the composition cannot change.
    """
    assert expression_tokens("zzz 999999999999 eu north 1") == expression_tokens("zzz 999999999999-eu-north-1")
    assert expression_tokens("zzz-999999999999-eu-north-1") == bucket_name_segments("zzz-999999999999-eu-north-1")
