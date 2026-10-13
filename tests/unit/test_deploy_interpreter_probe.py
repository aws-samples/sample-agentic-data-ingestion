# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the deploy.sh Interpreter_Probe (p31 task 1.2) and shellcheck (R1.5).

The probe lives in `scripts/deploy_prereq_lib.sh` as `resolve_python_interpreter`,
and `deploy.sh:check_python` translates its failure into a user-facing message.
Both are exercised as the real shell functions, driven from pytest via the
`tests/unit/conftest.py` harness — see `context/shell-test-approach.md` (spec
task 0.4) for why this shape rather than `./deploy.sh --dry-run` on a fixture
PATH.

The interpreter stubs replace the *external interpreter*, not the code under
test: the probe gates on the interpreter's own exit status, so a stub only has
to answer `-c <expr>` and `--version`.

**Validates: Requirements 2.5, 1.5**
"""

import shutil
import subprocess

import pytest
from tests.unit.conftest import (
    AGENTCORE_CLEANUP_SH,
    DEPLOY_PREREQ_LIB,
    DEPLOY_SH,
    DESTROY_LIB,
    DESTROY_SH,
    REPO_ROOT,
)

PROBE = "resolve_python_interpreter"

# The shell surface R1.5 covers. `destroy.sh` and its library (p55 W1) are the
# teardown counterparts of `deploy.sh` and `deploy_prereq_lib.sh` and are linted
# by the same gate — `LGR-b5b76ed-1` made the deploy pair one library plus one
# thin driver, and the teardown pair mirrors that shape by decision (W1-D1).
#
# `cleanup_agentcore_runtimes.sh` (p64 1b.4) is the third driver over that same
# library and is linted here too. It is the one script in this set that deletes AI
# agent runtimes, so it is the last one that should be lint-exempt — and the
# Makefile's `shellcheck` target lists it as well, so the two forms of the gate
# agree on it.
SHELL_SOURCES = (DEPLOY_SH, DEPLOY_PREREQ_LIB, DESTROY_SH, DESTROY_LIB, AGENTCORE_CLEANUP_SH)

# Files in SHELL_SOURCES that must exist for the gate to mean anything. The
# others are linted when present: `destroy.sh` and `destroy_lib.sh` land in
# separate tasks of one wave, and a not-yet-written driver should report itself
# as absent rather than as a shellcheck finding.
REQUIRED_SHELL_SOURCES = (DEPLOY_SH, DEPLOY_PREREQ_LIB, DESTROY_LIB)


# ── R2.5 case 1: the default interpreter already qualifies ────────────
def test_resolves_default_python3_when_it_is_312(call_prereq_fn, hermetic_python_path):
    """**Validates: Requirements 2.5**

    A host whose `python3` is 3.12+ is used as-is, with no fallback probing.
    """
    path = hermetic_python_path({"python3": "3.12.3"})

    result = call_prereq_fn(PROBE, path=path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3"


# ── R2.5 case 2: 3.12 present under a non-default name (AL2023) ───────
def test_falls_back_to_python312_when_default_is_39(call_prereq_fn, hermetic_python_path):
    """**Validates: Requirements 2.5**

    Amazon Linux 2023: `dnf install python3.12` installs /usr/bin/python3.12 and
    leaves python3 at 3.9. The probe must report the fallback interpreter, so the
    caller can use it for later Python invocations (R2.2).
    """
    path = hermetic_python_path({"python3": "3.9.6", "python3.12": "3.12.3"})

    result = call_prereq_fn(PROBE, path=path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3.12"


# ── R2.5 case 3: neither candidate qualifies ──────────────────────────
def test_reports_failure_and_no_interpreter_when_none_qualifies(call_prereq_fn, hermetic_python_path):
    """**Validates: Requirements 2.5**

    With no candidate on PATH the probe returns non-zero and writes nothing to
    stdout — a caller that ignored the status must not find a usable-looking
    interpreter name in the output.
    """
    path = hermetic_python_path({})

    result = call_prereq_fn(PROBE, path=path)

    assert result.returncode != 0
    assert result.stdout == ""


def test_probe_rejects_a_too_old_interpreter_even_when_present(call_prereq_fn, hermetic_python_path):
    """**Validates: Requirements 2.5**

    The 3.9-only host is the reported blocker: `python3` exists and runs, so
    presence alone must not satisfy the probe.
    """
    path = hermetic_python_path({"python3": "3.9.6"})

    result = call_prereq_fn(PROBE, path=path)

    assert result.returncode != 0
    assert result.stdout == ""


def test_failure_message_names_both_candidates(deploy_sh_failures, hermetic_python_path):
    """**Validates: Requirements 2.5**

    R2.3 is a requirement about the *message*, so it is asserted against
    `deploy.sh:check_python`'s `fail` string rather than the library function.
    Both candidate names must appear: naming only `python3` is what sent the
    tester hand-symlinking in the first place.
    """
    path = hermetic_python_path({})

    failures = deploy_sh_failures("check_python", path=path)

    assert len(failures) == 1, failures
    message = failures[0]
    assert "python3" in message
    assert "python3.12" in message
    # What to install, not just what is missing.
    assert "3.12" in message and "install" in message.lower()


def test_failure_message_does_not_instruct_repointing_the_symlink(deploy_sh_failures, hermetic_python_path):
    """**Validates: Requirements 2.5**

    R2.3 forbids telling the user to re-point the system `python3` symlink — on
    AL2023 that link is package-managed. Asserted as the absence of an
    *instruction*, not the absence of the word: the real message deliberately
    contains a counter-warning ("do NOT re-point the python3 symlink"), so
    `"symlink" not in message` would fail against correct text.
    """
    path = hermetic_python_path({})

    message = deploy_sh_failures("check_python", path=path)[0]

    lowered = message.lower()
    for instruction in ("ln -s", "ln -sf", "update-alternatives", "alternatives --set"):
        assert instruction not in lowered, f"message instructs {instruction!r}: {message}"
    # Any mention of the symlink must be the negative form.
    if "symlink" in lowered:
        assert "do not re-point" in lowered or "do not repoint" in lowered, message


# ── R1.5: shellcheck stays clean ──────────────────────────────────────
def test_the_required_shell_sources_are_present():
    """**Validates: Requirements 1.5**

    Separate from the shellcheck run so that a moved or missing script reports
    itself as missing rather than as a clean lint. The shellcheck test below
    skips entirely when the binary is absent, which would otherwise make a
    deleted library indistinguishable from an unlinted machine.
    """
    missing = [str(path) for path in REQUIRED_SHELL_SOURCES if not path.is_file()]

    assert not missing, f"shell sources missing: {missing}"


def test_shellcheck_is_clean():
    """**Validates: Requirements 1.5**

    Runs shellcheck over `deploy.sh`, `destroy.sh` and their libraries. This is a
    skip-if-absent gate with correspondingly weaker teeth: shellcheck is a system
    binary outside the uv lockfile, so making it a hard prerequisite of
    `make check` would turn the quality gate red on any machine that has not
    installed it. `make shellcheck` exists for an explicit, unskippable run.
    Stated plainly rather than pretending this form fully enforces R1.5.
    """
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip(
            "shellcheck not installed — install it (brew install shellcheck) to enforce R1.5 locally; `make shellcheck` runs it explicitly"
        )

    targets = [str(path) for path in SHELL_SOURCES if path.is_file()]

    result = subprocess.run(
        [shellcheck, *targets],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"shellcheck findings:\n{result.stdout}{result.stderr}"
