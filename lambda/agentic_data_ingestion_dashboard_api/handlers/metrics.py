#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudWatch metrics handler for dashboard API.

Queries real CloudWatch metrics across AWS/Lambda, AWS/States, AWS/DynamoDB,
and AgenticDataIngestion/Pipeline namespaces. Results are cached in DynamoDB with a
5-minute TTL to avoid hitting CloudWatch API limits.

Data-source notes (verified against the live account; see P22 metrics-accuracy
work):

* Stage counts come from the ``agentic_data_ingestion_state`` table (the orchestration
  source of truth that records every stage), windowed to the requested period.
  CloudWatch ``Pipeline.StageExecution`` is published with a per-PipelineId
  dimension set, so it cannot be aggregated by ``{StageName, Status}`` alone.
* Stage durations come from CloudWatch ``Pipeline.StageDuration`` as an
  invocation-weighted mean (summed ``Sum`` over summed ``SampleCount``);
  ``pipeline_run`` is derived from state ``started_at`` / ``completed_at``. A
  stage with no datapoint reports ``avg_time_ms`` ``None`` (rendered "N/A").
* Agent stage duration is the same weighted mean over the four agent stages
  (D1); the dispatcher Lambdas' ``Duration`` is never read for it.
* Rows loaded (the ``lines_ingested_24h`` key) is the sum of ``rows_written``
  on the Glue job's ``load_metrics`` state rows in the window, counted where the
  write was confirmed.
* The cost estimate is the sum of ``GlueJob.RunCostEstimate`` over the selected
  window — one datapoint per Glue run that reached a terminal state, published
  by ``job_completion`` (p39 R2.1). It is a DPU-minutes derivation with a
  10-minute per-run minimum, stated in the payload's ``monthly_cost_basis``;
  nothing in this system measures spend.
