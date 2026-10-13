# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the viewer's non-blocking severity set equals the gate's.

Two artifacts have to agree on one set of tokens and neither can import the
other:

- The **gate**, ``lambda/agentic_data_ingestion_test_code/review_verdict.py``,
  declares ``NON_BLOCKING_SEVERITIES``. It is load-bearing: rule 1 of
  ``effective_verdict`` reads it, and Blocking_Severity is its COMPLEMENT, so a
  token outside the set fails the gate.
- The **display**, ``dashboard-react/src/components/TestResultsViewer.tsx``,
  declares a mirror of the same name and uses it to choose which of four
  phrasings each rendered issue carries — "reported at non-blocking severity —
  did not fail the gate" versus "blocked the gate".

WHAT DRIFT COSTS: A DISPLAY LIE, NOT A GATE DEFECT
--------------------------------------------------
p43 design §5 risk 4. The gate keeps working whatever the viewer thinks, so
nothing fails and no run changes outcome. What breaks is the sentence a
reviewer reads. Add an ``info`` tier to the Python frozenset and not to the
TypeScript one, and the viewer labels an ``info`` finding "blocked the gate"
on a run that passed — which is the "could not check versus checked and
passed" confusion ``LGR-b2f79ef-8`` and ``LGR-b2f79ef-14`` (both **template /
ratified**) exist to remove, wearing a new costume. ``LGR-99050e6-1``
(**template / ratified**, whose anchors include ``TestResultsViewer.tsx``)
requires the state to be carried in TEXT because colour alone fails WCAG
1.4.1 — so the text is the whole of the signal here, and a wrong phrase is
the whole of the defect.

Duplicating the constant across the boundary was the deliberate choice
(design §4.5): annotating the issues on the Python side instead would change
the report structure the viewer extracts and splices, along with its nine
tests, for the same reader-visible result. The cost of that choice is one
duplicated set — and this file is the guard that was promised in exchange.

THE CONTRACT IS EQUALITY, IN BOTH DIRECTIONS
--------------------------------------------
This differs from ``test_not_verified_reason_labels_cross_language.py``, which
asserts a subset one way round on the stated ground that an unused label is
inert. Here **both** directions are live lies about the same run:

- a token in Python but not TypeScript renders "blocked the gate" over a
  finding the gate let through;
- a token in TypeScript but not Python renders "did not fail the gate" over a
  finding that failed it — a sentence contradicting the run's own outcome,
  printed beside it.

Neither is inert, so neither direction is allowed to drift.

WHY THIS PARSES THE TYPESCRIPT AND IMPORTS THE PYTHON
-----------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**): a contract test derives its
expectations from the other side's **source**, because "a test written from
the producer's output cannot detect the producer drifting, because the output
is its own oracle". So neither side's tokens are transcribed here. The
TypeScript set is read out of the component, and the Python set is imported —
not spelled ``{"minor"}`` in this file, which would make the test pass or fail
on a value rather than on agreement, and would leave both sides free to drift
together away from it.

Importing is safe on the Python side and is not the compromise it would be
elsewhere in this directory. ``review_verdict`` is pure stdlib by design
(``review_verdict.py:56-58`` — the AgentCore container shares the file with
the Lambda), so it needs no ``sys.modules`` stub dict. That is the difference
from the not-verified labels test, which reads its producers with ``ast``
precisely because importing ``lambda_function`` requires stubbing
``code_interpreter_runner`` with a ``MagicMock``, and a constant read off a
mock silently never matches.

The regex technique on the TypeScript side is this directory's existing one:
``test_approval_window_cross_language.py`` (``LGR-69ca78b-2``, **template /
ratified**) reads ``APPROVAL_TIMEOUT_SECONDS`` out of the CDK TypeScript the
same way, for the same reason — the two artifacts cannot import each other.
``api_responses/helpers.py``'s frontend-type oracle is not reusable: it parses
``export interface`` declarations, and this subject is a ``const`` bound to a
``new Set`` call.

HOW A SILENT PARSE FAILURE IS PREVENTED
---------------------------------------
A source-parsing test's whole risk is passing while having read nothing. An
empty TypeScript set would not pass here — the Python set is non-empty, so
equality fails — but "fails for the wrong reason" and "fails loudly naming the
extraction" are different diagnostics, and only the second one tells the next
reader to fix the regex rather than the constant. Four guards:

1. The component file must exist (``test_viewer_source_is_readable``), so a
   moved or renamed component fails rather than skips.
