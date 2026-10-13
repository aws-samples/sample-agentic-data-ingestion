# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 2.1 — the severity vocabulary constants and the gate's use of them.

What this file owns
-------------------
The Python side of the severity vocabulary, and nothing else. Four pins:

* **2.4a** ``NON_BLOCKING_SEVERITIES`` exists, is a ``frozenset``, equals
  ``{"minor"}`` (R2.4).
* **2.4b** it is a subset of ``RECOMMENDED_SEVERITIES``, and
  ``effective_verdict`` reads the constant with **no bare severity literal** in
  its body (R2.4).
* **2.5** the module states, at the two constants, both facts about a novel
  token — it cannot fail the STEP, and it does block the GATE — as two
  statements rather than one (R2.5).
* **2.6** ``RECOMMENDED_SEVERITIES`` still names the three tokens the
  Reviewer_Prompt asks for (R2.6).

Two neighbouring files deliberately do NOT cover this ground.
``tests/contracts/test_non_blocking_severities_cross_language.py`` says so in
its own words ("NOT DUPLICATED HERE"): it asserts only that the TypeScript
mirror in ``TestResultsViewer.tsx`` is the same set, and defers the Python
constant's value, shape and subset relation here.
``test_reviewer_prompt_severity_vocabulary.py`` asserts the prompt.

The 2.6 overlap, stated rather than left to be noticed
-----------------------------------------------------
``test_reviewer_prompt_severity_vocabulary.py::test_recommended_severities_is_the_vocabulary_the_prompts_enumerate``
already asserts ``RECOMMENDED_SEVERITIES == ("critical", "major", "minor")``.
This file asserts it again, and that duplication is deliberate:

* **Different subjects.** There the tuple is the *oracle* for a claim about the
  two ``.j2`` templates — it is how that file avoids hard-coding a literal
  triple in a prompt pin. Here the tuple is the *subject*: R2.6 is a claim about
  the constant, and it is the only acceptance criterion that file's control does
  not exist to serve. Delete or rewrite the prompt file and R2.6 would lose its
  only executable form.
* **This one also pins the container type.** ``tuple``, not ``frozenset`` — the
  house pattern where the container is itself the signal (design §4.3): a
  ``frozenset`` is load-bearing membership vocabulary (``ALLOWED_VERDICTS``,
  ``ALLOWED_ATTRIBUTIONS``, ``NON_BLOCKING_SEVERITIES``), a ``tuple`` is
  documentation or ordered display (``RECOMMENDED_SEVERITIES``,
  ``REVIEW_CATEGORIES``). The prompt file's control does not assert that, and
  the subset assertion in 2.4b would still pass if the tuple silently became a
  set.

One redundant equality assertion is cheaper than a criterion with no home.

Why 2.4b excludes the docstring, and what "no bare literal" means precisely
--------------------------------------------------------------------------
The naive shape — ``"minor" not in inspect.getsource(effective_verdict)`` —
fails today, and not because of anything wrong. It fails on the function's own
**docstring**, which design §4.1 requires to describe the floor and which
therefore says ``A ``minor`` finding is retained on the verdict and displayed
but does not fire this rule``. Forbidding that would forbid the documentation
the same spec asks for. (The constant's own name is not the trap: it contains
no substring ``minor``.)

So the assertion is built on the ``ast`` module rather than on substrings, and
excludes three things on purpose:

* **the docstring** — prose about the floor is exactly what R2.4's sibling
  criteria want written down;
* **comments** — the AST drops them, so the rule-1 comment naming
  Blocking_Severity is free to say so;
* **the constant reference itself** — ``NON_BLOCKING_SEVERITIES`` is an
  ``ast.Name``, never an ``ast.Constant``, so reading the set can never trip
  the check that forbids inlining it.

What remains is the only thing R2.4 is actually about: a **string literal in
executable code**. Every ``ast.Constant`` of type ``str`` in the function body
is collected, and none may equal a severity token after stripping and
lower-casing — which catches ``"Minor"`` and ``" minor"`` as well as ``"minor"``,
since an inline literal in any spelling is the defect. ``"critical"`` and
``"major"`` are forbidden by the same assertion: an inline blocking enumeration
is the other half of the same failure, and design §4.3 rejects a
``BLOCKING_SEVERITIES`` set precisely because unrecognised-blocks must stay the
absence of an exemption rather than a branch.