"""

import contextlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import boto3

from dynamo_typing import item_int, item_str

from .common import create_response, handle_error, logger, state_table

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch.type_defs import MetricDataQueryTypeDef

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
cloudwatch_client = boto3.client("cloudwatch")

# Reuse the state table for caching with a 'metrics_cache#' key prefix
_cache_table = state_table

# ---------------------------------------------------------------------------
# Period mapping
# ---------------------------------------------------------------------------
PERIOD_MAP: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# CloudWatch data-point period (seconds) per requested window
CW_PERIOD: dict[str, int] = {
    "1h": 60,
    "6h": 300,
    "24h": 300,
    "7d": 3600,
    "30d": 3600,
}

CACHE_TTL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Not_Established — the outcome shape (p39 R4.1-R4.3)
# ---------------------------------------------------------------------------
# Every read in this module can answer one of three different things, and they
# are three different facts:
#
#   1. a measurement                          -> "<source>"
#   2. we looked and there is nothing         -> "<source>_absent"
#   3. we could not look                      -> "<source>_incomplete"
#
# Collapsing any pair of them into one value is the defect p39 exists to
# remove: a missing baseline reported as `stable`, zero terminal executions
# reported as 100% success, a disabled metric reported as 0, a failed
# CloudWatch query reported as `healthy`. R4.1 says report the absence; R4.2
# says say which kind it is; R4.3 says name the fact that was unavailable.
#
# The shape is the ratified `determined_from` pattern from
# `shared/quality_verdict.py:101-105` (LGR-eee8ac8-6, template / ratified),
# where the same three-way split lets a reader "tell 'we looked and there is
# no DQ' from 'we could not look'". The base token names the SOURCE, not the
# tile, so several fields derived from one read share one provenance answer.
# `LGR-b2f79ef-14` and `LGR-b2f79ef-8` (both template / ratified) are the same
# rule applied elsewhere — "could not check" and "checked and passed" must not
# collapse — and `LGR-44d601e-1` (template / ratified) is why the answer is a
# named token rather than a tolerant sentinel like "" or 0: a sentinel has to
# be re-distinguished from absence at every reader.
#
# DELIBERATE DIVERGENCE FROM quality_verdict, and it stays: that module spells
# the third value `<source>_unreadable` (`FROM_UNREADABLE_CREATE_CODE_ROW`,
# quality_verdict.py:105). R4.2 specifies `<source>_incomplete`, and this
# follows the requirement's word. The two vocabularies describe different
# subjects — one state row's provenance versus a metric read's — so neither is
# renamed to match the other. Do not "unify" them: that would edit a ratified
# module (3 + 2 + 2 call sites, all in `create_quality_checks`, plus
# `tests/unit/shared/test_quality_verdict.py`) for cosmetic agreement.
#
# WHY THIS LIVES HERE AND NOT IN `shared/`: `LGR-aa15a1b-1` (template /
# ratified) moves a definition to `shared/` when TWO writers would otherwise
# keep copies free to diverge silently — there, two read-modify-writes against
# one Glue table, where the narrower key list wins and drops fields with no
# error. This handler is the only writer of a metric outcome, so a
# `shared/metric_outcome.py` would invert that record's own test. Do not
# create it.

# One token per read this handler performs.
SOURCE_SFN_EXECUTIONS = "sfn_execution_metrics"  # AWS/States Executions* (R5)
SOURCE_GLUE_RUN_COST = "glue_run_cost_metric"  # GlueJob.RunCostEstimate (R2)
SOURCE_STAGE_DURATION = "stage_duration_metric"  # Pipeline.StageDuration (R6, R7.4)
SOURCE_AGENT_LAMBDA_ERRORS = "agent_lambda_error_metrics"  # AWS/Lambda Invocations + Errors (R4.8)
SOURCE_S3_BUCKET_SIZE = "s3_bucket_size"  # AWS/S3 BucketSizeBytes (R1)
SOURCE_S3_REQUEST_METRICS = "s3_request_metrics"  # never enabled on the buckets (R4.7)
SOURCE_LOAD_METRICS_ROWS = "load_metrics_rows"  # the Glue job's (run_id, "load_metrics") rows (p37 C1)
SOURCE_FAILURE_RECORDS = "pipeline_failure_records"  # RecordFailure sentinel state rows (p59)

_ABSENT_SUFFIX = "_absent"
_INCOMPLETE_SUFFIX = "_incomplete"

# Trends carry Not_Established *in band* rather than through a provenance
# token: `up` / `down` / `stable` is already a closed vocabulary, so a fourth
# member says "no baseline" without a parallel field. R4.4's distinction is
# between an absent baseline and a measured baseline of zero, not between
# kinds of unavailability, so the three-value split above does not apply.
TREND_NOT_ESTABLISHED = "not_established"


class MetricOutcome(NamedTuple):
    """A metric read's value and the provenance of that value (R4.1, R4.2).

    ``value`` is ``None`` exactly when no measurement was obtained, so a
    caller assembling the payload reads absence off the value and the *reason*
    off ``determined_from`` — never the other way round, and never from a
    zero. Annotated ``float | None`` because that is what the payload carries;
    an ``int`` source (byte counts, row counts) passes unchanged.

    It is a ``NamedTuple`` so that the design's stated per-helper return shape
    — ``(value | None, determined_from)`` — is what call sites unpack, while
    the two positions have names at the sites that keep the whole thing::

        total, provenance = _get_s3_storage_bytes(end)   # tuple unpacking
        outcome.value is None                            # attribute access

    Construct it through :meth:`measured`, :meth:`absent` or
    :meth:`incomplete` rather than by hand, so the three tokens are spelled in
    one place and a new source cannot invent a fourth suffix.
    """

    value: float | None
    determined_from: str

    @classmethod
    def measured(cls, value: float, source: str) -> "MetricOutcome":
        """A real measurement from ``source``. Zero is a measurement."""
        return cls(value, source)

    @classmethod
    def absent(cls, source: str) -> "MetricOutcome":
        """``source`` was queried successfully and holds nothing for the window."""
        return cls(None, f"{source}{_ABSENT_SUFFIX}")

    @classmethod
    def incomplete(cls, source: str) -> "MetricOutcome":
        """``source`` could not be read — a raising client, or a missing input.

        The caller still logs at WARNING or above; this only records that the
        absence is an unknown rather than an established nothing (R1.4, R4.8).
        """
        return cls(None, f"{source}{_INCOMPLETE_SUFFIX}")


class _PublishedFact(NamedTuple):
    """One published fact: the value, where it came from, and what measures it.

    Three statements about one number, held in one place so they cannot drift
    apart. The registry of these in `_build_comprehensive_metrics` is what the
    payload's `determined_from`, `missing` and `measurement` maps are DERIVED
    from — none of the three is written out by hand (R4.2, R4.3, R7.6).

    ``value``
        The object actually published. Register the SAME local the payload
        publishes, never a re-derivation of it: `missing` is derived from
        ``value is None``, so a registry that disagreed with the payload would
        name a field as unavailable while publishing a number for it — the
        self-contradiction `shared/load_outcome.py` refuses in the same words
        ("reporting it for a count that was never established would contradict
        ``missing`` inside the same record").

    ``determined_from``
        The R4.2 token — ``<source>``, ``<source>_absent`` or
        ``<source>_incomplete``, always built through :class:`MetricOutcome`.
        Suffixed exactly when ``value`` is ``None``, which is what makes
        `missing` and `determined_from` two views of one fact rather than two
        lists to keep in step. ``None`` here means this fact has no provenance
        to publish yet — see the registry for the three cases and why.

    ``measurement``
        R7.6's answer to "what does this tile measure", set for the six
        performance tiles and ``None`` for a fact published elsewhere in the
        payload. `performance["measurement"]`'s key set is therefore exactly
        the performance tile key set by construction rather than by convention
        (Property 7), and a tile's label cannot drift from its source because
        the source is named in the payload the label renders.
    """

    value: Any
    determined_from: str | None = None
    measurement: str | None = None


# ---------------------------------------------------------------------------
# Stage mapping
# ---------------------------------------------------------------------------
# Frontend stage key -> agentic_data_ingestion_state step_name PARTITIONS to
# query (orchestration source of truth).
#
# P28 R8.2 (task 14.2) — dual-partition reconciliation: the step Lambdas now
# write canonical snake_case step names, but rows written before the rename
# persist with the legacy PascalCase names. The step_name-timestamp-index
# partitions by step_name, so a time window spanning the rename deploy has a
# stage's rows split across TWO partitions. Querying both (two bounded
# Queries, never a Scan — R17.1 holds) is the honest choice; querying only
# the canonical partition would silently undercount pre-rename runs.
# schema_extract / schema_mapping were always snake_case → one partition.
_STAGE_STEP_NAMES: dict[str, tuple[str, ...]] = {
    "schema_extract": ("schema_extract",),
    "schema_mapping": ("schema_mapping",),
    "code_generation": ("create_code", "CodeGeneration"),
    "infrastructure": ("create_infrastructure", "CreateInfrastructure"),
    "quality_checks": ("create_quality_checks", "CreateQualityChecks"),
    "pipeline_run": ("run_pipeline", "RunPipeline"),
}
# Frontend stage key -> CloudWatch `StageName` dimension value on
# `Pipeline.StageDuration`.
#
# R6.2 — ALL SIX frontend stage keys are mapped. The key set here is the same
# six the page renders (`MetricsDashboardPage.tsx:118-125` STAGE_KEYS) and the
# same six `_STAGE_STEP_NAMES` above carries. It used to map two, so even a
# fully-emitting producer left four stages at `Avg Time: N/A` — the consumer
# half of p39 C5.
#
# `StageName` values, and which of them a producer actually writes (established
# by grep over `lambda/**` for `record_stage_execution`, 9 Lambda dirs):
#
#   SchemaExtract         emitting
#   SchemaMapping         emitting
#   CodeGeneration        emitting since the flush fix (p39 8.1) reached it
#   CreateInfrastructure  emitting since the same fix
#   CreateQualityChecks   emit site added by p39 10.1
#   RunPipeline           NOTHING EMITS IT, DELIBERATELY — see below
#
# WHY `pipeline_run` IS MAPPED WITH NO PRODUCER, AND WHY THAT IS NOT A BUG TO
# FIX: `run_pipeline` holds no `PipelineMetrics` reference and p39 adds none.
# The mapping is here for R6.2's completeness — the map's key set IS the
# frontend stage key set, and a hole in it is the defect R6.2 closes. Its
# CloudWatch arm therefore yields nothing on every call, and the state-row
# derivation from `started_at`/`completed_at` in `_get_stage_metrics` stays
# its source; `run_pipeline` is the only stage whose rows carry those bounds.
# Do NOT "repair" this by adding a `RunPipeline` emit: the state rows already
# measure the Glue run end to end, and a second source would be two numbers
# for one span. Do NOT delete the key either — that reopens the 2-of-6 hole.
# The precedence rule in `_get_stage_metrics` is what makes both true at once.
_CW_STAGE_NAMES: dict[str, str] = {
    "schema_extract": "SchemaExtract",
    "schema_mapping": "SchemaMapping",
    "code_generation": "CodeGeneration",
    "infrastructure": "CreateInfrastructure",
    "quality_checks": "CreateQualityChecks",
    "pipeline_run": "RunPipeline",
}
_SUCCESS_STATUSES = {"success", "completed"}
_ERROR_STATUSES = {"error", "failed"}

# ---------------------------------------------------------------------------
# The RecordFailure sentinel, and why the stage panel has to read it (p59)
# ---------------------------------------------------------------------------
# `LGR-9e76249-6` (template / ratified) is what makes this a defect rather than
# a preference: the `(run_id, "pipeline_failure")` row is **the only carrier of
# a run's terminal FAILED status whenever the failing step never got to write a
# row of its own** — a Lambda timeout, `States.Timeout` parked at an approval
# gate, `GlueJobFailed`. Every task state Catches to RecordFailure for exactly
# those cases.
#
# So the six stage partitions cannot see most failures, and the two halves of
# the metrics page disagreed by construction: `ExecutionsFailed` counted the
# run, and no stage's `error_count` did. The observed split was 26 failed
# executions above a Stage Metrics panel showing ~1 error across all six.
#
# The attribution is available on the row: `failed_step` names the originating
# step (`record_failure/lambda_function.py:443`, R1.4), alongside
# `failed_step_source` (`step_failure` = authoritative, `inferred` = best
# effort), `error_type` and a message.
_FAILURE_RECORD_STEP_NAME = "pipeline_failure"

# 2026-10-10 — a run a reviewer REJECTED at an approval gate is not a failure.
# Both reject paths fail the gate's task token with this Error name
# (`approval_handler/lambda_function.py`, `handlers/approvals.py`), the gate's
# Catch routes to RecordFailure, and RecordFailure stores it verbatim as the
# row's `error_name` (`record_failure/lambda_function.py`
# `APPROVAL_REJECTED_ERROR_NAME`). The execution still ends FAILED, so
# `ExecutionsFailed` counts it; these rows are how the page takes it back out.
#
# Only this name. An approval nobody answered (`States.Timeout`,
# `ApprovalTimeout`) stays a failure: nobody decided, and an approval queue that
# nobody watches is an operational problem the page should show.
_APPROVAL_REJECTED_ERROR_NAME = "ApprovalRejected"


def _is_approval_rejection(row: Mapping[str, Any]) -> bool:
    """Whether a ``pipeline_failure`` row records a reviewer's rejection."""
    return item_str(row, "error_name") == _APPROVAL_REJECTED_ERROR_NAME


def _build_canonical_step_to_stage() -> dict[str, str]:
    """Canonical step name -> frontend stage key, DERIVED from _STAGE_STEP_NAMES.

    Not a second mapping. `_STAGE_STEP_NAMES` already says which state
    ``step_name`` partitions belong to which frontend stage, including the
    legacy PascalCase spellings, and `step_names.from_any` collapses both
    spellings of a stage onto one canonical member — so ``create_code`` and
    ``CodeGeneration`` both arrive here as ``create_code`` and produce ONE
    entry. A stage added to `_STAGE_STEP_NAMES` is attributable with no change
    here, and the two maps cannot come to disagree about which partitions a
    stage owns.

    `from_any` raises by contract on a name it does not know, and every value
    in `_STAGE_STEP_NAMES` is a canonical name or a cataloged legacy variant
    today. The suppress is there so that a future partition spelling nobody
    taught `step_names` about degrades to "this stage is unattributable" rather
    than taking the whole module's import down — the same non-raising posture
    `LGR-9e76249-6` chose for the classifier it governs.
    """
    from step_names import from_any

    mapping: dict[str, str] = {}
    for frontend_key, steps in _STAGE_STEP_NAMES.items():
        for step in steps:
            with contextlib.suppress(ValueError):
                mapping[from_any(step).value] = frontend_key
    return mapping


#: Resolved once at import — neither input varies per request.
_CANONICAL_STEP_TO_STAGE: dict[str, str] = _build_canonical_step_to_stage()


def _stage_for_failed_step(failed_step: str) -> str | None:
    """The frontend stage key a ``failed_step`` belongs to, or ``None``.

    ``None`` is the common case and it is NOT a failure to be swallowed: only
    six of the thirteen `Canonical_Step_Names` have a stage row on this page, so
    a run that died at ``file_validation``, ``schema_compare``, ``test_code``,
    ``create_glue_pipeline`` or at either approval gate names a step no stage
    renders. Those failures are counted and named under
    ``stage_failure_attribution.unattributed_by_step`` rather than dropped —
    dropping them silently is the defect this function exists to close, one
    level down.

    An unparseable or empty name reaches the same place, keyed by its raw value
    (or ``"unknown"``, which is what RecordFailure itself writes when it cannot
    attribute — `record_failure/lambda_function.py:206`).
    """
    from step_names import from_any

    if not failed_step:
        return None
    try:
        canonical = from_any(failed_step)
    except ValueError:
        return None
    return _CANONICAL_STEP_TO_STAGE.get(canonical.value)


class _FailureAttribution(NamedTuple):
    """What the ``pipeline_failure`` partition said, and how it landed on stages.

    ``records``
        Failure records in the window, NOT counting rejections (below). A
        measured zero is a real answer: the partition was read and nothing
        failed outside a stage row.
    ``rejected_by_step``
        Rows whose ``error_name`` is `_APPROVAL_REJECTED_ERROR_NAME`, by the
        gate (``failed_step``) the reviewer rejected at. Kept out of every
        other field: a rejection is a decision, so it is neither a stage error
        nor an unattributed failure, and `failed_pipelines` has these taken out
        of it before it reaches the residual.
    ``attributed``
        Records whose ``failed_step`` names one of the six frontend stages.
    ``counted``
        Of those, the ones that added a NEW error to a stage — i.e. survived the
        per-``run_id`` dedupe below.
    ``already_counted``
        Of those, the ones whose stage had already recorded an error row for the
        same ``run_id``. Counting these too is the double-count this dedupe
        prevents: a step can write its own ``status="error"`` row AND have
        RecordFailure fire for the same run.
    ``unattributed_by_step``
        Failure records naming a step no stage renders, by step name. The
        breakdown rather than a bare total, because "5 timed out at the code
        approval gate" and "5 failed somewhere" are different operational facts.
    ``outcome``
        Three-state provenance for the read (`LGR-4854b3d-1`, template /
        ratified). ``measured`` on a successful Query — including one that
        returned nothing — and ``incomplete`` when the Query raised. There is
        deliberately no ``absent`` arm: an empty partition IS the measurement
        "no run failed outside a stage row", the same reasoning that keeps
        `failed_pipelines` a plain number. ``value is None`` therefore means
        exactly "we could not look", and the published fact is ``None`` there
        rather than a dict of zeros that would read as "no failures".
    """

    records: int
    attributed: int
    counted: int
    already_counted: int
    unattributed_by_step: dict[str, int]
    rejected_by_step: dict[str, int]
    outcome: MetricOutcome

    @property
    def unattributed(self) -> int:
        """Failure records that name no frontend stage."""
        return sum(self.unattributed_by_step.values())

    @property
    def rejected(self) -> int:
        """Runs a reviewer rejected at an approval gate."""
        return sum(self.rejected_by_step.values())


#: The answer when the failure partition could not be read. NOT zeros dressed
#: as a measurement: `outcome` carries `_incomplete`, so `_published_attribution`
#: returns `None`, `missing` names the fact and the page says the stage error
#: accounting is incomplete instead of implying nothing failed.
_FAILURE_ATTRIBUTION_UNREADABLE = _FailureAttribution(
    records=0,
    attributed=0,
    counted=0,
    already_counted=0,
    unattributed_by_step={},
    rejected_by_step={},
    outcome=MetricOutcome.incomplete(SOURCE_FAILURE_RECORDS),
)


class _StageMetricsRead(NamedTuple):
    """The stage panel's two published halves, from ONE pass over the state table.

    Returned together rather than through two reads, because the dedupe that
    keeps `stages` honest and the numbers that make `attribution` checkable are
    the same arithmetic: which ``run_id``s a stage already counted as errors is
    known only inside the counting loop.

    ``stages`` keeps its exact shape — the six `_STAGE_STEP_NAMES` keys, three
    fields each. Nothing is added to it. A seventh pseudo-stage for
    unattributable failures was rejected: the key set is pinned to the six the
    page renders on both sides (`_CW_STAGE_NAMES`, `MetricsDashboardPage.tsx`
    `STAGE_KEYS`, and `test_metrics_stage_keys_contract.py` reads both from
    source), and a fake stage would have no duration, no success count and no
    detail view — it would look like a pipeline stage and be a bucket. The
    unattributable failures are a fact ABOUT the panel, so they are published as
    one.
    """

    stages: dict[str, dict[str, Any]]
    attribution: _FailureAttribution


def _published_attribution(
    attribution: _FailureAttribution,
    failed_executions: int | None,
) -> dict[str, Any] | None:
    """The ``stage_failure_attribution`` fact, or ``None`` if it was not read.

    ONE place builds this dict, so the reconciliation the page prints and the
    numbers the stage panel was adjusted by cannot drift. ``failed_executions``
    is `performance.failed_pipelines`' own local — one fact under two names, the
    way `infrastructure.agentcore.response_time_ms` mirrors
    `performance.agent_latency_ms` — and ``None`` when the AWS/States read did
    not complete, because a `0` there is that read's failure and not a count.
    Since 2026-10-10 that local has approval rejections taken out, and
    ``failure_records`` excludes them too, so the residual compares like with
    like; ``rejected_at_approval`` is the count taken out of both.

    ``failed_executions_without_a_record`` is the residual, published SIGNED and
    unclamped. It is what the page says when the two halves cannot be made to
    add up, and both directions are real:

    * positive — failed executions with no failure record. An execution can
      reach FAILED without RecordFailure writing anything (its own put_item is
      caught and logged, `record_failure/lambda_function.py:466-471`), and
      `ExecutionsAborted` / `ExecutionsTimedOut` never route through it at all.
    * negative — more failure records than failed executions. The two are read
      over the same wall-clock window from different systems: AWS/States by
      datapoint timestamp, the records by their own ``timestamp`` attribute, so
      a run straddling a bound is counted by one and not the other.

    Clamping either direction to zero would be the collapse `LGR-4854b3d-1`
    forbids, in the one field whose whole job is to admit that the arithmetic
    does not close.
    """
    if attribution.outcome.value is None:
        return None
    return {
        "failure_records": attribution.records,
        "attributed_to_stage": attribution.attributed,
        "counted_into_stage_errors": attribution.counted,
        "already_counted_by_stage_row": attribution.already_counted,
        "unattributed": attribution.unattributed,
        "unattributed_by_step": dict(sorted(attribution.unattributed_by_step.items())),
        "rejected_at_approval": attribution.rejected,
        "rejected_by_step": dict(sorted(attribution.rejected_by_step.items())),
        "failed_executions": failed_executions,
        "failed_executions_without_a_record": (
            None if failed_executions is None else failed_executions - attribution.records
        ),
    }


# ---------------------------------------------------------------------------
# Terminal execution outcomes (R5)
# ---------------------------------------------------------------------------
# Payload key -> AWS/States metric name, for every execution outcome that is
# TERMINAL.
#
# ONE named set, read by BOTH sides of the success rate: `_get_sfn_metrics`
# turns each entry into a SEARCH + SUM query pair, and `_success_rate` sums the
# same entries to build its denominator. R5.5 — "where the deployed state
# machine emits an additional terminal execution metric, include that metric in
# the denominator" — is therefore a ONE-LINE addition here, and it is not
# possible to add an outcome to the query set without adding it to the
# denominator, or the reverse. That coupling is the point of the constant; do
# not re-spell either list inline.
#
# `ExecutionsStarted` is deliberately NOT a member (R5.2): a started execution
# may still be running, so it is not an outcome at all. It is queried and
# reported separately as `started`, outside this mapping. On the verified 7-day
# window the four terminal counts were 12 / 14 / 10 / 0 against 36 started —
# so a denominator built from `started` would be counting runs that had not
# finished, and the pre-p39 `succeeded + failed` denominator dropped the 10
# aborted, 28 percent of everything that ran.
_TERMINAL_EXECUTION_METRICS: dict[str, str] = {
    "succeeded": "ExecutionsSucceeded",
    "failed": "ExecutionsFailed",
    "aborted": "ExecutionsAborted",
    "timed_out": "ExecutionsTimedOut",
}

# State-table GSI (data-stack.ts): partition `step_name`, sort `timestamp`.
# P28 R17.1: metrics read paths query this index with time-bounded key
# conditions instead of scanning the whole state table. Step rows always
# carry both attributes; rows without a timestamp (e.g. the metrics_cache
# row) never enter the sparse index — same effect as the old client-side
# "unparseable timestamp → skip" behavior.
_STEP_TIMESTAMP_INDEX = "step_name-timestamp-index"

# p39 R8.1/R8.2, task 6.2: this module no longer reads the executions table at
# all. The `StatusIndex` GSI constant and the closed execution-status set that
# used to live here existed only for the deleted flat-rate cost fallback, which
# enumerated every status — including `RUNNING` — with one bounded Query each.
# That enumeration was this handler's ONLY contact with p38's phantom rows, so
# deleting it removes the exposure rather than filtering it: p38 owns those
# rows, and R8.2 forbids a second filter here. `handlers/health.py:59-73` is the
# remaining StatusIndex reader and carries p38's own filter; it is out of p39's
# change set (constraint 10) and this module makes no claim about it.


def _query_state_by_step(
    step_name: str,
    start: datetime,
    end: datetime,
    **extra_kwargs: Any,
) -> list[dict[str, Any]]:
    """All state rows for one step within [start, end], via the step_name GSI.

    Query, not Scan (P28 R17.1): the key condition bounds the read to one
    step partition and the requested time window. ISO-8601 timestamps sort
    lexicographically, so BETWEEN on the string sort key matches the window;
    callers keep their client-side ``_parse_iso`` checks as the authoritative
    filter for mixed-suffix edge cases.
    """
    from boto3.dynamodb.conditions import Key

    query_kwargs: dict[str, Any] = {
        "IndexName": _STEP_TIMESTAMP_INDEX,
        "KeyConditionExpression": (
            Key("step_name").eq(step_name) & Key("timestamp").between(start.isoformat(), end.isoformat())
        ),
        **extra_kwargs,
    }
    items: list[dict[str, Any]] = []
    resp = state_table.query(**query_kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = state_table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **query_kwargs)
        items.extend(resp.get("Items", []))
    return items


# ---------------------------------------------------------------------------
# The two agent read sets — one per question (D1, ratified)
# ---------------------------------------------------------------------------
# One list used to serve BOTH the agent-latency tile and the agent error rate.
# D1 splits them, because the two reads ask different things about the same
# four agents:
#
#   how long did the agent WORK?   -> `Pipeline.StageDuration`, per stage
#   did the invocation FAIL?       -> `AWS/Lambda` Invocations + Errors
#
# The dispatcher Lambda's `Duration` cannot answer the first. The AgentCore
# entry point pops the task token, hands the work to `dispatch_background_step`
# and returns `build_dispatch_ack` immediately
# (`agentcore/create_code/main.py:76-86`, `sfn_task_token.py:242-251`), so that
# `Duration` measures the DISPATCH ACK — a verified ~1 s against a workload
# measured in minutes. `AWS/Bedrock-AgentCore` `Latency` on
# `InvokeAgentRuntime` measures the same ack (36 invocations, mean 817.1 ms
# over 7 days), which is why re-sourcing to it was rejected too: it moves one
# wrong number to another wrong number with a better provenance story.
#
# This list answers the SECOND question correctly, which is why it survives its
# old reader rather than being deleted with it: a dispatcher error IS a genuine
# agent-invocation error, so `_get_agent_error_rate` KEEPS this list and its
# `AWS/Lambda` source (D1, ratified; `R4.8` asks only that a failed query say
# so). Deleting `_AGENT_FUNCTIONS` outright would break that read.
_AGENT_FUNCTIONS = [
    "agentic_data_ingestion_create_code",
    "agentic_data_ingestion_test_code",
    "agentic_data_ingestion_create_glue_pipeline",
    "agentic_data_ingestion_create_infrastructure",
]

# The four agent stages the latency tile averages `Pipeline.StageDuration`
# over (D1, ratified). These are `StageName` dimension values on
# `AgenticDataIngestion/Pipeline`, timed from `_metrics_start` to completion
# INSIDE the agent container (`create_code:882-889`) — the agent's own work
# time, which is the question the tile sits under.
#
# FIXED AT FOUR, which is the form the user ratified: the query set is these
# four whether or not each has published anything. The alternative considered
# and rejected was a set built from the stages that reported, which makes the
# denominator a property of what happened to be emitting.
#
# DELIBERATELY NOT DERIVED FROM `_CW_STAGE_NAMES`, and not addable to it. That
# map is the six FRONTEND stage keys (R6.2, pinned by task 11.2's test) and it
# answers a different question: `SchemaExtract`, `SchemaMapping`,
# `CreateQualityChecks` and `RunPipeline` are pipeline stages no agent
# performs, while `CodeTesting` and `CreateGluePipeline` — two of the four
# below — have no frontend stage row at all. The two sets overlap on
# `CodeGeneration` and `CreateInfrastructure` and that overlap is a
# coincidence of naming, not a shared definition. Do not add keys to that map
# to make this convenient, and do not build this tuple from it.
_AGENT_STAGE_NAMES: tuple[str, ...] = (
    "CodeGeneration",
    "CodeTesting",
    "CreateGluePipeline",
    "CreateInfrastructure",
)


# ---------------------------------------------------------------------------
# Stage-duration reads: an invocation-weighted mean, never AVG(SEARCH(...))
# ---------------------------------------------------------------------------
# Both duration readers (`_stage_durations_cw`, `_get_agent_latency`) answer
# "how long did a stage take, on average, per invocation in this window". They
# used to compute it as AVG over a SEARCH of the `Average` statistic, and that
# divides by the wrong number.
#
# MEASURED, 2026-10-10, development deployment, 24h window. A SEARCH returns every series
# matching the brace that CloudWatch indexes (two weeks of history), not only
# the series with datapoints in the window. For `CodeGeneration` that was six
# series, one per pipeline that ever ran the stage, of which one had values
# (3024.7, 3001.8) and five were empty. `AVG(raw)` returned 504.1 and 500.3:
# each value divided by six, the empty series counted as zeros. Every stage card
# showed its real mean divided by its series count (Schema Extraction 38 s for
# runs of ~3.8 min; Code Generation 502 ms for 3.0 s), and the Avg Agent Stage
# Duration tile read 2 s for a 25.5 s invocation-weighted mean. The error grows
# with every pipeline the account has ever run.
#
# The fix is arithmetic CloudWatch cannot get wrong: SUM the `Sum` statistic and
# SUM the `SampleCount` statistic across the same SEARCH, then divide the window
# totals. An empty series adds nothing to either total, so it cannot dilute the
# result, and the mean is weighted by invocation rather than by 5-minute bucket.
# No datapoint anywhere (a zero count) is absent, never 0 — `LGR-4854b3d-1`.

#: The dimension-NAME set `record_stage_execution` publishes `Pipeline.StageDuration`
#: with. A SEARCH brace is an exact match on this set, so it must be the
#: publisher's exactly (task 13.3 / 13.5; pinned by the brace tests).
_STAGE_DURATION_BRACE = "{AgenticDataIngestion/Pipeline,PipelineId,StageName,Status,service}"


def _weighted_stage_duration_queries(id_prefix: str, stage_filter: str, period: int) -> list[dict[str, Any]]:
    """Four queries whose results `_weighted_stage_duration` divides.

    ``stage_filter`` is the SEARCH's StageName clause, e.g. ``StageName="SchemaExtract"``
    or ``StageName=("A" OR "B")``. The raw arrays are fetched with
    ``ReturnData: False``; only the two SUM reductions come back.
    """

    def search(statistic: str) -> str:
        return (
            f"SEARCH('{_STAGE_DURATION_BRACE} "
            f"MetricName=\"Pipeline.StageDuration\" {stage_filter}', '{statistic}', {period})"
        )

    return [
        {"Id": f"{id_prefix}_sum_raw", "Expression": search("Sum"), "ReturnData": False},
        {"Id": f"{id_prefix}_sum", "Expression": f"SUM({id_prefix}_sum_raw)", "Period": period},
        {"Id": f"{id_prefix}_n_raw", "Expression": search("SampleCount"), "ReturnData": False},
        {"Id": f"{id_prefix}_n", "Expression": f"SUM({id_prefix}_n_raw)", "Period": period},
    ]


def _weighted_stage_duration(results: Sequence[Mapping[str, Any]], id_prefix: str) -> float | None:
    """Window total duration ÷ window invocation count, or ``None`` with no invocations.

    Reads only the two SUM reductions named by ``id_prefix``. A missing
    reduction id carries no more information than an empty ``Values`` list, so
    both read as zero invocations, i.e. ``None``.
    """
    by_id = {series.get("Id"): series.get("Values", []) for series in results}
    count = float(sum(by_id.get(f"{id_prefix}_n", [])))
    if count <= 0:
        return None
    return float(sum(by_id.get(f"{id_prefix}_sum", []))) / count


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════


def get_comprehensive_metrics(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """Return comprehensive metrics from CloudWatch.

    Accepts an optional ``period`` query-string parameter (1h | 6h | 24h | 7d).
    """
    try:
        query_params: dict[str, Any] = {}
        if event:
            query_params = event.get("queryStringParameters") or {}
        period = query_params.get("period", "24h")
        if period not in PERIOD_MAP:
            period = "24h"

        # Check cache first
        cached = _get_cached_metrics(period)
        if cached is not None:
            return create_response(200, cached)

        delta = PERIOD_MAP[period]
        cw_period = CW_PERIOD[period]
        end_time = datetime.now(UTC)
        start_time = end_time - delta

        metrics = _build_comprehensive_metrics(start_time, end_time, cw_period, period)

        _cache_metrics(period, metrics)
        return create_response(200, metrics)

    except Exception as exc:
        return handle_error(exc, "Error fetching comprehensive metrics")


# ═══════════════════════════════════════════════════════════════════════════
# Aggregate builder
# ═══════════════════════════════════════════════════════════════════════════


def _build_comprehensive_metrics(
    start_time: datetime,
    end_time: datetime,
    cw_period: int,
    period_label: str,
) -> dict[str, Any]:
    """Assemble the full ComprehensiveMetrics payload matching the frontend interface.

    Every existing key keeps its name and its meaning. `performance` gains four
    additive keys, all of them DERIVED from the `_PublishedFact` registry below
    rather than written out beside the values they describe:

    * ``determined_from`` — field name -> R4.2 provenance token, three-valued.
    * ``missing`` — every fact that was unavailable (R4.3). Read off the
      assembled payload, so it cannot disagree with it.
    * ``measurement`` — what backs each performance tile (R7.6).
    * ``monthly_cost_basis`` / ``terminal_counts`` — R2.6 and R5.4.

    This function is TOTAL for an absent source (R4.10, R4.12): every helper it
    calls returns rather than raises on an absence, so the outer ``except`` in
    `get_comprehensive_metrics` stays what it always was — the last resort for
    a genuine fault — and a window in which nothing was measured returns 200
    with every field named in ``missing``.
    """

    delta = end_time - start_time
    prev_start = start_time - delta
    prev_end = start_time

    lambda_metrics = _get_lambda_metrics(start_time, end_time, cw_period)
    sfn_metrics = _get_sfn_metrics(start_time, end_time, cw_period)
    sfn_prev = _get_sfn_metrics(prev_start, prev_end, cw_period)
    dynamo_metrics = _get_dynamodb_metrics(start_time, end_time, cw_period)

    period_hours = delta.total_seconds() / 3600

    # --- lambda-derived ---
    total_invocations = lambda_metrics.get("invocations", 0)
    total_errors = lambda_metrics.get("errors", 0)
    error_rate = (total_errors / total_invocations * 100) if total_invocations > 0 else 0

    # --- stages ---
    # p59: the stage read returns TWO things from one pass — the six stage rows
    # the panel renders, and the reconciliation between them and
    # `failed_pipelines`. See `_StageMetricsRead` for why they arrive together.
    # Read here, ahead of the success rate, because since 2026-10-10 the same
    # pass also yields the approval-rejection count the rate and the Failed
    # Executions tile take out.
    stage_read = _get_stage_metrics(start_time, end_time, cw_period)
    stages = stage_read.stages
    # `None` when the failure records could not be read: then nothing is taken
    # out of either figure and `rejected_at_approval` is in `missing`.
    rejected_at_approval: int | None = (
        stage_read.attribution.rejected if stage_read.attribution.outcome.value is not None else None
    )

    # --- success rate (Step Functions), current + previous window for trend ---
    overall_success = _success_rate(sfn_metrics, rejected_at_approval)
    # The trend compares the two windows on ONE basis. If either window's
    # rejection count could not be read, both rates are compared as AWS/States
    # reports them; comparing an adjusted rate with an unadjusted one would
    # report a trend that is only the adjustment.
    prev_rejection_count = _count_approval_rejections(prev_start, prev_end).value
    prev_rejections = None if prev_rejection_count is None else int(prev_rejection_count)
    trend_adjusted = rejected_at_approval is not None and prev_rejections is not None
    success_for_trend = (
        overall_success if trend_adjusted or rejected_at_approval is None else _success_rate(sfn_metrics)
    )
    prev_success = _success_rate(sfn_prev, prev_rejections if trend_adjusted else None)
    sfn_failed = sfn_metrics.get("failed", 0) - _rejections_within_failed(sfn_metrics, rejected_at_approval)
    # R5.4: publish the per-status counts the denominator was built from, so a
    # reader can reconstruct the rate on screen. This is what makes the headline
    # moving from 36.4% to 33.3% checkable arithmetic (12 / (12+14+10+0)) rather
    # than a new defect. Built by iterating `_TERMINAL_EXECUTION_METRICS`, so a
    # metric added there appears here too and the published counts can never be
    # a subset of the denominator's terms.
    #
    # `None` — not a dict of zeros — when the read failed: there is nothing to
    # reconstruct from, and zeros would assert an empty window we did not see.
    sfn_read_failed = bool(sfn_metrics.get("read_failed"))
    terminal_counts: dict[str, Any] | None = (
        None if sfn_read_failed else {key: sfn_metrics.get(key, 0) for key in _TERMINAL_EXECUTION_METRICS}
    )
    # The counts' provenance is NOT `overall_success`'s. A window in which
    # nothing ran gives `_success_rate` an `absent` rate — no denominator — but
    # gives these four counts an honest measurement of zero, which is exactly
    # the arithmetic a reader needs to see that the rate is absent for want of
    # runs rather than for want of a read. They diverge only there; both read
    # `incomplete` when the query raised.
    terminal_counts_from = (
        MetricOutcome.incomplete(SOURCE_SFN_EXECUTIONS).determined_from if sfn_read_failed else SOURCE_SFN_EXECUTIONS
    )

    # --- rows loaded (state table, windowed), current + previous ---
    # `None` when no Glue run in the window wrote a `load_metrics` row. See
    # `_get_rows_loaded` for what is counted and why it replaced the
    # file-size estimate.
    lines_outcome = _get_rows_loaded(start_time, end_time)
    lines_prev = _get_rows_loaded(prev_start, prev_end)
    lines_ingested = lines_outcome.value
    # Rows per hour of the selected window (the per-second rate is sub-0.01 at
    # these volumes). A rate derived from an absent numerator is not a rate:
    # dividing "we do not know" by a wall clock does not produce a number of rows.
    processing_rate = round(lines_ingested / max(period_hours, 1), 2) if lines_ingested is not None else None

    # --- agent latency (the agent stages' own duration), current + previous ---
    # D1 (ratified): `Pipeline.StageDuration` averaged across the four agent
    # stages, never the dispatcher Lambdas' `Duration`. Both windows can now
    # answer Not_Established, and `R4.1` forbids standing the dispatcher figure
    # in for an absent one — the tile says nothing rather than saying ~1 s.
    agent_latency_outcome = _get_agent_latency(start_time, end_time, cw_period)
    agent_latency_prev = _get_agent_latency(prev_start, prev_end, cw_period)
    agent_latency_ms = agent_latency_outcome.value
    # R4.8: an unmeasured error rate is an unmeasured availability. Neither is
    # rounded through `max(0.0, ...)` — `100.0 - None` raises, and an absence
    # that reaches the outer `except` returns 500 for a window that simply had
    # no agent invocations (R4.12 forbids exactly that).
    agent_error_outcome = _get_agent_error_rate(start_time, end_time, cw_period)
    agentcore_availability = (
        round(max(0.0, 100.0 - agent_error_outcome.value), 1) if agent_error_outcome.value is not None else None
    )

    # --- estimated cost, current + previous window (R2.3, R2.5) ---
    # THE SAME TWO PAIRS every other windowed read above is given, which is the
    # whole of R2.3: the two windows are adjacent and disjoint because they are
    # the caller's, not the callee's. `_get_estimated_monthly_cost` used to take
    # only an `end` and anchor its own trailing 30 days from it, so these two
    # calls asked for windows overlapping by 29 of 30 days on a 24-hour period
    # and the trend below was arithmetic on two overlapping integrals of one
    # series. Nothing recomputes a window inside the function now.
    cost_outcome = _get_estimated_monthly_cost(start_time, end_time, cw_period)
    cost_prev = _get_estimated_monthly_cost(prev_start, prev_end, cw_period)
    monthly_cost = cost_outcome.value
    # R2.6's `basis` — the dict stating what was summed and over what window,
    # and R2.7's statement that nothing here measures spend. It comes back from
    # the read that established the figure rather than being assembled here, so
    # the bounds it names are the bounds actually queried and the two cannot
    # drift. `None` exactly when `monthly_cost` is: there is no basis for a
    # figure that was not established, and `missing` therefore names the two
    # together.
    monthly_cost_basis: dict[str, Any] | None = cost_outcome.basis

    # --- S3 requests per minute ---
    # R4.7: S3 request metrics were never enabled on the deployed buckets, so
    # there is no series to read and the `0` this used to publish was a
    # hand-written constant dressed as a measurement — a tile reporting a
    # perfectly idle bucket for a bucket nobody is measuring. Named as an
    # established nothing rather than a failed read: we know why it is empty.
    # Do NOT "fix" this by enabling request metrics or adding a query; that is
    # a deploy change and this requirement asks only that the absence be told.
    # Resolved here, above the payload, because the fact registry names it and
    # `missing` has to reach it; it is published under `infrastructure.s3`.
    s3_requests = MetricOutcome.absent(SOURCE_S3_REQUEST_METRICS)

    # --- S3 storage used ---
    # R1: read here, above the payload, rather than inline in the
    # `infrastructure.s3` literal, for the same reason as `requests_per_minute`
    # — the fact registry names it and `missing` has to reach it, and a call
    # made inline would have to be made twice or its provenance re-derived.
    storage_outcome = _get_s3_storage_bytes(end_time)

    # `failed_executions` is `sfn_failed`, or `None` when the AWS/States read did
    # not complete — `sfn_failed` is 0 in that case and a 0 there is the read's
    # failure, not a count. Passing it through would make the residual read as
    # "every failure record is unexplained", which is a different claim.
    stage_failure_attribution = _published_attribution(
        stage_read.attribution,
        None if sfn_read_failed else sfn_failed,
    )

    # Rounded ONCE, into locals, so the published number and the provenance
    # registry below cannot come to disagree about what was published. The
    # trends keep reading the unrounded values, as they always did.
    #
    # R4.6: `overall_success_rate` is `None` when no execution reached a
    # terminal outcome. Not 100.0, and not rounded — `round(None, 1)` raises,
    # which would send an absence to the outer `except` and return 500 for a
    # window that simply had no runs (R4.12 forbids exactly that).
    success_rate_reported = round(overall_success.value, 1) if overall_success.value is not None else None
    # D1 / R4.1: `None` when no agent stage published a duration in the window,
    # and not rounded — `round(None, 1)` raises, which would send an absence to
    # the outer `except` and return 500 for a window in which no agent ran (R4.12
    # forbids exactly that). Read TWICE from this one local: the performance tile
    # and `infrastructure.agentcore.response_time_ms` are the same fact.
    agent_latency_reported = round(agent_latency_ms, 1) if agent_latency_ms is not None else None
    # R2.4: `None` when no Glue run reached a terminal state in the window, and
    # not rounded — `round(None, 2)` raises, which would send an absence to the
    # outer `except` and return 500 for a window that simply had no runs (R4.12
    # forbids exactly that).
    monthly_cost_reported = round(monthly_cost, 2) if monthly_cost is not None else None

    # --- what every published fact was determined from (R4.2, R4.3, R7.6) ---
    # ONE registry, read three ways below. A fact is registered here once, with
    # its value, its provenance and — for a performance tile — the measurement
    # behind it; `determined_from`, `missing` and `measurement` are all derived
    # from it, so none of the three can fall out of step with the payload or
    # with each other.
    #
    # Names are the payload's own field names for anything in `performance`, and
    # a qualified name unique in the payload for a fact published under
    # `infrastructure` (`agentcore_availability`), so one flat list of names
    # addresses the whole payload.
    #
    # ONE FACT HAS NO `determined_from`, and it is a decision rather than an
    # omission:
    #
    # * `failed_pipelines` stays a plain number (task 3.5, contract
    #   `["number", "integer"]`): an `ExecutionsFailed` sum with no datapoints is
    #   a genuine zero, nothing failed. A failed READ is not a genuine zero, and
    #   that case is not lost from the payload — its two siblings from the same
    #   read, `overall_success_rate` and `terminal_counts`, both carry the
    #   `_incomplete` token that says the States read did not complete.
    #
    #   RE-CHECKED AT p59 AND IT STILL HOLDS, which is worth stating because p59
    #   is the change that made this number reconcilable against the stage panel
    #   and so had every reason to want it nullable. It does not: p59 changed
    #   nothing about where this figure comes from. It is still `ExecutionsFailed`
    #   (Sum), a sum with no datapoints is still nothing having failed, and the
    #   failed-read case is still carried by the same two siblings — now with a
    #   third, `stage_failure_attribution`, whose `failed_executions` member is
    #   `None` on exactly that condition. What p59 adds is a SECOND read (the
    #   `pipeline_failure` partition) with its own independent three-state
    #   provenance, which is why it is its own registry entry rather than an
    #   extension of this one: the two reads can fail separately and a reader has
    #   to be able to tell which did.
    #
    #   STILL HOLDS AFTER 2026-10-10, when approval rejections started being
    #   taken out of it. The figure is still a count that is 0 when nothing
    #   failed. When the rejection read fails nothing is taken out, so it falls
    #   back to the plain `ExecutionsFailed` sum, and that case is carried by
    #   `rejected_at_approval`, which is `None` with the `_incomplete` token.
    #
    # `monthly_cost` was the second until task 6.1 and `agent_latency_ms` the
    # third until task 15.1; both now carry the token their own read
    # establishes. `agent_latency_ms` could not before, and the reason is worth
    # keeping: behind a function that returned 0.0 for an empty window AND for a
    # raising query there was no way to tell a measured zero from an absence, so
    # any token would have been invented. Re-sourcing the read is what made the
    # provenance expressible, which is why D1 and R4.2 land in one task.
    facts: dict[str, _PublishedFact] = {
        # The measurement is the primary carrier of what this figure is and is
        # not. `_PublishedFact.measurement`'s own docstring is why: the label
        # cannot drift from its source because the source is named in the
        # payload the label renders.
        #
        # 2026-10-10: these two tiles moved from the `file_validation` size
        # estimate to the rows the Glue job reports writing. The estimate
        # divided file size by a fixed per-format guess, counted re-runs again,
        # credited runs that never loaded anything, and could say nothing at all
        # about a JDBC source, which has no file. The user chose to replace it
        # rather than show both. See `_get_rows_loaded`.
        "lines_ingested_24h": _PublishedFact(
            lines_ingested,
            lines_outcome.determined_from,
            (
                "rows_written on the Glue job's load_metrics rows in the selected window, counted only "
                "where the job confirmed the target write — every source type; runs that stopped before "
                "Glue, and runs from before the job recorded counts, contribute nothing"
            ),
        ),
        "processing_rate": _PublishedFact(
            processing_rate,
            # One read, two tiles. The rate IS the rows-loaded figure divided by
            # the window's wall clock, so it can be no better established than
            # its numerator and it carries the numerator's token — the token
            # names the SOURCE, not the tile.
            lines_outcome.determined_from,
            (
                "that same rows-loaded count ÷ wall-clock hours in the selected window — an average "
                "over the window, not the speed of any one run"
            ),
        ),
        "overall_success_rate": _PublishedFact(
            success_rate_reported,
            overall_success.determined_from,
            (
                "AWS/States ExecutionsSucceeded ÷ (Succeeded + Failed + Aborted + TimedOut) (Sum), with the "
                "runs rejected at approval taken out of Failed — a reviewer's rejection is not a failure. "
                "If the rejections could not be read, nothing is taken out"
            ),
        ),
        "failed_pipelines": _PublishedFact(
            sfn_failed,
            measurement=(
                "AWS/States ExecutionsFailed (Sum), less the runs rejected at approval — failed executions, "
                "not distinct pipelines. An approval nobody answered in time still counts as failed"
            ),
        ),
        # 2026-10-10 — the runs a reviewer rejected at an approval gate, taken
        # out of the two figures above. A tile, so it carries a `measurement`;
        # its token is the failure-record read's, because it is that read.
        "rejected_at_approval": _PublishedFact(
            rejected_at_approval,
            stage_read.attribution.outcome.determined_from,
            measurement=(
                "pipeline_failure records in the selected window whose error_name is ApprovalRejected — runs "
                "a reviewer rejected at an approval gate. Not counted in Failed Executions or the Success Rate"
            ),
        ),
        # R7.6 / D1: the measurement names the new source, so the tile's label
        # (task 18.1 relabels it "Avg Agent Stage Duration") and the series
        # behind it are read from one place and cannot drift apart.
        "agent_latency_ms": _PublishedFact(
            agent_latency_reported,
            agent_latency_outcome.determined_from,
            measurement="AgenticDataIngestion/Pipeline Pipeline.StageDuration, agent stages (Average)",
        ),
        "monthly_cost": _PublishedFact(
            monthly_cost_reported,
            cost_outcome.determined_from,
            measurement=(
                "AgenticDataIngestion/Pipeline GlueJob.RunCostEstimate (Sum over the selected window) "
                "— a Glue DPU-minutes estimate with a 10-minute per-run minimum, never measured spend"
            ),
        ),
        # Published in `performance` but not a tile, so no `measurement`.
        "terminal_counts": _PublishedFact(terminal_counts, terminal_counts_from),
        # p59 — the reconciliation between `failed_pipelines` and the Stage
        # Metrics panel. Not a tile, so no `measurement`: that map's key set is
        # the performance tile key set exactly (Property 7), and this is a
        # structured fact about the panel rather than a figure on one.
        #
        # It is registered here for the same reason every other fact is: so
        # `determined_from` and `missing` DERIVE from it. `None` when the failure
        # partition could not be read, which is what puts
        # `stage_failure_attribution` in `missing` carrying
        # `pipeline_failure_records_incomplete` — the page then says the stage
        # error accounting is incomplete instead of implying nothing failed.
        "stage_failure_attribution": _PublishedFact(
            stage_failure_attribution,
            stage_read.attribution.outcome.determined_from,
        ),
        # One read, two facts: the basis carries the SAME token as the figure it
        # annotates, because the token names the source and there is one source.
        "monthly_cost_basis": _PublishedFact(monthly_cost_basis, cost_outcome.determined_from),
        # Published under `infrastructure`, named here so R1.3, R4.7 and R4.8's
        # absences reach `missing` and carry their token. None of the three is a
        # performance tile, so none carries a `measurement` — that map's key set
        # is the performance tile key set exactly (Property 7).
        "agentcore_availability": _PublishedFact(agentcore_availability, agent_error_outcome.determined_from),
        "requests_per_minute": _PublishedFact(s3_requests.value, s3_requests.determined_from),
        "storage_used_bytes": _PublishedFact(storage_outcome.value, storage_outcome.determined_from),
    }

    performance: dict[str, Any] = {
        # `lines_ingested_24h`: the name is HISTORICAL on both counts and neither
        # is corrected here. The window is the CALLER's — `?period=` selects 24h,
        # 7d or 30d and the `_24h` suffix survived the other two being added, so a
        # 30-day request publishes a 30-day figure under a key that says 24 hours.
        # The figure is now rows the Glue job reports writing (see the
        # measurement string above). Renaming is a breaking wire change —
        # `test_metrics_response_contract.py` requires the key and
        # `dashboard-react/src/types.ts` declares it — so the meaning is carried
        # in the tile label and the measurement, not in the field name.
        "lines_ingested_24h": lines_ingested,
        "processing_rate": processing_rate,
        "overall_success_rate": success_rate_reported,
        "failed_pipelines": sfn_failed,
        "rejected_at_approval": rejected_at_approval,
        # The RAW AWS/States counts, unchanged by the rejection adjustment, so
        # a reader reconstructs the rate as succeeded ÷ (their sum − the
        # rejected count, capped at `failed`).
        "terminal_counts": terminal_counts,
        # p59 — what the stage panel could and could not account for. Published
        # in `performance` rather than inside `stages` because `stages`' key set
        # is the six the page renders and each entry's field set is exactly
        # `{success_count, error_count, avg_time_ms}`; see `_StageMetricsRead`.
        "stage_failure_attribution": stage_failure_attribution,
        "agent_latency_ms": agent_latency_reported,
        "monthly_cost": monthly_cost_reported,
        "monthly_cost_basis": monthly_cost_basis,  # R2.6, R2.7
        "measurement": {name: f.measurement for name, f in facts.items() if f.measurement is not None},
        "determined_from": {name: f.determined_from for name, f in facts.items() if f.determined_from is not None},
        "trends": {
            # ONE guard for all four (`_reported_trend`), where 3.3 and 3.4 left
            # a hand-written conditional per site as each source gained its
            # absent case. `cost` now answers `None` for a window with no
            # terminal Glue run, and R2.4's `not_established` for the cost trend
            # follows through this guard with NO new conditional — which is
            # exactly what task 3.4 built it for. `agent_latency` is the second
            # demonstration: task 15.1 re-sourced its read to an outcome that can
            # answer `None` on both sides, and its trend started answering
            # `not_established` with no conditional added here either.
            "ingestion": _reported_trend(lines_ingested, lines_prev.value),
            "success": _reported_trend(success_for_trend.value, prev_success.value),
            "agent_latency": _reported_trend(agent_latency_ms, agent_latency_prev.value),
            "cost": _reported_trend(monthly_cost, cost_prev.value),
        },
    }
    # R4.3 — `missing` names every fact that was unavailable, DERIVED from what
    # was just published rather than maintained beside it. A hand-written list
    # is a second copy of the same truth and the copy is what goes stale: a
    # source that gains an absent case, or a field that gains a null arm, would
    # leave a null in the payload and no name in `missing`. Here that is not
    # expressible — `set(missing)` is exactly the set of null field names
    # because it is computed FROM them (Property 1). The comprehension shape is
    # `shared/load_outcome.py`'s (p37), which derives its own `missing` the same
    # way.
    #
    # Two clauses, both derivations. The first reads `performance`'s own nulls
    # straight off the assembled dict, so a nullable key added to the payload
    # later is named whether or not anyone remembers this line; `measurement`,
    # `determined_from` and `trends` are dicts and strings, never `None`, so
    # they cannot enter it. The second adds the registered facts published
    # elsewhere in the payload — `agentcore_availability` (R4.8) and
    # `requests_per_minute` (R4.7) live under `infrastructure`. Performance
    # fields satisfy both clauses; the union makes that harmless, and means
    # neither clause alone can under-report.
    #
    # `missing` is inserted last so the scan above sees the finished section.
    performance["missing"] = sorted(
        {key for key, value in performance.items() if value is None}
        | {name for name, fact in facts.items() if fact.value is None}
    )

    # --- infrastructure section ---
    infrastructure = {
        "agentcore": {
            "availability": agentcore_availability,
            # D1: this mirrors `performance.agent_latency_ms` and therefore
            # inherits both the new source and its Not_Established case. It reads
            # the SAME rounded local rather than rounding again, so the two
            # published copies of one fact cannot disagree and neither needs a
            # null test of its own.
            #
            # NOT a second entry in the fact registry above, deliberately: it is
            # one fact under two names, and `missing` names each unavailable fact
            # ONCE (R4.3). Registering it again would list a single absence
            # twice, and `determined_from["agent_latency_ms"]` already carries
            # the reason for both copies.
            "response_time_ms": agent_latency_reported,
            # R4.8: the caller short-circuits here rather than
            # `_health_from_availability` growing an `unknown` branch. Absence
            # is decided once, at the site that knows the read failed, and the
            # health mapping keeps taking a percentage it can actually map
            # (`LGR-4854b3d-1`). `"unknown"` is already in the `HealthStatus`
            # union (`dashboard-react/src/types.ts:676`), so no type widens for
            # it.
            "status": (
                _health_from_availability(agentcore_availability) if agentcore_availability is not None else "unknown"
            ),
        },
        "dynamodb": {
            "read_capacity_per_hour": round(dynamo_metrics.get("read_capacity", 0) / max(period_hours, 1), 1),
            "write_capacity_per_hour": round(dynamo_metrics.get("write_capacity", 0) / max(period_hours, 1), 1),
            "status": _health_from_dynamo_consumed(
                dynamo_metrics.get("read_capacity", 0) / max(period_hours, 1),
                dynamo_metrics.get("write_capacity", 0) / max(period_hours, 1),
            ),
        },
        "s3": {
            # R1.2/R1.3 — the real deployed buckets, or Not_Established. Never a
            # byte count of zero standing in for a series that was not found.
            "storage_used_bytes": storage_outcome.value,
            "requests_per_minute": s3_requests.value,  # R4.7 — never enabled on the buckets
            "status": "healthy",
        },
        "lambda": {
            "invocations_per_minute": round(
                total_invocations / max((end_time - start_time).total_seconds() / 60, 1), 1
            ),
            "error_rate": round(error_rate, 1),
            "status": "healthy" if error_rate < 5 else ("warning" if error_rate < 15 else "critical"),
        },
    }

    return {
        "performance": performance,
        "stages": stages,
        "infrastructure": infrastructure,
        "period": period_label,
        "generated_at": end_time.isoformat(),
    }


def _rejections_within_failed(sfn_metrics: Mapping[str, Any], rejected: int | None) -> int:
    """How many of the window's `ExecutionsFailed` to take out as rejections.

    ``rejected`` is the window's approval-rejection count, or ``None`` when the
    failure records could not be read — then nothing is taken out and the
    figures stay what AWS/States says, rather than guessing.

    Capped at the failed count. The two are read from different systems over
    the same wall-clock window, so a rejected run on the boundary can be in the
    records and not in the metric; taking out more than failed would make a
    negative count. The signed residual in `_published_attribution` is where
    that mismatch is admitted, not here.

    ONE helper for both `failed_pipelines` and `_success_rate`'s denominator,
    so the tile and the rate can never take out different numbers.
    """
    if rejected is None or rejected <= 0:
        return 0
    return min(rejected, int(sfn_metrics.get("failed", 0)))


def _success_rate(sfn_metrics: Mapping[str, Any], rejected: int | None = None) -> MetricOutcome:
    """Succeeded as a percentage of every TERMINAL execution, or Not_Established.

    The denominator is the sum of `_TERMINAL_EXECUTION_METRICS` — succeeded,
    failed, aborted and timed out (R5.1, R5.3) — built by iterating that one
    named set rather than by naming the four counts here, so R5.5's "include an
    additional terminal metric" is an addition to the set and nothing else.
    `ExecutionsStarted` is excluded (R5.2); it is not in the set.

    LESS APPROVAL REJECTIONS (2026-10-10, amending R5.1). A run a reviewer
    rejected ends FAILED, so it is in `ExecutionsFailed`, but the gate did its
    job: counting it against the rate made the panel read 50% over a week with
    one real failure and three rejections. `_rejections_within_failed` takes
    ``rejected`` out of the failed term, and ``None`` (the records could not be
    read) takes nothing out. `performance.rejected_at_approval` publishes the
    count, so the rate stays reconstructible from the payload: succeeded ÷
    (sum of `terminal_counts` − that count).

    A denominator of zero answers Not_Established, never `100.0` (R4.6). Before
    p39 it answered `100.0`, so a window in which nothing ran was indistinguish-
    able from a window in which everything succeeded — a perfect record claimed
    on no evidence. `LGR-4854b3d-1` (template / ratified) is the fence: a
    three-state value is never collapsed to one of its ends, and the collapsing
    expression lives in exactly one place.

    The three provenance values (R4.2) map onto what the read actually knows:

    - a positive denominator -> ``measured``;
    - a successful read with nothing in the window -> ``absent`` ("we looked and
      there is nothing");
    - a raising CloudWatch query -> ``incomplete`` ("we could not look").

    The third depends on `_get_sfn_metrics` reporting `read_failed`, because its
    ``except`` arm returns zeros for every count. Without that flag a failed
    query is arithmetically identical to an empty window and would be published
    as ``absent`` — the collapse `LGR-b2f79ef-8` (template / ratified) rejected,
    where "no record" and "record with nothing in it" became one value.
    """
    if sfn_metrics.get("read_failed"):
        return MetricOutcome.incomplete(SOURCE_SFN_EXECUTIONS)
    succeeded = sfn_metrics.get("succeeded", 0)
    terminal_total = sum(sfn_metrics.get(key, 0) for key in _TERMINAL_EXECUTION_METRICS) - _rejections_within_failed(
        sfn_metrics, rejected
    )
    if terminal_total <= 0:
        return MetricOutcome.absent(SOURCE_SFN_EXECUTIONS)
    return MetricOutcome.measured(succeeded / terminal_total * 100, SOURCE_SFN_EXECUTIONS)


# ═══════════════════════════════════════════════════════════════════════════
# CloudWatch query helpers (standard namespaces)
# ═══════════════════════════════════════════════════════════════════════════


def _get_lambda_metrics(start: datetime, end: datetime, period: int) -> dict[str, Any]:
    """Query AWS/Lambda namespace for AgenticDataIngestion function metrics."""
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "invocations",
                    "Expression": "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Invocations\" FunctionName=agentic_data_ingestion', 'Sum', "
                    + str(period)
                    + ")",
                    "ReturnData": False,
                },
                {
                    "Id": "invocations_sum",
                    "Expression": "SUM(invocations)",
                    "Period": period,
                },
                {
                    "Id": "errors",
                    "Expression": "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Errors\" FunctionName=agentic_data_ingestion', 'Sum', "
                    + str(period)
                    + ")",
                    "ReturnData": False,
                },
                {
                    "Id": "errors_sum",
                    "Expression": "SUM(errors)",
                    "Period": period,
                },
                {
                    "Id": "duration",
                    "Expression": "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Duration\" FunctionName=agentic_data_ingestion', 'Average', "
                    + str(period)
                    + ")",
                    "ReturnData": False,
                },
                {
                    "Id": "duration_avg",
                    "Expression": "AVG(duration)",
                    "Period": period,
                },
            ],
            StartTime=start,
            EndTime=end,
        )
        parsed = _parse_metric_data(response)
        return {
            "invocations": parsed.get("invocations_sum", 0),
            "errors": parsed.get("errors_sum", 0),
            "avg_duration_ms": round(parsed.get("duration_avg", 0), 1),
            "p99_duration_ms": 0,  # p99 not available via SEARCH aggregation
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch Lambda metrics: {exc}")
        return {"invocations": 0, "errors": 0, "avg_duration_ms": 0, "p99_duration_ms": 0}


def _get_sfn_metrics(start: datetime, end: datetime, period: int) -> dict[str, Any]:
    """Query AWS/States namespace for AgenticDataIngestion state machine metrics.

    Returns one count per key of `_TERMINAL_EXECUTION_METRICS` (R5.1) plus
    `started` and `avg_duration_ms`. The terminal queries are GENERATED from
    that mapping, so adding an outcome to it adds both its SEARCH query here and
    its place in `_success_rate`'s denominator — R5.5 in one line.

    `read_failed` distinguishes a query that raised from a window that was
    genuinely empty. The ``except`` arm below has always returned zeros, which
    are arithmetically identical to an empty window; without the flag
    `_success_rate` would publish a failed read as an established nothing.
    """
    # One SEARCH (raw series, not returned) + one SUM (the reduction) per
    # terminal outcome. `Id`s are derived from the mapping keys, so the existing
    # `succeeded_sum` / `failed_sum` ids are unchanged and `aborted_sum` /
    # `timed_out_sum` join them.
    queries: list[dict[str, Any]] = [
        {
            "Id": "started",
            "Expression": "SEARCH('{AWS/States,StateMachineArn} MetricName=\"ExecutionsStarted\" agentic_data_ingestion', 'Sum', "
            + str(period)
            + ")",
            "ReturnData": False,
        },
        {
            "Id": "started_sum",
            "Expression": "SUM(started)",
            "Period": period,
        },
    ]
    for key, metric_name in _TERMINAL_EXECUTION_METRICS.items():
        queries.append(
            {
                "Id": key,
                "Expression": (
                    "SEARCH('{AWS/States,StateMachineArn} MetricName=\""
                    + metric_name
                    + "\" agentic_data_ingestion', 'Sum', "
                    + str(period)
                    + ")"
                ),
                "ReturnData": False,
            }
        )
        queries.append(
            {
                "Id": f"{key}_sum",
                "Expression": f"SUM({key})",
                "Period": period,
            }
        )
    queries.extend(
        [
            {
                "Id": "exec_time",
                "Expression": "SEARCH('{AWS/States,StateMachineArn} MetricName=\"ExecutionTime\" agentic_data_ingestion', 'Average', "
                + str(period)
                + ")",
                "ReturnData": False,
            },
            {
                "Id": "duration_avg",
                "Expression": "AVG(exec_time)",
                "Period": period,
            },
        ]
    )

    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=cast("Sequence[MetricDataQueryTypeDef]", queries),
            StartTime=start,
            EndTime=end,
        )
        parsed = _parse_metric_data(response)
        metrics: dict[str, Any] = {
            "started": parsed.get("started_sum", 0),
            "avg_duration_ms": round(parsed.get("duration_avg", 0), 1),
            "read_failed": False,
        }
        for key in _TERMINAL_EXECUTION_METRICS:
            metrics[key] = parsed.get(f"{key}_sum", 0)
        return metrics
    except Exception as exc:
        logger.warning(f"Failed to fetch SFN metrics: {exc}")
        failed_read: dict[str, Any] = {"started": 0, "avg_duration_ms": 0, "read_failed": True}
        for key in _TERMINAL_EXECUTION_METRICS:
            failed_read[key] = 0
        return failed_read


