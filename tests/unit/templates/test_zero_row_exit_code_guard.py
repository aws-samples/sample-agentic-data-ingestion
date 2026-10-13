# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R1.6 / do-NOT 2 — the zero-row branch keeps exiting **zero**. A regression guard.

WHY THIS FILE EXISTS, AND WHY IT IS NOT A CHANGE.

p64's phase 1a adds two refusals *upstream* of this branch: a zero-byte refusal
in ``file_validation`` (1a.3, R1.1) and an empty-schema refusal in
``schema_extract``'s CSV branch (1a.4, R1.3). Both turn an empty source into a
permanent ``StepFailure`` early. Someone reading that phase can very reasonably
conclude that the Glue template's ``sys.exit(0)`` on zero rows is the same
defect one step later, and "finish the job" by making it exit non-zero.

**It is not the same defect, and this file is what stops that edit.**

  * ``requirements.md`` **R1.6**: the zero-row branch in the Glue templates MUST
    keep exiting **0**. ``design.md`` do-NOT **2**: do not make it fail — it
    protects the empty *scheduled JDBC poll*, where a poll that legitimately
    finds no new rows is the normal case, not an error.

  * ``LGR-9e76249-1`` (**template / ratified**) makes the Load_Outcome
    vocabulary **display-only** — "the run keeps its ``SUCCEEDED``
    Run_Status" — and ``LGR-9e76249-2`` puts the derivation on the **read
    path**, never persisted. So ``empty_source`` **cannot** turn a run FAILED
    *by construction*, not merely by current wiring. A non-zero exit here would
    be the one way to defeat that, from outside the module those records govern.

  * ``shared/load_outcome.py:381-393`` classifies the ``rows_read=0``,
    ``rows_written=0``, ``write_confirmed=False`` triple as ``empty_source`` —
    "a legitimate no-op, not a data-loss event".

  * ``job_completion/lambda_function.py:770`` reads the Glue state and ``:837``
    maps it: ``"success" if state == "SUCCEEDED" else "failed"``. That mapping
    has no third arm. A non-zero exit therefore does not add a nuance — it
    flips a legitimate no-op into a **reported failure**, which is the same
    class of lie R1 exists to remove, pointing the other way.

SCOPE. This module asserts and changes nothing. Group **2b** is the task that
restructures these templates (it extracts ``glue_job_s3_tables.py.j2:174-341``
to a partial, entirely *after* the exit at ``:124``), and 2b.1's do-NOT 5
constrains what may move.

WHAT IS PINNED, AND WHY IT IS AST RATHER THAN SUBSTRING. A substring test for
``"sys.exit(0)"`` passes on a *comment* mentioning it and cannot tell
``sys.exit(0)`` from ``sys.exit(False)``. ``LGR-b46a5bb-4`` (**template /
ratified**) records the governing lesson — substring matching a fence forbids
documenting the fence — so every assertion here runs against the **parsed**
rendered script:

  1. exactly one ``if source_count == 0:`` guard, and its comparison is
     structurally ``source_count == 0`` (so ``if False:``, ``if source_count <
     0:`` or a flipped comparator all fail) — the branch is still *reached* by a
     zero-row DataFrame rather than being dead code;
  2. ``source_count`` is still assigned from a ``.count()`` call **before** the
     guard, so the guard reads a real row count and not a stale constant;
  3. the branch's exit is ``sys.exit(0)`` with the argument asserted as the
     **int literal 0** — ``sys.exit(1)`` fails, and so does ``sys.exit(False)``,
     which a bare ``== 0`` comparison would wave through;
  4. that exit is the **last** statement of the branch, so the branch always
     exits and nothing was appended after it as dead code;
  5. the empty path still **commits** before leaving, so the Glue bookmark is
     not stranded — directly in the branch for the four S3-source templates,
     and via the enclosing ``finally:`` for ``glue_job_jdbc_source.py.j2``,
     whose branch deliberately has no commit of its own (``LGR-0450623-1``).

DELIBERATELY SELF-CONTAINED. ``test_load_counts_render.py`` has richer render
machinery and this module does not import it. That module is p37's fence on the
*counts block*; its ``_early_exit_region`` helper happens to assert
``sys.exit(0)`` as a precondition, which is incidental coverage — the failure
reads as a counts-positioning problem and no docstring there says the exit code
is deliberate or why. Coupling this guard to that module would let a later
restructuring of it carry this fence away silently.

