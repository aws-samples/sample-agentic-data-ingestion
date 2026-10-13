# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: every not-verified reason Python emits has a dashboard label.

Two artifacts have to agree on one vocabulary and neither can import the other:

- The **producers**, all three in
  ``lambda/agentic_data_ingestion_test_code/``, declare the
  ``not_verified_reason`` strings a check can carry:
  ``pyspark_to_pandas.REFUSAL_REASON_SCALAR_LITERAL`` (the owner of the third
  proxy reason), ``code_interpreter_runner``'s four ``NOT_VERIFIED_REASON_*``
  constants, and ``lambda_function``'s ``NO_TARGET_SCHEMA_REASON`` /
  ``SCALAR_LITERAL_RECEIVER_REASON``.
- The **consumer**, ``dashboard-react/src/components/approvals/ApprovalArtifact.tsx``,
  turns a reason into the sentence a reviewer reads, through
  ``NOT_VERIFIED_LABELS``. That map is the single source of the reason
  vocabulary in ``dashboard-react/src`` — no other file duplicates it.

When a producer gains a reason the map does not know, nothing breaks and
nothing fails: ``checkStatusLabel`` falls through to
``NOT_VERIFIED_LABEL_FALLBACK``, the bare string ``'not verified'``, and the
reviewer loses the half of the message that says *whose fault it was and
whether there is anything in the code to fix*. Per ``LGR-99050e6-1`` (template
/ ratified) the state has to be carried in TEXT because the indicator colour
alone fails WCAG 1.4.1 — so the fallback renders a row that is honest about
its state and silent about its cause.

**The gap this closes.** Completeness was enforced by prose until now. The
labels test on the TypeScript side enumerates reasons by hand, so a producer
gaining a reason falls to the fallback without failing anything — which is how
the two p33 harness reasons (``test_frame_truncated``, ``no_source_schema``)
went unlabelled for a release.

Why this parses the TypeScript instead of restating the six keys: a second
copy of the vocabulary is the drift this exists to prevent.
``LGR-28b4d75-5`` (template / ratified) is the precedent — a contract test
derives its expectations from the other side's **source**, because "a test
written from the producer's output cannot detect the producer drifting, the
output is its own oracle". The technique is also already in this directory:
``test_approval_window_cross_language.py`` (``LGR-69ca78b-2``, template /
ratified) reads ``APPROVAL_TIMEOUT_SECONDS`` out of the CDK TypeScript with a
regex and asserts Python against it, for the same stated reason — the two
artifacts cannot import each other.

``api_responses/helpers.py``'s frontend-type oracle is not reusable here: it
parses ``export interface`` declarations, and this subject is a ``const``
object literal.

THE CONTRACT, STATED IN ONE DIRECTION ONLY
------------------------------------------
Every reason string a Python producer declares MUST have a key in
``NOT_VERIFIED_LABELS``. The reverse is deliberately **not** asserted: a label
with no producer is inert — it renders nothing until some producer emits that
reason — while a producer with no label degrades a live screen. Asserting
TS ⊆ Python would also narrow the frontend to suit a Python reader, which
``LGR-99050e6-5`` (template / ratified) rejects outright, and would fail on a
label staged ahead of its producer.

THE SIX-VERSUS-FIVE ASYMMETRY, WHICH IS NOT A BUG IN EITHER MAP
---------------------------------------------------------------
``NOT_VERIFIED_LABELS`` has **six** keys. ``lambda_function``'s own
``NOT_VERIFIED_REASON_LABELS`` has **five**, and the missing one is
``no_target_schema``. Both are correct, because they answer different
questions:

- The TypeScript map answers *"this check says ``verified: false`` — what do I
  tell the reviewer?"*. Every ``verified: false`` check reaches it, including
  the ``schema_compliance`` entry that carries ``no_target_schema``.
- The Python map answers *"nothing was executed — why not?"*. It feeds
  ``execution_verification_note``, which is keyed on
  ``EXECUTION_REPLACING_CHECK_NAMES``. ``no_target_schema`` belongs to a run
  whose transform **did** execute — the comparison had no target schema to
  compare against — so labelling that run "never executed" would be a new
  false claim made while removing one. ``lambda_function.py:287-289`` says so
  at the map.

So the Python set that must be a subset of the TypeScript keys is **every
declared reason constant**, not the Python label map's keys. Nothing here
"fixes" either map to make a naive equality hold, and
``test_python_reason_label_map_omits_only_the_executed_run_reason`` pins the
asymmetry so a later reader cannot tidy it away as an oversight.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on an empty set: an empty
``ts_keys`` makes ``python_reasons <= ts_keys`` false rather than true, but an
empty ``python_reasons`` would make it vacuously true. Four guards, in
increasing strength:

