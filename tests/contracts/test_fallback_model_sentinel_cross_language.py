# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the fallback provenance marker is spelled the same in both languages.

Two artifacts have to agree on one string literal and neither can import the other:

- The **producer**, ``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``,
  declares ``FALLBACK_GENERATED_BY`` (``:125``) and stamps it onto both
  ``generated_by`` and ``ai_model_used`` when a mapping falls back to substring
  matching (``:2861``, ``:2863``). P28 R3.1 requires exactly this: a fallback
  mapping must never claim the Bedrock model id.
- The **display**, ``dashboard-react/src/utils/formatting.ts``, declares
  ``FALLBACK_MODEL_SENTINEL`` for the same value so ``getModelDisplayName`` can
  render it as an explicit "no model used" statement instead of letting it reach
  the trailing truncation branch.

Before the display case existed the marker matched no model branch and
``"fallback_substring_match".split('.')`` had length 1, so it fell through to
``modelId.substring(0, 20) + '...'`` and rendered as ``fallback_substring_m...``
beneath a label reading "AI Model" — honest data presented as apparent corruption.
Fixing that created a **second literal for one value, across a language boundary**,
which is the drift ``LGR-1e135d2-12`` warns about ("re-measured, not cited" — a
figure or a literal in a second place is drift waiting to happen). A comment in
each file saying "keep these in sync" is not a mechanism, which is why this file
exists.

WHY THIS PARSES BOTH SOURCES AND TRANSCRIBES NEITHER
----------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**) is the governing record and its
rejection is the whole point, verbatim: *"Rejected transcribing the field list into
the test or into a JSON schema: a test written from the producer's output cannot
detect the producer drifting, because the output is its own oracle."* It is cited as
evidence rather than aspiration because it records **a live drift it caught** —
``CostRecommendation.type`` declaring ``'upgrade'`` while the agent emitted
``'upsize'``. ``LGR-cd58706-3`` (**template / ratified**) re-applies the same rule
and adds the reuse discipline this file follows below.

So the string ``fallback_substring_match`` is deliberately **not written down
anywhere in this file**. If it were, this test would pass while both sides drifted
away from it together, and — worse — it would pass while one side drifted *to* it,
which is the case a pin is for.

WHY ``ast`` ON THE PYTHON SIDE AND A REGEX ON THE TYPESCRIPT SIDE
-----------------------------------------------------------------
The asymmetry is inherited, not invented; both halves already exist in this
directory (``test_glue_target_prefix_cross_language.py`` makes the same pair of
choices for the same pair of reasons).

*Python with* ``ast``, following ``test_not_verified_reason_labels_cross_language.py``.
``LGR-b46a5bb-4`` (**template / ratified**) rejected substring matching for a
repository-wide invariant test because "it cannot tell source from prose", and that
applies here with force: ``FALLBACK_GENERATED_BY`` is named in a comment block
directly above its own declaration (``:123-124``) and in four further prose comments
in the same module (``:821``, ``:907``, ``:1229``, ``:1308``), any of which a
substring match could mistake for the declaration. Importing the module is worse
than unnecessary — it is a Lambda handler that reads configuration at import time —
so an import would need path surgery to read one string constant. ``ast`` reads the
declaration and nothing else.

