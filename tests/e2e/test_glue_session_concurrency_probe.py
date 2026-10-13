# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: Glue interactive-session concurrency and quota probe.

Spec: `.kiro/specs/p42-real-spark-verification/` task 1.3 — Requirements 11.1, 11.3.

This module answers the one question that gates moving the default
Verification_Executor off `pandas`: **at how many concurrent interactive
sessions does this account refuse `CreateSession`?** Nothing in the AWS surface
answers it (see the quota observation below), so it has to be measured.

Two tests, deliberately at different gates:

* ``test_no_published_interactive_session_quota_exists`` — read-only, runs at
  Gate 1 (``make test-e2e``). Pins Requirement 11.3 as a *checkable* claim
  rather than a comment: it fails if AWS ever publishes the quota, which is the
  event that would retire the probe.
* ``test_concurrent_create_session_ceiling`` — **billable**, runs only at Gate 2
  (``make test-e2e-trigger``). Creates real Glue interactive sessions.

## Cost

Each session billed here is 2 DPU with a 1-minute minimum at
``USW2-GlueInteractiveSession-DPU-Hour`` = $0.44/DPU-hour, so roughly **$0.015
per session** at the floor (design §C10, measured). At the default concurrency
of 6 that is about **$0.09** per probe run. ``_HARD_MAX_SESSIONS`` caps the
worst case regardless of what the environment asks for.

Every session is stopped **and** deleted in a ``finally``, on every exit path
including an exception, and the test **fails** if any session could not be
released — a leaked session bills until its idle timeout. ``IdleTimeout`` is 1
minute (design §4.3) precisely so that a leak the release path could not reach
costs ~$0.015 rather than ~$0.22.

## OBSERVATIONS

**R11.1 — the concurrency ceiling: NOT YET MEASURED.**
``MEASURED_CONCURRENCY_CEILING`` below is ``None``. It has not been measured in
any account, because running this probe creates billable sessions and that has
not been done. Requirement 11.2 keeps the default Verification_Executor at
`pandas` for exactly as long as that constant is ``None``; do not read the
constant's presence as evidence that a measurement happened. Running this test
prints and writes the observation record — paste the result into the constant
block and cite the account and region with it.

**R11.3 — no published Glue Spark interactive-session quota exists.**
Service Quotas returned **47** Glue quotas at the time of the design, and none
of them bounds concurrent Spark interactive sessions. `CreateSession` documents
``ResourceNumberLimitExceededException``, so the ceiling is real but unpublished.
**A later reader does not need to repeat that search** —
``test_no_published_interactive_session_quota_exists`` re-checks it on every
Gate 1 run and will fail if it stops being true. The 47 is recorded as an
observation, not asserted: the assertion is the *absence* of such a quota, since
AWS adding unrelated Glue quotas is normal and must not fail the suite.

**C9 — trust the reported ``DPUSeconds``, not the documented formula.**
The Glue ``Session`` structure documents ``DPUSeconds = ExecutionTime ×
MaxCapacity``. The probe session that produced §C10's numbers contradicts it:
``947.804 × 10.0 = 9478`` against a reported ``1849``. This module therefore
**reads** ``DPUSeconds`` and records the formula's product alongside it as a
discrepancy, and never substitutes the product for the field.

## Prerequisite

The probe passes the dedicated read-only verification session role (spec task
2.1, ``AgenticDataIngestion-VerificationSession-Role``) as the session's runtime
role. That role does not exist until task 2.1 is deployed, so until then this
test **skips** rather than falling back to a broader role — handing an
unmeasured probe a write-capable role is the wrong trade. Override with
``ADI_PROBE_GLUE_ROLE_ARN`` to measure with a role you have chosen explicitly.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| ``ADI_PROBE_CONCURRENCY`` | 6 | Sessions to request concurrently (capped at 12) |
| ``ADI_PROBE_GLUE_ROLE_ARN`` | — | Session runtime role ARN override |
| ``ADI_VERIFICATION_SESSION_ROLE_NAME`` | ``AgenticDataIngestion-VerificationSession-Role`` | Role looked up when no ARN override is given |
| ``ADI_PROBE_OBSERVATION_PATH`` | ``tests/e2e/_artifacts/glue_session_concurrency_probe.json`` | Where the observation record is written |