1. The file must exist (``test_dashboard_label_source_is_readable``) — a moved
   or renamed component fails loudly instead of skipping.
2. Both extractions must be non-empty, each with a message naming what
   changed.
3. Each parsed key must be a plausible reason (snake_case, non-empty label
   text), and the key-only and key-plus-label regexes must recover the **same**
   key set — so a comment line inside the object literal, or an entry whose
   label cannot be parsed, fails rather than being dropped.
4. Two independent derivations of the Python vocabulary must agree, up to the
   one documented difference: the AST scan over the producer directory, and
   ``NOT_VERIFIED_REASON_LABELS``' own keys. A silently degraded scan
   disagrees with the map and fails.

The Python side is read with ``ast``, not with a substring or regex scan, and
not by importing the modules. ``LGR-b46a5bb-4`` (template / ratified) rejected
substring matching for a repository-wide invariant test on the ground that it
cannot tell source from prose, and the same applies here: these constants are
named in dozens of comment lines. Importing is worse than unnecessary — every
test that imports ``lambda_function`` stubs ``code_interpreter_runner`` with a
bare ``MagicMock``, so an imported constant arrives as an auto-attribute mock
and a comparison against it silently never matches. ``ast`` cannot be fooled
that way, needs no stub dict, and mutates no ``sys.modules``.

The scan is scoped to a **directory**, not to an enumerated file list, so a
fourth producer module in ``test_code/`` is covered the day it appears.

NOT DUPLICATED HERE
-------------------
``tests/unit/lambdas/test_code/test_not_verified_classification_prop.py::test_the_three_spellings_of_the_scalar_literal_reason_agree``
already holds the three Python spellings of ``scalar_literal_receiver``
against each other as real objects. This file is about the Python↔TypeScript
seam only; the same string arriving from two spellings collapses into one
member of a set here, which is exactly right for the question being asked.

Validates: p40 Requirements R4
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APPROVAL_ARTIFACT = _PROJECT_ROOT / "dashboard-react" / "src" / "components" / "approvals" / "ApprovalArtifact.tsx"
_PRODUCER_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_test_code"

#: The label map's object literal, from the declaration to its closing ``};``.
#: Anchored on the ``const`` declaration rather than on a bare mention, so a
#: reference elsewhere in the file cannot satisfy it.
_LABEL_BLOCK_RE = re.compile(r"const NOT_VERIFIED_LABELS[^=]*=\s*\{([\s\S]*?)\n\};")

#: One entry: a two-space-indented key, and the single-quoted label that
#: follows it — on the same line, or on the next one where prettier wrapped it.
#: The two-space anchor is what skips the ``//`` comment lines inside the
#: literal.
_LABEL_ENTRY_RE = re.compile(r"^\s{2}(\w+):\s*(?:\n\s+)?'((?:[^'\\]|\\.)*)'", re.MULTILINE)

#: Keys only, deliberately a weaker pattern than ``_LABEL_ENTRY_RE``. The two
#: are cross-checked against each other: anything one sees and the other does
#: not is a parse the test refuses to trust.
_LABEL_KEY_RE = re.compile(r"^\s{2}(\w+):", re.MULTILINE)

_FALLBACK_RE = re.compile(r"const NOT_VERIFIED_LABEL_FALLBACK\s*=\s*'([^']*)'")

#: A ``not_verified_reason`` value, as the producers spell them.
_REASON_VALUE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_PYTHON_LABEL_MAP_NAME = "NOT_VERIFIED_REASON_LABELS"


