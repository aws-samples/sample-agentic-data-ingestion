# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sweep: no executions-table write can create a row with no ``started_at`` (p48 R9.2).

``RunsByTimeIndex`` (``(pipeline_id, started_at)``, ``deploy-cdk/lib/data-stack.ts``)
is a **sparse** index: DynamoDB indexes only items carrying BOTH key attributes,
so a row with no ``started_at`` is absent from the index entirely and invisible in
run history **with no error**. That is the load-bearing risk of p48's chosen
mechanism, and R9.2 requires the writer invariant be enforced by a test rather
than left as a convention.

**Why this is a sweep over the whole tree rather than a list of call sites.**
R9.2 was originally framed around ``put_item``, and a ``put_item``-only test
passed on a tree that had four unguarded ``update_item`` upserts in it — DynamoDB's
``update_item`` CREATES the item when the key is absent unless a condition forbids
it, so an "update" is a row producer. A test enumerating today's sites by line
number would rot on the next edit, so this discovers writers instead:

* every ``*.py`` under ``lambda/`` is parsed,
* every ``update_item`` / ``put_item`` call is examined,
* a call is an **executions-table** write when its key shape is
  ``{pipeline_id, run_id}`` — the executions table is the ONLY table in
  ``data-stack.ts`` with that key schema (``processed_files`` is
  ``(pipeline_id, fingerprint)``; the state table is ``(run_id, step_name)``),

so a writer added in a new file, a new function, or under a new table-handle name
is found automatically and must satisfy the invariant to pass.

**The invariant.** Any executions-table write CAPABLE OF CREATING A ROW either
sets ``started_at`` or cannot create a row. Three ways to satisfy it, each with a
mechanical reason:

1. ``ConditionExpression`` mentions ``attribute_exists(`` — the write annotates
   an existing row and can never create one. The technique is ratified
   (``LGR-60c4985-4``) and in use on this table at ``pipelines_runs.retry_run``.
2. The ``UpdateExpression`` assigns a **nested document path** (``SET a.b = ...``)
   — DynamoDB rejects that with a ``ValidationException`` on a missing item, so
   it cannot create one either.
3. The enclosing function establishes ``started_at`` — some executions write in
   that function carries it, as a ``put_item`` ``Item`` key or as a
   ``started_at =`` fragment of an ``UpdateExpression``. This covers writes
   assembled at runtime (``"SET " + ", ".join(parts)``) and fold-in updates that
   follow a ``put_item`` in the same function.

Rule 3 is the loose one and is deliberately scoped to the enclosing function:
a sibling write in the same function is the only case where the code can be read
as guaranteeing the row already carries ``started_at``.

**Do NOT** read this test as licence to use index sparseness as a filter — p48
do-NOT 2 and R9.4 forbid it, and the phantom-row filter
(``dashboard_api/handlers/common.py``) stays exactly where it is.

Validates: p48 Requirements 9.1, 9.2
"""

import ast
from pathlib import Path

import pytest

LAMBDA_ROOT = Path(__file__).resolve().parents[3] / "lambda"

# The executions table's key schema (data-stack.ts ExecutionsTable): PK
# pipeline_id, SK run_id. Unique across every table in the stack, which is what
# makes key shape a sound table identifier.
EXECUTIONS_KEY = frozenset({"pipeline_id", "run_id"})

# Key attributes of the OTHER tables. A `put_item` Item is a whole row, not just
# a key, so a state row carrying pipeline_id + run_id + step_name would otherwise
# look like an executions write. Any of these present means it is not this table.
OTHER_TABLE_KEY_ATTRS = frozenset(
    {
        "step_name",  # state, agent_traces
        "mapping_id",  # schema_mappings
        "approval_id",  # approvals, task_tokens
        "PK",  # generated_code, test_results
        "SK",
        "fingerprint",  # processed_files
        "error_date",  # errors
        "notification_id",  # notifications
        "connection_id",  # ws_connections
        "glue_job_run_id",  # state GlueJobRunIndex
        "pipeline_prefix",  # pipeline_locks
        "resource_id",  # audit_log
    }
)

# Sites that do NOT satisfy the invariant yet, each deliberately left alone.
#
# Empty since 2026-10-11. Its last entry was `cancel_run`, whose two unguarded
# upserts could create `{pipeline_id, run_id, status: CANCELLED, ...}` with no
# `started_at`. The cancel's writes now live in `_mark_cancelled`,
# `_mark_cancelling` and `_restore_after_failed_cancel`, each conditional on
# `attribute_exists(run_id)`.
#
# Listed by (relative path, function) rather than by line number so ordinary
# edits to the file do not churn this list. The set is asserted to be EXACTLY
# this, so a new violation fails the sweep and a fix forces a conscious deletion.
KNOWN_UNGUARDED: frozenset[tuple[str, str]] = frozenset()


# ---------------------------------------------------------------------------
# AST helpers — comments and docstrings cannot false-positive a constant match,
# which is why this is an AST walk and not a grep.
# ---------------------------------------------------------------------------


def _dict_str_keys(node: ast.expr | None) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _splat_dicts(call: ast.Call) -> list[ast.expr]:
    """``**kwargs`` values passed to a call."""
    return [k.value for k in call.keywords if k.arg is None]


def _string_constants(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _resolve_dict_keys(node: ast.expr | None, scopes: list[ast.AST | None], depth: int = 0) -> set[str] | None:
    """String keys of the dict an expression denotes, or ``None`` if unresolvable.

    Handles a dict literal, ``{**base, "k": v}`` unpacks, a name bound to a dict
    literal (``x = {...}`` or ``x: dict = {...}``) plus later ``x["k"] = v``
    subscript assignments, and a single-argument wrapper call such as
    ``convert_floats_to_decimal({...})``.
    """
    if node is None or depth > 5:
        return None
    if isinstance(node, ast.Dict):
        keys = _dict_str_keys(node)
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:  # ** unpack inside a dict literal
                inner = _resolve_dict_keys(value, scopes, depth + 1)
                if inner:
                    keys |= inner
        return keys
    if isinstance(node, ast.Name):
        keys: set[str] | None = None
        for scope in scopes:
            if scope is None:
                continue
            for stmt in ast.walk(scope):
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == node.id:
                        inner = _resolve_dict_keys(value, scopes, depth + 1)
                        if inner is not None:
                            keys = (keys or set()) | inner
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == node.id
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        keys = (keys or set()) | {target.slice.value}
        return keys
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        return _resolve_dict_keys(node.args[0], scopes, depth + 1)
    return None


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """node → innermost enclosing function definition."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):  # breadth-first: inner defs overwrite outer
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                parents[child] = node
    return parents


def _sets_nested_document_path(call: ast.Call) -> bool:
    """``SET a.b = :v`` — DynamoDB cannot create an item through a nested path."""
    for expression in _string_constants(_keyword(call, "UpdateExpression")):
        body = expression.replace("SET", " ")
        for fragment in body.split(","):
            left, _, right = fragment.partition("=")
            if right and "." in left.strip():
                return True
    return False


def _assigns_started_at(node: ast.AST | None) -> bool:
    """Any string constant assigning ``started_at`` in an UpdateExpression."""
    return any("started_at" in s and "=" in s.split("started_at")[1][:3] for s in _string_constants(node))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class Write:
    """One discovered executions-table write site."""

    def __init__(self, path: Path, call: ast.Call, function: ast.AST | None, item_attrs: set[str] | None):
        self.path = path
        self.rel = str(path.relative_to(LAMBDA_ROOT))
        self.lineno = call.lineno
        self.op = call.func.attr  # type: ignore[union-attr]
        self.function = getattr(function, "name", "<module>")
        self._call = call
        self._fn = function
        self._item_attrs = item_attrs

    @property
    def id(self) -> str:
        return f"{self.rel}:{self.lineno}:{self.function}:{self.op}"

    @property
    def guarded(self) -> bool:
        condition = " ".join(_string_constants(_keyword(self._call, "ConditionExpression")))
        return "attribute_exists(" in condition

    @property
    def nested_path_only(self) -> bool:
        return _sets_nested_document_path(self._call)

    @property
    def function_establishes_started_at(self) -> bool:
        if self._item_attrs is not None and "started_at" in self._item_attrs:
            return True
        if _assigns_started_at(self._call):
            return True
        # Runtime-assembled expressions, and fold-in updates following a
        # put_item in the same function.
        return _assigns_started_at(self._fn) or _function_puts_started_at(self._fn, self.path)

    @property
    def satisfies_invariant(self) -> bool:
        return self.guarded or self.nested_path_only or self.function_establishes_started_at


_PUT_STARTED_AT_CACHE: dict[tuple[str, str], bool] = {}


def _function_puts_started_at(function: ast.AST | None, path: Path) -> bool:
    """A sibling ``put_item`` in the same function whose Item carries ``started_at``."""
    if function is None:
        return False
    cache_key = (str(path), getattr(function, "name", ""))
    if cache_key in _PUT_STARTED_AT_CACHE:
        return _PUT_STARTED_AT_CACHE[cache_key]
    result = False
    for node in ast.walk(function):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "put_item":
            continue
        item = _keyword(node, "Item") or (node.args[0] if node.args else None)
        attrs = _resolve_dict_keys(item, [function])
        if attrs and "started_at" in attrs:
            result = True
            break
    _PUT_STARTED_AT_CACHE[cache_key] = result
    return result


def _scan(path: Path) -> tuple[list[Write], list[str]]:
    """Executions writes in one file, plus write sites the detector cannot resolve."""
    tree = ast.parse(path.read_text())
    parents = _enclosing_functions(tree)
    writes: list[Write] = []
    unresolved: list[str] = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("update_item", "put_item"):
            continue
        function = parents.get(node)
        scopes: list[ast.AST | None] = [function, tree]
        location = f"{path.relative_to(LAMBDA_ROOT)}:{node.lineno}"

        if node.func.attr == "update_item":
            key_node = _keyword(node, "Key")
            if key_node is not None:
                key_keys = _resolve_dict_keys(key_node, scopes)
            else:
                # `update_item(**update_kwargs)` — read "Key" out of the splat.
                key_keys = None
                for splat in _splat_dicts(node):
                    key_keys = _splat_key_entry(splat, scopes)
                    if key_keys is not None:
                        break
            if key_keys is None:
                unresolved.append(location)
                continue
            if frozenset(key_keys) != EXECUTIONS_KEY:
                continue
            item_attrs = None
        else:
            item = _keyword(node, "Item") or (node.args[0] if node.args else None)
            item_attrs = _resolve_dict_keys(item, scopes)
            if item_attrs is None:
                unresolved.append(location)
                continue
            if not item_attrs >= EXECUTIONS_KEY or item_attrs & OTHER_TABLE_KEY_ATTRS:
                continue

        writes.append(Write(path, node, function, item_attrs))
    return writes, unresolved


def _dict_literals_bound_to(name: str, scopes: list[ast.AST | None]) -> list[ast.Dict]:
    """Every dict literal assigned to ``name`` in the given scopes."""
    literals: list[ast.Dict] = []
    for scope in scopes:
        if scope is None:
            continue
        for stmt in ast.walk(scope):
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            if not isinstance(value, ast.Dict):
                continue
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                literals.append(value)
    return literals


def _splat_key_entry(splat: ast.expr, scopes: list[ast.AST | None]) -> set[str] | None:
    """The key shape of a ``"Key": {...}`` entry inside a splatted kwargs dict."""
    candidates: list[ast.Dict] = []
    if isinstance(splat, ast.Dict):
        candidates = [splat]
    elif isinstance(splat, ast.Name):
        candidates = _dict_literals_bound_to(splat.id, scopes)
    for candidate in candidates:
        for key, value in zip(candidate.keys, candidate.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "Key":
                return _resolve_dict_keys(value, scopes)
    return None


def _discover() -> tuple[list[Write], list[str]]:
    writes: list[Write] = []
    unresolved: list[str] = []
    for path in sorted(LAMBDA_ROOT.rglob("*.py")):
        found, blind = _scan(path)
        writes += found
        unresolved += blind
    return writes, unresolved


ALL_WRITES, UNRESOLVED_SITES = _discover()


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("write", ALL_WRITES, ids=[w.id for w in ALL_WRITES])
def test_no_executions_write_can_create_a_started_at_less_row(write):
    """R9.2: every row-creating executions write sets ``started_at`` or cannot create.

    A failure here means a new writer can put a row into the executions table
    that ``RunsByTimeIndex`` will not index, so the run never appears in run
    history and nothing raises. Fix it by adding
    ``ConditionExpression="attribute_exists(run_id)"`` if the write only
    annotates an existing row, or by setting ``started_at`` if it legitimately
    creates one.
    """
    if (write.rel, write.function) in KNOWN_UNGUARDED:
        pytest.xfail(f"known-unguarded, flagged for a follow-up spec: {write.rel}:{write.function}")
    assert write.satisfies_invariant, (
        f"{write.id} can CREATE an executions row with no `started_at`: it carries no "
        f"attribute_exists() condition, does not assign a nested document path, and nothing "
        f"in {write.function}() establishes `started_at`. Such a row is absent from "
        f"RunsByTimeIndex and invisible in run history with no error (p48 R9.2)."
    )


class TestTheSweepItselfHolds:
    """The detector's own preconditions. Without these the sweep can pass by
    finding nothing, which is the failure mode a discovery-based test has that a
    hardcoded list does not."""

    def test_detector_is_not_blind(self):
        """Every ``update_item`` / ``put_item`` under lambda/ resolves to a table.

        An unresolvable write shape is a hole: it could be an executions writer
        the sweep never examined. Teach ``_resolve_dict_keys`` the new shape (or
        restructure the call) rather than accepting the hole.
        """
        assert UNRESOLVED_SITES == [], (
            "the detector cannot determine which table these writes target, so they were "
            f"never checked against R9.2: {UNRESOLVED_SITES}"
        )

    def test_the_known_producers_are_found(self):
        """Pins the writers p48 task 1.4 enumerated by reading the source.

        Catches the detector silently going blind — a discovery predicate that
        stops matching would otherwise turn this whole sweep green.
        """
        found = {(w.rel, w.function) for w in ALL_WRITES}
        expected = {
            ("agentic_data_ingestion_pipeline_trigger/lambda_function.py", "_record_duplicate_skip"),
            ("agentic_data_ingestion_pipeline_trigger/lambda_function.py", "_record_contention_skip"),
            ("agentic_data_ingestion_pipeline_trigger/lambda_function.py", "_track_queued_file"),
            ("agentic_data_ingestion_execution_updater/lambda_function.py", "_process_record"),
            ("agentic_data_ingestion_execution_updater/lambda_function.py", "_apply_self_healing_outcome"),
            ("agentic_data_ingestion_approval_handler/lambda_function.py", "create_approval_request"),
            ("agentic_data_ingestion_approval_handler/lambda_function.py", "process_approval_decision"),
            ("agentic_data_ingestion_dashboard_api/handlers/approvals.py", "_annotate_execution_row"),
            ("agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py", "_mark_cancelled"),
            ("agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py", "_mark_cancelling"),
            ("agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py", "_restore_after_failed_cancel"),
            ("agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py", "get_run_details"),
            ("agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py", "retry_run"),
        }
        assert expected <= found, f"the detector stopped finding known writers: {expected - found}"

    def test_known_unguarded_set_is_exactly_the_outstanding_violations(self):
        """The allowlist can only change consciously — in either direction.

        A new violation fails; a fixed one fails until it is deleted from
        ``KNOWN_UNGUARDED``, so the list cannot quietly outlive the defect.
        """
        violations = {(w.rel, w.function) for w in ALL_WRITES if not w.satisfies_invariant}
        assert violations == set(KNOWN_UNGUARDED), (
            f"outstanding R9.2 violations are {sorted(violations)} but KNOWN_UNGUARDED is "
            f"{sorted(KNOWN_UNGUARDED)} — update the allowlist deliberately"
        )

    def test_the_four_approval_path_writers_are_guarded_not_merely_listed(self):
        """The four sites p48 task 5.4 fixed carry the condition itself.

        Asserted by mechanism rather than through ``satisfies_invariant``: these
        four must pass via ``attribute_exists``, because neither decision branch
        knows a run's start time and inventing one would put a fabricated value
        on ``RunsByTimeIndex``' sort key (R1.4).
        """
        guarded_functions = {
            ("agentic_data_ingestion_approval_handler/lambda_function.py", "create_approval_request"),
            ("agentic_data_ingestion_approval_handler/lambda_function.py", "process_approval_decision"),
            ("agentic_data_ingestion_dashboard_api/handlers/approvals.py", "_annotate_execution_row"),
        }
        for write in ALL_WRITES:
            if (write.rel, write.function) in guarded_functions:
                assert write.guarded, f"{write.id} lost its attribute_exists(run_id) condition"

    def test_execution_updater_creates_rows_and_therefore_sets_started_at(self):
        """``_process_record`` is an upsert on purpose, so it must carry the value.

        Guarding it would lose real runs when stream events arrive out of order —
        strictly worse than a sparse index. The invariant is satisfied the other
        way: it sets ``started_at`` on EVERY step, not only the ``file_validation``
        INSERT (p48 task 5.4's judgement call).
        """
        upserts = [
            w
            for w in ALL_WRITES
            if w.rel == "agentic_data_ingestion_execution_updater/lambda_function.py"
            and w.function == "_process_record"
        ]
        assert upserts, "the execution_updater upsert is no longer discovered"
        for write in upserts:
            assert not write.guarded, (
                f"{write.id} gained an attribute_exists() condition — that LOSES runs whose "
                f"stream events arrive out of order. Set `started_at` instead."
            )
            assert write.function_establishes_started_at, (
                f"{write.id} no longer establishes `started_at`; every row it creates would be "
                f"absent from RunsByTimeIndex"
            )