*TypeScript with a regex*, following ``test_approval_window_cross_language.py``
(``LGR-69ca78b-2``, **template / ratified**: "the two artifacts cannot import each
other, so the agreement is asserted by parsing ``APPROVAL_TIMEOUT_SECONDS`` out of
the TypeScript rather than restating it — a second literal is the drift R6.6 exists
to prevent"). The match is anchored on the ``export const`` **declaration**, not on
a bare mention, so the constant's own JSDoc — which names it — cannot satisfy it.

``api_responses/helpers.py``'s frontend-type oracle is **not** reusable here, and
that is checked rather than assumed: ``read_interface`` / ``load_interfaces``
(``helpers.py:61``, ``:104``) parse ``interface`` declarations into
``{field: (required, ts_type)}``. This subject is an ``export const`` string
literal, which that reader has no notion of. ``LGR-cd58706-3`` made reuse
*mandatory where the reader fits* — its concern was an eighth divergent copy of one
parser — so declining it for a different grammar honours the record rather than
sidestepping it, and no new interface parser is added.

WHAT IS DELIBERATELY *NOT* PINNED HERE
--------------------------------------
``FALLBACK_MODEL_DISPLAY`` — the human-readable string the marker renders as — has
no Python counterpart and is asserted in
``dashboard-react/src/utils/__tests__/formatting.test.ts`` instead. Pinning it here
would invent a contract that does not exist: the backend has no opinion on the
wording, and a display string is free to change without the stored value moving.

The ``fallback_`` *prefix* rule shared with ``ApprovalArtifact.tsx:865`` is likewise
out of scope. It is a frontend-local generalisation over markers, not an agreement
with Python about a value, so it belongs in the frontend's own tests.

WHY THE COMPARISON IS EXACT EQUALITY
------------------------------------
``getModelDisplayName`` compares with ``===`` and the producer writes the constant
verbatim into DynamoDB — no normalisation on either side. A case-insensitive or
"contains" pin would therefore permit a **real** drift: the producer could stamp
``Fallback_Substring_Match`` while the display matched nothing and quietly resumed
rendering ``Fallback_Substring_M...``, which is the exact defect this pin protects
against. ``test_the_producer_writes_the_marker_unnormalised`` pins that premise, so
if the Python side ever normalises, this file fails and says which comparison to
revisit rather than silently permitting the gap to reopen.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on nothing. An extraction that
silently yielded ``None`` on both sides would make the equality assertion vacuously
true, so:

1. Both files must exist (``test_both_sources_are_readable``) — a moved or renamed
   module fails loudly instead of skipping.
2. Each extraction raises through an assertion naming what changed, rather than
   returning a default.
3. Each parsed value must be plausible for what it is — a snake_case token
   beginning ``fallback_`` — so a mis-anchored regex that captured a type
   annotation, a JSDoc fragment or an empty string fails.
4. Both sides are corroborated against their **use sites**, because pinning a
   declaration that nothing reads is the other way for this file to pass while
   meaning nothing. On the Python side the marker must reach ``ai_model_used`` —
   the field the formatter is given — by NAME rather than as an inlined copy. On
   the TypeScript side ``getModelDisplayName`` must actually compare against the
   constant; an inlined literal there would leave the declaration agreeing with
   Python forever while the live comparison drifted freely.

Validates: P28 Requirement 3.1 (display half)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PRODUCER = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py"
_FORMATTING_MODULE = _PROJECT_ROOT / "dashboard-react" / "src" / "utils" / "formatting.ts"

#: The Python constant name, and the TypeScript constant it must equal.
_PYTHON_MARKER_NAME = "FALLBACK_GENERATED_BY"
_TS_MARKER_NAME = "FALLBACK_MODEL_SENTINEL"

#: The persisted field the frontend formatter is handed, and the function that
#: formats it. Both are corroboration targets, not subjects of the pin.
_DISPLAYED_FIELD = "ai_model_used"
_TS_FORMATTER_NAME = "getModelDisplayName"

#: What an honest-provenance marker looks like: a snake_case token in the
#: ``fallback_`` family. Deliberately a shape, not the value itself.
_MARKER_SHAPE_RE = re.compile(r"^fallback_[a-z0-9_]+$")


def _ts_const_pattern(name: str) -> re.Pattern[str]:
    """An ``export const NAME[: type] = '<value>';`` declaration.

    Anchored on ``export const`` so a mention in a doc comment or a use site
    cannot satisfy it — this constant's own JSDoc names it twice. Both quote
    styles are accepted because prettier's choice is not the contract.
    """
    return re.compile(rf"export\s+const\s+{re.escape(name)}\s*(?::\s*[^=]+?)?=\s*(['\"])((?:[^'\"\\]|\\.)*)\1")


def _producer_module() -> ast.Module:
    """``lambda_function.py`` parsed, never imported.

    See the module docstring: it is a Lambda handler that reads configuration at
    import time, and this test needs one string constant out of it.
    """
    return ast.parse(_PRODUCER.read_text(encoding="utf-8"), filename=str(_PRODUCER))


def _python_constant(name: str) -> str:
    """A module-level ``name = "<literal>"`` from the producer, by AST.

    Raises through an assertion rather than returning a default, so a renamed or
    computed constant can never present as "nothing to compare".
    """
    for node in _producer_module().body:
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
            "lambda/agentic_data_ingestion_schema_mapping/lambda_function.py — it is now "
            f"{ast.dump(node.value)[:80]}. Extend this extraction rather than deleting the "
            "assertion: an unread Python side makes the comparison below vacuous."
        )
        return node.value.value

    raise AssertionError(
        f"{name} not found at module level in "
        "lambda/agentic_data_ingestion_schema_mapping/lambda_function.py. P28 R3.1's honest "
        "provenance marker was renamed or moved, so the dashboard's copy of the same value can "
        "no longer be checked against it. Fix the name here; do not drop the check."
    )