Validates: Requirements 1.6
"""

from __future__ import annotations

import ast
import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = 'df_transformed = df.withColumn("name", col("name").cast(StringType()))'

# Shape only; no rule is evaluated here. Present so glue_job_s3_tables.py.j2's
# `{% if dqdl_rules %}` arms render — they are the arms most likely to
# restructure the region this module pins.
SAMPLE_DQDL = 'Rules = [ RowCount > 0, Completeness "id" >= 0.95 ]'

# The four S3_UPLOAD-source templates commit inside the branch; the JDBC-source
# template deliberately does not (LGR-0450623-1, backfilled: the redundant
# commit was removed because the `finally` block already commits and
# `sys.exit(0)` unwinds through it). Both shapes still commit on the empty path,
# which is the bookmark claim — they differ only in where.
S3_SOURCE_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
]
JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
ALL_TEMPLATES = [*S3_SOURCE_TEMPLATES, JDBC_SOURCE_TEMPLATE]


@pytest.fixture
def jinja_env() -> jinja2.Environment:
    """StrictUndefined, matching ``code_assembly._get_pyspark_jinja_env``."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render_variants(env: jinja2.Environment, name: str) -> list[tuple[str, str]]:
    """``(label, rendered)`` over the axes that could restructure the branch.

    For the four S3-source templates that is ``checkpoint_enabled`` x
    ``dqdl_rules``; ``write_mode`` is not varied because it only affects code
    *after* the early exit. For ``glue_job_jdbc_source.py.j2`` it is
    ``read_mode``, whose incremental arm inserts a ``df.unpersist()`` **inside**
    the branch — the one axis that changes the branch body's contents.
    """
    template = env.get_template(name)
    if name == JDBC_SOURCE_TEMPLATE:
        return [
            (
                f"{name} read_mode={read_mode}",
                template.render(
                    engine="postgres",
                    target_type="S3_TABLES",
                    read_mode=read_mode,
                    write_mode="overwrite",
                    partition_columns="",
                    merge_key="id",
                    initial_watermark="2024-01-01T00:00:00Z" if read_mode == "incremental" else "",
                    transform_block=SAMPLE_TRANSFORM,
                ),
            )
            for read_mode in ("full_table", "incremental")
        ]
    return [
        (
            f"{name} checkpoint={checkpoint_enabled} dqdl={bool(dqdl_rules)}",
            template.render(
                transform_block=SAMPLE_TRANSFORM,
                source_format="json",
                partition_columns="",
                merge_key="id",
                write_mode="overwrite",
                checkpoint_enabled=checkpoint_enabled,
                checkpoint_bucket="cp-bucket" if checkpoint_enabled else "",
                dqdl_rules=dqdl_rules,
                quarantine_bucket="quarantine-bucket",
            ),
        )
        for checkpoint_enabled in (False, True)
        for dqdl_rules in ("", SAMPLE_DQDL)
    ]


def _zero_row_guard(tree: ast.Module, label: str) -> ast.If:
    """The single ``if source_count == 0:`` node, located structurally.

    Matching on ``ast.If`` whose test is a ``Compare`` against the name
    ``source_count`` — not on the rendered text — so a comment mentioning the
    guard cannot satisfy it and a reshaped comparison cannot hide behind it.
    """
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "source_count"
    ]
    assert len(guards) == 1, (
        f"{label}: expected exactly one `if source_count == ...:` guard, found "
        f"{len(guards)}. R1.6 pins this branch; if it moved or multiplied, read "
        f"R1.6 and design.md do-NOT 2 before changing this test."
    )
    return guards[0]


def _sys_exit_calls(body: list[ast.stmt]) -> list[ast.Call]:
    """``sys.exit(...)`` calls that are DIRECT statements of ``body``.

    Direct rather than recursive on purpose: an exit nested inside a further
    ``if`` would be a conditional exit, which assertion 4 must catch rather
    than accept.
    """
    calls = []
    for statement in body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "exit"
            and isinstance(statement.value.func.value, ast.Name)
            and statement.value.func.value.id == "sys"
        ):
            calls.append(statement.value)
    return calls


