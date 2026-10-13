# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the Glue Catalog target prefix rule is spelled the same in both languages.

Two artifacts have to agree on one rule and neither can import the other:

- The **pre-flight**, ``lambda/agentic_data_ingestion_dashboard_api/handlers/resources.py``,
  declares ``_GLUE_DATABASE_PREFIX`` (``:338``) and
  ``_REASON_GLUE_DATABASE_PREFIX`` (``:340``). ``_glue_database_prefix_refusal``
  (``:416-444``) refuses a Glue Catalog target whose database name does not start
  with the prefix, and returns the reason token in its 400 body (``:442``).
- The **picker**, ``dashboard-react/src/utils/glue-target-eligibility.ts``, declares
  ``GLUE_TARGET_DATABASE_PREFIX`` and ``GLUE_TARGET_EXCLUSION_REASON`` for the same
  rule, applied client-side by both ``GlueCatalogForm``s (p66 O1, R4.1).

p66 R1.4 requires the excluding rule to be **expressed once**. After task 1.3 the
literal exists on both sides of the language boundary, and a comment in each file
saying "keep these in sync" is not a mechanism — which is exactly why this file
exists (p66 task 1.4).

WHY THIS PARSES BOTH SOURCES AND TRANSCRIBES NEITHER
----------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**) is the governing record and its
rejection is the whole point, verbatim: *"Rejected transcribing the field list
into the test or into a JSON schema: a test written from the producer's output
cannot detect the producer drifting, because the output is its own oracle."* It is
cited as evidence rather than aspiration because it records **a live drift it
caught** — ``CostRecommendation.type`` declaring ``'upgrade'`` while the agent
emitted ``'upsize'``. ``LGR-cd58706-3`` (**template / ratified**) re-applies the
same rule and adds the reuse discipline this file follows below.

So neither literal is written down here. If this file contained
``'agentic_data_ingestion_'`` as an expected value, it would pass while both sides
drifted away from it together, and — worse — it would pass while one side drifted
*to* it, which is the case a pin is for.

WHY ``ast`` ON THE PYTHON SIDE AND A REGEX ON THE TYPESCRIPT SIDE
-----------------------------------------------------------------
The asymmetry is inherited, not invented; both halves already exist in this
directory.

*Python with* ``ast``, following ``test_not_verified_reason_labels_cross_language.py``.
``LGR-b46a5bb-4`` (**template / ratified**) rejected substring matching for a
repository-wide invariant test because "it cannot tell source from prose", and that
applies here with force: ``_GLUE_DATABASE_PREFIX`` is named in the 50-line
precondition comment above its own declaration and in four f-strings. Importing the
module is worse than unnecessary — ``resources.py:18-19`` constructs two boto3
clients at module scope and ``handlers.common`` calls ``require_env("AUDIT_TABLE")``
at import time (which is why ``tests/contracts/conftest.py`` seeds that variable) —
so an import would need path surgery and would build AWS clients to read two string
constants. ``ast`` reads the declaration and nothing else.