def _typescript_constant(name: str) -> str:
    """An ``export const`` string literal from the formatting module, by regex."""
    source = _FORMATTING_MODULE.read_text(encoding="utf-8")
    match = _ts_const_pattern(name).search(source)
    assert match is not None, (
        f"export const {name} declaration not found in "
        "dashboard-react/src/utils/formatting.ts — the display's copy of the marker was "
        "renamed, moved or reshaped (a template literal, a computed value, a re-export), so it "
        "can no longer be held against the Python producer. Fix the extraction here: two "
        "unpinned literals for one cross-language value is what this file exists to prevent."
    )
    return match.group(2)


def test_both_sources_are_readable():
    """Guard: a moved or renamed source must fail loudly, not skip."""
    assert _PRODUCER.is_file(), f"schema_mapping producer not found at {_PRODUCER}"
    assert _FORMATTING_MODULE.is_file(), f"formatting module not found at {_FORMATTING_MODULE}"


def test_both_literals_parse_to_plausible_values():
    """Anti-vacuity: each extraction recovers a real value of the right shape.

    Runs on its own so a degraded parse is reported as *this* test failing rather
    than as the pin below passing over ``None == None``.
    """
    python_marker = _python_constant(_PYTHON_MARKER_NAME)
    ts_marker = _typescript_constant(_TS_MARKER_NAME)

    for label, value in ((_PYTHON_MARKER_NAME, python_marker), (_TS_MARKER_NAME, ts_marker)):
        assert _MARKER_SHAPE_RE.match(value), (
            f"parsed {label} as {value!r}, which is not the shape of an honest-provenance "
            "marker (a snake_case token in the 'fallback_' family). The extraction is picking "
            "up something that is not the constant — an annotation, a comment, or a truncated "
            "match — so the comparison below would not be comparing markers."
        )


def test_the_marker_reaches_the_field_the_formatter_displays():
    """Corroboration (Python): the constant reaches ``ai_model_used`` by NAME.

    P28 R3.1's display half only exists because the marker is written to
    ``ai_model_used``, which is what ``getModelDisplayName`` is handed at all five
    call sites. If that assignment ever inlines the literal, the declaration pinned
    below would keep agreeing with TypeScript while the value actually persisted
    drifted freely — so the marker is required to arrive by name.
    """
    module = _producer_module()

    referencing_names = {
        name.id
        for node in ast.walk(module)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == _DISPLAYED_FIELD
        for name in ast.walk(value)
        if isinstance(name, ast.Name)
    }

    assert _PYTHON_MARKER_NAME in referencing_names, (
        f"no {_DISPLAYED_FIELD!r} dict entry in "
        "lambda/agentic_data_ingestion_schema_mapping/lambda_function.py obtains its value "
        f"from {_PYTHON_MARKER_NAME}; the names reaching that field are "
        f"{sorted(referencing_names) or '(none)'}. P28 R3.1 requires a fallback mapping to "
        f"stamp the marker onto {_DISPLAYED_FIELD} rather than claim a Bedrock model id, and "
        "the dashboard formats exactly that field. If the marker no longer reaches it by name, "
        "the pin below is pinning a constant the persisted record does not carry."
    )