**Why this test exists at all.** A typo in the constant — ``"minr"`` — fails
closed. The gate keeps blocking every severity, the run keeps failing, and no
recognised-token test notices, because the floor was never exercised. That is
the one failure mode design §4.3 calls silent. Two independent pins catch it:
the subset relation against ``RECOMMENDED_SEVERITIES`` (a token the prompt never
asks for is unreachable) and the exact value in 2.4a.

**Non-vacuity.** An AST walk that found nothing would make the offender check
pass for free, so 2.4b asserts the collected literals are non-empty and include
``"severity"`` — the key the body reads. The gate consults severity and carries
no severity literal; both halves are asserted, so neither can be true by
accident.

NO FIXTURES
-----------
Nothing here needs one. These tests import one pure-stdlib module and read
source text — no AWS client, no moto mock, no Lambda environment — so no
fixture from ``tests/fixtures/conftest.py`` applies. The root
``tests/conftest.py`` still applies ``@pytest.mark.unit`` by location.

No line numbers appear in any assertion. ``review_verdict.py`` moved twice
during this spec already (task 1.1 added 22 lines, task 3.1's docstring rewrite
added ~22 more), so the constants are located by name and the gate by calling
``inspect`` on the real function.

**Validates: Requirements 2.4, 2.5, 2.6**
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys
import textwrap

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
import review_verdict as _rv
from review_verdict import (
    NON_BLOCKING_SEVERITIES,
    RECOMMENDED_SEVERITIES,
    effective_verdict,
)

#: Assignment lines for the two constants, anchored at column 0 so a mention
#: inside a comment or a docstring cannot satisfy the search.
_RECOMMENDED_ASSIGNMENT_RE = re.compile(r"^RECOMMENDED_SEVERITIES\s*=", re.MULTILINE)
_NON_BLOCKING_ASSIGNMENT_RE = re.compile(r"^NON_BLOCKING_SEVERITIES\s*=", re.MULTILINE)

#: Leading ``#`` / ``#:`` comment markers. ``[ \t]*`` rather than ``\s*`` so the
#: pattern cannot walk backwards over a newline and eat a code line's start.
_COMMENT_MARKER_RE = re.compile(r"^[ \t]*#:?[ \t]?", re.MULTILINE)

#: Claim boundaries inside the constants' comment block: sentence stops, the
#: semicolon joining the two bullets, and the bullet marker itself.
_CLAIM_SPLIT_RE = re.compile(r"[.;]|\s\*\s")

_NEGATION_RE = re.compile(r"\b(?:cannot|can't|never|not|no)\b")
_NOVELTY_RE = re.compile(r"\b(?:novel|unrecognised|unrecognized|unlisted|outside)\b|nobody listed")
_BLOCK_RE = re.compile(r"\bblock\w*\b")


def _severity_constants_segment() -> str:
    """The module source spanning both severity constants, comment blocks included.

    Runs from the first line of ``RECOMMENDED_SEVERITIES``' comment block down to
    the ``NON_BLOCKING_SEVERITIES`` assignment. This is the region R2.5 calls
    "at the severity constants", located by name rather than by line number.
    """
    source = inspect.getsource(_rv)
    lines = source.splitlines()

    recommended_index = next((i for i, line in enumerate(lines) if _RECOMMENDED_ASSIGNMENT_RE.match(line)), None)
    non_blocking_index = next((i for i, line in enumerate(lines) if _NON_BLOCKING_ASSIGNMENT_RE.match(line)), None)

    assert recommended_index is not None, (
        "no module-level `RECOMMENDED_SEVERITIES = ...` assignment found in review_verdict.py. "
        "Either it was renamed or it is no longer at module scope (R2.6)."
    )
    assert non_blocking_index is not None, (
        "no module-level `NON_BLOCKING_SEVERITIES = ...` assignment found in review_verdict.py. "
        "The gate's severity floor reads this constant (R2.4)."
    )
    assert recommended_index < non_blocking_index, (
        "NON_BLOCKING_SEVERITIES is declared ABOVE RECOMMENDED_SEVERITIES. Design §4.3 places the "
        "non-blocking set immediately below the prompt-facing tuple so the pair reads side by side, "
        "and the subset relation reads in the direction it is asserted (R2.5)."
    )

    start = recommended_index
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    return "\n".join(lines[start : non_blocking_index + 1])


