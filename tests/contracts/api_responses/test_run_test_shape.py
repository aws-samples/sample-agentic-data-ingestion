# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pin ``CodeInterpreterTestRunner.run_test``'s result shape, per outcome.

p42 (real-spark-verification) task 1.2 — the characterization baseline for
requirements 1.1 and 1.2. The declared shapes live in
``tests/fixtures/run_test_result_shapes.py``; this module drives the **real**
runner down each of its return paths and asserts the result conforms.

**Why the table is somewhere else, and where this departs from the directory's
ratified pattern.** ``LGR-28b4d75-5`` (template) states the principle these
tests are built on: "a test written from the producer's output cannot detect the
producer drifting, because the output is its own oracle". Its chosen mechanism
is to parse an *independent consumer* — a frontend TypeScript interface — at
test time, and it explicitly **rejects** "transcribing the field list into the
test or into a JSON schema", which is nearer to what this module does.

That rejection is not carried over here, and the reason is that its premise does
not hold: ``run_test`` has no consumer-side declaration of its shape to parse.
Its consumers read individual keys (``aggregate_check_results`` and the
dashboard's artifact components), none of which declares the envelope. The
oracle that *will* exist is a second producer — Property 2 (task 8.2) compares
``GlueSessionTestRunner.run_test`` against this table — and it cannot be parsed
because it has not been written yet. So the table is hand-authored, which keeps
the "own oracle" hazard in one specific form: a runner change plus a matching
table edit passes. What the split buys is that such an edit is a reviewable diff
in a file whose only purpose is to be that oracle, rather than an invisible
re-baseline. Without a written-down shape, R1.1's "the same key set" is
unfalsifiable in any form.

**What is and is not asserted.** Key set and exact value type, only. Values are
asserted exactly once per outcome, and only for the discriminators that decide
which shape it is (``executed``, ``execution_successful``, the non-verification
markers) — because a shape contract that also pinned ``error_details``' wording
would fail on every copy edit, and copy is not the contract. The values
themselves are covered by the seven existing test files that call ``run_test``
— five under ``tests/unit/lambdas/test_code/``, one under
``tests/unit/templates/``, one under ``tests/integration/pipeline_flow/``
(counted by grep; codegraph reports five, which undercounts for the reason
tasks.md's Notes give about bare-name imports under ``lambda/**``). This module
is about the envelope, not the values.

Nothing here touches ``code_interpreter_runner.py``. The five builders are read,
not modified.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from tests.fixtures.run_test_result_shapes import (
    RUN_TEST_OUTCOME_SHAPES,
    SPEC_ENUMERATED_OUTCOMES,
    observed_shape,
    shape_violations,
)

_TEST_CODE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

# The Lambda dir is not a package, so its modules import by bare name — and it
# comes back OFF sys.path the moment they are loaded. That removal is the fence
# documented at `test_test_failure_approval_payload.py:118-124`, which reaches
# into this same directory: it contains a `lambda_function.py`, and several
# contract files later in the run do a bare `import lambda_function` against
# their own Lambda dir, which a lingering entry at position 0 would shadow
# depending on collection order. `api_responses/conftest.py` pops
# `lambda_function` from `sys.modules` between files, so those imports really do
# re-resolve against sys.path rather than hitting a cache.
#
# Safe to remove immediately: `code_interpreter_runner` pulls in its only
# sibling (`pyspark_to_pandas`) at module scope, so nothing is left to resolve
# once the import statement returns.
sys.path.insert(0, _TEST_CODE_DIR)
try:
    from code_interpreter_runner import CodeInterpreterTestRunner
finally:
    if _TEST_CODE_DIR in sys.path:
        sys.path.remove(_TEST_CODE_DIR)

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

SOURCE_SCHEMA = {
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "float"},
    }
}
TARGET_SCHEMA = {
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "float"},
        "status": {"type": "string"},
    }
}

#: Converts cleanly — `withColumn`/`lower` have pandas equivalents.
CONVERTIBLE_TRANSFORM = 'df_transformed = df.withColumn("name", lower(col("name")))'

#: Refused by the proxy. `groupBy` is a member of
#: `_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS` — it changes the frame's shape, so no
#: single-frame pandas equivalent exists. Chosen over an unimplemented construct
#: on purpose: a permanent member's membership is governed by the criterion
#: documented at `code_interpreter_runner.py:131-186`, so it cannot be
#: reclassified out from under this test the way `initcap` was by p41.
UNCONVERTIBLE_TRANSFORM = 'df_transformed = df.groupBy("name").count()'