The region is **not** pinned here: ``LGR-3e45e65-1`` fixes the region by
assignment in the unit layer only, and records that integration and e2e "run
against real AWS and must keep whatever region the caller provides". The
``aws_region`` fixture reads it from the caller's environment.

Run with: ``make test-e2e-trigger``
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ── The R11.1 measurement slot ────────────────────────────────────────
#
# NOT YET MEASURED. Fill in from a real probe run, together with the account
# and region it was measured in — the ceiling is an account-and-region
# property, so a bare number is not a measurement. Requirement 11.2 holds the
# default Verification_Executor at `pandas` while this is None.
#
#   MEASURED_CONCURRENCY_CEILING = <int>   # sessions admitted before refusal
#   MEASURED_IN_ACCOUNT = "<account id>"
#   MEASURED_IN_REGION = "<region>"
#   MEASURED_AT = "<ISO-8601 UTC>"
MEASURED_CONCURRENCY_CEILING: int | None = None
MEASURED_IN_ACCOUNT: str | None = None
MEASURED_IN_REGION: str | None = None
MEASURED_AT: str | None = None

# Observed Glue quota count at design time (R11.3). Recorded, not asserted —
# see the module docstring for why.
OBSERVED_GLUE_QUOTA_COUNT_AT_DESIGN_TIME = 47

# ── Probe bounds ──────────────────────────────────────────────────────
# A hard ceiling on billable sessions, independent of what the environment
# asks for. At ~$0.015 per session this bounds a single run to ~$0.18.
_HARD_MAX_SESSIONS = 12
_DEFAULT_CONCURRENCY = 6

# Session shape — matches the measured probe session in design §C10 so the
# numbers this records are comparable with the ones already recorded there.
_IDLE_TIMEOUT_MINUTES = 1  # design §4.3 — bounds the cost of a leak
_WORKER_TYPE = "G.1X"
_NUMBER_OF_WORKERS = 2
_GLUE_VERSION = "5.0"

# Release budget. Glue refuses DeleteSession while a session is PROVISIONING,
# so the release path polls; ~46 s to READY was measured (§C10).
_RELEASE_DEADLINE_SECONDS = 240
_RELEASE_POLL_SECONDS = 5

_CAPACITY_ERROR_CODE = "ResourceNumberLimitExceededException"

_DEFAULT_OBSERVATION_PATH = os.path.join(os.path.dirname(__file__), "_artifacts", "glue_session_concurrency_probe.json")


def _requested_concurrency() -> int:
    """How many concurrent sessions to request, clamped to the hard cap."""
    raw = os.environ.get("ADI_PROBE_CONCURRENCY", str(_DEFAULT_CONCURRENCY))
    try:
        n = int(raw)
    except ValueError:
        pytest.skip(f"ADI_PROBE_CONCURRENCY is not an integer: {raw!r}")
    if n < 1:
        pytest.skip(f"ADI_PROBE_CONCURRENCY must be >= 1, got {n}")
    return min(n, _HARD_MAX_SESSIONS)


def _resolve_session_role_arn(boto3_session) -> str:
    """Resolve the runtime role for the probe sessions, or skip.

    Prefers an explicit ARN override. Otherwise looks up the dedicated
    read-only verification role (spec task 2.1). Deliberately does **not**
    fall back to any other role: a broader role would let this probe do more
    than create and destroy empty sessions.
    """
    override = os.environ.get("ADI_PROBE_GLUE_ROLE_ARN", "").strip()
    if override:
        return override

    role_name = os.environ.get(
        "ADI_VERIFICATION_SESSION_ROLE_NAME",
        "AgenticDataIngestion-VerificationSession-Role",
    )
    try:
        return str(boto3_session.client("iam").get_role(RoleName=role_name)["Role"]["Arn"])
    except ClientError as exc:
        pytest.skip(
            f"Verification session role {role_name!r} not resolvable ({exc.response['Error']['Code']}). "
            "Deploy spec task 2.1 (Foundation stack) or set ADI_PROBE_GLUE_ROLE_ARN to measure with an "
            "explicitly chosen role."
        )


