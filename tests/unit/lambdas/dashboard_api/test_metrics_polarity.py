# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structural test — no re-introduced backend inversion (``handlers/metrics.py``).

Feature: p39-metrics-truth, task 2.3.

Task 2.1 deleted the ``invert`` parameter from ``_compute_trend`` and the two
``invert=True`` call sites. This file is the guard that makes a re-introduction
**fail a test rather than a screen** (``R9.5``). Semantic polarity is the
frontend's, keyed on the trend field name and stated in text as well as in
colour (``MetricsDashboardPage.tsx:42-71``, ``LGR-99050e6-1``, template /
ratified) — that half is correct and is deliberately untouched here.

Three assertions, and the reason each is shaped the way it is:

1. **The signature carries no inversion parameter.**
   ``inspect.signature(_compute_trend)`` is inspected for a parameter whose
   *name* reads as a polarity switch, whose *default* is a boolean, or whose
   *annotation* is ``bool``. It deliberately does **not** assert an exact
   parameter list: p39 task 3.2 widens ``previous`` to ``float | None``
   (``R4.4``), and a guard that pinned the current annotations would go red on
   a change this spec has already scheduled. What is asserted is the absence of
   an inversion knob, which is what ``R3.5`` actually says.

2. **A rise reports ``up`` and a fall reports ``down``.** The behavioural half.
   Without it, an inversion moved out of the signature and into the body would
   satisfy assertion 1 — the direction would flip and no structural check would
   notice. Both directions are asserted because a body that returned a constant
   would pass a rise-only check. The magnitudes used are well outside the 5%
   stability band, and a *measured* positive baseline behaves the same after
   task 3.2, so this survives that change too.

3. **``metrics.py`` declares no polarity table of its own** (``R3.2``). The
   shape being forbidden is the frontend's, read from
   ``MetricsDashboardPage.tsx:42-52``: ``TREND_POLARITY``, a map from a trend
   field name to ``'higher-is-better'`` / ``'lower-is-better'``, plus
   ``POLARITY_CAPTION`` keyed on that vocabulary. The Python equivalent would
   be a module-level dict, set or tuple carrying those tokens, or any binding
   named for polarity or inversion.

