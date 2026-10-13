# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import boto3

# Initialize logger first
from aws_lambda_powertools import Logger
from strands import Agent

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for EventBridge publisher
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from agent_trace_plugin import AgentTracePlugin

initialize_strands_agent_with_mcp: Callable[..., Any] | None
try:
    from strands_mcp_factory import initialize_strands_agent_with_mcp
except ImportError:
    logger.warning("strands_mcp_factory not available, MCP initialization will fail")
    initialize_strands_agent_with_mcp = None

write_agent_audit_entry: Callable[..., Any] | None
try:
    from audit_utils import write_agent_audit_entry
except ImportError:
    logger.warning("audit_utils not available, agent audit entries will be skipped")
    write_agent_audit_entry = None

try:
    from env_utils import get_aws_account_id, require_env
except ImportError:
    # Layer_Import_Fallback: fix path + import the canonical resolver — do not
    # redefine it inline (P21 Req 5.3/5.4/5.7).
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import get_aws_account_id, require_env


load_prompt_template: Callable[..., Any] | None
try:
    from prompt_template_loader import load_prompt_template
except ImportError:
    logger.warning("prompt_template_loader not available")
    load_prompt_template = None

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    # Optional-import sentinel: rebinding an imported class to None is flagged by mypy
    # ([misc]/[assignment]); guarded at every use site with `is not None`.
    PipelineMetrics = None  # type: ignore[misc,assignment]

# Import standardized artifact schema
from artifact_schema import create_output_data

# P28 R19.2: single shared float→Decimal converter (replaces the local
# ``_convert_floats`` copy).
from decimal_utils import convert_floats_to_decimal
from dynamo_typing import item_dict, item_str
from eventbridge_utils import get_eventbridge_publishers

# Single source of truth for the Bedrock model ID (P21 Req 1).
from model_config import resolve_model_id

# p47 R12.4: the one pipelines-table record read. Sentinel, table, logger and
# classifier are all passed from here, so this module's contract (``None`` for a
# missing record — ``LGR-b2f79ef-8``) and every existing patch seam survive.
from pipeline_record_read import read_pipeline_record

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from sfn_task_token import step_task_token
from step_errors import StepFailure, raise_for_read_client_error, step_handler
from step_names import STEP_SEQUENCE, StepName

# p33 D5: the canonical target-schema read. Resolves bucket and key together so
# the pair cannot be split at this site (C1, ``LGR-7c9378d-1``) and a read
# failure arrives classified instead of flattened (requirement 2.8, C2).
from target_schema_resolution import resolve_target_schema

publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event = (
    get_eventbridge_publishers()
)

# Code Interpreter imports
from code_interpreter_runner import CodeInterpreterTestRunner

# P28 R13: structured review verdict (JSON schema) — the single parser and
# the authoritative gate input for review/regeneration decisions.
# P28b R1b: every verdict-derived decision consumes effective_verdict (the
# Issue_Attribution-calibrated gate), never the raw verdict directly.
from review_verdict import (
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
    VERDICT_FAIL,
    VERDICT_PASS,
    ReviewVerdictError,
    blocking_transform_block_findings,
    build_reparse_prompt,
    effective_verdict,
    parse_review_verdict,
    retained_nonblocking_findings,
    verdict_to_parsed_results,
)