def _get_dynamodb_metrics(start: datetime, end: datetime, period: int) -> dict[str, Any]:
    """Query AWS/DynamoDB namespace for AgenticDataIngestion table metrics."""
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "reads",
                    "Expression": "SEARCH('{AWS/DynamoDB,TableName} MetricName=\"ConsumedReadCapacityUnits\" agentic_data_ingestion', 'Sum', "
                    + str(period)
                    + ")",
                    "ReturnData": False,
                },
                {
                    "Id": "read_capacity",
                    "Expression": "SUM(reads)",
                    "Period": period,
                },
                {
                    "Id": "writes",
                    "Expression": "SEARCH('{AWS/DynamoDB,TableName} MetricName=\"ConsumedWriteCapacityUnits\" agentic_data_ingestion', 'Sum', "
                    + str(period)
                    + ")",
                    "ReturnData": False,
                },
                {
                    "Id": "write_capacity",
                    "Expression": "SUM(writes)",
                    "Period": period,
                },
            ],
            StartTime=start,
            EndTime=end,
        )
        parsed = _parse_metric_data(response)
        return {
            "read_capacity": round(parsed.get("read_capacity", 0), 1),
            "write_capacity": round(parsed.get("write_capacity", 0), 1),
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch DynamoDB metrics: {exc}")
        return {"read_capacity": 0, "write_capacity": 0}


# ═══════════════════════════════════════════════════════════════════════════
# AgenticDataIngestion custom metric / state helpers
# ═══════════════════════════════════════════════════════════════════════════


def _get_stage_metrics(start: datetime, end: datetime, period: int) -> _StageMetricsRead:
    """Per-stage counts (windowed) + durations, matching the StageMetrics interface.

    Counts: ``agentic_data_ingestion_state`` records within [start, end] (every
    stage), PLUS the RecordFailure sentinel rows attributed to the stage their
    ``failed_step`` names (p59 — see `_FAILURE_RECORD_STEP_NAME`). Without the
    second half a run that died at an approval gate or in Glue incremented
    `ExecutionsFailed` and contributed nothing to any stage's error count, so the
    page reported 26 failures above a panel showing ~1.

    Durations follow one precedence rule for all six stages (R6.3, R6.4): the
    CloudWatch ``Pipeline.StageDuration`` average when present, else the
    state-row ``started_at``/``completed_at`` span where a stage has one (only
    ``pipeline_run`` does), else Not_Established as ``None``. See the block
    comment at the rule itself for why the ordering is what it is and why
    ``pipeline_run`` is mapped to a ``StageName`` nothing emits.

    Returns both halves (`_StageMetricsRead`) because the dedupe and the
    published reconciliation are the same arithmetic.
    """
    step_to_fe = {step: fe for fe, steps in _STAGE_STEP_NAMES.items() for step in steps}
    counts: dict[str, dict[str, int]] = {fe: {"success": 0, "error": 0} for fe in _STAGE_STEP_NAMES}
    # Per stage, the run_ids that already contributed an error from a step row.
    # This is the dedupe key: a run can have BOTH a step row with
    # `status="error"` AND a `pipeline_failure` row — the step recorded its own
    # failure and RecordFailure also fired — and counting both would inflate the
    # stage. `run_id` is the state table's partition key but a GSI Query with a
    # ProjectionExpression returns only the attributes named, so it is projected
    # explicitly below; without it the dedupe would silently never match.
    stage_error_run_ids: dict[str, set[str]] = {fe: set() for fe in _STAGE_STEP_NAMES}
    run_durations: list[float] = []

    try:
        # One bounded Query per stage step-name PARTITION on the step_name GSI
        # instead of a full-table scan (P28 R17.1); renamed stages query both
        # their canonical and legacy partitions (P28 R8.2 — see
        # _STAGE_STEP_NAMES).
        #
        # AMENDED (p59). This comment used to end: "Failure_Record rows
        # (step_name "pipeline_failure") are naturally outside the queried
        # partitions, so they can't skew stage counts." The mechanism was right
        # and the conclusion was backwards. Those rows are outside these
        # partitions, and that is precisely why the panel could not see most
        # failures: `LGR-9e76249-6` (template / ratified) records that the
        # sentinel is the ONLY carrier of a run's terminal FAILED status when the
        # failing step never wrote a row of its own. Excluding it was not
        # protecting the counts from noise, it was withholding the signal.
        #
        # What is true now: the sentinel partition is read separately, below,
        # and each row is attributed to the stage its `failed_step` names. It
        # cannot skew a stage's counts because it is deduped by `run_id` against
        # the error rows collected here, not because it is unread.
        items: list[dict[str, Any]] = []
        for steps in _STAGE_STEP_NAMES.values():
            for step in steps:
                items.extend(
                    _query_state_by_step(
                        step,
                        start,
                        end,
                        ProjectionExpression="run_id,step_name,#s,#ts,started_at,completed_at",
                        ExpressionAttributeNames={"#s": "status", "#ts": "timestamp"},
                    )
                )

        for it in items:
            ts = _parse_iso(item_str(it, "timestamp"))
            if ts is None or ts < start or ts > end:
                continue
            fe = step_to_fe.get(item_str(it, "step_name"))
            if not fe:
                continue
            status = item_str(it, "status").lower()
            if status in _SUCCESS_STATUSES:
                counts[fe]["success"] += 1
            elif status in _ERROR_STATUSES:
                counts[fe]["error"] += 1
                run_id = item_str(it, "run_id")
                if run_id:
                    stage_error_run_ids[fe].add(run_id)
            if fe == "pipeline_run":
                s_at = _parse_iso(item_str(it, "started_at"))
                c_at = _parse_iso(item_str(it, "completed_at"))
                if s_at and c_at and c_at >= s_at:
                    run_durations.append((c_at - s_at).total_seconds() * 1000)
    except Exception as exc:
        logger.warning(f"Stage metrics state scan failed: {exc}")

    # ── THE FAILURES NO STAGE ROW RECORDED (p59) ────────────────────────────
    #
    # Its OWN try/except, not folded into the block above: the two reads fail
    # independently, and a failed stage read must not be reported as a failed
    # failure-record read or the reverse. The stage read's silence on failure is
    # pre-existing behaviour and is left alone here; this read's failure is
    # published, three-state, through `attribution.outcome`.
    attribution = _attribute_failure_records(start, end, counts, stage_error_run_ids)

    # ── THE DURATION PRECEDENCE RULE (R6.3, R6.4) ───────────────────────────
    #
    #   1. the CloudWatch `Pipeline.StageDuration` average, when present;
    #   2. else the state-row derivation, for the stages that have one;
    #   3. else Not_Established — `None`, never a zero.
    #
    # Stated as a rule rather than wired per stage. This block used to be a
    # three-key dict literal naming `schema_extract`, `schema_mapping` and
    # `pipeline_run`, which made the source of a stage's duration a property of
    # this literal instead of a property of the data: the three unnamed stages
    # could not report a duration even once their producer emitted one. With
    # `_CW_STAGE_NAMES` now covering all six keys (R6.2), the rule below is
    # what turns a newly-emitting producer into a number on the page with no
    # further change here.
    #
    # Ordering CloudWatch first is deliberate: it measures the stage's own work
    # inside the executing process, whereas a state-row span measures whatever
    # the writer chose to bracket. Only ONE stage has both arms available, and
    # only one arm ever answers for it in practice — `pipeline_run` maps to
    # `RunPipeline`, which nothing emits, so its CloudWatch arm is always empty
    # and clause 2 is always what answers. The rule is written to hold anyway
    # rather than special-casing that stage, so a future `RunPipeline` producer
    # does not silently lose to the DynamoDB span.
    #
    # Clause 3 is R6.4: `avg_time_ms` stays `None` for a stage with no
    # measurement from either source. `STAGE_METRICS_SCHEMA`'s `avg_time_ms` is
    # already `["number", "null"]`, so Not_Established needs no contract
    # change, and the frontend renders it "N/A" (`formatAvgTime`). A zero would
    # claim the stage ran instantly.
    cw_durations = _stage_durations_cw(start, end, period)
    # Clause 2's only source today: `run_pipeline` is the one stage whose state
    # rows carry `started_at`/`completed_at` (collected in the loop above).
    state_row_durations: dict[str, float | None] = {
        "pipeline_run": round(sum(run_durations) / len(run_durations), 1) if run_durations else None,
    }
    durations: dict[str, float | None] = {}
    for fe in _STAGE_STEP_NAMES:
        cw_value = cw_durations.get(fe)
        if cw_value is not None:
            durations[fe] = cw_value  # 1. CloudWatch
        else:
            durations[fe] = state_row_durations.get(fe)  # 2. state row, else 3. None

    stages: dict[str, dict[str, Any]] = {}
    for fe in _STAGE_STEP_NAMES:
        stages[fe] = {
            "success_count": counts[fe]["success"],
            "error_count": counts[fe]["error"],
            "avg_time_ms": durations.get(fe),  # None -> "N/A" on the frontend
        }
    return _StageMetricsRead(stages=stages, attribution=attribution)


def _attribute_failure_records(
    start: datetime,
    end: datetime,
    counts: dict[str, dict[str, int]],
    stage_error_run_ids: dict[str, set[str]],
) -> _FailureAttribution:
    """Read the ``pipeline_failure`` partition and fold it into ``counts`` (p59).

    Mutates ``counts`` and ``stage_error_run_ids`` in place — it is the second
    half of one accumulation, called from `_get_stage_metrics` with that
    function's own locals, and splitting it out is for readability rather than
    for reuse. There is no other caller.

    ONE MORE BOUNDED QUERY, NOT A NEW ACCESS PATTERN. ``pipeline_failure`` is its
    own partition of ``step_name-timestamp-index``, so this is
    `_query_state_by_step` with a different partition key — the same keyed,
    time-windowed Query shape P28 R17.1 requires, and emphatically not a Scan.
    It takes the stage read from ten Queries to eleven.

    NO STATUS FILTER, deliberately. Every row RecordFailure writes carries
    ``status="error"`` (`record_failure/lambda_function.py:440`), so membership
    of this partition IS the predicate; a `FilterExpression` would cost a
    round-trip's worth of complexity to re-assert what the partition already
    says, and would silently drop a row whose status attribute was ever spelled
    differently.

    THE DEDUPE. A record is counted into a stage only when that stage has not
    already recorded an error for the same ``run_id``, and the run is then added
    to the stage's set so a second sentinel row for one run also counts once.
    A record with no readable ``run_id`` cannot be deduped and IS counted:
    dropping it would be the defect this function closes, and RecordFailure
    always writes something (falling back to the execution name, then
    ``"unknown"`` — `:426`), so a blank is the rare shape rather than the norm.

    REJECTIONS ARE SET ASIDE FIRST (2026-10-10). A row whose ``error_name`` is
    `_APPROVAL_REJECTED_ERROR_NAME` goes to ``rejected_by_step`` and nowhere
    else: not ``records``, not a stage, not ``unattributed_by_step``. The test
    is on ``error_name`` alone, never on the gate, because what makes it a
    rejection is that a reviewer decided — the same gate also produces
    timeouts, which stay failures.
    """
    unattributed_by_step: dict[str, int] = {}
    rejected_by_step: dict[str, int] = {}
    records = 0
    attributed = 0
    counted = 0
    already_counted = 0

    try:
        rows = _query_state_by_step(
            _FAILURE_RECORD_STEP_NAME,
            start,
            end,
            ProjectionExpression="run_id,failed_step,error_name,#ts",
            ExpressionAttributeNames={"#ts": "timestamp"},
        )
    except Exception as exc:
        # `incomplete`, never a measured zero: an operator acts differently on
        # "nothing failed outside a stage row" and "we could not tell"
        # (`LGR-4854b3d-1`, `LGR-b2f79ef-8`, both template / ratified). The stage
        # counts stay as the step rows left them and the payload says so.
        logger.warning(f"Failure-record attribution read failed: {exc}")
        return _FAILURE_ATTRIBUTION_UNREADABLE

    for row in rows:
        ts = _parse_iso(item_str(row, "timestamp"))
        if ts is None or ts < start or ts > end:
            continue
        failed_step = item_str(row, "failed_step")
        if _is_approval_rejection(row):
            gate = failed_step or "unknown"
            rejected_by_step[gate] = rejected_by_step.get(gate, 0) + 1
            continue
        records += 1
        frontend_key = _stage_for_failed_step(failed_step)
        if frontend_key is None:
            key = failed_step or "unknown"
            unattributed_by_step[key] = unattributed_by_step.get(key, 0) + 1
            continue
        attributed += 1
        run_id = item_str(row, "run_id")
        if run_id and run_id in stage_error_run_ids[frontend_key]:
            already_counted += 1
            continue
        counted += 1
        counts[frontend_key]["error"] += 1
        if run_id:
            stage_error_run_ids[frontend_key].add(run_id)

    return _FailureAttribution(
        records=records,
        attributed=attributed,
        counted=counted,
        already_counted=already_counted,
        unattributed_by_step=unattributed_by_step,
        rejected_by_step=rejected_by_step,
        outcome=MetricOutcome.measured(records, SOURCE_FAILURE_RECORDS),
    )


def _count_approval_rejections(start: datetime, end: datetime) -> MetricOutcome:
    """Runs a reviewer rejected at an approval gate in [start, end].

    For the PREVIOUS window only, which the success-rate trend needs and the
    stage read does not cover. The current window's count comes from
    `_attribute_failure_records`, off the same rows as the rest of the
    attribution, so the tile and the panel cannot disagree. Both use
    `_is_approval_rejection` and the same client-side window check.

    Same partition, same bounded Query (P28 R17.1), projecting only what the
    count reads. ``measured`` on a successful read, zero included — an empty
    partition is the measurement "nobody rejected anything" — and
    ``incomplete`` when the Query raised.
    """
    try:
        rows = _query_state_by_step(
            _FAILURE_RECORD_STEP_NAME,
            start,
            end,
            ProjectionExpression="error_name,#ts",
            ExpressionAttributeNames={"#ts": "timestamp"},
        )
    except Exception as exc:
        logger.warning(f"Approval-rejection count read failed: {exc}")
        return MetricOutcome.incomplete(SOURCE_FAILURE_RECORDS)
    count = 0
    for row in rows:
        ts = _parse_iso(item_str(row, "timestamp"))
        if ts is None or ts < start or ts > end:
            continue
        if _is_approval_rejection(row):
            count += 1
    return MetricOutcome.measured(count, SOURCE_FAILURE_RECORDS)


def _stage_durations_cw(start: datetime, end: datetime, period: int) -> dict[str, float | None]:
    """Mean Pipeline.StageDuration (ms) per invocation, per stage that emits it, across every PipelineId.

    Invocation-weighted: the window's summed duration over its summed sample
    count, so an empty series cannot dilute it. See the block above
    `_weighted_stage_duration_queries` for the measured defect the old
    `AVG(SEARCH(... 'Average'))` had.
    """
    out: dict[str, float | None] = {fe: None for fe in _CW_STAGE_NAMES}
    queries: list[dict[str, Any]] = []
    for fe, cw_name in _CW_STAGE_NAMES.items():
        queries.extend(_weighted_stage_duration_queries(f"{fe}_dur", f'StageName="{cw_name}"', period))
    try:
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=cast("Sequence[MetricDataQueryTypeDef]", queries),
            StartTime=start,
            EndTime=end,
        )
        results = resp.get("MetricDataResults", [])
        for fe in _CW_STAGE_NAMES:
            mean = _weighted_stage_duration(results, f"{fe}_dur")
            out[fe] = round(mean, 1) if mean is not None else None
    except Exception as exc:
        logger.warning(f"Stage duration CloudWatch query failed: {exc}")
    return out


