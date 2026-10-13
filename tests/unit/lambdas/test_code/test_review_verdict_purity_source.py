# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 4.3 — structural purity of the gate and its two retention helpers.

What this file owns
-------------------
The *structural* half of four no-change claims about
``lambda/agentic_data_ingestion_test_code/review_verdict.py``. Nothing here
exercises behaviour; every assertion is over the AST of a real function object.

* **5.3** ``effective_verdict``, ``retained_nonblocking_findings`` and
  ``blocking_transform_block_findings`` contain no ``logger`` / ``logging``, so
  the gate's purity under R7.4 survives the observability work task 12.1 adds
  in ``lambda_function.py`` (R5.3).
* **7.4** ``effective_verdict`` takes exactly one parameter, ``verdict`` — the
  same fence as
  ``test_review_gate_calibration.py::test_effective_verdict_is_pure_over_the_verdict_dict``,
  asserted the same way (``list(inspect.signature(f).parameters)``) rather than
  by a second technique. The two helpers are held to the same arity for the
  reason set out under R8.4 below.
* **8.4** the gate and the helpers read no execution-record count key, so the
  unwired ``frame_mode`` synthetic-frame defect is neither depended on nor
  addressed here (do-NOT 10).
* **8.3** the same functions name no part of the regeneration attempt bound,
  its recorded value, or the feedback context (do-NOT 8).

Scope note: ``_is_reviewer_transform_block`` is asserted alongside the three
public functions. It is where rule 1's attribution antecedent is read, so
excluding it would leave the R8.4 claim with a hole exactly one call deep — a
``and issue.get("record_count", 0) == 0`` conjunct added there would satisfy
every assertion below if the predicate were out of scope.

Why ``ast`` and not substring matching — the sibling's reason, and a second one
------------------------------------------------------------------------------
Task 2.1 (``test_review_verdict_severity_vocabulary.py``) already faced this
choice for its "no bare ``minor`` literal" pin and resolved it with ``ast``,
because the naive substring form failed on ``effective_verdict``'s own
docstring, which design §4.1 *requires* to describe the floor in prose. The same
technique is used here, and for this file the argument is not merely aesthetic:

**the substring form of test 5.3 fails today, on correct code.**
``retained_nonblocking_findings``' docstring says, in its own words, "Pure,
non-mutating, and free of logging (R5.3, C12)". So
``"logging" not in inspect.getsource(retained_nonblocking_findings)`` is red
against a helper that contains no logging at all — the assertion would forbid
the documentation of the very absence it exists to check. Design §6's table and
task 12.2 both describe test 5.3 as an ``inspect.getsource`` check; that
description holds (the source is still what is read, via
``inspect.getsource`` inside ``ast.parse``), but the comparison has to be over
parsed code rather than raw text.

What the AST excludes, deliberately:

* **docstrings** — dropped before the walk. Prose saying a function does not log
  is wanted, not forbidden.
* **comments** — the AST discards them outright, so a comment at the log site
  in a *neighbouring* module, or one here explaining why nothing is logged, is
  free.

What it still catches: an ``ast.Name`` (``logger.warning(...)``), an
``ast.Attribute`` (``mod.logger``), an import inside the function body
(``import logging``), and a string constant in executable code
(``__import__("logging")``, ``getattr(mod, "logger")``) — the four routes a
logging call can actually arrive by.

What "references no execution-record count key" means precisely
--------------------------------------------------------------
A denylist of key names I thought of would be worth very little: it passes for
free against the one key nobody predicted. So R8.4 is asserted in two layers,
and the load-bearing one is an **allowlist**.

1. **Allowlist (the assertion that bites).** Every string literal in the
   executable body of the four functions must be a key of the canonical verdict
   shape — and that key set is *derived* by running a sample document through
   the real ``parse_review_verdict`` rather than written out here. Any key that
   is not part of the gate's declared input surface fails, whether it is
   ``record_count``, ``input_records`` or something invented next year. This is
   the same reasoning as ``LGR-28b4d75-5``: the oracle comes from the parser's
   contract, not from the gate's current body.