*TypeScript with a regex*, following ``test_approval_window_cross_language.py``
(``LGR-69ca78b-2``, **template / ratified**: "the two artifacts cannot import each
other, so the agreement is asserted by parsing ``APPROVAL_TIMEOUT_SECONDS`` out of
the TypeScript rather than restating it — a second literal is the drift R6.6 exists
to prevent"). The match is anchored on the ``export const`` **declaration**, not on
a bare mention, so a reference elsewhere in the file cannot satisfy it.

``api_responses/helpers.py``'s frontend-type oracle is **not** reusable here, and
that is checked rather than assumed: ``read_interface`` / ``load_interfaces``
(``helpers.py:61``, ``:104``) parse ``interface`` declarations into
``{field: (required, ts_type)}``. This subject is a pair of ``export const`` string
literals, which that reader has no notion of. ``LGR-cd58706-3`` made reuse
*mandatory where the reader fits* — its concern was an eighth divergent copy of one
parser — so declining it for a different grammar honours the record rather than
sidestepping it, and no new interface parser is added.

THE THIRD COPY OF THE PREFIX STRING, AND WHY IT IS DELIBERATELY NOT PINNED HERE
-------------------------------------------------------------------------------
``shared/glue_mirror_identifiers.py:47`` declares
``GLUE_MIRROR_DATABASE_PREFIX: Final[str] = "agentic_data_ingestion_"`` — the same
bytes, a **different rule**. It is the prefix ``create_infrastructure`` *writes*
onto S3 Tables mirror databases (``create_infrastructure/lambda_function.py:272``);
the two constants pinned below are the prefix a Glue Catalog target is *accepted*
under. ``LGR-9932c9b-1`` (**template / ratified**) records that
``agentic_data_ingestion_`` is "a different scheme sharing the prefix" and rejected
a prefix match on exactly that ground, and ``glue_mirror_identifiers``' own comment
says callers "must match the exact pair rather than test this prefix".

Asserting equality against it here would encode a coupling the ledger explicitly
denies: it would fail the day either naming scheme legitimately moves alone, and it
would teach the next reader that the mirror writer and the target pre-flight are one
rule. R1.4's scope is the **excluding rule consumed by every Database_Picker**, and
the mirror prefix is consumed by no picker. So it is named here and left alone. It
is reported as a finding p66's design did not enumerate, not silently absorbed.

WHY THE COMPARISON IS EXACT EQUALITY
------------------------------------
``_glue_database_prefix_refusal`` applies a bare ``database_name.startswith(...)``
(``resources.py:422``) — no ``.strip()``, no ``.lower()``, no normalisation. A
case-insensitive or "contains" pin would therefore permit a **real** drift: the
picker could offer ``AGENTIC_DATA_INGESTION_x`` while the pre-flight refused it at
Detect Schema, which is the false accept p66's O4 exists to prevent.
``test_the_python_rule_applies_no_normalisation`` pins that premise, so if the
Python rule ever normalises, this file fails and says which comparison to revisit
rather than quietly permitting a gap.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on nothing. An extraction that
silently yields ``None`` on both sides would make an equality assertion vacuously
true, so:

1. Both files must exist (``test_both_sources_are_readable``) — a moved or renamed
   module fails loudly instead of skipping.
2. Each of the four extractions raises through an assertion naming what changed,
   rather than returning a default.
3. Each parsed value must be plausible for what it is — the prefix a snake_case
   token ending in ``_``, the reason a bare snake_case token — so a mis-anchored
   regex that captured a type annotation or an empty string fails.
4. The two Python constants are corroborated against their **use sites**: the
   ``startswith`` call must take the prefix constant, and the 400 body's ``reason``
   must *be* the reason constant. Pinning a declaration that the live rule no longer
   reads is the other way for this file to pass while meaning nothing — and a
   mutation showed a weaker form of this guard (asserting only that the function
   mentions the name somewhere) does not catch it, because the name survives in the
   log line and two f-strings after the comparison has been inlined.

Validates: p66 Requirement 1.4
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PREFLIGHT = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api" / "handlers" / "resources.py"
_ELIGIBILITY_MODULE = _PROJECT_ROOT / "dashboard-react" / "src" / "utils" / "glue-target-eligibility.ts"

#: Python constant names, and the TypeScript constant each must equal.
_PYTHON_PREFIX_NAME = "_GLUE_DATABASE_PREFIX"
_PYTHON_REASON_NAME = "_REASON_GLUE_DATABASE_PREFIX"
_TS_PREFIX_NAME = "GLUE_TARGET_DATABASE_PREFIX"
_TS_REASON_NAME = "GLUE_TARGET_EXCLUSION_REASON"

#: The function that applies the rule, and the response key carrying the reason.
_REFUSAL_FUNCTION = "_glue_database_prefix_refusal"
_REASON_RESPONSE_KEY = "reason"


def _ts_const_pattern(name: str) -> re.Pattern[str]:
    """An ``export const NAME[: type] = '<value>';`` declaration.

    Anchored on ``export const`` so a mention in a doc comment or a use site
    cannot satisfy it — this module's own JSDoc names both constants repeatedly.
    Both quote styles are accepted because prettier's choice is not the contract.
    """
    return re.compile(rf"export\s+const\s+{re.escape(name)}\s*(?::\s*[^=]+?)?=\s*(['\"])((?:[^'\"\\]|\\.)*)\1")


#: What a Glue database prefix looks like: a snake_case token that ends in ``_``
#: so it can be prepended to a name. Deliberately not the value itself.
_PREFIX_SHAPE_RE = re.compile(r"^[a-z][a-z0-9_]*_$")

#: What a machine-readable ``reason`` token looks like, matching the vocabulary
#: the rest of the pre-flight emits.
_REASON_SHAPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _preflight_module() -> ast.Module:
    """``resources.py`` parsed, never imported.

    See the module docstring: importing builds two boto3 clients and reads the
    environment to obtain two string constants.
    """
    return ast.parse(_PREFLIGHT.read_text(encoding="utf-8"), filename=str(_PREFLIGHT))


def _python_constant(name: str) -> str:
    """A module-level ``name = "<literal>"`` from the pre-flight, by AST.

    Raises through an assertion rather than returning a default, so a renamed or
    computed constant can never present as "nothing to compare".
    """
    for node in _preflight_module().body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if name not in targets:
            continue
        assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), (
            f"{name} is no longer a plain string literal in "
            "lambda/agentic_data_ingestion_dashboard_api/handlers/resources.py — it is now "
            f"{ast.dump(node.value)[:80]}. Extend this extraction rather than deleting the "
            "assertion: an unread Python side makes the comparison below vacuous."
        )
        return node.value.value

    raise AssertionError(
        f"{name} not found at module level in "
        "lambda/agentic_data_ingestion_dashboard_api/handlers/resources.py. The Glue Catalog "
        "pre-flight's constant was renamed or moved, so the TypeScript picker's copy of the "
        "same rule can no longer be checked against it (p66 R1.4). Fix the name here; do not "
        "drop the check."
    )