#: The state-row step name the generated Glue job writes its row counts under
#: (`pyspark_templates/_partials/_load_counts.py.j2`, `LGR-9e76249-3`).
_LOAD_METRICS_STEP = "load_metrics"


def _row_count(row: Mapping[str, Any], key: str) -> int | None:
    """One count off a ``load_metrics`` row as an ``int``, or ``None`` if absent or unusable.

    The same two-default trick as ``pipelines_runs._optional_row_count``:
    ``item_int`` coerces DynamoDB's ``Decimal`` and rejects ``bool`` but answers
    "absent" and "0" alike, so asking with two defaults tells them apart. A
    negative count is not a count.
    """
    as_zero = item_int(row, key, default=0)
    as_one = item_int(row, key, default=1)
    if as_zero != as_one or as_zero < 0:
        return None
    return as_zero


def _get_rows_loaded(start: datetime, end: datetime) -> MetricOutcome:
    """Rows the Glue jobs in [start, end] report writing to their targets.

    Reads the ``(run_id, "load_metrics")`` rows the generated job writes after
    its target write (every one of the five templates), on the
    ``step_name-timestamp-index`` partition for that step, windowed on the row's
    own ``timestamp``. A Query, never a Scan (P28 R17.1).

    WHAT IS COUNTED. ``rows_written`` where ``write_confirmed`` is true: the
    templates stamp ``write_confirmed`` only after the write returned without
    raising (``_load_counts.py.j2``, the ``post_write`` stage), and
    ``rows_written`` alone is rows *handed to* the writer (p37 risk N1). An
    empty-source run writes its row with ``rows_written = 0`` and
    ``write_confirmed = false``: that is a measured run that loaded nothing, so
    it contributes 0 rather than being skipped. Every source type counts the
    same way, which is the point — the estimate this replaced could say nothing
    about a JDBC source.

    WHAT REPLACED WHAT, 2026-10-10. These tiles used to sum the
    ``record_count_estimate`` on successful ``file_validation`` rows: file size
    divided by a fixed per-format guess, re-counted on every re-run, credited
    to runs that then failed before Glue, and 0 or absent for a JDBC source.
    On a development deployment the tile read 0 for a day of JDBC runs that loaded 100 rows.

    NOT A PERSISTED PROJECTION. ``LGR-9e76249-2`` keeps the derived
    Load_Outcome on the read path and names "query outcomes across runs" as its
    revisit condition. This read does not query outcomes: it sums the raw count
    the job itself persisted, which is already queryable by step and time, so
    no projection is needed and ``shared/load_outcome.py`` is not involved.

    Three answers, as before (`LGR-b2f79ef-8`): at least one row with a usable
    ``rows_written`` -> ``measured`` (a sum of zeros is a measured 0); a
    successful Query with no such row -> ``absent``; a raising Query ->
    ``incomplete``.
    """
    total = 0
    rows_seen = 0
    try:
        items = _query_state_by_step(
            _LOAD_METRICS_STEP,
            start,
            end,
            ProjectionExpression="rows_written,write_confirmed,#ts",
            ExpressionAttributeNames={"#ts": "timestamp"},
        )
        for it in items:
            ts = _parse_iso(item_str(it, "timestamp"))
            if ts is None or ts < start or ts > end:
                continue
            written = _row_count(it, "rows_written")
            if written is None:
                continue
            rows_seen += 1
            if it.get("write_confirmed") is True:
                total += written
    except Exception as exc:
        logger.warning(f"Rows-loaded query failed: {exc}")
        return MetricOutcome.incomplete(SOURCE_LOAD_METRICS_ROWS)
    if rows_seen == 0:
        return MetricOutcome.absent(SOURCE_LOAD_METRICS_ROWS)
    return MetricOutcome.measured(total, SOURCE_LOAD_METRICS_ROWS)


