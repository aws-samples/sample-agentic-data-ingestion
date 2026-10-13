# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: a construct the pandas proxy REFUSES executes under real Spark.

Spec: `.kiro/specs/p42-real-spark-verification/` task 11.4 — **Requirement 3.1**.

This is the sharpest single demonstration of why the second executor exists. One
transform, two executors, opposite expected outcomes:

* ``initcap`` is a member of ``_UNSUPPORTED_FUNCTIONS``
  (``pyspark_to_pandas.py:573-582``), so the pandas proxy **refuses**
  ``withColumn('name', initcap(col('name')))`` and reports
  ``not_verified_by_proxy`` — nothing is proven about the transform.
* Design §C10 measured the *same* construct **executing successfully** on the
  billable probe session. In real Spark it needs no rule.

R3.1 is the claim that real Spark dissolves the refusal, and this module is the
evidence for it. ``test_a_refused_construct_executes_under_real_spark`` is the
only place in the suite where that claim is checked against real Spark rather
than against a mock.

## The deliberate symmetry with task 11.5

``tests/unit/lambdas/test_code/test_converter_intact.py`` uses **this same
transform** as its refusal specimen (its ``REFUSED`` constant) and asserts the
refusal still works. That is not a contradiction of this file — it is the other
half of the same fact, and the pairing is on purpose:

    one construct, two executors, opposite expected outcomes.

Precisely *because* real Spark dissolves the refusal, the refusal must keep
working until the Glue executor is the one actually running. Requirement 3.3 and
design §3 do-NOT 1 hold the converter as the fallback; ``LGR-4dd9b42-1``
(**template / ratified**) is the record that governs the refusal, and its revisit
condition is what this module instruments:

    "or PySpark becomes available to the test environment, at which point the
    refused shapes can be compared against real Spark and converted on evidence
    instead of refused on caution."

Note what that condition authorises: *converting on evidence*. This test produces
one data point of that evidence for one construct. It is **not** authorisation to
retire the converter — that needs the shadow-mode population (task 10.x), and
``LGR-4dd9b42-1`` retires together with ``LGR-f6519e1-1``, ``LGR-4099d91-1`` and
``LGR-4099d91-2`` (design §3 do-NOT 1). Reading a green run here as "the
converter can go" is the misreading ``test_converter_intact.py`` exists to stop.

## Two tests, at different gates

* ``test_the_pandas_proxy_refuses_this_construct`` — **free**, no AWS call, runs
  at Gate 1 (``make test-e2e``). It is the *premise* of the comparison, not a
  re-test of the converter. Without it the billable test could silently stop
  being evidence for anything: if ``initcap`` ever left
  ``_UNSUPPORTED_FUNCTIONS``, "a refused construct executes under real Spark"
  would be measuring an unrefused construct and would still pass. The converter's
  rules themselves belong to its own 42-file suite and are not re-tested here.
* ``test_a_refused_construct_executes_under_real_spark`` — **billable**, runs
  only at Gate 2 (``make test-e2e-trigger``). Creates a real Glue interactive
  session.

## Cost

One session, 2 DPU, 1-minute minimum billing at
``USW2-GlueInteractiveSession-DPU-Hour`` = $0.44/DPU-hour. Design §C10's measured
arithmetic for exactly this shape of run: ~46 s to ``READY`` + 8 s first
statement + 17 s first Spark action + 2 s + teardown ≈ **75 s wall, $0.0269**.
``IdleTimeout`` is 1 minute (design §4.3), so a session the release path could
not reach costs ~$0.015 rather than ~$0.22.

The release path is **not** re-implemented here. ``GlueSessionTestRunner``'s
``run_session`` stops and deletes every session it created in a ``finally``, on
every exit path including one taken because an exception was raised (R7.1, R7.2).
That mechanism is the thing under test, so this module *audits* it rather than
cleaning up behind it — see ``_billing_session_states`` for why a detected leak
fails the test instead of being silently deleted.

## Prerequisites, and the one that is not wired yet

**``VERIFICATION_SESSION_ROLE_ARN`` is not set by any CDK stack.** Verified by
grep across ``deploy-cdk/lib/`` at the time of writing: the variable is read at
``lambda_function.py:2500`` and named at ``:419``, and nothing assigns it. Task
2.4 added ``VERIFICATION_EXECUTOR`` to both environments; the role ARN was not
part of it. So whoever runs this test must supply the ARN — or the role name, and
let the IAM lookup below resolve it.