_GEN_JSON = json.dumps({"record_count": 100, "columns": ["id", "name", "amount"]})
_VAL_JSON = json.dumps(
    {
        "record_count": 95,
        "null_counts": {"id": 0, "name": 3, "amount": 2},
        "dtypes": {"id": "int64", "name": "object", "amount": "float64"},
        "duplicates": 2,
        "schema_compliance": 75.0,
        "completeness": 98.3,
        "uniqueness": 97.9,
        "overall_quality": 90.4,
        "missing_columns": ["status"],
    }
)
_TRACEBACK_STDERR = 'Traceback (most recent call last):\n  File "<stdin>", line 3\nKeyError: nope'


def _stream(text: str, stderr: str = "", exit_code: int = 0) -> dict:
    """A fake ``invoke_code_interpreter`` response using ``structuredContent``."""
    return {
        "stream": [
            {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"stdout": text, "stderr": stderr, "exitCode": exit_code},
                }
            }
        ]
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-shape"}
    client.stop_code_interpreter_session.return_value = {}
    return client


@pytest.fixture
def runner(mock_client):
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = mock_client
        r = CodeInterpreterTestRunner(region="us-west-2")
        r.client = mock_client
        return r


# ---------------------------------------------------------------------------
# Drivers — one per return path in run_test
# ---------------------------------------------------------------------------


def _drive_success(runner, mock_client) -> dict:
    mock_client.invoke_code_interpreter.side_effect = [
        _stream(f"GENERATED:{_GEN_JSON}"),
        _stream("transform executed"),
        _stream(f"VALIDATION:{_VAL_JSON}"),
    ]
    return runner.run_test(CONVERTIBLE_TRANSFORM, SOURCE_SCHEMA, TARGET_SCHEMA)


def _drive_statement_failure(runner, mock_client) -> dict:
    """Step 2 exits non-zero — code that RAN and failed."""
    mock_client.invoke_code_interpreter.side_effect = [
        _stream(f"GENERATED:{_GEN_JSON}"),
        _stream("partial", stderr=_TRACEBACK_STDERR, exit_code=1),
        _stream(f"VALIDATION:{_VAL_JSON}"),
    ]
    return runner.run_test(CONVERTIBLE_TRANSFORM, SOURCE_SCHEMA, TARGET_SCHEMA)


def _drive_conversion_refusal(runner, mock_client) -> dict:
    return runner.run_test(UNCONVERTIBLE_TRANSFORM, SOURCE_SCHEMA, TARGET_SCHEMA)


def _drive_frame_refusal(runner, mock_client) -> dict:
    """No declared source fields — `_frame_refusal` returns `no_source_schema`."""
    return runner.run_test(CONVERTIBLE_TRANSFORM, {}, TARGET_SCHEMA)


def _drive_client_unavailable(runner, mock_client) -> dict:
    runner.client = None
    return runner.run_test(CONVERTIBLE_TRANSFORM, SOURCE_SCHEMA, TARGET_SCHEMA)


def _drive_lifecycle_exception(runner, mock_client) -> dict:
    """An exception out of `_run_lifecycle`, caught at `run_test:309-311`."""
    mock_client.invoke_code_interpreter.side_effect = RuntimeError("interpreter went away")
    return runner.run_test(CONVERTIBLE_TRANSFORM, SOURCE_SCHEMA, TARGET_SCHEMA)


#: Outcome name → the driver that makes ``run_test`` return that shape.
DRIVERS = {
    "success": _drive_success,
    "statement_failure": _drive_statement_failure,
    "conversion_refusal": _drive_conversion_refusal,
    "frame_refusal": _drive_frame_refusal,
    "client_unavailable": _drive_client_unavailable,
    "lifecycle_exception": _drive_lifecycle_exception,
}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", sorted(RUN_TEST_OUTCOME_SHAPES))
def test_outcome_matches_declared_shape(outcome, runner, mock_client):
    """R1.1 / R1.2 — each outcome's key set and per-key value type are pinned."""
    result = DRIVERS[outcome](runner, mock_client)
    shape = RUN_TEST_OUTCOME_SHAPES[outcome]

    violations = shape_violations(result, shape)
    assert not violations, (
        f"run_test's {outcome!r} outcome no longer matches the declared shape in "
        f"tests/fixtures/run_test_result_shapes.py ({shape.source}):\n  "
        + "\n  ".join(violations)
        + "\n\nIf the change is intended, update the fixture table — deliberately, "
        "and in the same commit. Property 2 (p42 task 8.2) holds the Glue runner "
        "to that table, so a silent re-baseline here weakens that test too."
    )