def _get_agent_latency(start: datetime, end: datetime, period: int) -> MetricOutcome:
    """Average `Pipeline.StageDuration` (ms) over the agent stages, or Not_Established.

    RE-SOURCED, not adjusted (D1, ratified). This function used to average the
    dispatcher Lambdas' `AWS/Lambda` `Duration` — the ~1 s dispatch ack
    explained at `_AGENT_FUNCTIONS` — under a tile about agent performance. It
    now reads the duration the agent records inside its own container, across
    the four stages in `_AGENT_STAGE_NAMES`. `R7.4` offered two arms and both
    were rejected with reasons; this is the deviation that satisfies its stated
    purpose, and the user ratified it.

    **No datapoints answers Not_Established, and the dispatcher figure is never
    stood in for it (`R4.1`).** That is the whole point of the change: a tile
    that says nothing is honest, and one that says ~1 s is not. The tile going
    from a number to Not_Established is design risk (g) realised, not a
    regression — task 13 is the guard that the producer is emitting first.

    AVERAGING OVER PARTIAL DATA. The four stages are the QUERY SET, fixed by
    D1; the mean is over the invocations that came back. A stage with no
    datapoints adds nothing to either total, so it neither contributes a zero
    term (`LGR-4854b3d-1`, template / ratified: no zero-substitution) nor
    dilutes the others, and **only all four being absent reaches the absent
    arm.**

    THE MEAN IS INVOCATION-WEIGHTED, and this changed on 2026-10-10. It used to
    be `AVG` over a SEARCH of the `Average` statistic, which an earlier revision
    of this docstring reported as verified (4956.3 ms from two stages at 5245.2
    and 4667.3). That held only while each matched series had a datapoint in
    every bucket. SEARCH returns every series CloudWatch indexes for the brace,
    including ones with no datapoint in the window, and `AVG` counts those as
    zeros: measured on a development deployment, the tile read 2 s against a 25.5 s
    invocation-weighted mean across eight invocations. See the block above
    `_weighted_stage_duration_queries`.

    The three provenance answers (`R4.2`):

    - at least one datapoint -> ``measured``.
    - the query returned and no stage has a datapoint in the window ->
      ``absent``. "We looked and there is nothing" — every agent stage is
      silent, which is what an idle window looks like.
    - the query raised -> ``incomplete``, logged at WARNING. `LGR-b2f79ef-8`
      (template / ratified) is why this is not spelled ``absent``: an operator
      acts differently on "nothing ran" and "we could not look".

    A `SUM` reduction series missing from the response entirely is treated as
    ``absent`` rather than ``incomplete``: CloudWatch returns the ids it was
    asked for, so an absent id carries no more information than an empty
    ``Values`` list, and both mean the same thing about the window.

    THE DIMENSION SET IN THE BRACE MUST BE THE PUBLISHER'S EXACTLY. A SEARCH
    matches only series carrying precisely the dimensions named, so all four
    that `record_stage_execution` writes are listed — `PipelineId`,
    `StageName`, `Status` and Powertools' `service`. Naming three of them
    matches nothing and looks exactly like an idle system; that is the trap
    `_COST_METRIC_SERIES` documents for the cost read and the one task 13.5
    closed for `_stage_durations_cw`. This expression is a sibling of that
    one and shares its brace; the two are separate strings on purpose, because
    they answer per-stage and across-stages questions and their `StageName`
    filters are not the same.
    """
    stages = " OR ".join(f'"{name}"' for name in _AGENT_STAGE_NAMES)
    try:
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=cast(
                "Sequence[MetricDataQueryTypeDef]",
                _weighted_stage_duration_queries("alat", f"StageName=({stages})", period),
            ),
            StartTime=start,
            EndTime=end,
        )
    except Exception as exc:
        logger.warning(f"Agent latency query failed: {exc}")
        return MetricOutcome.incomplete(SOURCE_STAGE_DURATION)
    mean = _weighted_stage_duration(resp.get("MetricDataResults", []), "alat")
    if mean is None:
        return MetricOutcome.absent(SOURCE_STAGE_DURATION)
    return MetricOutcome.measured(mean, SOURCE_STAGE_DURATION)