def _claims(segment: str) -> list[str]:
    """The segment's comment prose, split into individual claims and lower-cased.

    Comment markers are stripped, whitespace collapsed so re-wrapping a comment
    line is not a pin failure, then split on sentence stops, on the semicolon
    joining the two bullets, and on the bullet marker. Splitting on newline was
    rejected: every claim in this block wraps across two or three lines, so a
    per-line window would orphan "cannot fail the STEP" from the sentence it
    belongs to and the two facts could never be told apart.
    """
    text = _COMMENT_MARKER_RE.sub("", segment)
    text = " ".join(text.split()).lower()
    return [claim.strip() for claim in _CLAIM_SPLIT_RE.split(text) if claim.strip()]


def _function_body_nodes(func) -> list[ast.stmt]:
    """``func``'s body statements with the docstring dropped.

    The docstring is dropped because R2.4 is a claim about what the gate *reads*,
    and design §4.1 requires the docstring to describe the floor in prose that
    legitimately names ``minor``. See the module docstring.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(definitions) == 1, (
        f"expected exactly one function definition in the source of {func.__name__}, got {len(definitions)}. "
        "The AST-based assertions below would be looking at the wrong node."
    )

    body = list(definitions[0].body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _executable_string_literals(func) -> list[str]:
    """Every ``str`` constant in ``func``'s executable body (docstring excluded).

    Comments never appear — the AST discards them — so a comment naming a
    severity is free. A reference to ``NON_BLOCKING_SEVERITIES`` is an
    ``ast.Name`` and never appears here either, which is what lets "reads the
    constant" and "carries no literal" be asserted together.
    """
    return [
        node.value
        for statement in _function_body_nodes(func)
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _referenced_names(func) -> set[str]:
    """Every bare name loaded in ``func``'s executable body."""
    return {
        node.id
        for statement in _function_body_nodes(func)
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
    }


# ---------------------------------------------------------------------------
# 2.4a — the constant's existence, container type and value
# ---------------------------------------------------------------------------


def test_non_blocking_severities_is_a_frozenset_containing_only_minor():
    """R2.4: the named constant the gate reads exists, and is exactly ``{"minor"}``.

    A ``frozenset`` and not a tuple, because in this module the container is the
    signal (design §4.3): ``ALLOWED_VERDICTS`` and ``ALLOWED_ATTRIBUTIONS`` are
    frozensets and are load-bearing membership vocabulary, while
    ``RECOMMENDED_SEVERITIES`` and ``REVIEW_CATEGORIES`` are tuples and are
    documentation. Rule 1 tests membership, so this one is a frozenset — and
    immutable, so no consumer can widen the floor at runtime.

    The exact value is pinned because a typo here fails closed and silently: the
    gate would keep blocking every severity, every run would keep failing, and
    no recognised-token test would notice.
    """
    assert hasattr(_rv, "NON_BLOCKING_SEVERITIES"), (
        "review_verdict no longer exposes NON_BLOCKING_SEVERITIES. R2.4 requires the gate to read a "
        "named constant rather than an inline literal, and three test files plus the TypeScript "
        "cross-language pin resolve it by that name."
    )
    assert isinstance(NON_BLOCKING_SEVERITIES, frozenset), (
        f"NON_BLOCKING_SEVERITIES is a {type(NON_BLOCKING_SEVERITIES).__name__}, not a frozenset. "
        "In this module the container type states which kind of vocabulary a constant is: frozenset "
        "for load-bearing membership, tuple for documentation (design §4.3). A mutable set would also "
        "let a consumer widen the severity floor at runtime."
    )
    # Member equality rather than `== frozenset({"minor"})`: with the frozenset
    # assertion above, sorted members are exactly set equality, and ruff's SIM300
    # reads the literal-on-the-right comparison as a Yoda condition.
    assert sorted(NON_BLOCKING_SEVERITIES) == ["minor"], (
        f"NON_BLOCKING_SEVERITIES is {sorted(NON_BLOCKING_SEVERITIES)!r}, expected exactly ['minor']. "
        "Blocking_Severity is this set's COMPLEMENT, so every edit here changes the gate: adding a "
        "token exempts it, and a typo removes the only exemption and silently reverts the floor."
    )


# ---------------------------------------------------------------------------
# 2.4b — the subset relation, and the gate reading the constant
# ---------------------------------------------------------------------------


