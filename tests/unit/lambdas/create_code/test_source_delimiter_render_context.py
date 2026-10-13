# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 R4.1 — ``generate_code`` renders the SOURCE's own Source_Delimiter.

A TSV upload is accepted today and then read as a single column: all four
Glue_Job_Templates called
``spark.read.option("header", True).option("inferSchema", False).csv(source_path)``
with no ``sep``, and Spark defaults ``sep`` to a comma. p55 W2 gives the
templates ``.option("sep", {{ source_delimiter | tojson }})`` and this module is
the create_code half of the fix: the value in that slot comes from
``delimiter_for_key`` over the source object's own key, not from a constant.

WHY THE ASSERTIONS ARE WHAT THEY ARE
------------------------------------
``code_assembly._get_pyspark_jinja_env`` renders with ``StrictUndefined``, so a
render path that omits ``source_delimiter`` raises ``UndefinedError`` rather
than emitting an empty option. ``generate_code`` holds the **only**
``template.render()`` call in this Lambda, and sets ``source_delimiter``
unconditionally beside ``source_format`` — so "no render path omits it" is
structural. :func:`test_every_template_renders_a_csv_source_without_raising`
is that claim's executable form, across all four templates.

The tab case asserts the two characters ``\\t`` and asserts a **literal** tab is
absent. ``| tojson`` rather than raw interpolation is the whole reason that
holds: a raw tab would land inside a Python string literal in generated code
that no test renders and no reader sees until a Glue run parses it.

``delimiter_for_key`` is total and answers a comma for anything it does not
recognise (R4.6), so the absent-URI and no-extension cases assert the
**pre-p55 behaviour is preserved exactly** rather than asserting a new one.

**Validates: Requirements 4.1**

No AWS client is constructed and no template is rendered through Bedrock: the
transform generator is a stub, matching how every other test in this directory
drives ``generate_code``.
"""

import os
import sys

import jinja2
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
for _path in (
    os.path.join(_REPO, "lambda", "agentic_data_ingestion_create_code"),
    os.path.join(_REPO, "shared"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# `code_assembly` imports no boto3 client and no strands — `LGR-1e135d2-2`
# (template / ratified) records that as a property of the module and
# `test_code_assembly_still_owns_no_aws_client` asserts it — so it is imported
# directly rather than through `lambda_function`'s stubbed module graph.
import code_assembly

#: The four templates whose csv branch carries the option, named rather than
#: globbed for the reason the sibling contract test gives: a glob that matched
#: nothing would make this file vacuous.
TARGET_CONFIGS = {
    "s3_tables": {
        "type": "s3_tables",
        "bucket_name": "my-bucket",
        "namespace_name": "my_ns",
        "table_name": "my_table",
        "warehouse_location": "s3://my-bucket/warehouse",
    },
    "glue_catalog": {"type": "glue_catalog", "database_name": "my_db", "table_name": "my_table"},
    "jdbc": {
        "type": "jdbc",
        "jdbc_url": "jdbc:postgresql://host:5432/db",
        "jdbc_table": "public.my_table",
        "jdbc_driver": "org.postgresql.Driver",
    },
    "s3_parquet": {"type": "s3_parquet", "output_path": "s3://output-bucket/data/"},
}


def _generate(target_key: str, source_object_uri: str | None) -> str:
    """``generate_code`` for one target type over one source URI.

    ``source_object_uri`` of ``None`` is the run whose ``schema_extract`` row is
    absent — the handler passes ``""`` there, and omitting the key entirely is
    the third shape (an older caller), so both reach
    ``pipeline_metadata.get(...) or ""``.
    """
    pipeline_metadata: dict = {
        "source_type": "S3_UPLOAD",
        "target_config": {**TARGET_CONFIGS[target_key], "source_format": "csv"},
        "source_schema": {},
        "target_schema": {},
    }
    if source_object_uri is not None:
        pipeline_metadata["source_object_uri"] = source_object_uri

    return code_assembly.generate_code(
        pipeline_metadata,
        {"field_mappings": []},
        generate_transform=lambda **_: "df_transformed = df",
        quarantine_bucket="adi-generated-code-123456789012-us-west-2",
    )


def _read_line(rendered: str) -> str:
    lines = [line.strip() for line in rendered.splitlines() if line.strip().startswith("df = spark.read")]
    assert len(lines) == 1, f"expected exactly one source read, found {len(lines)}: {lines}"
    return lines[0]


@pytest.mark.parametrize("target_key", sorted(TARGET_CONFIGS))
@pytest.mark.parametrize(
    ("source_object_uri", "expected_option"),
    [
        ("s3://bucket/pipeline-a/orders.csv", '.option("sep", ",")'),
        ("s3://bucket/pipeline-a/orders.tsv", '.option("sep", "\\t")'),
        # Compression-suffix stripping reaches this call site too: `.tsv.gz` is
        # still a TSV. `delimiter_for_key` strips the same suffixes
        # `detect_format` strips.
        ("s3://bucket/pipeline-a/orders.tsv.gz", '.option("sep", "\\t")'),
    ],
)
def test_the_rendered_read_carries_the_sources_own_delimiter(target_key, source_object_uri, expected_option):
    """R4.1 — one option per template, filled from the source object's extension."""
    rendered = _generate(target_key, source_object_uri)
    read_line = _read_line(rendered)

    assert expected_option in read_line, f"{target_key} / {source_object_uri} rendered {read_line!r}"
    assert "\t" not in read_line, (
        f"{target_key} / {source_object_uri} emitted a LITERAL tab inside the generated code: "
        f"{read_line!r}. The templates use `| tojson` so a tab reaches the job as the two characters "
        f"`\\t`; a raw tab here means the filter was dropped."
    )