def _get_agent_error_rate(start: datetime, end: datetime, period: int) -> MetricOutcome:
    """Error rate (%) across the AgentCore-backed agent Lambdas, or Not_Established.

    The `float | None` the design names is the `value` position of the returned
    outcome; the second position carries the provenance the availability figure
    is published with (R4.2).

    Both of today's zero-returning arms become Not_Established, for the same
    reason and with different tokens:

    - **a raising query** -> ``incomplete``. This is `R4.8` verbatim: a failed
      query used to return `0.0`, which the caller turned into an availability of
      `100.0` and a health of `healthy`. A CloudWatch outage was reported as a
      perfectly available AgentCore — the strongest possible claim, made on no
      evidence, from the one event that proves nothing was observed.
    - **zero invocations** -> ``absent``. `err / inv` is undefined at `inv == 0`,
      so there is no rate; reporting `0.0` asserts a flawless window in which
      nothing ran. Identical in kind to `_success_rate`'s pre-p39 `100.0`
      (`R4.6`), and answered the same way.

    The two are kept apart rather than both spelled ``absent`` per
    `LGR-b2f79ef-8` (template / ratified): "nothing there" and "could not look"
    are different facts and an operator acts differently on each.

    The caller short-circuits on `value is None` — `availability: None` and
    `status: "unknown"`, which is already a member of the `HealthStatus` union
    (`dashboard-react/src/types.ts:676`). `_health_from_availability` therefore
    gains NO `unknown` branch: a health mapping over an absent percentage has no
    percentage to map, and giving it one would put the absence decision in two
    places (`LGR-4854b3d-1`).

    The `AWS/Lambda` source is deliberate and stays: a dispatcher error IS a
    genuine agent-invocation error. `R4.8` asks only that a failed query say so.
    """
    fns = " OR ".join(f'"{fn}"' for fn in _AGENT_FUNCTIONS)
    try:
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "ai_raw",
                    "Expression": f"SEARCH('{{AWS/Lambda,FunctionName}} MetricName=\"Invocations\" FunctionName=({fns})', 'Sum', {period})",
                    "ReturnData": False,
                },
                {"Id": "ai", "Expression": "SUM(ai_raw)", "Period": period},
                {
                    "Id": "ae_raw",
                    "Expression": f"SEARCH('{{AWS/Lambda,FunctionName}} MetricName=\"Errors\" FunctionName=({fns})', 'Sum', {period})",
                    "ReturnData": False,
                },
                {"Id": "ae", "Expression": "SUM(ae_raw)", "Period": period},
            ],
            StartTime=start,
            EndTime=end,
        )
        parsed = _parse_metric_data(resp)
        inv = parsed.get("ai", 0)
        err = parsed.get("ae", 0)
        if inv <= 0:
            return MetricOutcome.absent(SOURCE_AGENT_LAMBDA_ERRORS)
        return MetricOutcome.measured(err / inv * 100, SOURCE_AGENT_LAMBDA_ERRORS)
    except Exception as exc:
        logger.warning(f"Agent error-rate query failed: {exc}")
        return MetricOutcome.incomplete(SOURCE_AGENT_LAMBDA_ERRORS)


