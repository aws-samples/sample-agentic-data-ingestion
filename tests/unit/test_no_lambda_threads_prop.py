# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: no Frozen_Thread_Antipattern in the Lambda source tree.

Feature: p30a-agent-activation, Property: no ``threading.Thread`` in ``lambda/**``

For any Python source file under ``lambda/``, no ``threading.Thread`` is
instantiated, referenced, or subclassed. A Lambda handler that starts a thread
and returns loses the work: Lambda freezes the execution environment the moment
the handler returns (design §3 C2).

**Why forbid it rather than just fix the two known sites?** Because the pattern
is *non-deterministically* broken, which is worse than reliably broken. Design
§5 O7 records 2 ``data_quality`` state rows produced by the frozen-thread path
in `handlers/agents.py` — a warm container occasionally resumed the thread long
enough for it to finish. Work that succeeds sometimes cannot be tested and
cannot be relied on, and it will not show up as a failure in review.

The replacement is a real async invoke: ``lambda_client.invoke(...,
InvocationType="Event")`` against the generic dispatch Lambda
(``agentic_data_ingestion_agent_dispatch``), which owns the long synchronous
AgentCore call inside its own 15-minute timeout.
``job_completion`` → ``SELF_HEALING_FN`` is the working reference shape.

Scope — deliberate, and narrower than "everywhere":

* This test scans **the ``lambda/`` source tree only**, which is what R2.4
  says. ``shared/`` is *also* copied into the Lambda layer at deploy time, so
  the scope is a statement about source location, not about what ends up in a
  Lambda package.
* ``shared/`` is excluded on purpose, and there is a real thread there:
  ``shared/sfn_task_token.py:242`` (``dispatch_background_step``) starts a
  thread whose lifetime is managed by the AgentCore runtime's
  ``add_async_task`` / ``complete_async_task`` pair — that flips ``/ping`` to
  ``HealthyBusy`` and keeps the microVM scheduled while the thread runs. It is
  called from the AgentCore container entrypoints, not from a Lambda handler,
  so the freeze-on-return hazard does not apply. Widening this scan to
  ``shared/`` would flag it, which is why the scan is scoped rather than
  allowlisted.

Detection is AST-based, not substring-based. That is load-bearing: the module
docstring of `handlers/agents.py` names ``threading.Thread`` on purpose, to
record why it must not come back. A check for the bare word would forbid
documenting the fence. Comments and docstrings are invisible to the AST, so
they are excluded structurally rather than by luck.

There is **no allowlist**. A legitimate in-Lambda thread use is a finding to
report, not something to exempt here.

**Validates: Requirements 2.4**
"""

import ast
import pathlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
LAMBDA_ROOT = PROJECT_ROOT / "lambda"


def _lambda_python_files() -> list[pathlib.Path]:
    """Every Python source file in the ``lambda/`` tree, ``__pycache__`` aside."""
    return sorted(p for p in LAMBDA_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


LAMBDA_PY_FILES = _lambda_python_files()

REMEDIATION = (
    "Lambda freezes the execution environment when the handler returns, so a "
    "thread doing post-response work is killed mid-call and the work is "
    "silently lost (sometimes — a warm container may let it finish, which is "
    "why this is a guard and not a style rule). Use a real async invoke "
    "instead: lambda_client.invoke(FunctionName=<AGENT_DISPATCH_FN>, "
    'InvocationType="Event", ...) against the dispatch Lambda '
    "agentic_data_ingestion_agent_dispatch, which owns the long synchronous "
    "call inside its own 15-minute timeout. See "
    "lambda/agentic_data_ingestion_job_completion -> SELF_HEALING_FN for the "
    "reference shape."
)


# ---------------------------------------------------------------------------
# AST detector
# ---------------------------------------------------------------------------


def _threading_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (names bound to the ``threading`` module, names bound to ``Thread``).

    Handles ``import threading``, ``import threading as t``,
    ``from threading import Thread`` and ``from threading import Thread as T``.
    """
    module_aliases: set[str] = set()
    thread_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "threading":
                    module_aliases.add(alias.asname or "threading")
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            for alias in node.names:
                if alias.name == "Thread":
                    thread_aliases.add(alias.asname or "Thread")
                elif alias.name == "*":
                    # ``from threading import *`` puts Thread in scope under
                    # its own name without a binding node to key off.
                    thread_aliases.add("Thread")

    return module_aliases, thread_aliases


def find_thread_uses(source: str) -> list[tuple[int, str]]:
    """Find every ``threading.Thread`` use in ``source``.

    Returns ``(line_number, description)`` pairs. Detects attribute access on
    the imported module (``threading.Thread``, incl. under an alias), a
    ``Thread`` name imported directly, and subclassing either form. Bare
    references count, not just calls: ``worker = threading.Thread`` followed by
    ``worker(...)`` is the same pattern with an extra hop.

    Docstrings and comments are not part of the AST, so prose naming the
    antipattern — which `handlers/agents.py` does deliberately — is invisible
    here.
    """
    tree = ast.parse(source)
    module_aliases, thread_aliases = _threading_bindings(tree)

    # Names introduced by the import statements themselves are bindings, not
    # uses; ast.Import/ImportFrom hold alias nodes, not Name nodes, so they
    # never reach the walk below.
    uses: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "Thread":
            if isinstance(node.value, ast.Name) and node.value.id in module_aliases:
                uses.append((node.lineno, f"{node.value.id}.Thread"))
        elif isinstance(node, ast.Name) and node.id in thread_aliases:
            uses.append((node.lineno, node.id))

    return sorted(set(uses))


def _violations_for(path: pathlib.Path) -> list[str]:
    """Formatted ``file:line: use`` strings for one file."""
    source = path.read_text()
    rel = path.relative_to(PROJECT_ROOT)
    return [f"{rel}:{lineno}: {desc}" for lineno, desc in find_thread_uses(source)]


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def test_lambda_source_scan_is_not_vacuous():
    """A guard test that scans nothing passes for the wrong reason.

    **Validates: Requirements 2.4**
    """
    assert LAMBDA_ROOT.is_dir(), f"Lambda source tree not found at {LAMBDA_ROOT}"
    assert len(LAMBDA_PY_FILES) > 20, (
        f"Expected the lambda/ tree to hold dozens of Python files; found {len(LAMBDA_PY_FILES)}. "
        "The scan below would pass vacuously — fix the path before trusting it."
    )


def test_no_threading_thread_anywhere_in_lambda_tree():
    """R2.4: no ``threading.Thread`` remains anywhere under ``lambda/``.

    Exhaustive over the tree, so a new Lambda is covered the moment it is
    added — nothing has to be registered here.

    **Validates: Requirements 2.4**
    """
    violations: list[str] = []
    for path in LAMBDA_PY_FILES:
        violations.extend(_violations_for(path))

    assert not violations, (
        "Frozen_Thread_Antipattern found in the Lambda source tree:\n  "
        + "\n  ".join(violations)
        + "\n\n"
        + REMEDIATION
    )


@given(path=st.sampled_from(LAMBDA_PY_FILES))
@settings(max_examples=len(LAMBDA_PY_FILES), deadline=None)
def test_no_lambda_module_starts_a_thread(path: pathlib.Path):
    """R2.4, per module: any file drawn from ``lambda/`` starts no thread.

    **Validates: Requirements 2.4**
    """
    violations = _violations_for(path)
    assert not violations, "Frozen_Thread_Antipattern in " + str(violations) + "\n\n" + REMEDIATION


def test_every_lambda_file_parses():
    """The detector is only as good as its ability to parse the tree.

    An unparseable file would silently contribute zero violations, so fail
    loudly instead.

    **Validates: Requirements 2.4**
    """
    unparseable: list[str] = []
    for path in LAMBDA_PY_FILES:
        try:
            ast.parse(path.read_text())
        except SyntaxError as exc:
            unparseable.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")

    assert not unparseable, "Files under lambda/ that the thread scan could not parse: " + "; ".join(unparseable)


# ---------------------------------------------------------------------------
# Negative control — a guard test that cannot fail is worthless
# ---------------------------------------------------------------------------

VIOLATING_SOURCES = [
    # The exact shape p30a removed from handlers/agents.py.
    "import threading\n\n\ndef h(event, context):\n    threading.Thread(target=work, daemon=True).start()\n    return 202\n",
    # Aliased module import.
    "import threading as t\n\n\ndef h(e, c):\n    t.Thread(target=work).start()\n",
    # Direct name import.
    "from threading import Thread\n\n\ndef h(e, c):\n    Thread(target=work).start()\n",
    # Direct name import under an alias.
    "from threading import Thread as T\n\n\ndef h(e, c):\n    T(target=work).start()\n",
    # Held as a reference, called later.
    "import threading\n\nworker = threading.Thread\n\n\ndef h(e, c):\n    worker(target=work).start()\n",
    # Subclassing is the same hazard.
    "import threading\n\n\nclass Worker(threading.Thread):\n    def run(self):\n        work()\n",
]


@pytest.mark.parametrize("source", VIOLATING_SOURCES)
def test_detector_catches_violating_source(source: str):
    """The detector flags every spelling of the antipattern it claims to.

    **Validates: Requirements 2.4**
    """
    assert find_thread_uses(source), f"Detector missed a threading.Thread use in:\n{source}"


def test_detector_ignores_prose_naming_the_antipattern():
    """Documenting the fence is not violating it.

    Task 1.5's first attempt at this assertion was a substring check and it
    failed on `handlers/agents.py`, whose module docstring names
    ``threading.Thread`` to record why it must not come back. AST analysis
    excludes docstrings and comments structurally.

    **Validates: Requirements 2.4**
    """
    source = (
        '"""Do NOT reintroduce a threading.Thread here — Lambda freezes on return."""\n'
        "\n"
        "# threading.Thread(target=work).start() was the old shape.\n"
        "\n"
        "\n"
        "def h(event, context):\n"
        '    return {"statusCode": 202}\n'
    )
    assert find_thread_uses(source) == []


def test_real_agents_handler_documents_the_fence_without_tripping_the_check():
    """The live instance of the case above, pinned against the real file.

    **Validates: Requirements 2.4**
    """
    agents_py = LAMBDA_ROOT / "agentic_data_ingestion_dashboard_api" / "handlers" / "agents.py"
    source = agents_py.read_text()

    assert "threading.Thread" in source, (
        "handlers/agents.py no longer documents why the Frozen_Thread_Antipattern must not "
        "return. That fence is the reason this test is AST-based; if the prose is gone, this "
        "assertion has stopped guarding anything — restore the docstring or delete this test "
        "deliberately."
    )
    assert find_thread_uses(source) == []