def _commit_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """``job.commit()`` calls that are DIRECT statements of ``body``."""
    return [
        statement
        for statement in body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "commit"
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "job"
    ]


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_zero_row_branch_exits_zero(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R1.6 / do-NOT 2: the exit code is the int literal ``0``, in all five templates.

    THE ONE ASSERTION THIS MODULE EXISTS FOR. ``sys.exit(1)`` fails here, and
    so does ``sys.exit(False)`` — ``False == 0`` is True in Python, so the
    literal's *type* is asserted too, otherwise the check a reviewer trusts
    would pass on a boolean.

    A non-zero exit is a Glue ``FAILED``, and
    ``job_completion/lambda_function.py:837`` maps anything that is not
    ``SUCCEEDED`` to ``"failed"`` with no third arm. So this is not a cosmetic
    pin: it is what keeps ``LGR-9e76249-1``'s display-only Load_Outcome
    ("the run keeps its ``SUCCEEDED`` Run_Status") true from outside the module
    that record governs.
    """
    for label, rendered in _render_variants(jinja_env, template_name):
        guard = _zero_row_guard(ast.parse(rendered), label)
        exits = _sys_exit_calls(guard.body)
        assert len(exits) == 1, (
            f"{label}: expected exactly one sys.exit(...) as a direct statement of "
            f"the zero-row branch, found {len(exits)}"
        )
        call = exits[0]
        assert not call.keywords, f"{label}: sys.exit takes no keyword arguments"
        assert len(call.args) == 1, (
            f"{label}: expected sys.exit to carry exactly one argument, found "
            f"{len(call.args)} — a bare sys.exit() exits 0 today but states nothing, "
            f"and R1.6 is about the code being deliberate"
        )
        argument = call.args[0]
        assert isinstance(argument, ast.Constant), (
            f"{label}: the exit code must be a literal, found "
            f"{ast.unparse(argument)!r} — a computed exit code cannot be pinned"
        )
        assert argument.value is not True and argument.value is not False, (
            f"{label}: the exit code must be the int 0, not the boolean "
            f"{argument.value!r}. `False == 0` is True in Python, so a boolean "
            f"would slip past a value-only check"
        )
        assert argument.value == 0, (
            f"{label}: the zero-row branch must exit 0, found "
            f"sys.exit({argument.value!r}). This is R1.6 and design.md do-NOT 2: "
            f"an empty source is a legitimate no-op (shared/load_outcome.py:381-393), "
            f"the empty scheduled JDBC poll is the case this arm protects, and "
            f"LGR-9e76249-1 keeps the run SUCCEEDED by construction. p64's 1a.3/1a.4 "
            f"refuse an empty source UPSTREAM of here — they are not a reason to "
            f"fail it here."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_zero_row_branch_always_exits_and_nothing_follows_the_exit(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The exit is unconditional: it is the LAST statement of the branch.

    Two failures this catches that a mere "an exit exists" test does not. An
    exit nested inside a further condition would leave the empty source falling
    through into the transform and the write, which is the data path R1.6's
    branch exists to skip. A statement appended *after* the exit would be dead
    code, and dead code beside a fence is how a fence stops being read.
    """
    for label, rendered in _render_variants(jinja_env, template_name):
        guard = _zero_row_guard(ast.parse(rendered), label)
        assert guard.body, f"{label}: the zero-row branch has an empty body"
        last = guard.body[-1]
        assert _sys_exit_calls([last]), (
            f"{label}: the last statement of the zero-row branch must be "
            f"sys.exit(0), found {ast.unparse(last).splitlines()[0]!r}. An exit that "
            f"is not last is either conditional or has dead code after it."
        )
        assert not guard.orelse, (
            f"{label}: the zero-row branch has an `else` arm. The branch exits, so "
            f"an else is unreachable-by-inversion confusion rather than a behaviour "
            f"— read R1.6 before adding one."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_zero_row_branch_is_reached_by_a_real_row_count(jinja_env: jinja2.Environment, template_name: str) -> None:
    """The branch is live code: guarded by ``source_count == 0`` off a ``.count()``.

    Pins the two halves of "reachable on a zero-row DataFrame" that can be
    established without a Spark runtime:

      * the comparison is structurally ``source_count == 0`` — an ``Eq`` against
        the int literal ``0``. ``if False:``, ``if source_count == 1:`` and
        ``if source_count > 0:`` each fail, and each is a way to leave the exit
        in place while making it unreachable or making it fire on the wrong
        input;
      * ``source_count`` is assigned from a ``.count()`` call on a line before
        the guard, so the value compared is a row count taken from the frame
        rather than a constant or a leftover.

    Deliberately NOT asserted: that Spark returns 0 for an empty CSV. That is
    runtime behaviour outside this tree, and phase 0's O1 verdict records it as
    the one gap it could not close. Claiming it here would be a fence resting
    on something this test cannot see.
    """
    for label, rendered in _render_variants(jinja_env, template_name):
        tree = ast.parse(rendered)
        guard = _zero_row_guard(tree, label)

        compare = guard.test
        assert isinstance(compare, ast.Compare)  # narrowed by _zero_row_guard
        assert len(compare.ops) == 1 and isinstance(compare.ops[0], ast.Eq), (
            f"{label}: the guard must compare with `==`, found {ast.unparse(compare)!r}"
        )
        comparator = compare.comparators[0]
        assert isinstance(comparator, ast.Constant) and comparator.value == 0, (
            f"{label}: the guard must test `source_count == 0`, found "
            f"{ast.unparse(compare)!r}. A guard that fires on anything else is a "
            f"different requirement than R1.6."
        )

        counted = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "source_count" for t in node.targets)
            and ".count()" in ast.unparse(node.value)
        ]
        assert counted, (
            f"{label}: `source_count` must be assigned from a `.count()` call — "
            f"without it the guard compares something that is not a row count"
        )
        assert any(assign.lineno < guard.lineno for assign in counted), (
            f"{label}: the `source_count = ....count()` assignment must precede the "
            f"guard at line {guard.lineno}, else the branch is dead code"
        )