**Why the detection is AST-based, and why that is load-bearing here.**
``_compute_trend``'s docstring names the removed flag on purpose
(``metrics.py:885-887``: "previously took an ``invert`` flag … Do not
reintroduce it") and uses the word "polarity" twice more at ``:882``. A
substring guard over the file source would fail on the fence that exists to
stop the fence being removed — the same trap ``LGR-b46a5bb-4`` (template /
ratified) records for the ``threading.Thread`` scan, where substring matching
"forbids documenting the fence, and failed for exactly that reason". Comments
and docstrings are not part of the AST, so prose is excluded *structurally*
rather than by luck: the detectors below look at parameter names, bound names,
container literals and non-docstring string constants.

Deliberately **not** asserted here:

* **The wording of that docstring.** Pinning prose would make a later p39 task
  that rewrites the docstring (3.2 revisits this function) go red for a reason
  that has nothing to do with polarity. Non-vacuity is established by the
  synthetic negative controls at the bottom instead: the detectors are shown to
  flag every spelling of a polarity table they claim to, and to ignore prose
  naming one.
* **Argument order at the call sites.** ``_compute_trend(previous, current)``
  would invert by swapping, and no structural check can see that without
  semantics. The call-site guard below bounds the argument *count* and forbids
  an inversion *keyword*; the swap case is covered behaviourally by task 2.2's
  Property 2 and by task 20's seam test, which read direction out of the
  assembled payload.
* **Frontend polarity.** ``TREND_POLARITY`` / ``POLARITY_CAPTION`` /
  ``trendColor`` stay exactly as they are (``LGR-99050e6-1``; requirements
  constraint 1). This file makes no claim about them beyond quoting their shape.

**Validates: Requirements 3.2, 3.5, 9.5**
"""

import ast
import inspect
import os
import pathlib
import re
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason the sibling
# test_metrics_stage_durations_char.py repeats it — under xdist --dist=loadfile
# any file in this directory may be the first one imported.
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# handlers.common resolves its table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py). AWS_REGION is deliberately NOT set here:
# the unit layer pins it by assignment in tests/unit/conftest.py
# (LGR-3e45e65-1, template / ratified).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

METRICS_PATH = pathlib.Path(inspect.getfile(metrics_mod))
METRICS_SOURCE = METRICS_PATH.read_text()

TREND_FUNCTION = "_compute_trend"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: A *name* that reads as a polarity or inversion switch — a parameter, a
#: variable, a function, a class. Broader than the value vocabulary below
#: because a name is cheap to match and a false positive here is a name worth
#: renaming anyway.
_INVERSION_NAME_RE = re.compile(
    r"invert|inverted|inversion|inverse|flip|negate|polarit|is[_-]?better|is[_-]?good|good[_-]?direction",
    re.IGNORECASE,
)

#: A *string value* that only a polarity table would carry. Narrower on
#: purpose: "invert" is excluded so that a future log message or error string
#: explaining that no inversion is applied does not trip the guard, while the
#: two tokens the frontend's TREND_POLARITY actually stores do.
_POLARITY_VALUE_RE = re.compile(
    r"higher[ _-]?is[ _-]?better|lower[ _-]?is[ _-]?better|polarit",
    re.IGNORECASE,
)

REMEDIATION = (
    "Semantic polarity belongs to the frontend, keyed on the trend field name and stated in text as "
    "well as in colour (MetricsDashboardPage.tsx TREND_POLARITY / POLARITY_CAPTION / trendColor, "
    "LGR-99050e6-1). The backend reports the direction of the raw change and nothing else (p39 R3.1, "
    "R3.2, R3.5). A backend inversion composed with the frontend's re-encoding once produced a trend "
    "that was wrong in both label and colour — that is the defect p39 task 2.1 removed."
)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def inversion_parameters(func) -> list[str]:
    """Parameters of ``func`` that look like an inversion switch.

    Three shapes, none of which depends on the current parameter list:

    * a name matching :data:`_INVERSION_NAME_RE` (``invert``, ``polarity``, …);
    * a boolean default — the old flag was ``invert: bool = False``, and a
      re-introduction would need a default to keep the four existing call sites
      working;
    * a ``bool`` annotation, whether evaluated or stringised (the module does
      not use ``from __future__ import annotations`` today; handling both means
      this does not quietly stop working if it starts).
    """
    flagged: list[str] = []
    for name, param in inspect.signature(func).parameters.items():
        if _INVERSION_NAME_RE.search(name):
            flagged.append(f"{name!r}: parameter name reads as a polarity/inversion switch")
            continue
        if isinstance(param.default, bool):
            flagged.append(f"{name!r}: boolean default {param.default!r} — a flag, and flags get passed")
            continue
        annotation = param.annotation
        stringised = annotation.strip() if isinstance(annotation, str) else ""
        if annotation is bool or stringised == "bool":
            flagged.append(f"{name!r}: annotated bool")
    return flagged


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Node ids of every bare string expression — docstrings and prose blocks.

    A docstring is a string *statement*, so it is reachable as
    ``Expr(Constant(str))``. Collecting all of them (rather than only the first
    statement of a module/def/class) also excludes a free-standing string used
    as a section comment, which is prose by any reading. Comments never reach
    the AST at all.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }


def _assigned_names(node: ast.AST) -> list[str]:
    """Every plain name a statement binds. Attributes and subscripts skipped."""
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]

    names: list[str] = []
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                names.append(sub.id)
    return names


def polarity_table_violations(source: str) -> list[str]:
    """Every polarity-table shape in ``source``, as ``line: description``.

    Flags, in the shape ``MetricsDashboardPage.tsx:42-52`` uses:

    * a binding — variable, function, class — whose name reads as polarity or
      inversion (``TREND_POLARITY``, ``_INVERTED_TRENDS``, ``_invert_trend``);
    * a string constant outside a docstring carrying a polarity token, which
      covers both a table's values and a bare ``return "lower-is-better"``.

    Prose is invisible here by construction: docstrings are excluded by node
    identity and comments are not in the AST. That is the whole reason this is
    an AST walk — ``metrics.py``'s own docstring at ``:882-887`` names both
    "polarity" and "invert" as a fence, and a substring check would fail on it
    (``LGR-b46a5bb-4``, template / ratified).
    """
    tree = ast.parse(source)
    docstring_ids = _docstring_constant_ids(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            for name in _assigned_names(node):
                if _INVERSION_NAME_RE.search(name):
                    violations.append(f"{node.lineno}: binding {name!r} reads as a polarity/inversion table")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if _INVERSION_NAME_RE.search(node.name):
                violations.append(f"{node.lineno}: {node.name!r} is defined as a polarity/inversion helper")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
            if _POLARITY_VALUE_RE.search(node.value):
                violations.append(f"{node.lineno}: string {node.value!r} is polarity vocabulary in code")

    return sorted(set(violations))


def trend_call_sites(source: str) -> list[tuple[int, int, list[str], bool]]:
    """Every ``_compute_trend(...)`` call: ``(line, n_args, keywords, starred)``.

    ``starred`` covers ``*args`` / ``**kwargs`` forwarding, which would let an
    inversion argument through without naming it at the call site.
    """
    calls: list[tuple[int, int, list[str], bool]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name != TREND_FUNCTION:
            continue
        starred = any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords)
        keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
        calls.append((node.lineno, len(node.args), keywords, starred))
    return calls


# ---------------------------------------------------------------------------
# 1. The signature carries no inversion parameter (R3.5)
# ---------------------------------------------------------------------------


def test_compute_trend_signature_has_no_inversion_parameter():
    """R3.5: no parameter permits a caller to invert a computed direction.

    Asserts the *absence* of an inversion knob rather than an exact parameter
    list, so p39 task 3.2's widening of ``previous`` to ``float | None`` leaves
    this green.

    **Validates: Requirements 3.5**
    """
    flagged = inversion_parameters(metrics_mod._compute_trend)

    assert not flagged, (
        f"{TREND_FUNCTION} has regained an inversion parameter:\n  " + "\n  ".join(flagged) + "\n\n" + REMEDIATION
    )


def test_compute_trend_call_sites_pass_no_inversion_argument():
    """R3.5, at the call sites: two values in, nothing else.

    The old shape was ``_compute_trend(current, previous, invert=True)`` at two
    of the four sites. Argument *count* is bounded and an inversion keyword is
    forbidden; ``*args`` / ``**kwargs`` forwarding is refused because it would
    smuggle a third argument past both checks.

    **Validates: Requirements 3.5**
    """
    calls = trend_call_sites(METRICS_SOURCE)

    assert calls, (
        f"No {TREND_FUNCTION} call site found in {METRICS_PATH.name}. This guard would pass "
        "vacuously — the function has been renamed or the trends are no longer computed there."
    )

    violations: list[str] = []
    for lineno, n_args, keywords, starred in calls:
        if starred:
            violations.append(f"{lineno}: forwards *args/**kwargs, so an inversion argument is unbounded")
        if n_args + len(keywords) > 2:
            violations.append(f"{lineno}: {n_args + len(keywords)} arguments — expected the two values only")
        for keyword in keywords:
            if _INVERSION_NAME_RE.search(keyword):
                violations.append(f"{lineno}: keyword {keyword!r} reads as a polarity/inversion switch")

    assert not violations, (
        f"{TREND_FUNCTION} call sites in {METRICS_PATH.name} pass more than the two values:\n  "
        + "\n  ".join(violations)
        + "\n\n"
        + REMEDIATION
    )


# ---------------------------------------------------------------------------
# 2. The behavioural half — a rise reports up (R3.1, R3.3 via R3.5)
# ---------------------------------------------------------------------------


def test_a_rise_reports_up():
    """A doubling is ``up``. An inversion inside the body fails here.

    This is the assertion a signature-only guard cannot make: polarity moved
    from the parameter list into the function body would leave assertion 1
    green and flip this one.

    **Validates: Requirements 3.5, 9.5**
    """
    assert metrics_mod._compute_trend(200.0, 100.0) == "up", REMEDIATION


def test_a_fall_reports_down():
    """A halving is ``down`` — so a constant return cannot satisfy the rise case.

    **Validates: Requirements 3.5, 9.5**
    """
    assert metrics_mod._compute_trend(100.0, 200.0) == "down", REMEDIATION


# ---------------------------------------------------------------------------
# 3. metrics.py declares no polarity table of its own (R3.2)
# ---------------------------------------------------------------------------


def test_metrics_module_declares_no_polarity_table():
    """R3.2: the dashboard is the only component applying semantic polarity.

    **Validates: Requirements 3.2**
    """
    violations = polarity_table_violations(METRICS_SOURCE)

    assert not violations, (
        f"Polarity vocabulary declared in {METRICS_PATH.name}:\n  " + "\n  ".join(violations) + "\n\n" + REMEDIATION
    )


def test_the_scanned_module_is_the_real_handler():
    """A guard that scanned the wrong file, or an empty one, passes for free.

    **Validates: Requirements 3.2**
    """
    assert METRICS_PATH.parts[-3:] == ("agentic_data_ingestion_dashboard_api", "handlers", "metrics.py"), (
        f"Expected to scan the dashboard API metrics handler; got {METRICS_PATH}"
    )
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == TREND_FUNCTION
        for node in ast.walk(ast.parse(METRICS_SOURCE))
    ), f"{TREND_FUNCTION} is not defined in {METRICS_PATH} — the scan above is not looking at the trend calculator."


# ---------------------------------------------------------------------------
# Negative controls — a guard that cannot fail is worthless
# ---------------------------------------------------------------------------

#: Every spelling of a backend polarity table this file claims to catch. The
#: first is the frontend's own shape (MetricsDashboardPage.tsx:42-52)
#: transliterated into Python, which is the literal thing R3.2 forbids.
POLARITY_TABLE_SOURCES = [
    '_TREND_POLARITY = {\n    "ingestion": "higher-is-better",\n    "cost": "lower-is-better",\n}\n',
    '_POLARITY_CAPTION = {"lower-is-better": "Lower is better"}\n',
    '_TREND_SIGNAL = {"cost": "lower_is_better"}\n',
    '_INVERTED_TRENDS = {"cost", "agent_latency"}\n',
    '_INVERT_TRENDS = ("cost",)\n',
    'def _invert_trend(direction: str) -> str:\n    return "down" if direction == "up" else "up"\n',
    'def _trend_polarity(field: str) -> str:\n    return "lower-is-better"\n',
    'class TrendPolarity:\n    """Polarity of a trend."""\n',
    'def _signal(field, direction):\n    return "lower-is-better" if field == "cost" else "higher-is-better"\n',
]


@pytest.mark.parametrize("source", POLARITY_TABLE_SOURCES)
def test_detector_flags_a_backend_polarity_table(source: str):
    """The detector catches every polarity shape it claims to.

    **Validates: Requirements 3.2**
    """
    assert polarity_table_violations(source), f"Detector missed a polarity table in:\n{source}"


def test_detector_ignores_prose_naming_the_removed_flag():
    """Documenting the fence is not violating it.

    ``_compute_trend``'s docstring names the deleted ``invert`` flag on purpose,
    and says "Do not reintroduce it" (``metrics.py:885-887``). A substring guard
    would fail on exactly that — the trap ``LGR-b46a5bb-4`` records for the
    ``threading.Thread`` scan. Docstrings and comments are excluded
    structurally.

    **Validates: Requirements 3.2**
    """
    source = (
        '"""This module applies no polarity transformation.\n'
        "\n"
        "``_compute_trend`` previously took an ``invert`` flag; a rise in cost was\n"
        "reported as ``down`` and then re-inverted on display. Do not reintroduce it.\n"
        '"""\n'
        "\n"
        "# The old shape was: _compute_trend(current, previous, invert=True), and the\n"
        '# frontend owns polarity ("higher-is-better" / "lower-is-better").\n'
        "\n"
        "\n"
        "def _compute_trend(current: float, previous: float) -> str:\n"
        '    """Return the direction of the raw change — polarity is not applied here."""\n'
        '    return "up" if current > previous else "down"\n'
    )
    assert polarity_table_violations(source) == []
    assert (
        inversion_parameters(
            # A signature identical to the real one, checked through the same detector.
            lambda current, previous: "up"
        )
        == []
    )


