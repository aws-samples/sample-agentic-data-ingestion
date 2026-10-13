# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The transform statement is submitted at top level, not at the job's indent.

Spec ``real-spark-verification``, defect 1 — the live defect that made **every**
Glue verification fail. Observed on run ``run-2026-08-18-17-48-19-bd6f4deb``
(pipeline ``helloworld-f9a76ce9``, account 777788889999, ``us-west-2``), in the
AgentCore log for ``agentic_data_ingestion_agent_test_code``::

    Glue statement 0 submitted for stage 'test data generation'   -> ok
    Glue statement 1 submitted for stage 'transform execution'
    Glue statement failed at stage 'transform execution'
    Glue transform execution failed: IndentationError: unexpected indent (<stdin>, line 2)

**Validates: Requirement 1.4** — "execute the Transform_Block without
translating it". Removing a common leading indent is not a translation, and this
file is where that distinction is made checkable: every test below asserts both
halves, that the statement *compiles at top level* and that *no statement text
changed*.

Why 1000+ tests and 19 properties missed it
-------------------------------------------

Every existing test passes a transform block at **column zero**. On that input
the dedent is an identity, so the whole class of defect is invisible. The live
input is not at column zero: ``lambda_function.extract_transform_block``
(``:178``) returns ``code[start_idx:end_idx].rstrip()`` — a raw slice — and all
five ``pyspark_templates/*.j2`` render the block as
``{{ transform_block | indent(4, first=False) }}`` inside ``main()``'s ``try:``.
So the gap was the *fixtures*, not the assertions, and closing it is the more
important half of this fix.

Two of the cases here are therefore built from **committed golden renders**
(``tests/fixtures/golden_scripts/``) put through the **real** extractor, rather
than from a string this file wrote: a fixture this file invents can drift back to
column zero, a golden render cannot without the render changing.

Why the fix is not in ``extract_transform_block``
------------------------------------------------

Its output has three consumers, and only one of them wants a dedent applied by
the extractor:

* ``PySpark2PandasConverter.convert`` already dedents its input
  (``pyspark_to_pandas.py:1948``) — dedenting upstream would dedent twice
* the reviewer prompt renders the block as the reviewer's subject
* this executor, which is the one that needs it

Fixing it upstream would move an established behaviour under two consumers that
did not ask, to serve the third. ``glue_session_runner.transform_statement`` is
the one place that submits the block, so it is where the normalisation belongs.

Ledger
------

``why`` on ``glue_session_runner.py`` returns ``LGR-ad78946-1`` … ``-5``, all
from the 60-file p42 commit and all carrying ``NOTE: BROAD``. None of them
governs the transform statement's text: ``-1`` is executor selection, ``-2``
``conversion_report``, ``-3`` the shadow row, ``-4`` IAM, ``-5`` the missing-role
fallback. So there is **no fence on this behaviour** — stated as a gap in the
ledger rather than as an all-clear. ``LGR-4dd9b42-1`` (**template / ratified**)
is the record that governs the pandas proxy's own refusals and is why the
dedent's precedent on that path is quotable; it does not reach this file.

Sibling coverage — not duplicated here
--------------------------------------

* ``test_run_test_composition.py`` — the composition and the pre-flight order,
  including ``test_the_transform_reaches_spark_byte_for_byte`` on a column-zero
  block. That test is the *unchanged-text* claim at its strongest (exact
  equality); this file is the same claim on input that actually needs
  normalising.
* ``test_extract_transform_block.py`` — the extractor's marker contract. It
  asserts ``TRANSFORM_LINE.strip() in block``, and the ``.strip()`` there is
  precisely what hid the indentation from view.
* ``test_validation_statement.py`` — the validation statement's own text.
"""

import os
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import (LGR-3e45e65-1 pins these in
# tests/unit/conftest.py; these setdefaults are consistent no-ops).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))

from glue_session_runner import (
    STAGE_TRANSFORM,
    GlueSessionTestRunner,
    dedent_for_statement,
    transform_statement,
)

# ``lambda_function`` is imported for the REAL extractor — the point of the
# golden-render cases is that the input reaching the runner is the input the live
# path produces, and a transcribed copy of it cannot make that claim. Same mock
# set and same ordering as ``test_session_capacity_non_verification.py``:
# ``glue_session_runner`` is imported above, against the real
# ``code_interpreter_runner``, before that module is mocked out here.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}
with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _handler


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _handler
    yield


# ---------------------------------------------------------------------------
# Fixtures — realistically indented input
# ---------------------------------------------------------------------------

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"
SCHEMA = {"properties": {"name": {"type": "string"}}}
TARGET_SCHEMA = {"properties": {"name": {"type": "string"}}}

GENERATED_STDOUT = 'GENERATED:{"record_count": 100, "columns": ["name"]}'
VALIDATION_STDOUT = 'VALIDATION:{"record_count": 100, "schema_compliance": 100.0}'

#: Two committed golden renders of real generated jobs. The first carries a
#: one-line transform at the templates' 4 spaces; the second is the
#: ``checkpoint_enabled`` shape, where the block between the markers is a
#: ``try:``/``except`` at 4 with the transform nested at 8 — so it also pins
#: that relative depth survives.
GOLDEN_RENDERS = ("s3_tables_append.py", "s3_tables_append_checkpoint.py")


def _golden_source(name: str) -> str:
    with open(os.path.join(_REPO_ROOT, "tests", "fixtures", "golden_scripts", name)) as handle:
        return handle.read()


#: A job shaped exactly as the templates render one: the block one level inside
#: ``main()``'s ``try:``, which is the shape named in the defect report. Written
#: out as well as drawn from the goldens because it is the case a reader checks
#: the reasoning against, and because it carries a transform with a nested body.
TEMPLATE_SHAPED_JOB = """import sys
from pyspark.sql.functions import col, initcap


def main():
    try:
        df = spark.read.csv(source_path)

        # === AI-Generated Transform Block ===
        df_transformed = df.withColumn("name", initcap(col("name")))
        if "amount" in df_transformed.columns:
            df_transformed = df_transformed.withColumn("amount", col("amount").cast("double"))
        # === End Transform Block ===

        df_transformed.write.parquet(output_path)
    finally:
        job.commit()
"""


def _ok(text: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}


@pytest.fixture
def glue():
    """A Glue client wired so a full three-stage run succeeds."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    client.get_statement.side_effect = [
        {"Statement": _ok(GENERATED_STDOUT)},
        {"Statement": _ok()},
        {"Statement": _ok(VALIDATION_STDOUT)},
    ]
    return client


@pytest.fixture
def runner(glue):
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


def _submitted(glue: MagicMock) -> list[str]:
    return [call.kwargs["Code"] for call in glue.run_statement.call_args_list]


def _compiles_at_top_level(code: str) -> bool:
    """Whether Glue's interpreter would accept *code* as a statement.

    ``compile`` in ``exec`` mode is the same judgement a Glue statement gets: a
    fresh top-level scope with no enclosing block. A ``SyntaxError`` other than
    ``IndentationError`` is NOT swallowed — this helper answers "would the indent
    be rejected", so a fixture that is broken for some other reason must fail
    loudly rather than read as an indentation problem.
    """
    try:
        compile(code, "<stdin>", "exec")
    except IndentationError:
        return False
    return True


# ---------------------------------------------------------------------------
# The defect, on the input the live path produces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("golden", GOLDEN_RENDERS)
def test_the_extractor_output_from_a_real_render_is_indented(golden):
    """The premise, asserted rather than assumed.

    If this ever fails, the golden render stopped indenting the block and the
    tests below became vacuous — which is the failure mode that let this defect
    ship. Asserted on the extractor's real output, so it covers the extractor
    and the render together.
    """
    block = _handler.extract_transform_block(_golden_source(golden))

    assert block.strip(), "the golden render produced an empty transform block"
    assert not _compiles_at_top_level(block), (
        "the extracted block already compiles at top level, so this file no longer exercises the defect"
    )


@pytest.mark.parametrize("golden", GOLDEN_RENDERS)
def test_a_real_renders_block_is_submitted_at_top_level(runner, glue, golden):
    """R1.4, end to end: golden render → real extractor → submitted statement.

    **Validates: Requirement 1.4**

    The whole live chain except Glue itself. What the previous behaviour
    submitted here is what the AgentCore log recorded: an indented statement,
    rejected with ``IndentationError`` before Spark evaluated anything.
    """
    block = _handler.extract_transform_block(_golden_source(golden))

    runner.run_test(block, SCHEMA, TARGET_SCHEMA)

    submitted = _submitted(glue)[1]
    assert _compiles_at_top_level(submitted), f"the transform statement is still indented:\n{submitted!r}"


@pytest.mark.parametrize("golden", GOLDEN_RENDERS)
def test_a_real_renders_block_keeps_every_statement_unchanged(golden):
    """The other half of R1.4: only whitespace moved.

    Line for line, ``strip()``-equal to the input. That is the strongest form of
    "nothing was translated" available on input that must change *somehow*: an
    added, dropped, reordered or rewritten line fails here, and so does a
    changed literal or identifier.
    """
    block = _handler.extract_transform_block(_golden_source(golden))

    _, code = transform_statement(block)

    assert [line.strip() for line in code.split("\n")] == [line.strip() for line in block.split("\n")]


def test_the_nested_body_inside_a_transform_keeps_its_relative_depth(runner, glue):
    """A dedent that flattened nesting would change what the code means.

    The ``if`` body below is at 12 in the job and must land at 4, not at 0 —
    at 0 it would run unconditionally. Asserted on the submitted statement, so
    it is the text Spark receives.
    """
    block = _handler.extract_transform_block(TEMPLATE_SHAPED_JOB)

    runner.run_test(block, SCHEMA, TARGET_SCHEMA)

    submitted = _submitted(glue)[1]
    assert _compiles_at_top_level(submitted)
    assert submitted == textwrap.dedent("""\
        df_transformed = df.withColumn("name", initcap(col("name")))
        if "amount" in df_transformed.columns:
            df_transformed = df_transformed.withColumn("amount", col("amount").cast("double"))""")


def test_the_statement_is_paired_with_the_transform_stage():
    """The label and the code are joined in one place, as with validation.

    A statement submitted under the wrong stage mis-attributes its own failure,
    which is the reason ``validation_statement`` exists in that shape.
    """
    stage, _ = transform_statement("df_transformed = df")

    assert stage == STAGE_TRANSFORM


# ---------------------------------------------------------------------------
# dedent_for_statement — the shapes plain textwrap.dedent does not strip
# ---------------------------------------------------------------------------


def test_a_block_already_at_column_zero_is_returned_byte_for_byte():
    """The identity case, and it must be an identity.

    ``extract_transform_block`` returns the WHOLE FILE when the opening marker
    is absent (``:190-192``). That is already valid top-level Python, and every
    existing test in the suite passes column-zero input, so anything other than
    an identity here is a behaviour change to the passing path.
    """
    block = 'df_transformed = df.withColumn("name", initcap(col("name")))\nprint(df_transformed.count())'

    assert dedent_for_statement(block) == block


#: Each case is a block plain ``textwrap.dedent`` leaves indented, with the input
#: shape that produces it. Both are reachable from the live path, and both are
#: valid Python once the block's own indent is off — which is what lets these two
#: assert a compiling result. The third reachable shape is not, and it has its
#: own test below.
DEFEATS_PLAIN_DEDENT = {
    # A comment-only line at a shallower column. Valid Python — the tokenizer
    # emits no INDENT/DEDENT for a comment-only line — and it reduces
    # `textwrap.dedent`'s common prefix to nothing.
    "comment at column zero": (
        "    df_a = df.filter(col('x') > 0)\n# the model wrote this comment flush left\n    df_transformed = df_a",
        "df_a = df.filter(col('x') > 0)\n# the model wrote this comment flush left\ndf_transformed = df_a",
    ),
    # Tabs against spaces: `textwrap.dedent` compares prefixes as text, so there
    # is no common prefix at all and it returns the block unchanged.
    "tab-indented line among spaces": (
        "    df_a = df\n\tdf_transformed = df_a",
        "df_a = df\ndf_transformed = df_a",
    ),
}


@pytest.mark.parametrize(
    ("block", "expected"),
    list(DEFEATS_PLAIN_DEDENT.values()),
    ids=list(DEFEATS_PLAIN_DEDENT),
)
def test_a_shape_that_defeats_plain_dedent_is_still_stripped(block, expected):
    """Each of these is why the fix is not one call to ``textwrap.dedent``.

    The contrast is asserted, not described: ``textwrap.dedent`` is shown
    failing on the same input in the same test, so a future simplification back
    to it fails here with the reason attached.
    """
    assert not _compiles_at_top_level(textwrap.dedent(block)), (
        "textwrap.dedent handled this shape, so this case no longer earns its place"
    )

    assert dedent_for_statement(block) == expected
    assert _compiles_at_top_level(dedent_for_statement(block))


def test_a_truncated_block_fails_on_its_real_problem_and_not_on_the_indent():
    """The third shape, and the honest limit of what a dedent can do for it.

    ``extract_transform_block`` returns ``code[start_idx:]`` when the CLOSING
    marker is absent (``:187-188``), and every template's tail carries
    ``except Exception as e:`` at column 0 (``glue_job_s3_parquet.py.j2:120``).
    That block does not parse at top level under any dedent — a dangling
    ``except`` is a ``SyntaxError`` on its own terms — so this test does **not**
    claim it compiles.

    What it does claim is the part that matters to whoever reads the failure: the
    block is anchored at column 0, so the reported error names the dangling
    ``except`` rather than an indent on line 1. Before this fix the two shapes
    were indistinguishable in the log — every failure read
    ``IndentationError: unexpected indent``, whatever was actually wrong.
    """
    block = "    df_transformed = df\nexcept Exception as e:\n    raise"

    result = dedent_for_statement(block)

    assert result.split("\n")[0] == "df_transformed = df"
    with pytest.raises(SyntaxError) as caught:
        compile(result, "<stdin>", "exec")
    assert not isinstance(caught.value, IndentationError), (
        f"the failure still reads as an indentation problem: {caught.value}"
    )


def test_a_deeper_comment_keeps_its_relative_indent():
    """Only a comment SHALLOWER than the anchor is flattened.

    A comment sitting with the body it describes stays with it, so the dedent
    does not quietly reformat the reviewer-visible block.
    """
    block = "    if x:\n        # explains the branch\n        df_transformed = df"

    assert dedent_for_statement(block) == "if x:\n    # explains the branch\n    df_transformed = df"


def test_a_whitespace_only_line_is_emptied():
    """Matching ``textwrap.dedent``'s own normalisation.

    A line of trailing spaces inside the block is not an indent, and leaving it
    as spaces would let it read as one to the next reader of a diff.
    """
    assert (
        dedent_for_statement("    df_a = df\n      \n    df_transformed = df_a") == "df_a = df\n\ndf_transformed = df_a"
    )


@pytest.mark.parametrize(
    "block",
    ["", "\n", "   \n  \n", "    # only a comment\n    # and another"],
    ids=["empty", "one newline", "whitespace only", "comments only"],
)
def test_a_block_with_no_statement_does_not_raise(block):
    """There is no anchor to read, so the fallback has to be total.

    None of these reaches the live path — the frame refusal and the reviewer gate
    are upstream — but a helper that raises on one of them would turn an empty
    block into a lifecycle exception instead of a transform that does nothing.
    """
    result = dedent_for_statement(block)

    assert isinstance(result, str)
    assert _compiles_at_top_level(result)