@pytest.mark.parametrize("template_name", S3_SOURCE_TEMPLATES)
def test_s3_source_zero_row_branch_commits_before_exiting(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R1.6's "commits" clause: the empty path does not strand the bookmark.

    ``job.commit()`` is what finalises the Glue job bookmark. An exit that
    skipped it would leave the run's bookmark unadvanced — a *different* defect
    from the exit code, and one that a reader trimming "redundant" lines out of
    this branch could introduce while leaving ``sys.exit(0)`` untouched. So the
    ordering is asserted, not just the presence.

    Scoped to the four S3-source templates on purpose;
    ``glue_job_jdbc_source.py.j2`` commits through its ``finally`` instead and
    is covered by the test below.
    """
    for label, rendered in _render_variants(jinja_env, template_name):
        guard = _zero_row_guard(ast.parse(rendered), label)
        commits = _commit_statements(guard.body)
        assert len(commits) == 1, (
            f"{label}: expected exactly one job.commit() as a direct statement of "
            f"the zero-row branch, found {len(commits)}"
        )
        exit_call = _sys_exit_calls(guard.body)[0]
        assert commits[0].lineno < exit_call.lineno, (
            f"{label}: job.commit() must precede sys.exit(0) in the zero-row branch "
            f"— a commit after the exit never runs, and the bookmark is stranded"
        )


def test_jdbc_source_zero_row_exit_unwinds_through_a_committing_finally(
    jinja_env: jinja2.Environment,
) -> None:
    """The other half of the bookmark claim, for the template with no branch commit.

    ``glue_job_jdbc_source.py.j2``'s zero-row branch deliberately emits **no**
    ``job.commit()``: ``sys.exit(0)`` raises ``SystemExit``, which unwinds
    through the enclosing ``finally:``, and that block already commits exactly
    once. The rendered template says so itself, in a ``# NOTE: do NOT call
    job.commit() here.`` comment inside the branch.

    ATTRIBUTION, STATED PRECISELY. ``test_load_counts_render.py`` credits that
    removal to ``LGR-0450623-1`` and its double-commit history to
    ``LGR-334667e-1``, both as **backfilled** records. Neither id resolves in the
    ledger as it stands — ``record()`` returns nothing for them, and ``rootmap``
    reports the ledger as template-provenance only with zero backfilled records.
    So they are cited here as *that test module's own attribution*, not as
    ratified decisions retrieved from the ledger. The held assertion is this
    test and the template's own NOTE.

    ``test_load_counts_render.py::test_empty_exit_commit_asymmetry_is_preserved``
    already pins *where* the commit is not. This asserts the fact that makes
    that absence safe and that nothing there covers: a ``try`` whose
    ``finally`` commits **lexically encloses** the guard. Move the guard out of
    that ``try`` and the exit becomes a commit-less exit — the bookmark defect
    — with the branch still reading as correct.
    """
    for label, rendered in _render_variants(jinja_env, JDBC_SOURCE_TEMPLATE):
        tree = ast.parse(rendered)
        guard = _zero_row_guard(tree, label)

        assert _commit_statements(guard.body) == [], (
            f"{label}: this template's zero-row branch must emit NO job.commit() — "
            f"the finally block already commits and sys.exit(0) unwinds through it "
            f"(LGR-0450623-1)"
        )

        enclosing = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and _commit_statements(node.finalbody)
            and node.lineno < guard.lineno <= (node.end_lineno or node.lineno)
        ]
        assert enclosing, (
            f"{label}: the zero-row guard must sit inside a `try` whose `finally` "
            f"calls job.commit(). Without that enclosure sys.exit(0) leaves without "
            f"committing and the Glue bookmark is stranded."
        )