def test_non_blocking_severities_is_a_subset_of_recommended_severities():
    """R2.4: every exempt token is one the Reviewer_Prompt actually asks for.

    Design §4.3 keeps these two constants independent — deriving one from the
    other would make a prompt-facing documentation tuple load-bearing, so
    editing the prompt's vocabulary would change the gate — and asserts the
    subset relation instead. That is what still catches the typo: a token the
    prompt never asks for is unreachable, so ``frozenset({"minr"})`` is a dead
    calibration, and this assertion is one of the two that sees it.

    Direction matters. The prompt tuple may hold tokens that block
    (``critical``, ``major``); the exempt set may not hold tokens the prompt
    never names.
    """
    unreachable = sorted(NON_BLOCKING_SEVERITIES - set(RECOMMENDED_SEVERITIES))

    assert not unreachable, (
        f"NON_BLOCKING_SEVERITIES exempts {unreachable!r}, which RECOMMENDED_SEVERITIES "
        f"{RECOMMENDED_SEVERITIES} never asks the reviewer for. An exemption for a token the model is "
        "not asked to emit is unreachable — and if it replaced a reachable one, the floor is dead and "
        "fails closed silently (design §4.3, R2.4)."
    )


def test_the_gate_reads_the_constant_and_carries_no_bare_severity_literal():
    """R2.4: rule 1 names ``NON_BLOCKING_SEVERITIES``; its body inlines no token.

    Asserted over the AST of ``effective_verdict``, not over its source text.
    The substring form would fail on the function's own docstring, which design
    §4.1 requires to describe the floor and which therefore writes ``minor`` in
    prose. What R2.4 forbids is narrower and precise: a **string literal in
    executable code**. Docstring prose and comments are excluded deliberately —
    the AST drops comments outright — and a reference to the constant is an
    ``ast.Name``, so "reads the constant" and "inlines no literal" can both be
    asserted without one defeating the other.

    ``critical`` and ``major`` are forbidden by the same check. An inline
    blocking enumeration is the other half of the same defect, and design §4.3
    rejects a ``BLOCKING_SEVERITIES`` set so that unrecognised-blocks stays the
    absence of an exemption rather than a deletable branch.

    Both directions are asserted because either alone is satisfiable by
    accident: a body with no literal that also never reads the constant has no
    floor at all.
    """
    names = _referenced_names(effective_verdict)
    literals = _executable_string_literals(effective_verdict)

    assert "NON_BLOCKING_SEVERITIES" in names, (
        "effective_verdict's body does not reference NON_BLOCKING_SEVERITIES. R2.4 requires the gate "
        f"to read the named constant rather than an inline literal. Names read: {sorted(names)!r}."
    )

    # Non-vacuity: an AST walk that collected nothing would pass the offender
    # check for free. The body must read the severity key it classifies.
    assert literals, (
        "no string literals found in effective_verdict's body at all. The extraction is broken, and "
        "the offender assertion below would pass without checking anything."
    )
    assert "severity" in literals, (
        f"effective_verdict's body no longer reads the 'severity' key (literals: {sorted(set(literals))!r}). "
        "Either the floor was removed, or severity is now reached some other way and this test is "
        "asserting against the wrong function (R2.4)."
    )

    severity_tokens = set(RECOMMENDED_SEVERITIES) | set(NON_BLOCKING_SEVERITIES)
    offenders = sorted({literal for literal in literals if literal.strip().lower() in severity_tokens})

    assert not offenders, (
        f"effective_verdict inlines the severity literal(s) {offenders!r}. R2.4: the gate reads "
        "NON_BLOCKING_SEVERITIES, never a bare token. An inline exemption bypasses the constant every "
        "other consumer and the TypeScript mirror are pinned against; an inline 'critical'/'major' is "
        "a blocking enumeration, which design §4.3 rejects because unrecognised-blocks must remain the "
        "absence of an exemption rather than a branch a later reader can delete unnoticed."
    )


# ---------------------------------------------------------------------------
# 2.5 — the two facts about a novel token, stated separately
# ---------------------------------------------------------------------------


