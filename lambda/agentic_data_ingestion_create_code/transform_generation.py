# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AI transform-block generation for create_code (P28 R19.1 decomposition).

Extracted verbatim from ``lambda_function.py`` so the AI-transform prompt
building, model invocation, and AST anti-pattern validation are a testable
unit. This module deliberately does NOT import strands: the ``Agent``
constructor is injected via ``agent_factory`` by the caller
(``lambda_function`` passes its module-level ``Agent``), which keeps the
existing test seam (``patch.object(lambda_function, "Agent", ...)``)
working and keeps this module import-light.

The old function-attribute trace-plugin hack
(``generate_transform_with_ai._trace_plugin = plugin``) is replaced by the
explicit ``trace_plugin`` parameter, threaded from the handler through
``code_assembly.generate_code``.

Contracts carried by this module:
- P28 R3.3: AI failure raises StepFailure — no silent passthrough.
- P28 R13.3: regeneration prompts embed reviewer feedback
  (``_build_feedback_section``) so retries never reuse an identical prompt.
"""

from collections.abc import Callable
from typing import Any

from model_config import resolve_model_id
from prompt_template_loader import load_prompt_template

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from step_errors import StepFailure


def _build_feedback_section(regeneration_feedback: dict | None) -> str:
    """Render reviewer feedback for inclusion in the generation prompt.

    P28 R13.3: regeneration prompts must differ from the original by
    including the reviewer's issues — no identical-prompt retries. The
    feedback dict is the ``feedback_context`` the test step stored
    (``reviewer_issues`` structured issues + ``issues_found`` strings).
    Returns "" when there is no feedback (first attempt).
    """
    if not regeneration_feedback:
        return ""
    lines = ["PREVIOUS ATTEMPT WAS REJECTED BY CODE REVIEW. You MUST fix these issues:"]
    for issue in regeneration_feedback.get("reviewer_issues") or []:
        severity = issue.get("severity", "issue")
        description = issue.get("description", "")
        category = issue.get("category")
        prefix = f"[{severity}]" + (f" ({category})" if category else "")
        lines.append(f"- {prefix} {description}")
    for found in regeneration_feedback.get("issues_found") or []:
        lines.append(f"- {found}")
    summary = regeneration_feedback.get("summary")
    if summary:
        lines.append(f"Reviewer summary: {summary}")
    if len(lines) == 1:
        # Feedback dict present but carries nothing actionable — still mark
        # the retry so the prompt is not byte-identical to the original.
        lines.append("- Previous attempt failed review; regenerate with stricter adherence to the rules below.")
    return "\n".join(lines)


def generate_transform_with_ai(
    mappings: list,
    source_schema: dict | None = None,
    target_schema: dict | None = None,
    regeneration_feedback: dict | None = None,
    unsourced_target_fields: list[str] | None = None,
    *,
    agent_factory: Callable[..., Any],
    trace_plugin: Any = None,
) -> str:
    """Build a simplified prompt from field mappings, call Bedrock, return raw Python code.

    If no mappings are provided (a real, approved mapping record with zero
    field mappings — nothing to transform), returns a simple passthrough
    transform. This is the designed no-op-mapping flow, distinct from the
    failure paths below.

    ``target_schema`` (p33 D10, requirement 2.5): the pipeline's resolved
    target schema. **Rendered** into the prompt as declared field names and
    types, so the generator is told the target field names the field mappings
    alone do not carry, and so reviewer feedback naming a target-schema
    mismatch becomes actionable. This is what makes a regenerated script able
    to differ: p32 proved the loop mechanics work — three attempts, counter
    0→1→2, feedback stored and read back — and still produced three
    byte-identical scripts, because the mappings do not change between
    attempts and were the generator's only information about target names.

    ``source_schema``: accepted and **not rendered**, deliberately. The
    asymmetry with ``target_schema`` is recorded rather than tidied away (p33
    design R4), and the reason is that it is not an oversight:

    * The mapping block already renders ``source_field (source_type)`` for
      every mapping (``transform_generation.j2``), so a source field's name
      and type are already in the prompt. Rendering ``source_schema`` would
      add a second, redundant copy — cost with no new information.
    * The target side is NOT symmetric. A mapping line enumerates a mapped
      *pair*, so a target field that no mapping covers appears nowhere, and
      that is precisely the shortfall ``schema_compliance`` now fails a run
      for.

    So the two parameters are not two instances of the same thing: one would
    repeat what the prompt already says, the other supplies what it cannot
    say. Rendering ``source_schema`` too is a prompt-quality change with its
    own token cost and its own effect on every first-attempt prompt for every
    pipeline; it is out of scope for a bugfix whose requirement (2.5) is about
    the target schema. R4 carries it forward.

    ``regeneration_feedback`` (P28 R13.3): the reviewer's feedback_context
    from the failed test step. When present, its issues are embedded in the
    prompt so the regeneration prompt differs from the original.

    ``unsourced_target_fields`` (p37 R3.5, design C12): the target-schema field
    names the mapping claims but supplies no source column for. Rendered as an
    annotation on the target-schema lines the block above already emits, so the
    model is told *this field has no source — set it to an explicit
    null/default* rather than being left to source it from an unrelated column.
    It is **not** an instruction to omit the column: ``_build_validation_code``
    derives ``expected`` from ``target_schema["properties"].keys()``
    (``test_code/code_interpreter_runner.py``), so a missing target column fails
    ``schema_compliance`` — omitting would trade a quarantine for a failed gate.
    Passed through as a list rather than pre-formatted here for the same reason
    ``target_schema`` is: the template owns the presentation. ``None`` is passed
    through as-is — the template's ``| default([], true)`` absorbs it, so a
    caller that omits it renders byte-identically to before this change.

    ``agent_factory``: the strands ``Agent`` constructor (injected by
    lambda_function so this module carries no strands import and existing
    ``Agent`` patches keep working).

    ``trace_plugin``: optional AgentTracePlugin hook for observability,
    passed explicitly by the handler (replaces the old function-attribute
    injection).

    Raises:
        StepFailure: When the AI call fails (P28 R3.3). AI degradation must
            be loud — the old silent ``df_transformed = df`` fallback shipped
            untransformed data under a green pipeline.
    """
    if not mappings:
        return "df_transformed = df"

    # Build the prompt using the transform_generation template (reviewer
    # feedback is rendered into it on regeneration — P28 R13.3; the target
    # schema's declared fields are rendered on every attempt — p33 D10 / 2.5).
    #
    # `target_schema` is passed through as the resolved dict rather than
    # pre-flattened here: the template owns the presentation, which keeps this
    # layer a pass-through the same way `mappings` is, and keeps the render
    # context inspectable by the prompt tests. `None` is passed through as-is —
    # the template's `| default({}, true)` absorbs it, so a pipeline with no
    # target schema renders byte-identically to before this change.
    prompt_text = load_prompt_template(
        "transform_generation.j2",
        mappings=mappings,
        target_schema=target_schema,
        unsourced_target_fields=unsourced_target_fields,
        feedback_section=_build_feedback_section(regeneration_feedback),
    )

    # Call Bedrock via a lightweight Strands agent
    try:
        transform_agent = agent_factory(
            model=resolve_model_id(),
            system_prompt="You are a PySpark code generator. Output ONLY Python code. No explanations.",
            hooks=[trace_plugin] if trace_plugin else [],
        )
        response = transform_agent(prompt_text)
        raw_code = str(response).strip()
    except Exception as e:
        # P28 R3.3: AI failure fails the step — no silent passthrough.
        raise StepFailure(
            "create_code",
            f"AI transform generation failed: {e}",
            details={
                "original_error_type": type(e).__name__,
                "mapping_count": len(mappings),
            },
        ) from e

    # Strip markdown fences if present
    raw_code = _strip_markdown_fences(raw_code)
    return raw_code


def _strip_markdown_fences(code: str) -> str:
    """Remove markdown code fences from AI output."""
    code = code.strip()
    if code.startswith("```python"):
        code = code[len("```python") :].strip()
    elif code.startswith("```"):
        code = code[3:].strip()
    if code.endswith("```"):
        code = code[:-3].strip()
    return code


def validate_pyspark_antipatterns(code: str) -> list[str]:
    """Parse PySpark code and detect common Column method anti-patterns.

    Walks the AST looking for:
      1. Method calls like ``.lower()``, ``.upper()``, ``.trim()``,
         ``.substring()``, ``.length()`` which are incorrect when called
         on PySpark Column objects — the correct usage is
         ``pyspark.sql.functions.<func>()``.
      2. ``substring(col, pos, len)`` calls where ``pos`` or ``len`` is a
         function call (likely returns a Column). The PySpark API
         requires both to be Python ``int`` literals — passing a Column
         raises ``PySparkTypeError [NOT_ITERABLE]`` at runtime because
         Py4J tries to iterate the Column as a Python list. The fix is
         either ``initcap(col(...))`` for title-casing or
         ``expr("substring(field, 2, length(field))")`` for variable
         slicing in pure SQL.
    """
    import ast

    issues = []
    COLUMN_METHODS = {"lower", "upper", "trim", "substring", "length"}
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in COLUMN_METHODS:
                    issues.append(
                        f"Line {node.lineno}: .{node.func.attr}() called as method"
                        f" — use pyspark.sql.functions.{node.func.attr}() instead"
                    )
            # Catch substring(col, pos, len) with Column args for pos/len.
            # The arg is a Column whenever it is itself a function call —
            # e.g. ``length(trim(col("first_name")))`` or
            # ``col("len_field")``. Python int literals are
            # ``ast.Constant`` and pure arithmetic on ints is safe;
            # variable references / unary ops are flagged conservatively.
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "substring"
                and len(node.args) == 3
            ):
                for arg_idx in (1, 2):  # pos and len are 2nd / 3rd args
                    arg = node.args[arg_idx]
                    arg_name = "pos" if arg_idx == 1 else "len"
                    if isinstance(arg, ast.Call):
                        # length(...), col(...), trim(...) etc — Column-returning
                        called_name = (
                            arg.func.id if isinstance(arg.func, ast.Name) else getattr(arg.func, "attr", "<expr>")
                        )
                        issues.append(
                            f"Line {node.lineno}: substring(..., {arg_name}=) "
                            f"received {called_name}(...) which returns a Column. "
                            "substring's pos/len must be Python int literals; "
                            "use initcap(col(...)) for title-case or "
                            'expr("substring(field, pos, length(field))") '
                            "for variable slicing."
                        )
    except SyntaxError:
        pass  # If code can't be parsed, skip validation
    return issues