2. The declaration must match, anchored on ``const NON_BLOCKING_SEVERITIES``
   through ``new Set([`` rather than on a bare mention, so a use site cannot
   satisfy it. Not found is an assertion, not an empty return.
3. The parsed body must contain **nothing but** quoted strings, commas and
   whitespace. Strip the string literals and the residue has to be empty — so
   a spread, an identifier, a template literal or a comment inside the
   brackets fails rather than being quietly dropped from the set.
4. Every token must be lowercase and non-empty. The Verdict_Parser lowercases
   and strips severity at ``review_verdict.py:203``, so the gate only ever
   compares against a normalised token; a ``'Minor'`` in the viewer's set could
   never match one and is a defect in its own right, not just a parse worry.

MULTI-LINE: THE TEST ADAPTS, THE SHAPE STILL HAS TO HOLD
--------------------------------------------------------
The component writes the declaration on one line deliberately, so that an
anchor can find it. This test nonetheless tolerates newlines and arbitrary
whitespace **inside the brackets**, because the alternative fails on a correct
change: add a token to both sides, let prettier wrap the line past its print
width, and a line-bound regex reports drift where there is none. A test that
cries wolf on formatting is a test the next reader relaxes, and a relaxed pin
is the same as no pin.

What it does **not** tolerate is a change of construction shape — a computed
set, a spread from another module, an ``Object.freeze``, a set built from a
map's keys. Guard 3 fails on all of those, by design: at that point the
extraction genuinely cannot be trusted, and the right response is to teach it
the new shape rather than to widen it until it matches anything.

NO FIXTURES
-----------
This test reads two files off disk and imports one pure stdlib module. It
needs nothing from ``tests/fixtures/conftest.py`` — no AWS client, no moto
mock, no Lambda environment — so it takes no fixture. The root
``tests/conftest.py`` applies ``@pytest.mark.contract`` to everything under
``tests/contracts/``, so it still runs in the contracts layer of
``make test-fast``.

NOT DUPLICATED HERE
-------------------
The Python constant's own value, shape and ``RECOMMENDED_SEVERITIES`` subset
relation belong to
``tests/unit/lambdas/test_code/test_review_verdict_severity_vocabulary.py``
(p43 tests 2.4a/2.4b), and the viewer's four phrasings belong to
``TestResultsViewer.verdict.test.tsx``. This file asserts one thing only: that
the two sets are the same set.

Validates: p43 Requirements 4.3
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VIEWER_SOURCE = _PROJECT_ROOT / "dashboard-react" / "src" / "components" / "TestResultsViewer.tsx"
_GATE_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_test_code"

sys.path.insert(0, str(_GATE_DIR))

from review_verdict import NON_BLOCKING_SEVERITIES

#: The mirror's declaration, from ``const`` through the opening ``[`` to the
#: closing ``])``. Anchored on the declaration, never on a use site: ``[^=]*``
#: cannot cross an ``=``, so the match is bounded to this one statement.
#: Tolerates a TS type annotation (``: Set<string>``, ``: ReadonlySet<string>``),
#: a constructor generic (``new Set<string>(...)``) and any whitespace,
#: newlines included, inside the brackets.
_TS_SET_RE = re.compile(
    r"const\s+NON_BLOCKING_SEVERITIES\b[^=]*=\s*new\s+Set\s*(?:<[^>]*>)?\s*\(\s*\[([\s\S]*?)\]\s*\)"
)

#: One member: a single- or double-quoted string, escapes respected.
_TS_MEMBER_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")

#: A severity token as the Verdict_Parser produces it: already lowercased and
#: stripped (``review_verdict.py:203``), so the gate compares exact equality.
_SEVERITY_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _viewer_non_blocking_severities() -> set[str]:
    """The viewer's ``NON_BLOCKING_SEVERITIES`` members, read from its source.

    Asserts at every step where the source could have moved or changed shape,
    rather than returning a partial or empty set — so an unparseable component
    can never present as "an empty set of non-blocking severities".
    """
    source = _VIEWER_SOURCE.read_text(encoding="utf-8")

    declaration = _TS_SET_RE.search(source)
    assert declaration is not None, (
        "the `const NON_BLOCKING_SEVERITIES = new Set([...])` declaration was not found in "
        "dashboard-react/src/components/TestResultsViewer.tsx. Either it moved or was renamed, "
        "or its construction shape changed to something this extraction cannot read (a computed "
        "set, a spread, an Object.freeze, keys off a map). Teach the extraction the new shape "
        "rather than deleting this test: without it, nothing at all holds the viewer's "
        "blocking/non-blocking labels to the gate that decides them, and drift shows up only as "
        "a wrong sentence in front of a reviewer (p43 design §5 risk 4)."
    )
    body = declaration.group(1)

    members = [single or double for single, double in _TS_MEMBER_RE.findall(body)]

    # Guard: nothing in the brackets except the string literals we just took.
    # A spread, an identifier, a template literal or a comment would otherwise
    # be dropped from the set without failing anything.
    residue = _TS_MEMBER_RE.sub("", body).replace(",", "").strip()
    assert not residue, (
        f"the NON_BLOCKING_SEVERITIES set literal contains something this test cannot read: "
        f"{residue!r}. Every member has to be a plain quoted string, or the parsed set is "
        "incomplete and the comparison below is against the wrong thing."
    )

    assert members, (
        "NON_BLOCKING_SEVERITIES parsed to ZERO members in TestResultsViewer.tsx. The "
        "declaration matched but no quoted token did, so the set shape changed under the "
        "extraction. Fix it here — an empty parse must never read as agreement."
    )

    for token in members:
        assert _SEVERITY_TOKEN_RE.match(token), (
            f"the viewer's non-blocking set carries {token!r}, which is not the shape a parsed "
            "severity has. review_verdict.py:203 lowercases and strips severity before the gate "
            "ever sees it, so a token with uppercase, whitespace or punctuation can never match "
            "one — the entry would be dead and every issue at that severity would be labelled "
            "'blocked the gate' regardless of what the gate did."
        )

    return set(members)


def test_viewer_source_is_readable():
    """Guard: a moved or renamed component must fail loudly, not skip."""
    assert _VIEWER_SOURCE.is_file(), f"dashboard source not found at {_VIEWER_SOURCE}"


def test_the_gate_constant_is_importable_and_non_empty():
    """Anti-vacuity on the Python side, and the reason it can be imported at all.

    Deliberately does NOT assert the value: pinning ``{"minor"}`` here would
    make this file pass or fail on a token rather than on agreement, and would
    let both sides drift together away from it. The value, the ``frozenset``
    shape and the ``RECOMMENDED_SEVERITIES`` subset relation belong to p43
    tests 2.4a/2.4b in
    ``tests/unit/lambdas/test_code/test_review_verdict_severity_vocabulary.py``.
    """
    assert NON_BLOCKING_SEVERITIES, (
        "review_verdict.NON_BLOCKING_SEVERITIES is empty. Every severity would then be a "
        "Blocking_Severity by the complement encoding, and the equality below would be "
        "asserting that two empty sets agree — which is true and means nothing."
    )
    assert isinstance(NON_BLOCKING_SEVERITIES, frozenset | set), (
        f"NON_BLOCKING_SEVERITIES is a {type(NON_BLOCKING_SEVERITIES).__name__}; this test "
        "compares it as a set of tokens."
    )


def test_viewer_set_parse_is_neither_empty_nor_partial():
    """Anti-vacuity on the TypeScript side: every extraction guard, run alone.

    So a degraded parse is reported as *this* test failing, naming the
    extraction, rather than as the contract test below reporting drift that
    does not exist.
    """
    parsed = _viewer_non_blocking_severities()
    assert parsed, "the viewer's non-blocking severity set parsed to nothing."


def test_viewer_non_blocking_severities_equal_the_gate_constant():
    """The viewer's set == ``review_verdict.NON_BLOCKING_SEVERITIES``, both ways.

    Validates: p43 Requirements 4.3
    """
    viewer = _viewer_non_blocking_severities()
    gate = set(NON_BLOCKING_SEVERITIES)

    gate_only = gate - viewer
    viewer_only = viewer - gate

    assert not gate_only, (
        f"the gate treats {sorted(gate_only)} as non-blocking and the viewer does not, so "
        "TestResultsViewer.tsx labels a finding at that severity 'blocked the gate' on a run the "
        "gate passed. Add the token to the `NON_BLOCKING_SEVERITIES` set in "
        "dashboard-react/src/components/TestResultsViewer.tsx. Do NOT resolve this by removing "
        "the token from review_verdict.py unless you mean to change the gate: that is a "
        "calibration change, and this is a display mirror (p43 design §4.5)."
    )
    assert not viewer_only, (
        f"the viewer treats {sorted(viewer_only)} as non-blocking and the gate does not, so "
        "TestResultsViewer.tsx prints 'did not fail the gate' beside a finding that failed it — "
        "a sentence contradicting the run's own outcome, on the same screen as the outcome. "
        "review_verdict.NON_BLOCKING_SEVERITIES is the source of truth; the viewer's set mirrors "
        "it and never leads it."
    )

    assert viewer == gate, (
        f"the viewer's non-blocking severity set {sorted(viewer)} differs from the gate's {sorted(gate)}."
    )