The role itself is ``AgenticDataIngestion-VerificationSession-Role`` (task 2.1,
Foundation stack): read-only, no write action of any kind (R6.1-R6.3). This module
deliberately does **not** fall back to a broader role when it cannot resolve that
one — it skips. Handing unreviewed AI-generated PySpark a write-capable role is
the trade design §4.6 refuses.

**No credential of any kind appears here.** The transform runs against a
synthetic frame built from the declared schema; no JDBC source is read (R9.3), so
``LGR-819980f-1`` (**standing invariant**: JDBC credentials are only ever
referenced by Secrets Manager ARN, because plaintext leaked a real credential
once) has nothing to bind to on this path. If a future edit wants a real source
here, read that record and ``LGR-819980f-2`` first: the task 2.1 role has no
Secrets Manager access at all, which is deliberate.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| ``VERIFICATION_SESSION_ROLE_ARN`` | — | Session runtime role ARN (the production variable name) |
| ``ADI_PROBE_GLUE_ROLE_ARN`` | — | Secondary override, shared with the concurrency probe |
| ``ADI_VERIFICATION_SESSION_ROLE_NAME`` | ``AgenticDataIngestion-VerificationSession-Role`` | Role looked up when no ARN is given |
| ``ADI_REFUSED_CONSTRUCT_OBSERVATION_PATH`` | ``tests/e2e/_artifacts/glue_refused_construct.json`` | Where the observation record is written |

The region is **not** pinned here. ``LGR-3e45e65-1`` (**template / ratified**)
fixes the region by assignment in the *unit* layer only, and records that
integration and e2e "run against real AWS and must keep whatever region the
caller provides". The ``aws_region`` fixture reads it from the caller's
environment.

