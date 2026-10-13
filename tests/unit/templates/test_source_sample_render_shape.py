# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render-shape fences for p53 C1's Sample_Artifact write (task 2.3).

The subject is `pyspark_templates/glue_job_jdbc_source.py.j2` — the JDBC
**source** template — and the question is whether C1's block has the shape C1
claims, in **every** configuration rather than one. Everything here is asserted
against RENDERED text; nothing in this module executes Spark, boto3 or the
generated script. Task 2.2 owns the executed bounds/coercion properties and
task 5.1 owns the moto flow, so a claim about runtime behaviour does not belong
here.

WHY A NEW MODULE RATHER THAN AN EXTENSION.
`test_glue_job_jdbc_source.py` is the template's general fence set and
`test_load_counts_render.py` is p37's; both are large, both are cited by
`LGR-9e76249-10`'s evidence list, and task 2.2 is writing into this directory
concurrently. A separate module keeps C1's fences findable and keeps three
tasks off one file. Where an existing assertion already holds, this module
**extends** it along an axis the original does not vary rather than restating
it — each such test says which one and how.

THE CONFIGURATION SPACE — 120, not 30.
`target_type` (5) x `read_mode` (2) x `write_mode` (3) x `partition_columns`
(2) x `target_secret_arn` (2). `test_load_counts_render.py::_jdbc_source_variants`
covers the first three (30 variants); the last two are this module's addition.
`target_secret_arn` USED to switch the JDBC/RDS write partial's credential path
— the one place a credential token could newly appear. Since the JDBC-target
credential fix (`LGR-819980f-1`) the plaintext arm is deleted and no template
reads `target_secret_arn`; the axis is kept so the credential fence below proves
both values now render one and the same Secrets Manager path.