def _dashboard_labels() -> dict[str, str]:
    """``{reason: label}`` read out of ``ApprovalArtifact.tsx``.

    Raises through an assertion rather than returning ``{}`` at every step
    where the source could have moved, so an unparseable file can never
    present as "no reasons to check".
    """
    source = _APPROVAL_ARTIFACT.read_text(encoding="utf-8")

    block = _LABEL_BLOCK_RE.search(source)
    assert block is not None, (
        "NOT_VERIFIED_LABELS declaration not found in "
        "dashboard-react/src/components/approvals/ApprovalArtifact.tsx — the "
        "label map was renamed, moved or reshaped, so the Python reason "
        "constants can no longer be checked against it. Fix the extraction "
        "here rather than deleting this test: an unchecked label map is how "
        "the p33 harness reasons went unlabelled for a release."
    )
    body = block.group(1)

    entries = {key: label for key, label in _LABEL_ENTRY_RE.findall(body)}
    keys_only = set(_LABEL_KEY_RE.findall(body))

    assert entries, (
        "NOT_VERIFIED_LABELS parsed to ZERO entries. The declaration was "
        "found but no `key: 'label'` pair matched, so every assertion below "
        "would pass against an empty set. The entry shape changed (double "
        "quotes, a template literal, a computed key) and the extraction has "
        "to change with it."
    )
    assert keys_only == set(entries), (
        f"the two extractions disagree: keys-only found {sorted(keys_only)}, "
        f"key-plus-label found {sorted(entries)}. Something in the object "
        "literal looks like a key but has no readable label (or vice versa), "
        "so the parse cannot be trusted to be complete."
    )

    for key, label in entries.items():
        assert _REASON_VALUE_RE.match(key), (
            f"parsed {key!r} as a reason key, which is not the snake_case "
            "shape a not_verified_reason has — the extraction is picking up "
            "something that is not an entry."
        )
        assert label.strip(), f"NOT_VERIFIED_LABELS[{key!r}] is empty; a not-verified row would render nothing."

    return entries


def _declared_python_reasons() -> dict[str, str]:
    """``{"<module>.<CONSTANT>": value}`` for every producer reason constant.

    Module-level assignments in ``lambda/agentic_data_ingestion_test_code/``
    whose name contains ``REASON`` and whose value is a string literal. An
    assignment of that shape whose value is a bare name — the deliberate alias
    ``NOT_VERIFIED_REASON_SCALAR_LITERAL = REFUSAL_REASON_SCALAR_LITERAL`` — is
    resolved against the literals rather than skipped, so an alias bound to
    something this scan cannot see fails loudly instead of hiding a reason.
    """
    sources = sorted(_PRODUCER_DIR.glob("*.py"))
    assert sources, f"no producer modules found under {_PRODUCER_DIR} — the reason constants moved."

    literals: dict[str, str] = {}
    aliases: dict[str, str] = {}

    for path in sources:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            for name in names:
                if "REASON" not in name:
                    continue
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    literals[f"{path.stem}.{name}"] = node.value.value
                elif isinstance(node.value, ast.Name):
                    aliases[f"{path.stem}.{name}"] = node.value.id

    by_constant_name = {qualified.split(".", 1)[1]: value for qualified, value in literals.items()}
    for qualified, target in aliases.items():
        assert target in by_constant_name, (
            f"{qualified} is bound to {target}, which is not a reason string "
            "literal this scan found. The alias may be carrying a reason the "
            "dashboard has never been told about — resolve it here rather "
            "than letting it drop out of the vocabulary silently."
        )
        literals[qualified] = by_constant_name[target]

    assert literals, (
        "no reason constants found in "
        "lambda/agentic_data_ingestion_test_code/ — the `*REASON*` naming "
        "convention changed, so the subset assertion below would pass "
        "vacuously against an empty set."
    )
    return literals


def _python_reason_label_keys() -> set[str]:
    """The keys of ``lambda_function.NOT_VERIFIED_REASON_LABELS``.

    A second, near-independent derivation of the Python vocabulary: four of
    its five keys are plain string literals in the dict, and the fifth is the
    ``SCALAR_LITERAL_RECEIVER_REASON`` name, resolved against the constants.
    """
    path = _PRODUCER_DIR / "lambda_function.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    by_constant_name = {qualified.split(".", 1)[1]: value for qualified, value in _declared_python_reasons().items()}

    for node in module.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target: str | None = node.target.id
            value: ast.expr | None = node.value
        elif isinstance(node, ast.Assign):
            target = next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
            value = node.value
        else:
            continue
        if target != _PYTHON_LABEL_MAP_NAME or not isinstance(value, ast.Dict):
            continue

        keys: set[str] = set()
        for key in value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            elif isinstance(key, ast.Name):
                assert key.id in by_constant_name, (
                    f"{_PYTHON_LABEL_MAP_NAME} is keyed on {key.id}, which is not a reason constant this scan found."
                )
                keys.add(by_constant_name[key.id])
            else:
                raise AssertionError(
                    f"{_PYTHON_LABEL_MAP_NAME} has a key this test cannot read "
                    f"({'**-unpacking' if key is None else ast.dump(key)}); the "
                    "corroboration below would be incomplete without failing."
                )
        assert keys, f"{_PYTHON_LABEL_MAP_NAME} parsed to zero keys."
        return keys

    raise AssertionError(
        f"{_PYTHON_LABEL_MAP_NAME} not found in "
        "lambda/agentic_data_ingestion_test_code/lambda_function.py — the "
        "execution-replacing reason labels moved, and the asymmetry this file "
        "documents can no longer be checked."
    )


