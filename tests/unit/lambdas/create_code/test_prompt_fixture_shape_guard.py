# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Divergence guard: the prompt fixtures must not drift from template or producer (p52 C2).

**Why this file exists.** p52 task 2.1 fixed the transform-prompt budget measurement
*once*, by adding `_make_production_mappings` beside the pre-existing `_make_mappings`.
Nothing stops the same drift recurring the next time someone copies the older fixture:
`_make_mappings` carries `transform`, a key **no producer in this repository ever
writes**, and omits `confidence`, which every producer always writes. A budget built on
it renders a prompt no pipeline sends, is green, and guarantees nothing. That is the
defect p52 exists to remove, and this file is what keeps it removed.

Two halves, deriving from the two artefacts that can drift independently:

1. **Template-derived** — every mapping-record key the template *branches on* is
   covered by at least one prompt fixture in `test_transform_prompt.py`. A new branch
   in the template fails this guard until a fixture exercises it (R2.1, R2.3).
2. **Producer-derived** — the Production_Shape fixture's key set equals the key set the
   producer literally writes, discovered **by AST over key shape** rather than
   transcribed (R2.2).

────────────────────────────────────────────────────────────────────────────
HOW R2.1 IS READ HERE, AND WHY. Read this before concluding the guard is weaker
than the requirement.
────────────────────────────────────────────────────────────────────────────
R2.1 as literally worded — "every mapping-record key the template branches on is
present in at least one **Production_Shape** fixture" — is **unsatisfiable**, and p52
gate 1.1 flagged it as such rather than quietly weakening it. The template branches on
five keys (`_template_branch_keys()` below discovers them; they are `source_type`,
`target_type`, `transformation`, `transform`, `confidence` on this tree). Three of
those — `source_type`, `target_type`, `transform` — are **precisely the keys production
never writes**. Putting `transform` into a Production_Shape fixture would make that
fixture *not* Production_Shape, and would re-create the exact defect p52 removes.

So the requirement is split into the two assertions it must have meant:

- **Union coverage** (`test_every_template_branch_key_is_covered_by_a_prompt_fixture`)
  — every discovered branch key appears in at least one of the module's prompt fixtures,
  Fixture_Shape ∪ Production_Shape. This is what "no template branch is unexercised"
  actually means, and it is the load-bearing half: a *new* template branch fails here.
- **Production_Shape-specific coverage**
  (`test_template_keys_production_writes_are_covered_by_production_shape`) — the branch
  keys production *does* write (computed as the intersection, not transcribed) must be
  covered by a **Production_Shape** fixture specifically. On this tree that is
  `transformation` and `confidence`, and it is R2.4's letter: the primary
  `transformation` branch renders, not only the `elif transform` fallback. This is the
  half Fixture_Shape must **not** be allowed to satisfy, and
  `test_fixture_shape_cannot_satisfy_the_transformation_branch` pins that it cannot.

This is a *reading* of R2.1, not a relaxation of it: nothing the requirement can be
satisfied by is left unasserted. R2.1's wording could stand amending to say so
directly.

────────────────────────────────────────────────────────────────────────────
DO NOT ADD PROSE TO `transform_generation.j2`.
────────────────────────────────────────────────────────────────────────────
This file reads the template with a **plain file read**, not through the Jinja loader,
because half 1 needs the raw source — comments and all — to scope its regex to the
mapping-line loop. `tests/unit/lambdas/test_code/test_converter_vocabulary_closure_prop.py:82-83`
reads the same template raw for the same reason, and harvests function names out of it
by regex (`:125-141`: the parenthesised `pyspark.sql.functions (...)` list, an
`is imported as X` instruction, and `f(col(` illustrations). Prose added to the
template's comments is picked up by that harvest — it has already injected two
non-existent function names once. So: read this template, never write to it. p52 edits
no prompt template at all, which is what keeps `LGR-a419843-2`'s fence (the source
schema stays out of the transform prompt) honoured by construction rather than by
assertion.