# Environment variables
GENERATED_CODE_TABLE = require_env("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
TEST_RESULTS_TABLE = require_env("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
AGENTCORE_ENDPOINT = os.environ.get("AGENTCORE_ENDPOINT")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Shared bucket names (from CloudFormation)
TEST_OUTPUTS_BUCKET = os.environ.get("TEST_OUTPUTS_BUCKET", f"adi-test-outputs-{get_aws_account_id()}-{AWS_REGION}")

# Initialize AWS clients
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
generated_code_table = dynamodb.Table(GENERATED_CODE_TABLE)
test_results_table = dynamodb.Table(TEST_RESULTS_TABLE)
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)


def get_test_bucket_and_prefix(pipeline_id):
    """Get shared bucket and pipeline-specific prefix for test outputs"""
    return TEST_OUTPUTS_BUCKET, f"{pipeline_id}/test-outputs/"


def get_pipeline_metadata(pipeline_id: str) -> dict | None:
    """Get pipeline metadata including approval and target-schema configuration.

    Returns ``None`` — not ``{}`` — when the record is genuinely not found (p33
    design D8). The caller resolves the target schema from this record, and the
    two states it has to tell apart are "no pipeline record" (a lookup problem,
    p33 requirement 2.8 — fail the step) and "record exists and carries no
    target schema" (a legitimate state, requirement 2.6 — report not verified).
    ``{}`` cannot carry that distinction: it has no ``target_schema_s3_key``
    either, so a missing record would silently present as "no target schema".
    ``None`` matches the convention at ``schema_compare:351-366`` and
    ``schema_mapping:133-149``.

    P21 Req 6.4 is a **logging** requirement, and it lists ``None`` alongside
    ``{}`` and ``[]`` as acceptable degraded values, so returning ``None`` is
    compatible with it. What Req 6.4 actually requires is that a handler which
    *catches an exception* and returns a degraded value log at WARNING or above
    naming the exception. Do NOT drop that log: it is the only place a missing
    pipelines *table* stays distinguishable from a missing *record*, because
    ``ResourceNotFoundException`` is in ``NOT_FOUND_CLIENT_ERROR_CODES``
    (``shared/step_errors.py:54-62``) so the classifier returns normally for it
    and both paths now return ``None`` (D8 correction 2). The earlier docstring
    here credited Req 6.4 with mandating the ``{}`` return; it does not.

    **AMENDED by p47 E2 (R12.4), not weakened.** The ``get_item`` +
    ``raise_for_read_client_error`` + ``"Item" in response`` mechanism — and the
    ``logger.error`` this docstring said was "below" — now live once in
    ``shared/pipeline_record_read.read_pipeline_record``, which this function
    calls with ``not_found=None`` and **this module's** logger. So the log still
    fires on exactly the same path, at the same level, with the same message and
    the same logger name; ``LGR-b2f79ef-8``'s constraint is that it is not
    dropped, and a required ``logger`` parameter now makes dropping it
    impossible rather than merely discouraged. The ``None`` sentinel is passed in
    rather than assumed, because ``create_code`` passes ``{}`` and the shared
    mechanism must not pick between them (R12.4). The table is still constructed
    here, per call, and still handed in — that is what keeps
    ``patch.object(_mod, "dynamodb", …)`` working (R12.8).

    Infrastructure errors (throttles, access denied) raise instead of
    masquerading as missing metadata (P28 R17.3).
    """
    pipelines_table = dynamodb.Table(require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"))
    return read_pipeline_record(
        pipelines_table,
        pipeline_id,
        step_name="test_code",
        operation="get_pipeline_metadata",
        not_found=None,
        logger=logger,
        # Passed from this module's globals at call time so
        # `patch.object(_mod, "raise_for_read_client_error", ...)` still
        # intercepts — `test_get_pipeline_metadata_characterization.py`'s
        # "not reclassified on the way out" test depends on it, and would pass
        # vacuously if the classifier were resolved inside the shared module.
        classify=raise_for_read_client_error,
    )


def extract_transform_block(code: str) -> str:
    """Extract the AI-generated transform block from full Glue job code.

    Looks for the ``# === AI-Generated Transform Block ===`` markers that
    the code-generation Lambda inserts.  Falls back to returning the full
    code if markers are not found.
    """
    start_marker = "# === AI-Generated Transform Block ==="
    # P28b task 6.1 (live-verification fix): every pyspark_templates/*.j2
    # emits "# === End Transform Block ===" as the closing marker — the
    # previous "# === End AI-Generated Transform Block ===" never matched,
    # so extraction ran to end-of-file and the Code Interpreter converted
    # the trailing template scaffolding (DateType auto-cast, DQ, Iceberg
    # write) instead of just the AI transform, failing every run with a
    # ConversionError on scaffolding constructs.
    end_marker = "# === End Transform Block ==="

    start_idx = code.find(start_marker)
    if start_idx == -1:
        # No markers — return the whole code (best-effort)
        return code

    # Move past the marker line itself
    start_idx = code.index("\n", start_idx) + 1

    end_idx = code.find(end_marker)
    if end_idx == -1:
        return code[start_idx:]

    return code[start_idx:end_idx].rstrip()


#: Name of the check that records a pandas-proxy refusal (p32 2.20). The
#: transform never ran, so this is neither a pass nor a failure — it is a
#: statement that nothing was verified.
NOT_VERIFIED_CHECK_NAME = "not_verified_by_proxy"


def is_not_verified_check(check: dict[str, Any]) -> bool:
    """True for a check that verified nothing (p32 2.20).

    The discriminator is an explicit ``verified: False`` key carried ONLY by
    not-verified entries — a boolean ``passed`` cannot express three states,
    and adding a third field to every check would blunt the P28 R4.3
    guarantee that "passed" means every check passed.
    """
    return check.get("verified") is False


#: Name of the COMPUTED schema-compliance check (p33 D1). Deliberately distinct
#: from ``static_analysis:Schema Compliance``, which is the *reviewer's*
#: judgement parsed out of the verdict below: one is a column-set comparison
#: over what the transform actually produced, the other is an LLM opinion. Both
#: appear in the check list on purpose — do not merge them.
SCHEMA_COMPLIANCE_CHECK_NAME = "schema_compliance"

#: ``not_verified_reason`` for a schema-compliance entry that could not be
#: computed because the pipeline carries no target schema (p33 2.6 / D2).
NO_TARGET_SCHEMA_REASON = "no_target_schema"

#: ``not_verified_reason`` for the THIRD proxy refusal (p40 R4): the converter's
#: receiver-shape fence declined to apply a Series-only pandas member to a
#: scalar-literal emission. Named here, in the same style as
#: :data:`NO_TARGET_SCHEMA_REASON`, rather than imported from
#: ``code_interpreter_runner`` — which is where the producer-side name
#: (``NOT_VERIFIED_REASON_SCALAR_LITERAL``, itself an alias of
#: ``pyspark_to_pandas.REFUSAL_REASON_SCALAR_LITERAL``) lives.
#:
#: The import is not usable at THIS seam, and that was measured rather than
#: assumed: every test that imports this module stubs ``code_interpreter_runner``
#: with a bare ``MagicMock`` (24 files under ``tests/``), so an imported constant
#: arrives as an auto-attribute mock. Comparing a reason string against a mock
#: does not raise — it silently never matches — which would leave the arm below
#: dead in the whole suite while the tests still passed. A spelled string cannot
#: fail that way. It is a second spelling, so: change both or neither, the same
#: contract :data:`HARNESS_FIELD_COUNT_KEY` states for its own pair.
SCALAR_LITERAL_RECEIVER_REASON = "scalar_literal_receiver"

#: Name of the check that records a non-verification the TEST HARNESS caused —
#: a test frame that could not be built to carry every field the source schema
#: declares (p33 D12, requirement 2.11). It replaces
#: ``code_interpreter_execution`` the same way a proxy refusal does.
#:
#: Deliberately NOT ``not_verified_by_proxy`` (C14): that detail names a
#: construct and a line number, which a frame-width refusal does not have, so
#: reusing it would render "construct 'unknown construct' on line None" and
#: misattribute the cause to the pandas converter. The *state* is still
#: ``verified: False`` — this is a second reason, not a third state (C3).
#:
#: p42 R8.1 gave it a THIRD cause — a Glue interactive session refused because
#: the account was at its concurrent-session limit
#: (:data:`SESSION_CAPACITY_REASON`) — and p42 R7.3 a FOURTH, a test frame that
#: could not be built from the declared source types
#: (:data:`FRAME_UNBUILDABLE_REASON`). p44 R2.1 gives it a FIFTH, the run's own
#: source object failing to be read (:data:`SOURCE_READ_FAILED_REASON`). One
#: name, five reasons: what they share is that the harness could not get far
#: enough to run anything, which is exactly what this name says.
NOT_VERIFIED_BY_HARNESS_CHECK_NAME = "not_verified_by_harness"

#: Keys ``code_interpreter_runner._frame_truncation_results`` (p33 task 4.10)
#: carries so this function can name the field count and the ceiling as text.
#: This is the contract between the two: change both or neither.
#:
#: Carried by the two WIDTH reasons only. A capacity refusal has no field count
#: and no ceiling, so ``glue_session_runner.session_capacity_results`` omits both
#: rather than sending zeroes — see the harness branch in
#: :func:`aggregate_check_results`, which is why its opening clause is chosen by
#: reason instead of being one sentence for all three.
HARNESS_FIELD_COUNT_KEY = "not_verified_field_count"
HARNESS_FRAME_CEILING_KEY = "not_verified_frame_ceiling"

#: ``not_verified_reason`` for the THIRD harness reason (p42 R8.1): a Glue
#: interactive session could not be created because the account already held its
#: maximum number of them, so the transform never reached Spark.
#:
#: A new REASON, not a new field and not a new state — the same argument the
#: ``scalar_literal_receiver`` arm of :func:`aggregate_check_results` writes out
#: in full (``:508-527``), including ``LGR-b2f79ef-2`` having refused a third
#: check field once already. The check this produces is
#: :data:`NOT_VERIFIED_BY_HARNESS_CHECK_NAME` with ``passed: False`` +
#: ``verified: False`` (R8.2), so :func:`is_not_verified_check` and the
#: verified-check partition below are untouched, and R8.3 follows from the
#: existing ``verified: False`` discriminator rather than from new logic.
#:
#: Spelled here rather than imported from ``glue_session_runner``, for the reason
#: :data:`SCALAR_LITERAL_RECEIVER_REASON` records at length: every test that
#: imports this module stubs its sibling producers with a bare ``MagicMock``, so
#: an imported constant arrives as an auto-attribute mock and a comparison
#: against it silently never matches, leaving the arm below dead while the suite
#: still passes. It is a second spelling, so: change both or neither.
SESSION_CAPACITY_REASON = "session_capacity_exhausted"

#: ``not_verified_reason`` for the FOURTH harness reason (p42 R7.3, task 8.1): the
#: test frame could not be BUILT at all, because a declared source field carries a
#: type the frame builder has no Spark type for. Nothing ran and no session was
#: created.
#:
#: Distinct from the two WIDTH reasons on purpose. Those two render "the source
#: schema declares N field(s) against a test-frame width ceiling of M" — and here
#: the schema is present and inside the ceiling, so that sentence would be a false
#: claim about the schema made while removing a false claim about the transform.
#: It therefore gets its own opening clause below, exactly as
#: :data:`SESSION_CAPACITY_REASON` does, and carries neither
#: :data:`HARNESS_FIELD_COUNT_KEY` nor :data:`HARNESS_FRAME_CEILING_KEY`.
#:
#: Spelled here rather than imported from ``glue_session_runner``, for the reason
#: :data:`SCALAR_LITERAL_RECEIVER_REASON` records at length. It is a second
#: spelling of ``glue_session_runner.NOT_VERIFIED_REASON_FRAME_UNBUILDABLE``, so:
#: change both or neither.
FRAME_UNBUILDABLE_REASON = "test_frame_unbuildable"

#: ``not_verified_reason`` for the FIFTH harness reason (p44 R2.1, R2.2, R3.4): the
#: run's own source object could not be read, so the transform never executed.
#: Reachable only where the Frame_Source is a Sample_Frame — from a frame-build
#: statement that failed, or from one that never reached a terminal state inside
#: the statement deadline.
#:
#: Distinct from :data:`FRAME_UNBUILDABLE_REASON` on purpose, and the distinction
#: is the whole point of the reason existing. That one means the harness could not
#: represent a declared type and nothing was spent — no session existed. This one
#: means a session was created, billed, and the object could not be opened: the
#: action is to check the object and the session's read access, so
#: ``FRAME_UNBUILDABLE_REASON``'s label ("could not build a test frame from the
#: declared source types") would be a false claim about the schema. Like capacity
#: and frame-unbuildable it therefore gets its own opening clause below, and it
#: carries neither :data:`HARNESS_FIELD_COUNT_KEY` nor
#: :data:`HARNESS_FRAME_CEILING_KEY` — there is no width problem here.
#:
#: Also not the stage label: ``glue_session_runner.STAGE_FRAME_BUILD`` is
#: ``"test data generation"``, and "test data generation failed" is precisely the
#: misattribution R2.1 exists to remove, so the arm below names the source read
#: and the object instead.
#:
#: A new REASON, not a new field and not a new state, exactly as the two above:
#: ``LGR-b2f79ef-2`` refused a third check FIELD once already, so
#: :func:`is_not_verified_check` and the verified-check partition are untouched —
#: the existing ``verified: False`` discriminator already does that work.
#:
#: Spelled here rather than imported from ``glue_session_runner``, for the reason
#: :data:`SCALAR_LITERAL_RECEIVER_REASON` records at length. It is a second
#: spelling of ``glue_session_runner.NOT_VERIFIED_REASON_SOURCE_READ_FAILED``, so:
#: change both or neither.
SOURCE_READ_FAILED_REASON = "source_read_failed"

#: The two check names that REPLACE ``code_interpreter_execution`` instead of
#: reporting it (p40 R6). Both are appended from an ``elif`` arm of the same
#: if/elif/else in :func:`aggregate_check_results` that would otherwise append
#: ``code_interpreter_execution``, so either name appearing in a check list
#: means no transform ran. Defined here, immediately after the two names, so
#: the set cannot drift from them.
EXECUTION_REPLACING_CHECK_NAMES: Final = frozenset({NOT_VERIFIED_CHECK_NAME, NOT_VERIFIED_BY_HARNESS_CHECK_NAME})

#: Short labels for the ``not_verified_reason`` values an execution-replacing
#: check can carry — the three proxy reasons (p32 2.23's two plus p40 R4's
#: :data:`SCALAR_LITERAL_RECEIVER_REASON`) and the five harness reasons (p33's
#: two width reasons plus p42 R8.1's :data:`SESSION_CAPACITY_REASON`, p42
#: R7.3's :data:`FRAME_UNBUILDABLE_REASON` and p44 R2.1's
#: :data:`SOURCE_READ_FAILED_REASON`). They
#: are the text R6 states at the two sites that carry prose: the
#: ``verification_note`` on ``test_result`` and the handler's ``message``.
#:
#: :data:`NO_TARGET_SCHEMA_REASON` is deliberately ABSENT. It is a
#: ``verified: False`` reason, but it belongs to a run that executed, so it
#: never reaches this map — see :func:`execution_was_verified`.
#:
#: Every OTHER declared reason must be present, and that is enforced rather than
#: remembered: ``tests/contracts/test_not_verified_reason_labels_cross_language.py``
#: asserts that the declared-reason set minus this map's keys is exactly
#: ``{no_target_schema}``, and separately that every declared reason has a
#: dashboard label in ``ApprovalArtifact.tsx``'s ``NOT_VERIFIED_LABELS``. A reason
#: added to a producer without both entries degrades a live screen to the bare
#: string "not verified" — which is how p33's two harness reasons shipped
#: unlabelled for a release.
NOT_VERIFIED_REASON_LABELS: Final[dict[str, str]] = {
    "permanently_inexpressible": "the pandas proxy can never express this construct",
    "not_yet_expressible": "the pandas proxy cannot yet express this construct",
    SCALAR_LITERAL_RECEIVER_REASON: "the pandas proxy declined a column operation applied to a constant",
    "test_frame_truncated": "the test harness could not build a frame carrying every declared field",
    "no_source_schema": "the test harness had no source schema to build a test frame from",
    SESSION_CAPACITY_REASON: "the test harness could not obtain a Spark session — the account was at its limit",
    FRAME_UNBUILDABLE_REASON: "the test harness could not build a test frame from the declared source types",
    SOURCE_READ_FAILED_REASON: "the test harness could not read the run's own source object",
}


# ──────────────────────────────────────────────────────────────────────────
# Verification_Executor selection (p42 R2)
# ──────────────────────────────────────────────────────────────────────────

#: The environment variable naming the executor. Set to ``'glue'`` in BOTH
#: ``compute-stack.ts`` (the code-testing Lambda) and ``agentcore-stack.ts``'s
#: ``agentContainerEnv``. It is still set in both, and both copies are still
#: pinned by ``tests/cdk/stacks/test_verification_executor_env.py`` — but the
#: REASON has changed and the fence no longer guards what it used to.
#:
#: ``LGR-87b8538-1`` (**template / ratified**) rejected setting only
#: ``compute-stack.ts``, because with the code default at ``pandas`` a
#: one-sided set "yields an inert flag on the live path while every test
#: passes": this handler's first branch dispatches to the AgentCore container
#: when ``USE_AGENTCORE_RUNTIME`` is ``'true'``, so the runner never runs in
#: the Lambda process on the live path, and the container would have kept
#: reading an unset variable and resolving to ``pandas``.
#:
#: **That is no longer the failure mode.** The code fall-through is now
#: ``glue`` (see :func:`resolve_verification_executor`), so a stack that omits
#: the variable entirely gets ``glue`` anyway. Omitting it no longer downgrades
#: a run, and the two-sided CDK test is therefore no longer what catches the
#: omission — it now pins only that the deployed value is *stated* rather than
#: inherited, which is worth keeping (a reader of either stack can see which
#: executor is intended, and a future change back to ``pandas`` has to touch
#: both sides) but is a weaker guarantee than the record describes. Recorded
#: here rather than left as a fence that silently guards nothing.
VERIFICATION_EXECUTOR_ENV: Final = "VERIFICATION_EXECUTOR"

#: The environment variable naming the Frame_Mode — which frame a Glue
#: verification measures the transform against (p44 R1.1, R1.3).
#:
#: **SET BY BOTH STACKS**, in ``compute-stack.ts`` (the code-testing Lambda) and
#: in ``agentcore-stack.ts``'s ``agentContainerEnv``, for exactly the reason
#: :data:`VERIFICATION_EXECUTOR_ENV` records above: this handler's first branch
#: dispatches to the AgentCore container when ``USE_AGENTCORE_RUNTIME`` is
#: ``'true'``, so the runner never runs in the Lambda process on the live path
#: and a value set only on the Lambda is inert. ``LGR-87b8538-1`` (**template /
#: ratified**) rejected the one-stack form in its own words: setting only
#: ``compute-stack.ts`` "yields an inert flag on the live path while every test
#: passes". The obvious stack is the inert one.
#:
#: Unlike ``VERIFICATION_EXECUTOR``, the fence here is not weakened by the code
#: fall-through: an unset variable resolves to a Synthetic_Frame, so a stack that
#: omits the variable verifies against generated rows — a real downgrade in what
#: the run proves, which is what p44 exists to remove.
#:
#: Read at the point of construction rather than at import (R1.3); see the
#: comment at the ``glue`` branch's runner construction.
VERIFICATION_FRAME_MODE_ENV: Final = "VERIFICATION_FRAME_MODE"

#: A frame generated from the declared source schema — what a run gets when the
#: Frame_Mode is unset.
#:
#: Spelled here rather than imported from ``glue_session_runner``, for the reason
#: :data:`SCALAR_LITERAL_RECEIVER_REASON` records at length. It is a second
#: spelling of ``glue_session_runner.FRAME_MODE_SYNTHETIC``, so: change both or
#: neither.
#:
#: This is the DEFAULT ONLY, and deliberately not a validated vocabulary. The
#: recognised pair lives in one classifier —
#: ``GlueSessionTestRunner.resolve_frame_source`` — and a second inference path
#: here would be the defect, not the safeguard: a value outside the pair reaches
#: that classifier verbatim, falls through to a synthetic frame and is named in a
#: WARNING. ``LGR-ad78946-1`` (**template / ratified**) rejected normalising the
#: sibling ``VERIFICATION_EXECUTOR`` value on the same grounds — "normalising
#: removes the only signal that the deployed value is not the intended one" —
#: and the argument transfers unchanged. **Do not add ``.strip()``, ``.lower()``
#: or a membership check at the read site.**
FRAME_MODE_SYNTHETIC_DEFAULT: Final = "synthetic"

#: The pandas proxy: ``code_interpreter_runner.CodeInterpreterTestRunner``.
#:
#: **No longer the fall-through, and no longer where a misconfigured run
#: lands.** It is reached by exactly two routes now:
#:
#: 1. An explicit, exactly-spelled ``VERIFICATION_EXECUTOR=pandas`` — the
#:    deliberate opt-out.
#: 2. :func:`resolve_effective_executor`'s downgrade when ``glue`` is the
#:    resolved request and ``VERIFICATION_SESSION_ROLE_ARN`` is unset. That
#:    one is not a policy choice and did not move: ``role_arn`` is keyword-only
#:    and required on ``glue_session_runner.GlueSessionTestRunner``, so there
#:    is no session to create (``LGR-ad78946-5``, **template / ratified**).
#:
#: Everything else — unset, empty, a typo, a casing slip, a whitespace
#: near-miss — now resolves to ``glue``, because the user's decision is that
#: verification is glue-first. A misspelled variable therefore creates a
#: billable Glue interactive session instead of degrading to the cheap
#: executor; that cost was accepted explicitly and is recorded at the
#: fall-through itself.
#:
#: **The R11.2 deviation still stands and is still a deviation.** R11.2 held
#: ``pandas`` WHILE the concurrent-session ceiling was unmeasured, and it is
#: still unmeasured: the p42 task 1.3 probe
#: (``tests/e2e/test_glue_session_concurrency_probe.py``) was written and
#: deliberately never run, because running it bills. Deploying ``glue`` was
#: already an accepted deviation rather than a satisfaction of R11.2
#: (``LGR-87b8538-1``); making ``glue`` the code default widens it, because
#: R2.3's fall-through to ``pandas`` was the second layer that record cited as
#: unchanged, and that layer is now gone. What keeps the widened deviation
#: tolerable is the same fact as before: exhausting the ceiling surfaces as a
#: capacity refusal, which the harness maps to the
#: ``session_capacity_exhausted`` non-verification, so an affected run reports
#: "not verified" rather than failing.
VERIFICATION_EXECUTOR_PANDAS: Final = "pandas"

#: Real Spark, via ``glue_session_runner.GlueSessionTestRunner``.
#:
#: Also the fall-through: this is what an unset, empty or unrecognised
#: configuration value resolves to (:func:`resolve_verification_executor`).
VERIFICATION_EXECUTOR_GLUE: Final = "glue"

#: Pandas authoritative, Glue additionally, and the disagreement recorded
#: (R4.1). Resolved here; the Glue half is orchestrated by
#: :func:`record_shadow_verification` (p42 task 10.3), which the handler calls
#: after the verdict is already computed.
VERIFICATION_EXECUTOR_SHADOW: Final = "shadow"

#: The whole vocabulary. Three values and not two booleans: two booleans cannot
#: express three states, and ``shadow`` is a third state rather than a modifier
#: of the other two. That is the same argument :func:`is_not_verified_check`
#: makes about ``passed`` (`:215-223` — "a boolean cannot express three
#: states"), reused here so the file keeps one idiom for this shape.
VERIFICATION_EXECUTORS: Final = frozenset(
    {VERIFICATION_EXECUTOR_PANDAS, VERIFICATION_EXECUTOR_GLUE, VERIFICATION_EXECUTOR_SHADOW}
)

#: ARN of the Verification_Session_Role — the dedicated read-only role a Glue
#: interactive session assumes (R6.4). The role itself is created by the
#: Foundation stack with the fixed physical name
#: ``AgenticDataIngestion-VerificationSession-Role``; the ARN is passed as an
#: environment variable rather than reconstructed from account and region here,
#: because reconstructing it would encode a CDK physical name in Python and
#: silently produce a valid-looking ARN for a role that does not exist.
#:
#: **SET BY BOTH STACKS**, in ``compute-stack.ts`` and in
#: ``agentcore-stack.ts``'s ``agentContainerEnv``, for exactly the reason
#: :data:`VERIFICATION_EXECUTOR_ENV` records: the live path runs in the
#: container, so a value set only on the Lambda is inert. Both copies are pinned
#: by ``tests/cdk/stacks/test_verification_session_role_arn_env.py``.
#:
#: This is now LOAD-BEARING rather than latent. It used to only make ``glue``
#: *selectable* while ``VERIFICATION_EXECUTOR`` stayed ``pandas``; the executor
#: has since been flipped to ``glue``, so if this variable goes missing,
#: :func:`resolve_effective_executor` downgrades every run back to the pandas
#: proxy and logs at ERROR — the deployed verification path would silently
#: revert.
VERIFICATION_SESSION_ROLE_ARN_ENV: Final = "VERIFICATION_SESSION_ROLE_ARN"


def resolve_verification_executor(raw: str | None) -> str:
    """Resolve the Verification_Executor from its raw configured value (R2.1-R2.4).

    Total over ``str | None``: every input resolves to a member of
    :data:`VERIFICATION_EXECUTORS`, and anything not recognised resolves to
    :data:`VERIFICATION_EXECUTOR_GLUE`.

    **Glue is the fall-through, reversing R2.3.** Verification is glue-first:
    real Spark is what a run gets unless something explicitly and exactly asks
    for one of the other two. ``pandas`` and ``shadow`` are reachable only by
    their own exact spelling. Two consequences, both deliberate:

    * An unset variable is now the documented normal state, not a
      misconfiguration, so it stays SILENT — warning on it would put a line in
      every correctly-deployed run's log, which is what trains a reader to
      ignore the one case R2.4 cares about.
    * An unrecognised value — empty string, typo, casing slip, whitespace
      near-miss — resolves to ``glue`` and **still earns its WARNING**. The
      cost of that combination was accepted explicitly rather than discovered:
      **a misspelled ``VERIFICATION_EXECUTOR`` now creates a billable Glue
      interactive session** instead of degrading to the cheap pandas proxy.
      Keeping unrecognised values on ``pandas`` was the recommended option and
      was overruled on the grounds that "it should always be glue first". The
      WARNING is what keeps the misconfiguration visible rather than merely
      expensive, so it is load-bearing here in a way it was not before.

    **Exact string comparison, no normalisation** (``LGR-ad78946-1``,
    **template / ratified**). This follows the ``USE_AGENTCORE_RUNTIME``
    precedent in this file — ``os.environ.get(...) == "true"``, a plain compare
    against an explicit string — so ``"GLUE"``, ``" glue"``, ``"SHADOW"`` and
    ``"shadow "`` are NOT accepted. ``LGR-ad78946-1`` rejected
    case-insensitive matching and ``.strip()`` because "normalising removes the
    only signal that the deployed value is not the intended one", and that
    argument is unaffected by which value the fall-through lands on. **Do not
    add ``.strip()`` or ``.lower()``:**
    ``tests/unit/lambdas/test_code/test_executor_selection_totality_prop.py``
    generates casing and whitespace near-misses (including ``\\u00a0`` and
    ``\\u2009``, which ``str.strip()`` also removes) specifically to break if
    someone does.

    Note where the fence now bites differently: ``"shadow "`` still does NOT
    resolve to ``shadow``. It resolves to ``glue`` — the fall-through — rather
    than to the trimmed member. The refusal is intact; only the landing spot
    moved.

    The log uses ``%r`` deliberately: a trailing space is invisible under
    ``%s``, and an invisible typo is the case this warning exists for.
    """
    if raw is None:
        return VERIFICATION_EXECUTOR_GLUE
    if raw in VERIFICATION_EXECUTORS:
        return raw
    logger.warning(
        "%s=%r is not one of %s — verifying on real Spark (a Glue interactive "
        "session, which bills) because that is the default",
        VERIFICATION_EXECUTOR_ENV,
        raw,
        sorted(VERIFICATION_EXECUTORS),
    )
    return VERIFICATION_EXECUTOR_GLUE


def resolve_effective_executor(raw: str | None, session_role_arn: str | None) -> str:
    """The executor the call site will actually build, role availability included.

    :func:`resolve_verification_executor` answers "what was asked for". This
    answers "what can be built", which is the question the call site has. They
    differ in exactly one place: ``glue`` was requested and there is no
    Verification_Session_Role ARN to pass, so there is no session to create
    (``role_arn`` is keyword-only and required on
    ``glue_session_runner.GlueSessionTestRunner``).

    That downgrade is logged at ERROR, not WARNING, and the distinction is the
    point rather than a detail (``LGR-ad78946-5``, **template / ratified**).
    R2.4's WARNING means "this value is not in the vocabulary" — a typo. A
    ``glue`` request with no role ARN is a request the deployment cannot
    satisfy, which is an operator-visible misconfiguration; logging both at the
    same level would make them indistinguishable in the log.

    **The two diagnostics are no longer mutually exclusive, and that is new.**
    Under the old ``pandas`` fall-through an unrecognised value had already
    become ``pandas`` by the time the role guard ran, so it could not reach the
    ERROR arm — one call emitted at most one line. Now an unrecognised value
    resolves to ``glue``, so a typo on a stack with no
    ``VERIFICATION_SESSION_ROLE_ARN`` emits BOTH: the WARNING naming the
    unrecognised value and the ERROR naming the missing role. They stay
    attributable — different levels, different messages, each naming its own
    variable — but a reader can now see both from one resolution, and the
    property that asserted exclusivity has been amended to say so rather than
    weakened to hide it.

    Falling back rather than raising keeps the step verifying something instead
    of failing on configuration, and it does not overstate what ran — the
    returned value is the label the log and the report carry, so a downgraded
    run reports ``pandas``.

    **Known deviation from R2.1's literal wording**, recorded rather than
    hidden: R2.1 says a Verification_Executor of ``glue`` SHALL verify with the
    Glue_Session_Runner. This returns ``pandas`` for ``glue`` when no role ARN
    is configured. The AC presupposes a deployable configuration; the
    alternative — raising — turns a missing environment variable into a failed
    pipeline run, which is a worse answer than verifying on the proxy and
    saying so at ERROR.

    **This downgrade deliberately did NOT move to ``glue`` when the
    fall-through did.** It is not a policy choice and so was not the user's to
    reverse: ``role_arn`` is keyword-only and required on
    ``GlueSessionTestRunner``, so with no ARN there is no session to create and
    ``pandas`` is the only executor that can run. Its blast radius did grow,
    though, because absence now resolves to ``glue``: a stack with neither
    variable set reaches this ERROR instead of quietly running the proxy.
    """
    requested = resolve_verification_executor(raw)
    if requested == VERIFICATION_EXECUTOR_GLUE and not session_role_arn:
        logger.error(
            "%s=%r but %s is unset — no runtime role to pass to a Glue session, "
            "so verification falls back to the pandas proxy",
            VERIFICATION_EXECUTOR_ENV,
            VERIFICATION_EXECUTOR_GLUE,
            VERIFICATION_SESSION_ROLE_ARN_ENV,
        )
        return VERIFICATION_EXECUTOR_PANDAS
    return requested


# ══════════════════════════════════════════════════════════════════════════
# Shadow mode — the second, NON-AUTHORITATIVE Glue run (R4, design §4.7)
# ══════════════════════════════════════════════════════════════════════════
#
# Under ``VERIFICATION_EXECUTOR=shadow`` the pandas proxy runs first and IS the
# verdict (R4.1 — ``test_status`` and the check list derive from it alone).
# Everything below runs *afterwards*, records where the two executors
# disagreed, and is forbidden from changing anything the step returns.
#
# Three mechanisms hold that, and they are three because no one of them would
# be enough on its own:
#
# 1. **A separate state row** (R4.3). The comparison is written to
#    ``(run_id, "shadow_verification")`` by ``PutItem`` — never onto the
#    ``(run_id, "test_code")`` row. Two reasons, one per record.
#    ``LGR-60c4985-4`` (template / ratified) establishes that ``output_data``
#    on a state row is fixed to five keys by
#    ``shared/artifact_schema.create_output_data`` (``artifact_type``,
#    ``primary_id``, ``status``, ``summary``, ``s3_uris``) and that reading a
#    value out of it that it does not carry is how a live JSONPath failure was
#    reproduced; a comparison payload is none of those five. ``LGR-9e76249-3``
#    (template / ratified) chose a separate ``(run_id, "load_metrics")`` row
#    over an ``UpdateItem`` on an existing row for the adjacent reason that the
#    ``UpdateItem`` would have clobbered the row carrying the run's terminal
#    status. Same shape, one domain over. And a separate row makes R4.3 hold
#    **by construction** rather than by discipline: nothing in the gate reads
#    that key, so shadow mode cannot reach a verdict even by mistake.
# 2. **Fail-soft** (R4.4). ``LGR-60c4985-5`` (template / ratified) states the
#    discipline for the analogous resume hooks — "Both halves … **fail soft**:
#    neither can turn a working step into a failing one." Here the guarantee is
#    the ``except Exception`` at :func:`record_shadow_verification`'s outermost
#    level: it returns a label and never raises, so no Glue-side exception,
#    deadline overrun or capacity refusal can alter ``test_status``, the check
#    list or the return value. A second, inner arm around the Glue run
#    *classifies* rather than guards — see that function's docstring for why the
#    two are not the same catch written twice.
# 3. **Its own wall-clock deadline** (R4.6). Design C3: TestCode is a
#    ``lambda:invoke.waitForTaskToken`` state, and ``LGR-60c4985-3`` (template
#    / ratified) records that such states are deliberately excluded from
#    resume caching precisely because a short-circuit would return before the
#    token is sent and the state would then hang for its **full**
#    ``TimeoutSeconds`` — 1200 s. So a stalled shadow run does not fail fast.
#    Shadow doubles the step's wall clock; the deadline is what stops that
#    from being unbounded.
#
# **Inline, never on a thread.** ``LGR-b46a5bb-1`` (standing invariant) forbids
# ``threading.Thread`` for post-response work anywhere in ``lambda/**``: the
# Lambda environment freezes on return and kills the thread. The Glue half
# therefore runs inline, before the handler returns — which is also why it
# needs a deadline rather than a background lifetime.

#: ``step_name`` of the row the Shadow_Comparison is written to (R4.3).
#:
#: **A record row, not a Step_Row** — the same category as ``load_metrics``,
#: ``data_quality`` and ``cache``. Two readers classify state rows and they
#: disagree in kind, so each is worth stating separately:
#:
#: * ``execution_updater`` is an **allowlist** keyed on ``shared/step_names.py``
#:   (``execution_updater/lambda_function.py:116-126``): a name that is not a
#:   step is routed away before any status is derived. This name is safe there
#:   by construction and needs no change.
#: * The dashboard run-detail reader is a **denylist** —
#:   ``_synthetic_steps`` in ``dashboard_api/handlers/pipelines_runs.py`` —
#:   whose own comment records that "a future record type has to be added here
#:   by hand". **This name is not in it yet.** No live run writes this row
#:   (``VERIFICATION_EXECUTOR`` defaults to ``pandas`` and no deployed
#:   environment sets ``shadow``), so there is no consequence today, but adding
#:   the key is a **prerequisite for enabling shadow mode**: without it the row
#:   renders as a trailing step with an unknown status, which is exactly the
#:   live regression ``load_metrics`` produced before p37 added its key.
#:
#: Spelled as a module constant and deliberately not inline. It is not a
#: :class:`StepName` and ``step_names.from_any`` raises on it, while the
#: canonical-step-name sweep (``tests/unit/lambdas/test_canonical_state_rows_sweep.py``)
#: reads step names out of this file's dict literals by AST and requires every
#: *resolvable* one to be this writer's own canonical name. A ``Name`` node is
#: not resolvable, and the item below carries neither ``status`` nor
#: ``step_sequence``, so the sweep correctly does not see this row as a
#: Step_Row — which is what it is not.
SHADOW_VERIFICATION_STEP_NAME: Final = "shadow_verification"

#: Wall-clock budget for the shadow half, in seconds, measured from the start
#: of the step (R4.6). Configurable so an operator running shadow mode can tune
#: it without a code change; unset is the normal case.
SHADOW_DEADLINE_SECONDS_ENV: Final = "SHADOW_VERIFICATION_DEADLINE_SECONDS"

#: 900 s of TestCode's 1200 s ``TimeoutSeconds``
#: (``orchestration-stack.ts``'s ``CONTAINER_STEP_TIMEOUT_SECONDS``), leaving
#: 300 s for the handler's own work after the shadow half. Deliberately NOT the
#: full 1200: design C3 says in as many words that the step's timeout is not
#: headroom, because the same step already runs a Strands review agent with a
#: reparse retry — up to two model invocations — before the executor starts.
DEFAULT_SHADOW_DEADLINE_SECONDS: Final = 900

#: The Glue runner's own worst case, summed from its per-stage budgets:
#: ``_SESSION_READY_DEADLINE_SECONDS`` 180 + 3 × ``_STATEMENT_DEADLINE_SECONDS``
#: 120 + ``_RELEASE_DEADLINE_SECONDS`` 120 = 660 (``glue_session_runner.py``'s
#: "Time budgets" block, which does the same arithmetic there).
#:
#: A second spelling of numbers that live in that module, and it is one on
#: purpose: importing them here would import a Glue client on the default path,
#: which the function-local import below exists to avoid. The drift is fenced by
#: a test that imports both and asserts this bound still covers them, so the two
#: cannot disagree silently.
#:
#: What it is *for*: the deadline can only be enforced where this handler holds
#: control — before the Glue half starts and after it returns — because
#: preempting it would need a thread, and ``LGR-b46a5bb-1`` forbids one here.
#: So the pre-flight check asks whether the runner's whole worst case still fits
#: inside the remaining budget. Without that, a shadow run begun one second
#: before the deadline could still add 660 s to a step that does not fail fast.
SHADOW_GLUE_WORST_CASE_SECONDS: Final = 660

#: Both executors produced a result and the comparison in the row is theirs.
SHADOW_STATUS_COMPARED: Final = "compared"

#: The Glue half produced nothing — it raised, or was refused for capacity.
#: The row is still written, with the comparison the pure function returns for a
#: ``None`` Glue side, because "shadow ran and got nothing" and "shadow never
#: ran" are different facts and must not read the same (``LGR-28b4d75-3``'s
#: argument for writing an empty-fleet row, and ``LGR-b2f79ef-8``'s for not
#: collapsing "missing" into "empty").
SHADOW_STATUS_GLUE_PRODUCED_NOTHING: Final = "glue_produced_nothing"

#: The step had already spent its budget, so the Glue half was never started
#: (R4.6). Distinct from the label below: nothing was spent here.
SHADOW_STATUS_NOT_STARTED_DEADLINE: Final = "not_started_deadline"

#: The Glue half returned, but past the deadline, so its result is discarded
#: rather than compared (R4.6).
SHADOW_STATUS_ABANDONED_DEADLINE: Final = "abandoned_deadline"

#: ``shadow`` was requested with no Verification_Session_Role ARN configured.
#: The pandas half is unaffected — it needs no role, which is why
#: :func:`resolve_effective_executor` deliberately does not downgrade ``shadow``
#: — so this declines the Glue half cleanly instead of raising.
SHADOW_STATUS_NO_SESSION_ROLE: Final = "no_session_role"

#: The fail-soft arm fired: something in the shadow half raised where nothing
#: was expected to. Returned rather than raised (R4.4).
SHADOW_STATUS_ERROR: Final = "error"


def resolve_shadow_deadline_seconds(raw: str | None) -> int:
    """Resolve the shadow wall-clock budget from its raw configured value.

    Total over ``str | None``: absent, empty, non-numeric or non-positive all
    resolve to :data:`DEFAULT_SHADOW_DEADLINE_SECONDS`, following the same
    posture :func:`resolve_verification_executor` takes — an unusable value
    falls through to this function's own documented default and says so, rather
    than failing a run over a configuration typo. (That resolver's default is
    now ``glue`` rather than "the existing behaviour"; the shared posture is
    fall-through-and-warn, not the particular value.) A non-positive value is
    refused rather than honoured because zero would disable the shadow half by a
    route that looks
    like a deadline setting; disabling it is what ``VERIFICATION_EXECUTOR`` is
    for.
    """
    if raw is None:
        return DEFAULT_SHADOW_DEADLINE_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer — using the default shadow deadline of %ss",
            SHADOW_DEADLINE_SECONDS_ENV,
            raw,
            DEFAULT_SHADOW_DEADLINE_SECONDS,
        )
        return DEFAULT_SHADOW_DEADLINE_SECONDS
    if seconds <= 0:
        logger.warning(
            "%s=%r is not positive — using the default shadow deadline of %ss",
            SHADOW_DEADLINE_SECONDS_ENV,
            raw,
            DEFAULT_SHADOW_DEADLINE_SECONDS,
        )
        return DEFAULT_SHADOW_DEADLINE_SECONDS
    return seconds


def shadow_glue_run_fits(elapsed_seconds: float, deadline_seconds: int) -> bool:
    """True when the Glue half's whole worst case still fits in the budget.

    The pre-flight half of R4.6, and the half with teeth. See
    :data:`SHADOW_GLUE_WORST_CASE_SECONDS` for why the question is "does the
    worst case fit" rather than "is there any budget left": the deadline cannot
    preempt a call in progress, so the only moment a 660 s overrun can be
    prevented is before it starts.

    Pure and takes the elapsed time as a number rather than reading a clock, so
    the boundary is testable without waiting for it.
    """
    return elapsed_seconds + SHADOW_GLUE_WORST_CASE_SECONDS <= deadline_seconds


def _finite_floats_only(value: Any) -> Any:
    """Replace non-finite floats with a string, recursively.

    :func:`convert_floats_to_decimal` handles the ordinary float case — and it
    has to, because DynamoDB rejects a Python ``float`` outright, which is the
    trap ``shadow_comparison.compare_results`` names for this writer: the
    comparison passes executor values through untouched, so ``schema_compliance``
    arrives as a ``float``. But ``Decimal("nan")`` and ``Decimal("Infinity")``
    are equally unwritable, so a non-finite value would turn a fail-soft path
    into a path that fails to record anything at all.

    Not expected to fire: no compared dimension is a value either executor
    computes as non-finite, and ``shadow_comparison.values_agree`` treats two
    NaNs as agreeing so a matched pair never reaches an entry. It is here
    because the cost of being wrong about that is losing the whole record, and
    the cost of the guard is this function.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else f"non_finite:{value!r}"
    if isinstance(value, dict):
        return {key: _finite_floats_only(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_floats_only(item) for item in value]
    return value


def build_shadow_verification_item(
    *,
    run_id: str,
    pipeline_id: str | None,
    comparison: dict[str, Any],
    shadow_status: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Build the ``(run_id, "shadow_verification")`` item (R4.2, R4.3, R4.5).

    DynamoDB-safe by construction, which is this function's whole reason to
    exist separately from the write: every ``float`` becomes a ``Decimal``
    through the shared converter (P28 R19.2's single copy), non-finite floats
    become strings first (:func:`_finite_floats_only`), and ``elapsed_seconds``
    is rounded to an ``int``. ``LGR-9e76249-3`` recorded the same discipline at
    the producer for the ``load_metrics`` row — "ints and strings only, no
    floats" — for the same reason.

    ``pipeline_id`` is carried deliberately rather than omitted. ``LGR-9e76249-3``
    made that call explicitly: omitting it would make ``execution_updater``
    drop the row, which is "a free dodge resting on an omission rather than on a
    rule". The rule is that reader's allowlist, and this row is already outside
    it by name.

    Carries no ``status`` and no ``step_sequence``. Not an accident of shape: it
    is what says this is a record row rather than a Step_Row, and it is what
    keeps the canonical-step-name sweep from reading it as one (see
    :data:`SHADOW_VERIFICATION_STEP_NAME`). Nothing verdict-shaped goes on this
    row either — no ``test_status``, no check list — because R4.3's guarantee is
    that nothing in the gate reads this key, and a field that looks like a
    verdict invites the reader that would break it.
    """
    payload: dict[str, Any] = {
        "run_id": run_id,
        "step_name": SHADOW_VERIFICATION_STEP_NAME,
        "timestamp": datetime.now(UTC).isoformat(),
        "pipeline_id": pipeline_id,
        # The executor that produced the row, so a reader never has to infer it.
        "verification_executor": VERIFICATION_EXECUTOR_SHADOW,
        "shadow_status": shadow_status,
        # R4.5 — ``omitted`` is what makes a bounded record distinguishable from
        # a complete one, and ``dimensions_compared`` distinguishes "they agreed"
        # from "there was almost nothing to compare". Both come straight off
        # ``compare_results``; neither is recomputed here.
        "disagreements": comparison.get("disagreements", []),
        "omitted": comparison.get("omitted", 0),
        "dimensions_compared": comparison.get("dimensions_compared", 0),
        "shadow_elapsed_seconds": round(elapsed_seconds),
    }
    # Bound to a typed local rather than returned directly: the shared converter
    # is annotated `Any -> Any` (it walks arbitrary structures), so returning its
    # result straight out is a `no-any-return`.
    item: dict[str, Any] = convert_floats_to_decimal(_finite_floats_only(payload))
    return item


def _default_shadow_glue_runner(*, region: str, role_arn: str, source_data_uri: str | None) -> Any:
    """Construct the Glue runner for the shadow half.

    A seam, so task 10.4's property test can drive an arbitrary Glue-side
    outcome — including a raised exception and a timeout — without a Glue
    client, and so no test can create a billable session by accident.

    The import is **function-local**, matching the ``glue`` branch in the
    handler and the ``agentcore_proxy`` import above it, and for the reasons
    ``LGR-60c4985-5`` gives for importing ``retry_utils`` lazily: it keeps this
    module's import graph unchanged for every caller that does not select
    ``shadow`` — 12 test files patch ``CodeInterpreterTestRunner`` on this
    module and none of them stubs ``glue_session_runner`` — and the default
    path never imports a Glue client it will not use.

    **The Frame_Mode is pinned to a Synthetic_Frame here, and written out rather
    than left to the constructor default** (R1.6). A default is a fact about
    ``glue_session_runner``; the pin is a fact about shadow mode, so spelling it
    means the pin survives a later change to that default. The pin lives in this
    factory and NOT at ``record_shadow_verification``'s call site, because that
    site goes through the ``runner_factory`` test seam — passing ``frame_mode``
    there would put it somewhere an injected factory can be handed ``sample``.

    Why pin at all: parity is *unimplementable* under shadow, not merely
    unimplemented. The pandas half is ``CodeInterpreterTestRunner(region=…)``
    (``code_interpreter_runner.py:223``), whose ``__init__`` takes ``region``
    and nothing else, opens one ``bedrock-agentcore`` client, and carries no
    ``frame_mode``, no ``source_data_uri`` and no S3 reference of any kind — its
    frame is generated inside the interpreter from the DECLARED schema. Giving
    the Glue half a real sample would therefore compare real rows against
    generated ones, so every data-quality dimension would disagree for a reason
    that has nothing to do with either executor — the one thing shadow mode
    exists to measure. A Sample_Frame is reachable only under the non-shadow
    ``glue`` executor.

    ``FRAME_MODE_SYNTHETIC`` is IMPORTED here, and that is safe **at this site
    specifically** even though :data:`FRAME_MODE_SYNTHETIC_DEFAULT` exists as a
    deliberate second spelling for the environment default. The two are not
    interchangeable and the difference is the reason: that constant is the
    fall-through for an unset environment variable — a bare string that has to
    match — whereas this is the runner's own vocabulary, so importing it cannot
    drift from the value the runner recognises. Sharing the statement with
    ``GlueSessionTestRunner`` is what makes the import safe: a test that stubs
    ``glue_session_runner`` has stubbed the runner too and is mocking the whole
    factory, so there is no state in which the constant is a ``MagicMock`` while
    a real runner is constructed.
    """
    from glue_session_runner import FRAME_MODE_SYNTHETIC, GlueSessionTestRunner

    return GlueSessionTestRunner(
        region=region,
        role_arn=role_arn,
        source_data_uri=source_data_uri,
        frame_mode=FRAME_MODE_SYNTHETIC,
    )


def record_shadow_verification(
    *,
    run_id: str,
    pipeline_id: str | None,
    transform_block: str,
    source_schema: dict[str, Any],
    target_schema: dict[str, Any],
    pandas_result: dict[str, Any] | None,
    source_data_uri: str | None,
    session_role_arn: str,
    step_started_at: float,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: int | None = None,
    runner_factory: Callable[..., Any] | None = None,
    writer: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Run the non-authoritative Glue half and record the Shadow_Comparison.

    Called only when the Verification_Executor is ``shadow``, and only **after**
    the pandas proxy has produced the run's verdict (R4.1). Returns a status
    label — one of the ``SHADOW_STATUS_*`` constants — for the log and for
    tests.

    **Never raises.** That is the contract, not a best effort: R4.4 requires the
    step to return the same ``test_status`` and check list it would have
    returned under ``pandas``, whatever the Glue half does, and
    ``LGR-60c4985-5`` states the discipline.

    Two ``except Exception`` arms, doing different jobs — worth separating,
    because "catches everything" twice would just look like belt and braces:

    * the **inner** one, around the Glue run, is a *classifier*. A Glue-side
      failure of any kind is a normal outcome of this function, so it becomes
      :data:`SHADOW_STATUS_GLUE_PRODUCED_NOTHING` and a row is still written.
      Without it, a Glue exception would be recorded as "shadow mode failed"
      rather than as "the Glue executor could not run this transform", which is
      the more interesting of the two facts and the one the retirement decision
      needs.
    * the **outer** one is the *guarantee*. It covers everything else in the
      body, including the comparison and the write, and it is what makes the
      "never raises" claim true rather than aspirational.

    :param step_started_at: the step's monotonic start. The deadline is measured
        from here rather than from the shadow half's own start, because what
        design C3 is about is the *step's* wall clock: a review agent that spent
        the budget on a reparse retry is exactly the case where the shadow half
        must not start.
    :param clock: injected so task 10.5 can test the deadline with no waiting.
        Must be monotonic; the default is.
    :param deadline_seconds: resolved from the environment when not supplied.
    :param runner_factory: builds the Glue runner. Defaults to
        :func:`_default_shadow_glue_runner`.
    :param writer: writes the item. Defaults to a ``PutItem`` on the state
        table — ``PutItem`` and not ``UpdateItem`` because this row has exactly
        one writer and no other attributes to preserve, which is the condition
        ``build_state_row_update``'s comment names as the reason the ``test_code``
        row cannot use ``PutItem``.
    """
    try:
        from shadow_comparison import compare_results

        budget = resolve_shadow_deadline_seconds(
            str(deadline_seconds) if deadline_seconds is not None else os.environ.get(SHADOW_DEADLINE_SECONDS_ENV)
        )
        started = clock()
        elapsed_at_start = started - step_started_at

        glue_result: dict[str, Any] | None = None
        if not session_role_arn:
            # `shadow` deliberately survives the role guard in
            # `resolve_effective_executor` — its authoritative half needs no
            # role — so arriving here with no ARN is a configuration the Glue
            # half declines rather than an error it raises.
            logger.warning(
                "shadow verification: %s is unset, so no Glue session can be created; "
                "recording that the Glue half did not run",
                VERIFICATION_SESSION_ROLE_ARN_ENV,
            )
            shadow_status = SHADOW_STATUS_NO_SESSION_ROLE
        elif not shadow_glue_run_fits(elapsed_at_start, budget):
            # R4.6, pre-flight. See `shadow_glue_run_fits`.
            logger.warning(
                "shadow verification: %.0fs of the %ss budget already spent and the Glue "
                "runner's worst case is %ss — not starting it",
                elapsed_at_start,
                budget,
                SHADOW_GLUE_WORST_CASE_SECONDS,
            )
            shadow_status = SHADOW_STATUS_NOT_STARTED_DEADLINE
        else:
            factory = runner_factory or _default_shadow_glue_runner
            # Inline, before the handler returns. NOT on a thread:
            # `LGR-b46a5bb-1` (standing invariant) forbids one in `lambda/**`
            # because the environment freezes on return and kills it.
            try:
                runner = factory(
                    region=AWS_REGION,
                    role_arn=session_role_arn,
                    source_data_uri=source_data_uri,
                )
                glue_result = runner.run_test(transform_block, source_schema, target_schema)
                shadow_status = SHADOW_STATUS_COMPARED
            except Exception as glue_error:
                # A Glue-side failure of ANY kind, including the capacity
                # refusal, lands here and is recorded as "the Glue half
                # produced nothing" (R4.4). `compare_results` documents
                # `glue_result=None` as a normal input for exactly this path.
                #
                # `%r` on the exception and NOT `%s`: `%s` calls `__str__`, and
                # an exception whose `__str__` itself raises would make this
                # recovery path a raise site. `BaseException.__repr__` formats
                # `self.args` and never reaches an overridden `__str__`, so it
                # cannot fail for that reason. Do not "tidy" it back — see the
                # outer arm's note for the half that actually escaped.
                logger.warning(
                    "shadow verification: the Glue half raised %s: %r",
                    type(glue_error).__name__,
                    glue_error,
                )
                glue_result = None
                shadow_status = SHADOW_STATUS_GLUE_PRODUCED_NOTHING

            if shadow_status == SHADOW_STATUS_COMPARED and (clock() - step_started_at) > budget:
                # R4.6, post-flight. The result arrived, but past the budget, so
                # it is discarded rather than compared: a late measurement is
                # not evidence about a run that has already been decided, and
                # recording it would hide that the budget was exceeded.
                logger.warning(
                    "shadow verification: the Glue half returned after the %ss budget — abandoning its result",
                    budget,
                )
                glue_result = None
                shadow_status = SHADOW_STATUS_ABANDONED_DEADLINE

        comparison = compare_results(pandas_result, glue_result)
        item = build_shadow_verification_item(
            run_id=run_id,
            pipeline_id=pipeline_id,
            comparison=comparison,
            shadow_status=shadow_status,
            elapsed_seconds=clock() - started,
        )
        (writer or _put_shadow_verification_row)(item)
        logger.info(
            "shadow verification recorded on (%s, %s): status=%s, %s disagreement(s), %s omitted",
            run_id,
            SHADOW_VERIFICATION_STEP_NAME,
            shadow_status,
            len(comparison.get("disagreements", [])),
            comparison.get("omitted", 0),
        )
        return shadow_status
    except Exception as exc:
        # The fail-soft arm (R4.4, `LGR-60c4985-5`). Nothing above may reach the
        # caller: the verdict is already computed and returning normally is what
        # keeps it that way.
        #
        # `%r` on the exception and NOT `%s`, and here it is load-bearing rather
        # than tidy: this log call is the LAST statement before the `return`, so
        # a raise inside it escapes the function and defeats the "never raises"
        # contract — turning a working step into a failing one, which is exactly
        # what `LGR-60c4985-5`'s fail-soft discipline forbids. `%s` calls
        # `__str__`, so an exception whose `__str__` raises did escape here.
        # `BaseException.__repr__` formats `self.args` and never reaches an
        # overridden `__str__`. Invisible under the stdlib `StreamHandler` the
        # Lambda and AgentCore runtimes use — its `handleError` writes to stderr
        # and returns — so production logging masked it; visible under
        # `_pytest.logging.LogCaptureHandler`, which re-raises on purpose.
        # The type name stays `%s`: it is already a plain string, and it is what
        # makes the line scannable when a repr is long (`LGR-b2f79ef-8`'s point
        # that a log line carrying a distinction is behaviour).
        logger.warning(
            "shadow verification failed and was skipped (%s: %r) — the run's verdict is unaffected",
            type(exc).__name__,
            exc,
        )
        return SHADOW_STATUS_ERROR


def _put_shadow_verification_row(item: dict[str, Any]) -> None:
    """Write the shadow row. Separate so :func:`record_shadow_verification` has
    a seam a test can pass without touching DynamoDB."""
    pipeline_runs_table.put_item(Item=item)


def execution_was_verified(checks: list[dict[str, Any]]) -> bool:
    """False when nothing was executed: a check REPLACED
    ``code_interpreter_execution`` rather than reporting it (p40 R6).

    Keyed on :data:`EXECUTION_REPLACING_CHECK_NAMES` — the two
    execution-replacing check names — and deliberately NOT on
    :func:`is_not_verified_check`. A ``schema_compliance`` entry carrying
    ``verified: False`` and :data:`NO_TARGET_SCHEMA_REASON` belongs to a run
    whose transform **did** execute: that entry is emitted only when
    ``data_quality_results`` carries a parsed ``VALIDATION:`` payload, and only
    an execution produces one. Labelling such a run "never executed" would be
    a NEW false claim made in the course of removing one, which is the whole
    subject of R6 — so the discriminator has to be the check NAME, not the
    three-state ``verified`` flag those two entries share.

    Pure: derives from the check list alone and reads nothing.
    """
    return not any(check.get("name") in EXECUTION_REPLACING_CHECK_NAMES for check in checks)


def execution_verification_note(checks: list[dict[str, Any]]) -> str:
    """One line stating what did not happen, or ``""`` when it did (p40 R6).

    Same keying as :func:`execution_was_verified`, over the same constant, so
    the boolean and the prose cannot disagree. The reason is read off the
    execution-replacing entry rather than re-derived, and an unmapped reason
    falls back to the raw reason string rather than to silence: R6 is about
    saying so, and a producer that gains a sixth reason must still say
    something true here.

    Pure: derives from the check list alone and reads nothing.
    """
    for check in checks:
        if check.get("name") in EXECUTION_REPLACING_CHECK_NAMES:
            reason = str(check.get("not_verified_reason") or "reason not stated")
            return f"Transform NOT executed — {NOT_VERIFIED_REASON_LABELS.get(reason, reason)}."
    return ""


class _UnspecifiedTargetSchema:
    """Type of :data:`TARGET_SCHEMA_UNSPECIFIED`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "TARGET_SCHEMA_UNSPECIFIED"


#: Default for :func:`aggregate_check_results`' ``target_schema`` parameter.
#:
#: ``None`` cannot be the default, because ``None`` is a MEANINGFUL value there:
#: it is requirement 2.6's "this pipeline has no target schema at all", which
#: the shared resolver returns as ``(None, None)``. A caller that says nothing
#: about the target-schema state — every direct unit call that exercises only
#: the review/execution halves of the derivation — is not asserting that the
#: pipeline is schema-less, so it must not get 2.6's not-verified entry.
TARGET_SCHEMA_UNSPECIFIED: Final = _UnspecifiedTargetSchema()


def aggregate_check_results(
    parsed_results: dict,
    execution_results: dict | None,
    target_schema: dict[str, Any] | None | _UnspecifiedTargetSchema = TARGET_SCHEMA_UNSPECIFIED,
) -> tuple[str, list[dict[str, Any]]]:
    """P28 R4.3: honest per-check aggregation — ANY failed check → failed.

    Collects every executed check as ``{name, passed, detail}`` and derives the
    overall ``test_status`` from them:

      * one entry per static-analysis category parsed from the review
        (Correctness, Security, Schema Compliance, ...), and
      * one entry for the Code Interpreter execution of the generated
        transform.

    Returns ``("failed", checks)`` when ANY check failed, ``("passed", checks)``
    only when every executed check passed. There is no majority heuristic, no
    execution-success override of static failures, and no forced status.

    A missing/unavailable Code Interpreter run is recorded as a failed check:
    a test step that could not execute the transform has not proven anything,
    so it must not report "passed" (the SFN Choice added in task 5.2 routes
    failed results to a forced manual review, where a human can approve
    anyway).

    P28 R13.1: when ``parsed_results`` carries the structured review verdict
    (it always does in the handler path — parse failure raises upstream), the
    verdict participates as its own check, so the quality gate consumes the
    reviewer's machine-readable pass/fail — never keyword counts or substring
    scans of the review text.

    p32 2.20/2.21: when the pandas proxy REFUSED the transform (an
    unconvertible construct — ``not_verified_by_proxy`` on
    ``execution_results``), the transform never ran, so no
    ``code_interpreter_execution`` check is emitted at all. A
    ``not_verified_by_proxy`` check takes its place, naming the construct, the
    line and which reason applies — 2.23's two, plus p40 R4's
    ``scalar_literal_receiver`` — and it is partitioned out of the status
    derivation while staying IN the emitted list — "not verified" is not
    implemented as "not reported". Every check that DID verify something still
    has to have passed, so R4.3 is unchanged for those.

    p33 D1/D2 — ``schema_compliance`` becomes a real check that can fail a run.
    ``target_schema`` is the state the caller RESOLVED, passed in so this stays
    a pure derivation over its inputs (no read happens here):

      * a dict — the pipeline's target schema. One ``schema_compliance`` entry
        is emitted with ``passed = (missing_columns == [])``, and the score and
        the absent columns stated as TEXT in ``detail`` (C5). It carries no
        ``verified`` key, so it is a verified check and a shortfall derives
        ``failed``.
      * ``None`` — requirement 2.6's schema-less pipeline, for which the score
        that *was* computed is the vacuous 100 (an empty declared set has
        nothing to be short of). That 100 is replaced by an entry carrying
        ``verified: False`` and ``not_verified_reason: "no_target_schema"``, so
        "we could not check" stops being indistinguishable from "we checked and
        it passed" (1.7). It is partitioned out of the derivation (C4), so it
        can neither pass nor fail a run.
      * :data:`TARGET_SCHEMA_UNSPECIFIED` — the caller said nothing, so no
        not-verified entry is ever synthesised from silence; the score is
        reported as computed.

    In every case the entry is emitted ONLY when the execution actually produced
    validation output, which is exactly what a ``schema_compliance`` key inside
    ``data_quality_results`` means: ``_parse_results`` writes that key and
    ``missing_columns`` together, and only for a parsed ``VALIDATION:`` payload.
    On a proxy refusal, a harness refusal, ``execution_results is None`` and an
    execution failure there is no such output (every failure shape carries
    ``data_quality_results: {}``), so nothing is emitted — the transform never
    ran, so there is nothing to compare and nothing to say about the comparison.
    Note what this gating does NOT lose: the confusion 2.6 exists to remove is a
    *reported* vacuous pass, and a score is reported only when validation ran,
    so every vacuous 100 is caught here.

    C13 needs no branch of its own: a frame the harness could not build is
    refused before the session starts, so it produces no validation output and
    therefore no score. A shortfall the harness caused can never present as a
    transform failure.

    p33 D12/C14: when ``execution_results`` carries ``not_verified_by_harness``
    (the frame could not be built to carry every declared field), a
    ``not_verified_by_harness`` check replaces ``code_interpreter_execution``,
    naming the field count and the ceiling as text. Same *state* as a proxy
    refusal (``verified: False``), a different *reason* — so
    :func:`is_not_verified_check` and the partition below are unchanged (C3).
    """
    checks: list[dict[str, Any]] = []

    verdict = parsed_results.get("verdict")
    if verdict is not None:
        issues = parsed_results.get("issues") or []
        # P28b R1b: the check's pass/fail is the Issue_Attribution-calibrated
        # effective verdict (parsed_results carries the verdict + issues keys
        # it reads). The detail surfaces the raw verdict, the effective
        # verdict, and per-attribution issue counts, so a scaffolding-only
        # pass shows its RETAINED issues in the checks list, the comprehensive
        # report, and the approval payload (R1.3's visibility clause).
        effective = effective_verdict(parsed_results)
        transform_issue_count = sum(1 for i in issues if i.get("attribution") == ATTRIBUTION_TRANSFORM_BLOCK)
        scaffolding_issue_count = sum(1 for i in issues if i.get("attribution") == ATTRIBUTION_TEMPLATE_SCAFFOLDING)
        # p43 R3.6: the count of Reviewer_Transform_Block_Findings the severity
        # floor RETAINED as non-blocking, stated as text distinct from the
        # per-attribution counts above. Read from the one shared helper rather
        # than re-derived, so this detail, the report and the log cannot disagree
        # about the number.
        #
        # `len(retained)`, never a `count` method call — the C11 source fence at
        # `test_review_gate_calibration.py::test_gate_functions_contain_no_token_presence_or_keyword_count_logic`
        # rejects that spelling anywhere inside this function, which is also why
        # the two per-attribution counts above use `sum(1 for ...)`. That fence is
        # a substring scan over `inspect.getsource`, so it reads comments too —
        # hence the construct is described here rather than quoted.
        retained = retained_nonblocking_findings(parsed_results)
        if issues:
            issue_summary = "; ".join(f"[{i.get('severity', 'issue')}] {i.get('description', '')}" for i in issues[:5])
            # Position is deliberate twice over. The detail is truncated at 1000
            # characters below, so the count sits AHEAD of the free-text issue
            # summary truncation eats first. And it is inserted AFTER the
            # per-attribution counts, so the two substrings pinned at
            # `test_review_gate_calibration.py:157` and `:191` —
            # "0 transform_block, 2 template_scaffolding" and
            # "1 transform_block, 0 template_scaffolding" — stay byte-for-byte
            # intact.
            detail = (
                f"Reviewer verdict: {verdict}; effective verdict: {effective} "
                f"({len(issues)} issue(s): {transform_issue_count} transform_block, "
                f"{scaffolding_issue_count} template_scaffolding; "
                f"{len(retained)} retained as non-blocking; {issue_summary})"
            )
        else:
            detail = f"Reviewer verdict: {verdict}; effective verdict: {effective} (no issues reported)"
        checks.append(
            {
                "name": "ai_review_verdict",
                "passed": effective == VERDICT_PASS,
                "detail": detail[:1000],
            }
        )

    for category, info in (parsed_results.get("test_categories") or {}).items():
        passed = bool(info.get("passed", False))
        checks.append(
            {
                "name": f"static_analysis:{category}",
                "passed": passed,
                "detail": f"Static analysis category '{category}': {info.get('status', 'UNKNOWN')}",
            }
        )

    if execution_results is None:
        # p32 3.22: a missing or unavailable Code Interpreter run stays a
        # FAILED check. That path proved nothing AND named no reason, which is
        # not the same as a refusal that named its construct.
        checks.append(
            {
                "name": "code_interpreter_execution",
                "passed": False,
                "detail": "Code Interpreter execution did not run",
            }
        )
    elif execution_results.get("not_verified_by_proxy"):
        # p32 2.20/2.23: the proxy could not express the transform. Report the
        # non-verification as TEXT — the construct, the line, and which of the
        # two reasons applies — never as a recoloured badge (LGR-99050e6-1's
        # house pattern: colour alone fails WCAG 1.4.1 and makes the
        # distinction invisible).
        construct = execution_results.get("not_verified_construct") or "unknown construct"
        line_number = execution_results.get("not_verified_line_number")
        reason = execution_results.get("not_verified_reason") or "not_yet_expressible"
        if reason == "permanently_inexpressible":
            reason_text = (
                "no single-frame pandas equivalent exists, so the proxy will never "
                "express it — this is not a defect in the generated PySpark and there "
                "is nothing to fix"
            )
        elif reason == SCALAR_LITERAL_RECEIVER_REASON:
            # p40 R4: the THIRD reason. The two above describe a converter that
            # REFUSED; this one describes a converter that would otherwise have
            # LIED — the receiver-shape fence declining to apply a Series-only
            # pandas member to a scalar emission. So it is neither "nothing to
            # fix, ever" nor "a converter gap to close", and its copy must stay
            # distinct from both (p40 3.7: adding a reason must not blur them).
            #
            # A third REASON, not a third STATE: the entry below is still
            # `not_verified_by_proxy` with `passed: False` + `verified: False`,
            # discriminated by `is_not_verified_check`. `LGR-b2f79ef-12` is the
            # precedent (a new reason gets its own text, not its own field), and
            # `LGR-b2f79ef-2` refused a third check field once already. Because
            # no state is added, the partition at the `all()` below and
            # `LGR-b2f79ef-2`'s caveat — a `verified: False` entry must never be
            # the only check — are untouched by this arm.
            reason_text = (
                "the transform applies a pandas operation that only exists on a column "
                "to a constant value. The proxy refuses rather than emit pandas that "
                "raises, or pandas that silently disagrees with Spark about the column's "
                "values — the generated PySpark is valid and there is nothing in it to fix"
            )
        else:
            reason_text = (
                "the converter cannot yet express this construct — a defect in the "
                "checker, not in the generated PySpark"
            )
        checks.append(
            {
                "name": NOT_VERIFIED_CHECK_NAME,
                "passed": False,
                "detail": (
                    f"NOT VERIFIED by the pandas proxy: construct '{construct}' on "
                    f"line {line_number} was not converted, so the transform never ran. "
                    f"Reason ({reason}): {reason_text}."
                )[:1000],
                # p32 2.20 — the three-state discriminator, present only here.
                "verified": False,
                "not_verified_reason": reason,
            }
        )
    elif execution_results.get("not_verified_by_harness"):
        # p33 2.11 / D12: the TEST HARNESS could not build a frame carrying
        # every field the source schema declares, so it refused before starting
        # a session. Nothing ran. Reported as a non-verification in place of
        # `code_interpreter_execution` — the same substitution p32 established
        # for a proxy refusal — with the field count and the ceiling as TEXT
        # (C5), and with a name and reason of its own rather than the proxy's
        # (C14: a width refusal has no construct and no line number).
        harness_reason = execution_results.get("not_verified_reason") or "test_frame_truncated"
        field_count = execution_results.get(HARNESS_FIELD_COUNT_KEY)
        ceiling = execution_results.get(HARNESS_FRAME_CEILING_KEY)
        if harness_reason == SESSION_CAPACITY_REASON:
            # p42 R8.1 — the THIRD harness reason, and the first one that is not
            # about the test frame at all. An account already holding its maximum
            # number of Glue interactive sessions is a fact about the account, so
            # the copy has to say "come back later", not "your schema is too wide"
            # and not "your transform failed" — that misattribution is the whole
            # of R8.
            #
            # A third REASON, not a third STATE: the entry below is the SAME
            # `not_verified_by_harness` check with `passed: False` +
            # `verified: False`, discriminated by `is_not_verified_check`. The
            # argument is written out at the `scalar_literal_receiver` arm above
            # (`:508-527`), where `LGR-b2f79ef-2` is recorded as having refused a
            # third check field once already. Because no state and no field is
            # added, the partition at the `all()` below is untouched, and R8.3
            # holds through the existing `verified: False` discriminator: this
            # entry neither passes nor fails the run.
            #
            # It also adds nothing a Step Functions Choice state reads (R8.4).
            # The Choice states over this step read
            # `$.test_code_result.test_result.status`, `.regeneration_needed`,
            # `.regeneration_attempt`, `.regeneration_max_attempts` and
            # `.approval_required` (`orchestration-stack.ts:851-923`); a key on a
            # check inside the check list is addressed by none of them, and
            # `LGR-69ca78b-1` (invariant) means the governance Choices fail
            # closed, so an unread key is inert rather than permissive.
            situation = "no Spark session could be obtained to run the transform in"
            harness_reason_text = (
                "the account was already at its limit of concurrent Glue interactive "
                "sessions, so the session this verification needed could not be created "
                "— a capacity limit on the account, not a defect in the generated "
                "PySpark. Nothing about the transform was observed; re-running the "
                "verification later is expected to succeed"
            )
        elif harness_reason == FRAME_UNBUILDABLE_REASON:
            # p42 R7.3 — the FOURTH harness reason, and the second one carrying no
            # field count and no ceiling. The schema is present and inside the
            # ceiling; what the harness cannot do is give one of its declared
            # types a Spark type. Rendering the width sentence here would state
            # "declares None field(s) against a ceiling of None" — a false claim
            # about the schema, made while removing a false claim about the
            # transform, which is the objection R8's own copy already records.
            #
            # A fourth REASON on the same check, with the same `passed: False` +
            # `verified: False` pair — see the capacity arm above for the full
            # argument, including `LGR-b2f79ef-2` having refused a third check
            # FIELD once already. Nothing here is read by a Step Functions Choice
            # state (R8.4).
            situation = "no test frame could be built from the declared source schema"
            harness_reason_text = (
                "one of the fields the source schema declares carries a type the test "
                "harness has no Spark equivalent for, so a frame holding every column "
                "the transform references could not be built — a limitation of the "
                "test harness, not a defect in the generated PySpark. The declared "
                "type is named in the error detail above"
            )
        elif harness_reason == SOURCE_READ_FAILED_REASON:
            # p44 R2.1/R2.2/R3.4 — the FIFTH harness reason, and the first one about
            # the run's own DATA rather than about its schema or the account. A
            # session was created and billed; what failed was reading the object the
            # frame was to be built from.
            #
            # So this clause names THE OBJECT AND THE READ, never the schema and
            # never the stage label. The width sentence would read "declares None
            # field(s) against a ceiling of None" here — the false-claim-about-the-
            # schema objection the capacity arm already records — and the producer's
            # stage label is `STAGE_FRAME_BUILD = "test data generation"`, so
            # borrowing the statement-failure sentence would say "test data
            # generation failed" about a run that was reading a real object. That
            # sentence is exactly what R2.1 exists to remove.
            #
            # A fifth REASON on the same check, with the same `passed: False` +
            # `verified: False` pair — see the capacity arm above for the full
            # argument, including `LGR-b2f79ef-2` having refused a third check FIELD
            # once already. No state and no field is added, so the partition at the
            # `all()` below is untouched, and nothing here is read by a Step
            # Functions Choice state (R8.4).
            situation = "the run's own source object could not be read"
            harness_reason_text = (
                "the transform's input was to be read from the pipeline's own source "
                "object and that read did not succeed, so nothing about the transform "
                "was observed — there is nothing in the generated PySpark to fix. "
                "Check the object and the verification session's read access to it; "
                "the object and the reported reason are named in the error detail above"
            )
        else:
            # The two WIDTH reasons, which are the only two carrying a field count
            # and a ceiling. Kept as one clause so the sentence they render is
            # byte-for-byte what it was before capacity joined them.
            situation = (
                f"the source schema declares {field_count} field(s) against a test-frame width ceiling of {ceiling}"
            )
            if harness_reason == "no_source_schema":
                harness_reason_text = (
                    "no source schema was available for this run, so the only frame the "
                    "harness could build would not carry the columns the transform "
                    "references — a limitation of the test harness, not a defect in the "
                    "generated PySpark"
                )
            else:
                harness_reason_text = (
                    "the source schema is wider than the test frame can carry, so a frame "
                    "holding every column the transform references could not be built — a "
                    "limitation of the test harness, not a defect in the generated PySpark"
                )
        checks.append(
            {
                "name": NOT_VERIFIED_BY_HARNESS_CHECK_NAME,
                "passed": False,
                "detail": (
                    f"NOT VERIFIED by the test harness: {situation}, so the transform "
                    f"was never executed. "
                    f"Reason ({harness_reason}): {harness_reason_text}. No schema-compliance "
                    "score is reported for this run."
                )[:1000],
                # p32 2.20's discriminator, reused unchanged (C3).
                "verified": False,
                "not_verified_reason": harness_reason,
            }
        )
    elif execution_results.get("execution_successful", False):
        checks.append(
            {
                "name": "code_interpreter_execution",
                "passed": True,
                "detail": (
                    f"Executed transform: {execution_results.get('input_records', 0)} → "
                    f"{execution_results.get('output_records', 0)} records"
                ),
            }
        )
    else:
        error_details = str(execution_results.get("error_details") or "Unknown execution error")
        checks.append(
            {
                "name": "code_interpreter_execution",
                "passed": False,
                "detail": f"Execution failed: {error_details[:500]}",
            }
        )

    # ── p33 D1/D2: the computed schema-compliance check ─────────────────────
    # The measurement, not the reviewer's opinion. `_parse_results` writes
    # `schema_compliance` and `missing_columns` together and only for a parsed
    # `VALIDATION:` payload, so this key's presence IS "the execution produced
    # validation output". Every failure/refusal shape carries an empty
    # `data_quality_results`, which is what suppresses the score on those paths
    # — including C13's incomplete frame, which never reaches a session.
    quality_results = (execution_results or {}).get("data_quality_results") or {}
    if SCHEMA_COMPLIANCE_CHECK_NAME in quality_results:
        if target_schema is None:
            # Requirement 2.6 / D2. A score IS present here, and it is the
            # vacuous one: with no declared columns the expected list is empty,
            # so the arithmetic yields 100 no matter what the transform
            # produced. That 100 is the defect (1.2/1.7), so it is REPLACED by
            # an explicit non-verification rather than reported. `verified:
            # False` keeps it out of the derivation (C4), so an unverifiable
            # check can neither pass nor fail the run.
            checks.append(
                {
                    "name": SCHEMA_COMPLIANCE_CHECK_NAME,
                    "passed": False,
                    "detail": (
                        "NOT VERIFIED: no target schema was available for this pipeline, so the "
                        "transform's output columns could not be compared against any declared "
                        "set. This is not a passing score — nothing was checked."
                    ),
                    # p32 2.20's discriminator, reused rather than adding a
                    # third state (C3) — `is_not_verified_check` is unchanged.
                    "verified": False,
                    "not_verified_reason": NO_TARGET_SCHEMA_REASON,
                }
            )
        else:
            # An empty declared set (`{}`) scores 100 with nothing missing,
            # which is correct rather than vacuous: a schema declaring no
            # columns cannot be short of any.
            score = quality_results.get(SCHEMA_COMPLIANCE_CHECK_NAME)
            missing_columns = list(quality_results.get("missing_columns") or [])
            detail = (
                f"Schema compliance {score}: the transform output carries every one of the "
                "target schema's declared columns."
                if not missing_columns
                else (
                    f"Schema compliance {score}: {len(missing_columns)} declared target column(s) "
                    f"missing from the transform output: {', '.join(str(c) for c in missing_columns)}"
                )
            )
            checks.append(
                {
                    "name": SCHEMA_COMPLIANCE_CHECK_NAME,
                    "passed": not missing_columns,
                    # C5 / requirement 3.13 — the score and the absent columns
                    # as TEXT, so the dashboard and the approval payload can
                    # present them without relying on colour.
                    "detail": detail[:1000],
                }
            )

    # p32 2.21 (C4): partition the not-verified entries out BEFORE the all().
    # They stay in `checks` (2.20) but they are not evidence of failure, so a
    # refusal alone must not derive "failed" and must not force the gate.
    # A run in which NOTHING was verifiable is still "failed" — that is the
    # same posture as a missing execution (3.22): it proved nothing.
    verified_checks = [check for check in checks if not is_not_verified_check(check)]
    test_status = "passed" if verified_checks and all(check["passed"] for check in verified_checks) else "failed"
    return test_status, checks


#: Default regeneration-attempt bound (P28 R13.3 — design default 2),
#: overridable via the CODE_REGENERATION_MAX_ATTEMPTS env var.
DEFAULT_REGENERATION_MAX_ATTEMPTS = 2


def get_regeneration_max_attempts() -> int:
    """Regeneration-attempt bound from config (env), default 2 (R13.3).

    Misconfigured values raise honestly (ValueError) instead of silently
    falling back — mirroring shared/model_config's env-reading discipline.
    """
    raw = os.environ.get("CODE_REGENERATION_MAX_ATTEMPTS")
    if raw is None or raw.strip() == "":
        return DEFAULT_REGENERATION_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"CODE_REGENERATION_MAX_ATTEMPTS must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"CODE_REGENERATION_MAX_ATTEMPTS must be >= 0, got {value}")
    return value


def analyze_test_results_for_regeneration(
    test_status: str,
    checks_run: int,
    checks_passed: int,
    review_verdict: dict,
    regeneration_attempt: int = 0,
    max_attempts: int | None = None,
) -> dict:
    """Decide whether code regeneration is needed — from structured inputs only.

    P28 R13.2: the trigger consumes the structured review verdict and the
    honest executed-check status. The substring scan of the review text is
    DELETED — "no syntax errors found" can no longer trigger regeneration,
    because no free text is inspected.

    P28b R1b: the verdict trigger is the Issue_Attribution-calibrated
    ``effective_verdict(review_verdict) == "fail"`` — a scaffolding-only
    fail does not regenerate. Bounds, the feedback context carrying the
    reviewer's issues, and the attempt accounting are all unchanged (R1.6).

    P28 R13.3: the reviewer's ``issues`` are carried in ``feedback_context``
    (so the regeneration prompt differs from the original by including them),
    and attempts are bounded by config (default 2) with the bound recorded in
    the returned analysis (and, via the handler, in the state row).

    P28 R10.6: check counts are counts of actually executed checks (from
    :func:`aggregate_check_results`), never LLM-parsed numbers.
    """
    if max_attempts is None:
        max_attempts = get_regeneration_max_attempts()
    checks_failed = checks_run - checks_passed
    reviewer_issues = list(review_verdict.get("issues") or [])

    regeneration_needed = False
    feedback_context: dict[str, Any] = {
        "summary": "",
        "issues_found": [],
        "reviewer_issues": reviewer_issues,
        "recommendations": [],
    }

    regeneration_triggers: list[str] = []
    # P28b R1b: effective (attribution-calibrated) fail triggers, raw does not.
    if effective_verdict(review_verdict) == VERDICT_FAIL:
        regeneration_triggers.append("review verdict: fail")
    # p32 2.21: the honest aggregated status is the ONE check-based trigger.
    # The former `or checks_failed > 0` disjunct was exactly equivalent to
    # this while every non-passing check was a failure; once a refusal is
    # classified as a non-verification (C4) it is no longer equivalent, it is
    # wrong — a not-verified entry would keep setting regeneration_needed, and
    # a run whose PySpark is correct would be sent back to CreateCode with
    # nothing to fix. There is nothing to regenerate when only the checker
    # could not express the transform.
    #
    # (p32 2.17 note: this used to add "and determine_code_approval_requirement
    # fires on regeneration_needed BEFORE it looks at test_status". It no longer
    # does — that branch is gone, and regeneration_needed now routes to the
    # regeneration edge instead. The reason above stands on its own.)
    if test_status == "failed":
        regeneration_triggers.append(f"executed checks failed: {checks_failed} out of {checks_run}")

    if regeneration_triggers and regeneration_attempt < max_attempts:
        regeneration_needed = True
        feedback_context["issues_found"].extend(regeneration_triggers)
        for issue in reviewer_issues:
            feedback_context["issues_found"].append(
                f"[{issue.get('severity', 'issue')}] {issue.get('description', '')}"
            )
        feedback_context["recommendations"].append("Address every reviewer issue listed above and fix failing checks")

    max_attempts_reached = regeneration_attempt >= max_attempts
    if regeneration_needed:
        feedback_context["summary"] = (
            f"Code regeneration needed (attempt {regeneration_attempt + 1}/{max_attempts}): "
            f"{len(reviewer_issues)} reviewer issue(s), {checks_failed} failed check(s)"
        )
    elif regeneration_triggers and max_attempts_reached:
        feedback_context["summary"] = (
            f"Maximum regeneration attempts reached ({regeneration_attempt}/{max_attempts}), "
            "proceeding to manual review with current code"
        )
    else:
        feedback_context["summary"] = "Review verdict pass and all executed checks passed; no regeneration needed"

    return {
        "regeneration_needed": regeneration_needed,
        "feedback_context": feedback_context,
        "max_attempts_reached": max_attempts_reached,
        "regeneration_attempt": regeneration_attempt,
        "max_attempts": max_attempts,
    }


#: Sentinel for R7.4's source log: the setting came from the Pipeline_Record.
_SETTING_FROM_RECORD = "pipeline_record"

#: Sentinel for R7.4's source log: the setting was defaulted, either because
#: the record does not carry it (R1.2) or because what it carries is not a JSON
#: boolean (R1.4). Both are the same *source* — this function's own default —
#: and R1.5 is what distinguishes the malformed case in the log.
_SETTING_FROM_DEFAULT = "default"


def resolve_code_approval_setting(pipeline_metadata: dict) -> tuple[bool, str]:
    """Resolve Code_Approval_Setting from an ALREADY-READ pipeline record.

    Takes the record, not a ``pipeline_id``: the caller already has it (the
    handler's ``get_pipeline_metadata(pipeline_id)`` call, ``:1264`` — design
    §4.1 anchors it at ``:1208``, before this function was inserted above it),
    and reading it a second time inside this function would let two reads in one
    invocation disagree. Pure — no boto3, no exceptions of its own.

    Fail-closed to ``True`` for absent (R1.2) and non-boolean (R1.4). ``is``
    checks, not truthiness: ``"false"`` is a truthy string and ``0`` is a falsy
    int, and coercing either would silently invert the user's setting. The
    malformed case is reachable in practice — ``pipelines_crud.py:1157`` passes
    the request-body value into the nested document-path update unvalidated, so
    the dashboard cannot produce a non-boolean but a direct API caller can.

    Returns ``(setting, source)`` where ``source`` is
    :data:`_SETTING_FROM_RECORD` or :data:`_SETTING_FROM_DEFAULT` — R7.4's
    per-invocation log names it, so the evaluated value and where it came from
    are both readable from one line.

    **No ``try``/``except`` here, deliberately.** ``LGR-78ce0b7-13``
    (**template / ratified**) narrowed a blanket ``except Exception`` that
    stringified an already-classified ``StepFailure`` /
    ``TransientStepFailure`` into a bare ``ValueError``. The caller's read
    (``get_pipeline_metadata``, ``:131``) raises classified exceptions and
    ``:1265-1278`` raises ``StepFailure`` for a missing record (p33 R2.8, design
    §4.1's ``:1216-1222``); a clause here would swallow both. So would one at
    the call site — see task 2.4, which owns the wiring. R1.6/R1.7's
    fail-closed intent is satisfied
    in the stronger direction — the step fails and can never reach
    ``CreateInfrastructure`` — see design D1.
    """
    approval_config = pipeline_metadata.get("approval_config")
    if not isinstance(approval_config, dict) or "code_generation_enabled" not in approval_config:
        return True, _SETTING_FROM_DEFAULT
    raw = approval_config["code_generation_enabled"]
    if raw is True or raw is False:
        return raw, _SETTING_FROM_RECORD
    logger.warning(
        "Code_Approval_Setting for this pipeline is not a JSON boolean "
        f"(type={type(raw).__name__}, value={raw!r}) — evaluating as true (R1.4/R1.5)"
    )
    return True, _SETTING_FROM_DEFAULT


def determine_code_approval_requirement(
    test_status: str,
    checks_run: int,
    checks_passed: int,
    review_verdict: dict,
    regeneration_analysis: dict,
) -> bool:
    """Determine if approval is required — structured inputs only (P28 R13.1).

    Gate inputs: the honest aggregated status, executed-check counts, the
    structured review verdict, and the regeneration analysis. The substring
    scan of the review text (``critical_phrases``) is DELETED — free text
    like "no syntax errors found" can neither trigger nor suppress approval.

    P28 R10.6: counts are executed-check counts. The former coverage gate is
    deleted — coverage was an LLM-parsed number with no real measurement
    behind it.

    p32 2.17: ``regeneration_needed`` is NO LONGER a reason for approval. It
    was, and that was the whole of Defect 4's visible half (1.16): the counter
    was pinned at 0, so ``0 < max_attempts`` held forever, so
    ``regeneration_needed`` was True on every failing run — and this function
    converted it straight into "ask a human", ahead of its own test-status and
    verdict branches. The system decided the code should be regenerated and
    then requested an approval instead. The destination now exists (D2's
    ``TestResultChoice`` → ``CreateCode`` edge, keyed on
    ``regeneration_needed`` plus the two attempt numbers), so the conclusion
    routes there.

    Removing that branch does not relax the gate, because every input that
    could reach it is still caught downstream: ``regeneration_needed`` is only
    True when ``test_status == "failed"`` or the effective verdict is fail
    (``analyze_test_results_for_regeneration``'s trigger list), and both of
    those are branches below. So a failing in-bounds run still reports
    ``approval_required = True`` — which is also the fail-safe direction: if
    the regeneration edge's guards ever fail to match, the run lands on
    ``ForceTestReviewApproval``, and an approve-anyway from there resumes at
    ``CheckCodeApproval``, which reads this flag. Returning False there would
    let a failing run reach ``CreateInfrastructure`` on one human click
    (3.21/P28 R4.5). What changes is the *destination the step names* —
    see ``next_step`` in the handler.

    ``max_attempts_reached`` stays the first branch, unchanged: out of bounds,
    the run falls through to the human decision exactly as today (2.17's
    "a failing pipeline SHALL NOT be able to regenerate indefinitely").
    """
    checks_failed = checks_run - checks_passed

    # Always require approval if regeneration was needed but max attempts reached
    if regeneration_analysis.get("max_attempts_reached", False):
        logger.info("Approval required due to max regeneration attempts reached")
        return True

    # P28 R4.3: ANY failed check fails the test step and requires review —
    # the old majority heuristic (only fail when tests_failed > tests_passed)
    # is removed.
    if test_status == "failed":
        logger.info(f"Approval required: test status is failed ({checks_failed} failed check(s))")
        return True

    # P28 R13.1: the structured verdict is the review gate input.
    # P28b R1b: calibrated by Issue_Attribution — a scaffolding-only fail
    # does not require approval on verdict grounds.
    if effective_verdict(review_verdict) == VERDICT_FAIL:
        logger.info("Approval required: effective review verdict is fail")
        return True

    logger.info(f"No approval required - checks passed: {checks_passed}/{checks_run}")
    return False


def determine_merged_approval_requirement(
    *,
    config_requires_approval: bool,
    quality_requires_approval: bool,
    regeneration_attempt: int,
) -> tuple[bool, dict[str, bool]]:
    """The Approval_Policy: disjunction of three independent reasons (R2.4).

    Returns the decision **and** the per-input map R3.5 records, so the audit row
    and the decision cannot disagree — they are computed once, together, from one
    dict. A second function returning the map would be a second place for the
    two to drift, and R3.5's whole purpose is that the recorded reason is the
    reason that actually decided.

    Generalises the sibling gate's existing convention rather than inventing one:
    ``schema_mapping.determine_approval_requirement`` checks fallback provenance
    (``schema_mapping/lambda_function.py:960-967``) and
    ``always_approve_schema_changes`` (``:969-975``) **before** it reads its own
    ``schema_mapping_enabled`` flag (``:985``), so a disabled configuration
    already loses to a quality or risk concern in this repo.

    The three disjuncts, each an acceptance criterion:

    * ``config_requires_approval`` — R2.2. The Code_Approval_Setting as resolved
      by :func:`resolve_code_approval_setting`, so R1.2/R1.4's fail-closed
      defaults arrive here already applied. ``True`` forces the gate whatever
      the quality gate concluded, which is R1.1 — a clean run on a pipeline
      whose owner asked for review is still reviewed.
    * ``quality_requires_approval`` — R2.1. :func:`determine_code_approval_requirement`'s
      return, unmerged and unchanged. ``True`` forces the gate whatever the
      configuration says, so code that failed its own tests cannot reach
      ``CreateInfrastructure`` unreviewed on a pipeline whose routine approvals
      are switched off.
    * ``regeneration_attempt > 0`` — R2.3, relocated verbatim in intent from
      ``create_code:331-334``: regenerated code is reviewer-flagged code. The
      escalation becomes the third disjunct rather than a branch inside a
      function nothing reads (``LGR-f077f1b-1``, **template / ratified**:
      ``test_code`` owns the code-generation Approval_Policy and
      ``create_code.determine_approval_requirement`` is deleted rather than
      renamed).

    R1.3 is the residual: approval is not required only when none of the three
    requires it — the setting explicitly ``false``, the quality gate clean, and
    the attempt count ``0``.

    ``regeneration_attempt`` is present and numeric by the time it reaches here:
    the ASL's ``CheckRegenerationAttempt`` guard
    (``orchestration-stack.ts:774-782``) is ``And[IsPresent, IsNumeric, …]`` with
    an explicit apply-default state for the residual, and the handler reads the
    value at ``:1323``. A JSON boolean does not satisfy ``IsNumeric``, so the
    ``bool``-as-``int`` case cannot arrive from the state machine; ``True > 0``
    would in any case evaluate as attempt 1, which is the arithmetically correct
    reading rather than a misbehaviour.

    R2.6 is satisfied structurally, not by a branch: this function takes no
    checks list, so a Not_Verified_Check cannot reach it except through
    ``quality_requires_approval`` — and ``aggregate_check_results`` partitions
    not-verified entries out before the ``all()`` (``:571-572``), so such an
    entry can neither pass nor fail a run.

    Pure: no boto3, no ``try``/``except``, no logging. The caller (task 2.4)
    owns the R7.4 log line and the state-row write.
    """
    inputs = {
        "pipeline_config": config_requires_approval,
        "quality_gate": quality_requires_approval,
        "regeneration": regeneration_attempt > 0,
    }
    return any(inputs.values()), inputs


def build_state_row_update(attributes: dict[str, Any]) -> dict[str, Any]:
    """Build ``update_item`` kwargs that SET exactly ``attributes``, nothing else.

    Returns the ``UpdateExpression`` / ``ExpressionAttributeNames`` /
    ``ExpressionAttributeValues`` trio; the caller supplies ``Key``.

    **Why this exists instead of a full-row ``put_item``.** ``PutItem`` replaces
    the WHOLE item, so a full-row write deletes every attribute it does not
    itself name — including attributes written by a *different* writer, even one
    in the same invocation. That was a live defect: two writes hit the same
    ``(run_id, StepName.TEST_CODE)`` state row inside one ``lambda_handler``
    call — ``store_regeneration_feedback`` SETs ``regeneration_feedback`` /
    ``feedback_timestamp``, then the handler's row write landed ~130ms later and
    deleted both. Observed on run ``run-2026-08-05-13-20-43-433f80b2``: feedback
    stored 13:21:19.150, row written 13:21:19.271, ``create_code`` logged
    "No regeneration feedback found" at 13:21:24.085, and all three generated
    scripts came out byte-identical (sha256 ``e110e570ceafcafd…``) — the
    byte-identical regeneration prompt that p32 2.16 forbids.

    The rule this restores: **no writer clobbers attributes it does not own.**
    Two alternatives were considered and rejected. Folding
    ``regeneration_feedback`` into the row write's ``Item`` leaves
    ``store_regeneration_feedback`` dead on its only production call path.
    Reordering the two writes makes correctness depend on an unwritten invariant
    about statement order that the next edit to this handler would break.

    Every attribute name is written through an ``ExpressionAttributeNames``
    alias. DynamoDB rejects a reserved word appearing literally in an
    ``UpdateExpression``, and two of the names on this row — ``status`` and
    ``timestamp`` — are reserved. Aliasing uniformly rather than case-by-case
    means adding an attribute here can never reintroduce that failure.
    """
    set_clauses = []
    expression_names: dict[str, str] = {}
    expression_values: dict[str, Any] = {}

    for name, value in attributes.items():
        alias = f"#{name}"
        placeholder = f":{name}"
        expression_names[alias] = name
        expression_values[placeholder] = value
        set_clauses.append(f"{alias} = {placeholder}")

    return {
        "UpdateExpression": "SET " + ", ".join(set_clauses),
        "ExpressionAttributeNames": expression_names,
        "ExpressionAttributeValues": expression_values,
    }


def store_regeneration_feedback(run_id: str, feedback_context: dict) -> None:
    """Store detailed feedback context in DynamoDB for code generation retrieval"""
    try:
        test_step_id = run_id
        timestamp = datetime.now(UTC).isoformat()

        pipeline_runs_table.update_item(
            Key={"run_id": test_step_id, "step_name": StepName.TEST_CODE},
            UpdateExpression="SET regeneration_feedback = :feedback, feedback_timestamp = :timestamp",
            ExpressionAttributeValues={":feedback": feedback_context, ":timestamp": timestamp},
        )

        logger.info(f"Regeneration feedback stored for run_id: {run_id}")

    except Exception as e:
        logger.error(f"Error storing regeneration feedback: {e}")


# P28 R13.1: the former ``parse_test_report`` (regex extraction of markdown
# category headings) is DELETED. The single parser is
# ``review_verdict.parse_review_verdict`` — schema-validated JSON, one
# reparse retry, then honest failure. ``verdict_to_parsed_results`` adapts
# the verdict to the ``parsed_results`` shape downstream consumers read.


# p32 2.22: what the Code Interpreter runs is a *pandas approximation* of the
# transform over *generated sample data* — never the Glue/Spark job that runs in
# production. The report used to report execution results without naming what
# executed them, which made "Code validated through execution" (the PRODUCTION
# READY branch) the strongest claim in the report and the least supported (1.22).
# This caveat is a statement about the checker, so it is true of every run —
# including one where the converter refused and nothing executed at all.
#
# V5 records the honest alternative — running the generated code against genuine
# Spark, e.g. a Glue interactive session — as a deferred follow-up with real
# per-run cost. Until that exists, naming the approximation is what stands in
# for it. Do NOT delete this caveat without landing V5.
PANDAS_PROXY_CAVEAT = (
    "the Code Interpreter runs a **pandas approximation** of the transform over "
    "**generated sample data** — not the Glue/Spark job that runs in production"
)

# p42 — the same sentence for the other executor, and it is the inverse claim
# rather than a softer version of the one above.
#
# **The authority for adding this is the caveat above's own revisit condition.**
# ``PANDAS_PROXY_CAVEAT``'s comment ends: "V5 records the honest alternative —
# running the generated code against genuine Spark, e.g. a Glue interactive
# session — as a deferred follow-up with real per-run cost. Until that exists,
# naming the approximation is what stands in for it. Do NOT delete this caveat
# without landing V5." V5 has landed — it is the `real-spark-verification` spec,
# and `VERIFICATION_EXECUTOR=glue` is deployed. So the condition is met, and this
# is a BRANCH rather than a replacement: the pandas caveat is untouched and still
# reaches every pandas run, which is what the fence actually protects.
#
# What this one may and may not claim, stated because overclaiming here is the
# same defect in the other direction:
#
# * It MAY claim real Spark semantics. The transform is submitted to a Glue
#   interactive session and evaluated by Spark, untranslated — that is the whole
#   point of the executor (R1.4, R3.1).
# * It may NOT claim validation against the pipeline's data. `frame_mode` has no
#   caller, so `GlueSessionTestRunner.resolve_frame_source` returns on its first
#   branch every run and the frame is synthesised from the DECLARED schema
#   (R9.3, design C8 — the runner reads no JDBC source at all, by design, and
#   `LGR-819980f-1` is why).
# * It may NOT claim the production job ran. What executes is the transform
#   block plus a frame build and a validation select — not the generated job's
#   auto-cast, DQ or write stages.
#
# Same grammar as `PANDAS_PROXY_CAVEAT` (a clause, no leading capital, no
# trailing full stop) because both are interpolated into the same sentence.
GLUE_SPARK_CAVEAT = (
    "the transform runs on **real Spark**, in a Glue interactive session, over a "
    "**synthetic frame built from the declared source schema** — real Spark semantics, "
    "but not the pipeline's own source data and not the full production Glue job"
)

# ── p44 R5.2/R5.3/R5.4 — the frame the run actually used ─────────────────────
#
# `LGR-5c30733-3` (**template / ratified**) is SUPERSEDED here, and only on one of
# its four claims. Its stated basis ends: "`frame_mode` has no caller, so the
# frame is synthesised from the declared schema." p44 wires the caller, so that
# basis is gone and the Glue wording above is false for *some* runs. The other
# three claims are carried forward unchanged and are still what this code does:
# branch on the RESOLVED executor; take the value from the handler rather than
# reading the environment here; `shadow` maps to the pandas caveat.
#
# **Nothing is deleted** (do-NOT 6, do-NOT 18). `GLUE_SPARK_CAVEAT` above keeps
# its bytes and becomes the SYNTHETIC-FRAME arm, which is the same move
# `LGR-5c30733-3` itself made when it added a second caveat beside
# `PANDAS_PROXY_CAVEAT` rather than replacing it. Ten existing assertions across
# `test_report_names_the_running_executor.py` and `test_comprehensive_report.py`
# therefore keep passing untouched: they described "the Glue report's wording"
# and now describe "the *synthetic-frame* Glue report's wording", which is what
# they always described — synthetic was the only reachable frame.

#: The three values ``FrameSource.provenance()`` puts on its ``frame`` key
#: (``glue_session_runner.py:472-486``). Three and not two: ``sample_unavailable``
#: is not a frame MODE the runner can resolve into ``FrameSource.mode``, it is a
#: provenance distinction between two runs that both got a synthetic frame, which
#: is the whole of R5.4.
#:
#: Spelled here rather than imported, for the reason
#: :data:`SESSION_CAPACITY_REASON` states for its own pair: every test that
#: imports this module stubs its sibling producers with a bare ``MagicMock``, so
#: an *imported* constant arrives as an auto-attribute mock and every comparison
#: against it silently never matches, leaving these arms dead while the suite
#: passes. They are second spellings of the producer's vocabulary, so: **change
#: both or neither.**
#:
#: :data:`FRAME_MODE_SYNTHETIC_DEFAULT` has the same value as
#: :data:`FRAME_SYNTHETIC` and is deliberately NOT the same constant: that one is
#: the environment default the handler passes IN as a requested mode, this one is
#: what a finished run reports having USED. They differ on every run whose
#: requested sample was unavailable.
FRAME_SYNTHETIC: Final = "synthetic"
FRAME_SAMPLE: Final = "sample"
FRAME_SAMPLE_UNAVAILABLE: Final = "sample_unavailable"

#: The Glue caveat for a run whose frame was read from the pipeline's own source
#: object. The inverse of :data:`GLUE_SPARK_CAVEAT`'s second clause, and the ONLY
#: arm permitted to claim verification against the pipeline's own data (R5.5).
#:
#: Same grammar as the other two (a clause, no leading capital, no trailing full
#: stop) because all three are interpolated into the same sentence. The row bound
#: is a ``{row_bound}`` placeholder filled from the provenance rather than the
#: literal 100, so it cannot drift from ``glue_session_runner._SAMPLE_FRAME_ROWS``
#: — which is itself written as ``_SYNTHETIC_FRAME_ROWS`` for the same reason.
GLUE_SPARK_SAMPLE_CAVEAT = (
    "the transform runs on **real Spark**, in a Glue interactive session, over a "
    "**bounded {row_bound}-row sample of this pipeline's own source object** — real Spark "
    "semantics over real source data, but not the full production Glue job"
)

#: The Glue caveat for a run that ASKED for a sample and could not have one. It
#: carries :data:`GLUE_SPARK_CAVEAT`'s claim — the frame was synthetic and real
#: source behaviour is unverified — plus the fact that a sample was requested and
#: the recorded reason it was not built (R5.4).
#:
#: A separate arm rather than the synthetic wording, because the two runs are
#: different facts: one never asked, one asked and was refused, and only the
#: second tells a reader to go and look at the source object. That is the same
#: distinction ``FrameSource.sample_unavailable`` exists to make
#: (``glue_session_runner.py:409-415``), and C9 / do-NOT 7 are why it is a
#: *report* distinction and not a ``not_verified_reason``: a fallback to synthetic
#: is a fully verified run against a generated frame.
#:
#: **It is shorter than the design's draft, and the reason is measurement rather
#: than taste** (design §3, C15's method applied to this arm). This is the longest
#: of the three caveats — it carries a whole extra fact plus a reason id of 32-38
#: characters — and the draft's 297-303 rendered characters fell OUTSIDE
#: ``review_summary``'s 500-character excerpt on **every** verdict branch, which
#: would have left R5.7 unmet for this frame. Dropping "real Spark semantics, but"
#: costs nothing a reader loses: the sentence this clause opens continues "Any
#: execution result below describes that Spark run", and the section heading two
#: lines above says "Real Spark Execution Results (Glue interactive session)". At
#: 242-248 rendered it is inside the excerpt on the three branches where it is the
#: only frame claim there; on ``PRODUCTION READY`` and the legacy no-verdict branch
#: it still overflows, and on exactly those two the ``overall_status``
#: parenthetical carries the frame claim instead (86-197). So R5.7 holds on every
#: frame x verdict cell, with the carrier differing by branch — the two-placement
#: structure C15 measured, used as two placements.
GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT = (
    "a real-source sample was requested and could not be read ({reason}), so the frame is "
    "a **synthetic frame built from the declared source schema** — not the pipeline's own "
    "source data and not the full production Glue job"
)

#: The Glue caveat for a run whose Frame_Source WAS a Sample_Frame and whose
#: harness then verified nothing — p44 R2.5, task 12.5.
#:
#: **The arm that was missing, and it is the dishonest cell.**
#: :data:`GLUE_SPARK_SAMPLE_CAVEAT` is selected from
#: ``frame_provenance["frame"]``, which is ``"sample"`` whenever
#: ``FrameSource.is_sample`` — including on a run whose source read FAILED, where
#: nothing executed. Measured rather than reasoned about: rendering a real
#: ``glue_session_runner.sample_read_failure_results(...)`` through this builder
#: emitted "real Spark semantics over real source data" for a run whose frame-build
#: statement died on ``AccessDenied``. That is the claim R5.5 exists to forbid and
#: the direction p42 spent a spec removing.
#:
#: So the sample arm is **not** reachable on a run the harness could not verify,
#: and this arm — which claims nothing — is. It is selected by the frame AND the
#: run's own ``executed`` flag, never by the frame alone; see the selection at the
#: builder, and :data:`_GLUE_FRAME_CAVEATS` for why this is not a fourth key in
#: that mapping.
#:
#: Named for what happened rather than for the reason id, because the reason is
#: rendered separately in the execution section from
#: :data:`NOT_VERIFIED_REASON_LABELS`, and both doors onto
#: :data:`SOURCE_READ_FAILED_REASON` — a failed statement and an elapsed deadline —
#: reach this one string.
#:
#: Same grammar as its three siblings (a clause, no leading capital, no trailing
#: full stop) because all four are interpolated into the same sentence, and it
#: carries the ``{row_bound}`` placeholder for the same reason
#: :data:`GLUE_SPARK_SAMPLE_CAVEAT` does. It names **neither executor's**
#: vocabulary — no "pandas", and no claim that Spark ran — because on this path
#: neither ran anything.
GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT = (
    "the frame was to be a **bounded {row_bound}-row sample of this pipeline's own source "
    "object** and that read did not succeed, so the transform **never ran** and **nothing "
    "about it was verified** — neither Spark semantics nor behaviour on real source data"
)

#: Which caveat the Glue arm carries, by the frame the run actually used.
#:
#: A SECOND mapping composed after :data:`_EXECUTOR_CAVEATS` rather than re-keying
#: that one on ``(executor, frame)``, because the frame dimension does not exist
#: for the other two executors and a two-dimensional table would invent cells
#: that cannot occur. Under ``shadow`` the authoritative result is the pandas
#: one, which has no frame source at all — ``CodeInterpreterTestRunner`` takes
#: ``region`` and nothing else (``code_interpreter_runner.py:223``) — and R1.6
#: additionally pins shadow's Glue half to a synthetic frame.
#:
#: Every value here is read through ``str.format``, so **do not put a literal
#: brace in one of these strings** unless it is a placeholder.
#:
#: **Three keys and not four**, deliberately (task 12.5).
#: :data:`GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT` is NOT a fourth entry here,
#: because it is not a fourth frame: ``FrameSource.provenance()`` emits exactly
#: three ``frame`` values and task 5.2's ``test_frame_provenance.py`` pins that as
#: an equality, so a fourth key would either need a fourth provenance value or a
#: value only this dict knows about. It is a **sample run that verified nothing** —
#: the frame plus the run's outcome — so it is selected by a condition at the
#: builder instead. Adding it here would also force matching entries in
#: :data:`_GLUE_FRAME_STATUS_NOTES` and :data:`_GLUE_FRAME_DECISION_CLAUSES`,
#: whose consumers are both unreachable on a non-executed run, so two of the three
#: would exist only to stop a ``.get`` falling back.
#:
#: ``None`` is in the key DOMAIN even though it is not a key: a ``glue`` run that
#: reached the builder with no ``frame_provenance`` — the ``getattr`` default at
#: the handler, and the ~47 direct test call sites — looks up ``None`` and takes
#: the synthetic arm by falling back. The three mappings below are annotated
#: ``str | None`` so that lookup type-checks as the intended behaviour rather than
#: as an error; the dict literals and every value are unchanged.
_GLUE_FRAME_CAVEATS: Final[dict[str | None, str]] = {
    FRAME_SYNTHETIC: GLUE_SPARK_CAVEAT,
    FRAME_SAMPLE: GLUE_SPARK_SAMPLE_CAVEAT,
    FRAME_SAMPLE_UNAVAILABLE: GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT,
}

#: The parenthetical on the Glue arm of ``overall_status``, by frame.
#:
#: **This placement is measured, not preferred** (design §3, C15). The executor
#: caveat spans report offsets 253-486 and ``overall_status`` spans 86-156,
#: leaving **14 characters** before the 500-character boundary
#: ``code_quality_metrics.review_summary`` slices at (`:3143`). So R5.7 —
#: the frame statement inside the excerpt — is NOT satisfiable by appending a
#: sentence (do-NOT 19). Both strings that already carry a frame claim are inside
#: the budget, so branching them makes R5.7 hold by construction.
#:
#: The synthetic value is byte-identical to what p42 shipped, which is what keeps
#: ``test_the_overall_status_line_names_the_executor`` passing unedited.
#: ``str | None`` key for the reason given at :data:`_GLUE_FRAME_CAVEATS`.
_GLUE_FRAME_STATUS_NOTES: Final[dict[str | None, str]] = {
    FRAME_SYNTHETIC: "synthetic input data",
    FRAME_SAMPLE: "real source sample, {row_bound} rows",
    FRAME_SAMPLE_UNAVAILABLE: "synthetic input data — a sample was requested and unavailable",
}

#: The second half of the Glue arm's PRODUCTION READY deployment decision, by
#: frame — everything after "…in a Glue interactive session; ".
#:
#: This one sits past character 500, so it is not what satisfies R5.7. It is here
#: because R5.5 forbids an own-source claim on a non-sample run and this is one of
#: the places that claim is made or withheld. The synthetic value plus the fixed
#: prefix at the site reproduces p42's sentence byte for byte, which is what keeps
#: ``test_the_production_ready_deployment_decision_names_the_executor`` passing
#: unedited — including its ``"pandas" not in decision`` assertion, so no arm here
#: may name pandas.
#: ``str | None`` key for the reason given at :data:`_GLUE_FRAME_CAVEATS`.
_GLUE_FRAME_DECISION_CLAUSES: Final[dict[str | None, str]] = {
    FRAME_SYNTHETIC: (
        "the input was a **synthetic frame built from the declared source schema**, so "
        "Spark semantics are verified and behaviour on real source data is not"
    ),
    FRAME_SAMPLE: (
        "the input was a **bounded {row_bound}-row sample of this pipeline's own source "
        "object**, so Spark semantics are verified and so is behaviour on those sampled "
        "rows — the rest of that object is not"
    ),
    FRAME_SAMPLE_UNAVAILABLE: (
        "a real-source sample was requested and could not be read ({reason}), so the input "
        "was a **synthetic frame built from the declared source schema** — Spark semantics "
        "are verified and behaviour on real source data is not"
    ),
}

#: Which caveat each executor's report carries. A mapping rather than an
#: ``if``/``else`` at each of the four sites below, so a third executor cannot be
#: added to the resolution (`resolve_effective_executor`) while leaving one of the
#: report's claims still naming pandas.
#:
#: ``shadow`` maps to the PANDAS caveat, and that is correct rather than a
#: fallback: under `shadow` the pandas proxy is the authoritative executor
#: (R4.1), the Glue half is non-authoritative and its result reaches only the
#: `shadow_verification` row (`LGR-ad78946-3`), and `execution_results` — the
#: dict this report describes — is the pandas one. A report that named Spark
#: under `shadow` would attribute the verdict to the run that did not produce it.
_EXECUTOR_CAVEATS: Final = {
    VERIFICATION_EXECUTOR_PANDAS: PANDAS_PROXY_CAVEAT,
    VERIFICATION_EXECUTOR_SHADOW: PANDAS_PROXY_CAVEAT,
    VERIFICATION_EXECUTOR_GLUE: GLUE_SPARK_CAVEAT,
}


def generate_comprehensive_test_report(
    static_analysis_report: str,
    execution_results: dict,
    parsed_results: dict | None = None,
    *,
    executor: str = VERIFICATION_EXECUTOR_PANDAS,
    frame_provenance: dict | None = None,
) -> str:
    """Generate comprehensive test report combining static analysis and Code Interpreter results.

    The "Combined Assessment" verdict factors in BOTH signals:

      * Code Interpreter execution (does the transform actually run and
        produce records?), and
      * static analysis (the per-category code review).

    Execution success alone does NOT make code production-ready: a transform
    can run cleanly on sample data while still failing blocking static checks
    (e.g. a security flaw or a correctness bug). Treating execution success as
    an automatic "APPROVE / deploy" — as a prior version did — produced
    misleading reports that recommended deploying code the review had flagged.

    Verdict matrix (when ``parsed_results`` is supplied):
      * execution failed                          → NOT PRODUCTION READY (reject)
      * execution ok + blocking static failure    → REVIEW REQUIRED (do not auto-deploy)
      * execution ok + only advisory failures      → APPROVE WITH FIXES
      * execution ok + no static failures           → PRODUCTION READY (approve)

    Blocking categories are Correctness, Security, and Schema Compliance; the
    remaining categories (Error Handling, AWS Best Practices, Performance) are
    treated as advisory for the deploy decision but are still surfaced.

    p32 2.22 — what the "execution" half of that matrix is worth: on the pandas
    executor it is ``PANDAS_PROXY_CAVEAT``, a pandas approximation over generated
    sample data, not Glue/Spark. The four rows above are unchanged; every
    execution claim in the rendered report names what produced it, and the
    PRODUCTION READY branch does not claim validation on that basis.

    p42 — *which* executor produced it, which is what ``executor`` is for. The
    report used to state the pandas caveat unconditionally, so a run verified on
    real Spark told the operator it was a pandas approximation: observed on run
    ``run-2026-08-18-17-48-19-bd6f4deb`` with ``VERIFICATION_EXECUTOR=glue``
    deployed. Four claims were wrong on that path — the section heading, the
    scope caveat, ``overall_status`` and the PRODUCTION READY deployment
    decision — and each is now resolved from ``executor`` rather than fixed.

    **Passed in, never read from the environment here.** This function is a pure
    builder over its arguments, and it has 30-odd test call sites that construct
    reports directly; an ``os.environ`` read inside it would make every one of
    them depend on ambient state and would diverge from the handler's own
    resolution, which applies ``resolve_effective_executor``'s fallbacks
    (``LGR-ad78946-5``: a ``glue`` request with no role ARN resolves to
    ``pandas``). The handler resolves once and tells the report what actually
    ran.

    **The keyword default stays ``pandas`` even though the resolver's
    fall-through is now ``glue``, and the divergence is deliberate.** This
    parameter governs what the report *claims*, not what runs, and the two
    defaults answer different questions. The handler always passes the resolved
    value explicitly (`:2777`), so this default is only ever reached by a
    caller that forgot — 30-odd of them in the test suite. A ``pandas`` default
    makes such a caller UNDERSTATE what ran: the report carries the proxy
    caveat for a run that may have used real Spark, which is conservative and
    visible. A ``glue`` default would make it claim real Spark on a run that
    may never have had it — the overclaim direction that p42 and p43 both exist
    to remove (p42: a Spark run told the operator it was a pandas
    approximation; p43: "could not check" reading as "checked and passed"). So
    matching the resolver here would be symmetry bought at the cost of the one
    property this argument was added to protect.

    **The not-verified arms keep their pandas wording, deliberately.** They are
    unreachable on the Glue executor: ``not_verified_by_proxy`` has no producer
    in ``glue_session_runner`` (``LGR-ad78946-2``, and Property 4 asserts it), so
    the refusal copy below cannot be reached by a Spark run and does not need a
    branch. p32 2.22's fence stands where it applies.

    p44 R2.5 (task 12.5) — the OTHER non-verification, which this builder used to
    have no arm for at all. ``not_verified_by_harness`` says the harness could not
    get far enough to run anything: five reasons, two of them pandas-side and
    three Spark-side. Every one of them arrived here carrying
    ``execution_successful: False`` and fell to the execution arm, so the report
    stated an execution status, a record count and an "Execution Error Details"
    block for a transform that was never submitted — p32 1.21's observation
    verbatim, on a marker p32 did not cover. That was **pre-existing for p42's and
    p33's four reasons** and is fixed for all five together, because it is one
    defect. Which VERDICT such a run reaches is unchanged: ``LGR-334667e-1``'s
    matrix is untouched and the arm below alters only what the execution section
    says about its own evidence.

    p32 2.20/2.21 — the input those four rows never contemplated: a run in which
    the proxy REFUSED the transform, so nothing executed at all. Every row above
    describes an execution, so a refusal takes none of them:

      * refused + blocking static failure  → REVIEW REQUIRED (3.23 — a refusal
        adds one correctly-classified input to the gate and must not weaken it,
        so a blocking static failure still blocks)
      * refused + nothing blocking failed  → NOT VERIFIED BY THE PANDAS PROXY,
        naming the construct, the line and which reason applies — 2.23's two,
        plus p40 R4's third (:data:`SCALAR_LITERAL_RECEIVER_REASON`), each with
        its own reason text and its own next step

    That second wording is deliberately neither ``PRODUCTION READY`` nor
    ``NOT PRODUCTION READY``: the first is a claim an unverified run cannot
    support (1.22) and the second is a verdict on an execution that did not
    happen (1.21). ``LGR-334667e-1``'s fence — execution success never
    overrides a blocking static failure — is untouched.

    Args:
        static_analysis_report: Original static analysis report from AI agent
        execution_results: Results from the selected runner's ``run_test()`` —
            ``CodeInterpreterTestRunner`` or ``GlueSessionTestRunner``, which
            return the same nine keys (R1.1).
        parsed_results: Structured output of :func:`parse_test_report`. When
            omitted, falls back to an execution-only verdict (legacy behaviour).
        executor: The Verification_Executor that produced *execution_results* —
            one of :data:`VERIFICATION_EXECUTOR_PANDAS`,
            :data:`VERIFICATION_EXECUTOR_GLUE`,
            :data:`VERIFICATION_EXECUTOR_SHADOW`. An unrecognised value is
            treated as ``pandas`` — it no longer "matches the resolver", whose
            own fall-through is now ``glue``. The report's fall-through is
            deliberately the conservative one for the reason given above the
            parameter list: an unrecognised label here must understate what ran,
            never claim Spark. Not raising is unchanged (``LGR-ad78946-5``): a
            report is not the place to fail a run over a flag.
        frame_provenance: ``FrameSource.provenance()`` for the run that produced
            *execution_results*, or ``None``. p44 R5.1/R5.2/R5.3/R5.4 — WHICH
            frame that Spark run was measured against, which the executor alone
            cannot say once ``frame_mode`` has a caller.

            **Keyword-only, with a default, and no required parameter is added.**
            ``None`` selects :data:`GLUE_SPARK_CAVEAT` — the synthetic arm — which
            is why all 47 direct test call sites keep their current meaning. The
            reason is the one given above for ``executor``'s default and it applies
            verbatim: this is a pure builder with dozens of callers that construct
            reports directly, and a required argument would rewrite every one of
            them to state a fact most of them are not about.

            **Passed, never re-derived** (R5.1). An ``os.environ`` read here would
            claim a frame the run did not use, exactly as it would have claimed a
            Spark run that did not happen — `LGR-5c30733-3`'s own argument one
            level down. Two ratified precedents say the same in other domains:
            ``LGR-9e76249-2`` (derive once on the read path; mirroring the
            derivation in a second consumer explicitly rejected) and
            ``LGR-28b4d75-1`` ("derived once … and used by both"). The handler
            reads it off the runner instance the run left it on and passes it.

            Read only on the ``glue`` arm: the other two executors have no frame
            source, so an unrecognised or absent value there is inert rather than
            defaulted.

    Returns:
        str: Enhanced test report with execution results
    """
    execution_successful = execution_results.get("execution_successful", False)
    output_records = execution_results.get("output_records", 0)
    input_records = execution_results.get("input_records", 0)
    data_quality = execution_results.get("data_quality_results", {})
    error_details = execution_results.get("error_details")

    # p32 2.20/2.21 (C5, refusal direction): a REFUSAL and a genuine execution
    # FAILURE both arrive carrying ``execution_successful: False``, so that flag
    # alone cannot tell them apart — which is why every statement below used to
    # describe an execution that never happened (1.21). The discriminator is the
    # marker ``_conversion_failure_results`` carries and that
    # ``aggregate_check_results`` already reads; ``_failed_execution_results``
    # and ``_empty_results`` deliberately do NOT carry it (3.22), so a real
    # failure and a missing run keep the failure wording.
    not_verified = bool(execution_results.get("not_verified_by_proxy"))
    # p44 R2.5 (task 12.5): the OTHER marker, and the one the suppression above
    # was missing. A harness non-verification has the same nothing-ran shape as a
    # proxy refusal — `session_capacity_results`, `frame_refusal_results`,
    # `sample_read_failure_results`, `sample_read_deadline_results` and
    # `code_interpreter_runner._frame_truncation_results` all build on the
    # "nothing ran, nothing proven" defaults — but it carries
    # `not_verified_by_harness`, so every statement below described an execution
    # that never happened, exactly as p32 1.21 observed for the proxy.
    #
    # Measured, not reasoned about: rendering a real `sample_read_failure_results`
    # through this builder emitted "**Execution Status:** ❌ FAILED",
    # "**Records Processed:** 0 → 0 records" and an "Execution Error Details"
    # block for a run whose transform statement was never submitted.
    #
    # **`executed` is tested too, and that is not belt-and-braces.** The marker
    # says the harness refused; `executed` says whether anything ran. Every
    # producer sets both, so on real input the two agree — and requiring the
    # agreement means a malformed result that claims a refusal over a genuine
    # execution keeps the execution wording rather than having its status, its
    # record counts and its error silently suppressed. Suppressing what a run
    # actually observed is the mirror image of the defect being fixed here.
    not_verified_by_harness = bool(execution_results.get("not_verified_by_harness")) and not execution_results.get(
        "executed", False
    )
    nv_construct = execution_results.get("not_verified_construct") or "unknown construct"
    nv_line_number = execution_results.get("not_verified_line_number")
    nv_source_line = execution_results.get("not_verified_source_line") or ""
    nv_reason = execution_results.get("not_verified_reason") or "not_yet_expressible"
    nv_permanent = nv_reason == "permanently_inexpressible"
    # p40 R4: the THIRD reason, derived once here beside `nv_permanent` because
    # the same distinction is needed twice — in the reason text below and in the
    # next step at the verdict. Spelled against this module's own
    # `SCALAR_LITERAL_RECEIVER_REASON` (see its docstring for why the producer's
    # constant is not imported at this seam).
    nv_scalar_literal = nv_reason == SCALAR_LITERAL_RECEIVER_REASON

    # p42: which executor's claims this report may make. Resolved ONCE here and
    # read at each of the four sites, so the heading, the caveat, the status
    # string and the deployment decision cannot disagree with each other about
    # what ran. An unrecognised value takes the pandas arm (R2.3's posture).
    ran_on_spark = executor == VERIFICATION_EXECUTOR_GLUE
    executor_caveat = _EXECUTOR_CAVEATS.get(executor, PANDAS_PROXY_CAVEAT)

    # p44 R5.1: which FRAME that executor was given, resolved ONCE here for the
    # same reason `ran_on_spark` is — three sites read it (the scope caveat,
    # `overall_status` and the PRODUCTION READY deployment decision) and they must
    # not be able to disagree about one run's frame.
    #
    # Read off the argument, never from the environment (see the parameter's own
    # note). `.get` rather than `[...]`: this dict crosses a module boundary, and
    # a missing key must fall to the conservative synthetic arm rather than raise
    # inside a report builder (`LGR-ad78946-5`: a report is not the place to fail a
    # run over a flag).
    frame = frame_provenance.get("frame") if frame_provenance else None
    frame_row_bound = frame_provenance.get("row_bound") if frame_provenance else None
    frame_unavailable_reason = frame_provenance.get("unavailable_reason") if frame_provenance else None
    # p44 R2.5/R5.5 (task 12.5): a Sample_Frame run whose harness verified nothing.
    #
    # `frame` says which frame the run was GOING to be measured against;
    # `not_verified_by_harness` says the measurement never happened. The sample
    # caveat and the sample scope tail are the only two strings in this builder
    # that CLAIM verification against the pipeline's own data, and on this
    # combination both claims are false — the read is what failed. So the frame
    # alone may not select them.
    #
    # Derived here beside `frame` rather than at the two sites, for the reason
    # `frame` itself is derived once: two sites read it, and a run whose caveat
    # said one thing while the sentence after it said another is the p42 defect in
    # miniature.
    frame_sample_unverified = frame == FRAME_SAMPLE and not_verified_by_harness
    # Only the `glue` arm has a frame dimension, so the other two executors never
    # reach these and their `_EXECUTOR_CAVEATS` selection above stands unchanged.
    # An unrecognised `frame` value takes the SYNTHETIC arm — the conservative
    # direction, the same posture the unrecognised `executor` takes: understate
    # what was verified, never claim the pipeline's own data (R5.5).
    glue_status_note = _GLUE_FRAME_STATUS_NOTES[FRAME_SYNTHETIC]
    glue_decision_clause = _GLUE_FRAME_DECISION_CLAUSES[FRAME_SYNTHETIC]
    if ran_on_spark:
        # One substitution map for all three, so a placeholder cannot mean one
        # thing in the caveat and another in the status line. `row_bound` is
        # interpolated unguarded on purpose: `provenance()` sets it from
        # `_SAMPLE_FRAME_ROWS` on every sample, so a `sample` frame arriving
        # without one is malformed input and should read as visibly wrong rather
        # than be papered over with an invented number.
        frame_fields = {
            "row_bound": frame_row_bound,
            "reason": frame_unavailable_reason or "reason not recorded",
        }
        # p44 R2.5/R5.5 (task 12.5): a sample whose harness verified nothing takes
        # its own caveat instead of the mapping's sample arm. Written as an
        # override of the mapping lookup rather than as a fourth mapping key —
        # `_GLUE_FRAME_CAVEATS`' own docstring records why — and applied only to
        # the caveat, because the other two strings are rendered exclusively on the
        # PRODUCTION READY branch, which a non-executed run cannot reach.
        if frame_sample_unverified:
            executor_caveat = GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT.format(**frame_fields)
        else:
            executor_caveat = _GLUE_FRAME_CAVEATS.get(frame, GLUE_SPARK_CAVEAT).format(**frame_fields)
        glue_status_note = _GLUE_FRAME_STATUS_NOTES.get(frame, glue_status_note).format(**frame_fields)
        glue_decision_clause = _GLUE_FRAME_DECISION_CLAUSES.get(frame, glue_decision_clause).format(**frame_fields)

    # p32 2.22: name what executes before reporting anything it produced. The
    # sentence is about the checker, not about this particular run, so it stays
    # true whether or not an execution happened — which is why it is not branched
    # on `execution_successful`, on either executor.
    if ran_on_spark:
        # p44 R5.5: the sentence after the caveat is a frame claim too, and on a
        # Sample_Frame the p42 wording is the false half of it — the report would
        # state real source data was verified in the caveat and unverified in the
        # next sentence. Only the `sample` arm needs different words: it is
        # accurate as written for a synthetic frame AND for a requested sample that
        # could not be read, so those two keep p42's bytes exactly.
        #
        # The tail carries its own `\n> ` because it continues a blockquote line;
        # that is what keeps the non-sample rendering byte-identical to p42's.
        #
        # p44 R2.5 (task 12.5): `frame_sample_unverified` excludes the sample arm
        # for a run the harness could not verify. The else branch is then *true* on
        # that path rather than merely safe — the pipeline's real source data was
        # indeed not verified by it, because the read of it is what failed — so no
        # third tail is needed and p42's bytes carry it.
        if frame == FRAME_SAMPLE and not frame_sample_unverified:
            scope_tail = (
                "and behaviour on those sampled rows of the pipeline's own source object\n"
                "> is verified by it — the rest of that object is not"
            )
        else:
            scope_tail = "and behaviour on the pipeline's real source data is\n> not verified by it"
        execution_section = f"""
## 🚀 Real Spark Execution Results (Glue interactive session)

> **Scope of this check:** {executor_caveat}. Any execution result below
> describes that Spark run, {scope_tail}.
"""
    else:
        execution_section = f"""
## 🚀 Code Interpreter Execution Results

> **Scope of this check:** {executor_caveat}. Any execution result below
> describes that approximation, and Spark semantics are not verified by it.
"""

    if not_verified:
        # p32 2.20/2.21: the transform never ran, so there is no execution
        # status, no record count and no execution error to report — printing
        # "❌ FAILED", "0 → 0 records" and an "Execution Error Details" block
        # here is what 1.21 observed verbatim on the live run. What IS reported
        # is the non-verification itself: "not verified" must not be
        # implemented as "not reported" (2.20), so the construct, the line and
        # which reason applies are all named, as text — 2.23's two reasons and
        # p40 R4's third.
        if nv_permanent:
            nv_reason_text = (
                "no single-frame pandas equivalent exists, so this proxy will never express "
                "the construct. This is **not** a defect in the generated PySpark and there is "
                "nothing here to fix"
            )
        elif nv_scalar_literal:
            # p40 R4. The register matches the other two arms — what did not
            # happen, and whose fault it is — but the substance is a third
            # thing, and 3.7 forbids blurring it into either: this is not a
            # construct the proxy can never reach (the arm above) and not a gap
            # someone should close (the arm below). The proxy CHOSE to decline
            # this receiver shape, because the alternatives were pandas that
            # raises and pandas that quietly disagrees with Spark.
            nv_reason_text = (
                "the transform applies a pandas operation that only exists on a column to a "
                "constant value. The proxy **declines** that shape on purpose rather than emit "
                "pandas that raises, or pandas that quietly disagrees with Spark about the "
                "column's values. The generated PySpark is **valid**, and the one shape here "
                "worth converting — a string cast over a literal — already converts"
            )
        else:
            nv_reason_text = (
                "the converter cannot **yet** express this construct — a gap in the checker, "
                "**not** a defect in the generated PySpark"
            )
        execution_section += f"""
### ⏸️ Not Verified by the Pandas Proxy

The transform was **never executed**, so this report states no execution status,
no record counts and no execution error — there was no execution to describe.
A non-verification is not a failure: it is the absence of evidence in either
direction.

- **Construct not converted:** `{nv_construct}`
- **At line:** {nv_line_number}
- **Source line:** `{nv_source_line}`
- **Reason (`{nv_reason}`):** {nv_reason_text}.
"""
    elif not_verified_by_harness:
        # p44 R2.5 (task 12.5) — the SAME suppression, for the other marker, and
        # p32 2.20's rule applies unchanged: "not verified" must not be implemented
        # as "not reported". So the three false statements go and the
        # non-verification itself is stated, as text.
        #
        # **Its own words, not the arm above's.** Routing a harness refusal through
        # `⏸️ Not Verified by the Pandas Proxy` would put a pandas phrase on a Glue
        # run — the misattribution p42 and p44 both exist to remove — and this arm
        # is reachable on BOTH executors (`code_interpreter_runner`'s two width
        # reasons are pandas-side; the three `glue_session_runner` reasons are
        # Spark-side), so neither executor's vocabulary belongs in it. It also has
        # no construct and no line number to name, which is C14's original reason
        # for `not_verified_by_harness` being a separate check name in
        # `aggregate_check_results`; the same argument decides the report's copy.
        #
        # What identifies the failure instead is the REASON, through
        # `NOT_VERIFIED_REASON_LABELS` — the map task 4.1 extended to five harness
        # reasons and that
        # `tests/contracts/test_not_verified_reason_labels_cross_language.py`
        # already requires an entry in for every declared reason. So a sixth reason
        # inherits this rendering instead of needing its own arm, and a reason with
        # no label cannot silently degrade this section to a bare id.
        #
        # `error_details` is rendered as **what the harness reported**, never under
        # the "Execution Error Details" heading: on this path it is not the
        # transform's error, and for the two Sample_Read_Failure doors it is the
        # sentence that names the source read and the object, which is the first
        # half of what R2.5 asks the report to say.
        harness_reason = execution_results.get("not_verified_reason") or ""
        harness_reason_label = NOT_VERIFIED_REASON_LABELS.get(
            harness_reason, "the test harness could not verify this run"
        )
        harness_detail = error_details or "no further detail was reported"
        execution_section += f"""
### ⏸️ Not Verified by the Test Harness

The transform was **never executed**, so this report states no execution status,
no record counts and no execution error — there was no execution to describe.
A non-verification is not a failure: it is the absence of evidence in either
direction, and there is nothing in the generated PySpark that this run found.

- **What the harness could not do (`{harness_reason}`):** {harness_reason_label}.
- **Reported by the harness:** {harness_detail}
"""
    else:
        execution_status = "✅ SUCCESS" if execution_successful else "❌ FAILED"
        execution_section += f"""
**Execution Status:** {execution_status}
**Records Processed:** {input_records} → {output_records} records
"""

        if data_quality:
            execution_section += f"""
### Data Quality Assessment
- **Overall Quality Score:** {data_quality.get("overall_quality_score", 0)}/100
- **Completeness:** {data_quality.get("completeness_score", 0)}/100
- **Uniqueness:** {data_quality.get("uniqueness_score", 0)}/100
- **Schema Compliance:** {data_quality.get("schema_compliance", 0)}/100
- **Duplicates:** {data_quality.get("duplicates", 0)}
- **Missing Columns:** {data_quality.get("missing_columns", [])}
"""

        if not execution_successful and error_details:
            execution_section += f"""
### ❌ Execution Error Details
```
{error_details}
```
"""

    # ── Combined verdict — respects BOTH execution and static analysis ──
    BLOCKING_CATEGORIES = {"Correctness", "Security", "Schema Compliance"}

    # P28 R10.6: static counts are derived from the parsed per-category
    # verdicts (structural extraction), never from LLM-invented numeric totals.
    static_total = 0
    static_passed = 0
    static_failed = 0
    blocking_failures: list[str] = []
    advisory_failures: list[str] = []
    recommendation = ""
    if parsed_results:
        recommendation = str(parsed_results.get("recommendation", "")).upper()
        static_categories = parsed_results.get("test_categories") or {}
        static_total = len(static_categories)
        for category, info in static_categories.items():
            if info.get("passed", False):
                static_passed += 1
            else:
                static_failed += 1
                if category in BLOCKING_CATEGORIES:
                    blocking_failures.append(category)
                else:
                    advisory_failures.append(category)

    # A REJECT recommendation from the reviewer is itself blocking even if the
    # per-category breakdown didn't parse into a known blocking category.
    has_blocking = bool(blocking_failures) or recommendation == "REJECT"

    if not_verified:
        # p32 2.20/2.21/2.22 — the transform never ran. The four rows of
        # `LGR-334667e-1`'s matrix all describe an execution, so none of them
        # applies: a refusal may reach neither "❌ NOT PRODUCTION READY" (a
        # verdict on an execution that did not happen, 1.21) nor "✅ PRODUCTION
        # READY" (a claim an unverified run cannot support, 1.22).
        #
        # 3.23 is why this is a split and not a bypass: a refusal adds one
        # correctly-classified input to the gate and must NOT weaken it, so a
        # blocking static failure still reaches the blocking verdict. The
        # not-verified wording is only reachable when nothing blocking failed.
        if parsed_results and has_blocking:
            blockers = ", ".join(blocking_failures) if blocking_failures else "reviewer recommended REJECT"
            production_ready = "⚠️ **REVIEW REQUIRED**"
            deployment_decision = (
                f"DO NOT AUTO-DEPLOY - static analysis flagged blocking issues ({blockers}); the "
                f"transform was additionally never exercised (the proxy could not express "
                f"'{nv_construct}' on line {nv_line_number}), so there is no execution evidence in "
                "either direction"
            )
            next_steps = "Resolve the failed blocking checks, then re-test before deploying"
            overall_status = "STATIC ANALYSIS BLOCKING ISSUES — TRANSFORM NOT VERIFIED BY THE PANDAS PROXY"
        else:
            if static_failed > 0:
                advisories = ", ".join(advisory_failures) if advisory_failures else "see findings below"
                static_note = f"static analysis raised advisory findings ({advisories})"
                status_note = "advisory static findings"
            else:
                static_note = "static analysis passed"
                status_note = "static analysis passed"
            # 2.23 in the report: the reasons mean different things to a
            # reviewer, so they get different next steps. p40 R4 adds the third.
            if nv_permanent:
                next_steps = (
                    "Judge the transform on static analysis and, if it matters, a genuine Spark run — "
                    "this construct is permanently outside the proxy, so there is nothing to fix here"
                )
            elif nv_scalar_literal:
                # p40 R4: NEITHER of the other two next steps. Not "extend the
                # converter" — the converter is doing what it was asked to do,
                # and the receiver shape worth converting already converts. Not
                # "nothing to fix, ever" either — that belongs to a construct no
                # single-frame proxy can express, which this is not. What is
                # left for the reviewer is the static analysis, and knowing that
                # the silence here is a decision rather than a shortfall.
                next_steps = (
                    "Judge the transform on static analysis — the proxy declines this receiver shape "
                    "deliberately, and the shape worth converting already converts, so there is no "
                    "converter work to schedule and no defect to send back to code generation"
                )
            else:
                next_steps = (
                    "Judge the transform on static analysis, and extend the converter to express this "
                    "construct — the gap is in the checker, not in the generated PySpark"
                )
            production_ready = "⏸️ **NOT VERIFIED BY THE PANDAS PROXY**"
            deployment_decision = (
                f"NEEDS HUMAN JUDGEMENT - {static_note}, but the pandas proxy could not express "
                f"'{nv_construct}' on line {nv_line_number}, so the transform never ran. That is "
                "neither evidence of a defect in the generated PySpark nor evidence that it works"
            )
            overall_status = f"NOT VERIFIED BY THE PANDAS PROXY — NO EXECUTION EVIDENCE ({status_note})"
    elif not execution_successful:
        production_ready = "❌ **NOT PRODUCTION READY**"
        deployment_decision = "REJECT - Fix execution issues first"
        next_steps = "Debug and regenerate code"
        overall_status = "FAILED - EXECUTION ISSUES DETECTED"
    elif parsed_results and has_blocking:
        # Execution passed but the review found blocking issues — do NOT
        # auto-approve. This is the case the old logic mishandled.
        blockers = ", ".join(blocking_failures) if blocking_failures else "reviewer recommended REJECT"
        production_ready = "⚠️ **REVIEW REQUIRED**"
        deployment_decision = (
            "DO NOT AUTO-DEPLOY - execution validated the transform, but static "
            f"analysis flagged blocking issues ({blockers})"
        )
        next_steps = "Resolve the failed blocking checks, then re-test before deploying"
        overall_status = "EXECUTION PASSED — STATIC ANALYSIS BLOCKING ISSUES"
    elif parsed_results and static_failed > 0:
        # Execution passed and only advisory checks failed — usable, but the
        # findings should be addressed.
        advisories = ", ".join(advisory_failures) if advisory_failures else "see findings below"
        production_ready = "🟡 **APPROVE WITH FIXES**"
        deployment_decision = f"APPROVE WITH FOLLOW-UPS - execution validated; address advisory findings ({advisories})"
        next_steps = "Deployable; schedule the advisory fixes as follow-ups"
        overall_status = "PASSED WITH EXECUTION VALIDATION (advisory findings)"
    else:
        # p32 2.22 / 1.22: which verdict is reached here is unchanged
        # (`LGR-334667e-1`'s matrix still holds) — what changed is what the
        # verdict says about its own evidence. The former
        # "Code validated through execution and static analysis" claimed Spark
        # validation on the strength of pandas over generated sample data.
        #
        # p42: and on the Glue executor the honest claim is the INVERSE, not an
        # upgrade. Spark semantics ARE verified here; what is not is behaviour on
        # the pipeline's own data, because the frame is synthesised from the
        # declared schema (R9.3, design C8). Both halves are stated, because
        # stating only the first is the overclaim this branch exists to avoid.
        production_ready = "✅ **PRODUCTION READY**"
        if ran_on_spark:
            # p44 R5.2/R5.4/R5.5: the frame-dependent halves are the clause and
            # the parenthetical, resolved once above. The prefix, the verdict
            # itself and `next_steps` are untouched — no new all-caps verdict
            # phrase and no branch of `LGR-334667e-1`'s matrix moves, because the
            # frame changes what the report says about its evidence, never which
            # verdict that evidence reaches.
            deployment_decision = (
                "APPROVE - static analysis passed and the transform ran on **real Spark** in a Glue "
                f"interactive session; {glue_decision_clause}"
            )
            overall_status = f"PASSED - STATIC ANALYSIS + REAL SPARK EXECUTION ({glue_status_note})"
        else:
            deployment_decision = (
                "APPROVE - static analysis passed and the transform ran as a **pandas approximation** "
                "over **generated sample data**; that is NOT a Glue/Spark run, so Spark semantics are unverified"
            )
            overall_status = "PASSED - STATIC ANALYSIS + PANDAS-PROXY EXECUTION (Spark semantics unverified)"
        next_steps = "Deploy to production environment"

    static_summary = ""
    # p43 R4.1: the Reviewer_Transform_Block_Findings this verdict retained as
    # non-blocking, read from the one helper the `ai_review_verdict` check detail
    # and the retention log also read. Deriving the number here a second time is
    # the second-inference-path defect the spec was written to avoid — three
    # surfaces quoting one helper cannot disagree about it.
    #
    # Bound before the `if` in the shape `blocking_failures` above already uses,
    # so the Deployment Decision clause further down can read it without
    # depending on the branch having run. No new parameter and no environment
    # read: this function is a pure builder with 30-odd test call sites that
    # construct reports directly (see the docstring above), and everything needed
    # is already inside `parsed_results`.
    retained: list[dict[str, Any]] = []
    if parsed_results:
        retained = retained_nonblocking_findings(parsed_results)
        static_summary = (
            f"\n### Static Analysis Summary\n"
            f"- **Checks Passed:** {static_passed}/{static_total}\n"
            f"- **Reviewer Recommendation:** {parsed_results.get('recommendation', 'NEEDS_REVIEW')}\n"
        )
        if blocking_failures:
            static_summary += f"- **Blocking Failures:** {', '.join(blocking_failures)}\n"
        if advisory_failures:
            static_summary += f"- **Advisory Failures:** {', '.join(advisory_failures)}\n"
        # Conditional on a non-empty `retained`, and deliberately UNLIKE the check
        # detail, which states the number including zero (R3.6): that detail's
        # neighbouring per-attribution counts are already unconditional, whereas a
        # report line saying zero findings were retained is noise in a document a
        # human reads. The two surfaces differ on purpose; do not align them.
        #
        # The line names **the review gate** specifically rather than the report,
        # because a retained finding can coexist with a blocking static failure —
        # the run then reaches the blocking verdict above and its status line says
        # static analysis blocked, which this line must not appear to contradict.
        if retained:
            severities = ", ".join(sorted({str(i.get("severity", "")) for i in retained}))
            static_summary += (
                f"- **Reported, not blocking:** {len(retained)} reviewer finding(s) "
                f"at non-blocking severity ({severities}); reported by the reviewer "
                f"and did not block the review gate\n"
            )

    # p43 R4.1, second placement: appended ONCE, here, after the branch chain
    # above has finished choosing its wording — never inside an arm.
    #
    # `LGR-5c30733-3` (**template / ratified**) branches this sentence's execution
    # caveat on the resolved executor, and each arm's claim is calibrated to what
    # actually ran; editing either arm to carry this clause would disturb that
    # calibration. Appending after the selection leaves the Glue wording and the
    # pandas wording byte-identical, and reaches neither the verdict banner nor
    # the `Next Steps:` line — the two strings
    # `test_the_verdict_is_identical_under_both_executors` compares across
    # executors. The clause is true in every branch, which is what lets one
    # append stand in for four.
    if parsed_results and retained:
        deployment_decision += (
            f"; {len(retained)} reviewer finding(s) were reported at non-blocking "
            "severity and did not block the review gate"
        )

    enhanced_report = f"""# Comprehensive Test Report (Static Analysis + Code Interpreter)

**Overall Status:** {overall_status}

---

{execution_section}

---

# Original Static Analysis Report

{static_analysis_report}

---

## 🎯 Combined Assessment

### Production Readiness Assessment
{production_ready}
{static_summary}
### Recommendations
1. **Deployment Decision:** {deployment_decision}
2. **Next Steps:** {next_steps}

---

*Report generated with AgenticDataIngestion Code Interpreter Testing*
"""

    return enhanced_report


@step_handler("test_code")
@step_task_token("test_code")
def lambda_handler(event, context):
    """
    Step 6: Code Testing with Strands Agent + Code Interpreter
    Follows DATA_FLOW_SPECIFICATION exactly
    """
    # ── AgentCore Runtime proxy path ────────────────────────────────
    if os.environ.get("USE_AGENTCORE_RUNTIME") == "true":
        from agentcore_proxy import invoke_agentcore_runtime

        return invoke_agentcore_runtime(
            endpoint=os.environ["AGENTCORE_TEST_CODE_ENDPOINT"],
            payload=event,
            step_name="test_code",
        )

    import time as _time

    _metrics_start = _time.time()

    # The step's MONOTONIC start, for the shadow-mode deadline (R4.6) — a
    # separate reading from `_metrics_start` on purpose. `_metrics_start` is a
    # wall clock and feeds a duration metric, where a clock adjustment is a
    # cosmetic error; a deadline compared against a wall clock that steps
    # backwards is a deadline that does not hold.
    _step_monotonic_start = time.monotonic()

    try:
        logger.info(f"Event received: {json.dumps(event)}")

        # Extract required data from event
        run_id = event.get("run_id")
        pipeline_id = event.get("pipeline_id")
        code_version_id = event.get("code_version_id")
        code_s3_uri = event.get("code_s3_uri")
        source_data_uri = event.get("source_data_uri", event.get("json_s3_uri", ""))
        regeneration_attempt = event.get("regeneration_attempt", 0)

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name=StepName.TEST_CODE)

        # ── Target schema: the canonical location, not the mappings record ──
        # p33 task 4.3. This read used to go to the schema-mappings item's
        # `target_schema` attribute, which no writer has ever set: `item_dict`
        # returned {}, `_build_validation_code`'s `expected` list was empty, and
        # `code_interpreter_runner.py:374` short-circuited `schema_compliance`
        # to 100.0 — a passing gate over nothing. The canonical location is the
        # pipeline record's `target_schema_bucket` + `target_schema_s3_key`
        # pair, resolved together by the shared resolver (D5, C1).
        pipeline_metadata = get_pipeline_metadata(pipeline_id)
        if pipeline_metadata is None:
            # `None` means the record is not there at all, or the pipelines
            # table is not there (`ResourceNotFoundException` is a not-found
            # code, so the classifier returns and the helper degrades — see
            # D8 correction 2; the helper's logger.error is where the table
            # case stays distinguishable). Either way this is requirement
            # 2.8's lookup problem, not requirement 2.6's absent schema.
            raise StepFailure(
                "test_code",
                f"Pipeline record {pipeline_id!r} was not found, so the target schema cannot be resolved "
                "and schema compliance cannot be measured. This is a lookup failure, not a pipeline "
                "without a target schema.",
                details={"operation": "get_pipeline_metadata", "pipeline_id": pipeline_id},
            )

        # ── Code_Approval_Setting: resolved from the record just read ───────
        # p34 D1. No second DynamoDB read and no new IAM grant: the record is
        # already in scope from the call above, and two reads of one item in one
        # invocation could disagree. Resolved HERE, next to its read, so R7.4's
        # log fires on every invocation that gets a record at all — whatever the
        # run's outcome downstream. The merge itself waits for the quality gate
        # (see `determine_merged_approval_requirement` at the gate below).
        #
        # Deliberately NOT wrapped in try/except. `LGR-78ce0b7-13` (template /
        # ratified) narrowed a blanket `except Exception` that stringified an
        # already-classified StepFailure / TransientStepFailure into a bare
        # ValueError. A clause here would sit around a read whose exceptions
        # include the StepFailure raised immediately above for a missing record
        # (p33 R2.8), so it would silently revert that requirement as well as
        # re-label a transient. Nothing is lost by letting both through: an
        # infrastructure error fails the step, and a failed step goes Catch →
        # RecordFailure → PipelineFailed and can never reach
        # CreateInfrastructure — strictly more conservative than "require
        # approval", which is how R1.6/R1.7 are satisfied (design D1).
        code_approval_setting, code_approval_setting_source = resolve_code_approval_setting(pipeline_metadata)
        logger.info(
            f"Code_Approval_Setting for pipeline {pipeline_id} evaluated as {code_approval_setting} "
            f"(source: {code_approval_setting_source}) (R7.4)"
        )

        # (None, None) here means, and ONLY means, that the record carries no
        # `target_schema_s3_key` — requirement 2.6's legitimate no-schema state.
        # A key with no bucket, or a key naming an object that is not there,
        # raises (requirements 2.7 / 2.8, D14).
        target_schema, target_schema_hash = resolve_target_schema(pipeline_metadata, "test_code")
        if target_schema is None:
            logger.warning(
                f"Pipeline {pipeline_id} carries no target schema — schema compliance cannot be verified for this run"
            )
        else:
            logger.info(
                f"Resolved target schema for {pipeline_id}: "
                f"{len(target_schema.get('properties', {}))} declared field(s), hash {target_schema_hash}"
            )

        # `target_schema` above is the resolved STATE — `None` distinguishes
        # requirement 2.6's "no target schema at all" from an empty one, and
        # that distinction is what task 4.4 consumes. The Code Interpreter
        # runner below takes a plain dict and cannot express the state, so it
        # sees {} — exactly what it saw before this change whenever no schema
        # was resolved. The review prompt does NOT use this alias; it renders
        # the state in text instead (task 4.5, below).
        target_schema_for_run: dict[str, Any] = target_schema or {}

        # ── Source schema: the run's schema_extract row ─────────────────────
        # p33 D9 / requirement 2.3 — the same authority `create_code` already
        # uses (`create_code/lambda_function.py:406-416`). It used to come from
        # the mappings item's `source_schema` attribute, which no writer sets
        # either, so the Code Interpreter frame was always the builder's
        # `{id, value}` fallback rather than the pipeline's actual input.
        source_schema: dict[str, Any] = {}
        schema_extract_response = pipeline_runs_table.get_item(
            Key={"run_id": run_id, "step_name": StepName.SCHEMA_EXTRACT}
        )
        extracted_schema = item_str(schema_extract_response.get("Item") or {}, "extracted_schema")
        if extracted_schema:
            source_schema = json.loads(extracted_schema)
            logger.info(f"Retrieved source schema from the {StepName.SCHEMA_EXTRACT} state row")
        else:
            # Left as {} rather than raised: task 4.10 turns this into a
            # reported non-verification (`no_source_schema`, requirement 2.11)
            # at the `run_test` level, which is where the frame is built.
            logger.warning(f"No source schema on the {StepName.SCHEMA_EXTRACT} state row for run {run_id}")

        # Generate test execution ID
        test_execution_id = str(uuid.uuid4())
        test_timestamp = datetime.now(UTC).isoformat()
        agent_session_id = str(uuid.uuid4())

        # Retrieve successful code generation from pipeline runs table
        logger.info(f"Looking up successful code generation for run: {run_id}")

        code_gen_response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": StepName.CREATE_CODE})

        if "Item" not in code_gen_response:
            raise ValueError(f"Code generation step not found for run {run_id}")

        code_gen_item = code_gen_response["Item"]
        status = item_str(code_gen_item, "status").lower()
        if status not in ["success", "completed"]:
            raise ValueError(f"Code generation step failed for run {run_id}: {item_str(code_gen_item, 'status')}")

        output_data = item_dict(code_gen_item, "output_data")
        actual_code_version_id = output_data.get("code_version_id")
        actual_code_s3_uri = output_data.get("code_s3_uri")

        if not actual_code_version_id or not actual_code_s3_uri:
            raise ValueError(
                f"Code generation step missing required data: code_version_id={actual_code_version_id}, code_s3_uri={actual_code_s3_uri}"
            )

        logger.info(f"Found successful code generation: {actual_code_version_id}")

        # Use the actual successful code version
        code_version_id = actual_code_version_id
        code_s3_uri = actual_code_s3_uri

        # Retrieve code metadata from generated code table
        code_response = generated_code_table.get_item(Key={"PK": f"CODE#{code_version_id}", "SK": "VERSION"})

        if "Item" not in code_response:
            raise ValueError(f"Code version {code_version_id} not found in {GENERATED_CODE_TABLE}")

        code_metadata = code_response["Item"]

        # Retrieve code from S3
        bucket = code_s3_uri.replace("s3://", "").split("/")[0]
        key = "/".join(code_s3_uri.replace("s3://", "").split("/")[1:])

        code_s3_object = s3_client.get_object(Bucket=bucket, Key=key)
        generated_code = code_s3_object["Body"].read().decode("utf-8")
        logger.info(f"Retrieved code from S3: {len(generated_code)} characters")

        # ----------------------------------------------------------------
        # Phase 1: Static analysis with Strands agent
        # ----------------------------------------------------------------
        # Initialize trace plugin for observability
        trace_plugin = AgentTracePlugin(
            pipeline_id=pipeline_id,
            run_id=run_id,
            step_name="code_testing",
        )

        logger.info("Attempting to initialize Strands Agent with AWS Data Processing MCP tools for testing")

        if load_prompt_template:
            test_analysis_prompt = load_prompt_template("test_analysis.j2")
        else:
            test_analysis_prompt = "You are an expert AWS Glue and PySpark code analyzer."

        strands_agent = None
        strands_mcp_client = None
        if initialize_strands_agent_with_mcp:
            strands_agent, strands_mcp_client = initialize_strands_agent_with_mcp(system_prompt=test_analysis_prompt)

        if strands_agent and strands_mcp_client:
            logger.info("Successfully initialized Strands Agent with MCP tools for testing")
            agent = strands_agent
            agent.add_hook(trace_plugin)
            mcp_client = strands_mcp_client
            using_strands_mcp = True
        else:
            logger.warning("Failed to initialize Strands Agent with MCP for testing, using fallback agent")
            if load_prompt_template:
                fallback_analysis_prompt = load_prompt_template("test_analysis_fallback.j2")
            else:
                fallback_analysis_prompt = "You are an expert AWS Glue and PySpark code analyzer."

            agent = Agent(
                model=resolve_model_id(),
                system_prompt=fallback_analysis_prompt,
                hooks=[trace_plugin],
            )
            mcp_client = None
            using_strands_mcp = False

        # ── The prompt's target-schema section (p33 task 4.5, req 2.4) ──────
        # Rendered from `target_schema` — the resolved STATE — rather than from
        # the `target_schema_for_run` alias. Task 4.3 already fixed the
        # RESOLVED case here by re-sourcing the read, and that path is
        # byte-identical to what it renders below: `Target Schema:\n<json>`.
        # What 4.3 could not fix is the schema-LESS case, because the alias
        # coerces `None` to `{}` and the prompt then presents `{}` as though it
        # were the schema.
        #
        # That is not good enough, and the live counterexample is why: on run
        # `run-2026-08-05-14-29-05-cc73973d` the reviewer raised a CRITICAL
        # target-schema issue about six `_out` renames while this same prompt
        # showed it the target schema as `{}`. Requirement 2.4 exists to remove
        # that incoherence. Re-sourcing the read removes it for every pipeline
        # that HAS a schema; without the branch below it survives verbatim for
        # every pipeline that does not.
        #
        # Two reasons `{}` cannot stand on the schema-less path:
        #   1. The system prompt makes the verdict MANDATORY. `test_analysis.j2`
        #      requires all six categories including "Schema Compliance" (`:23`,
        #      `:38`) with no "not applicable" value, so the reviewer must
        #      return pass or fail whatever it was shown.
        #   2. `{}` is a legitimate JSON Schema meaning "no constraints". A
        #      reviewer cannot tell "unconstrained" from "unavailable" and has
        #      to guess — and either guess is asserted as a verdict.
        # So state which it is, in text, matching the wording the run record
        # itself now carries on this path (`aggregate_check_results`'
        # `no_target_schema` non-verification).
        if target_schema is None:
            target_schema_section = (
                "Target Schema: NOT AVAILABLE — this pipeline has no target schema on record, so "
                "there is no declared column set to compare the transform's output against. Do NOT "
                "report a Schema Compliance issue about a schema you were not given; judge that "
                "category on the type handling and mapping logic visible in the code alone."
            )
        else:
            target_schema_section = f"Target Schema:\n{json.dumps(target_schema, indent=2)}"

        # Code review prompt — focuses on code quality, not test existence.
        # P28 R13.1: the reviewer must return the structured JSON verdict.
        prompt = f"""Review this AI-generated AWS Glue PySpark ETL code for production readiness. Return ONLY the JSON verdict object specified in the system prompt — no markdown fences, no prose. Be concise — 1-2 sentences per issue description. No code blocks, no suggestions, no rewrites.

Code:
```python
{generated_code}
```

{target_schema_section}

Source Data URI: {source_data_uri}

Return ONLY the JSON object."""

        logger.info("Analyzing code with Strands agent...")
        start_time = time.time()

        def _invoke_review_agent(prompt_text: str) -> str:
            if using_strands_mcp and mcp_client:
                logger.info("Running test agent with MCP tools in context")
                with mcp_client:
                    return str(agent(prompt_text))
            logger.info("Running test agent in standard mode")
            return str(agent(prompt_text))

        # P28 R13.1: schema-validated structured verdict with exactly ONE
        # reparse retry (re-ask the model), then fail honestly. No free-text
        # fallback parse exists.
        review_result = _invoke_review_agent(prompt)
        logger.info(f"Code analysis completed: {len(review_result)} characters")
        try:
            review_verdict = parse_review_verdict(review_result)
        except ReviewVerdictError as first_parse_error:
            logger.warning(f"Review verdict parse failed ({first_parse_error}); performing the single reparse retry")
            review_result = _invoke_review_agent(build_reparse_prompt(review_result, str(first_parse_error)))
            try:
                review_verdict = parse_review_verdict(review_result)
            except ReviewVerdictError as retry_parse_error:
                # Honest failure: the review gate cannot make a decision
                # without a schema-valid verdict (P28 R1 + R13.1).
                raise StepFailure(
                    "test_code",
                    "code review model did not return a schema-valid structured verdict after one reparse retry",
                    details={
                        "run_id": run_id,
                        "pipeline_id": pipeline_id,
                        "first_parse_error": str(first_parse_error),
                        "retry_parse_error": str(retry_parse_error),
                        "raw_response_head": review_result[:500],
                    },
                ) from retry_parse_error

        execution_time_ms = int((time.time() - start_time) * 1000)

        # Flush trace data to DynamoDB
        trace_plugin.flush()

        # Adapt the structured verdict to the parsed_results shape downstream
        # consumers read (test_categories for the dashboard/report, plus the
        # authoritative verdict + issues). One parser, no free-text metrics.
        parsed_results = verdict_to_parsed_results(review_verdict)
        logger.info(
            f"Structured review verdict: {review_verdict['verdict']} "
            f"({len(review_verdict['issues'])} issue(s), {len(review_verdict['categories'])} category verdict(s))"
        )

        # p43 R5.1/R5.2 — say out loud which way the severity floor went. The two
        # branches are mutually exclusive by construction, which is Property 12 in
        # `test_retained_findings_helpers_prop.py`: a blocking finding forces an
        # effective `fail`, and the retained list is empty unless the effective
        # verdict is `pass`.
        #
        # The `elif` keys on the BLOCKING LIST, never on
        # `effective_verdict(...) == VERDICT_FAIL`. A rule-2 failure — a raw
        # `fail` carrying one unattributed issue — is an effective fail with an
        # EMPTY blocking list, so keying on the verdict would announce a blocking
        # reviewer finding on every one of those runs and misname the rule that
        # fired.
        #
        # DEPARTURE from this module's level semantics, deliberate and recorded.
        # Elsewhere here WARNING means "outside the vocabulary" — a misspelled
        # executor (`:466`), a non-integer shadow deadline (`:678`), a non-boolean
        # approval setting (`:1629`) — and a correctly configured run puts no line
        # in the log at all. A retained finding is NOT a misconfiguration: it is a
        # reviewer finding that was reported and deliberately not acted on, and
        # this WARNING says so. `LGR-ad78946-5` (**template / ratified**) requires
        # only that the two levels be DISTINCT, "because a correctly spelled but
        # unsatisfiable request is a different fact from a typo, and logging both
        # at one level makes them indistinguishable". The direction is chosen on
        # which fact would otherwise leave no trace: a blocking finding already
        # announces itself four other ways (the step fails,
        # `determine_code_approval_requirement` logs it at `:1700`, the report
        # reaches its review-required banner, and an approval is requested),
        # whereas a retained finding on a green run appears nowhere except inside
        # the embedded reviewer JSON.
        retained = retained_nonblocking_findings(review_verdict)
        if retained:
            logger.warning(
                "Review gate retained %d non-blocking finding(s) (severities: %s) — "
                "reviewer transform-block findings below the blocking severity floor; "
                "the gate did not fail on them",
                len(retained),
                ", ".join(sorted(str(i.get("severity", "")) for i in retained)),
            )
        elif blocking := blocking_transform_block_findings(review_verdict):
            logger.info(
                "Review gate failed on %d blocking reviewer transform-block finding(s) (severities: %s)",
                len(blocking),
                ", ".join(sorted(str(i.get("severity", "")) for i in blocking)),
            )

        # ----------------------------------------------------------------
        # Phase 2: transform verification
        # ----------------------------------------------------------------
        # Named for the job rather than for one executor: p42 R2 makes Code
        # Interpreter one of two, selected by `VERIFICATION_EXECUTOR`.
        execution_results = None
        # p44 R5.1 — which frame the verification was measured against, read off
        # the runner AFTER the run and passed to the report. Bound here, beside
        # `execution_results`, so the `except` path below and the report call
        # further down both see a defined value: a run that never reached the
        # runner has no frame to report, and `None` is that.
        frame_provenance: dict | None = None
        transform_block = extract_transform_block(generated_code)

        # p42 R2 — resolve the Verification_Executor HERE, immediately before the
        # runner is constructed, and not at import: the value is read once, on
        # the path that uses it, so there is no module-level cache to go stale
        # between a configuration change and a warm container.
        session_role_arn = os.environ.get(VERIFICATION_SESSION_ROLE_ARN_ENV) or ""
        verification_executor = resolve_effective_executor(os.environ.get(VERIFICATION_EXECUTOR_ENV), session_role_arn)

        logger.info("Running transform verification (executor=%s)", verification_executor)
        try:
            if verification_executor == VERIFICATION_EXECUTOR_GLUE:
                # Imported function-locally, on the branch that uses it — the
                # same shape as the `agentcore_proxy` import on the
                # `USE_AGENTCORE_RUNTIME` branch above, and for the same two
                # reasons. It keeps this module's import graph unchanged for
                # every caller that does not select `glue` (12 test files patch
                # `CodeInterpreterTestRunner` on this module and none of them
                # stubs `glue_session_runner`), and it means the default path
                # never imports a Glue client it will not use.
                from glue_session_runner import GlueSessionTestRunner

                # p42 task 9.3 / R9.6 — `source_data_uri` reaches the runner
                # through this CONSTRUCTOR, never as a fourth argument to
                # `run_test`. `run_test`'s arity IS the contract R1 pins: 32
                # references and five covering test files depend on the
                # three-argument form, and `GlueSessionTestRunner.run_test`'s own
                # docstring says so at the point a reader would be tempted
                # ("Do not add a fourth parameter").
                #
                # `or None` normalises the one value that has two spellings here.
                # The payload read above defaults to `""` when neither
                # `source_data_uri` nor `json_s3_uri` is present, while the runner
                # documents `None` as "absent" — and both already resolve to the
                # same `SAMPLE_UNAVAILABLE_NOT_S3_SOURCE` fallback in
                # `GlueSessionTestRunner.resolve_frame_source`
                # (`glue_session_runner.py:918-925`), since `is_s3_source_uri`
                # answers False for `""` and for `None` alike — its own docstring
                # tabulates both as "absent".
                # So this changes no behaviour; it keeps the *recorded* URI on the
                # fallback `FrameSource` from claiming `""` is the offending URI
                # when in fact there was none. The review prompt's own use of the
                # value (`:1964`) is a SEPARATE consumer and is deliberately left
                # reading the raw `""` — p33 task 4.5's reasoning about it still
                # applies.

                # p44 R1.1/R1.3 — the Frame_Mode is read HERE, on the same
                # footing and for the same reason as `verification_executor`
                # five lines above the `try`: read once, on the path that uses
                # it, so there is no module-level cache to go stale between a
                # configuration change and a warm container.
                #
                # `or` rather than `os.environ.get(..., DEFAULT)`, so an EMPTY
                # value is "unset" rather than an unrecognised mode. The
                # two-argument form would pass `""` through to
                # `resolve_frame_source`, which would fall through to synthetic
                # anyway — but by way of the unrecognised arm, putting a WARNING
                # naming `''` in every run of a deployment that has not set the
                # variable. That is the line that trains a reader to ignore the
                # one case the WARNING exists for.
                #
                # The value is passed RAW: no `.strip()`, no `.lower()`, no
                # membership check (`LGR-ad78946-1`, **template / ratified**).
                # `resolve_frame_source` is the one classifier for this
                # vocabulary; a second inference path here is the defect. A
                # near-miss spelling therefore does reach it and is named at
                # WARNING, which is the signal that record protects.
                runner = GlueSessionTestRunner(
                    region=AWS_REGION,
                    role_arn=session_role_arn,
                    source_data_uri=source_data_uri or None,
                    frame_mode=os.environ.get(VERIFICATION_FRAME_MODE_ENV) or FRAME_MODE_SYNTHETIC_DEFAULT,
                )
            else:
                # `pandas` AND `shadow` both land here, and that is not a stub:
                # R4.1 requires shadow mode to derive `test_status` and the check
                # list from the Pandas_Proxy result ALONE, so the pandas proxy is
                # the authoritative executor under `shadow` as well. What p42
                # task 10.3 adds is the second, non-authoritative Glue run and
                # the comparison row — NOT a different verdict source. So the
                # resolution is already three-valued and this dispatch is
                # already correct for two of the three; `shadow` currently
                # differs from `pandas` only in that nothing extra is recorded.
                runner = CodeInterpreterTestRunner(region=AWS_REGION)
            execution_results = runner.run_test(transform_block, source_schema, target_schema_for_run)

            # p44 R5.1 — the frame decision, read from the object that made it.
            # `getattr` because the same local `runner` is a
            # `CodeInterpreterTestRunner` on the `pandas` and `shadow` arms, which
            # has no such attribute and must not grow one: it has no source read of
            # any kind (`code_interpreter_runner.py:223-229`), so the default is
            # load-bearing rather than defensive.
            #
            # AFTER `run_test` returns, not before: `last_frame_source` is a per-run
            # observation that `run_guarded_session` records the moment the decision
            # is made (`glue_session_runner.py:1054`), and it is `None` until a run
            # has happened. `frame_mode` on the same object is the input
            # configuration and is the wrong one to read here.
            #
            # A plain dict, not the dataclass: it also rides into
            # `create_output_data`'s `summary` and thence to DynamoDB through
            # `convert_floats_to_decimal`, which walks dicts and would not see
            # inside a frozen dataclass. It carries no cell value by construction —
            # `FrameSource` has five fields and none of them is a row (R6.7).
            last_frame_source = getattr(runner, "last_frame_source", None)
            frame_provenance = last_frame_source.provenance() if last_frame_source is not None else None

            if execution_results.get("execution_successful", False):
                logger.info(
                    "Verification execution successful (executor=%s): %s records",
                    verification_executor,
                    execution_results["output_records"],
                )
            else:
                logger.warning(
                    "Verification execution failed (executor=%s): %s",
                    verification_executor,
                    execution_results.get("error_details", "Unknown error"),
                )
        except Exception as e:
            logger.error(f"Verification execution error (executor={verification_executor}): {e}")
            # Same structured shape as the runner's failure results (P28 R10.1):
            # nothing executed, so executed=False with no exit code.
            execution_results = {
                "execution_successful": False,
                "executed": False,
                "exit_code": None,
                "stderr": "",
                "error_details": str(e),
            }

        # P28 R4.3: honest per-check aggregation. ANY failed check → failed.
        # No execution-success status override, no metric boosting, no
        # "don't penalize" exemption for a failed/unavailable execution.
        # p33 task 4.4: the RESOLVED target-schema state is passed in, not read
        # inside — `target_schema` (not `target_schema_for_run`) so `None` still
        # means requirement 2.6's "this pipeline has no target schema", which is
        # the distinction the {} coercion at the resolution site erases.
        test_status, checks = aggregate_check_results(parsed_results, execution_results, target_schema)
        # p32 2.20/2.21: a not-verified check (the pandas proxy refused the
        # transform) verified nothing, so it is neither a failed check nor an
        # executed one. It stays in `checks` — and therefore in the state row
        # and the approval payload — but it is counted separately so the
        # reported counts say what actually ran.
        not_verified_check_names = [check["name"] for check in checks if is_not_verified_check(check)]
        verified_checks = [check for check in checks if not is_not_verified_check(check)]
        failed_check_names = [check["name"] for check in verified_checks if not check["passed"]]

        # P28 R10.6: the ONLY reported metrics are counts of actually
        # executed checks — nothing numeric is taken from the LLM review text.
        checks_run = len(verified_checks)
        checks_passed = checks_run - len(failed_check_names)
        checks_failed = len(failed_check_names)
        logger.info(
            f"Aggregated test status: {test_status} "
            f"({checks_passed}/{checks_run} checks passed"
            + (f"; failed: {', '.join(failed_check_names)}" if failed_check_names else "")
            + (f"; not verified: {', '.join(not_verified_check_names)}" if not_verified_check_names else "")
            + ")"
        )

        # ── Shadow mode: the second, non-authoritative Glue run (p42 R4) ────
        # HERE, and not up in Phase 2 beside the executor dispatch, and the
        # placement is the argument for R4.1/R4.3 rather than a detail of
        # reading order: `test_status` and `checks` are already bound by the
        # time this runs, from the pandas result alone. So the shadow half runs
        # *after* the verdict exists and cannot be an input to it — no ordering
        # discipline is required of a later reader, because there is no path
        # from here back to a value the gate reads.
        #
        # `record_shadow_verification` never raises (R4.4) — see its docstring
        # for the outer `except Exception` that guarantees it, and for why the
        # inner one is a classifier rather than a second guard. It logs its own
        # outcome, so its return value is deliberately not read here.
        #
        # The Glue half runs inline inside it, before this handler returns,
        # because `LGR-b46a5bb-1` (standing invariant) forbids a thread here.
        if verification_executor == VERIFICATION_EXECUTOR_SHADOW:
            record_shadow_verification(
                run_id=run_id,
                pipeline_id=pipeline_id,
                transform_block=transform_block,
                source_schema=source_schema,
                target_schema=target_schema_for_run,
                pandas_result=execution_results,
                source_data_uri=source_data_uri or None,
                session_role_arn=session_role_arn,
                step_started_at=_step_monotonic_start,
            )

        # Audit: record code testing decision (honest aggregated status)
        if write_agent_audit_entry:
            write_agent_audit_entry(
                agent_type="code_testing",
                action="test_code",
                resource_id=code_version_id,
                details={
                    "test_status": test_status,
                    "checks_run": checks_run,
                    "checks_passed": checks_passed,
                    "failed_checks": failed_check_names,
                    # p32 2.20: a non-verification stays visible in the audit
                    # trail rather than vanishing from the counts.
                    "not_verified_checks": not_verified_check_names,
                },
                run_id=run_id,
                pipeline_id=pipeline_id,
            )

        # Generate comprehensive test report combining static analysis and execution results
        if execution_results:
            # p42 — `verification_executor` is passed EXPLICITLY. It is the value
            # already resolved in Phase 2 and already used to choose the runner,
            # so the report describes the executor that actually produced
            # `execution_results`. The report builder deliberately does not read
            # the environment itself: it would miss
            # `resolve_effective_executor`'s fallbacks (`LGR-ad78946-5`) and
            # could name Spark on a run that fell back to pandas.
            #
            # p44 R5.1 — and `frame_provenance` beside it, for the same reason one
            # level down: the executor says a Spark run happened, the provenance
            # says what it was measured against, and both are facts about the run
            # that produced `execution_results` rather than about the current
            # configuration. `None` on the pandas and shadow arms, where there is
            # no frame source to report.
            review_result = generate_comprehensive_test_report(
                review_result,
                execution_results,
                parsed_results,
                executor=verification_executor,
                frame_provenance=frame_provenance,
            )
            logger.info("Enhanced test report generated (executor=%s)", verification_executor)

        # Analyze test results for regeneration needs — structured verdict in,
        # bounded attempts out (P28 R13.2/R13.3). No free text is inspected.
        regeneration_analysis = analyze_test_results_for_regeneration(
            test_status, checks_run, checks_passed, review_verdict, regeneration_attempt
        )
        regeneration_max_attempts = regeneration_analysis["max_attempts"]

        # Store regeneration feedback if needed — carries the reviewer's
        # issues so the regeneration prompt differs from the original (R13.3).
        if regeneration_analysis.get("regeneration_needed", False):
            store_regeneration_feedback(run_id, regeneration_analysis["feedback_context"])

        # The Code_Quality_Gate: unchanged (structured verdict gate — P28 R13.1).
        # Its answer is now published on its own field rather than being the
        # merged decision, so p34 R3.1's subject — what the quality gate alone
        # concluded, with no configuration input — stays observable.
        quality_gate_requires_approval = determine_code_approval_requirement(
            test_status, checks_run, checks_passed, review_verdict, regeneration_analysis
        )

        # ── The Approval_Policy (p34 R2.4, design D2) ───────────────────────
        # `approval_required` is the MERGED decision: the disjunction of the
        # pipeline's own Code_Approval_Setting, the Code_Quality_Gate above, and
        # the regeneration-attempt count. It keeps its name because it is the
        # JSONPath the ASL already reads
        # ($.test_code_result.approval_required) — but its MEANING is now wider
        # than the quality gate's return, which is why the unmerged answer sits
        # beside it on `quality_gate_requires_approval` (design D3).
        #
        # This is the fix: before p34 the field the state machine reads could not
        # see the configuration, so a pipeline carrying
        # approval_config.code_generation_enabled = true was never reviewed on a
        # clean run. `test_code` owns this decision (`LGR-f077f1b-1`, template /
        # ratified) — `create_code` does not compute it.
        approval_required, approval_decision_inputs = determine_merged_approval_requirement(
            config_requires_approval=code_approval_setting,
            quality_requires_approval=quality_gate_requires_approval,
            regeneration_attempt=regeneration_attempt,
        )
        logger.info(
            f"Approval_Policy decision for pipeline {pipeline_id}: approval_required={approval_required} "
            f"(inputs: {approval_decision_inputs})"
        )

        # Get shared bucket and pipeline-specific prefix
        test_bucket, test_prefix = get_test_bucket_and_prefix(pipeline_id)

        # S3 destinations (computed up front so the structured test_result can
        # reference the full report location)
        test_output_key = f"{test_prefix}{test_execution_id}/test_results.json"
        test_output_s3_uri = f"s3://{test_bucket}/{test_output_key}"
        report_key = f"{test_prefix}{test_execution_id}/test_report.md"
        report_s3_uri = f"s3://{test_bucket}/{report_key}"

        # P28 R4.3: structured, honest test result. This is the stable shape
        # the Step Functions TestResultChoice (task 5.2) branches on. With the
        # TestCode task using resultPath "$.test_code_result" +
        # payloadResponseOnly, the Choice condition JSONPath is:
        #   $.test_code_result.test_result.status  ("passed" | "failed")
        #
        # p40 R6: the absence of execution evidence, stated AT the point of the
        # success claim rather than only in the report. One derivation, five
        # statement sites — this is the first, and four of the others read it
        # from here: `test_output` below, the approval payload, the state row
        # and the handler return.
        #
        # Adding keys here is INERT for governance, and that is read rather than
        # assumed (`B3`, `LGR-69ca78b-1` invariant): the Choice branches on
        # `$.test_code_result.test_result.status` only — the comment directly
        # above — so no Choice reads either key and no new governance shape
        # exists that could route permissively by default.
        #
        # `validation_status` is deliberately NOT widened (§4.7 do-NOT / N5): it
        # still says "validated" on a non-verified run, because two readers pass
        # it through (`dashboard_api/handlers/pipelines_artifacts.py:102` and
        # `dashboard-react/src/hooks/use-code.ts:49`, which falls back to it for
        # `test_status`). The boolean and the note below are what carry the
        # truth, along with the report heading and the dashboard's row label.
        #
        # A non-verification still does not fail the gate (§4.7 Q2, decided
        # "no"): p32 2.21 stands unreversed, and nothing here feeds
        # `approval_required` or `regeneration_needed`.
        execution_verified = execution_was_verified(checks)
        verification_note = execution_verification_note(checks)
        test_result: dict[str, Any] = {
            "status": test_status,
            "checks": checks,
            "report_s3_uri": report_s3_uri,
            "execution_verified": execution_verified,
        }
        if not execution_verified:
            test_result["verification_note"] = verification_note

        # P28 R10.6: all counts below are executed-check counts (checks_run /
        # checks_passed / checks_failed). The legacy key names tests_run /
        # tests_passed / tests_failed are kept where the dashboard reads them,
        # but their values are honest counts. coverage_percent is REMOVED
        # everywhere: it was LLM-invented with no real measurement behind it
        # (p29 note: the dashboard coverage box simply no longer renders —
        # TestResultsViewer types it optional and renders only when > 0).
        test_output = {
            "test_execution_id": test_execution_id,
            "code_version_id": code_version_id,
            "test_timestamp": test_timestamp,
            "test_status": test_status,
            "test_result": test_result,
            "review_result": review_result,
            "structured_test_report": review_result,
            "parsed_results": parsed_results,
            "test_summary": {
                "checks_run": checks_run,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "total_tests": checks_run,
                "tests_passed": checks_passed,
                "tests_failed": checks_failed,
                "recommendation": parsed_results.get("recommendation", "NEEDS_REVIEW"),
                "test_categories": parsed_results.get("test_categories", {}),
            },
            "execution_time_ms": execution_time_ms,
        }

        s3_client.put_object(
            Bucket=test_bucket,
            Key=test_output_key,
            Body=json.dumps(test_output, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(f"Test output stored in S3: {test_output_s3_uri}")

        s3_client.put_object(
            Bucket=test_bucket, Key=report_key, Body=review_result.encode("utf-8"), ContentType="text/markdown"
        )
        logger.info(f"Test report (markdown) stored in S3: {report_s3_uri}")

        # Write to agentic-data-ingestion-test-results table
        test_results_table.put_item(
            Item={
                "PK": f"TEST#{code_version_id}",
                "SK": f"RESULT#{test_timestamp}",
                "test_execution_id": test_execution_id,
                "code_version_id": code_version_id,
                "test_timestamp": test_timestamp,
                "test_status": test_status,
                # P28 R10.6: executed-check counts. Legacy tests_* keys kept
                # for the dashboard (TestResultsViewer/use-code read them via
                # the pass-through API) but with honest values; coverage_percent
                # removed (no honest source).
                "test_results": {
                    "checks_run": checks_run,
                    "checks_passed": checks_passed,
                    "checks_failed": checks_failed,
                    "tests_run": checks_run,
                    "tests_passed": checks_passed,
                    "tests_failed": checks_failed,
                    "execution_time_ms": execution_time_ms,
                },
                "test_output_s3_uri": test_output_s3_uri,
                "test_report_s3_uri": report_s3_uri,
                "error_details": (
                    None if test_status == "passed" else f"Failed checks: {', '.join(failed_check_names)}"
                ),
                "agent_session_id": agent_session_id,
                "validation_status": "validated" if test_status == "passed" else "failed",
                # p40 R6: beside `validation_status` on purpose. That field's
                # value set is unchanged (§4.7 do-NOT / N5), so it can still
                # read "validated" on a run whose transform never executed —
                # this is the field on the same row that says otherwise. Its
                # read path is not widened here: the artifacts handler forwards
                # an explicit key list
                # (`dashboard_api/handlers/pipelines_artifacts.py:92-105`, read),
                # so this key is inert for that endpoint until that list gains
                # it, which is a follow-up and not this task.
                "execution_verified": execution_verified,
                "retry_count": 0,
                "fixed_code_version_id": None,
            }
        )
        logger.info(f"Test results stored in {TEST_RESULTS_TABLE}")

        # Write to pipeline runs table with regeneration analysis
        test_approval_payload = {
            "code_version_id": code_version_id,
            "test_execution_id": test_execution_id,
            "test_status": test_status,
            "test_result": test_result,
            "test_results": {
                "checks_run": checks_run,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "tests_run": checks_run,
                "tests_passed": checks_passed,
                "tests_failed": checks_failed,
            },
            "code_quality_metrics": {"execution_time_ms": execution_time_ms, "review_summary": review_result[:500]},
            "generated_code_s3_uri": code_s3_uri,
            "regeneration_analysis": regeneration_analysis,
        }
        test_output_data = create_output_data(
            artifact_type="test_results",
            primary_id=test_execution_id,
            status="success" if test_status == "passed" else "failed",
            summary={
                "tests_passed": checks_passed,
                "tests_failed": checks_failed,
                "tests_total": checks_run,
                "test_report_markdown": review_result[:5000],
                # p40 R6 inside `summary`, never as a sixth key (`B8`,
                # `LGR-60c4985-4`): `create_output_data` fixes `output_data` to
                # exactly five keys and validates them, so a sixth would raise.
                # `summary` is free-form and is the sanctioned carrier. The
                # `>=` shape of `tests/contracts/api_responses/`
                # `test_output_data_contract.py:145` means added summary keys
                # are inert for it.
                "execution_verified": execution_verified,
                **({"verification_note": verification_note} if not execution_verified else {}),
                # p44 R5.6 (task 14.1): WHICH frame the verification ran
                # against, beside the statement of whether it ran at all. Inside
                # `summary` for the reason directly above, and it is the same
                # `FrameSource.provenance()` dict the report call already
                # received — reused from that local rather than read off the
                # runner a second time (R5.1's derive-once posture;
                # `LGR-9e76249-2`, template / ratified, rejected mirroring a
                # derivation in a second consumer). A plain dict, so the
                # DynamoDB serialiser walks into it; it carries no cell value by
                # construction, because `FrameSource` has five fields and none
                # of them is a row (R6.7).
                #
                # Conditional in `verification_note`'s shape above: `None` on a
                # `pandas` or `shadow` run, whose runner has no
                # `last_frame_source`, and on a run that never reached the runner
                # at all — neither has a frame to report, and neither should
                # carry an empty key.
                **({"frame_provenance": frame_provenance} if frame_provenance else {}),
            },
            s3_uris={
                "full_report_json": test_output_s3_uri,
            },
        )
        test_output_data["approval"] = convert_floats_to_decimal(test_approval_payload)

        # P28 R4.3: the state item status reflects the honest aggregation —
        # the previous hardcoded `"status": "success"` (written even when
        # tests failed) is removed.
        #
        # This is a SET update, NOT a full-row put_item: PutItem here deleted
        # the regeneration feedback ``store_regeneration_feedback`` had written
        # a few lines above, in the same invocation, on this same item. See
        # ``build_state_row_update`` for the live evidence and for the two
        # alternatives that were rejected. The attribute set below is exactly
        # what this step owns and is unchanged by that conversion.
        state_row_attributes = {
            "status": "success" if test_status == "passed" else "failed",
            "step_sequence": STEP_SEQUENCE[StepName.TEST_CODE],
            "timestamp": test_timestamp,
            "pipeline_id": pipeline_id,
            "original_pipeline_id": pipeline_id,
            "regeneration_attempt": regeneration_attempt,
            # P28 R13.3: the attempt bound is recorded in state alongside
            # the attempt count (also inside regeneration_analysis).
            "regeneration_max_attempts": regeneration_max_attempts,
            # p34 R3.2: the merged Approval_Policy decision — the field the
            # Code_Approval_Gate reads.
            "approval_required": approval_required,
            # p34 R3.1: the Code_Quality_Gate's own answer, unmerged, with no
            # configuration input.
            "quality_gate_requires_approval": quality_gate_requires_approval,
            # p34 R3.5: which of the three inputs required approval. State row
            # ONLY — design D3 keeps it off the handler response, because this
            # spec does not add a dashboard field with no reader. The run-detail
            # step card is the named follow-up
            # (`dashboard_api/handlers/pipelines_runs.py` builds `step_info`
            # from this row).
            "approval_decision_inputs": approval_decision_inputs,
            "regeneration_analysis": json.dumps(regeneration_analysis),
            "output_data": test_output_data,
            "test_execution_id": test_execution_id,
            "test_status": test_status,
            # p40 R6: the run record's statement of the non-verification arrives
            # inside `test_result` (`execution_verified`, plus
            # `verification_note` when it is false) and inside
            # `output_data.summary` above. NO new top-level state-row attribute
            # is added — the label is already on the row twice, and a third copy
            # would be a third thing to keep in agreement.
            "test_result": convert_floats_to_decimal(test_result),
            # Value set unchanged (§4.7 do-NOT / N5) — see the note at
            # `test_result`'s construction for why, and for what carries the
            # truth instead.
            "validation_status": "validated" if test_status == "passed" else "failed",
            "test_output_s3_uri": test_output_s3_uri,
            "test_report_s3_uri": report_s3_uri,
            "agent_session_id": agent_session_id,
        }
        pipeline_runs_table.update_item(
            Key={
                "run_id": run_id,
                # P28 R8.2 (task 14.2): canonical step name — the legacy
                # "CodeTesting" state-row variant is retired.
                "step_name": StepName.TEST_CODE,
            },
            **build_state_row_update(state_row_attributes),
        )
        logger.info(f"Pipeline state with regeneration analysis stored in {PIPELINE_RUNS_TABLE}")

        # p32 2.17 — REVERSES P28 R4.4, deliberately. R4.4 removed a
        # `next_step: "code_regeneration"` reference because no state machine
        # state backed it: the counter was never set, so the loop was wired
        # but dead (1.14-1.16). P32 D2 builds the destination first — the
        # TestResultChoice → ComputeNextRegenerationAttempt →
        # IncrementRegenerationAttempt → CreateCode edge in
        # `orchestration-stack.ts` — so the name now points at a real state.
        #
        # The state machine does NOT branch on this field (it branches on
        # `regeneration_needed` plus the two attempt numbers, re-evaluating the
        # bound on the edge itself so a regression here cannot loop forever).
        # `next_step` is the step's own statement of where the run goes, read
        # by the state row and the dashboard, and it was the one place that
        # still said "approval_check" while the machine regenerated.
        #
        # In bounds → regeneration. Out of bounds, `regeneration_needed` is
        # False and `max_attempts_reached` is True, so this falls through to
        # the human decision exactly as before.
        #
        # p34 R3.2: the `approval_required` this reads is the MERGED
        # Approval_Policy decision, not the quality gate's return, so a clean run
        # on a pipeline whose owner asked for review now names `approval_check`
        # rather than `create_glue_pipeline`. That is the behavioural change
        # R7.2 describes. `quality_gate_requires_approval` is deliberately NOT
        # consulted here — one field decides the destination.
        if regeneration_analysis.get("regeneration_needed", False):
            next_step = "code_regeneration"
        elif approval_required:
            next_step = "approval_check"
        else:
            next_step = "create_glue_pipeline"

        # Publish stage metrics
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CodeTesting",
                    pipeline_id=pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: stage metrics are best-effort telemetry; a publish failure
                # must not fail a successful test run (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CodeTesting (completed) stage metrics for "
                    f"pipeline_id={pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                )

        return {
            "statusCode": 200,
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "code_version_id": code_version_id,
            "test_execution_id": test_execution_id,
            "test_status": test_status,
            # P28 R4.3: structured honest result. Stable contract for the SFN
            # TestResultChoice (task 5.2): with TestCode's
            # resultPath="$.test_code_result" + payloadResponseOnly=true the
            # Choice branches on $.test_code_result.test_result.status.
            "test_result": test_result,
            "validation_status": "validated" if test_status == "passed" else "failed",
            # P28 R10.6: executed-check counts only (legacy tests_* keys kept
            # with honest values; coverage_percent removed — no honest source).
            "test_results": {
                "checks_run": checks_run,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "tests_run": checks_run,
                "tests_passed": checks_passed,
                "tests_failed": checks_failed,
                "execution_time_ms": execution_time_ms,
            },
            "test_summary": {
                "checks_run": checks_run,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "total_tests": checks_run,
                "tests_passed": checks_passed,
                "tests_failed": checks_failed,
                "recommendation": parsed_results.get("recommendation", "NEEDS_REVIEW"),
                "test_categories": parsed_results.get("test_categories", {}),
            },
            "test_output_s3_uri": test_output_s3_uri,
            "test_report_s3_uri": report_s3_uri,
            "test_data_s3_uri": test_output_s3_uri,
            "regeneration_attempt": regeneration_attempt,
            "regeneration_max_attempts": regeneration_max_attempts,
            "regeneration_needed": regeneration_analysis.get("regeneration_needed", False),
            "regeneration_feedback": regeneration_analysis.get("feedback_context", {}),
            # p34 R3.2: the merged Approval_Policy decision. This is the field
            # the Code_Approval_Gate reads
            # ($.test_code_result.approval_required) and the only approval field
            # it reads (R3.3).
            "approval_required": approval_required,
            # p34 R3.1: the Code_Quality_Gate's own answer, unmerged. Published
            # so the verdict-only subject stays assertable now that the merged
            # field carries the configuration too. `approval_decision_inputs` is
            # NOT published here — state row only, per design D3.
            "quality_gate_requires_approval": quality_gate_requires_approval,
            "next_step": next_step,
            # p40 R6: amended IN PLACE (`LGR-60c4985-6`, `LGR-b2f79ef-6`) rather
            # than replaced — the original sentence is byte-identical and the
            # note is appended after it. Without this, the one-line summary a
            # reader sees first ("7/8 checks passed") reads as a plain pass on a
            # run whose transform was never executed, which is exactly the
            # `14:24:49Z` shape 2.14 is about.
            "message": (
                f"Test analysis completed. {checks_passed}/{checks_run} checks passed. "
                f"Report stored in S3.{'' if execution_verified else ' ' + verification_note}"
            ),
        }

    except Exception as e:
        logger.error(f"Error: {e!s}", exc_info=True)

        # For StepFailure, str(e) is its JSON serialization — use the
        # human-readable message for dashboard-facing records instead.
        error_message = e.message if isinstance(e, StepFailure) else str(e)

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CodeTesting",
                    pipeline_id=event.get("pipeline_id", "unknown"),
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: failure-path stage metrics are best-effort telemetry; the
                # original error is still recorded below (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CodeTesting (failed) stage metrics for "
                    f"pipeline_id={event.get('pipeline_id', 'unknown')} "
                    f"run_id={event.get('run_id', 'unknown')}: {type(exc).__name__}: {exc}"
                )

        try:
            run_id = event.get("run_id", "unknown")

            # A SET update, NOT a full-row ``put_item`` — the same rule the
            # success path follows, arriving by the other door. This row is
            # shared: the success write and ``store_regeneration_feedback`` both
            # land on ``(run_id, test_code)``, and regeneration makes a SECOND
            # invocation of this step routine (``DEFAULT_REGENERATION_MAX_ATTEMPTS``
            # is 2), so "first invocation succeeded, a later one failed" is
            # reachable rather than hypothetical. A PutItem here reduced the row
            # to the nine attributes it names and deleted everything else —
            # ``output_data``, ``test_result``, the report URIs, the approval
            # fields, ``regeneration_feedback``. Read
            # ``build_state_row_update``'s docstring, which carries the live
            # evidence (run ``run-2026-08-05-13-20-43-433f80b2``) and the rule:
            # **no writer clobbers attributes it does not own.**
            #
            # ``UpdateItem`` upserts, so this still CREATES the row when the
            # error path is the FIRST writer — the common case, e.g. a failure
            # before the success write ever runs. No ``attribute_exists``
            # condition is added, on purpose: ``LGR-60c4985-4`` uses exactly that
            # condition to stop ``cache_step_result`` leaving a phantom row, and
            # this write has the opposite obligation — P28 R1.1/R8.5 require the
            # failure record, so it must not be conditional.
            error_state_attributes = {
                "status": "failed",
                "step_sequence": STEP_SEQUENCE[StepName.TEST_CODE],
                "timestamp": datetime.now(UTC).isoformat(),
                "pipeline_id": event.get("pipeline_id", "unknown"),
                "original_pipeline_id": event.get("pipeline_id", "unknown"),
                "error_message": error_message,
                "error_type": "ProcessingError",
                # The three below are the step's own test verdict. The error path
                # computed none, and leaving an earlier invocation's would put
                # ``test_status: "passed"`` on a row whose ``status`` is
                # ``"failed"`` — and ``pipelines_runs.py:641`` reads top-level
                # ``test_status`` FIRST, so that is exactly what the run-detail
                # step card would show. So they are WITHDRAWN, not restated:
                #   * ``"unknown"`` is this repo's existing no-verdict value for
                #     the field (``approval_handler/lambda_function.py:287,313``).
                #   * ``None`` for ``test_result`` is the shape every reader
                #     already guards for (``approval_handler:275,296,302``;
                #     ``record_failure:281`` via ``_as_dict``).
                #   * ``validation_status`` is a pure function of ``test_status``
                #     on the success path ("validated" if passed else "failed"),
                #     so ``"failed"`` is that same expression evaluated at
                #     ``"unknown"`` — the producer's own rule, not a new claim.
                # Nothing is lost by withdrawing them: the per-attempt verdict is
                # durably recorded on its own ``TEST#{code_version_id}`` row in
                # the test-results table (``:2967``), which is what
                # ``pipelines_artifacts.py`` reads.
                "test_status": "unknown",
                "test_result": None,
                "validation_status": "failed",
            }
            # Deliberately NOT set: ``output_data``, the ``regeneration_*`` and
            # ``approval_*`` fields, the report URIs and ``agent_session_id``.
            # They belong to the invocation that produced them and are the record
            # this conversion exists to preserve; none of them asserts a test
            # verdict on its own. ``output_data.approval.test_status`` is the one
            # that could, and the top-level ``test_status`` above shadows it at
            # its only reader (``pipelines_runs.py:641``). Writing ``output_data``
            # here would also mean calling ``create_output_data`` with values the
            # error path does not have — ``LGR-60c4985-4`` fixes it to five
            # validated keys.
            pipeline_runs_table.update_item(
                Key={
                    "run_id": run_id,
                    "step_name": StepName.TEST_CODE,
                },
                **build_state_row_update(error_state_attributes),
            )
        except Exception as db_error:
            logger.error(f"Failed to write error to DynamoDB: {db_error!s}")

        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire
        # (a returned 500 payload would be treated as task success). An
        # already-structured StepFailure (e.g. the R13.1 verdict parse
        # failure) is re-raised as-is — never re-wrapped.
        if isinstance(e, StepFailure):
            raise
        raise StepFailure(
            "test_code",
            str(e),
            details={
                "run_id": event.get("run_id", "unknown"),
                "pipeline_id": event.get("pipeline_id", "unknown"),
            },
        ) from e
