# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the schema_mapping ``bedrock-runtime`` client carries an explicit config (p47 R9).

Characterization tests written **before** the change, with expectations derived
from `requirements.md` R9.1–R9.5 rather than from the client's current
behaviour — current behaviour IS the defect (a bare ``boto3.client(
"bedrock-runtime")`` inheriting botocore's defaults), so pinning it would pin
the bug.

The three properties, and why each number in them is what it is:

- **R9.1/R9.3** every one of ``connect_timeout``, ``read_timeout`` and
  ``retries`` is set explicitly, i.e. none is inherited. ``retries`` is checked
  through ``total_max_attempts``, which is the key botocore normalises
  ``max_attempts`` into (``botocore/args.py:_compute_retry_max_attempts``); a
  bare client leaves it absent entirely.
- **R9.2** ``read_timeout`` exceeds the generation the call actually asks for.
  ``maxTokens`` is ``model_config.DEFAULT_MAX_TOKENS`` and ``converse`` is
  **non-streaming** — the whole response arrives in one socket read, so the
  read timeout has to cover total generation, not time-to-first-token, at a
  pessimistic throughput. That is why botocore's 60 s default is the defect and
  not merely untidy.
- **R9.5** the worst-case wall clock the client can spend —
  ``total_attempts x (connect_timeout + read_timeout)`` — fits inside the
  Lambda's own timeout (the ``SchemaMapping`` function in
  ``deploy-cdk/lib/compute-stack.ts``), so the client gives up and reports
  before the platform kills the invocation.

**Amended when ``DEFAULT_MAX_TOKENS`` rose from 4,000 to 16,000** (the revisit
condition of ``LGR-b397ee2-4``). The two constants below moved with it, and
each says why: the Lambda timeout is now 900 s, and the pessimistic throughput
is now 45 tok/s, derived from measured Bedrock latency instead of the
unmeasured 25 tok/s. At 25 tok/s a 16,000-token reply would need 640 s for one
read, which no retry could fit. The three properties themselves are unchanged.

