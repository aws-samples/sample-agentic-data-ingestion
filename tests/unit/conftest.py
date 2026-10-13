# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test layer conftest — auto-applies @pytest.mark.unit to all tests.

Also carries the shell harness used by the `deploy.sh` prerequisite tests
(`test_deploy_interpreter_probe.py`, `test_deploy_bootstrap_targets.py`).
"""

import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Sequence
from unittest.mock import MagicMock

import boto3
import botocore.client
import pytest

# ── Deterministic AWS region for the unit layer ───────────────────────
# Unit tests must not depend on the ambient shell environment. Many modules in
# this tree pin a region at import time with
# ``os.environ.setdefault("AWS_REGION", ...)``, but ``setdefault`` is a no-op
# once the variable exists, so the effective region was decided by whichever
# module was imported first (53 files ask for us-east-1, 56 for us-west-2) — or
# by a stray ``export AWS_REGION=...`` in the developer's shell. Modules that
# freeze the region into a constant at import time then disagree with modules
# that read it at call time, producing failures that appear only in a
# full-suite run and pass in isolation.
#
# conftest.py is imported before any test module in this directory, so
# assigning here (not setdefault) makes every downstream setdefault a
# consistent no-op. Only the unit layer is pinned; integration and e2e run
# against real AWS and keep whatever region the caller provides.
UNIT_TEST_AWS_REGION = "us-west-2"
os.environ["AWS_REGION"] = UNIT_TEST_AWS_REGION
os.environ["AWS_DEFAULT_REGION"] = UNIT_TEST_AWS_REGION

# ── Deterministic env_utils mode for the unit layer ───────────────────
# Same mechanism as the region pin above, for ``env_utils._LOCAL_MODE``.
# ``shared/env_utils.py`` reads ``AGENTIC_DATA_INGESTION_ENV`` once, when it is
# first imported, and ``require_env(name, default)`` falls back to its default
# only in local mode. ~140 test modules ``setdefault`` it to ``local`` before
# importing a handler, but the first module to import the real ``env_utils``
# decides the mode for the whole process. That stayed hidden while dozens of
# modules left an ``env_utils`` MagicMock in ``sys.modules``; once they stopped
# leaking it, any module importing the real one before setting the variable
# (e.g. ``agents/complex_mapping_agent/test_complex_mapping.py``, through
# ``agent_trace_plugin``) froze production mode, and every handler collected
# after it failed at import on a missing table name. Assigning here, before any
# test module is imported, makes every downstream ``setdefault`` a consistent
# no-op. ``test_env_utils_props.py`` exercises production mode on a private
# copy of the module, so it is unaffected.
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"

_THIS_DIR = os.path.dirname(__file__)


def pytest_collection_modifyitems(items):
    """Automatically add the 'unit' marker to every test in this directory."""
    for item in items:
        if os.path.commonpath([str(item.fspath), _THIS_DIR]) == _THIS_DIR:
            item.add_marker(pytest.mark.unit)


# ── No unit test reaches CloudWatch PutMetricData ─────────────────────
# SCOPE NOTE: this guard is OUTSIDE p39-metrics-truth's task plan. It was
# approved by the user during execution of task 10.2, and task 24.1 records it
# as a deviation.
#
# Why it exists. p39 task 8.1 added ``metrics_facade.flush()`` to the tail of
# ``PipelineMetrics.record_stage_execution``. Before that the method buffered
# three metrics and ``put_direct_metric`` auto-flushed only at ``batch_size``
# (20), so a unit test reaching it made no API call at all. After it, any unit
# test that reaches a *real* ``record_stage_execution`` publishes for real:
# ``POWERTOOLS_ENABLED`` is unset in the test environment, so ``put_metric``
# falls through to the buffered path, and ``flush()`` -> ``flush_direct()`` ->
# ``_get_cloudwatch().put_metric_data(...)`` (``metrics_facade.py:125, 219,
# 239``). ``flush_direct``'s own ``except`` logs and returns, so the publish was
# silent in both directions.
#
# Measured, not inferred: with a botocore-level probe,
# ``tests/unit/lambdas/create_quality_checks/`` attempted **21** PutMetricData
# calls into ``AgenticDataIngestion/Pipeline`` — those three files stub
# ``metrics_facade`` in none of them and both of task 10.1's emit sites are
# reachable. A full-suite run corroborates it from the other direction: the only
# ``urllib3`` SSL warnings in the log come from ``test_duration_metrics.py``,
# i.e. an outbound HTTP attempt from a unit test.
#
# MECHANISM CHOSEN: stub the CloudWatch *client*, at both seams it can be
# acquired through.
#   1. ``boto3.client`` — where every PutMetricData writer in this tree acquires
#      it lazily, per call or per instance: ``metrics_facade.py:125`` and
#      ``notification_service.py:318``. Replaced with a ``MagicMock`` for the
#      ``cloudwatch`` service only; every other service is delegated to the real
#      ``boto3.client``, so the shared DynamoDB / S3 / SQS fixtures are
#      untouched.
#   2. ``BaseClient._make_api_call`` — a backstop for a client the construction
#      seam cannot see, either because it was built at module import time (before
#      any fixture runs — ``handlers/metrics.py:49``,
#      ``approval_timeout_handler/lambda_function.py:92``) or because it was
#      built through ``boto3.Session().client`` rather than ``boto3.client``.
#      One operation name, no behaviour change for anything else.
#
# REJECTED: forcing ``POWERTOOLS_ENABLED=true`` so ``put_metric`` takes the EMF
# branch and never buffers. It closes the network path too, but it changes
# *which* code path the unit layer exercises. The buffered path is the one every
# AgentCore container actually runs (``agentcore-stack.ts:105-135`` sets no
# ``POWERTOOLS_ENABLED``) and it is exactly what task 8.1 fixed, so switching
# branches would leave that fix untested.
#
# Autouse rather than opt-in, for the same reason ``AWS_REGION`` above is
# assigned rather than ``setdefault``ed (``LGR-3e45e65-1``, template /
# ratified): a per-file opt-in lets whichever file forgets it decide the
# session's behaviour, and the files that forgot are precisely the ones that
# leaked. A test that genuinely wants to observe a publish assigns its own
# double locally — ``metrics_facade.metrics_facade._cloudwatch = MagicMock()`` —
# which takes precedence over seam 1 because ``_get_cloudwatch()`` only builds a
# client while ``_cloudwatch is None``, and never reaches seam 2 either.
# ``tests/unit/shared/test_metrics_facade_stage_flush.py`` (task 8.3),
# ``tests/unit/lambdas/create_code/test_stage_metrics_emission.py`` (task 8.3)
# and the task 5.3 / 5.4 facade tests all do exactly that, and keep their
# assertions.
#
# The violation type derives from ``BaseException``, not ``Exception``, on
# purpose: every metric publish in this tree is best-effort and sits inside
# ``except Exception``, so an ``Exception`` here would be logged and swallowed by
# the code under test and the guard would fail silently — the exact failure mode
# it exists to stop.

_REAL_BOTO3_CLIENT = boto3.client
_REAL_MAKE_API_CALL = botocore.client.BaseClient._make_api_call

# Kept as a set so a second write operation can be added in one line.
GUARDED_CLOUDWATCH_OPERATIONS = frozenset({"PutMetricData"})


class CloudWatchGuardViolation(BaseException):
    """A unit test attempted a real CloudWatch write. Stub the client locally."""


@pytest.fixture(autouse=True)
def no_cloudwatch_writes(monkeypatch):
    """Make a real CloudWatch write unreachable from the unit layer."""

    def _guarded_boto3_client(*args, **kwargs):
        service_name = kwargs.get("service_name", args[0] if args else None)
        if service_name == "cloudwatch":
            return MagicMock(name="unit_layer_cloudwatch_stub")
        return _REAL_BOTO3_CLIENT(*args, **kwargs)

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_CLOUDWATCH_OPERATIONS:
            raise CloudWatchGuardViolation(
                f"a unit test reached a real CloudWatch {operation_name} call. Stub the client in the test "
                "(e.g. `metrics_facade.metrics_facade._cloudwatch = MagicMock()`) rather than weakening this guard "
                "in tests/unit/conftest.py."
            )
        return _REAL_MAKE_API_CALL(self, operation_name, api_params)

    monkeypatch.setattr(boto3, "client", _guarded_boto3_client)
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


@pytest.fixture
def cloudwatch_guard_violation():
    """The exception type ``no_cloudwatch_writes`` raises, for the guard's own test.

    A fixture rather than an import: ``tests/`` is not a package, so
    ``tests.unit.conftest`` is not importable by name.
    """
    return CloudWatchGuardViolation


# ── Shell harness for deploy.sh's prerequisite helpers ────────────────
# `scripts/deploy_prereq_lib.sh` is sourceable and side-effect-free, so its
# functions can be called directly instead of only through a full prerequisite
# sweep of deploy.sh. Everything below is invocation plumbing: the bash
# function under test is always the real one.
#
# This lives in conftest.py rather than a helpers module because
# test_naming_convention_prop.py allows exactly conftest.py, __init__.py,
# schemas.py and helpers.py as non-`test_*` filenames under tests/unit/.

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY_PREREQ_LIB = REPO_ROOT / "scripts" / "deploy_prereq_lib.sh"
DEPLOY_SH = REPO_ROOT / "deploy.sh"

# The teardown counterpart (p55 W1). Same shape by decision, per `LGR-b5b76ed-1`
# whose scope covers `tests/unit/conftest.py` and `tests/unit/test_deploy_*.py`:
# the pure computations live in a sourceable library and the entrypoint is a thin
# driver, so the library is what the tests call.
DESTROY_LIB = REPO_ROOT / "scripts" / "destroy_lib.sh"
DESTROY_SH = REPO_ROOT / "destroy.sh"

# The standalone AgentCore runtime sweep (p64 1b.4). A second thin driver over
# `destroy_lib.sh` rather than a third library: its pure computations live in that
# library per `LGR-b5b76ed-1`, so what this path names is the API-calling half.
# Deliberately NOT reached from destroy.sh — see `compose_agentcore_sweep_refusal`
# for why the two stay separate.
AGENTCORE_CLEANUP_SH = REPO_ROOT / "scripts" / "cleanup_agentcore_runtimes.sh"

# Resolved from the *ambient* PATH, because subprocess resolves argv[0] against
# the PATH it is handed — a hermetic fixture PATH containing only interpreter
# stubs cannot find bash itself.
_BASH = shutil.which("bash") or "/bin/bash"


def _run_bash(program: str, args: Sequence[str] = (), path: str | None = None, stdin: str | None = None):
    """Run `program` under bash, optionally with a hermetic PATH.

    `args` land as $1, $2, … ($0 is a placeholder). When `path` is given it is
    the *entire* PATH the program sees. When `stdin` is given it is the whole
    standard input the program sees, including `""` for a deliberately empty
    one; when it is None the parent's stdin is inherited, which is what the
    `deploy_prereq_lib.sh` callers have always done and none of them read.
    """
    env = os.environ.copy()
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [_BASH, "-c", program, "_", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        **({} if stdin is None else {"input": stdin}),
    )


def call_prereq(fn: str, *args: str, path: str | None = None):
    """Call one function from `scripts/deploy_prereq_lib.sh` in a fresh shell.

    The shell's exit status is the function's return code, and its stdout is
    the function's own output — the library writes nothing else.

    Exposed at module level as well as through the `call_prereq_fn` fixture:
    a Hypothesis `@given` test cannot take a function-scoped fixture, so the
    property tests import this directly.
    """
    program = f'set -uo pipefail; . "$1"; shift; {fn} "$@"'
    return _run_bash(program, args=[str(DEPLOY_PREREQ_LIB), *args], path=path)


@pytest.fixture
def call_prereq_fn():
    """Fixture form of `call_prereq` — ``(fn_name, *args, path=None)``."""
    return call_prereq


def call_destroy_lib(fn: str, *args: str, path: str | None = None, stdin: str | None = None):
    """Call one function from `scripts/destroy_lib.sh` in a fresh shell.

    A sibling of `call_prereq` rather than a parameter on it. `call_prereq` does
    not pass stdin, and `select_prefixed` *reads* stdin — so the two callers
    need different plumbing, and widening `call_prereq`'s signature would change
    the shape the existing `test_deploy_*.py` files call it with. Same contract
    otherwise: the shell's exit status is the function's return code and its
    stdout is the function's own output, because the library writes nothing else.

    `stdin=""` is meaningfully different from `stdin=None`: the first is an empty
    input list (R1.10's empty account), the second inherits the parent's stdin.
    """
    program = f'set -uo pipefail; . "$1"; shift; {fn} "$@"'
    return _run_bash(program, args=[str(DESTROY_LIB), *args], path=path, stdin=stdin)


@pytest.fixture
def call_destroy_fn():
    """Fixture form of `call_destroy_lib` — ``(fn_name, *args, path=None, stdin=None)``."""
    return call_destroy_lib


def _extract_bash_function(source: str, name: str) -> str:
    """Return the text of a top-level `name() { … }` definition."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    assert match, f"could not find a top-level {name}() definition"
    return match.group(0)


@pytest.fixture
def deploy_sh_failures():
    """Run one `deploy.sh` check function and return the messages it recorded.

    `deploy.sh` is not sourceable — it runs the whole prerequisite sweep at
    import time — so the named function's definition is lifted out of it
    verbatim and evaluated against the real library plus a stub `fail`. That
    keeps the assertion on the *behaviour* of the real function rather than on
    aggregate deploy output, which this repo has rejected once already for an
    invariant test (`LGR-b46a5bb-4`, template/ratified).

    Returns a callable ``(fn_name, path=None) -> list[str]``.
    """

    def call(fn: str, path: str | None = None) -> list[str]:
        definition = _extract_bash_function(DEPLOY_SH.read_text(), fn)
        program = "\n".join(
            [
                "set -uo pipefail",
                '. "$1"',
                "FAILURES=()",
                'fail() { FAILURES+=("$1"); }',
                "RESOLVED_PYTHON=''",
                definition,
                fn,
                'for f in ${FAILURES[@]+"${FAILURES[@]}"}; do printf "FAILURE:%s\\n" "$f"; done',
            ]
        )
        result = _run_bash(program, args=[str(DEPLOY_PREREQ_LIB)], path=path)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        return [line[len("FAILURE:") :] for line in result.stdout.splitlines() if line.startswith("FAILURE:")]

    return call


@pytest.fixture
def hermetic_python_path(tmp_path):
    """Build a PATH containing only the stub interpreters asked for.

    Returns a callable ``({"python3": "3.9.6", …}) -> path string``. Each stub
    is a three-line script standing in for the *external interpreter* — the
    same role moto plays for AWS in this suite — and only needs to answer the
    two things the probe asks: `-c <expr>` (exit status carries the version
    comparison) and `--version`. A hermetic PATH is required because a
    developer machine's real `python3` is usually already 3.12+, which makes
    R2.5's "default is 3.9" case otherwise unreproducible.
    """
    bin_dir = tmp_path / "hermetic_bin"
    bin_dir.mkdir()

    def build(interpreters: dict[str, str]) -> str:
        for name, version in interpreters.items():
            major, minor = (int(part) for part in version.split(".")[:2])
            qualifies = 0 if (major, minor) >= (3, 12) else 1
            stub = bin_dir / name
            stub.write_text(
                "#!/bin/sh\n"
                f'case "$1" in\n'
                f"  -c) exit {qualifies} ;;\n"
                f'  --version) echo "Python {version}" ;;\n'
                "  *) exit 2 ;;\n"
                "esac\n"
            )
            stub.chmod(0o755)
        return str(bin_dir)

    return build