def test_dashboard_label_source_is_readable():
    """Guard: a moved or renamed component must fail loudly, not skip."""
    assert _APPROVAL_ARTIFACT.is_file(), f"dashboard source not found at {_APPROVAL_ARTIFACT}"


def test_parsed_dashboard_labels_are_neither_empty_nor_comment_noise():
    """Anti-vacuity: the parse recovers real entries, and its escape hatch survives.

    Every guard in ``_dashboard_labels`` runs here on its own, so a degraded
    extraction is reported as *this* test failing rather than as the contract
    test below passing over an empty set.
    """
    labels = _dashboard_labels()
    assert len(labels) > 1, (
        f"NOT_VERIFIED_LABELS parsed to {len(labels)} entry — too few to be the real map; "
        "the extraction is matching one line and missing the rest."
    )

    source = _APPROVAL_ARTIFACT.read_text(encoding="utf-8")
    fallback = _FALLBACK_RE.search(source)
    assert fallback is not None and fallback.group(1).strip(), (
        "NOT_VERIFIED_LABEL_FALLBACK is gone. It is residual by design, not dead: an "
        "unrecognised reason must still render its state in text (LGR-99050e6-1), and "
        "without it a check the map does not know renders an empty Status cell."
    )


def test_every_python_not_verified_reason_has_a_dashboard_label():
    """Producer reason constants ⊆ ``NOT_VERIFIED_LABELS`` keys.

    Validates: p40 Requirements R4
    """
    labels = _dashboard_labels()
    declared = _declared_python_reasons()

    unlabelled = {qualified: value for qualified, value in declared.items() if value not in labels}
    assert not unlabelled, (
        "these Python reason constants have no key in NOT_VERIFIED_LABELS, so a check "
        "carrying one renders the bare fallback 'not verified' with no cause: "
        f"{sorted(f'{k} = {v!r}' for k, v in unlabelled.items())}. "
        "Add an entry to ApprovalArtifact.tsx stating whose fault the non-verification is "
        "and whether there is anything in the generated code to fix — that missing half of "
        "the sentence is the whole defect. Do NOT relax this test: the two p33 harness "
        "reasons shipped unlabelled for a release for exactly the want of it."
    )


def test_python_reason_label_map_omits_only_the_executed_run_reason():
    """The six-vs-five asymmetry, asserted rather than commented.

    Two claims, and both are about keeping the maps as they are:

    * ``NOT_VERIFIED_REASON_LABELS`` covers every declared reason **except**
      ``no_target_schema``, whose run executed — so the Python map must not
      grow that key, and must not shrink by any other.
    * ``no_target_schema`` is nonetheless labelled on the dashboard, because
      the TypeScript map answers a wider question: any ``verified: false``
      check reaches it.

    Together they also corroborate the AST scan. Two derivations of the Python
    vocabulary agreeing up to one documented difference is what makes an empty
    or degraded scan impossible to mistake for a pass.

    Validates: p40 Requirements R4
    """
    declared_values = set(_declared_python_reasons().values())
    python_label_keys = _python_reason_label_keys()
    dashboard_keys = set(_dashboard_labels())

    executed_run_reason = "no_target_schema"
    assert executed_run_reason in declared_values, (
        f"{executed_run_reason!r} is no longer a declared reason; this test's subject moved."
    )

    assert declared_values - python_label_keys == {executed_run_reason}, (
        "the Python execution-replacing label map no longer omits exactly "
        f"{executed_run_reason!r}: missing {sorted(declared_values - python_label_keys)}. "
        "That omission is deliberate (lambda_function.py:287-289) — the reason belongs to a "
        "run whose transform DID execute, and execution_verification_note must not say "
        "'Transform NOT executed' about it. If a genuinely execution-replacing reason has "
        "been added, give it a label there; do not add no_target_schema to make this pass."
    )
    assert not python_label_keys - declared_values, (
        "the Python label map is keyed on something no producer declares: "
        f"{sorted(python_label_keys - declared_values)} — a typo'd key labels nothing."
    )
    assert executed_run_reason in dashboard_keys, (
        f"{executed_run_reason!r} lost its dashboard label. It is absent from the PYTHON "
        "label map on purpose and present in the TypeScript one on purpose: the two answer "
        "different questions, and a schema_compliance check carrying it is still a "
        "`verified: false` row a reviewer has to be able to read."
    )