MEASURED, ON THIS TREE, HEAD (`7382e31`) VERSUS THE WORKING TREE.
Every fence token below was counted in all 120 configurations both before and
after C1, and the numbers are the reason the assertions are exact counts rather
than "absent":

    token                       HEAD                     after C1
    put_object(                 0                        1
    spark.read.format("jdbc")   1 full / 2 incremental   unchanged
    SELECT                      0 full / 1 incremental   unchanged
    df.write / .write.json(     0                        unchanged
    db_user / db_password       2 full / 3 incremental   unchanged
    df.cache()                  0 full / 1 incremental   unchanged
    df.unpersist()              0 full / 2 incremental   unchanged
    .count()                    2 (3 for S3_TABLES)      unchanged
    .persist(                   0                        unchanged

C1 is therefore additive on exactly one token. An exact count is what makes
that provable: "no new `spark.read`" is unfalsifiable as an absence assertion
because the template legitimately has between one and five.

THE LINE BUDGET IS NOT RE-ASSERTED HERE, AND THAT IS DELIBERATE — see
`test_this_template_carries_no_rendered_line_fence`.

Validates: Requirements 1.6, 4.1, 4.4
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")
TEMPLATE_NAME = "glue_job_jdbc_source.py.j2"

# The same transform body every other template test module uses, so a count
# here is comparable with a count there.
SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

TARGET_TYPES = ["S3_TABLES", "GLUE_CATALOG", "JDBC", "RDS_DATABASE", "S3_PARQUET"]
READ_MODES = ["full_table", "incremental"]
WRITE_MODES = ["overwrite", "append", "merge"]
# The render form `code_assembly._partition_columns_argument` binds: each name
# as a string literal. The stored `"region,dt"` was once passed straight
# through, which rendered `partitionBy(region,dt)`, two undefined names.
PARTITION_COLUMNS = ["", '"region", "dt"']
# A secret-backed target used to switch the JDBC/RDS write partial to the runtime
# Secrets Manager fetch (`106e9250c7`), with a plaintext-arg arm for `None`. That
# arm is deleted (`LGR-819980f-1`); both values must now render the runtime fetch,
# which `test_credential_token_counts_are_unchanged` pins.
TARGET_SECRET_ARNS = [None, "arn:aws:secretsmanager:us-west-2:111122223333:secret:tgt-AbCdEf"]

# The two markers C1 emits around its block. Anchoring the region on these is
# what lets a fence be scoped to C1's own lines rather than to the whole
# script — the distinction the existing whole-script counts cannot make.
BLOCK_START = "# === p53 C1: bounded source sample."
BLOCK_END = "# === End p53 C1 ==="


class Variant:
    """One rendered script plus the render facts a fence is keyed on."""

    def __init__(self, label: str, rendered: str, *, target_type: str, read_mode: str):
        self.label = label
        self.rendered = rendered
        self.target_type = target_type
        self.read_mode = read_mode

    def __repr__(self) -> str:  # pragma: no cover - test diagnostics only
        return f"<Variant {self.label}>"

    @property
    def block(self) -> str:
        """C1's own rendered lines, delimited by its two markers.

        The slice starts at the BEGINNING of the marker's line, not at the
        marker itself, so the block's own indentation survives into the slice —
        `test_the_sample_block_is_indented_inside_the_job_try` reads it.
        """
        marker = self.rendered.index(BLOCK_START)
        start = self.rendered.rindex("\n", 0, marker) + 1
        end = self.rendered.index(BLOCK_END) + len(BLOCK_END)
        return self.rendered[start:end]


def _jinja_env() -> jinja2.Environment:
    """StrictUndefined, matching `code_assembly._get_pyspark_jinja_env`."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _variants(env: jinja2.Environment) -> Iterator[Variant]:
    """All 120 configurations, rendered once each."""
    template = env.get_template(TEMPLATE_NAME)
    for target_type in TARGET_TYPES:
        for read_mode in READ_MODES:
            for write_mode in WRITE_MODES:
                for partition_columns in PARTITION_COLUMNS:
                    for target_secret_arn in TARGET_SECRET_ARNS:
                        rendered = template.render(
                            engine="postgres",
                            target_type=target_type,
                            read_mode=read_mode,
                            write_mode=write_mode,
                            partition_columns=partition_columns,
                            merge_key="id",
                            initial_watermark=("2024-01-01T00:00:00Z" if read_mode == "incremental" else ""),
                            target_secret_arn=target_secret_arn,
                            transform_block=SAMPLE_TRANSFORM,
                        )
                        yield Variant(
                            f"target={target_type} read_mode={read_mode} write_mode={write_mode} "
                            f"partitioned={bool(partition_columns)} target_secret={bool(target_secret_arn)}",
                            rendered,
                            target_type=target_type,
                            read_mode=read_mode,
                        )


@pytest.fixture(scope="module")
def variants() -> list[Variant]:
    """The 120 rendered scripts, built once for the module.

    Rendering is ~1 ms each; building them per test would be 120 renders x the
    number of tests below for no additional coverage.
    """
    built = list(_variants(_jinja_env()))
    assert len(built) == 120, f"expected 120 configurations, built {len(built)}"
    return built


# ---------------------------------------------------------------------------
# AST helpers — the try/except fence is structural, not a substring
# ---------------------------------------------------------------------------


def _put_object_call_linenos(tree: ast.AST) -> list[int]:
    """Line numbers of every `<...>.put_object(...)` CALL in the script.

    A call node rather than a substring, so the assertion cannot be satisfied
    or broken by prose. `LGR-b46a5bb-4` (template / ratified) is the governing
    reason substring matching is avoided wherever a structural check is
    available: substring matching an invariant forbids documenting it.
    """
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "put_object"
    ]


def _guarding_try_blocks(tree: ast.AST, lineno: int) -> list[ast.Try]:
    """Every `try:` whose BODY (not its handlers) spans *lineno*, outermost first."""
    found: list[ast.Try] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.body:
            continue
        body_start = min(stmt.lineno for stmt in node.body)
        body_end = max(stmt.end_lineno or stmt.lineno for stmt in node.body)
        if body_start <= lineno <= body_end:
            found.append(node)
    return sorted(found, key=lambda n: min(stmt.lineno for stmt in n.body))


def _swallows_exception(handler: ast.ExceptHandler) -> bool:
    """True iff *handler* catches `Exception` (or bare) and does not re-raise."""
    catches_broadly = handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception")
    if not catches_broadly:
        return False
    return not any(isinstance(node, ast.Raise) for node in ast.walk(handler))


# ---------------------------------------------------------------------------
# Clause 1 — exactly one put_object, in every configuration
# ---------------------------------------------------------------------------


def test_exactly_one_put_object_in_every_configuration(variants: list[Variant]) -> None:
    """C1 renders exactly ONE `put_object` call, in all 120 configurations.

    This is the assertion the block's PLACEMENT exists to satisfy, so it is the
    one that proves the placement rather than assuming it. C1 sits after the
    three source-read arms have converged on a single `df`; putting it inside
    the arms would render three. `read_mode` is the axis that splits them, and
    it is varied here.

    Measured at HEAD: 0. After C1: 1. C1 is additive on exactly this token.
    """
    for variant in variants:
        tree = ast.parse(variant.rendered)
        calls = _put_object_call_linenos(tree)
        assert len(calls) == 1, (
            f"{variant.label}: expected exactly 1 put_object call, found {len(calls)} "
            f"at lines {calls}. Three would mean the block moved inside the read arms."
        )


def test_the_single_put_object_is_the_sample_write(variants: list[Variant]) -> None:
    """The one `put_object` is C1's, keyed on the arguments only C1 passes.

    Without this, the count above would still pass if C1's write were deleted
    and some unrelated `put_object` added. The `Key=` argument is the
    deterministic key task 3.1's `HeadObject` probe targets, so it is a
    contract rather than an implementation detail.
    """
    for variant in variants:
        assert "Bucket=_adi_smp_bucket" in variant.block, variant.label
        assert "Key=_adi_smp_key" in variant.block, variant.label
        assert 'ContentType="application/json"' in variant.block, variant.label
        # The key's shape, as schema_extract will probe it.
        assert 'f"samples/{_adi_smp_pipeline_id}/source-sample.json"' in variant.block, variant.label
        assert 'f"adi-processed-files-{_adi_smp_account}-{_adi_smp_region}"' in variant.block, variant.label


# ---------------------------------------------------------------------------
# Clause 2 — the write is inside a try/except that swallows (R1.6)
# ---------------------------------------------------------------------------


def test_the_sample_write_is_inside_a_try_that_swallows(variants: list[Variant]) -> None:
    """R1.6: a telemetry write must not fail a run whose read succeeded.

    Structural, and discriminating rather than nominal: the template's OUTER
    `try:` also spans this line and its `except Exception as e:` **re-raises**
    (template tail). So "is inside a try" alone would pass on a block with no
    handler of its own. The assertion is on the INNERMOST guarding `try` — it
    must catch `Exception` and contain no `raise`.
    """
    for variant in variants:
        tree = ast.parse(variant.rendered)
        (lineno,) = _put_object_call_linenos(tree)
        guards = _guarding_try_blocks(tree, lineno)
        assert guards, f"{variant.label}: the put_object call is not inside any try block"
        innermost = guards[-1]
        assert any(_swallows_exception(h) for h in innermost.handlers), (
            f"{variant.label}: the innermost try guarding the sample write does not catch "
            f"Exception without re-raising. R1.6 forbids a telemetry failure from failing the run."
        )
        # And the outer guard is still the one that DOES re-raise, so this test
        # cannot be satisfied by deleting the inner handler.
        assert not any(_swallows_exception(h) for h in guards[0].handlers), (
            f"{variant.label}: the outermost try now swallows exceptions. That would mean the "
            f"job's own failure path stopped propagating — a much larger change than C1."
        )


def test_the_sample_block_logs_on_both_the_success_and_the_failure_path(
    variants: list[Variant],
) -> None:
    """Risk N1: "no sample ever appeared" must be diagnosable from the job log.

    A swallowed failure that logs nothing is indistinguishable from a block
    that never ran. INFO on success and WARNING on failure make the two
    distinguishable, which is what the risk asks for.
    """
    for variant in variants:
        assert "logger.info(" in variant.block, variant.label
        assert "logger.warning(" in variant.block, variant.label
        assert "logger.error(" not in variant.block, (
            f"{variant.label}: a swallowed telemetry failure logged at ERROR would page on a run that succeeded."
        )


# ---------------------------------------------------------------------------
# Clause 3 — no new spark.read (R4.1, do-NOT 7)
# ---------------------------------------------------------------------------


def test_source_read_count_is_unchanged_by_the_sample_write(variants: list[Variant]) -> None:
    """R4.1 / do-NOT 7: the sample comes off a frame already read.

    An exact count per read_mode, because absence is not assertable — the
    template legitimately reads the source once (full_table) or twice
    (incremental: the watermark arm and the no-watermark first-run arm are both
    rendered). Measured identical at HEAD and after C1.
    """
    for variant in variants:
        expected = 1 if variant.read_mode == "full_table" else 2
        actual = variant.rendered.count('spark.read.format("jdbc")')
        assert actual == expected, (
            f"{variant.label}: expected {expected} JDBC source reads, found {actual}. "
            f"C1 must add none — the sample is taken from the frame the job already read."
        )


def test_the_sample_block_itself_contains_no_spark_read(variants: list[Variant]) -> None:
    """The stronger, localised form of the fence above.

    A whole-script count can be kept constant by removing a read elsewhere and
    adding one here. Scoping the assertion to C1's own lines cannot be.
    """
    for variant in variants:
        assert "spark.read" not in variant.block, variant.label
        assert "spark.sql" not in variant.block, variant.label
        assert ".load()" not in variant.block, variant.label


def test_the_sample_bound_is_applied_before_the_collect(variants: list[Variant]) -> None:
    """do-NOT 7's second forbidden shape: materialise the full frame, then bound.

    `.limit(100)` must be composed onto the frame BEFORE `.collect()`, in one
    expression, so no action ever sees the unbounded frame.
    """
    for variant in variants:
        assert "df.limit(100).collect()" in variant.block, (
            f"{variant.label}: expected the bound and the collect in one expression. "
            f"A collect on the unbounded frame is do-NOT 7's forbidden shape."
        )


# ---------------------------------------------------------------------------
# Clause 4 — no SELECT added (C2, do-NOT 6, do-NOT 7)
# ---------------------------------------------------------------------------


def test_select_count_is_unchanged_by_the_sample_write(variants: list[Variant]) -> None:
    """C2: no SQL is constructed. Asserted as an exact count, per read_mode.

    Task 2.3's brief is explicit that "no new SELECT" is too weak: a future
    change could slip one in while an absence assertion still passed on the
    incremental arm, which legitimately has one (the watermark subquery). The
    counts are 0 for full_table and 1 for incremental, measured identical at
    HEAD and after C1.

    If this fails on the full_table arm, the mechanism has moved from (d) to
    (b) and BC3, BC4, do-NOT 3, 4, 6 and 7 all re-engage — stop rather than
    adjust the number.
    """
    for variant in variants:
        expected = 0 if variant.read_mode == "full_table" else 1
        actual = variant.rendered.count("SELECT")
        assert actual == expected, (
            f"{variant.label}: expected {expected} SELECT occurrences, found {actual}. C2 constructs no SQL at all."
        )


def test_the_sample_block_constructs_no_sql_and_quotes_no_identifier(
    variants: list[Variant],
) -> None:
    """C2, localised. Also pins that BC3's toolkit is not engaged by C1.

    `validate_identifier` / `quote_identifier` are required wherever an
    identifier reaches SQL text (do-NOT 3). C1 reaching for either would be the
    signal that it had started building a query.
    """
    for variant in variants:
        block = variant.block
        for token in ("SELECT", "LIMIT", "WHERE", "FROM ", "quote_identifier", "validate_identifier"):
            assert token not in block, (
                f"{variant.label}: the sample block contains {token!r}. C2 constructs no SQL; "
                f"if it now does, the mechanism changed and four constraints re-engage."
            )


# ---------------------------------------------------------------------------
# Clause 5 — a single object, never a Spark directory write (C1)
# ---------------------------------------------------------------------------


def test_no_spark_write_of_the_source_frame_anywhere(variants: list[Variant]) -> None:
    """C1: `df.write.json(path)` emits a DIRECTORY, and a prefix has no extension.

    `sample_read_format` returns `None` for a prefix
    (`glue_session_runner.py:2191-2193`, and its docstring at `:2173-2177`
    names the folder-prefix case as the observed instance), so a directory
    would yield `SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT` and defeat consumer 3.
    A single `put_object` is required, not preferred.

    Scoped to `df.write` — the SOURCE frame. The target write is
    `df_transformed.write...` and is untouched by p53.
    """
    for variant in variants:
        assert variant.rendered.count("df.write") == 0, variant.label
        assert variant.rendered.count(".write.json(") == 0, variant.label


def test_the_sample_block_uses_no_spark_writer_at_all(variants: list[Variant]) -> None:
    """The localised form: C1's own lines contain no Spark write of any kind."""
    for variant in variants:
        block = variant.block
        for token in (".write.", "write.json", "write.parquet", "saveAsTable", "_SUCCESS"):
            assert token not in block, f"{variant.label}: the sample block contains {token!r}"


def test_the_artifact_is_one_json_array_object(variants: list[Variant]) -> None:
    """R1.3: a JSON array under a `.json` key is the only shape all four consumers read.

    Parquet has no branch in the p49 readers; CSV read with `inferSchema=False`
    makes every column a string; and `_SAMPLE_READERS["json"]` is
    `multiLine=True`, which expects an array rather than JSONL. The array is
    the intersection, and `json.dumps` of a list is what produces it.
    """
    for variant in variants:
        block = variant.block
        assert "json.dumps(_adi_smp_rows)" in block, variant.label
        assert "_adi_smp_rows = []" in block, variant.label
        # Newline-delimited output would defeat the multiLine reader.
        assert '"\\n".join' not in block, (
            f"{variant.label}: a newline-joined body would be JSONL, which "
            f'_SAMPLE_READERS["json"] (multiLine=True) cannot read.'
        )


# ---------------------------------------------------------------------------
# Clause 6 — no credential token (LGR-819980f-1, standing invariant)
# ---------------------------------------------------------------------------


def test_credential_token_counts_are_unchanged(variants: list[Variant]) -> None:
    """`LGR-819980f-1` (standing invariant): a credential travels only as a secret ARN.

    Exact counts per read_mode rather than absence, because the source read
    legitimately passes `db_user` / `db_password` as `.option()` values after
    fetching them from Secrets Manager at runtime. Measured 2 (full_table) and
    3 (incremental) at HEAD and after C1 — C1 adds neither.

    EXTENDED to the TARGET side by the JDBC-target credential fix, which is where
    this module's `target_secret_arn` axis used to matter. Before it, a JDBC/RDS
    variant rendered with `target_secret_arn=None` declared `target_jdbc_user` /
    `target_jdbc_password` and read them from `args` — the plaintext arm that
    leaked a target credential — and this test, counting only `db_*`, did not see
    it. Now, measured over all 120 variants after the fix:

      * `jdbc_user` / `jdbc_password` (any prefix): 0 everywhere;
      * `get_secret_value`: 1 (the source fetch), plus 1 for a JDBC/RDS target —
        the target fetch — whatever `target_secret_arn` was;
      * `target_secret_arn`: 2 for a JDBC/RDS target (declared, then fetched by),
        0 otherwise.
    """
    for variant in variants:
        expected = 2 if variant.read_mode == "full_table" else 3
        for token in ("db_user", "db_password"):
            actual = variant.rendered.count(token)
            assert actual == expected, (
                f"{variant.label}: expected {expected} occurrences of {token!r}, found {actual}. "
                f"LGR-819980f-1 is a standing invariant and has leaked a real credential once."
            )

        jdbc_target = variant.target_type in ("JDBC", "RDS_DATABASE")
        for token in ("jdbc_user", "jdbc_password"):
            assert variant.rendered.count(token) == 0, (
                f"{variant.label}: {token!r} appears in the rendered script. The plaintext target "
                f"credential arm is deleted; LGR-819980f-1 permits only a Secrets Manager reference."
            )
        expected_fetches = 2 if jdbc_target else 1
        assert variant.rendered.count("get_secret_value") == expected_fetches, (
            f"{variant.label}: expected {expected_fetches} get_secret_value call(s) — the source fetch"
            f"{' and the target fetch' if jdbc_target else ''}."
        )
        assert variant.rendered.count("target_secret_arn") == (2 if jdbc_target else 0), variant.label


def test_the_sample_block_touches_no_credential(variants: list[Variant]) -> None:
    """The localised form. C1 reads the ARN's region/account SEGMENTS only.

    Splitting `--secret_arn` on `:` yields the region and account without an
    API call. That is a read of the ARN's shape, never of the secret's value,
    so `get_secret_value` must not appear here either.
    """
    for variant in variants:
        block = variant.block
        for token in ("db_user", "db_password", "username", "password", "get_secret_value", "SecretString"):
            assert token not in block, (
                f"{variant.label}: the sample block references {token!r}. LGR-819980f-1 permits "
                f"only a Secrets Manager ARN reference."
            )
        # What it DOES read: the ARN string, split for its segments.
        assert "args['secret_arn'].split(\":\")" in block, variant.label


# ---------------------------------------------------------------------------
# Clause 7 — fence-token counts unchanged, per read_mode
# ---------------------------------------------------------------------------


def test_cache_and_unpersist_counts_are_unchanged_over_the_full_matrix(
    variants: list[Variant],
) -> None:
    """EXTENDS `test_load_counts_render.py:598-620` along two axes it does not vary.

    That module already pins these counts per read_mode — and it is the right
    place for the read_mode split, which it explains at length (over-broadening
    the fence to "no unpersist anywhere" would be a FALSE fence, because the
    incremental branch legitimately caches per Req 7.6). What it does not vary
    is `partition_columns` and `target_secret_arn`, so this is the same fence
    over 120 configurations rather than 30. `04506234eb` is the history: a
    reviewer's `cache()` on the full-table path was reverted because the tests
    assert full-table mode emits neither.

    A note on that record's id. The tests and `LGR-9e76249-10`'s own evidence
    list cite it as `LGR-0450623-1`, described as backfilled. On this tree the
    ledger is 251 records, all template provenance and zero backfilled, and
    commit `04506234eb` carries `records: []` — so `LGR-0450623-1` does NOT
    resolve, and `record('LGR-0450623-1')` returns nothing. The commit body is
    real and says what the tests say it says; the ID is not a citable record.
    """
    for variant in variants:
        if variant.read_mode == "full_table":
            assert variant.rendered.count("df.cache()") == 0, variant.label
            assert variant.rendered.count("df.unpersist()") == 0, variant.label
        else:
            assert variant.rendered.count("df = df.cache()") == 1, variant.label
            assert variant.rendered.count("df.unpersist()") == 2, variant.label
        assert variant.rendered.count(".persist(") == 0, variant.label


def test_count_call_totals_are_unchanged_over_the_full_matrix(variants: list[Variant]) -> None:
    """EXTENDS `test_load_counts_render.py:443` along the same two axes.

    2 `.count()` sites, plus 1 for S3_TABLES whose write partial re-reads the
    target to reconcile. C1 adds no Spark action of its own, which is do-NOT
    7's second clause stated as a number.
    """
    for variant in variants:
        expected = 2 + (1 if variant.target_type == "S3_TABLES" else 0)
        actual = variant.rendered.count(".count()")
        assert actual == expected, (
            f"{variant.label}: expected {expected} `.count()` call sites, found {actual}. C1 must add none."
        )


def test_the_sample_block_contains_none_of_the_four_fence_tokens(
    variants: list[Variant],
) -> None:
    """The localised form, and the coverage the whole-script counts cannot give.

    A whole-script count stays constant if a future edit removes a `cache()`
    elsewhere and adds one here. Scoping to C1's own lines closes that.
    """
    for variant in variants:
        block = variant.block
        for token in ("df.cache()", "df.unpersist()", ".count()", ".persist("):
            assert token not in block, (
                f"{variant.label}: the sample block contains {token!r}. It must add no Spark "
                f"action and no caching of its own."
            )


# ---------------------------------------------------------------------------
# The comment-leak canary — the class of defect, not just the instance
# ---------------------------------------------------------------------------


# Tokens that exist ONLY in the template's Jinja comments. A Jinja comment
# never reaches the output, so any of these appearing in a rendered script
# means a comment terminated early and leaked its prose as template text —
# which is exactly what happened once during task 2.1, when the note quoted its
# own closing delimiter and leaked eight lines.
#
# Every token here was verified absent from all 120 renders. `R1.6` is
# DELIBERATELY NOT in this list — see the test below it.
JINJA_ONLY_TOKENS = [
    "LGR-",
    "BC10",
    "BC3",
    "BC4",
    "do-NOT",
    "mechanism (d)",
    "Triply determined",
    "WHY THIS SITE",
    "04506234eb",
    "R4.1",
    "R4.4",
    "template / ratified",
    "standing invariant",
    "_MAX_SAMPLE_BYTES",
    "SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT",
    "_SAMPLE_READERS",
    "glue_session_runner.py",
    "iam_roles.py",
]


@pytest.mark.parametrize("token", JINJA_ONLY_TOKENS)
def test_no_jinja_comment_prose_reaches_the_rendered_script(variants: list[Variant], token: str) -> None:
    """A generalised leak canary, not a fix for one instance.

    Task 2.1's Jinja comment is long and carries the whole rationale for the
    block — deliberately, because `LGR-b46a5bb-4` (template / ratified) records
    that substring matching an invariant forbids documenting it in emitted
    code, and this template's fences ARE substring assertions on rendered
    output. The first draft wrote that rationale as a PYTHON comment and broke
    five fences by naming them: `df.cache()` 0 -> 1 on full_table,
    `df.unpersist()` 2 -> 3 on incremental, `.count()` 2 -> 3.

    Moving it to Jinja fixed that but created a second failure mode with no
    existing fence: Jinja ends a comment at the FIRST occurrence of its closing
    delimiter, so prose that spells the delimiter leaks everything after it as
    literal template text. This asserts the whole class — no ledger id, no
    do-NOT reference, no cross-file line citation should ever appear in a
    generated Glue script — rather than the one token that failed.
    """
    for variant in variants:
        assert token not in variant.rendered, (
            f"{variant.label}: {token!r} reached the rendered script. It exists only in a Jinja "
            f"comment, so this means a comment terminated early and leaked its prose."
        )


def test_the_one_requirement_id_in_the_emitted_comment_is_pinned_at_one(
    variants: list[Variant],
) -> None:
    """`R1.6` appears ONCE, on purpose, and a blanket requirement-id ban is therefore unavailable.

    Task 2.3's brief proposed generalising the canary above to "no requirement
    id like `R4.1`". That is not available and the reason is worth writing
    down rather than discovering twice: C1's EMITTED Python comment says
    "Best-effort (R1.6): a telemetry failure must NOT fail a run whose read
    succeeded". That is a deliberate cross-reference in the generated script,
    not a leak — measured `R1.6` 0 -> 1 across all 120 renders, while `R4.1`
    and `R4.4` stay at 0 because they appear only in the Jinja comment.

    So the id is pinned at exactly one instead. A leak that duplicated the
    emitted comment would take it above one and fail here; a leak of any
    Jinja-only id fails the parametrised canary above. Between them the class
    is covered without forbidding the deliberate reference.
    """
    for variant in variants:
        assert variant.rendered.count("R1.6") == 1, (
            f"{variant.label}: expected exactly 1 occurrence of 'R1.6' — the emitted comment's "
            f"deliberate cross-reference — found {variant.rendered.count('R1.6')}."
        )


def test_the_emitted_comment_points_at_the_jinja_comment_for_its_rationale(
    variants: list[Variant],
) -> None:
    """The emitted comment must remain a pointer, not a copy.

    This is the assertion that keeps the fix from being undone by helpfulness:
    a future author who moves the rationale back into the emitted comment
    breaks the fences it names. The pointer's presence is what tells them where
    the rationale actually lives.
    """
    for variant in variants:
        assert "glue_job_jdbc_source.py.j2" in variant.block, (
            f"{variant.label}: the emitted comment no longer points at the Jinja comment that "
            f"holds the rationale. LGR-b46a5bb-4 is why the rationale cannot live here."
        )


# ---------------------------------------------------------------------------
# Every configuration still parses
# ---------------------------------------------------------------------------


def test_every_configuration_renders_valid_python(variants: list[Variant]) -> None:
    """C1's block is emitted at one indent inside `try:`; all 120 must parse.

    `test_glue_job_jdbc_source.py` and `test_load_counts_render.py` both parse
    this template already, over 30 and 25 variants respectively. This is the
    same check over the 120-configuration space, which is cheap and is the only
    place `partition_columns` x `target_secret_arn` is crossed with C1's block.
    """
    for variant in variants:
        try:
            ast.parse(variant.rendered)
        except SyntaxError as exc:  # pragma: no cover - diagnostic path
            pytest.fail(f"{variant.label}: rendered script is not valid Python: {exc}")


def test_the_sample_block_is_indented_inside_the_job_try(variants: list[Variant]) -> None:
    """The block's first emitted line keeps its four-space indent.

    Task 2.1 records this as found by rendering: a whitespace-stripping Jinja
    close (`-#}`) would eat the newline AND the indent, leaving a bare `#` at
    column 0 inside `try:`. Still legal Python, but the generated script is
    read in the Glue console.
    """
    for variant in variants:
        for line in variant.block.splitlines():
            if not line.strip():
                continue
            assert line.startswith("    "), (
                f"{variant.label}: the sample block emitted a line at column 0: {line!r}. "
                f"Check the Jinja comment's closing delimiter for a whitespace-stripping minus."
            )


# ---------------------------------------------------------------------------
# The retired clause — recorded rather than invented
# ---------------------------------------------------------------------------


def test_this_template_carries_no_rendered_line_fence() -> None:
    """THE 260-LINE ASSERTION HAS NEVER COVERED THIS TEMPLATE, AND NONE IS ADDED HERE.

    Task 2.3's second clause said "re-run the 260-line assertion using 1.3's
    measurement". There is no such assertion to re-run for this template, and
    this test is the recording of that so the next reader does not re-derive
    the confusion.

    THE FACTS, measured on this tree.
    `tests/unit/lambdas/create_code/test_code_assembly.py:138` is the ONLY
    rendered-line-budget assertion in the suite. Its `TEMPLATES` map
    (`:31-63`) parametrises FOUR templates; the repo has five, and this one is
    the omission. `LGR-9e76249-10`'s "jdbc 191" is `glue_job_jdbc.py.j2` — the
    JDBC **target** template, a different file.

    What this template actually renders, over the 120 configurations above:
    246-418 lines before C1, 332-504 after (C1's block is 86 rendered lines
    and constant across every configuration). So **108 of 120 configurations
    already failed a `< 260` assertion before p53 touched anything**, and all
    120 do now.

    WHY NO NEW BUDGET IS SET HERE. Extending a length fence to this template
    is a first-ever budget-setting decision, not a re-check.
    `LGR-9e76249-10` (template / ratified) requires that a deliberate hold
    encoded as a test assertion "be changed on purpose and stay legible as a
    reversal rather than reading as a regression", and it names its own scope
    as `test_code_assembly.py` and `test_checkpoint_templates.py` only.
    Inventing a number here would install a fence nobody ratified, under a
    record that does not govern it. Task 1.3 proposed a round 440 and flagged
    it explicitly as a proposal rather than a measurement; it is left as one.

    WHAT IS ASSERTED INSTEAD is the fact itself: the budgeted map does not name
    this template. If a future task adds it, this test fails and the author is
    pointed at the decision they are making rather than at a number they can
    adjust. Scanned as text so this module does not import from a test module
    another task may be editing — the same idiom
    `test_load_counts_render.py::test_jdbc_source_is_covered_by_no_golden_snapshot_test`
    uses, for the same reason.
    """
    budget_module = os.path.join(
        os.path.dirname(__file__),
        "..",
        "lambdas",
        "create_code",
        "test_code_assembly.py",
    )
    assert os.path.exists(budget_module), f"expected the budget module at {budget_module}"
    with open(budget_module) as handle:
        source = handle.read()

    # The assertion is still there, and still says 260 — this test is about
    # scope, not about retiring the budget for the four templates it covers.
    assert "limit: 260" in source, (
        "test_code_assembly.py no longer carries the 260-line budget. If it moved on purpose, "
        "amend this test in place with the reason, per LGR-9e76249-10's own convention."
    )
    assert TEMPLATE_NAME not in source, (
        f"test_code_assembly.py now parametrises {TEMPLATE_NAME}. Its 260-line assertion has "
        f"NEVER covered this template, and this template renders 332-504 lines across the 120 "
        f"configurations in this module — so adding it there fails all 120. Setting a length "
        f"fence on this template is a new budget-setting decision needing its own ledger record "
        f"(LGR-9e76249-10 scopes itself to test_code_assembly.py and test_checkpoint_templates.py), "
        f"not a re-check of an existing one."
    )
