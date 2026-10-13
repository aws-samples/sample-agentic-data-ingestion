# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R12 — runtime merge_key guard in the rendered MERGE INTO job.

The generated S3 Tables job reads ``merge_key`` from its runtime Glue
arguments and f-strings it into ``spark.sql("MERGE INTO ... ON
t.{merge_key} = s.{merge_key} ...")``. Because job arguments can be set at
``start_job_run`` time (bypassing every Python-side validation), the
rendered job itself carries an identifier-allowlist guard IMMEDIATELY after
reading the argument — a hostile ``--merge_key`` fails the job before any
SQL is assembled.

These tests extract the guard block from the real rendered template output
and EXECUTE it against hostile and benign ``args`` values, proving the
guard that ships inside the job actually rejects injection attempts
(quotes, semicolons, newlines, SQL comment tokens — R12.4).

Covers both templates that render the MERGE statement:
- ``glue_job_s3_tables.py.j2`` (inline merge block, ``--merge_key``)
- ``glue_job_jdbc_source.py.j2`` → ``_partials/_write_s3_tables.py.j2``
  (``--target_merge_key`` via ``key_prefix``)

**Validates: Requirements 12.1, 12.2, 12.4**
"""

import os
import textwrap

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

BASE_VARS = {
    "transform_block": "df_transformed = df",
    "write_mode": "merge",
    "source_format": "json",
    "partition_columns": None,
    "merge_key": "id",
    "checkpoint_enabled": False,
    "checkpoint_bucket": "",
}

JDBC_SOURCE_VARS = {
    **BASE_VARS,
    "target_type": "S3_TABLES",
    "engine": "postgresql",
    "read_mode": "full",
    "jdbc_url": "jdbc:postgresql://host:5432/db",
    "table_name": "src_table",
    "secret_arn": "",
}

# Hostile identifiers per R12.4: quotes, semicolons, newlines, comment tokens.
HOSTILE_IDENTIFIERS = [
    pytest.param('col"; DROP TABLE x; --', id="double-quote-semicolon-comment"),
    pytest.param("col'; DROP TABLE x; --", id="single-quote-semicolon-comment"),
    pytest.param("col; DELETE FROM t", id="semicolon"),
    pytest.param("col\nDROP TABLE x", id="newline"),
    pytest.param("col--comment", id="line-comment-token"),
    pytest.param("col/*comment*/", id="block-comment-token"),
    pytest.param("col`; DROP TABLE x", id="backtick"),
]


@pytest.fixture(scope="module")
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _extract_merge_guard(rendered: str, arg_key: str) -> str:
    """Slice the guard block out of the rendered job: from the merge_key
    assignment through the ``raise ValueError`` line (inclusive).

    The assignment resolves the runtime ``--{arg_key}`` argument via the
    R19.9 ``_runtime_arg`` mechanism (the former args-dict read could never
    see the argument — caught live in task 17.2 verification).
    """
    lines = rendered.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.strip().startswith(f'merge_key = _runtime_arg("{arg_key}"')
    )
    end = next(i for i, line in enumerate(lines[start:], start) if "raise ValueError" in line)
    return textwrap.dedent("\n".join(lines[start : end + 1]))


def _run_guard(guard_code: str, merge_key_arg: dict) -> dict:
    """Execute the extracted guard with the given runtime args; returns namespace.

    ``_runtime_arg`` is stubbed with the same contract as the rendered
    mechanism (return the runtime argument when supplied, else the default)
    so the guard logic under test — length + allowlist + raise — runs
    against the hostile/benign values exactly as it would in the job.
    """
    namespace = {"_runtime_arg": lambda name, default="": merge_key_arg.get(name, default)}
    # exec is deliberate: this runs the job's own rendered guard under test.
    exec(guard_code, namespace)
    return namespace


_CASES = [
    pytest.param("glue_job_s3_tables.py.j2", BASE_VARS, "merge_key", id="s3_tables-inline"),
    pytest.param("glue_job_jdbc_source.py.j2", JDBC_SOURCE_VARS, "target_merge_key", id="jdbc_source-partial"),
]


class TestRenderedGuardExecution:
    """The guard shipped inside the job rejects hostile runtime arguments.

    **Validates: Requirements 12.1, 12.2, 12.4**
    """

    @pytest.mark.parametrize("template_name,render_vars,arg_key", _CASES)
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_runtime_merge_key_raises(self, jinja_env, template_name, render_vars, arg_key, hostile):
        rendered = jinja_env.get_template(template_name).render(**render_vars)
        guard = _extract_merge_guard(rendered, arg_key)

        with pytest.raises(ValueError, match="Invalid merge_key identifier") as exc_info:
            _run_guard(guard, {arg_key: hostile})

        # R12.2: the failure names the offending identifier.
        assert repr(hostile) in str(exc_info.value)

    @pytest.mark.parametrize("template_name,render_vars,arg_key", _CASES)
    def test_valid_runtime_merge_key_passes(self, jinja_env, template_name, render_vars, arg_key):
        rendered = jinja_env.get_template(template_name).render(**render_vars)
        guard = _extract_merge_guard(rendered, arg_key)

        namespace = _run_guard(guard, {arg_key: "customer_id"})
        assert namespace["merge_key"] == "customer_id"

    @pytest.mark.parametrize("template_name,render_vars,arg_key", _CASES)
    def test_default_merge_key_passes(self, jinja_env, template_name, render_vars, arg_key):
        """Absent argument falls back to 'id', which passes the allowlist."""
        rendered = jinja_env.get_template(template_name).render(**render_vars)
        guard = _extract_merge_guard(rendered, arg_key)

        namespace = _run_guard(guard, {})
        assert namespace["merge_key"] == "id"

    @pytest.mark.parametrize("template_name,render_vars,arg_key", _CASES)
    def test_overlong_runtime_merge_key_raises(self, jinja_env, template_name, render_vars, arg_key):
        rendered = jinja_env.get_template(template_name).render(**render_vars)
        guard = _extract_merge_guard(rendered, arg_key)

        with pytest.raises(ValueError, match="Invalid merge_key identifier"):
            _run_guard(guard, {arg_key: "k" * 256})


class TestGuardPrecedesSql:
    """The guard sits between the argument read and the MERGE SQL assembly.

    **Validates: Requirements 12.1**
    """

    @pytest.mark.parametrize("template_name,render_vars,arg_key", _CASES)
    def test_guard_before_merge_sql(self, jinja_env, template_name, render_vars, arg_key):
        rendered = jinja_env.get_template(template_name).render(**render_vars)

        guard_pos = rendered.index("Invalid merge_key identifier for MERGE INTO")
        sql_pos = rendered.index("MERGE INTO {target_table}")
        assert guard_pos < sql_pos, "the identifier guard must run before the MERGE SQL is assembled"