2. **Vocabulary denylist (the assertion that explains).** Every identifier the
   four functions reference — names, attributes, string constants, keyword
   argument names, imports — is checked against the token families R8.3 and R8.4
   name. This catches the route the allowlist cannot see: a key hidden behind a
   module-level constant (``verdict.get(_RECORD_COUNT_KEY)`` inlines no
   literal), or an attribute access (``result.record_count``). It also names the
   defect in its failure message, which an allowlist violation cannot.

   The families are drawn from keys that really exist in this Lambda directory,
   not from imagination: ``record_count``, ``input_records`` and
   ``output_records`` (``code_interpreter_runner.py``, ``glue_session_runner.py``,
   ``lambda_function.py``, ``shadow_comparison.py``), ``rows_read`` /
   ``rows_written`` (``shared/load_outcome.py``), ``frame_mode``
   (``glue_session_runner.py``, the do-NOT 10 defect itself), and
   ``max_attempts`` / ``max_attempts_reached`` / ``regeneration_attempt`` /
   ``regeneration_feedback`` / ``reviewer_issues`` for R8.3.

A third route exists and is closed by arity: a count cannot be *read* from a
verdict dict that never carried one, but it can be *passed in*. That is why the
signature assertions cover the two helpers as well as the gate — between the
allowlist, the denylist and the parameter list, the three ways an execution
record count could reach these functions are each pinned.

The no-mutation claim (C12), and which half lives here
------------------------------------------------------
``retained_nonblocking_findings`` returns a new list and writes nothing back
onto the verdict. The behavioural half of that is
``test_reviewer_path_invariance_prop.py`` (Property 18), which deep-copies
``parsed_results`` and asserts it is pristine after the call — re-run unedited
for this task, not amended.

Stated honestly: Property 18 exercises ``aggregate_check_results``, which does
not yet *call* either helper — task 10.1 puts the first call there and 12.1 the
second. So today Property 18 covers the path that will reach them, and the
static assertion here (no store into a subscript or attribute, no call to a
mutating list/dict method) is what covers the helpers themselves. Once 10.1 and
12.1 land, Property 18's pristine check becomes the dynamic half of the same
claim for real.

NO FIXTURES
-----------
Nothing here needs one. These tests import a pure-stdlib module and read its
parsed source — no AWS client, no moto mock, no Lambda environment — so no
fixture from ``tests/fixtures/conftest.py`` applies. The root
``tests/conftest.py`` still applies ``@pytest.mark.unit`` by location.

No line numbers appear in any assertion. ``review_verdict.py`` moved three times
during this spec already (tasks 1.1, 3.1 and 4.1 each added lines), so functions
are resolved by import and their source located with ``inspect``.

**Validates: Requirements 5.3, 7.4, 8.3, 8.4**
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import textwrap
from collections.abc import Callable, Iterator
from typing import Any

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
import review_verdict as _rv
from review_verdict import (
    _is_reviewer_transform_block,
    blocking_transform_block_findings,
    effective_verdict,
    parse_review_verdict,
    retained_nonblocking_findings,
)

#: The gate, its predicate, and the two retention helpers task 4.1 added. Named
#: rather than discovered so that a function silently dropping out of this file's
#: coverage is a visible edit here, not an absence nobody notices.
_GATE_FUNCTIONS: tuple[Callable[..., Any], ...] = (
    effective_verdict,
    _is_reviewer_transform_block,
    retained_nonblocking_findings,
    blocking_transform_block_findings,
)

#: The two helpers, which R8.4 holds to the gate's own arity (see the module
#: docstring: a count cannot be read from the verdict, but it could be passed in).
_RETENTION_HELPERS: tuple[Callable[..., Any], ...] = (
    retained_nonblocking_findings,
    blocking_transform_block_findings,
)

#: Substrings that make an identifier a logging identifier. Matched on the
#: lower-cased form so ``LOGGER``, ``_logger`` and ``structlog``-style wrappers
#: are caught as well as the two spellings R5.3 names.
_LOGGING_TOKENS = ("logger", "logging")