# ---------------------------------------------------------------------------
# The cost read (R2)
# ---------------------------------------------------------------------------
# The metric this reads, spelled once. `SOURCE_GLUE_RUN_COST` above is the
# PROVENANCE token for the same read; this is the CloudWatch identity of the
# series, and the two are deliberately separate strings because one appears in
# the payload and the other in a SEARCH expression.
#
# The dimension schema must be the publisher's EXACT dimension set: a
# CloudWatch SEARCH matches only series carrying precisely the dimensions
# named. `publish_glue_run_cost` (`shared/metrics_facade.py:686-727`) publishes
# `{PipelineId, JobName, WorkerType, Environment}`, so all four are listed. The
# old expression named three — `{PipelineId, WorkerType, Environment}` — which
# was correct for `GlueJob.EstimatedCostPerRun`'s creation-time publisher and
# would match nothing here. Adding a dimension on the producer side without
# adding it here empties the series silently.
_COST_METRIC_NAME = "GlueJob.RunCostEstimate"
_COST_METRIC_SERIES = (
    '{AgenticDataIngestion/Pipeline,PipelineId,JobName,WorkerType,Environment} MetricName="GlueJob.RunCostEstimate"'
)

# The producer's two pricing constants, REPORTED here and applied nowhere.
#
# `job_completion:128-135` computes `actual_cost` as `dpu_hours * 0.44`, then
# raises it to `(total_dpu * 600 / 3600) * 0.44` whenever the run was shorter
# than ten minutes. This handler sums the result; it does not recompute it. The
# two numbers are duplicated across a Lambda boundary on purpose — they are
# what `basis` has to STATE (R2.7) so that a reader can see the figure is a
# DPU-minutes derivation and not a bill, and a shared module for two literals
# that one side applies and the other only quotes would invert
# `LGR-aa15a1b-1`'s test (a definition moves to `shared/` when two WRITERS
# would otherwise diverge; here there is one writer and one reporter).
#
# If the producer's rate changes, this states the old one. That is visible in
# the payload — `basis.rate_usd_per_dpu_hour` is published — which is the whole
# reason the rate is named rather than folded silently into a number.
_GLUE_RATE_USD_PER_DPU_HOUR = 0.44
_GLUE_DPU_MINIMUM_MINUTES = 10

# R2.7 in one sentence, in the payload rather than in a comment. The 10-minute
# minimum is why every observed run's `actual_cost` is a constant 0.147 and why
# the pipeline page's `$0.44 / $0.15` reconciles as `3 × 0.147` exactly
# (requirements constraint 14). NOTHING IN THIS SYSTEM MEASURES SPEND, and this
# is where a reader is told so.
_COST_BASIS_NOTE = "estimate derived from Glue DPU-minutes with a 10-minute per-run minimum; not measured spend"

# Retention-safe default period. 1-hour datapoints are retained for 455 days,
# so every window this handler can be asked for is readable at it — including
# the previous half of the `30d` window, which reaches 60 days back and returns
# NOTHING at a 1-minute period because CloudWatch keeps 1-minute data for only
# 15 days. An unreadable window looks exactly like an empty one, so too fine a
# period would report `absent` for data that exists (R4.2's distinction, lost).
# `_build_comprehensive_metrics` passes its own `cw_period` instead, which is
# already chosen per window (`CW_PERIOD`); the default is for a two-argument
# caller and is the coarsest granularity that is safe for all five windows.
_COST_DEFAULT_PERIOD_SECONDS = 3600


class _CostOutcome(NamedTuple):
    """The cost read's three answers: the value, its `basis`, its provenance.

    The design names `(value | None, basis, determined_from)` for this one read
    because it carries a third thing that no other read has — R2.6's `basis`,
    the dict stating what was summed and over what window. A plain
    :class:`MetricOutcome` cannot hold it, and returning `(MetricOutcome, basis)`
    would make the call site unpack two things of different kinds.

    So this is a three-field tuple, BUILT FROM a :class:`MetricOutcome` through
    :meth:`of` rather than spelling its own tokens. The R4.2 vocabulary stays
    defined in exactly one place — a new suffix cannot be invented here — while
    the positional shape at the call site is the one the design states.

    ``basis`` is non-``None`` exactly when ``value`` is. There is no basis for a
    figure that was not established: the window bounds are known either way, but
    an annotation of what was summed, when nothing was summed, would be a
    statement about a measurement that does not exist. It also keeps the
    `_PublishedFact` registry's rule intact for both entries — a suffixed token
    exactly when the value is ``None`` — so `missing` names `monthly_cost` and
    `monthly_cost_basis` together rather than one without the other.
    """

    value: float | None
    basis: dict[str, Any] | None
    determined_from: str

    @classmethod
    def of(cls, outcome: MetricOutcome, basis: dict[str, Any] | None = None) -> "_CostOutcome":
        """Lift a :class:`MetricOutcome` into a cost outcome, with its basis."""
        return cls(outcome.value, basis, outcome.determined_from)


def _get_estimated_monthly_cost(
    start: datetime,
    end: datetime,
    period: int = _COST_DEFAULT_PERIOD_SECONDS,
) -> _CostOutcome:
    """Glue run-cost estimate summed over ``[start, end]``, or Not_Established.

    THE EMISSION CADENCE THIS ACTUALLY READS (R2.8). One
    ``GlueJob.RunCostEstimate`` datapoint per Glue run that reached a **terminal
    state** — `SUCCEEDED`, `FAILED`, `STOPPED` or `TIMEOUT`, because DPU-minutes
    are billed for a run that failed as much as for one that succeeded.
    Published by `job_completion` from its post-callback anchor
    (`lambda_function.py:697-711`) via `publish_glue_run_cost`
    (`shared/metrics_facade.py:686`), once per run: a duplicate EventBridge
    delivery is turned away by the consumed-token check before it reaches the
    publish, so `R2.2` holds on the existing idempotency and not on a new
    mechanism. A run whose Glue job-run details could not be read publishes
    **nothing** rather than a zero, so an unreadable run is absent from the sum
    instead of counted as free.

    The old docstring described a cadence that was never true. It said "emitted
    once per run" of ``GlueJob.EstimatedCostPerRun``, which
    `publish_glue_job_creation_metrics` emits once per job **created**
    (`metrics_facade.py:645`, inside its `if creation_success:` branch; callers
    `job_builders.py:761` and `:785`). Summing it counted job creations and
    priced them at an estimate — a creation counter under a tile labelled with a
    currency.

    BOTH BOUNDS ARE APPLIED (R2.3, R2.5). The window is the caller's, passed in
    whole; this function anchors nothing of its own. The old signature took only
    ``end`` and derived a trailing 30 days from it, so the two calls
    `_build_comprehensive_metrics` makes — one for the current window, one for
    the previous — produced windows overlapping by 29 of 30 days on a 24-hour
    period, and the cost trend was arithmetic on two overlapping integrals of
    the same series. The caller now passes the same two pairs it already
    computes for every other windowed read, so current and previous are
    adjacent and disjoint.

    Adjacency and disjointness hold to CloudWatch's own timestamp granularity,
    which is stated rather than assumed: `GetMetricData` rounds a requested
    bound down to the whole minute for data under 15 days old, to five minutes
    under 63 days, and to the hour beyond that. Two adjacent windows can
    therefore share at most one rounding interval at their join. That is the
    same exposure every other windowed read in this payload already has
    (`_get_sfn_metrics`, `_get_agent_error_rate`), which is the point — the two
    cost windows are now comparable to each other AND on the same footing as
    the metrics they sit beside.

    THE FIGURE IS THE SELECTED WINDOW'S, NOT A MONTH'S. The payload key keeps
    its name (`monthly_cost`, `monthly_cost_basis`) because every existing key
    does, but the number is now the sum over whatever period the caller asked
    for. `basis.window_start` / `basis.window_end` are the bounds actually
    queried, which is how a reader learns which window they are looking at
    (R2.6) — and it is why `basis` reports the bounds this function received
    rather than re-deriving them.

    THE THREE PROVENANCE ANSWERS map onto what the read knows, as everywhere
    else in this module:

    - **datapoints in the window** -> ``measured``, with a `basis`. A sum of
      ``0.0`` over datapoints that exist is a measurement, not an absence.
    - **a successful query with no datapoints** -> ``absent``, ``value`` and
      ``basis`` both ``None`` (R2.4). Never ``0.0``: no Glue run reaching a
      terminal state in the window and a window whose cost happened to total
      nothing are different facts, and the flat-rate fallback this replaces
      answered the first with a number derived from a different table
      altogether.
    - **a raising client** -> ``incomplete`` plus a WARNING.

    R2.4's second half — `not_established` for the cost **trend** — follows
    from the ``None`` with no conditional here and none at the call site:
    :func:`_reported_trend` already guards all four trend entries, so an absent
    current window answers `TREND_NOT_ESTABLISHED` and an absent previous one
    reaches `_compute_trend`'s ``previous is None`` arm. That is what the guard
    was built for.
    """
    try:
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "rc_raw",
                    "Expression": f"SEARCH('{_COST_METRIC_SERIES}', 'Sum', {period})",
                    "ReturnData": False,
                },
                {"Id": "rc_total", "Expression": "SUM(rc_raw)", "Period": period},
                # SampleCount, reduced the same way, IS the run count: the
                # publisher emits exactly one datapoint per terminal run, so the
                # number of samples in the window is the number of runs the sum
                # is over. `basis.runs` is that count and not a re-query.
                {
                    "Id": "rc_runs_raw",
                    "Expression": f"SEARCH('{_COST_METRIC_SERIES}', 'SampleCount', {period})",
                    "ReturnData": False,
                },
                {"Id": "rc_runs", "Expression": "SUM(rc_runs_raw)", "Period": period},
            ],
            StartTime=start,
            EndTime=end,
        )
    except Exception as exc:
        logger.warning(
            f"Glue run-cost query failed for [{start.isoformat()}, {end.isoformat()}]: {exc}",
            exc_info=True,
        )
        return _CostOutcome.of(MetricOutcome.incomplete(SOURCE_GLUE_RUN_COST))

    # Read the raw series rather than `_parse_metric_data`, which returns 0 for
    # an empty one — the exact collapse R2.4 forbids. Emptiness is the signal
    # here, so it has to survive the parse.
    series_values: dict[str, list[float]] = {
        series["Id"]: list(series.get("Values", []) or []) for series in resp.get("MetricDataResults", [])
    }
    cost_values = series_values.get("rc_total") or []
    if not cost_values:
        # No datapoint, or the reduction series was not in the response at all —
        # a successful call that established no cost either way.
        return _CostOutcome.of(MetricOutcome.absent(SOURCE_GLUE_RUN_COST))

    total = float(sum(cost_values))
    basis: dict[str, Any] = {
        "metric": _COST_METRIC_NAME,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "runs": int(sum(series_values.get("rc_runs") or [])),
        "rate_usd_per_dpu_hour": _GLUE_RATE_USD_PER_DPU_HOUR,
        "dpu_minimum_minutes": _GLUE_DPU_MINIMUM_MINUTES,
        "note": _COST_BASIS_NOTE,
    }
    return _CostOutcome.of(MetricOutcome.measured(total, SOURCE_GLUE_RUN_COST), basis)


# ---------------------------------------------------------------------------
# The deploy-injected bucket name the storage filter is derived FROM (R1.5)
# ---------------------------------------------------------------------------
# `PROCESSED_BUCKET` is set on this Lambda by the deploy, from the bucket
# construct itself: `compute-stack.ts:645` reads
# `foundation.processedBucket.bucketName`, and that bucket is created at
# `foundation-stack.ts:80-83` as `adi-processed-files-${account}-${region}`.
# All nine buckets share the form `adi-<purpose>-${account}-${region}`
# (`foundation-stack.ts:67-121`).
#
# So the ONE identifier that names the buckets at deploy time is readable here
# at run time, and R1.5 — "the filter SHALL be derived from the same identifier
# that names the buckets at deploy time, so that a rename of the deployment
# prefix cannot silently empty the series" — is satisfied structurally rather
# than by convention. A rename of `adi-` in `foundation-stack.ts` changes this
# filter in the same deploy that changes the bucket names, because the filter is
# a function of one of those names. NO CDK CHANGE: the variable is already there.
#
# WHY THE OLD FILTER FOUND NOTHING: the SEARCH searched for the bare token
# `agentic_data_ingestion`. That is the SNAKE naming form, which this repo uses
# for Lambdas, Glue jobs and state machines — and no bucket name contains it.
# The buckets carry the KEBAB form (`adi-…`), as does the Cognito domain
# (`agentic-data-ingestion-<account>-<env>`). Both forms are live in this repo
# on purpose; `2b45b72912` had to teach the Glue IAM ARNs to carry both. One
# hand-written identifier in the wrong form matched zero series, `vals` was
# empty, and ~135 MB of real storage was published as `0`.
#
# Do NOT put a bucket-name literal back into the SEARCH expression, in any form
# — not the deploy prefix, not the account id, not a purpose segment. Every term
# in that expression must be a segment of the value this variable holds at run
# time.
#
# FOR TASK 4.2's STRUCTURAL GUARD: assert that, not the file's text. The prose
# below has to quote the real bucket names to explain the defect, and this module
# legitimately hand-writes `agentic_data_ingestion` in the AWS/Lambda and
# AWS/States SEARCHes, where it is the correct identifier for those resources —
# so a source-text search for either naming form cannot distinguish the fix from
# the documentation of the fix, nor from an unrelated correct literal. What R1.5
# actually asserts is a data dependency: change `PROCESSED_BUCKET` and the
# expression sent to CloudWatch changes with it; give the function no usable
# value and it queries nothing at all.
_DEPLOY_BUCKET_ENV = "PROCESSED_BUCKET"

# An AWS account id is twelve digits. It is what separates the `<purpose>`
# segments from the `-${account}-${region}` suffix in a name whose purpose
# segment count varies (`processed-files` is two, `templates` is one).
_ACCOUNT_ID_SEGMENT_LEN = 12


def _deploy_bucket_search_tokens(bucket_name: str) -> list[str]:
    """AND-ed CloudWatch SEARCH terms matching every bucket of this deployment.

    Splits ``adi-processed-files-444455556666-us-west-2`` into its `-`
    segments, finds the account id (the twelve-digit segment), and returns the
    deploy prefix plus everything from the account id onward — dropping the
    `<purpose>` segments in between, which are what make one bucket name
    specific to one bucket::

        ['adi', '444455556666', 'us', 'west', '2']

    Returns ``[]`` when the name is not in the deploy form, so the caller can
    report Not_Established instead of guessing a prefix.

    WHY EVERY TERM IS A SINGLE ALPHANUMERIC SEGMENT. The design left the
    composition open (risk h): two AND-ed segments, or one concatenated
    `<account>-<region>` term. Task 4.3 settled it read-only against the
    fresh-account deployment with ten `get-metric-data` probes, and the
    dichotomy turns out to be false — **both compositions are equivalent**,
    because CloudWatch tokenizes the search term as well as the identifier.

    THE MATCHING MODEL, AS MEASURED. Both the term and the searchable
    identifier are split on non-alphanumerics, and every term-token must be
    present as a WHOLE token of the identifier:

      - not substring: `rocessed` and `processe` each matched 0 series, where
        the whole token `processed` matched 1;
      - not one-token-per-term either: a hyphenated `us-west-2` matched 10
        series, and an underscored spelling of a hyphenated bucket name matched
        that bucket — so a multi-token term is AND-ed token-wise;
      - so the segment form and the concatenated form tokenize to the same set,
        and both returned the same 8 series and the same total.

    That falsifies two of the three models this function could not previously
    choose between (substring-anywhere; term-equals-one-token) and confirms the
    third. It also explains the two SEARCHes elsewhere in this module that
    always worked: `agentic_data_ingestion` tokenizes to three tokens, each
    present in `agentic_data_ingestion_create_code` and in a StateMachineArn.
    The segment form therefore ships because it is correct, not because it was
    the safe guess, and concatenating the suffix later would not break it.

    The account and region terms narrow nothing on their own, also as measured:
    the account id alone matched every series in the account — it is in the
    searchable space, not only the bucket name — the region words matched every
    series whose bucket name carries them, and the prefix term alone returned
    the same 8 series and the same total as all five terms together. They are
    here because R1.5 is about where the filter COMES FROM, and carrying the
    whole deploy scope makes the filter a function of the full bucket name
    rather than of its first three characters. They can only narrow the match
    set, never widen it, and every one of them is provably a segment of the
    name they were taken from.
    """
    segments = [seg for seg in bucket_name.split("-") if seg]
    account_at = next(
        (
            i
            for i, seg in enumerate(segments)
            # `i >= 2`: at least a prefix segment and one purpose segment must
            # precede the account, or this is not the deploy naming form.
            if i >= 2 and len(seg) == _ACCOUNT_ID_SEGMENT_LEN and seg.isascii() and seg.isdigit()
        ),
        None,
    )
    # Nothing after the account id means no region segment, so the name is not
    # `adi-<purpose>-<account>-<region>` and no deploy scope can be recovered.
    if account_at is None or account_at + 1 >= len(segments):
        return []
    return [segments[0], *segments[account_at:]]