────────────────────────────────────────────────────────────────────────────
WHY HALF 2 IS AN AST WALK OVER KEY SHAPE, AND NOT ANY OF THE EASIER THINGS.
────────────────────────────────────────────────────────────────────────────
`LGR-b46a5bb-4` and `LGR-9e76249-5` (both **template / ratified**) both rule AST over
substring matching, the second on the ground that substring matching "fails in both
directions on real inputs". Here it would fail concretely: the literal
`"transformation": "direct"` appears three times in the producer module — the record
write, a prompt example, and a `.get()` default.

`LGR-7d19eca-2` (**standing invariant**) goes further and rejects *enumerating writers
by file and line*, because such a list rots on the next edit. So this guard discovers
writers **by key shape** and asserts the count, rather than naming a path:

    every `ast.Call` whose func is an `ast.Attribute` with `attr == "append"`,
    exactly one positional arg, no keywords, that arg an `ast.Dict`, whose
    string-Constant keys are a **superset** of {"target_field", "source_field"}.

Measured on this tree: **exactly 1 hit**, at
`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1218`
(the `.append(` call; its dict literal opens at `:1219` and its keys sit at
`:1220-1227`), carrying
`{confidence, notes, source_field, target_field, transformation}`.

Three collision candidates are excluded, each verified rather than assumed:

- `:1091`, the prompt example, is **not an `ast.Dict` node at all** — it lives inside
  the f-string whose `JoinedStr` starts at `:1070`. Dict-based walks are structurally
  blind to it, so that half of the discrimination is free.
- the `.get()` defaults are three calls (`:1712`, `:1713`, `:1718`) and none is a Dict
  key or value.
- the real hazard is **`:1715`** — `field_mapping_details` inside
  `mapping_visualization`, a genuine `ast.Dict` carrying the **identical five keys in a
  different order**. It is excluded because it is not an `.append(...)` argument. Keys
  are therefore compared **by `set`, never by list order**.

**The hit count is asserted to be exactly 1, and 0 fails as loudly as 2.** The
symmetric failure to the one `LGR-7d19eca-2` rejects is a rename silently yielding zero
hits and a vacuously green guard. A second writer appearing is a real finding about the
repository, not noise to absorb.

Only the producer's **file path** is read. `schema_mapping` is deliberately **not
imported**: that avoids its boto3-at-import cost and the `import lambda_function`
collision hazard that a `sys.path`-injected import of a Lambda entry point carries.
Mechanics lifted from `test_extract_field_mappings_characterization.py:79-80`, which
uses the same `parents[4]` anchor.

