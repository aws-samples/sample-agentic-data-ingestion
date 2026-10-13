# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: ``mapping_resolution._extract_field_mappings`` (p37 T1, task 1.2).

**Why this file exists.** `_extract_field_mappings`
(`lambda/agentic_data_ingestion_create_code/mapping_resolution.py:42`) is one of the two
zero-coverage findings that survived `requirements.md`'s `lambda/**` false-negative check. A
CodeGraph zero-coverage flag under `lambda/**` usually means "unresolved", not "untested",
because test call sites reach their target through a `sys.path`-injected
`import lambda_function as _mod` alias. This one was re-confirmed by searching `tests/` for the
symbol name: **zero hits**, so it is genuinely untested. It has **2 real call sites** —
`code_assembly.py:227` (the template render, i.e. the code the job actually runs) and
`lambda_function.py:623` (the approval payload's ruleset) — and `design.md` C11 moves a third
caller onto it. This file pins the arm that must survive C11 unchanged; without it, "the caller
changed" and "the arm changed" are indistinguishable in review.

**Where the expectations come from, and where they deliberately do not.** `LGR-28b4d75-5`
(**template / ratified**) forbids deriving a test's expectations from the producer's own output —
"a test written from the producer's output cannot detect the producer drifting, because the output
is its own oracle". That applies with extra force here, because **the current output of the caller
this file is about IS the bug**: `requirements.md` Correction 1 records `Field mappings: 0` logged
and persisted for a run generated from **eight** mappings. So the expectations below come from

    R3.4  WHEN the Code_Generation_Step records its reasoning for a run, THE recorded
          field-mapping count SHALL equal the number of Field_Mapping_Entries the generated code
          was derived from.

and from a full read of the function (`mapping_resolution.py:42-68`) — not from running it and
recording what it printed.

**Two windows, named separately so a red is diagnosable.**

1. *The three-variant resolution assertions are expected **GREEN immediately**.* The function
   itself is correct: it resolves `field_mappings`, `mapping_config.field_mappings`, and
   `mapping_result` as **both** a JSON string and a dict. A red in any of them is a real finding
   about `_extract_field_mappings`, not an expected window, and must be reported rather than
   adjusted away.
2. *The `R3.4` assertion was expected **RED until task 9.1**, and that window is now **CLOSED**.*
   It ties the count the caller *reports* to the count the function *resolves*. Before 9.1 the
   caller counted two of the three variants inline, missing `mapping_result`-as-JSON-string —
   which this function's own docstring calls the "**current** schema_mapping output" (`:48-49`).
   It was marked `xfail(strict=True)` so a reviewer seeing red would not revert a correct change,
   and so the marker itself became a failure the moment 9.1 landed and the assertion started
   passing. 9.1 hoisted `_extract_field_mappings(mapping_item)` to a local that feeds both the
   reasoning string and `_generate_dqdl_rules`, the assertion went green, and the marker was
   removed with the assertion untouched. **A red here is now a real finding.**

`xfail(strict=True)` as the shape for a pre-change characterization window is the house practice,
not an invention here: `LGR-78ce0b7-13` (**template / ratified**) records p33 writing "the
characterization test … before the change" and keeping "three `xfail(strict=True)` markers" red
until the change landed, precisely so a check never "claims to measure one thing while failing for
another". `LGR-78ce0b7-5` (**template / ratified**) is the sibling precedent for characterizing a
read path that "was undocumented and had zero covering tests" *before* touching it.

**Ledger provenance for the third variant.** `LGR-d3c81fc-1` (**backfilled** — the author's own
words, unconfirmed history) is the commit that added it: "Extract field mappings from
mapping_result JSON string so AI generates actual transform code instead of passthrough
(df_transformed = df)". That is why variant 3 must resolve: without it, generation silently
degrades to a passthrough. `why` on `mapping_resolution.py` returns only `LGR-0176ccc-1`, the
documented 634-file snapshot — no signal.

**Observed edge deliberately NOT asserted.** A `mapping_result` string holding *valid* JSON that
is not an object (`"[1, 2]"`, `"\\"x\\""`) reaches `parsed.get("mappings", [])` and raises
`AttributeError`, which the `except (json.JSONDecodeError, TypeError)` at `:64` does not catch.
`R3.4` says nothing about that input and pinning it either way would invent a requirement, so it
is recorded here and left unasserted.

Pure function: **no moto, no boto3 mocks, no AWS**.

**Validates: Requirements 3.4**
"""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CREATE_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_create_code"
_SHARED_DIR = Path(__file__).resolve().parents[4] / "shared"

# `mapping_resolution` imports dynamo_typing / step_errors / step_names from the shared layer.
# None of them create an AWS client at import time (verified by reading their import blocks), so
# no stubbing is needed — unlike the `lambda_function` entry point, which does.
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))


def _load_mapping_resolution():
    """Load the module under a private alias.

    Loading by file location under a distinct name leaves ``sys.modules["mapping_resolution"]``
    untouched, so this file cannot hand a differently-stubbed copy to the sibling modules that
    import it inside a ``patch.dict("sys.modules", ...)`` block
    (`test_mapping_resolution_regeneration_feedback.py:115-123`).
    """
    spec = importlib.util.spec_from_file_location(
        "p37_t1_mapping_resolution", _CREATE_CODE_DIR / "mapping_resolution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_extract_field_mappings = _load_mapping_resolution()._extract_field_mappings

_CALLER_PATH = _CREATE_CODE_DIR / "lambda_function.py"


def _entries(*target_fields: str) -> list[dict]:
    """Field_Mapping_Entries in the shape `requirements.md`'s glossary states."""
    return [
        {
            "target_field": name,
            "source_field": name,
            "transformation": "direct",
            "confidence": 0.95,
            "notes": "",
        }
        for name in target_fields
    ]


# The observed run's shape: eight target fields, one of them an Unsourced_Target_Field.
# `requirements.md` Correction 1 derives the count from the rendered ruleset — 1 RowCount +
# 8 Completeness + 2 Uniqueness (`cust_id` matches `.*_id`, `email` matches `email`) = the 11 rules
# the Glue job evaluated. `phone` carries an empty `source_field`, which is how a Completeness rule
# on a column the transform nulls came to exist.
_OBSERVED_MAPPINGS = [
    *_entries("cust_id"),
    {"target_field": "first_name", "source_field": "fname", "confidence": 0.8},
    {"target_field": "last_name", "source_field": "lname", "confidence": 0.8},
    *_entries("email"),
    {"target_field": "phone", "source_field": "", "confidence": 0.1, "notes": "no source column"},
    *_entries("created_date", "state", "premium_member"),
]
_OBSERVED_COUNT = 8


# ---------------------------------------------------------------------------
# Window 1 — the three storage variants. Expected GREEN immediately.
# A red here is a real finding about `_extract_field_mappings`.
# ---------------------------------------------------------------------------


def test_variant_1_direct_field_mappings_resolves():
    """Variant 1, `field_mappings` (legacy direct field) — `mapping_resolution.py:46`."""
    record = {"field_mappings": _entries("a", "b")}

    assert _extract_field_mappings(record) == _entries("a", "b")


def test_variant_2_nested_mapping_config_resolves():
    """Variant 2, `mapping_config.field_mappings` (legacy nested) — `:47`."""
    record = {"mapping_config": {"field_mappings": _entries("a", "b", "c")}}

    assert _extract_field_mappings(record) == _entries("a", "b", "c")


def test_variant_3_mapping_result_json_string_resolves():
    """Variant 3 as a JSON string — the "**current** schema_mapping output" (`:48-49`).

    This is the variant the `agent_reasoning` caller misses, and `LGR-d3c81fc-1`
    (**backfilled**) is the commit that added it because missing it produced a passthrough
    transform.
    """
    record = {"mapping_result": json.dumps({"mappings": _entries("a", "b", "c", "d")})}

    assert _extract_field_mappings(record) == _entries("a", "b", "c", "d")


def test_variant_3_mapping_result_dict_resolves():
    """Variant 3 as an already-deserialized dict — the `elif` arm at `:66-67`."""
    record = {"mapping_result": {"mappings": _entries("a", "b")}}

    assert _extract_field_mappings(record) == _entries("a", "b")


def test_variant_1_takes_precedence_over_variant_2():
    record = {
        "field_mappings": _entries("winner"),
        "mapping_config": {"field_mappings": _entries("loser_a", "loser_b")},
    }

    assert _extract_field_mappings(record) == _entries("winner")


def test_variant_1_takes_precedence_over_variant_3():
    record = {
        "field_mappings": _entries("winner"),
        "mapping_result": json.dumps({"mappings": _entries("loser_a", "loser_b")}),
    }

    assert _extract_field_mappings(record) == _entries("winner")


def test_variant_2_takes_precedence_over_variant_3():
    record = {
        "mapping_config": {"field_mappings": _entries("winner")},
        "mapping_result": json.dumps({"mappings": _entries("loser_a", "loser_b")}),
    }

    assert _extract_field_mappings(record) == _entries("winner")


def test_present_but_empty_variant_1_falls_through_to_variant_3_not_variant_2():
    """A present-but-empty `field_mappings` masks variant 2 and falls through to variant 3.

    `dict.get(key, default)` evaluates its default eagerly but returns the *present* value, so
    `{"field_mappings": []}` yields `[]` and variant 2 is never consulted; the falsy check at `:57`
    then reaches variant 3. Pinned because it is the one precedence rule the docstring's numbered
    list does not state, and C11 must not change it.
    """
    record = {
        "field_mappings": [],
        "mapping_config": {"field_mappings": _entries("nested")},
        "mapping_result": json.dumps({"mappings": _entries("from_mapping_result")}),
    }

    assert _extract_field_mappings(record) == _entries("from_mapping_result")


def test_observed_run_shape_resolves_to_eight():
    """The record shape that produced `Field mappings: 0` resolves to eight entries.

    Green now, and it is what makes the `R3.4` window below diagnosable: if this fails, the
    fixture is wrong and the xfail below is red for a setup reason rather than for the caller.
    """
    record = {"mapping_result": json.dumps({"mappings": _OBSERVED_MAPPINGS})}

    assert len(_extract_field_mappings(record)) == _OBSERVED_COUNT


# ---------------------------------------------------------------------------
# Window 1 (continued) — the empty and malformed arms. Also expected GREEN.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({}, id="empty_record"),
        pytest.param({"field_mappings": []}, id="empty_variant_1"),
        pytest.param({"mapping_config": {}}, id="empty_mapping_config"),
        pytest.param({"mapping_config": {"field_mappings": []}}, id="empty_variant_2"),
        pytest.param({"mapping_result": ""}, id="empty_mapping_result_string"),
        pytest.param({"mapping_result": "{}"}, id="json_object_without_mappings_key"),
        pytest.param({"mapping_result": json.dumps({"mappings": []})}, id="json_with_empty_mappings"),
        pytest.param({"mapping_result": {"mappings": []}}, id="dict_with_empty_mappings"),
        pytest.param({"mapping_result": {}}, id="empty_dict"),
        pytest.param({"mapping_result": None}, id="none_is_neither_str_nor_dict"),
        pytest.param({"mapping_result": 0}, id="int_is_neither_str_nor_dict"),
    ],
)
def test_no_resolvable_mappings_returns_empty_list(record):
    """Every empty arm returns `[]` — never `None`, never a falsy other type (`:68`).

    `_generate_dqdl_rules` returns `""` for an empty list (`code_assembly.py:149-150`), which is
    what makes `R3.4`'s count meaningful, so the empty case has to be a list rather than absence.
    """
    result = _extract_field_mappings(record)

    assert result == []
    assert isinstance(result, list)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not json", id="unterminated_object"),
        pytest.param("   ", id="whitespace_only"),
        pytest.param("{'mappings': []}", id="single_quoted_not_json"),
    ],
)
def test_malformed_json_string_returns_empty_list_without_raising(raw):
    """The `except (json.JSONDecodeError, TypeError)` arm at `:64-65` swallows and returns `[]`.

    Not raising is load-bearing: `create_code` re-raises out of the step, so a malformed mapping
    record would fail the run rather than generate from zero mappings.
    """
    assert _extract_field_mappings({"mapping_result": raw}) == []


