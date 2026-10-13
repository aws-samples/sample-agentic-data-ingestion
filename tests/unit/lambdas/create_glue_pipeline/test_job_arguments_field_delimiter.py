# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``--field-delimiter`` carries the source's Source_Delimiter, not a comma.

p55 W2 / R4.5. ``build_glue_catalog_arguments`` hardcoded
``job_arguments["--field-delimiter"] = ","``, so a tab-separated upload was
described to the job as comma-separated. The value now comes from
``shared/format_delimiters.delimiter_for_key`` applied to ``source_path``, which
``artifacts.get_source_path`` resolves to the *original* uploaded object's URI
(``original_s3_uri``) rather than the converted JSON — so the extension read here
is the source's real one.

The lookup is total and defaults to a comma (R4.6), which makes this change a
no-op for every non-TSV source. That is the property that makes it safe, so it is
asserted here rather than assumed.
"""

import importlib.util
import os
import sys

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)
sys.path.insert(0, LAMBDA_DIR)

os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

#: A Glue Catalog target whose OUTPUT format is csv — the one branch that emits
#: ``--field-delimiter`` at all.
CSV_TARGET = {"type": "GLUE_CATALOG", "database": "my_db", "table": "my_table", "format": "csv"}


def _load(mod_name: str):
    """Load a sibling module of the create_glue_pipeline Lambda standalone."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(f"cgp_{mod_name}", mod_path, submodule_search_locations=[LAMBDA_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestFieldDelimiterFollowsTheSource:
    def test_tsv_source_gets_a_tab(self):
        ja = _load("job_arguments")
        args = ja.build_glue_catalog_arguments(CSV_TARGET, source_path="s3://bucket/uploads/people.tsv")
        assert args["--field-delimiter"] == "\t"

    def test_compressed_tsv_source_gets_a_tab(self):
        """``delimiter_for_key`` looks through the compression suffix."""
        ja = _load("job_arguments")
        args = ja.build_glue_catalog_arguments(CSV_TARGET, source_path="s3://bucket/uploads/people.tsv.gz")
        assert args["--field-delimiter"] == "\t"

    def test_csv_source_still_gets_a_comma(self):
        ja = _load("job_arguments")
        args = ja.build_glue_catalog_arguments(CSV_TARGET, source_path="s3://bucket/uploads/people.csv")
        assert args["--field-delimiter"] == ","

    def test_non_delimited_source_gets_the_default_comma(self):
        """R4.6 — the change is a no-op for every source without a delimiter."""
        ja = _load("job_arguments")
        for key in (
            "s3://bucket/uploads/people.parquet",
            "s3://bucket/uploads/people.json",
            "s3://bucket/uploads/people.avro",
            "s3://bucket/uploads/people.orc",
            "s3://bucket/uploads/people",
            "",
        ):
            args = ja.build_glue_catalog_arguments(CSV_TARGET, source_path=key)
            assert args["--field-delimiter"] == ",", key

    def test_absent_source_path_gets_the_default_comma(self):
        """The parameter's own default — no source_path resolved, no exception."""
        ja = _load("job_arguments")
        args = ja.build_glue_catalog_arguments(CSV_TARGET)
        assert args["--field-delimiter"] == ","

    def test_non_csv_output_format_emits_no_field_delimiter(self):
        """The argument is gated on the TARGET's output format, and stays gated.

        A tab-separated *source* written to a parquet Glue Catalog table must not
        acquire a ``--field-delimiter``: this task changed the value in that
        branch, not the branch condition.
        """
        ja = _load("job_arguments")
        for output_format in ("parquet", "orc", "json"):
            target = dict(CSV_TARGET, format=output_format)
            args = ja.build_glue_catalog_arguments(target, source_path="s3://bucket/uploads/people.tsv")
            assert "--field-delimiter" not in args, output_format
