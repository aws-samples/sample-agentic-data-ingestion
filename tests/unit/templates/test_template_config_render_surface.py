# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No value from pipeline configuration reaches a template's ``{{ }}`` as raw code.

``code_assembly.generate_code`` builds the render context by merging
``source_config`` and ``target_config`` into it wholesale
(``template_vars.update(...)``), so every key of either config is a template
variable, and ``{{ name }}`` emits that variable's value as Python source in
the Glue job. Both configs are free-form: the edit endpoint merges a submitted
``target_config`` key by key and stores it verbatim. A raw ``{{ name }}`` is
therefore safe only when the merge cannot supply ``name``, or when what it
supplies has been validated or escaped first.

An external review rendered ``target_config.partition_columns =
'__import__("os").getcwd()'`` into ``partitionBy(__import__("os").getcwd())``.
Sweeping every output expression the way this module does found two more on
the same path: ``checkpoint_bucket``, emitted inside an f-string, and
``engine``, emitted inside a quoted literal.

Each ``{{ }}`` in every template must be one of:

* escaped: the whole expression ends in ``| tojson``;
* bound by the template itself, ``{% set name = <literal> %}``;
* bound by every site that includes the partial, ``{% with name = <literal> %}``;
* bound explicitly by ``generate_code`` AFTER the merge, unconditionally, and
  listed in ``REVIEWED_RAW_BINDINGS`` with what makes its value safe.