def _typescript_constant(name: str) -> str:
    """An ``export const`` string literal from the eligibility module, by regex."""
    source = _ELIGIBILITY_MODULE.read_text(encoding="utf-8")
    match = _ts_const_pattern(name).search(source)
    assert match is not None, (
        f"export const {name} declaration not found in "
        "dashboard-react/src/utils/glue-target-eligibility.ts — the picker's copy of the rule "
        "was renamed, moved or reshaped (a template literal, a computed value, a re-export), "
        "so it can no longer be held against the Python pre-flight. Fix the extraction here: "
        "two unpinned literals for one rule is what p66 R1.4 exists to prevent."
    )
    return match.group(2)


def _refusal_function() -> ast.FunctionDef:
    """``_glue_database_prefix_refusal``'s AST node."""
    for node in _preflight_module().body:
        if isinstance(node, ast.FunctionDef) and node.name == _REFUSAL_FUNCTION:
            return node
    raise AssertionError(
        f"{_REFUSAL_FUNCTION} not found in resources.py. It is the only reader of "
        f"{_PYTHON_PREFIX_NAME}, so without it the constant pinned below is pinned to nothing."
    )


def test_both_sources_are_readable():
    """Guard: a moved or renamed source must fail loudly, not skip."""
    assert _PREFLIGHT.is_file(), f"Glue Catalog pre-flight not found at {_PREFLIGHT}"
    assert _ELIGIBILITY_MODULE.is_file(), f"eligibility module not found at {_ELIGIBILITY_MODULE}"