@pytest.mark.parametrize(
    "func",
    [
        lambda current, previous, invert=False: "up",
        lambda current, previous, inverted=False: "up",
        lambda current, previous, polarity="lower-is-better": "up",
        lambda current, previous, lower_is_better=True: "up",
        lambda current, previous, negate=False: "up",
    ],
)
def test_detector_flags_an_inversion_parameter(func):
    """The signature detector catches a flag by name, by default, or by type.

    **Validates: Requirements 3.5**
    """
    assert inversion_parameters(func), f"Detector missed an inversion parameter in {inspect.signature(func)}"


@pytest.mark.parametrize(
    "source",
    [
        "x = _compute_trend(a, b, invert=True)\n",
        "x = _compute_trend(a, b, True)\n",
        "x = _compute_trend(*args)\n",
        "x = _compute_trend(a, b, **opts)\n",
    ],
)
def test_call_site_detector_flags_a_third_argument(source: str):
    """The call-site guard catches the shape task 2.1 deleted.

    **Validates: Requirements 3.5**
    """
    calls = trend_call_sites(source)
    assert calls, f"Detector found no call site in:\n{source}"
    _lineno, n_args, keywords, starred = calls[0]
    flagged = starred or n_args + len(keywords) > 2 or any(_INVERSION_NAME_RE.search(k) for k in keywords)
    assert flagged, f"Detector missed a third argument in:\n{source}"
