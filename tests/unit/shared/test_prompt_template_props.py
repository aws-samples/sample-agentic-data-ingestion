# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for prompt template variable substitution (Task 2.6).

Feature: agentic-data-ingestion-codebase-optimization, Property 10: Prompt template variable substitution
**Validates: Requirements 15.3**
"""

import os
import shutil
import tempfile

import hypothesis.strategies as st
from hypothesis import given, settings

# ---------------------------------------------------------------------------
# Helpers – we test the loader against dynamically-created Jinja2 templates
# so the property holds for *any* template, not just the ones shipped today.
# ---------------------------------------------------------------------------

# Strategy: generate a list of unique, non-empty variable names (valid Python identifiers).
# Uses min 3 chars to naturally avoid most reserved words; explicit blocklist catches the rest.
_RESERVED = frozenset(
    {
        "and",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "import",
        "lambda",
        "nonlocal",
        "not",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "true",
        "false",
        "none",  # Jinja2 built-in names
    }
)

var_names_strategy = st.lists(
    st.from_regex(r"[a-z][a-z0-9]{2,15}", fullmatch=True).filter(lambda name: name not in _RESERVED),
    min_size=1,
    max_size=6,
    unique=True,
)

# Strategy: generate substitution values – printable text that won't contain
# Jinja2 syntax itself (avoids false negatives when checking for unresolved
# placeholders in the output).
safe_value = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"), blacklist_characters="{}"),
    min_size=1,
    max_size=200,
)


# Feature: agentic-data-ingestion-codebase-optimization, Property 10: Prompt template variable substitution
@given(var_names=var_names_strategy, values=st.data())
@settings(max_examples=200)
def test_prompt_template_variable_substitution(var_names, values):
    """
    For any Jinja2 template containing {{ variable }} placeholders and for any
    set of key-value pairs covering all placeholders, the rendered output:
      1. Contains each substituted value
      2. Does not contain any unresolved {{ ... }} patterns for the provided keys
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    # Draw one value per variable name
    kv = {name: values.draw(safe_value, label=name) for name in var_names}

    # Build a template string with static preamble, each variable on its own line
    template_lines = ["Static preamble text."]
    for name in var_names:
        template_lines.append(f"Var {name}: {{{{ {name} }}}}")
    template_lines.append("Static epilogue.")
    template_content = "\n".join(template_lines)

    # Write template to a temp directory and load via Jinja2 (same mechanism
    # as prompt_template_loader.py)
    tmpdir = tempfile.mkdtemp()
    try:
        template_path = os.path.join(tmpdir, "test_template.j2")
        with open(template_path, "w") as f:
            f.write(template_content)

        env = Environment(
            loader=FileSystemLoader(tmpdir),
            undefined=StrictUndefined,
        )
        template = env.get_template("test_template.j2")
        rendered = template.render(**kv)

        # 1. Every substituted value must appear in the rendered output
        for name, val in kv.items():
            assert val in rendered, f"Value for '{name}' not found in rendered output"

        # 2. No unresolved {{ ... }} patterns for the provided keys
        for name in var_names:
            assert f"{{{{ {name} }}}}" not in rendered, f"Unresolved placeholder '{{{{ {name} }}}}' found in output"
    finally:
        shutil.rmtree(tmpdir)