**Validates: Requirements R9.1, R9.2, R9.3, R9.5**
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add Lambda dir and shared dir to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub out Lambda-layer dependencies (mirrors test_schema_mapping_model_budget.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod

    from model_config import DEFAULT_MAX_TOKENS
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: The Lambda's configured timeout, read from ``deploy-cdk/lib/compute-stack.ts``
#: (``timeout: cdk.Duration.seconds(900)`` on the ``SchemaMapping`` function;
#: 600 until the ceiling rose to 16,000). Duplicated here deliberately: R9.5 is
#: an arithmetic relationship between the client config and this number, and a
#: test that reads the number out of the Python it is checking would assert
#: nothing.
_LAMBDA_TIMEOUT_SECONDS = 900

#: Pessimistic output throughput, tokens/second, end to end including prefill.
#: Used only to turn ``maxTokens`` into the latency floor R9.2 requires
#: ``read_timeout`` to clear. Measured from Bedrock's CloudWatch metrics over 14
#: days in both dev accounts: Opus 5.5 67-78, Sonnet 4.6 ~78, Sonnet 5.5 ~150.
#: 45 is a third below the slowest of those. (Was an unmeasured 25.)
_PESSIMISTIC_OUTPUT_TOKENS_PER_SECOND = 45

#: botocore's default read timeout — the value the bare client inherited. R9.2
#: is not satisfied by anything at or below it.
_BOTOCORE_DEFAULT_READ_TIMEOUT_SECONDS = 60


@pytest.fixture(autouse=True)
def _restore_module():
    """Keep the shared ``lambda_function`` slot pointing at this Lambda's module."""
    sys.modules["lambda_function"] = _mod
    yield


def _client_config():
    """The resolved botocore config on the module-level Bedrock client."""
    return _mod.bedrock_runtime.meta.config


# --- R9.1 / R9.3: nothing is inherited ---


def test_bedrock_client_sets_connect_and_read_timeouts_explicitly():
    """R9.1: both timeouts are set, and neither is botocore's 60 s default."""
    config = _client_config()
    assert config.connect_timeout is not None
    assert config.read_timeout is not None
    assert config.connect_timeout != _BOTOCORE_DEFAULT_READ_TIMEOUT_SECONDS
    assert config.read_timeout != _BOTOCORE_DEFAULT_READ_TIMEOUT_SECONDS


def test_bedrock_client_sets_retry_attempts_explicitly():
    """R9.3: the attempt count is configured, not inherited from the retry policy.

    A bare client's ``retries`` is ``{"mode": "legacy"}`` with no attempt key at
    all, which silently resolves to ``_retry.json``'s ``__default__``
    ``max_attempts: 5``. Presence of ``total_max_attempts`` is what distinguishes
    "chosen" from "inherited".
    """
    retries = _client_config().retries
    assert "total_max_attempts" in retries, f"attempt count left to the retry policy: {retries}"


def test_bedrock_client_retry_count_is_lower_than_the_inherited_default():
    """R9.3: a billed generation is not retried as many times as a cheap API call.

    botocore's inherited default is 5 total attempts; each retry of this call
    re-runs and re-bills a whole generation of up to ``DEFAULT_MAX_TOKENS``.
    """
    assert _client_config().retries["total_max_attempts"] < 5


# --- R9.2: read_timeout clears the generation it asks for ---


def test_read_timeout_exceeds_pessimistic_generation_latency():
    """R9.2: ``read_timeout`` covers a full ``maxTokens`` non-streaming generation."""
    latency_floor = DEFAULT_MAX_TOKENS / _PESSIMISTIC_OUTPUT_TOKENS_PER_SECOND
    read_timeout = _client_config().read_timeout
    assert read_timeout > latency_floor, (
        f"read_timeout={read_timeout}s does not cover a {DEFAULT_MAX_TOKENS}-token "
        f"generation at {_PESSIMISTIC_OUTPUT_TOKENS_PER_SECOND} tok/s ({latency_floor:.0f}s); "
        "a slow generation would be cancelled and re-billed"
    )


def test_read_timeout_exceeds_botocore_default():
    """R9.2: the inherited 60 s is below the generation ceiling, so it must rise."""
    assert _client_config().read_timeout > _BOTOCORE_DEFAULT_READ_TIMEOUT_SECONDS


# --- R9.5: the whole budget fits inside the Lambda ---


def test_worst_case_client_budget_fits_inside_the_lambda_timeout():
    """R9.5: every attempt can time out and the client still gives up first."""
    config = _client_config()
    total_attempts = config.retries["total_max_attempts"]
    worst_case = total_attempts * (config.connect_timeout + config.read_timeout)
    assert worst_case <= _LAMBDA_TIMEOUT_SECONDS, (
        f"{total_attempts} attempts x ({config.connect_timeout}s connect + "
        f"{config.read_timeout}s read) = {worst_case}s exceeds the Lambda's "
        f"{_LAMBDA_TIMEOUT_SECONDS}s timeout — the platform would kill the "
        "invocation before the client reported"
    )


def test_worst_case_budget_leaves_room_for_the_rest_of_the_invocation():
    """R9.5: the Bedrock call is not the only thing in the Lambda's timeout.

    The same invocation also reads the target schema from S3, reads and writes
    four DynamoDB tables, queries mapping memory and publishes EventBridge
    events. Consuming the entire timeout on one client would satisfy the
    inequality above while still leaving nothing to report with.
    """
    config = _client_config()
    total_attempts = config.retries["total_max_attempts"]
    worst_case = total_attempts * (config.connect_timeout + config.read_timeout)
    assert worst_case <= _LAMBDA_TIMEOUT_SECONDS * 0.9, (
        f"worst-case client budget {worst_case}s leaves under 10% of the "
        f"{_LAMBDA_TIMEOUT_SECONDS}s timeout for the rest of the invocation"
    )