# ---------------------------------------------------------------------------
# Window 2 — R3.4. Expected RED until task 9.1.
# ---------------------------------------------------------------------------


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _count_slot(func: ast.AST) -> ast.expr | None:
    """The `{...}` expression that fills the `- Field mappings: ` line of `agent_reasoning`."""
    for node in ast.walk(func):
        if not isinstance(node, ast.JoinedStr):
            continue
        for index, part in enumerate(node.values[:-1]):
            if isinstance(part, ast.Constant) and isinstance(part.value, str) and "Field mappings:" in part.value:
                following = node.values[index + 1]
                if isinstance(following, ast.FormattedValue):
                    return following.value
    return None


def _names_bound_to_extract(func: ast.AST) -> set[str]:
    """Locals the function binds to `_extract_field_mappings(...)` — C11's hoist target."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _callee_name(node.value.func) == "_extract_field_mappings":
                names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _free_names(expr: ast.expr) -> set[str]:
    return {node.id for node in ast.walk(expr) if isinstance(node, ast.Name)}


# The only builtin the slot may use. Named rather than inherited so the evaluated expression can
# reach nothing else, and so `len` is not mistaken for an unbound local below.
_EVAL_BUILTINS = {"len": len}


def _reasoning_count_slot() -> tuple[ast.expr, set[str]]:
    tree = ast.parse(_CALLER_PATH.read_text(encoding="utf-8"), filename=str(_CALLER_PATH))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        slot = _count_slot(node)
        if slot is not None:
            return slot, _names_bound_to_extract(node)
    raise AssertionError(
        f"No '- Field mappings: ' slot found in an agent_reasoning f-string in {_CALLER_PATH}. "
        "R3.4 is about that recorded claim; if the line was renamed, this test must follow it "
        "rather than be deleted."
    )


# WINDOW CLOSED by task 9.1 (C11). The `xfail(strict=True)` marker that stood here is removed
# rather than the assertion loosened, which is what the marker's own reason text instructed:
# "When it lands, this xfail becomes an XPASS failure: remove the marker, do not re-loosen the
# test." 9.1 hoisted `_extract_field_mappings(mapping_item)` to
# `resolved_field_mappings` (`create_code/lambda_function.py:676`) and the reasoning string now
# reads `len(resolved_field_mappings)` (`:770`), so one evaluation feeds both the
# `- Field mappings: ` count and `_generate_dqdl_rules`' argument. The assertion below is
# unchanged from the form it held while red, and it is now a live regression fence: a future
# caller that recomputes the count from anything other than `_extract_field_mappings` fails here.
def test_r3_4_reported_field_mapping_count_equals_resolved_count():
    """R3.4: the count `agent_reasoning` reports equals the count the code was derived from.

    Asserted against the caller's own expression rather than a copy of it: the `- Field mappings: `
    slot is lifted out of `lambda_function.py` by `ast` and evaluated against the record shape from
    the observed run, with `mapping_item` and any local the function binds to
    `_extract_field_mappings(...)` supplied. The same record is passed to
    `_extract_field_mappings` directly to get the count the generated code *was* derived from
    (`code_assembly.py:227` resolves it through this exact function).
    """
    record = {"mapping_result": json.dumps({"mappings": _OBSERVED_MAPPINGS})}
    resolved = _extract_field_mappings(record)
    assert len(resolved) == _OBSERVED_COUNT, "fixture drift, not the R3.4 defect"

    slot, hoisted_names = _reasoning_count_slot()
    slot_source = ast.unparse(slot)
    calls_extract = "_extract_field_mappings" in {
        _callee_name(node.func) for node in ast.walk(slot) if isinstance(node, ast.Call)
    }

    assert calls_extract or (_free_names(slot) & hoisted_names), (
        "The reported field-mapping count is not derived from _extract_field_mappings, so it "
        "cannot equal the count the generated code was derived from (R3.4). The reasoning string "
        f"computes it from: {slot_source}"
    )

    namespace = {"mapping_item": record, "_extract_field_mappings": _extract_field_mappings}
    for name in hoisted_names:
        namespace[name] = resolved
    unbound = _free_names(slot) - set(namespace) - set(_EVAL_BUILTINS)
    assert not unbound, (
        f"The count expression reads names this test cannot bind: {sorted(unbound)}. Either the "
        f"hoist used a name not assigned from _extract_field_mappings, or the slot moved: {slot_source}"
    )

    # `eval` of this repo's own source, lifted by `ast` — the point is to read the caller's
    # expression rather than a copy of it. `S` is off for `tests/**` (pyproject per-file-ignores).
    reported = eval(
        compile(ast.fix_missing_locations(ast.Expression(body=slot)), "<agent_reasoning>", "eval"),
        {"__builtins__": _EVAL_BUILTINS},
        namespace,
    )

    assert reported == len(resolved) == _OBSERVED_COUNT, (
        f"agent_reasoning reports {reported} field mappings for a record the generated code was "
        f"derived from {len(resolved)} of (R3.4). Expression: {slot_source}"
    )


# ===========================================================================
# The sibling extractor — `_extract_unmapped_target_fields`
#
# Added with the branch B fix. `_unsourced_target_field_names` now reads BOTH of
# a mapping record's lists, and reaching the second one from `generate_code`
# needs an extractor that resolves the SAME three storage variants: on the live
# variant, `unmapped_target_fields` sits inside the JSON string
# `_extract_field_mappings` parses and then discards (it returns
# `parsed.get("mappings", [])` and nothing else). A fix that read
# `mapping_result["unmapped_target_fields"]` directly would work on the legacy
# shape and silently do nothing on the shape production writes.
#
# Written from the sibling's documented rules, so the two are pinned to the same
# precedence rather than to each other's current output.
# ===========================================================================

_extract_unmapped_target_fields = _load_mapping_resolution()._extract_unmapped_target_fields


def _unmapped(*names: str) -> list[dict]:
    """`unmapped_target_fields` entries in the shape `generate_simple_mapping` writes."""
    return [{"field": name, "default_value": None, "reason": "No matching source field found"} for name in names]


def test_unmapped_variant_1_direct_field_resolves():
    """Variant 1, top level — the shape p37 C9's normaliser writes back onto the record."""
    record = {"field_mappings": _entries("a"), "unmapped_target_fields": _unmapped("phone")}

    assert _extract_unmapped_target_fields(record) == _unmapped("phone")