def test_all_four_literals_parse_to_plausible_values():
    """Anti-vacuity: every extraction recovers a real value of the right shape.

    Each guard runs here on its own, so a degraded parse is reported as *this*
    test failing rather than as the two pins below passing over ``None == None``.
    """
    python_prefix = _python_constant(_PYTHON_PREFIX_NAME)
    ts_prefix = _typescript_constant(_TS_PREFIX_NAME)
    python_reason = _python_constant(_PYTHON_REASON_NAME)
    ts_reason = _typescript_constant(_TS_REASON_NAME)

    for label, value in ((_PYTHON_PREFIX_NAME, python_prefix), (_TS_PREFIX_NAME, ts_prefix)):
        assert _PREFIX_SHAPE_RE.match(value), (
            f"parsed {label} as {value!r}, which is not the shape of a Glue database prefix "
            "(a snake_case token ending in '_'). The extraction is picking up something that "
            "is not the constant — an annotation, a comment, or a truncated match."
        )

    for label, value in ((_PYTHON_REASON_NAME, python_reason), (_TS_REASON_NAME, ts_reason)):
        assert _REASON_SHAPE_RE.match(value), (
            f"parsed {label} as {value!r}, which is not the shape of a machine-readable "
            "reason token. A consumer branches on this string, so an unparseable value here "
            "means the comparison below is not comparing reasons."
        )


def test_the_python_constants_are_the_ones_the_preflight_actually_reads():
    """Corroboration: both constants reach their use sites by NAME, not by copy.

    Pinning a constant nothing reads is the second way this file could pass while
    meaning nothing. So the refusal must obtain the prefix through
    ``_GLUE_DATABASE_PREFIX`` and must key its 400's ``reason`` on
    ``_REASON_GLUE_DATABASE_PREFIX`` — an inlined literal at either site would put
    the live rule outside this test's reach while leaving the declaration to agree
    with TypeScript forever.
    """
    refusal = _refusal_function()

    # The comparison itself, not merely a mention. An earlier draft of this test
    # asserted only that the function *referenced* the constant somewhere, and a
    # mutation proved that insufficient: inlining the literal into the
    # ``startswith`` call left the name in the log message and the two f-strings,
    # so the test passed while the live rule had escaped it.
    prefix_tests = [
        node
        for node in ast.walk(refusal)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
    ]
    assert len(prefix_tests) == 1, (
        f"{_REFUSAL_FUNCTION} makes {len(prefix_tests)} startswith calls, expected exactly 1. "
        "The rule is one comparison; if it has become several, this test cannot say which one "
        "the pinned constant governs."
    )
    argument = prefix_tests[0].args[0] if prefix_tests[0].args else None
    assert isinstance(argument, ast.Name) and argument.id == _PYTHON_PREFIX_NAME, (
        f"{_REFUSAL_FUNCTION}'s startswith no longer takes {_PYTHON_PREFIX_NAME} — it takes "
        f"{ast.dump(argument) if argument else 'nothing'}. The constant this file pins against "
        "TypeScript is then not the value the pre-flight compares, so the agreement below "
        "would hold while the live rule drifted freely."
    )

    # The reason likewise: the 400 body's ``reason`` must be the constant, not a
    # copy of its value. An inlined token would be invisible to the pin below.
    reason_values = [
        value
        for node in ast.walk(refusal)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == _REASON_RESPONSE_KEY
    ]
    assert len(reason_values) == 1, (
        f"expected exactly one {_REASON_RESPONSE_KEY!r} key in {_REFUSAL_FUNCTION}'s response "
        f"body, found {len(reason_values)}. C6's shape is 'classify locally, emit a "
        "machine-readable reason'; without that key the shared token has no wire presence and "
        "the picker's matching token means nothing."
    )
    assert isinstance(reason_values[0], ast.Name) and reason_values[0].id == _PYTHON_REASON_NAME, (
        f"{_REFUSAL_FUNCTION}'s {_REASON_RESPONSE_KEY!r} is no longer {_PYTHON_REASON_NAME} — "
        f"it is {ast.dump(reason_values[0])}. The emitted token must come from the constant, "
        "so that the picker sharing it (p66 O4 — one vocabulary, two checks) shares the one "
        "actually on the wire."
    )