Anything else is reachable from configuration and fails here. The table is the
point where a person decides; the behaviour behind each entry is tested in
``tests/unit/lambdas/create_code/test_config_render_injection.py`` and the
modules each entry names.
"""

from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict
from pathlib import Path

from jinja2 import nodes

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (_REPO_ROOT / "lambda" / "agentic_data_ingestion_create_code", _REPO_ROOT / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import code_assembly

#: Values ``generate_code`` binds after the merge that a template emits raw,
#: each with what makes it safe. A new raw binding fails until it is added here.
REVIEWED_RAW_BINDINGS: dict[str, str] = {
    "transform_block": "AI output; validate_transform_block_safety (threat model T-01) runs before the render",
    "column_rename_block": "_rename_projection_block: names as _python_string_literal, targets via validate_identifier",
    "dqdl_rules": "_generate_dqdl_rules: every rule field passes validate_identifier",
    "merge_key": "validate_identifier, before the AI call",
    "partition_columns": "_partition_columns_argument: parse_identifier_list, then each name as a string literal",
    "checkpoint_bucket": "_checkpoint_bucket: the S3 bucket-name allowlist",
    "quarantine_bucket": "the Lambda's own GENERATED_CODE_BUCKET, a generate_code parameter, not pipeline configuration",
}

#: Emitted raw, bound nowhere on purpose: the DQ partial's else-arm names it so a
#: mistyped stage fails the render naming itself. Every include site binds the
#: stage to 'evaluate' or 'report', so the arm never renders;
#: ``test_dq_template_vars_contract.py`` fences both halves of that.
NEVER_DEFINED_SENTINELS = frozenset({"data_quality_stage_must_be_either_evaluate_or_report"})

#: The templates production renders. Partials are reached only through these.
TOP_LEVEL_PREFIX = "glue_job_"


def _env():
    return code_assembly._get_pyspark_jinja_env()


def _template_names() -> list[str]:
    names = [name for name in _env().list_templates() if name.endswith(".j2")]
    assert any(name.startswith(TOP_LEVEL_PREFIX) for name in names), names
    return sorted(names)


def _parse(name: str) -> nodes.Template:
    env = _env()
    return env.parse(env.loader.get_source(env, name)[0])


def _is_literal(node: nodes.Node) -> bool:
    """A value with no context lookup in any result: a constant, or a choice of constants."""
    if isinstance(node, nodes.Const):
        return True
    if isinstance(node, nodes.CondExpr):
        return _is_literal(node.expr1) and node.expr2 is not None and _is_literal(node.expr2)
    return False


def _raw_outputs(tree: nodes.Template) -> dict[str, list[int]]:
    """Context names each un-escaped ``{{ }}`` reads, with the lines it appears on."""
    found: dict[str, list[int]] = defaultdict(list)
    for output in tree.find_all(nodes.Output):
        for expression in output.nodes:
            if isinstance(expression, nodes.TemplateData):
                continue
            if isinstance(expression, nodes.Filter) and expression.name == "tojson":
                continue
            names = [expression] if isinstance(expression, nodes.Name) else []
            names += list(expression.find_all(nodes.Name))
            for name in names:
                if name.ctx == "load":
                    found[name.name].append(expression.lineno)
    return found


def _literal_set_bindings(tree: nodes.Template) -> set[str]:
    return {
        assign.target.name
        for assign in tree.find_all(nodes.Assign)
        if isinstance(assign.target, nodes.Name) and _is_literal(assign.node)
    }


def _include_sites() -> dict[str, list[tuple[str, frozenset[str]]]]:
    """For each included template: (including template, names a literal ``with`` binds there)."""
    sites: dict[str, list[tuple[str, frozenset[str]]]] = defaultdict(list)

    def walk(node: nodes.Node, owner: str, bound: frozenset[str]) -> None:
        if isinstance(node, nodes.With):
            added = {
                target.name
                for target, value in zip(node.targets, node.values, strict=True)
                if isinstance(target, nodes.Name) and _is_literal(value)
            }
            for child in node.body:
                walk(child, owner, bound | added)
            return
        if isinstance(node, nodes.Include):
            assert isinstance(node.template, nodes.Const), f"{owner}: an include with a computed name"
            sites[node.template.value].append((owner, bound))
        for child in node.iter_child_nodes():
            walk(child, owner, bound)

    for name in _template_names():
        walk(_parse(name), name, frozenset())
    return sites


def _bound_after_merge() -> set[str]:
    """``template_vars["x"] = ...`` statements of the render function, after its last merge.

    Only DIRECT statements of the function body count, so the binding happens
    on every render; one nested in an ``if`` can be skipped and leave the
    merged value in place.
    """
    tree = ast.parse(Path(code_assembly.__file__).read_text(encoding="utf-8"))
    renderers = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "render"
            and any(
                kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == "template_vars"
                for kw in call.keywords
            )
            for call in ast.walk(fn)
        )
    ]
    assert len(renderers) == 1, [fn.name for fn in renderers]
    fn = renderers[0]
    merges = [
        stmt.lineno
        for stmt in fn.body
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == "update"
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "template_vars"
    ]
    assert merges, "generate_code no longer merges the configs into template_vars; re-derive this module"
    last_merge = max(merges)
    bound: set[str] = set()
    for stmt in fn.body:
        if stmt.lineno <= last_merge or not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "template_vars"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                bound.add(target.slice.value)
    return bound


def _unsafe_raw_outputs() -> tuple[list[str], set[str]]:
    """``(violations, raw names that are explicit bindings)`` across every template."""
    sites = _include_sites()
    explicit = _bound_after_merge()
    violations: list[str] = []
    raw_explicit: set[str] = set()

    for name in _template_names():
        tree = _parse(name)
        local = _literal_set_bindings(tree)
        include_sites = sites.get(name, [])
        for variable, lines in sorted(_raw_outputs(tree).items()):
            if variable in local or variable in NEVER_DEFINED_SENTINELS:
                continue
            if variable in explicit:
                raw_explicit.add(variable)
                if variable not in REVIEWED_RAW_BINDINGS:
                    violations.append(
                        f"{name}:{lines[0]} emits `{{{{ {variable} }}}}` raw; generate_code binds it after the "
                        "merge, but no one has recorded why its value is safe as code. Add it to "
                        "REVIEWED_RAW_BINDINGS naming the validator, or render it through `| tojson`."
                    )
                continue
            if (
                not name.startswith(TOP_LEVEL_PREFIX)
                and include_sites
                and all(variable in bound for _, bound in include_sites)
            ):
                continue
            violations.append(
                f"{name}:{lines[0]} emits `{{{{ {variable} }}}}` raw, and the config merge can supply it: "
                "nothing binds it after `template_vars.update(...)` and no literal `set`/`with` binds it. "
                "Render it through `| tojson`, or bind it in generate_code from a validated value."
            )
    return violations, raw_explicit


def test_no_template_emits_a_configuration_value_as_raw_code():
    violations, _ = _unsafe_raw_outputs()

    assert not violations, "\n".join(violations)


def test_every_reviewed_binding_is_still_a_raw_output():
    """Keeps the table honest: an entry whose output is now escaped, or whose
    binding moved, is removed rather than left to vouch for nothing."""
    _, raw_explicit = _unsafe_raw_outputs()

    assert set(REVIEWED_RAW_BINDINGS) == raw_explicit


def test_the_sweep_sees_the_outputs_it_classifies():
    """The negative control: the sweep is not passing because it reads nothing.
    `partition_columns` is emitted raw in four templates and a partial, and
    `engine` only through `tojson`."""
    raw = defaultdict(set)
    for name in _template_names():
        for variable in _raw_outputs(_parse(name)):
            raw[variable].add(name)

    assert len(raw["partition_columns"]) >= 4
    assert "engine" not in raw
    assert "key_prefix" in raw