def test_unmapped_variant_2_nested_mapping_config_resolves():
    """Variant 2, `mapping_config.unmapped_target_fields` (legacy nested)."""
    record = {"mapping_config": {"unmapped_target_fields": _unmapped("phone", "fax")}}

    assert _extract_unmapped_target_fields(record) == _unmapped("phone", "fax")


def test_unmapped_variant_3_mapping_result_json_string_resolves():
    """Variant 3 as a JSON string — the "current schema_mapping output".

    THE variant the branch B fix turns on. Both lists come out of one string, and
    only `mappings` used to survive the parse.
    """
    record = {
        "mapping_result": json.dumps({"mappings": _entries("a", "b"), "unmapped_target_fields": _unmapped("phone")})
    }

    assert _extract_field_mappings(record) == _entries("a", "b")
    assert _extract_unmapped_target_fields(record) == _unmapped("phone")


def test_unmapped_variant_3_mapping_result_dict_resolves():
    """Variant 3 as an already-deserialized dict — the `elif` arm."""
    record = {"mapping_result": {"mappings": _entries("a"), "unmapped_target_fields": _unmapped("phone")}}

    assert _extract_unmapped_target_fields(record) == _unmapped("phone")


def test_unmapped_variant_1_takes_precedence_over_variants_2_and_3():
    """Same precedence order as the sibling: 1, then 2, then 3."""
    record = {
        "unmapped_target_fields": _unmapped("winner"),
        "mapping_config": {"unmapped_target_fields": _unmapped("loser_nested")},
        "mapping_result": json.dumps({"unmapped_target_fields": _unmapped("loser_json")}),
    }

    assert _extract_unmapped_target_fields(record) == _unmapped("winner")