def test_the_severity_constants_state_the_step_and_gate_facts_separately():
    """R2.5: a novel token cannot fail the STEP, and it does block the GATE.

    R2.5 exists because the comment that used to sit alone above
    ``RECOMMENDED_SEVERITIES`` conflated them: it said only that any non-empty
    token is accepted "so a novel severity cannot fail the step" — true, and
    silent about the gate, which reads that same token as blocking. One
    sentence, one of the two facts, and a reader who stopped there would
    conclude an unrecognised severity was harmless.

    So this asserts **two** statements, not one containing both words: a claim
    naming a novel token and the step under a negation, and a separate claim
    naming a novel token blocking the gate. C4 is the fence on the first half
    (no raise for a cosmetic spelling) and R2.3 the second (unrecognised
    blocks); the pin is that the module says both, where a reader of either
    constant will see them.

    A documentation assertion in the style of the existing source check at
    ``test_review_gate_calibration.py::test_gate_functions_contain_no_token_presence_or_keyword_count_logic``
    — prose is not machine-checkable, so what is checked is that both claims are
    present and distinct, not that they are well written.
    """
    segment = _severity_constants_segment()
    claims = _claims(segment)

    # Non-vacuity: an over-matching extraction would make every claim search
    # below vacuous. The segment must span both constants and open on a comment.
    assert "RECOMMENDED_SEVERITIES" in segment and "NON_BLOCKING_SEVERITIES" in segment, (
        "the extracted segment does not span both severity constants — the assertions below would be "
        f"looking at the wrong region of review_verdict.py. Segment:\n{segment}"
    )
    assert segment.lstrip().startswith("#"), (
        "the extracted segment does not open on a comment line, so RECOMMENDED_SEVERITIES' comment "
        f"block was not captured and R2.5's 'at the severity constants' is unverified. Segment:\n{segment}"
    )
    assert len(claims) > 5, (
        f"the segment split into only {len(claims)} claim(s), which is too few for the comment blocks "
        f"R2.5 requires. Either the comments were removed or the extraction is broken. Segment:\n{segment}"
    )

    step_claims = [c for c in claims if "step" in c and _NEGATION_RE.search(c) and _NOVELTY_RE.search(c)]
    gate_claims = [c for c in claims if "gate" in c and _BLOCK_RE.search(c) and _NOVELTY_RE.search(c)]

    assert step_claims, (
        "the severity constants no longer state that a novel severity token cannot fail the STEP. "
        "C4: the parser accepts any non-empty token precisely so a cosmetic spelling costs a reparse "
        "retry and an honest step failure instead of being ignored. Say it here, next to the constant "
        "that does not exempt it (R2.5). Claims found:\n  " + "\n  ".join(claims)
    )
    assert gate_claims, (
        "the severity constants no longer state that a novel severity token DOES block the GATE. "
        "R2.3: Blocking_Severity is NON_BLOCKING_SEVERITIES' complement, so an unrecognised token "
        "blocks — and that is the half a reader is most likely to get wrong, because the neighbouring "
        "comment says the parser accepts it (R2.5). Claims found:\n  " + "\n  ".join(claims)
    )

    assert any(step_claim != gate_claim for step_claim in step_claims for gate_claim in gate_claims), (
        "the step fact and the gate fact are stated by the same single claim:\n  "
        f"{step_claims[0]}\n"
        "R2.5 asks for two statements 'so the two statements are not confusable'. One sentence carrying "
        "both is how the old comment above RECOMMENDED_SEVERITIES came to say only half of it. Split "
        "them."
    )


# ---------------------------------------------------------------------------
# 2.6 — the prompt-facing tuple
# ---------------------------------------------------------------------------


def test_recommended_severities_names_the_three_prompt_facing_tokens():
    """R2.6: the documentation tuple still names ``critical``, ``major``, ``minor``.

    Deliberately duplicated with
    ``test_reviewer_prompt_severity_vocabulary.py::test_recommended_severities_is_the_vocabulary_the_prompts_enumerate``,
    for the reasons set out in this module's docstring: there the tuple is the
    oracle for a claim about the two ``.j2`` templates, here it is the subject of
    R2.6 itself, and only this one pins the container type.

    ``tuple`` matters. It is the house signal that nothing load-bearing reads
    this constant (design §4.3) — and it is also what keeps the subset assertion
    in 2.4b honest: if this became a set, the two constants would be the same
    kind of object and the "documentation versus load-bearing" distinction would
    stop being visible at the declaration.

    Before this test the tuple had exactly one occurrence in the tree, its own
    definition. Nothing read it, so nothing pinned it.
    """
    assert isinstance(RECOMMENDED_SEVERITIES, tuple), (
        f"RECOMMENDED_SEVERITIES is a {type(RECOMMENDED_SEVERITIES).__name__}, not a tuple. In this "
        "module a tuple means documentation or ordered display and a frozenset means load-bearing "
        "membership vocabulary (design §4.3); no gate decision reads this constant, so it stays a tuple."
    )
    assert RECOMMENDED_SEVERITIES == ("critical", "major", "minor"), (
        f"RECOMMENDED_SEVERITIES is {RECOMMENDED_SEVERITIES!r}, expected ('critical', 'major', 'minor') "
        "— the three tokens the Reviewer_Prompt asks the model for (R2.6). p43 changes no prompt text, "
        "and NON_BLOCKING_SEVERITIES must stay a subset of this tuple, so editing it can strand the "
        "gate's only exemption."
    )