def _count_existing_sessions(glue) -> int | None:
    """Count sessions already open in the account, or None if unreadable.

    Pre-existing sessions count against the same ceiling, so the measurement
    is only attributable if this is recorded alongside it.
    """
    try:
        total = 0
        next_token = None
        while True:
            kwargs = {"MaxResults": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = glue.list_sessions(**kwargs)
            total += len(resp.get("Ids", []) or resp.get("Sessions", []) or [])
            next_token = resp.get("NextToken")
            if not next_token:
                break
        return total
    except ClientError as exc:
        logger.warning("Could not list existing Glue sessions: %s", exc)
        return None


def _create_session(glue, session_id: str, role_arn: str) -> None:
    """Issue one CreateSession. Raises ClientError on refusal."""
    glue.create_session(
        Id=session_id,
        Role=role_arn,
        Command={"Name": "glueetl", "PythonVersion": "3"},
        Description="agentic-data-ingestion concurrency probe (spec real-spark-verification 1.3)",
        GlueVersion=_GLUE_VERSION,
        WorkerType=_WORKER_TYPE,
        NumberOfWorkers=_NUMBER_OF_WORKERS,
        IdleTimeout=_IDLE_TIMEOUT_MINUTES,
        Tags={"Project": "AgenticDataIngestion", "Purpose": "concurrency-probe"},
    )


def _read_billing_fields(glue, session_id: str) -> dict:
    """Read the session's billing fields, honouring design C9.

    Records the reported ``DPUSeconds`` **and** the ``ExecutionTime ×
    MaxCapacity`` product the API documents, as separate values. The reported
    field is the one to trust; the product is recorded only so the discrepancy
    stays visible instead of being silently recomputed away.
    """
    try:
        session = glue.get_session(Id=session_id).get("Session", {})
    except ClientError as exc:
        return {"session_id": session_id, "error": exc.response["Error"]["Code"]}

    reported = session.get("DPUSeconds")
    execution_time = session.get("ExecutionTime")
    max_capacity = session.get("MaxCapacity")
    documented_product = None
    if execution_time is not None and max_capacity is not None:
        documented_product = float(execution_time) * float(max_capacity)

    return {
        "session_id": session_id,
        "status": session.get("Status"),
        # C9: this is the value to trust.
        "dpu_seconds_reported": reported,
        "execution_time": execution_time,
        "max_capacity": max_capacity,
        # C9: recorded for comparison only — never substituted for the field above.
        "documented_formula_product": documented_product,
        "formula_disagrees_with_reported_field": (
            None if reported is None or documented_product is None else abs(float(reported) - documented_product) > 1.0
        ),
    }


def _release_session(glue, session_id: str) -> dict:
    """Stop and delete one session. Idempotent and never raises.

    Tolerant by construction: a session that was never created, or already
    gone, releases cleanly. Glue refuses DeleteSession while a session is
    PROVISIONING, so this polls to a releasable state before deleting.

    Returns an outcome dict; ``released`` False means the session may still
    be billing and the test must fail.
    """
    outcome: dict = {"session_id": session_id, "stopped": False, "deleted": False, "released": False, "errors": []}

    def _code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", "Unknown"))

    try:
        glue.stop_session(Id=session_id)
        outcome["stopped"] = True
    except ClientError as exc:
        code = _code(exc)
        if code == "EntityNotFoundException":
            # Never created, or already fully released.
            outcome["released"] = True
            outcome["errors"].append(f"stop_session: {code} (nothing to release)")
            return outcome
        # IllegalSessionStateException — already stopping/stopped, or still
        # provisioning. Both are handled by the delete loop below.
        outcome["errors"].append(f"stop_session: {code}")

    deadline = time.time() + _RELEASE_DEADLINE_SECONDS
    while time.time() < deadline:
        try:
            glue.delete_session(Id=session_id)
            outcome["deleted"] = True
            outcome["released"] = True
            return outcome
        except ClientError as exc:
            code = _code(exc)
            if code == "EntityNotFoundException":
                outcome["released"] = True
                return outcome
            if code != "IllegalSessionStateException":
                outcome["errors"].append(f"delete_session: {code}")
                return outcome
            # Still PROVISIONING or STOPPING — wait and retry. The state is
            # read only so that a release which never completes says why in
            # the log; every refusing state is retried the same way, so
            # nothing branches on it.
            try:
                state = glue.get_session(Id=session_id).get("Session", {}).get("Status")
            except ClientError:
                state = None
            logger.debug("Session %s not yet deletable (state=%s); retrying", session_id, state)
            time.sleep(_RELEASE_POLL_SECONDS)

    outcome["errors"].append(f"delete_session: not released within {_RELEASE_DEADLINE_SECONDS}s")
    return outcome


def _write_observation(record: dict) -> str | None:
    """Write the observation record to disk and log it. Never raises."""
    path = os.environ.get("ADI_PROBE_OBSERVATION_PATH", _DEFAULT_OBSERVATION_PATH)
    rendered = json.dumps(record, indent=2, default=str, sort_keys=True)
    logger.warning(
        "\n=== GLUE SESSION CONCURRENCY PROBE OBSERVATION (R11.1) ===\n%s\n"
        "=== Paste MEASURED_CONCURRENCY_CEILING / MEASURED_IN_ACCOUNT / MEASURED_IN_REGION "
        "into %s ===",
        rendered,
        os.path.basename(__file__),
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        return path
    except OSError as exc:
        logger.warning("Could not write observation record to %s: %s", path, exc)
        return None


def _looks_like_session_concurrency_quota(quota_name: str) -> bool:
    """Whether a Glue quota name would publish the interactive-session ceiling.

    Matches on ``session`` alone. Glue quotas about jobs, crawlers, triggers
    and the catalog are expected and must not trip this; any quota naming
    *sessions* means R11.3's observation is stale and the search is worth
    repeating after all.
    """
    return "session" in quota_name.lower()


def test_no_published_interactive_session_quota_exists(boto3_session, aws_region):
    """R11.3: Service Quotas publishes no Glue interactive-session ceiling.

    Read-only, no session created, so this runs at Gate 1. It exists so a later
    reader does not repeat the search the design already performed: 47 Glue
    quotas, none of them this one.

    The assertion is the **absence** of a session quota, not the count — AWS
    adding unrelated Glue quotas is routine. If this fails, R11.3 has been
    overtaken by events: read the published quota and retire the probe.
    """
    client = boto3_session.client("service-quotas")

    quotas: list[dict] = []
    try:
        paginator = client.get_paginator("list_aws_default_service_quotas")
        for page in paginator.paginate(ServiceCode="glue"):
            quotas.extend(page.get("Quotas", []))
    except ClientError as exc:
        pytest.skip(f"Service Quotas unreadable in {aws_region} ({exc.response['Error']['Code']})")

    assert quotas, f"Service Quotas returned no Glue quotas at all in {aws_region} — unexpected; investigate"

    names = [q.get("QuotaName", "") for q in quotas]
    session_quotas = sorted(n for n in names if _looks_like_session_concurrency_quota(n))

    logger.info(
        "Glue quotas visible in %s: %d (design-time observation: %d). None name sessions.",
        aws_region,
        len(quotas),
        OBSERVED_GLUE_QUOTA_COUNT_AT_DESIGN_TIME,
    )

    assert not session_quotas, (
        "R11.3 is now stale: Service Quotas publishes Glue quota(s) naming sessions "
        f"in {aws_region}: {session_quotas}. The concurrency probe was written because no such quota "
        "existed (47 Glue quotas at design time, none of them this). Read the published quota, record "
        "it against Requirement 11.1, and retire test_concurrent_create_session_ceiling."
    )


@pytest.mark.slow
def test_concurrent_create_session_ceiling(
    requires_trigger,
    boto3_session,
    aws_region,
    account_id,
    e2e_ledger,
):
    """R11.1: measure the concurrency at which CreateSession is first refused.

    **Billable.** Issues ``ADI_PROBE_CONCURRENCY`` concurrent ``CreateSession``
    calls and records how many the account admitted before it answered
    ``ResourceNumberLimitExceededException``, together with the account and
    region — the ceiling is a property of those, not of Glue.

    What is asserted, and what is only recorded:

    * **Asserted:** every session created is released. A leak bills.
    * **Recorded:** the ceiling itself. There is nothing to assert about a
      measurement, and inventing a bound would defeat the purpose.

    If no refusal appears, the ceiling is simply *above* the concurrency
    attempted; the record says so, and the run should be repeated with a higher
    ``ADI_PROBE_CONCURRENCY``. That is an incomplete measurement, not a passing
    one — Requirement 11.2 keeps the default at `pandas` either way.
    """
    concurrency = _requested_concurrency()
    role_arn = _resolve_session_role_arn(boto3_session)
    glue = boto3_session.client("glue")

    pre_existing = _count_existing_sessions(glue)
    run_tag = f"{int(time.time())}-{os.getpid()}"
    # Ids are generated up front, not read back off the responses, so that a
    # CreateSession whose response is lost (timeout, retry) still leaves a
    # session this test knows the name of and can release.
    session_ids = [f"adi-probe-{run_tag}-{i:02d}" for i in range(concurrency)]

    # Belt-and-braces: the ledger releases anything the finally below cannot
    # reach (e.g. an exception inside the finally itself). _release_session is
    # idempotent, so releasing twice is harmless.
    for session_id in session_ids:
        e2e_ledger.add_callable(_release_session, glue, session_id)

    created: list[str] = []
    capacity_refusals: list[dict] = []
    other_errors: list[dict] = []
    completion_order: list[dict] = []
    release_outcomes: list[dict] = []
    billing: list[dict] = []
    started_at = datetime.now(UTC).isoformat()

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_create_session, glue, sid, role_arn): sid for sid in session_ids}
            for future in as_completed(futures):
                session_id = futures[future]
                try:
                    future.result()
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
                    entry = {
                        "session_id": session_id,
                        "error_code": code,
                        "message": str(exc.response.get("Error", {}).get("Message", "")),
                    }
                    completion_order.append({"session_id": session_id, "outcome": code})
                    if code == _CAPACITY_ERROR_CODE:
                        capacity_refusals.append(entry)
                    else:
                        other_errors.append(entry)
                except Exception as exc:  # a probe records the failure, it does not classify it
                    completion_order.append({"session_id": session_id, "outcome": type(exc).__name__})
                    other_errors.append(
                        {"session_id": session_id, "error_code": type(exc).__name__, "message": str(exc)}
                    )
                else:
                    created.append(session_id)
                    completion_order.append({"session_id": session_id, "outcome": "created"})

        # Billing fields, read before teardown (C9).
        billing = [_read_billing_fields(glue, sid) for sid in created]
    finally:
        # Every id, not just the created ones: a create whose response was lost
        # may still have left a session behind. Runs on every exit path.
        release_outcomes = [_release_session(glue, sid) for sid in session_ids]

    # ── The observation record (R11.1) ────────────────────────────────
    first_refusal_completion_ordinal = next(
        (i + 1 for i, e in enumerate(completion_order) if e["outcome"] == _CAPACITY_ERROR_CODE),
        None,
    )
    refused = bool(capacity_refusals)
    record = {
        "requirement": "11.1",
        "spec": "real-spark-verification task 1.3",
        "measured_at": started_at,
        "account": account_id,
        "region": aws_region,
        "glue_version": _GLUE_VERSION,
        "worker_type": _WORKER_TYPE,
        "number_of_workers": _NUMBER_OF_WORKERS,
        "idle_timeout_minutes": _IDLE_TIMEOUT_MINUTES,
        "sessions_pre_existing_in_account": pre_existing,
        "concurrency_attempted": concurrency,
        "sessions_created": len(created),
        "capacity_refusals": len(capacity_refusals),
        "other_errors": other_errors,
        # The load-bearing number. Under a concurrent burst the refusal's
        # *completion* ordinal is not the creation ordinal, so the ceiling is
        # expressed as the count the account admitted.
        "sessions_admitted_before_refusal": len(created) if refused else None,
        "creation_first_refused_at_concurrency": (len(created) + 1) if refused else None,
        "first_refusal_completion_ordinal": first_refusal_completion_ordinal,
        "ceiling_is_above_attempted_concurrency": not refused,
        "measurement_complete": refused,
        "billing_per_session_c9": billing,
        "release_outcomes": release_outcomes,
        "note": (
            "No published Glue Spark interactive-session quota exists (R11.3); "
            f"{OBSERVED_GLUE_QUOTA_COUNT_AT_DESIGN_TIME} Glue quotas at design time, none of them this. "
            "DPUSeconds is read from the Session structure, never recomputed as ExecutionTime x MaxCapacity (C9)."
        ),
    }
    record["observation_path"] = _write_observation(record)

    # ── Assertions: cost safety only ──────────────────────────────────
    leaked = [o for o in release_outcomes if not o["released"]]
    assert not leaked, (
        f"{len(leaked)} Glue session(s) could not be released and are billing until their "
        f"{_IDLE_TIMEOUT_MINUTES}-minute idle timeout: {leaked}. Delete them manually: "
        "aws glue delete-session --id <id>"
    )

    # A probe that created nothing measured nothing — that is a setup failure,
    # not a ceiling of zero.
    assert created or capacity_refusals, (
        f"No session was created and no capacity refusal was observed; nothing was measured. Errors: {other_errors}"
    )