Run with: ``make test-e2e-trigger``
"""

import importlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ── The specimen ──────────────────────────────────────────────────────
#
# Byte-identical to `test_run_test_composition.py::TRANSFORM` (task 8.1), and the
# same construct as `test_converter_intact.py::REFUSED` — which spells the inner
# strings with double quotes. The quoting differs, the construct does not: both
# apply `initcap` to `col("name")` inside a `withColumn`, and the converter
# refuses on the FUNCTION NAME (`pyspark_to_pandas._function_call`), not on the
# quote style. Kept aligned with the composition test rather than with the
# converter test because this file drives the same executor that one does.
TRANSFORM = "df_transformed = df.withColumn('name', initcap(col('name')))"

#: The refused member. Named as a constant because both tests below assert
#: something about *this* function, and R3.1 is a claim about membership of
#: `_UNSUPPORTED_FUNCTIONS` rather than about `initcap` specifically.
REFUSED_FUNCTION = "initcap"

#: One column, a type the synthetic frame builder knows (`spark_type_for`).
#: Matching `test_run_test_composition.py`'s SCHEMA/TARGET_SCHEMA so that a
#: divergence between the mocked drive and this real one is attributable to the
#: executor rather than to the input.
SCHEMA = {"properties": {"name": {"type": "string"}}}
TARGET_SCHEMA = {"properties": {"name": {"type": "string"}}}

# ── Session states that bill ──────────────────────────────────────────
# A session in one of these is consuming DPU. `_SESSION_STATES_TERMINAL_BAD`
# in the runner covers the opposite end (FAILED/STOPPED/STOPPING/TIMEOUT);
# these two are the ones a leak would sit in.
_BILLING_SESSION_STATES = frozenset({"PROVISIONING", "READY"})

#: The runner's own id prefix (`glue_session_runner._SESSION_ID_PREFIX`). Read as
#: a literal rather than imported so the leak audit does not depend on a private
#: name, and asserted against the module in the billable test so a rename is
#: caught rather than silently disabling the audit.
_SESSION_ID_PREFIX = "adi-verify"

_DEFAULT_OBSERVATION_PATH = os.path.join(os.path.dirname(__file__), "_artifacts", "glue_refused_construct.json")

_LAMBDA_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "agentic_data_ingestion_test_code")
)


def _import_test_code_module(module_name: str) -> ModuleType:
    """Import a module from the code-testing Lambda, lazily.

    Two deliberate deviations from how the unit layer does this, both about not
    disturbing a layer that has never needed it — no other file under
    ``tests/e2e/`` touches ``sys.path`` at all:

    * **Inside a function, not at module scope.** A module-scope import here
      would run at *collection* time on every ``pytest tests/e2e/`` invocation,
      including ones that will skip. An import error would then be a collection
      error for the whole layer, which
      ``test_migration_preservation_prop.py::test_zero_collection_errors_per_layer[e2e]``
      asserts against.
    * **``append``, not ``insert(0)``.** The unit layer inserts at the front,
      which is safe there because ``tests/unit/lambdas/conftest.py`` evicts
      ``lambda_function`` from ``sys.modules`` between test modules. The e2e
      layer has no such hook, so putting this directory first would let its
      ``lambda_function`` shadow any other Lambda a future e2e test imports.
      Appending is enough: ``glue_session_runner``, ``code_interpreter_runner``
      and ``pyspark_to_pandas`` exist nowhere else on the path.
    """
    if _LAMBDA_TEST_CODE_DIR not in sys.path:
        sys.path.append(_LAMBDA_TEST_CODE_DIR)
    return importlib.import_module(module_name)


def _resolve_session_role_arn(boto3_session) -> str:
    """Resolve the verification session's runtime role, or skip.

    Order: the production variable, then the probe's shared override, then an IAM
    lookup of the role by name. There is deliberately **no** fourth step: a
    broader role would let unreviewed AI-generated PySpark do more than read
    (R6.3, design §4.6), and the point of this test is not worth that.
    """
    for env_var in ("VERIFICATION_SESSION_ROLE_ARN", "ADI_PROBE_GLUE_ROLE_ARN"):
        override = os.environ.get(env_var, "").strip()
        if override:
            logger.info("Using session role from %s", env_var)
            return override

    role_name = os.environ.get(
        "ADI_VERIFICATION_SESSION_ROLE_NAME",
        "AgenticDataIngestion-VerificationSession-Role",
    )
    try:
        return str(boto3_session.client("iam").get_role(RoleName=role_name)["Role"]["Arn"])
    except ClientError as exc:
        pytest.skip(
            f"Verification session role {role_name!r} not resolvable "
            f"({exc.response['Error']['Code']}). Deploy spec task 2.1 (Foundation stack), or set "
            "VERIFICATION_SESSION_ROLE_ARN — no CDK stack sets it yet."
        )


def _verification_session_ids(glue) -> set[str]:
    """Every session id in the account carrying the runner's prefix.

    Returns an empty set when sessions cannot be listed, so a missing
    ``glue:ListSessions`` permission degrades the leak audit to a no-op rather
    than failing a test about something else. The audit says so in its own
    message when that happens.
    """
    ids: set[str] = set()
    try:
        next_token = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = glue.list_sessions(**kwargs)
            # `list_sessions` may answer with bare `Ids` or with full `Sessions`
            # entries depending on the request; tolerate both rather than
            # depending on which.
            ids.update(str(i) for i in resp.get("Ids", []))
            ids.update(str(s.get("Id")) for s in resp.get("Sessions", []) if s.get("Id"))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except ClientError as exc:
        logger.warning("Could not list Glue sessions; leak audit degraded: %s", exc)
        return set()
    return {i for i in ids if i.startswith(_SESSION_ID_PREFIX)}


def _billing_session_states(glue, session_ids: set[str]) -> dict[str, str]:
    """Which of *session_ids* are in a state that bills, and what state.

    Used to audit R7.1/R7.2 after the runner has returned. A detected leak
    **fails the test and is not deleted**, which is a decision rather than an
    omission: an ``adi-verify-*`` id cannot be proven to belong to this test —
    a concurrent pipeline verification in the same account creates sessions with
    the same prefix — and deleting another run's session to tidy up a test would
    be the more destructive error. It is also the point: a release path that
    leaks must fail loudly, and a test that quietly cleaned up after it would
    hide exactly the defect R7.1 and R7.2 exist to prevent. ``IdleTimeout`` is 1
    minute, so an unswept leak costs ~$0.015 and self-releases.
    """
    billing: dict[str, str] = {}
    for session_id in sorted(session_ids):
        try:
            state = str(glue.get_session(Id=session_id).get("Session", {}).get("Status", "")).upper()
        except ClientError:
            # Gone, or unreadable. Gone is the good case and the common one.
            continue
        if state in _BILLING_SESSION_STATES:
            billing[session_id] = state
    return billing


def _write_observation(record: dict) -> str | None:
    """Write the observation record to disk and log it. Never raises."""
    path = os.environ.get("ADI_REFUSED_CONSTRUCT_OBSERVATION_PATH", _DEFAULT_OBSERVATION_PATH)
    rendered = json.dumps(record, indent=2, default=str, sort_keys=True)
    logger.warning(
        "\n=== REFUSED-CONSTRUCT-UNDER-REAL-SPARK OBSERVATION (R3.1) ===\n%s\n"
        "=== This is one data point toward LGR-4dd9b42-1's revisit condition, NOT "
        "authorisation to retire the converter (R3.3, design do-NOT 1) ===",
        rendered,
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        return path
    except OSError as exc:
        logger.warning("Could not write observation record to %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Gate 1 — the premise. Free, no AWS call, no session.
# ---------------------------------------------------------------------------


def test_the_pandas_proxy_refuses_this_construct():
    """The specimen is still a construct the pandas proxy refuses (R3.1's premise).

    **Validates: Requirement 3.1**

    Not a re-test of the converter — that is its own 42-file suite, and task
    11.5's ``test_converter_intact.py`` owns the "it still refuses" fence. What
    this asserts is that the *specimen below is still a specimen*: R3.1 is a claim
    about "a function the Pandas_Proxy lists as unsupported
    (``pyspark_to_pandas.py:573-582``)", so if ``initcap`` ever left that set,
    the billable test would be executing an unrefused construct, would still go
    green, and would be evidence for nothing at all.

    Membership is asserted directly against ``_UNSUPPORTED_FUNCTIONS`` as well as
    through the raised error, because the two fail differently: the set could keep
    the name while ``_function_call`` stopped consulting it, and the refusal could
    survive for some other reason than membership.
    """
    pyspark_to_pandas = _import_test_code_module("pyspark_to_pandas")

    assert REFUSED_FUNCTION in pyspark_to_pandas._UNSUPPORTED_FUNCTIONS, (
        f"{REFUSED_FUNCTION!r} is no longer in _UNSUPPORTED_FUNCTIONS "
        "(pyspark_to_pandas.py:573-582). This test's specimen is no longer a refused "
        "construct, so test_a_refused_construct_executes_under_real_spark is no longer "
        "evidence for R3.1 — pick a construct that is still refused, or retire this file "
        "along with the requirement."
    )

    with pytest.raises(pyspark_to_pandas.ConversionError) as raised:
        pyspark_to_pandas.PySpark2PandasConverter.convert(TRANSFORM)

    assert raised.value.construct == REFUSED_FUNCTION
    assert raised.value.line == TRANSFORM


# ---------------------------------------------------------------------------
# Gate 2 — the measurement. BILLABLE: creates a real Glue session.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_refused_construct_executes_under_real_spark(
    requires_trigger,
    boto3_session,
    aws_region,
    account_id,
):
    """R3.1: the construct the proxy refuses executes, and the outcome is reported.

    **Validates: Requirement 3.1**

    **Billable.** One Glue interactive session, ~75 s wall and ~$0.03 (design
    §C10). Runs only under ``make test-e2e-trigger``.

    Driven through the **public** ``run_test``, with the same three arguments and
    the same constructor shape production uses (``lambda_function.py:2539-2543``),
    so what is measured is the executor a real run would get rather than a
    lifecycle assembled for the test. ``source_data_uri`` is deliberately left
    unset: the frame is synthetic, which keeps this test's subject the *transform*
    rather than a source read (R9.2's fallback, and no credential surface —
    ``LGR-819980f-1``).

    What is asserted, and why each one:

    * **R3.1, the whole point.** ``execution_successful`` true, ``executed`` true,
      ``exit_code`` zero — R1.5's shape for a transform that ran and validated.
      The pandas proxy reaches none of these three on this input: it returns
      ``executed`` false without spending a session at all
      (``test_converter_intact.py::test_run_test_still_routes_a_refusal_through_the_converter``).
    * **R3.2, in the one place it can be observed for real.** No
      ``not_verified_by_proxy`` marker, because there is no proxy on this path.
      Property 4 (task 8.3) proves this over generated outcomes against a mock;
      this is the same claim against real Spark.
    * **Validation actually ran.** ``execution_successful`` is set by
      ``_parse_results`` only where it found a ``VALIDATION:`` payload with no
      ``error`` key, so a non-empty ``data_quality_results`` is what distinguishes
      "Spark ran the transform and the validation select" from a success that
      claims more than it measured (R12.4).
    * **R7.1/R7.2, audited not re-implemented.** The runner releases its own
      session in a ``finally``; this checks that no prefixed session is left in a
      billing state. A leak fails rather than being swept — see
      :func:`_billing_session_states`.

    Deliberately **not** asserted: any data-quality *value*. Those are task 1.1's
    characterization and Property 13's subject, derived from the definitions in
    R10.2. Re-deriving an expected number here would duplicate that work in the
    one place it cannot be cheaply re-run.
    """
    glue_session_runner = _import_test_code_module("glue_session_runner")

    # The leak audit reads ids by prefix, so a rename would silently disable it
    # rather than fail it. Checked here, where a failure is cheap and legible.
    assert glue_session_runner._SESSION_ID_PREFIX == _SESSION_ID_PREFIX, (
        "the runner's session-id prefix moved; the leak audit in this file keys off it"
    )

    role_arn = _resolve_session_role_arn(boto3_session)
    glue = boto3_session.client("glue")

    # Snapshot BEFORE the run so the audit can tell a session this run left
    # behind from one that was already open — a concurrent pipeline verification
    # in the same account carries the same prefix.
    pre_existing = _verification_session_ids(glue)

    runner = glue_session_runner.GlueSessionTestRunner(
        region=aws_region,
        role_arn=role_arn,
    )

    started_at = datetime.now(UTC).isoformat()
    results: dict[str, Any] | None = None
    try:
        results = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)
    finally:
        # Recorded on every exit path, including a raise, because a run that blew
        # up mid-session is exactly when the release audit matters most.
        appeared = _verification_session_ids(glue) - pre_existing
        still_billing = _billing_session_states(glue, appeared)
        record = {
            "requirement": "3.1",
            "spec": "real-spark-verification task 11.4",
            "measured_at": started_at,
            "account": account_id,
            "region": aws_region,
            "transform": TRANSFORM,
            "refused_function": REFUSED_FUNCTION,
            "refused_by_pandas_proxy": True,
            "results": results,
            "sessions_pre_existing": sorted(pre_existing),
            "sessions_appeared_during_run": sorted(appeared),
            "sessions_still_billing_after_run": still_billing,
            "note": (
                "One data point toward LGR-4dd9b42-1's revisit condition (compare refused "
                "shapes against real Spark). NOT authorisation to retire the converter: "
                "R3.3 and design do-NOT 1 keep it as the fallback until shadow mode (task "
                "10.x) supplies the population. Design C10 measured this same construct "
                "executing on the probe session; this re-measures it through the public "
                "run_test. Per C10, do not turn a single observation into a rate."
            ),
        }
        record["observation_path"] = _write_observation(record)

    # ── R7.1 / R7.2 — cost safety first, before anything about the result ──
    assert not still_billing, (
        f"{len(still_billing)} Glue session(s) created during this run are still in a billing "
        f"state and the runner's release path should have stopped and deleted them "
        f"(R7.1, R7.2): {still_billing}. They self-release at the 1-minute IdleTimeout; to "
        "release now: aws glue delete-session --id <id>"
    )
    assert runner.session_id is None, (
        f"the runner still holds session {runner.session_id!r} after run_test returned; "
        "run_session's finally should have cleared it (R7.1, R7.2)"
    )

    # ── R3.1 — the construct the proxy refuses ran, and ran successfully ──
    assert results is not None
    assert results["execution_successful"] is True, (
        f"a transform using {REFUSED_FUNCTION!r} did not execute successfully under real Spark. "
        f"R3.1 is the claim that it does, and design C10 measured it doing so. "
        f"error_details={results.get('error_details')!r} stderr={results.get('stderr')!r}"
    )
    assert results["executed"] is True
    assert results["exit_code"] == 0
    assert results["error_details"] is None

    # ── R3.2 — no proxy ran, so no proxy non-verification can be claimed ──
    assert "not_verified_by_proxy" not in results, (
        "a Glue result carries a proxy non-verification marker; there is no proxy on this "
        f"path (R3.2). results keys: {sorted(results)}"
    )
    assert results["conversion_report"] is None, (
        "conversion_report must stay None on the Glue executor — no conversion ran "
        "(glue_session_runner.run_test's docstring records why synthesising one was rejected)"
    )

    # ── R12.4 — the success is a validated one, not a vacuous one ──
    assert results["data_quality_results"], (
        "execution reported success but no data-quality values were parsed, so the "
        "VALIDATION: payload did not arrive — a success that validated nothing (R12.4). "
        f"stderr={results.get('stderr')!r}"
    )