def test_unmapped_variant_2_takes_precedence_over_variant_3():
    record = {
        "mapping_config": {"unmapped_target_fields": _unmapped("winner")},
        "mapping_result": json.dumps({"unmapped_target_fields": _unmapped("loser_json")}),
    }

    assert _extract_unmapped_target_fields(record) == _unmapped("winner")


def test_present_but_empty_unmapped_variant_1_falls_through_to_variant_3():
    """The one precedence rule the sibling's numbered list does not state, mirrored.

    `dict.get(key, default)` returns the *present* value, so `{"unmapped_target_fields": []}`
    yields `[]`, variant 2 is never consulted, and the falsy check reaches
    variant 3. Pinned so the two extractors cannot drift on it.
    """
    record = {
        "unmapped_target_fields": [],
        "mapping_config": {"unmapped_target_fields": _unmapped("nested")},
        "mapping_result": json.dumps({"unmapped_target_fields": _unmapped("from_mapping_result")}),
    }

    assert _extract_unmapped_target_fields(record) == _unmapped("from_mapping_result")


def test_the_two_extractors_read_the_same_variant_independently():
    """Each list resolves from wherever IT lives, not from wherever the other did.

    A record whose `field_mappings` is top-level while its
    `unmapped_target_fields` is only inside the JSON string is reachable — the
    two keys have different writers. Resolving the second list by "whichever
    variant the first came from" would find nothing here.
    """
    record = {
        "field_mappings": _entries("a", "b"),
        "mapping_result": json.dumps({"mappings": _entries("ignored"), "unmapped_target_fields": _unmapped("phone")}),
    }

    assert _extract_field_mappings(record) == _entries("a", "b")
    assert _extract_unmapped_target_fields(record) == _unmapped("phone")


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({}, id="empty_record"),
        pytest.param({"unmapped_target_fields": []}, id="empty_variant_1"),
        pytest.param({"mapping_config": {}}, id="empty_mapping_config"),
        pytest.param({"mapping_config": {"unmapped_target_fields": []}}, id="empty_variant_2"),
        pytest.param({"mapping_result": ""}, id="empty_mapping_result_string"),
        pytest.param({"mapping_result": "{}"}, id="json_object_without_the_key"),
        pytest.param({"mapping_result": json.dumps({"mappings": _entries("a")})}, id="json_with_mappings_only"),
        pytest.param({"mapping_result": {"mappings": _entries("a")}}, id="dict_with_mappings_only"),
        pytest.param({"mapping_result": {}}, id="empty_dict"),
        pytest.param({"mapping_result": None}, id="none_is_neither_str_nor_dict"),
        pytest.param({"mapping_result": 0}, id="int_is_neither_str_nor_dict"),
        pytest.param({"unmapped_target_fields": "phone"}, id="non_list_string"),
        pytest.param({"unmapped_target_fields": {"field": "phone"}}, id="non_list_dict"),
        pytest.param({"mapping_result": json.dumps({"unmapped_target_fields": "phone"})}, id="non_list_in_json"),
    ],
)
def test_no_resolvable_unmapped_fields_returns_empty_list(record):
    """Every empty and non-list arm returns `[]` — never `None`, never a bare string.

    A string would iterate character by character in the reader and annotate
    single letters, so the non-list guard is load-bearing rather than defensive.
    """
    result = _extract_unmapped_target_fields(record)

    assert result == []
    assert isinstance(result, list)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not json", id="unterminated_object"),
        pytest.param("   ", id="whitespace_only"),
        pytest.param("{'unmapped_target_fields': []}", id="single_quoted_not_json"),
        pytest.param("[1, 2, 3]", id="json_array_has_no_get"),
        pytest.param("42", id="json_number_has_no_get"),
        pytest.param('"phone"', id="json_string_has_no_get"),
    ],
)
def test_malformed_json_string_returns_empty_unmapped_list_without_raising(raw):
    """Swallow and return `[]`, like the sibling.

    Not raising is load-bearing for the same reason: `create_code` re-raises out
    of the step, so a malformed record would fail the run rather than generate
    without an annotation. The JSON-array and JSON-number cases are why
    `AttributeError` is caught alongside `JSONDecodeError`/`TypeError` — `[1,2,3]`
    parses fine and then has no `.get`.
    """
    assert _extract_unmapped_target_fields({"mapping_result": raw}) == []


def test_elements_are_returned_untouched_in_record_order():
    """The extractor resolves WHERE the list lives; it never interprets elements.

    Both element shapes and the junk in between arrive at the reader as written,
    because `code_assembly._unmapped_target_field_name` is the one place that
    decides what an element names.
    """
    mixed = [{"field": "phone", "default_value": None}, "fax", None, 7, {"no_field_key": "x"}]
    record = {"unmapped_target_fields": mixed}

    assert _extract_unmapped_target_fields(record) == mixed