#: Token families R8.4 (execution record counts, and the do-NOT 10 defect) and
#: R8.3 (the regeneration attempt bound and the feedback context) forbid. Matched
#: as substrings of a lower-cased identifier, so a new key inside a family — a
#: ``sample_row_count`` nobody has written yet — is caught without this table
#: being updated. Each entry names a real key in this Lambda directory; see the
#: module docstring for where each one lives.
_FORBIDDEN_VOCABULARY: tuple[tuple[str, str], ...] = (
    ("record", "an execution record count (record_count / input_records / output_records) — R8.4, do-NOT 10"),
    ("row", "a row tally (rows_read / rows_written, shared/load_outcome.py) — R8.4"),
    ("count", "a count of anything: R8.4, and the `.count(` fence the calibration suite already holds"),
    ("frame_mode", "the unwired synthetic-frame defect itself — do-NOT 10, getting its own spec"),
    ("execution_result", "the execution result, which the gate decision may not read at all — R8.4"),
    ("attempt", "the regeneration attempt bound or its recorded value (max_attempts*) — R8.3, do-NOT 8"),
    ("feedback", "the regeneration feedback context (regeneration_feedback / reviewer_issues) — R8.3, do-NOT 8"),
)

#: List and dict methods that mutate the receiver in place. A call to any of
#: these is the static form of the C12 violation Property 18 catches dynamically.
_MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "popitem",
        "clear",
        "update",
        "setdefault",
        "sort",
        "reverse",
        "__setitem__",
        "__delitem__",
    }
)


def _canonical_verdict_keys() -> set[str]:
    """Every key of the canonical verdict shape, derived from the real parser.

    Written as a parser round-trip rather than a hand-listed set so the allowlist
    tracks ``parse_review_verdict``'s contract instead of restating it. A sample
    carrying an optional ``category`` and an explicit ``attribution`` is used so
    the optional issue keys are present too, which keeps the allowlist from being
    narrower than the shape the gate is legitimately allowed to read.
    """
    canonical = parse_review_verdict(
        json.dumps(
            {
                "verdict": "fail",
                "issues": [
                    {
                        "severity": "minor",
                        "description": "A finding.",
                        "category": "Correctness",
                        "attribution": "transform_block",
                    }
                ],
                "categories": {"Correctness": "fail"},
            }
        )
    )
    keys = set(canonical)
    for issue in canonical["issues"]:
        keys |= set(issue)
    return keys


def _function_body_nodes(func: Callable[..., Any]) -> list[ast.stmt]:
    """``func``'s body statements with the docstring dropped.

    Deliberately duplicated from
    ``test_review_verdict_severity_vocabulary.py`` rather than imported: test
    modules importing each other couples them through pytest's rootdir and
    import mode, and fifteen lines of AST plumbing is a cheaper duplication than
    that coupling. The reason the docstring is dropped differs per file — there
    it legitimately writes ``minor``, here it legitimately writes "free of
    logging" — but the mechanism is the same one on purpose.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(definitions) == 1, (
        f"expected exactly one function definition in the source of {func.__name__}, got "
        f"{len(definitions)}. The assertions below would be looking at the wrong node."
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


def _walk_body(func: Callable[..., Any]) -> Iterator[ast.AST]:
    """Every AST node in ``func``'s executable body (docstring excluded)."""
    for statement in _function_body_nodes(func):
        yield from ast.walk(statement)


