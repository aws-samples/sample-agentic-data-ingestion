# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 2.2 — pin the reviewer prompts as UNCHANGED.

Why a no-change pin is the point rather than an omission
--------------------------------------------------------
p43 puts a severity floor on rule 1 of ``effective_verdict``: an explicit
reviewer transform-block finding blocks the gate at ``critical``, ``major`` or
any unrecognised severity, and is retained-but-not-blocking at ``minor``.
Nothing in the reviewer's instructions changes, and that is a decision, not an
oversight — the gate has been *contradicting* the prompt it already ships.
``test_analysis.j2`` and ``test_analysis_fallback.j2`` already tell the model to
reserve a ``fail`` verdict for issues that would block production deployment at
critical or major severity, and the run that motivated this spec did exactly
that: two ``minor`` findings, verdict ``pass``. The gate then overruled it. The
floor makes the gate agree with the prompt already in the tree.

So R6 has no implementation. These pins are the only executable form it has,
and without them nothing in ``tests/`` would notice a later edit that undid the
premise the floor rests on.

do-NOT 4, and why the ABSENCE assertions are the load-bearing half
-----------------------------------------------------------------
``requirements.md`` do-NOT 4: *do NOT change the Reviewer_Prompt to say that
minor findings no longer block.* A model told its ``minor`` findings are ignored
has an incentive to inflate severities, and severity is unvalidated model
output that this spec makes load-bearing for the first time (``requirements.md``
risk 3). An inflating reviewer would silently defeat the floor. The presence
pins below (R6.1, R6.3, R6.4) would all still hold if someone appended "note
that minor findings do not block the gate" — so the absence pins are what
actually enforce do-NOT 4.

How the absence assertion is shaped, and why
--------------------------------------------
Three shaping decisions, each forced by something in the real files:

1. **Assert against MODEL-FACING text, not the raw file.** Jinja ``{# … #}``
   comments are stripped before the model ever sees the prompt, and
   ``test_analysis.j2`` uses one to tell *maintainers* that "the quality gate
   consumes {verdict, issues, categories}". A raw-file assertion that the word
   "gate" is absent therefore fails today, on a comment no model reads. Verified
   on disk: "gate" IS present in raw ``test_analysis.j2`` and is NOT present
   once comments are stripped. R6.2 is a claim about what the model is told, so
   the strip is what makes the assertion mean the right thing.

2. **Forbid the word "gate" outright** (``_FORBIDDEN_GATE_STATEMENTS``), which is
   deliberately stricter than R6.2 requires. R6.2 forbids stating *a rule about
   which severities block the gate*; the cheapest sufficient condition is that
   model-facing text never mentions the gate at all, and today it never does.
   That converts a fuzzy prose requirement into a tripwire that fires on nearly
   any gate-related addition.

3. **Plus a narrower co-occurrence check** for wordings that avoid the word
   "gate" — "minor findings are ignored", "minor issues will not fail the run".
   Within any sentence that mentions a severity, an *operator* (a negation like
   "not"/"never"/"exempt", or a restriction like "only"/"solely") followed by a
   *consequence* verb ("block", "fail", "reject", "stop", "count", "trigger") is
   forbidden. Scoped to severity-bearing sentences precisely because R6.2's
   subject is severity, and because the prompts carry legitimate unnegated
   blocking language ("issues that would block production deployment") and
   legitimate negations elsewhere ("scaffolding findings MUST NOT alone produce
   a fail verdict" — a rule about attribution, not severity).

Patterns that were tried and rejected, because being wrong here is cheap to do.
Each was measured against the real prompts plus a suite of plausible bad edits
and legitimate-edit controls:

* ``"minor" not in text`` — fails immediately; the prompt legitimately
  enumerates ``minor`` as a severity the model should emit.
* ``"block" not in text`` — fails immediately on the R6.3 sentence.
* ``except`` as a negation token — ``test_analysis.j2`` says "try/except
  coverage" under Error Handling. Verified and removed from the negation set.
* **Splitting sentences on ``;``** — orphaned the consequence from the severity
  and let "Report minor issues; they never trigger a rejection" through, because
  the second clause names no severity. The splitter therefore breaks on ``.``
  and newline only, keeping semicolon-joined clauses in one window.
* **Bare ``only critical`` / ``only major`` as forbidden phrases** — false
  positive on ordinary English ("report only critical facts you observe"). The
  phrase list now carries only the specific dangerous form
  ``only critical and major``; the general case is handled by the restriction
  operator above, which requires a consequence to follow.
* **``re.search`` for the operator and the consequence** — returns only the
  *first* of each, so an early consequence masks a later pair: "A ``fail``
  verdict is required for critical issues; minor issues do not block" was
  missed because the ``fail`` at position 3 preceded the ``not``. The check now
  compares every operator against every consequence.

**Honest limit.** This is a tripwire on the phrasings a future editor would
plausibly reach for, not a proof of absence. Measured against the real prompts:
16 of 16 plausible bad edits caught, 0 false positives on 9 legitimate-edit
controls. A sufficiently novel paraphrase could still slip past. Stated rather
than implied, because a pin that oversells its coverage is worse than one that
describes it.

**Non-vacuity control.** A strip regex that ate the whole file would make every
absence assertion pass. The presence pins (R6.1, R6.3, R6.4) run against the
same derived strings, so they are the control — plus
``test_model_facing_text_extraction_is_not_vacuous`` asserts it directly.

Scope
-----
This spec edits NO template file and no production code. Every assertion here is
a no-change pin. No fixtures are used and none are needed: the tests read two
files off disk and call one pure function, so there is no AWS client to stub —
no ``moto.mock_aws()``, no boto3 mock, nothing from
``tests/fixtures/conftest.py``.

Line numbers are deliberately absent from every assertion. ``review_verdict.py``
moved twice during this task alone (task 1.1 added 22 lines, task 3.1's docstring
rewrite added ~22 more), so the reparse prompt is located by calling the real
function and asserting on its output, and the templates are located by content.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**
"""

import os
import re
import sys

import pytest

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
from review_verdict import (
    RECOMMENDED_SEVERITIES,
    build_reparse_prompt,
)

_TEMPLATE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared", "prompt_templates")
)

#: The two Reviewer_Prompt templates. `test_analysis.j2` is the MCP path,
#: `test_analysis_fallback.j2` the path taken when Strands MCP init fails
#: (`test_code/lambda_function.py`, the two `load_prompt_template(...)` calls).
REVIEWER_PROMPT_TEMPLATES = ("test_analysis.j2", "test_analysis_fallback.j2")

#: Neutral placeholders for `build_reparse_prompt`'s two arguments. The function
#: interpolates caller-supplied runtime data (`previous_response[:6000]` and
#: `parse_error`) into its output; that data is not prompt authorship, so
#: passing anything containing a forbidden phrase would fail these tests on the
#: fixture rather than on the prompt. Kept deliberately bland.
_REPARSE_ARGS = ("PREVIOUS RESPONSE PLACEHOLDER", "PARSE ERROR PLACEHOLDER")

# The enumeration both templates and the reparse prompt share verbatim. R6.1 and
# R6.4 are the same claim about two different artifacts, so they pin one string.
_SEVERITY_ENUMERATION = '"severity": "critical"|"major"|"minor"'

# R6.3's sentence, present byte-identically in both templates.
_FAIL_VERDICT_SENTENCE = (
    'Use "fail" when the code has issues that would block production deployment '
    '(critical or major severity); otherwise "pass".'
)

# ---------------------------------------------------------------------------
# do-NOT 4 / R6.2: nothing may tell the model which severities block the gate
# ---------------------------------------------------------------------------

#: Substrings that cannot appear in model-facing prompt text, matched
#: case-insensitively. Every one is verified absent from all three prompts as of
#: this pin. "gate" is the broad one and is intentionally stricter than R6.2:
#: a prompt that never mentions the gate cannot state a rule about it.
_FORBIDDEN_GATE_STATEMENTS = (
    "gate",
    "non-blocking",
    "nonblocking",
    "effective verdict",
    "severity floor",
    "blocking severity",
    # The specific dangerous restriction, not bare "only critical" — that has a
    # legitimate English reading ("report only critical facts you observe").
    "only critical and major",
    "only major and critical",
    "ignor",  # ignore / ignored / ignoring
    "disregard",
    "suppress",
    "harmless",
    "advisory",
)

#: Negations a "these do not block" sentence would be built from. `except` is
#: NOT here on purpose: "try/except coverage" is legitimate prompt text.
_NEGATION = r"(?:not|never|n't|no longer|nor|without|exempt)"

#: Restrictions a "only these block" sentence would be built from — the positive
#: form of the same claim, which a negation-only check misses entirely.
_RESTRICTION = r"(?:only|solely|exclusively)"

#: Consequences such a sentence would negate or restrict. `block\w*` does not
#: match inside `transform_block` (no word boundary after `_`).
_GATE_CONSEQUENCE = r"(?:block\w*|fail\w*|reject\w*|stop\w*|halt\w*|matter\w*|count\w*|trigger\w*)"

#: The two operator classes, named so a failure message says which one fired.
_OPERATOR_RES = (
    ("negation", re.compile(rf"\b{_NEGATION}\b", re.IGNORECASE)),
    ("restriction", re.compile(rf"\b{_RESTRICTION}\b", re.IGNORECASE)),
)
_CONSEQUENCE_RE = re.compile(rf"\b{_GATE_CONSEQUENCE}", re.IGNORECASE)
_SEVERITY_MENTION_RE = re.compile(r"\b(?:critical|major|minor)\b", re.IGNORECASE)

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def _normalise(text: str) -> str:
    """Collapse whitespace, so re-wrapping a prompt line is not a pin failure."""
    return " ".join(text.split())


def _raw_template(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _model_facing(name: str) -> str:
    """The template text the reviewer model actually receives.

    Jinja ``{# … #}`` comments are stripped because the renderer strips them —
    they are maintainer notes, and one of them mentions the quality gate. See
    the module docstring for why asserting on the raw file would be wrong.
    """
    return _JINJA_COMMENT_RE.sub("", _raw_template(name))


def _all_model_facing_prompts() -> dict[str, str]:
    """Every model-facing prompt R6 governs, keyed for readable test ids."""
    prompts = {name: _model_facing(name) for name in REVIEWER_PROMPT_TEMPLATES}
    prompts["build_reparse_prompt"] = build_reparse_prompt(*_REPARSE_ARGS)
    return prompts


def _sentences(text: str) -> list[str]:
    """Split into sentence-ish fragments on `.` and newlines.

    Deliberately NOT on `;`. A semicolon usually joins two clauses of one claim,
    and splitting there orphaned the consequence from the severity: "Report minor
    issues; they never trigger a rejection" got through, because the clause
    carrying "never trigger" names no severity and was therefore skipped.

    `.` still over-splits in places — `"description": "..."` fragments on its
    ellipsis — and that is harmless, because narrowing the window only costs
    recall on a shape nothing in these prompts has.
    """
    return [frag for frag in re.split(r"[.\n]", text) if frag.strip()]


def _forbidden_severity_claim(sentence: str) -> str | None:
    """An operator followed by a consequence, or None.

    Compares EVERY operator against EVERY consequence rather than the first of
    each. `re.search` alone missed "A ``fail`` verdict is required for critical
    issues; minor issues do not block", because the `fail` at position 3
    preceded the `not` and the first-match comparison then read as ordered
    correctly.
    """
    consequences = list(_CONSEQUENCE_RE.finditer(sentence))
    if not consequences:
        return None
    for name, operator_re in _OPERATOR_RES:
        for operator in operator_re.finditer(sentence):
            for consequence in consequences:
                if consequence.start() > operator.start():
                    return f"{name} {operator.group()!r} before {consequence.group()!r}"
    return None


_PROMPT_IDS = (*REVIEWER_PROMPT_TEMPLATES, "build_reparse_prompt")


def _prompt_text(prompt_id: str) -> str:
    return _all_model_facing_prompts()[prompt_id]


# ---------------------------------------------------------------------------
# R6.1 — the severity vocabulary the prompt asks for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", REVIEWER_PROMPT_TEMPLATES)
def test_reviewer_prompt_enumerates_the_three_severity_tokens(template):
    """R6.1: both Reviewer_Prompts still ask for critical | major | minor.

    Pinned against ``RECOMMENDED_SEVERITIES`` rather than a literal triple, so
    the prompt and the module constant cannot drift apart silently. That
    constant is prompt-facing by design — no gate decision reads it (R2.6) —
    which is exactly what makes it the right oracle for a prompt pin.

    p43 changes no prompt text; this asserts that.
    """
    text = _model_facing(template)

    assert _SEVERITY_ENUMERATION in text, (
        f"{template} no longer enumerates the severity vocabulary as "
        f"{_SEVERITY_ENUMERATION!r}. p43 changes no prompt text (R6.1)."
    )
    for severity in RECOMMENDED_SEVERITIES:
        assert f'"{severity}"' in text, f"{template} no longer asks the model for severity {severity!r} (R6.1)."


def test_recommended_severities_is_the_vocabulary_the_prompts_enumerate():
    """R6.1/R6.4: the module constant and the prompts name the same three tokens.

    The reverse direction of the test above. Without it, adding a fourth token
    to ``RECOMMENDED_SEVERITIES`` would leave the prompt pin green while the
    constant claimed a vocabulary the reviewer is never asked for — and
    ``NON_BLOCKING_SEVERITIES`` is required to stay a subset of it (design §4.3),
    so an unreachable token there is a dead calibration.
    """
    assert RECOMMENDED_SEVERITIES == ("critical", "major", "minor")


# ---------------------------------------------------------------------------
# R6.3 — the fail-verdict instruction the floor now agrees with
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", REVIEWER_PROMPT_TEMPLATES)
def test_reviewer_prompt_reserves_fail_for_critical_or_major(template):
    """R6.3: the instruction reserving ``fail`` for critical/major survives.

    This sentence is the premise of the whole spec: the reviewer on the failing
    run followed it (two minor findings, verdict ``pass``) and the gate overruled
    it anyway. Delete this sentence and the floor is no longer "make the gate
    agree with the prompt" — it is an unexplained relaxation.

    Asserted on whitespace-normalised text so re-wrapping the line is not a
    failure, and clause-by-clause first so a failure says which half moved.
    """
    text = _model_facing(template)

    assert "would block production deployment" in text, (
        f"{template} no longer reserves a fail verdict for production-blocking issues (R6.3)."
    )
    assert "(critical or major severity)" in text, (
        f"{template} no longer names critical or major as the fail-verdict threshold (R6.3)."
    )
    assert _normalise(_FAIL_VERDICT_SENTENCE) in _normalise(text), (
        f"{template}'s fail-verdict instruction changed. p43 changes no prompt text (R6.3)."
    )


# ---------------------------------------------------------------------------
# R6.4 — the reparse prompt describes the same vocabulary
# ---------------------------------------------------------------------------


def test_reparse_prompt_describes_the_same_severity_vocabulary():
    """R6.4: ``build_reparse_prompt``'s schema text matches the Reviewer_Prompt.

    Asserted against the function's RETURN VALUE, not its source: the reparse
    prompt's schema text sits below ``effective_verdict`` in
    ``review_verdict.py`` and every earlier p43 task shifts its line number.
    Calling the function also pins what the model receives rather than what the
    module happens to contain.

    Note the reparse prompt carries the severity *vocabulary* but NOT the
    fail-verdict sentence — it says only ``"verdict" must be "pass" or "fail"``.
    R6.4 asks for the vocabulary, so that is what is asserted; claiming the
    sentence too would be a pin on behaviour the artifact never had.
    """
    prompt = build_reparse_prompt(*_REPARSE_ARGS)

    assert _SEVERITY_ENUMERATION in prompt, (
        "the reparse prompt no longer describes the Reviewer_Prompt's severity vocabulary (R6.4)."
    )
    for severity in RECOMMENDED_SEVERITIES:
        assert f'"{severity}"' in prompt, f"the reparse prompt no longer names severity {severity!r} (R6.4)."


def test_reparse_prompt_schema_example_uses_a_recommended_severity():
    """R6.4: the embedded schema example draws its severity from the vocabulary.

    The example depicts ``"severity": "minor"`` alongside
    ``"attribution": "transform_block"`` — which after p43 is the one
    non-blocking combination. It is a SHAPE example and says nothing about
    blocking, and do-NOT 4 forbids teaching the model about the floor, so it
    stays exactly as it is (design §5 risk 5). Pinned so a future reader who
    mistakes the example for a statement cannot quietly "fix" it.
    """
    prompt = build_reparse_prompt(*_REPARSE_ARGS)

    example_severities = re.findall(r'"severity":\s*"([^"]+)"', prompt)
    assert example_severities, "the reparse prompt no longer embeds a schema example with a severity (R6.4)."
    for severity in example_severities:
        assert severity in RECOMMENDED_SEVERITIES, (
            f"the reparse prompt's schema example depicts severity {severity!r}, "
            f"which is outside RECOMMENDED_SEVERITIES {RECOMMENDED_SEVERITIES} (R6.4)."
        )


# ---------------------------------------------------------------------------
# R6.2 / do-NOT 4 — the absence of any statement about what blocks the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
def test_no_model_facing_prompt_mentions_the_review_gate(prompt_id):
    """R6.2 / do-NOT 4: no prompt states a rule about which severities block.

    Enforced by forbidding the vocabulary such a rule would need — "gate",
    "non-blocking", "ignored", "advisory" and the rest of
    ``_FORBIDDEN_GATE_STATEMENTS``. Forbidding "gate" outright is stricter than
    R6.2 requires and that is the point: a prompt that never mentions the gate
    cannot state a rule about it, and today none of the three does.

    A model told its ``minor`` findings are ignored has an incentive to inflate
    severities, and p43 makes severity — unvalidated model output — load-bearing
    for the first time. That is the whole reason this assertion exists.

    Asserted on model-facing text: "gate" IS present in raw ``test_analysis.j2``,
    inside a ``{# … #}`` maintainer comment the renderer strips.
    """
    text = _prompt_text(prompt_id).lower()

    for phrase in _FORBIDDEN_GATE_STATEMENTS:
        assert phrase not in text, (
            f"{prompt_id} now contains {phrase!r}, which suggests it tells the model "
            f"something about what blocks the review gate. do-NOT 4 forbids this: a "
            f"model told its minor findings are ignored has an incentive to inflate "
            f"severities, and the prompt already asks for the behaviour the floor "
            f"implements (R6.2)."
        )


@pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
def test_no_model_facing_prompt_ties_a_severity_to_a_gate_consequence(prompt_id):
    """R6.2: no severity-bearing sentence negates or restricts a consequence.

    The narrower half of the absence pin, for wordings that avoid the word
    "gate" — "minor findings are not counted", "minor issues will not fail the
    run", "only critical issues fail". Within any sentence mentioning a
    severity, an operator (negation or restriction) *followed by* a consequence
    verb is forbidden.

    Scoped to severity-bearing sentences because R6.2's subject is severity, and
    because the prompts carry two things this must not fire on: legitimate
    unnegated blocking language ("issues that would block production
    deployment") and a legitimate negation about attribution rather than
    severity ("scaffolding findings MUST NOT alone produce a fail verdict").
    Ordering matters for the same reason — the operator must precede the
    consequence, which is how such a sentence reads.
    """
    for sentence in _sentences(_prompt_text(prompt_id)):
        if not _SEVERITY_MENTION_RE.search(sentence):
            continue
        claim = _forbidden_severity_claim(sentence)
        if claim:
            pytest.fail(
                f"{prompt_id} appears to tell the model how a severity relates to "
                f"a consequence ({claim}):\n    {sentence.strip()}\n"
                f"R6.2 / do-NOT 4: the Reviewer_Prompt states no rule about which "
                f"severities block the gate."
            )


# ---------------------------------------------------------------------------
# Controls for the two absence tests above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_id", _PROMPT_IDS)
def test_model_facing_text_extraction_is_not_vacuous(prompt_id):
    """The control: an absence assertion over an empty string passes for free.

    If ``_JINJA_COMMENT_RE`` ever over-matched and ate a prompt, every assertion
    in the two tests above would pass while checking nothing. The presence pins
    (R6.1, R6.3, R6.4) already run against these same strings and are the real
    control; this states the invariant directly so the failure is legible rather
    than arriving as four confusing greens.
    """
    text = _prompt_text(prompt_id)

    assert len(text) > 1000, f"{prompt_id} extracted to {len(text)} chars — the absence assertions would be vacuous."
    assert '"verdict"' in text, f"{prompt_id} lost the JSON verdict contract — extraction is suspect."
    assert '"issues"' in text, f"{prompt_id} lost the issues schema — extraction is suspect."


@pytest.mark.parametrize("template", REVIEWER_PROMPT_TEMPLATES)
def test_jinja_comments_are_excluded_from_model_facing_text(template):
    """The strip is load-bearing, not cosmetic.

    ``test_analysis.j2`` uses a ``{# … #}`` comment to tell maintainers that
    "the quality gate consumes {verdict, issues, categories}" — so a raw-file
    assertion that "gate" is absent would fail today, on text no model reads.
    This pins the distinction the absence tests depend on: comments are
    maintainer-facing, and only the rendered remainder is model-facing.
    """
    raw = _raw_template(template)
    model_facing = _model_facing(template)

    assert "{#" in raw, f"{template} no longer carries a Jinja comment — re-check the absence tests' premise."
    assert "{#" not in model_facing, f"{template}'s Jinja comments survived the strip."
    assert "#}" not in model_facing, f"{template}'s Jinja comment terminators survived the strip."
    assert len(model_facing) < len(raw), f"{template}: stripping comments removed nothing."