def test_every_driver_produces_a_distinct_outcome(runner, mock_client):
    """Each driver reaches a DIFFERENT return path.

    Without this, a driver that quietly stopped reproducing its outcome — a
    mocked step that no longer fails, say — would still pass the shape test
    above by landing on a shape it happens to match, and the outcome it was
    meant to pin would go unexercised. The two ``_empty_results`` outcomes are
    the live case: they share a key set and differ only in
    ``conversion_report``.
    """
    fingerprints: dict[tuple, str] = {}
    for outcome, drive in sorted(DRIVERS.items()):
        # A fresh runner per driver: `_drive_client_unavailable` nulls the
        # client and `side_effect` lists are consumed.
        with patch("code_interpreter_runner.boto3") as mock_boto:
            client = MagicMock()
            client.start_code_interpreter_session.return_value = {"sessionId": "sess-shape"}
            mock_boto.client.return_value = client
            fresh = CodeInterpreterTestRunner(region="us-west-2")
            fresh.client = client
            result = drive(fresh, client)

        fingerprint = tuple(sorted((k, t.__name__) for k, t in observed_shape(result).items()))
        assert fingerprint not in fingerprints, (
            f"{outcome!r} and {fingerprints[fingerprint]!r} produced an identical "
            "key/type fingerprint, so one of them is no longer reaching the return "
            "path it names."
        )
        fingerprints[fingerprint] = outcome


def test_spec_enumerated_outcomes_are_all_covered():
    """The five outcomes task 1.2 names all have a declared shape and a driver.

    Guards the gap this fixture exists to close: a shape table missing an
    outcome makes Property 2 pass on an executor that never produces it.
    """
    missing_shapes = [name for name in SPEC_ENUMERATED_OUTCOMES if name not in RUN_TEST_OUTCOME_SHAPES]
    missing_drivers = [name for name in SPEC_ENUMERATED_OUTCOMES if name not in DRIVERS]
    assert not missing_shapes, f"no declared shape for {missing_shapes}"
    assert not missing_drivers, f"no driver for {missing_drivers}"


def test_declared_shapes_all_have_a_driver():
    """And the converse — nothing in the table is unexercised."""
    undriven = sorted(set(RUN_TEST_OUTCOME_SHAPES) - set(DRIVERS))
    assert not undriven, (
        f"{undriven} are declared in the fixture table but no driver reaches them, "
        "so their shapes are asserted against nothing."
    )


# ---------------------------------------------------------------------------
# The discriminators — the values that decide WHICH shape an outcome is
# ---------------------------------------------------------------------------


def test_success_discriminators(runner, mock_client):
    """R1.5's shape: ran, succeeded, exit zero."""
    result = _drive_success(runner, mock_client)
    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["error_details"] is None


def test_statement_failure_discriminators(runner, mock_client):
    """Ran and failed — `executed` True is what separates this from the two
    "nothing ran" outcomes, and it is the distinction R5.1 carries over to
    Glue."""
    result = _drive_statement_failure(runner, mock_client)
    assert result["execution_successful"] is False
    assert result["executed"] is True
    assert result["exit_code"] != 0


def test_conversion_refusal_discriminators(runner, mock_client):
    """The proxy marker, and the absence of the harness one.

    Both directions matter: `aggregate_check_results` branches on which marker
    is present, so a result carrying both would be reported twice.
    """
    result = _drive_conversion_refusal(runner, mock_client)
    assert result["not_verified_by_proxy"] is True
    assert "not_verified_by_harness" not in result
    assert result["execution_successful"] is False
    assert result["executed"] is False


def test_frame_refusal_discriminators(runner, mock_client):
    """The harness marker, and the absence of the proxy one."""
    result = _drive_frame_refusal(runner, mock_client)
    assert result["not_verified_by_harness"] is True
    assert "not_verified_by_proxy" not in result
    assert result["executed"] is False


def test_frame_refusal_carries_no_data_quality_results(runner, mock_client):
    """C13: an empty `data_quality_results` is what suppresses the
    `schema_compliance` score for a frame the harness caused to be short. The
    type is `dict` either way, so the shape test above cannot see this."""
    result = _drive_frame_refusal(runner, mock_client)
    assert result["data_quality_results"] == {}


def test_no_session_is_started_for_either_pre_flight_refusal(runner, mock_client):
    """Both refusals happen before `_start_session`.

    Not a shape claim, but the reason the two refusal shapes exist at all — and
    the behaviour task 5.3 carries into the Glue runner (R7.3), where a session
    costs real money.
    """
    _drive_conversion_refusal(runner, mock_client)
    mock_client.start_code_interpreter_session.assert_not_called()

    _drive_frame_refusal(runner, mock_client)
    mock_client.start_code_interpreter_session.assert_not_called()


def test_conversion_report_type_is_set_by_the_call_path(runner, mock_client):
    """The one key whose type the builders do not decide.

    All five `_*_results` builders return `conversion_report=None`; `run_test`
    overwrites it with the real dict at `:304` and `:316`. So three outcomes
    carry a dict there and two carry None, and reading the builders alone gets
    that backwards. Asserted here because it is the single most likely thing to
    be got wrong when the Glue runner is written against this contract.
    """
    assert _drive_frame_refusal(runner, mock_client)["conversion_report"] is not None
    assert _drive_client_unavailable(runner, mock_client)["conversion_report"] is None