def _string_literals(func: Callable[..., Any]) -> list[str]:
    """Every ``str`` constant in ``func``'s executable body."""
    return [node.value for node in _walk_body(func) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _identifiers(func: Callable[..., Any]) -> set[str]:
    """Every identifier ``func``'s executable body mentions, lower-cased.

    Five kinds, because a forbidden key can arrive as any of them:

    * ``ast.Name`` — a bare name, including a module-level constant standing in
      for a key (``verdict.get(_RECORD_COUNT_KEY)``);
    * ``ast.Attribute`` — an attribute read or a method call
      (``result.record_count``, ``logger.warning``, ``issues.count``);
    * ``ast.Constant`` of ``str`` — a key read from a dict, or a module name
      handed to ``__import__``;
    * ``ast.keyword`` — a keyword argument name;
    * ``ast.Import`` / ``ast.ImportFrom`` — a function-local import.
    """
    identifiers: set[str] = set()
    for node in _walk_body(func):
        if isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            identifiers.add(node.value.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            identifiers.add(node.arg.lower())
        elif isinstance(node, ast.Import):
            identifiers.update(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                identifiers.add(node.module.lower())
            identifiers.update(alias.name.lower() for alias in node.names)
    return identifiers


# ---------------------------------------------------------------------------
# 5.3 — the gate and both helpers are free of logging
# ---------------------------------------------------------------------------


def test_the_gate_and_the_retention_helpers_contain_no_logging():
    """R5.3: no logging call in the gate, its predicate, or either helper.

    Task 12.1 puts the retention log in ``lambda_function.py``, at the handler,
    beside the ``logger.info`` that already names the raw verdict — never in
    ``review_verdict.py``. Two reasons this is pinned rather than trusted: R7.4's
    purity claim is about the gate being a function of its argument and nothing
    else, and this module is shared verbatim with the AgentCore container
    (``review_verdict.py``'s own docstring: pure stdlib, json and re), where a
    logging import would arrive as a second thing to configure.

    Asserted over the AST, not the source text, because the substring form is
    red today against correct code: ``retained_nonblocking_findings``' docstring
    says "free of logging (R5.3, C12)". See the module docstring.
    """
    for func in _GATE_FUNCTIONS:
        identifiers = _identifiers(func)

        # Non-vacuity: an extraction that returned nothing would make the
        # offender search below pass for free.
        assert identifiers, (
            f"no identifiers extracted from {func.__name__}'s body at all — the extraction is broken "
            "and the logging assertion below would be checking nothing."
        )

        offenders = sorted(
            identifier for identifier in identifiers if any(token in identifier for token in _LOGGING_TOKENS)
        )
        assert not offenders, (
            f"{func.__name__} references {offenders!r}. R5.3: the Review_Gate and its retention helpers "
            "stay free of logging calls so that R7.4's purity survives task 12.1's observability work. "
            "The retention log belongs at the handler in lambda_function.py, where the raw verdict is "
            "already logged — and review_verdict.py is copied verbatim into the AgentCore container, "
            "which is a second reason to keep it pure stdlib."
        )


def test_review_verdict_imports_no_logging_at_module_scope():
    """R5.3: not even the import, which is the first move of wiring logging in.

    Slightly wider than R5.3's letter — the criterion is about logging *calls* —
    and deliberately so. A module-level ``logger = logging.getLogger(__name__)``
    is inert on its own and would pass every assertion above, while being the
    half of the change that makes the next half a one-liner. The module's own
    docstring already states the standard being pinned: pure stdlib, ``json`` and
    ``re``, so the container and the Lambda share it with zero extra dependencies.

    Only the import is asserted, not the whole import set: a future legitimate
    stdlib import should not be a test failure.
    """
    module_source = inspect.getsource(_rv)
    imports: set[str] = set()
    for node in ast.walk(ast.parse(module_source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.lower())

    # Non-vacuity: the module really does import something (json, re, typing).
    assert imports, "no imports found in review_verdict.py at all — the AST walk is broken."

    offenders = sorted(name for name in imports if any(token in name for token in _LOGGING_TOKENS))
    assert not offenders, (
        f"review_verdict.py imports {offenders!r} at module scope. Nothing in this module logs (R5.3), "
        "and an inert logging import is the first half of the change that would break that — the "
        "second half being a call the assertions above would then catch one commit too late."
    )


# ---------------------------------------------------------------------------
# 7.4 / 8.4 — arity: what can be passed in
# ---------------------------------------------------------------------------


def test_effective_verdict_takes_only_the_verdict_dict():
    """R7.4: the gate's signature is exactly ``["verdict"]``.

    The same fence as
    ``test_review_gate_calibration.py::test_effective_verdict_is_pure_over_the_verdict_dict``,
    asserted with the same technique on purpose. Two different spellings of one
    structural claim is how they drift apart; the calibration suite got here
    first (P28b R1.7), so this file follows it rather than inventing a second
    idiom.

    It is repeated here because this file is where R8.4 is discharged, and the
    parameter list is one of the three routes an execution record count could
    take into the gate — the other two being a key read from the verdict
    (allowlisted below) and a module-level constant standing in for one
    (denylisted below).
    """
    params = list(inspect.signature(effective_verdict).parameters)
    assert params == ["verdict"], (
        f"effective_verdict's parameters are {params!r}, expected ['verdict']. R7.4: the gate decision "
        "is a pure function of the structured verdict, and R8.4 adds that it reads no execution record "
        "count — a second parameter is the shortest route to breaking both at once."
    )


def test_the_retention_helpers_take_only_the_verdict_dict():
    """R8.4: the helpers cannot be handed an execution result either.

    ``retained_nonblocking_findings`` and ``blocking_transform_block_findings``
    accept a canonical verdict or a ``parsed_results`` dict — both carry
    ``verdict`` and ``issues``, which is the whole of the gate's input. Holding
    them to the gate's arity is what stops the count arriving as a second
    argument at the one place task 12.1's log line is assembled, which is
    precisely where an execution record count would look tempting.
    """
    for func in _RETENTION_HELPERS:
        params = list(inspect.signature(func).parameters)
        assert params == ["verdict"], (
            f"{func.__name__}'s parameters are {params!r}, expected ['verdict']. R8.4: the retention "
            "helpers derive from the verdict dict alone. A second parameter is how an execution record "
            "count would reach the retention log without any key read below noticing."
        )


# ---------------------------------------------------------------------------
# 8.4 — the allowlist: every key read belongs to the canonical verdict
# ---------------------------------------------------------------------------


def test_the_gate_and_helpers_read_no_key_outside_the_canonical_verdict():
    """R8.4: every string literal in these bodies is a canonical verdict key.

    An allowlist rather than a denylist, and derived from
    ``parse_review_verdict``'s own output rather than written out here. A
    denylist of keys I could name would pass for free against the key nobody
    named; this fails on *any* key outside the gate's declared input surface,
    which is what makes it a real answer to "would it fail if someone wired an
    execution record count in".

    The oracle direction matters (``LGR-28b4d75-5``): the allowlist comes from
    the parser's contract, not from the gate's current body, so a gate that grew
    a new key cannot also widen the set that permits it.
    """
    allowed = _canonical_verdict_keys()

    # Non-vacuity on the oracle: a shape that lost `severity` or
    # `attribution_source` would silently permit their absence downstream.
    assert {"verdict", "issues", "severity", "attribution", "attribution_source"} <= allowed, (
        f"the canonical verdict shape derived from parse_review_verdict is {sorted(allowed)!r}, which is "
        "missing keys rule 1 reads. Either the parser's output shape changed or the sample document "
        "above no longer exercises it, and this allowlist is not the gate's input surface."
    )

    for func in _GATE_FUNCTIONS:
        literals = _string_literals(func)

        # Non-vacuity: these functions read keys, so an empty collection means
        # the extraction broke rather than that the body is clean.
        assert literals, (
            f"no string literals found in {func.__name__}'s body at all. Every one of these four "
            "functions reads at least one verdict key, so the extraction is broken and the assertion "
            "below would pass without checking anything."
        )

        offenders = sorted({literal for literal in literals if literal not in allowed})
        assert not offenders, (
            f"{func.__name__} reads the key(s) {offenders!r}, which are not part of the canonical "
            f"verdict shape {sorted(allowed)!r}. R8.4: the Review_Gate decision is derived without "
            "reading any execution record count, so the unwired frame_mode synthetic-frame defect is "
            "neither depended on nor addressed here (do-NOT 10) — that defect is getting its own spec, "
            "and a gate that reads a record count would make the two changes hard to separate. R8.3 "
            "says the same about the regeneration attempt bound and the feedback context."
        )


# ---------------------------------------------------------------------------
# 8.3 / 8.4 — the vocabulary denylist: the routes a literal cannot show
# ---------------------------------------------------------------------------


def test_the_gate_and_helpers_name_no_execution_count_attempt_or_feedback_vocabulary():
    """R8.3, R8.4: no identifier here belongs to a forbidden token family.

    The allowlist above sees a key read as a literal. It cannot see one hidden
    behind a module-level constant (``verdict.get(_RECORD_COUNT_KEY)`` inlines no
    string) or reached as an attribute (``result.record_count``). This closes
    that route, over names, attributes, keyword arguments, imports and literals
    alike.

    Matched by token *family* rather than by exact key so a name inside a family
    that nobody has written yet — a ``sample_row_count`` — is caught without this
    table being updated first. Every family is drawn from a key that really
    exists in this Lambda directory; the module docstring lists where each one
    lives.

    ``count`` also re-states, over these four functions, the fence
    ``test_review_gate_calibration.py::test_gate_functions_contain_no_token_presence_or_keyword_count_logic``
    holds over the five gate functions: ``.count(`` is an ``ast.Attribute`` named
    ``count``, so it fails here too.
    """
    for func in _GATE_FUNCTIONS:
        identifiers = _identifiers(func)
        assert identifiers, f"no identifiers extracted from {func.__name__}'s body — the walk is broken."

        for token, why in _FORBIDDEN_VOCABULARY:
            offenders = sorted(identifier for identifier in identifiers if token in identifier)
            assert not offenders, (
                f"{func.__name__} names {offenders!r}, matching the forbidden token {token!r}: {why}. "
                "The Review_Gate and its retention helpers derive from the verdict dict alone — the "
                "execution result, the record counts and the regeneration machinery are all outside "
                "this spec, and a reference here would couple the gate to a defect that is being fixed "
                "elsewhere."
            )


# ---------------------------------------------------------------------------
# C12 — the static half of the no-mutation claim
# ---------------------------------------------------------------------------


def test_the_gate_and_helpers_write_nothing_back_onto_the_verdict():
    """C12: no in-place write anywhere in these four bodies.

    ``retained_nonblocking_findings`` returns a new list; neither helper marks
    the verdict, and the gate reads it. The behavioural half of this is
    ``test_reviewer_path_invariance_prop.py`` (Property 18), which deep-copies
    ``parsed_results`` and asserts it comes back pristine — re-run unedited for
    this task.

    That property exercises ``aggregate_check_results``, which does not call
    either helper yet (task 10.1 adds the first call, 12.1 the second). So this
    assertion is what covers the helpers *today*, and Property 18 becomes the
    dynamic half of the same claim once those tasks land. Stated rather than
    left implied, because "Property 18 covers it" would be an overclaim right
    now.

    Three shapes are forbidden: a store into a subscript or an attribute
    (``verdict["retained"] = ...``, ``issue.retained = ...``), an augmented
    assignment to either, and a call to a list/dict method that mutates its
    receiver. Binding a local name is untouched — ``effective_verdict`` binds
    ``all_explicit_scaffolding`` and must keep being able to.
    """
    for func in _GATE_FUNCTIONS:
        for node in _walk_body(func):
            if isinstance(node, ast.Assign):
                bad_targets = [t for t in node.targets if isinstance(t, (ast.Subscript, ast.Attribute))]
                assert not bad_targets, (
                    f"{func.__name__} assigns into a subscript or attribute of one of its inputs. C12: "
                    "these functions return new values and write nothing back onto the verdict — three "
                    "consumers read the same dict, and a marker written here would reach all of them."
                )
            if isinstance(node, ast.AugAssign):
                assert not isinstance(node.target, (ast.Subscript, ast.Attribute)), (
                    f"{func.__name__} augment-assigns into a subscript or attribute of one of its "
                    "inputs, which mutates the caller's verdict dict (C12)."
                )
            assert not isinstance(node, ast.Delete), (
                f"{func.__name__} contains a `del` statement. C12: the verdict dict these functions "
                "receive belongs to the caller and is read by three consumers."
            )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _MUTATING_METHODS, (
                    f"{func.__name__} calls .{node.func.attr}(), which mutates its receiver in place. "
                    "C12: build a new list or dict instead. Property 18 in "
                    "test_reviewer_path_invariance_prop.py asserts the same thing dynamically over the "
                    "path that will call these helpers."
                )