def _get_s3_storage_bytes(end: datetime) -> MetricOutcome:
    """Latest total StandardStorage bytes across this deployment's buckets, or Not_Established.

    The bucket-name filter is derived from `PROCESSED_BUCKET` — see
    `_DEPLOY_BUCKET_ENV` above for why that satisfies R1.5 structurally, and
    `_deploy_bucket_search_tokens` for the term composition and its reasoning.

    The three provenance values (R4.2) map onto what the read knows:

    - **datapoints in the window** -> ``measured``. R1.2: at least one deployed
      bucket reporting a `StandardStorage` datapoint yields a positive total,
      which is the whole of Group A — the old filter made this unreachable.
    - **a successful query with no datapoints** -> ``absent`` (R1.3). Not a byte
      count of zero: `BucketSizeBytes` is a daily metric, so a young or
      unmeasured deployment has no series rather than an empty one, and `0`
      would assert that the buckets were measured and found empty.
    - **a raising client** -> ``incomplete`` plus the WARNING (R1.4).

    NO DEPLOY-DERIVED FILTER -> ``absent``, never a guessed prefix. Two cases
    reach it: `PROCESSED_BUCKET` unset, and a value not in the deploy naming
    form. Both are the same fact — there is no identifier to build a filter
    from, so no bucket series was established — and neither is allowed to fall
    back on a literal prefix, which would be the hand-written identifier R1.5
    exists to remove and would re-create Group A the first time the deploy
    prefix changed. (An argument exists for ``incomplete`` on the unset case, since a
    filter we could not build is closer to "we could not look" than to "we
    looked and there is nothing"; ``absent`` is what R1's task text specifies,
    and it is one call away if that is revisited.) Neither case is reachable in
    a deployed Lambda — `compute-stack.ts:645` always injects the variable — so
    what this arm really guards is a misconfigured or half-deployed function,
    which is exactly when a fabricated number is most expensive.

    `os.environ.get` deliberately, NOT `require_env`: this absence is a value
    to report, not a fault to raise, and `require_env` raises on a missing name.
    """
    bucket_name = os.environ.get(_DEPLOY_BUCKET_ENV, "").strip()
    tokens = _deploy_bucket_search_tokens(bucket_name) if bucket_name else []
    if not tokens:
        logger.warning(
            f"S3 storage filter not established: {_DEPLOY_BUCKET_ENV} is "
            f"{'unset' if not bucket_name else 'not in the <prefix>-<purpose>-<account>-<region> deploy form'}; "
            "reporting Not_Established rather than guessing a bucket prefix"
        )
        return MetricOutcome.absent(SOURCE_S3_BUCKET_SIZE)

    bucket_filter = " ".join(tokens)
    start = end - timedelta(days=2)
    try:
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "s3_raw",
                    "Expression": (
                        'SEARCH(\'{AWS/S3,BucketName,StorageType} MetricName="BucketSizeBytes" '
                        f"{bucket_filter} StorageType=\"StandardStorage\"', 'Average', 86400)"
                    ),
                    "ReturnData": False,
                },
                {"Id": "s3sum", "Expression": "SUM(s3_raw)", "Period": 86400},
            ],
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
        for series in resp.get("MetricDataResults", []):
            if series["Id"] == "s3sum":
                vals = series.get("Values", [])
                # TimestampDescending, so vals[0] is the most recent daily sum.
                if vals:
                    return MetricOutcome.measured(int(vals[0]), SOURCE_S3_BUCKET_SIZE)
                return MetricOutcome.absent(SOURCE_S3_BUCKET_SIZE)
    except Exception as exc:
        logger.warning(f"S3 storage query failed: {exc}")
        return MetricOutcome.incomplete(SOURCE_S3_BUCKET_SIZE)
    # The reduction series was not in the response at all — a successful call
    # that established no total, which is the same fact as an empty one.
    return MetricOutcome.absent(SOURCE_S3_BUCKET_SIZE)


# ═══════════════════════════════════════════════════════════════════════════
# Caching helpers
# ═══════════════════════════════════════════════════════════════════════════
#
# THE ONE RESPONSE-CACHE MECHANISM FOR THIS LAMBDA (p47 R7.2).
#
# These two functions are no longer metrics-private. `handlers/stats.py` calls
# them for `GET /api/stats` (p47 D3), because R7.2 says in as many words that
# the stats cache "MUST reuse the in-tree DynamoDB-backed pattern at
# handlers/metrics.py" and "MUST NOT introduce a second, divergent caching
# mechanism". The names are kept — `_get_cached_metrics` / `_cache_metrics` —
# rather than generalised to `_get_cached_response`, because eight test modules
# patch these two symbols or `_cache_table` by name, and a rename would churn
# every one of them for no behavioural gain.
#
# THREE KEYWORD-ONLY PARAMETERS were added for the second caller. All three
# default to exactly what this module did before, so no metrics behaviour
# changes and no metrics call site moves:
#
#   cache_namespace  the `<namespace>#<variant>` key prefix. Default
#                    "metrics_cache" — the row `data-stack.ts:66-69` names as
#                    the sparse-GSI example. Stats passes its own namespace so
#                    two unrelated payload shapes never share a key: a
#                    `metrics_cache#…` row deserialised into a stats body would
#                    be a silent wrong-shape bug rather than a cache miss.
#   table            which table resource to read/write. Default `_cache_table`,
#                    so `patch("handlers.metrics._cache_table")` keeps
#                    intercepting every metrics call. Stats passes the handle
#                    from *its* module namespace, so a test that patches
#                    `handlers.stats.state_table` redirects the cache too rather
#                    than leaving it pointed at a live table.
#   ttl_seconds      write-side only, and resolved at call time rather than as
#                    a default expression so `CACHE_TTL_SECONDS` stays late-bound.
#
# The TTL is the reason a parameter was needed at all, and it is safe to
# parameterise precisely because it is stored per item: `_cache_metrics` writes
# an absolute `ttl` and `_get_cached_metrics` compares `time.time()` against
# whatever that item carries. The TTL appears in neither the key nor the
# read-side staleness test, so two callers can hold different TTLs in one table
# without the reader knowing which is which. R7.5 needs ≤ 60 s for stats;
# metrics keeps its 300 s.


def _get_cached_metrics(
    period: str,
    *,
    cache_namespace: str = "metrics_cache",
    table: Any = None,
) -> dict[str, Any] | None:
    """Return the cached payload if its TTL has not expired, else None.

    Degrades rather than raises: any failure — a missing row, an unreadable
    ``ttl``, a throttle, a table that does not exist — is a ``None`` and a
    warning, so the caller recomputes. R7.2 requires the stats cache to fail
    this way (fall through to computing) rather than the way the pipelines scan
    fails (a 500).

    Args:
        period: the key's variant token. For metrics this is the window
            (``"24h"``); for stats there is no window and it is ``"global"``.
        cache_namespace: key prefix. See the block comment above.
        table: table resource to read. ``None`` means this module's
            ``_cache_table``.
    """
    try:
        cache_key = f"{cache_namespace}#{period}"
        cache_table = _cache_table if table is None else table
        resp = cache_table.get_item(Key={"run_id": cache_key, "step_name": "cache"})
        item = resp.get("Item")
        if not item:
            return None
        ttl = item_int(item, "ttl")
        if time.time() > ttl:
            return None
        data_str = item_str(item, "data", "{}")
        result: dict[str, Any] = json.loads(data_str)
        return result
    except Exception as exc:
        logger.warning(f"Cache read failed: {exc}")
        return None


def _cache_metrics(
    period: str,
    metrics: dict[str, Any],
    *,
    cache_namespace: str = "metrics_cache",
    table: Any = None,
    ttl_seconds: int | None = None,
) -> None:
    """Write a payload to the DynamoDB response cache. Never raises.

    The row deliberately carries **no** ``timestamp`` attribute. That is what
    keeps it out of ``step_name-timestamp-index``, which is sparse by design —
    ``data-stack.ts:66-69`` names this very row as its example. Adding one would
    put cache rows into the index that p47 D4's per-step counts Query.

    Args:
        period: the key's variant token (see ``_get_cached_metrics``).
        metrics: the payload. JSON-encoded with ``default=str``.
        cache_namespace: key prefix. See the block comment above.
        table: table resource to write. ``None`` means ``_cache_table``.
        ttl_seconds: lifetime in seconds. ``None`` means ``CACHE_TTL_SECONDS``
            (300), read at call time so the module constant stays late-bound.
    """
    try:
        cache_key = f"{cache_namespace}#{period}"
        cache_table = _cache_table if table is None else table
        ttl = CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        cache_table.put_item(
            Item={
                "run_id": cache_key,
                "step_name": "cache",
                "data": json.dumps(metrics, default=str),
                "ttl": int(time.time()) + ttl,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception as exc:
        logger.warning(f"Cache write failed (non-fatal): {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Internal utilities
# ═══════════════════════════════════════════════════════════════════════════


def _parse_metric_data(response: Mapping[str, Any]) -> dict[str, float]:
    """Collapse a CloudWatch GetMetricData response into {id: total}."""
    result: dict[str, float] = {}
    for series in response.get("MetricDataResults", []):
        metric_id = series["Id"]
        values = series.get("Values", [])
        result[metric_id] = sum(values) if values else 0
    return result


def _batch_get_metric_data(
    queries: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    """CloudWatch allows max 500 queries per call. Batch if needed."""
    all_parsed: dict[str, float] = {}
    batch_size = 500
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=cast("Sequence[MetricDataQueryTypeDef]", batch),
            StartTime=start,
            EndTime=end,
        )
        all_parsed.update(_parse_metric_data(resp))
    return all_parsed


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp that may be naive, 'Z'-suffixed, or offset-aware.

    Naive timestamps are assumed to be UTC. Returns None on failure.
    """
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (ValueError, TypeError):
        return None


def _reported_trend(current: float | None, previous: float | None) -> str:
    """The trend a payload publishes for a metric, absent current value included.

    ONE mechanism for all four trends, which is the whole reason it exists.
    Tasks 3.3 and 3.4 each gave their own source an absent case and each wrote
    a conditional at its own trend entry, so the payload was accumulating one
    hand-written null test per metric — four spellings of one rule, three of
    which would be edited by tasks 15.1 and 6.1 and one of which would be
    missed. `LGR-4854b3d-1` (template / ratified) is the fence, in its own
    words: a three-state value is never collapsed to one of its ends, and *the
    collapsing expression lives in exactly one place*. For a trend, that place
    is here.

    ``current is None`` means this window established no value, so there is no
    change to take the direction OF and `TREND_NOT_ESTABLISHED` is the only
    honest answer — the same answer `_compute_trend` gives for an absent
    *previous* (R4.4), reached for the mirror-image reason. Reporting `stable`
    instead would say the metric held level, which is a measurement, and both
    windows may be unmeasured.

    :func:`_compute_trend` deliberately keeps ``current: float`` NON-optional
    (task 3.2) and that stays: it is the arithmetic, and the absence decision
    does not belong inside it. This function is the guard in front of it. Both
    orderings were available and only one of them keeps `_compute_trend`
    total — widening ``current`` to ``float | None`` there would put the same
    decision in the body of the function that computes the change, where the
    relative-change formula has no meaning for it.

    `cost` is the demonstration that this was worth building. Task 6.1 rewrote
    `_get_estimated_monthly_cost` to answer ``None`` for a window with no
    terminal Glue run, and `R2.4`'s "report Not_Established for the cost trend"
    became true **with no conditional added anywhere** — not here, not at the
    call site — because the guard the value flows through was already the one it
    needed. `agent_latency` is the second demonstration and it was the stated
    prediction: task 15.1 re-sourced `_get_agent_latency` per D1 to a read that
    answers `None` on either window, and its trend began answering
    `TREND_NOT_ESTABLISHED` through this same guard with nothing added. Passing
    a value through a guard that is a no-op for it today is not dead code; it is
    the difference between a later task changing one function and a later task
    having to remember to add a null test here.
    """
    if current is None:
        return TREND_NOT_ESTABLISHED
    return _compute_trend(current, previous)


def _compute_trend(current: float, previous: float | None) -> str:
    """Return the direction of the raw change: 'up', 'down', 'stable', or
    ``TREND_NOT_ESTABLISHED``.

    Called only through :func:`_reported_trend`, which is what guarantees
    ``current`` is a number: an absent current window is answered there, so
    this function is the arithmetic and nothing else.

    p39 R3.1/R3.5: this reports the *direction* of the change and applies no
    polarity transformation. Whether an increase is good or bad news is the
    frontend's to decide, keyed on the trend field name, and stated in text as
    well as in colour (`MetricsDashboardPage.tsx`, LGR-99050e6-1). This function
    previously took an `invert` flag that two call sites passed, so a rise in
    cost or agent latency was reported as `down` and then re-inverted on
    display — wrong in both label and colour. Do not reintroduce it.

    `previous is None` means the previous window established **no baseline** —
    an absent or unreadable source, not a source that read zero. R4.4 answers
    that with `TREND_NOT_ESTABLISHED` and never `stable`: "there is nothing to
    compare against" and "the comparison came out level" are different facts,
    and an operator who cannot tell them apart reads an unmeasured metric as a
    steady one. LGR-4854b3d-1 (template / ratified) is the fence — a
    three-state value is never collapsed to its zero, and the collapsing
    expression lives in exactly one place, which for a trend is here.

    A **measured** `previous` of 0 is a baseline (R4.5), so it never answers
    `TREND_NOT_ESTABLISHED`. Before p39 both cases arrived at the same
    `"stable"`, which is the collapse LGR-4854b3d-1 forbids.
    """
    if previous is None:
        return TREND_NOT_ESTABLISHED
    if previous == 0:
        # An ABSOLUTE rule, not an arithmetic one: the relative-change formula
        # below divides by `abs(previous)`, so it cannot be applied to a zero
        # denominator and cannot produce this answer. R4.5 states the rule
        # directly instead — something this window where there was nothing last
        # window is a rise, and nothing either window has not moved.
        #
        # No `down` arm: all four trend entries in `_build_comprehensive_metrics`
        # (`ingestion`, `success`, `agent_latency`, `cost`) pass a non-negative
        # measurement — a summed record estimate, a percentage, milliseconds
        # and USD — so `current` below zero is unreachable and `else` is
        # `current == 0`.
        if current > 0:
            return "up"
        return "stable"
    change = (current - previous) / abs(previous)
    if abs(change) < 0.05:
        return "stable"
    return "up" if change > 0 else "down"


def _health_from_utilization(pct: float) -> str:
    """Map utilization percentage to health status."""
    if pct < 60:
        return "healthy"
    if pct < 85:
        return "warning"
    return "critical"


def _health_from_dynamo_consumed(read_per_hour: float, write_per_hour: float) -> str:
    """Map consumed capacity units per hour to health status."""
    total = read_per_hour + write_per_hour
    if total < 5000:
        return "healthy"
    if total < 20000:
        return "warning"
    return "critical"


def _health_from_availability(pct: float) -> str:
    """Map an availability percentage to a health status."""
    if pct >= 99:
        return "healthy"
    if pct >= 95:
        return "warning"
    return "critical"