@pytest.mark.parametrize("target_key", sorted(TARGET_CONFIGS))
@pytest.mark.parametrize(
    "source_object_uri",
    [
        pytest.param("", id="absent-schema-extract-row"),
        pytest.param(None, id="key-omitted-entirely"),
        pytest.param("jdbc://pipeline/my-pipeline", id="jdbc-sentinel"),
        pytest.param("s3://bucket/pipeline-a/", id="multi-file-prefix-no-extension"),
        pytest.param("s3://bucket/pipeline-a/data.parquet", id="non-delimited-extension"),
    ],
)
def test_an_unrecognised_source_still_renders_the_pre_p55_comma(target_key, source_object_uri):
    """R4.6 — the lookup is total, so nothing here is a new failure mode.

    Each of these rendered ``sep``-less (Spark's comma default) before p55 and
    renders an explicit comma now. Asserted so a later reader can see that the
    delimiter mechanism did not quietly change what a non-TSV source does.
    """
    read_line = _read_line(_generate(target_key, source_object_uri))

    assert '.option("sep", ",")' in read_line, f"{target_key} / {source_object_uri!r} rendered {read_line!r}"


@pytest.mark.parametrize("target_key", sorted(TARGET_CONFIGS))
def test_every_template_renders_a_csv_source_without_raising(target_key):
    """StrictUndefined, so this is the "no render path omits it" check.

    ``generate_code`` sets ``source_delimiter`` unconditionally, so the csv
    branch cannot reach an undefined. Were the assignment made conditional —
    on the format, on the target type, on the URI being present — this fails
    with ``UndefinedError`` naming ``source_delimiter`` rather than shipping a
    job that cannot be rendered.
    """
    try:
        rendered = _generate(target_key, "s3://bucket/pipeline-a/orders.tsv")
    except jinja2.UndefinedError as exc:  # pragma: no cover — the failure this pins
        pytest.fail(f"{target_key} render raised UndefinedError: {exc}")

    compile(rendered, f"<{target_key}>", "exec")