def test_the_formatter_compares_against_the_typescript_constant():
    """Corroboration (TypeScript): ``getModelDisplayName`` reads the constant.

    An inlined literal inside the comparison would put the live rule outside this
    test's reach while leaving the exported declaration to agree with Python
    forever — the mirror image of the Python risk above.
    """
    source = _FORMATTING_MODULE.read_text(encoding="utf-8")

    formatter_start = source.find(f"export function {_TS_FORMATTER_NAME}")
    assert formatter_start != -1, (
        f"export function {_TS_FORMATTER_NAME} not found in "
        "dashboard-react/src/utils/formatting.ts. It is the only consumer of "
        f"{_TS_MARKER_NAME}, so without it the constant pinned below is pinned to nothing."
    )

    # Bounded at the next top-level declaration so a later function mentioning the
    # constant cannot satisfy this on the formatter's behalf.
    next_declaration = source.find("\nexport ", formatter_start + 1)
    body = source[formatter_start:] if next_declaration == -1 else source[formatter_start:next_declaration]

    assert _TS_MARKER_NAME in body, (
        f"{_TS_FORMATTER_NAME} no longer references {_TS_MARKER_NAME} in its body. Either the "
        "fallback case was deleted — which restores the original defect, rendering "
        "'fallback_substring_m...' under a label reading 'AI Model' — or the marker was "
        "inlined as a bare literal, which takes the live comparison outside this pin while "
        "the declaration keeps agreeing with Python. Restore the reference; do not drop this "
        "assertion."
    )


def test_the_producer_writes_the_marker_unnormalised():
    """The premise that makes exact equality the right comparison below.

    The marker is persisted verbatim: no ``.lower()``, no ``.strip()`` applied to
    the constant on its way to the record. The TypeScript side compares with
    ``===`` for the same reason. If the producer ever normalises, the pin below
    stops being sufficient on its own and the display has to normalise
    identically, so this fails and says so rather than letting the gap open
    silently.
    """
    module = _producer_module()

    normalised = {
        node.func.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == _PYTHON_MARKER_NAME
    }

    assert not normalised, (
        f"{_PYTHON_MARKER_NAME} is now transformed before use ({sorted(normalised)}). "
        "getModelDisplayName compares the persisted value with '===', so the two sides now "
        "disagree about anything in the gap between them — the display would stop recognising "
        "the marker and silently resume truncating it. Mirror the normalisation in "
        "dashboard-react/src/utils/formatting.ts and widen this test to pin both; do not "
        "delete it."
    )


def test_fallback_marker_agrees_across_the_language_boundary():
    """``FALLBACK_GENERATED_BY`` == ``FALLBACK_MODEL_SENTINEL``.

    Exact equality, deliberately: both sides handle the value verbatim, so any
    looser comparison here would permit a marker the producer writes and the
    display fails to recognise.

    Validates: P28 Requirement 3.1 (display half)
    """
    python_marker = _python_constant(_PYTHON_MARKER_NAME)
    ts_marker = _typescript_constant(_TS_MARKER_NAME)

    assert python_marker == ts_marker, (
        f"the fallback provenance marker has drifted: schema_mapping stamps {python_marker!r} "
        f"onto {_DISPLAYED_FIELD} ({_PYTHON_MARKER_NAME}) while the dashboard recognises "
        f"{ts_marker!r} (formatting.ts {_TS_MARKER_NAME}). Nothing raises on a mismatch — "
        "getModelDisplayName simply stops matching and falls through to its truncation branch, "
        "rendering the raw marker as 'fallback_substring_m...' beneath a label reading 'AI "
        "Model', which reads as data corruption to a user. This assertion is the only thing "
        "that notices. The producer owns the value (P28 R3.1's honest-provenance contract, "
        "pinned by tests/unit/lambdas/schema_mapping/test_schema_mapping_fallback_provenance.py) "
        "— so move the TypeScript constant to match it, never the other way round."
    )