def test_database_prefix_agrees_across_the_language_boundary():
    """``_GLUE_DATABASE_PREFIX`` == ``GLUE_TARGET_DATABASE_PREFIX``.

    Exact equality, deliberately: the Python rule is a bare ``startswith`` with no
    trim and no case fold, so any looser comparison here would permit a name the
    picker offers and the pre-flight refuses.

    Validates: p66 Requirement 1.4
    """
    python_prefix = _python_constant(_PYTHON_PREFIX_NAME)
    ts_prefix = _typescript_constant(_TS_PREFIX_NAME)

    assert python_prefix == ts_prefix, (
        f"the Glue Catalog target prefix has drifted: the pre-flight refuses anything not "
        f"starting with {python_prefix!r} (resources.py {_PYTHON_PREFIX_NAME}) while the "
        f"picker offers databases starting with {ts_prefix!r} "
        f"(glue-target-eligibility.ts {_TS_PREFIX_NAME}). One of these is now offering a "
        "target the other will reject at Detect Schema, which is the false accept p66's O4 "
        "exists to prevent. The pre-flight is the authority — it is what the per-pipeline "
        "Glue role is actually scoped to (create_glue_pipeline/iam_roles.py:255-262) — so "
        "move the TypeScript to match it, not the other way round."
    )


def test_exclusion_reason_agrees_across_the_language_boundary():
    """``_REASON_GLUE_DATABASE_PREFIX`` == ``GLUE_TARGET_EXCLUSION_REASON``.

    The token is shared on purpose: p66's O4 is "one vocabulary, two checks", so a
    refusal reads as **one rule at two points in the flow** rather than two
    coincidentally similar ones. Two spellings would not fail anywhere — a consumer
    branching on the reason would simply never match — which is why the agreement
    is asserted rather than commented.

    Validates: p66 Requirement 1.4
    """
    python_reason = _python_constant(_PYTHON_REASON_NAME)
    ts_reason = _typescript_constant(_TS_REASON_NAME)

    assert python_reason == ts_reason, (
        f"the Glue Catalog prefix exclusion reason has drifted: the pre-flight emits "
        f"{python_reason!r} in its 400 body (resources.py {_PYTHON_REASON_NAME}) while the "
        f"picker classifies its exclusions as {ts_reason!r} "
        f"(glue-target-eligibility.ts {_TS_REASON_NAME}). Nothing raises on a mismatch — a "
        "consumer branching on the reason silently never matches — so this assertion is the "
        "only thing that notices. The pre-flight owns the vocabulary (C2 forbids changing "
        "it); move the TypeScript token."
    )


def test_the_python_rule_applies_no_normalisation():
    """The premise that makes exact equality the right comparison above.

    ``_glue_database_prefix_refusal`` tests the raw ``database_name`` — no
    ``.strip()``, no ``.lower()``, no ``casefold()``. The TypeScript predicate is a
    bare ``startsWith`` for the same reason. If the Python side ever normalises,
    the two pins above stop being sufficient on their own and the picker has to
    normalise identically, so this fails and says so rather than letting the gap
    open silently.

    Validates: p66 Requirement 1.4
    """
    refusal = _refusal_function()
    normalisers = {
        node.func.attr
        for node in ast.walk(refusal)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } & {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "replace", "normalize"}

    assert not normalisers, (
        f"{_REFUSAL_FUNCTION} now normalises before comparing ({sorted(normalisers)}). The "
        "picker's isEligibleGlueTargetDatabase is a bare startsWith, so the two rules now "
        "disagree about names in the gap between them — the picker would offer a name the "
        "pre-flight refuses, or refuse one it accepts. Mirror the normalisation in "
        "dashboard-react/src/utils/glue-target-eligibility.ts and widen this test to pin "
        "both; do not delete it."
    )
