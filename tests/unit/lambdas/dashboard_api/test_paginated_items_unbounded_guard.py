# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structural guards for the p66 R3.4 / R2.6 seam and for R3.3's single paginator.

p66 task 2.5 — no production behaviour, and that is the deliverable
---------------------------------------------------------------------------
`_paginated_items` (`handlers/schemas.py:345`) takes no page cap and no deadline,
so p66's R3.4 (`WHERE a bound stops an enumeration short`) and R2.6 (`IF the
Tables_Endpoint reports that its enumeration was incomplete`) have **no
producer**. O5 introduces none, following `LGR-5d68eeb-3`'s ratified precedent:
`CheckJobTimeoutConfig`'s configured branch "stays as a documented
forward-compatibility seam with **no producer**", and its deliverable was
"accurate documentation at the guard plus one assertion pinning the posture" —
an assertion that was then **verified to bite** by seeding the thing it forbids.
Both halves are reproduced here: the documentation is the docstring block this
file's first test reads past, and the assertions below are what make a future
bound fail loudly.

What is actually at stake, so a future author can weigh it
---------------------------------------------------------------------------
R2.5 forbids the Glue Catalog Table_Picker's empty state claiming a database is
empty when the enumeration was truncated or bounded. The picker is *allowed* to
make the positive claim only because a zero-length result out of
`_paginated_items` cannot be a stopped enumeration. A cap or a deadline makes
R2.5's premise false **silently** — the copy keeps asserting a cause nothing
checks. These tests convert that silence into a failure.

Why structural, and not behavioural
---------------------------------------------------------------------------
This is a test about the **absence** of something, which is the easiest kind to
write vacuously. A behavioural test cannot see the difference: an unbounded walk
and a walk bounded at 1,000 pages produce identical output for every fixture
small enough to run, so "the pages all came back" is not evidence there is no
cap. `design.md` A1 also records `_paginated_items` as **ungoverned as a
function** — no ledger record fences it — so there is nothing else standing
between it and a bound.

Signature and AST, not substring matching
---------------------------------------------------------------------------
`LGR-b46a5bb-4` (**template / ratified**) settled the mechanism for a repository
guard of exactly this shape: it "uses AST analysis scoped to the lambda/ source
tree, with no allowlist", having **rejected substring matching** because "it
forbids documenting the fence, and failed for exactly that reason against
handlers/agents.py, whose docstring names threading.Thread on purpose".

That is not a hypothetical here. `handlers/resources.py:22` contains the literal
`get_paginator` **inside a comment** explaining why the helper is imported rather
than redefined, and `_paginated_items`' own docstring now contains the words
"page cap" and "deadline" in the sentences that say there are none. A grep-based
guard would fail on its own documentation. Both tests below therefore parse.

The same record's other half is honoured too: **no allowlist.** There is no
exemption parameter, because "an allowlist entry is a door".

**Validates: Requirements 2.5, 2.6, 3.3, 3.4**
"""

import ast
import inspect
import os

# ---------------------------------------------------------------------------
# Import-time env — table names only.
#
# `sys.path` for this whole directory is handled by
# `tests/unit/lambdas/dashboard_api/conftest.py:27-30`. The table names are not:
# this module reaches `handlers/common.py:42-43` through `handlers.resources`,
# and both `AUDIT_TABLE` and `AGENT_TRACES_TABLE` are resolved there with
# `require_env` at **module** scope. Without them the file is only collectable
# when some other module in the layer happened to be imported first — i.e. it
# would pass in a directory run and fail standalone. `setdefault` is this
# layer's per-module idiom (`LGR-3e45e65-1`).
#
# `AWS_REGION` / `AWS_DEFAULT_REGION` are deliberately absent: `LGR-3e45e65-1`
# pins both by **assignment** in `tests/unit/conftest.py:35-36`, so a
# `setdefault` here would be a no-op that reads as if this module had a say.
# ---------------------------------------------------------------------------
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import resources as _resources
from handlers import schemas as _schemas
from handlers.schemas import _paginated_items

#: What to do instead of deleting an assertion in this file. Every failure
#: message below ends with it, because a guard that only says "this changed" gets
#: deleted, and `LGR-5d68eeb-3`'s precedent is a posture pinned *with* its
#: revisit path rather than a tripwire with none.
_IF_YOU_MEANT_IT = (
    "\n\nIf you are adding a bound deliberately, it is a four-part change and this "
    "assertion is the last part, not the obstacle:\n"
    "  1. p66 R3.4 and R2.6 become live — the endpoint must name the truncation in "
    "its response body, and the Table_Picker must say the list may be partial.\n"
    "  2. `LGR-9a35b2d-3`'s always-present `partial_counts` is the shape to copy. "
    "p66 O5 rejected an always-`false` `truncated` key *because* the bound was not "
    "real; once it is, that objection lapses.\n"
    "  3. Withdraw the Table_Picker's positive empty-state claim first "
    "(`GLUE_TARGET_NO_TABLES_*`, R2.5) — in the same change, not after it. It "
    "currently asserts a database is empty on the strength of this function "
    "having no bound.\n"
    "  4. Then update `_paginated_items`' docstring and this test together."
)

#: The parameter list as it stands. Order included: these are positional, and
#: `handlers/resources.py:148` / `:185` and `handlers/schemas.py:473` / `:478` /
#: `:579` / `:949` all call them positionally.
_EXPECTED_PARAMETERS = ("client", "operation", "result_key", "kwargs")


def _function_ast(func) -> ast.FunctionDef:
    """The `ast.FunctionDef` for `func`, parsed from its own source.

    `inspect.getsource` on a function returns the `def` block alone, so the
    parsed module has exactly one top-level statement. Dedented because a nested
    definition would not parse otherwise — `_paginated_items` is module-level, so
    this is belt-and-braces rather than load-bearing.
    """
    tree = ast.parse(inspect.cleandoc("\n" + inspect.getsource(func)))
    (node,) = tree.body
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef), (
        f"expected a function definition, parsed {type(node).__name__}"
    )
    return node


class TestPaginatedItemsCarriesNoBound:
    """R3.4 / R2.6's antecedents have no producer, and this is what keeps it so."""

    def test_signature_carries_no_page_cap_and_no_deadline(self):
        """The parameter list is exactly `(client, operation, result_key, **kwargs)`.

        The cheapest way to add a bound is a defaulted keyword — `page_cap=None`,
        `max_items=1000`, `deadline=None` — which is source-compatible with all
        six existing call sites and therefore invisible to every other test in
        this layer. This assertion is the one that sees it.

        Defaults are checked separately from names: a default on an existing
        parameter is the other compatible way in, and `result_key="Contents"`
        would additionally undo the reason that parameter is explicit (naming it
        at the call site is what makes a mismatch loud).

        **Validates: Requirements 2.5, 3.4**
        """
        signature = inspect.signature(_paginated_items)

        assert tuple(signature.parameters) == _EXPECTED_PARAMETERS, (
            f"`_paginated_items`' parameters are now {tuple(signature.parameters)}, expected "
            f"{_EXPECTED_PARAMETERS}. A new parameter here is how a page cap or a deadline "
            f"arrives without any other test noticing." + _IF_YOU_MEANT_IT
        )

        kwargs_parameter = signature.parameters["kwargs"]
        assert kwargs_parameter.kind is inspect.Parameter.VAR_KEYWORD, (
            "`kwargs` must stay `**kwargs` — it forwards the caller's own arguments "
            f"(`DatabaseName`, `Bucket`, `Prefix`) to `.paginate()`, and it is {kwargs_parameter.kind} now. "
            "A named parameter in its place would also let `PaginationConfig` be passed as if it "
            "were an ordinary argument." + _IF_YOU_MEANT_IT
        )

        defaulted = [
            name for name, parameter in signature.parameters.items() if parameter.default is not inspect.Parameter.empty
        ]
        assert defaulted == [], (
            f"`_paginated_items` now has defaulted parameters {defaulted}. All three named parameters "
            "are required on purpose: a default is the source-compatible way to add a bound, and a "
            "default on `result_key` would undo the reason it is passed explicitly at all." + _IF_YOU_MEANT_IT
        )

    def test_body_is_a_total_walk_with_no_conditional_and_no_clock(self):
        """The body is one `for` over every page and one `yield from`. Nothing else.

        A bound does not need a parameter. It can be a module constant read in
        the body, a `break` once a counter is reached, a `PaginationConfig`
        dict handed to `.paginate()`, or a wall-clock comparison. Each of those
        introduces at least one AST node this function does not currently
        contain, so the guard is stated as the shape of the body rather than as a
        list of things not to do:

        * no `ast.If` and no `ast.Compare` — a cap or a deadline has to *test*
          something, and the only currently-present branch-like construct is the
          `or []` coalescing at `schemas.py:359-360`, which is an `ast.BoolOp`;
        * no `ast.Break`, `ast.While`, `ast.Return` or `ast.Try` — an early exit
          from the page walk is precisely what R3.4 describes;
        * no clock function referenced anywhere, checked by attribute and name
          rather than by substring, because the docstring says the word
          "deadline" on purpose (`LGR-b46a5bb-4`);
        * no `PaginationConfig`, which is boto3's own `MaxItems` / `PageSize`
          bound and would cap the enumeration without any Python-level branch at
          all — the one way in that none of the assertions above would see.

        The assertion is deliberately tight. A refactor that is genuinely
        unbounded but differently shaped will fail here, and that is the right
        cost: this is 2 statements of code, and re-reading them is cheaper than
        the failure mode the tightness buys.

        **Validates: Requirements 2.5, 2.6, 3.4**
        """
        node = _function_ast(_paginated_items)

        # Statement shape: the docstring, then the `for`. Nothing may sit
        # before, between or after them.
        statement_kinds = [type(statement).__name__ for statement in node.body]
        assert statement_kinds == ["Expr", "For"], (
            f"`_paginated_items`' body is now {statement_kinds}, expected ['Expr', 'For'] — the "
            "docstring followed by the page walk. A counter, a deadline computation or a guard "
            "clause is a third statement." + _IF_YOU_MEANT_IT
        )

        forbidden_nodes = (ast.If, ast.IfExp, ast.Compare, ast.Break, ast.While, ast.Return, ast.Try)
        found = sorted({type(child).__name__ for child in ast.walk(node) if isinstance(child, forbidden_nodes)})
        assert found == [], (
            f"`_paginated_items` now contains {found}. A page cap or a deadline has to test "
            "something and then stop early; an unbounded walk needs neither a comparison nor an "
            "exit. R2.5's positive empty-state claim rests on there being no early exit here." + _IF_YOU_MEANT_IT
        )

        # A clock, by name rather than by text. `monotonic` / `perf_counter` /
        # `now` / `utcnow` / `time` cover every in-tree spelling of "read the
        # clock" — `handlers/schemas.py` itself uses `time.monotonic()` for the
        # Display_Label deadline (`LGR-14d6dd3-2`), which is the nearby
        # precedent this function deliberately does not follow.
        clock_names = {"monotonic", "perf_counter", "now", "utcnow", "time", "process_time"}
        clocks = sorted(
            {
                child.attr if isinstance(child, ast.Attribute) else child.id
                for child in ast.walk(node)
                if (isinstance(child, ast.Attribute) and child.attr in clock_names)
                or (isinstance(child, ast.Name) and child.id in clock_names)
            }
        )
        assert clocks == [], (
            f"`_paginated_items` now reads a clock ({clocks}). A wall-clock deadline is the second "
            "shape of bound R3.4 describes, and it is the one `_resolve_display_labels` in the same "
            "module legitimately has — that one is a per-object fan-out whose cost scales with row "
            "count, ratified as bounded by `LGR-14d6dd3-2`. This is a page walk." + _IF_YOU_MEANT_IT
        )

        # boto3's own bound, which needs no Python branch: a `PaginationConfig`
        # keyword or dict key reaching `.paginate()` caps the enumeration inside
        # botocore. Checked as an identifier/constant, not as text.
        pagination_config = [
            child
            for child in ast.walk(node)
            if (isinstance(child, ast.keyword) and child.arg == "PaginationConfig")
            or (isinstance(child, ast.Constant) and child.value in ("PaginationConfig", "MaxItems", "PageSize"))
        ]
        assert pagination_config == [], (
            "`_paginated_items` now passes a `PaginationConfig` / `MaxItems` / `PageSize` to "
            "`.paginate()`. That is a bound enforced inside botocore, with no Python-level branch "
            "for the assertions above to catch, and it truncates the enumeration exactly as R3.4 "
            "describes." + _IF_YOU_MEANT_IT
        )


class TestResourcesUsesTheImportedHelper:
    """R3.3 — the pagination mechanism is the existing one, not a second definition."""

    def test_resources_binds_the_same_function_object_as_schemas(self):
        """`handlers.resources._paginated_items` **is** `handlers.schemas._paginated_items`.

        Identity, not equality of behaviour. R3.3 requires the mechanism to be
        "the one already in this codebase … rather than a second definition of
        pagination", and a copied body would satisfy every behavioural test in
        this layer while being exactly what R3.3 forbids. `is` is the only
        assertion that can tell an import from a duplicate.

        **Validates: Requirement 3.3**
        """
        assert _resources._paginated_items is _schemas._paginated_items, (
            "`handlers/resources.py` no longer binds `handlers/schemas.py`'s `_paginated_items` — "
            "it has its own. R3.3 requires one definition of pagination; two is the drift the "
            "helper exists to prevent (`handlers/resources.py:16-26` states this at the import "
            "site). Import it rather than copying it."
        )

    def test_resources_declares_no_paginator_of_its_own(self):
        """`resources.py` neither defines `_paginated_items` nor calls `get_paginator`.

        Parsed, not grepped, for a concrete reason: `resources.py:22` contains the
        literal `get_paginator` inside the comment that explains the cross-module
        import. A substring guard would fail on the documentation of the very
        rule it enforces — the failure mode `LGR-b46a5bb-4` records against
        `handlers/agents.py`. Only an `ast.Attribute` named `get_paginator`, i.e.
        a real call, counts.

        `list_glue_databases` and `list_glue_tables` are then checked to be the
        callers, so this is not satisfied by a module that paginates nothing at
        all.

        **Validates: Requirement 3.3**
        """
        tree = ast.parse(inspect.getsource(_resources))

        redefinitions = [
            statement.name
            for statement in ast.walk(tree)
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef) and statement.name == "_paginated_items"
        ]
        assert redefinitions == [], (
            "`handlers/resources.py` defines its own `_paginated_items`. R3.3 requires the existing "
            "helper in `handlers/schemas.py`; a same-named local shadow is worse than a differently "
            "named one, because the import site still reads correct."
        )

        paginator_calls = [
            child for child in ast.walk(tree) if isinstance(child, ast.Attribute) and child.attr == "get_paginator"
        ]
        assert paginator_calls == [], (
            f"`handlers/resources.py` calls `get_paginator` directly at line(s) "
            f"{sorted(call.lineno for call in paginator_calls)}. R3.3: `_paginated_items` is the ONE "
            "place that call is made, so a second one can drift — a different `result_key`, a "
            "`PaginationConfig`, or a page loop that stops early. Call the imported helper."
        )

        # And both handlers do go through it, so the two assertions above are not
        # vacuously satisfied by a module that enumerates nothing.
        for handler_name, operation, result_key in (
            ("list_glue_databases", "get_databases", "DatabaseList"),
            ("list_glue_tables", "get_tables", "TableList"),
        ):
            handler = _function_ast(getattr(_resources, handler_name))
            calls = [
                child
                for child in ast.walk(handler)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_paginated_items"
            ]
            assert len(calls) == 1, (
                f"`{handler_name}` makes {len(calls)} `_paginated_items` call(s), expected exactly 1 "
                f"(R3.1 / R3.2). Zero means the enumeration returns one page again and every "
                f"database or table past it is silently absent from the picker."
            )
            literals = [argument.value for argument in calls[0].args if isinstance(argument, ast.Constant)]
            assert literals == [operation, result_key], (
                f"`{handler_name}` paginates {literals}, expected ['{operation}', '{result_key}']. "
                "The operation and its result key are passed together on purpose: a mismatch is a "
                "silently empty source rather than an error (`handlers/schemas.py:355-357`)."
            )