Pure: no AWS, no moto, no network. Reads three kinds of file and renders the real
template.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**
"""

import ast
import os
import re
import sys
from pathlib import Path

# Same shared-path insert `test_transform_prompt.py:8` uses, so the real
# `prompt_template_loader` renders the real template.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

from prompt_template_loader import load_prompt_template

from .test_transform_prompt import _make_mappings, _make_production_mappings

#: `agentic_data_ingestion/`. Verified anchor, shared with
#: `test_extract_field_mappings_characterization.py:79-80`.
_PROJECT_ROOT = Path(__file__).resolve().parents[4]

_TEMPLATE_PATH = _PROJECT_ROOT / "shared" / "prompt_templates" / "transform_generation.j2"

#: The first-party source roots a mapping-record writer could live in. Scoped sweep,
#: no allowlist of files — `LGR-b46a5bb-4`'s shape.
_PRODUCER_ROOTS = ("lambda", "shared", "agentcore")

_SKIP_PARTS = frozenset({"__pycache__", "cdk.out", "node_modules", ".venv", "build"})

#: The two keys that make a dict literal a mapping record. Not the full key set — the
#: full set is what this guard *discovers*, so anchoring on it would be circular.
_RECORD_ANCHOR = frozenset({"target_field", "source_field"})

#: `m.get('<key>')` — the template's branch predicate on a mapping record.
_BRANCH_KEY = re.compile(r"m\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)")


# ──────────────────────────────────────────────────────────────────────
# Half 1 — template-derived
# ──────────────────────────────────────────────────────────────────────


def _mapping_line_loop_body() -> str:
    """The raw source between `{% for m in mappings %}` and its `{% endfor %}`.

    Scoped deliberately. The template says "mappings" in prose elsewhere (the
    target-schema comment block, and `:199`), and the whole file contains other
    `{% for %}` loops over other variables — an unscoped regex would harvest keys that
    are not mapping-record branches. The loop is located rather than sliced by line
    number so the guard survives the template gaining or losing lines above it.
    """
    source = _TEMPLATE_PATH.read_text(encoding="utf-8")

    opens = [match.start() for match in re.finditer(r"{%-?\s*for\s+m\s+in\s+mappings\s*-?%}", source)]
    assert len(opens) == 1, (
        f"expected exactly one `{{% for m in mappings %}}` loop in {_TEMPLATE_PATH.name}, found "
        f"{len(opens)}. The mapping line has moved or been duplicated; this guard's scoping "
        f"assumption no longer holds and half 1 would harvest the wrong keys."
    )
    start = source.index("%}", opens[0]) + 2
    end = source.index("{% endfor %}", start)
    return source[start:end]


def _template_branch_keys() -> set[str]:
    """Mapping-record keys the template branches on, read from the raw template."""
    body = _mapping_line_loop_body()
    keys = set(_BRANCH_KEY.findall(body))
    assert keys, (
        f"no `m.get('<key>')` branch found in {_TEMPLATE_PATH.name}'s mapping-line loop. Either the "
        f"template stopped branching on mapping keys, or it now branches in a form this regex does "
        f"not match (e.g. `m['key']` or `'key' in m`). Either way half 1 has gone vacuous and must "
        f"be re-derived, NOT deleted. Loop body read:\n{body}"
    )
    return keys


def _fixture_keys(records: list) -> set[str]:
    """Union of keys across every record in a fixture.

    Union, not the first record's keys: `_make_mappings` cycles `transform` through
    `None` values, and a fixture is free to vary shape across records.
    """
    keys: set[str] = set()
    for record in records:
        keys |= set(record)
    return keys


def test_every_template_branch_key_is_covered_by_a_prompt_fixture():
    """R2.1, R2.3: no template branch goes unexercised by the module's fixtures.

    Coverage is over the **union** of `test_transform_prompt.py`'s two prompt fixtures,
    Fixture_Shape ∪ Production_Shape. See this module's docstring for why the union is
    the only satisfiable reading of R2.1 — three of the five branch keys are keys
    production never writes, so demanding a Production_Shape fixture carry them would
    demand the fixture stop being Production_Shape.

    The Production_Shape-specific half of R2.1/R2.4 is asserted separately, in
    `test_template_keys_production_writes_are_covered_by_production_shape`.

    **Validates: Requirements 2.1, 2.3**
    """
    branch_keys = _template_branch_keys()
    fixture_shape = _fixture_keys(_make_mappings(6))
    production_shape = _fixture_keys(_make_production_mappings(6))
    covered = fixture_shape | production_shape

    uncovered = sorted(branch_keys - covered)
    assert not uncovered, (
        f"transform_generation.j2 branches on mapping-record key(s) {uncovered} that NO prompt "
        f"fixture in test_transform_prompt.py carries, so that branch is never rendered by any "
        f"budget assertion. Add the key to whichever fixture is honest for it: `_make_mappings` if "
        f"no producer writes it (Fixture_Shape), `_make_production_mappings` ONLY if a producer "
        f"really does write it — in which case half 2 of this guard will tell you so. "
        f"Template branches on: {sorted(branch_keys)}. "
        f"Fixture_Shape carries: {sorted(fixture_shape)}. "
        f"Production_Shape carries: {sorted(production_shape)}."
    )


def test_the_template_still_has_a_primary_and_a_fallback_transformation_branch():
    """R2.4's premise: the two-branch structure the guard below relies on still exists.

    R2.4 is about the *primary* `transformation` branch rendering rather than only the
    `elif transform` fallback. That distinction only exists while the template carries
    both. Asserted structurally, from the template, so R2.4's assertion below cannot
    quietly become tautological if the fallback is removed.

    These two names come from R2.4 itself, not from the producer — `LGR-28b4d75-5`'s
    objection is to transcribing the *producer's* key set, which half 2 derives.

    **Validates: Requirements 2.4**
    """
    body = _mapping_line_loop_body()
    assert "m.get('transformation')" in body, (
        "the primary `transformation` branch is gone from transform_generation.j2's mapping line. "
        "R2.4 is about that branch rendering; re-derive R2.4 before deleting this assertion."
    )
    assert "elif m.get('transform')" in body, (
        "the `elif m.get('transform')` fallback is gone from transform_generation.j2's mapping "
        "line. If that is deliberate, `_make_mappings` (Fixture_Shape) now renders no hint at all "
        "and every budget figure built on it has moved — re-measure before adjusting this guard."
    )


# ──────────────────────────────────────────────────────────────────────
# Half 2 — producer-derived, by AST over key shape
# ──────────────────────────────────────────────────────────────────────


def _mapping_record_writers() -> list[tuple[str, int, frozenset]]:
    """Every literal mapping-record write in first-party source, discovered by key shape.

    Returns `(path relative to the project root, lineno of the `.append(` call, key set)`.

    Discovery, not enumeration — see this module's docstring for the predicate and for
    why `LGR-7d19eca-2` forbids the file-and-line list this replaces. A file that fails
    to parse is reported rather than skipped: a `SyntaxError` under `lambda/` or
    `shared/` would hide a writer.
    """
    writers: list[tuple[str, int, frozenset]] = []
    unparseable: list[str] = []

    for root_name in _PRODUCER_ROOTS:
        root = _PROJECT_ROOT / root_name
        assert root.is_dir(), f"expected producer root {root} to exist; the sweep would be silently empty"
        for path in sorted(root.rglob("*.py")):
            if _SKIP_PARTS & set(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                unparseable.append(f"{path.relative_to(_PROJECT_ROOT)}: {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute) or node.func.attr != "append":
                    continue
                if len(node.args) != 1 or node.keywords:
                    continue
                argument = node.args[0]
                if not isinstance(argument, ast.Dict):
                    continue
                # Compare by SET. `:1715`'s field_mapping_details carries the identical
                # five keys in a different order and is excluded structurally (not an
                # `.append` argument), but order must never be load-bearing here.
                keys = frozenset(
                    key.value for key in argument.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
                if keys >= _RECORD_ANCHOR:
                    writers.append((str(path.relative_to(_PROJECT_ROOT)), node.lineno, keys))

    assert not unparseable, (
        "these first-party source files could not be parsed, so the mapping-record sweep is "
        f"incomplete and its count means nothing: {unparseable}"
    )
    return writers


def _sole_mapping_record_writer() -> tuple[str, int, frozenset]:
    """The one literal mapping-record write, or a failure naming what was found."""
    writers = _mapping_record_writers()
    assert len(writers) == 1, (
        f"expected EXACTLY ONE literal mapping-record write across {list(_PRODUCER_ROOTS)}, found "
        f"{len(writers)}: {[(path, line, sorted(keys)) for path, line, keys in writers]}.\n"
        f"ZERO is as much a failure as two: it means the producer was renamed or restructured and "
        f"this guard has gone vacuous, which is the failure `LGR-7d19eca-2` (standing invariant) "
        f"exists to prevent — it rejected enumerating writers by file and line precisely because "
        f"such a list rots silently.\n"
        f"TWO OR MORE is a real finding about the repository, not a merge conflict to absorb: a "
        f"second literal writer means the Production_Shape key set has a second oracle, and the "
        f"two must be reconciled before this guard can mean anything. Do NOT relax the count."
    )
    return writers[0]


def test_exactly_one_producer_writes_mapping_records_literally():
    """R2.2: the Production_Shape oracle is discoverable, and there is exactly one of it.

    Discovered by key shape rather than by path, per `LGR-7d19eca-2`. Fails on 0 as
    loudly as on 2 — see `_sole_mapping_record_writer` for why each direction matters.

    **Validates: Requirements 2.2**
    """
    path, lineno, keys = _sole_mapping_record_writer()

    # Reported, not asserted: pinning the path would be the enumeration
    # `LGR-7d19eca-2` rejects. The count is the assertion; the location is the
    # diagnostic a failure needs.
    assert keys, f"the mapping-record writer at {path}:{lineno} has no string keys"


def test_production_shape_fixture_key_set_equals_the_producers():
    """R2.2: the fixture's key set is derived from the producer, not transcribed.

    Equality, not superset, in both directions:

    - a key the producer writes and the fixture omits is the `confidence` defect p52
      exists to remove, one generation later;
    - a key the fixture carries and the producer does not is the `transform` defect, and
      would put `_make_production_mappings` back where `_make_mappings` is.

    `notes` is in the set even though the template never reads it: the fixture is the
    *producer's* shape, and an unread key costs zero tokens.

    **Validates: Requirements 2.2**
    """
    path, lineno, producer_keys = _sole_mapping_record_writer()
    fixture_keys = frozenset(_fixture_keys(_make_production_mappings(3)))

    assert fixture_keys == producer_keys, (
        f"`_make_production_mappings` no longer matches the producer at {path}:{lineno}.\n"
        f"  producer writes: {sorted(producer_keys)}\n"
        f"  fixture carries: {sorted(fixture_keys)}\n"
        f"  producer-only (fixture is missing these — the `confidence` defect again): "
        f"{sorted(producer_keys - fixture_keys)}\n"
        f"  fixture-only (invented — the `transform` defect again): "
        f"{sorted(fixture_keys - producer_keys)}\n"
        f"Fix the FIXTURE to match the producer, never the reverse: the producer is the oracle."
    )


# ──────────────────────────────────────────────────────────────────────
# R2.4 — the primary `transformation` branch, and only Production_Shape may satisfy it
# ──────────────────────────────────────────────────────────────────────


def test_template_keys_production_writes_are_covered_by_production_shape():
    """R2.1, R2.4: the branch keys production writes are covered by a Production_Shape fixture.

    This is the half of R2.1 that Fixture_Shape must **not** be able to satisfy, and it
    is R2.4's letter. The expected set is **computed** — the intersection of the keys the
    template branches on with the keys the producer writes — so neither side is
    transcribed. On this tree it resolves to `{confidence, transformation}`.

    **Validates: Requirements 2.1, 2.4**
    """
    branch_keys = _template_branch_keys()
    _, _, producer_keys = _sole_mapping_record_writer()
    production_shape = frozenset(_fixture_keys(_make_production_mappings(3)))

    must_be_production_shaped = branch_keys & producer_keys
    assert must_be_production_shaped, (
        "the template branches on NO key the producer writes, so the production render exercises "
        "no mapping branch at all. That is either a template change or a producer change, and "
        "either way the budget assertions have stopped measuring the mapping line."
    )

    uncovered = sorted(must_be_production_shaped - production_shape)
    assert not uncovered, (
        f"key(s) {uncovered} are branched on by transform_generation.j2 AND written by the "
        f"producer, but are absent from the Production_Shape fixture — so the production budget "
        f"assertions render a prompt missing them. Covering these with `_make_mappings` "
        f"(Fixture_Shape) does NOT satisfy this: the point is that the PRODUCTION render takes "
        f"these branches. Template branches on {sorted(branch_keys)}; producer writes "
        f"{sorted(producer_keys)}; Production_Shape carries {sorted(production_shape)}."
    )


def test_the_primary_transformation_branch_renders_not_the_fallback():
    """R2.4: the production render reaches `if m.get('transformation')`, not the `elif`.

    Rendered rather than reasoned about, and with a record carrying **both** keys so the
    two branches are distinguishable in the output: the primary value must appear and
    the fallback value must not. A test that only asserted "the hint text is in the
    prompt" would pass through either branch and prove nothing about which one fired.

    **Validates: Requirements 2.4**
    """
    both = _make_production_mappings(1, hint="PRIMARY_BRANCH_RENDERED")
    both[0]["transform"] = "FALLBACK_BRANCH_RENDERED"

    prompt = load_prompt_template("transform_generation.j2", mappings=both)

    assert "PRIMARY_BRANCH_RENDERED" in prompt, (
        "the `transformation` value did not render. The primary branch "
        "`{% if m.get('transformation') %}` is no longer reached by a Production_Shape record, so "
        "every production budget figure measures a mapping line without its transformation hint."
    )
    assert "FALLBACK_BRANCH_RENDERED" not in prompt, (
        "both branches rendered, or the fallback won. `{% elif m.get('transform') %}` must stay an "
        "`elif`: if it became an independent `{% if %}` a record carrying both keys would render "
        "two hints, and the Production_Shape budget would silently under-measure any record that "
        "carries only one."
    )

    # ...and the ordinary Production_Shape fixture, unmodified, reaches the same branch.
    plain = load_prompt_template("transform_generation.j2", mappings=_make_production_mappings(1))
    assert "source_field_0 → target_field_0, direct [confidence: 0.95]" in plain, (
        "the Production_Shape mapping line has changed shape. Note there are NO type parentheses "
        "in it, deliberately: production writes neither `source_type` nor `target_type`, so the "
        "honest render carries no type annotation. See `_make_production_mappings`' docstring."
    )


def test_fixture_shape_cannot_satisfy_the_transformation_branch():
    """R2.4: Fixture_Shape reaches only the fallback, and must stay unable to reach the primary.

    Without this, the two assertions above could both be satisfied by someone adding
    `transformation` to `_make_mappings` — at which point Fixture_Shape and
    Production_Shape stop being distinguishable and the p52 defect returns in a new
    disguise.

    Fixture_Shape is the correct fixture for the tests that use it (R2.5): their subject
    is the template's Fixed_Cost, and the Fixed_Cost is the part of the render
    Fixture_Shape and production genuinely share. It is *not* a valid stand-in for a
    production render, and that is what this pins.

    **Validates: Requirements 2.4**
    """
    fixture_shape = _fixture_keys(_make_mappings(6))

    assert "transformation" not in fixture_shape, (
        "`_make_mappings` now carries `transformation`, so Fixture_Shape reaches the template's "
        "PRIMARY branch and is no longer distinguishable from Production_Shape. Either it should "
        "have been `_make_production_mappings`, or the two fixtures have been merged — in which "
        "case the Fixed_Cost assertions at `test_transform_prompt.py` have silently changed "
        "subject and their figures must be re-measured."
    )
    assert "transform" in fixture_shape, (
        "`_make_mappings` no longer carries `transform`, so it no longer exercises the template's "
        "`elif` fallback branch — which was its whole remaining value after p52."
    )
    assert "confidence" not in fixture_shape, (
        "`_make_mappings` now carries `confidence`, which every Fixed_Cost figure in "
        "test_transform_prompt.py was measured without. Re-measure those ceilings before keeping "
        "this."
    )

    # The rendered consequence, so the claim is not purely about dict keys.
    fixture_prompt = load_prompt_template("transform_generation.j2", mappings=_make_mappings(6))
    assert "[confidence:" not in fixture_prompt, (
        "the Fixture_Shape render now carries a confidence annotation. The `< 500` Fixed_Cost "
        "ceilings in test_transform_prompt.py were measured without it (~5 tokens per field)."
    )
