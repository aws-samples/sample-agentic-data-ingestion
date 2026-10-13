# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R5.5 — the target-type label, and the legacy alias it must never report.

``test_schemas_listing_sources.py`` asserts R5.5 at the **response** level: no
`glue_catalog` row carries the hardcoded ``"Aurora Postgres"``, and no row carries
``RDS_DATABASE``. Neither of those can reach the normalisation itself, because no
fixture in the listing produces an ``rds_database`` token in the first place — the
assertion passes for the trivial reason that nothing emits it, which is exactly
what it would do if the normalisation were absent.

So the three tests here go at ``_target_type_label`` directly. ``RDS_DATABASE`` is
a legacy alias consolidated into ``jdbc`` (P17 task 20.4) and normalised on read;
`requirements.md` §"Do NOT" states it plainly — do not treat ``RDS_DATABASE`` as a
distinct target type. The frontend has already made the same consolidation and
pins it from its own side (``WizardStepReview.test.tsx:254-263``: the `jdbc` label
is present and the `rds_database` row is gone).

No AWS call, no fixture, no mock: these are pure functions over a dict.

**Validates: Requirement 5.5**
"""

import os
import sys

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.schemas
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers.schemas import _glue_target_type, _target_type_label


def test_the_legacy_rds_database_alias_normalises_to_the_jdbc_label():
    """`RDS_DATABASE` in any casing resolves to the one `jdbc` label.

    The alias is consolidated into `jdbc` and normalised on read, so all three
    spellings must land on the same value and none of them may come back as
    `RDS_DATABASE`.

    **Validates: Requirement 5.5**
    """
    jdbc_label = _target_type_label("jdbc")
    assert jdbc_label != "RDS_DATABASE"
    for alias in ("RDS_DATABASE", "rds_database", "  RDS_Database  "):
        assert _target_type_label(alias) == jdbc_label, (
            f"{alias!r} must normalise to the jdbc label, got {_target_type_label(alias)!r}"
        )


def test_each_canonical_target_type_gets_a_label_and_none_of_them_is_aurora_postgres():
    """The four canonical target types plus `uploaded` each resolve to a label.

    Asserted against the frontend's own vocabulary
    (`dashboard-react/src/pages/wizard/WizardStepReview.tsx:18`) so the listing
    and the wizard name the same thing the same way, and asserted negatively
    against the string R5.5 exists to remove.

    **Validates: Requirement 5.5**
    """
    labels = {
        token: _target_type_label(token) for token in ("s3_tables", "glue_catalog", "jdbc", "s3_parquet", "uploaded")
    }
    assert labels["s3_tables"] == "S3 Table Bucket"
    assert labels["glue_catalog"] == "Glue Catalog"
    assert labels["s3_parquet"] == "S3 Parquet"
    assert "JDBC" in labels["jdbc"]
    assert labels["uploaded"] == "Target Schema"
    assert "Aurora Postgres" not in labels.values(), (
        f"R5.5: no target type may resolve to the hardcoded label — {labels}"
    )
    assert "RDS_DATABASE" not in labels.values()


def test_a_glue_table_is_jdbc_only_when_it_names_a_glue_connection():
    """`_glue_target_type` reads the table entry it already has, and degrades safely.

    A JDBC-crawled catalog table names the connection it was crawled through
    (`handlers/jdbc_resources.py:379`), and that is the one Glue row for which a
    database engine really is the target. Everything else — including an S3 Tables
    Iceberg mirror, which is what carried `"Aurora Postgres"` most misleadingly —
    is a Glue Catalog table and says so.

    **Validates: Requirement 5.5**
    """
    iceberg_mirror = {
        "Name": "testingbucketns_testingbuckettn",
        "Parameters": {"table_type": "ICEBERG"},
        "StorageDescriptor": {"Columns": []},
    }
    assert _glue_target_type(iceberg_mirror) == "glue_catalog"
    assert _target_type_label(_glue_target_type(iceberg_mirror)) != "Aurora Postgres"

    # A table entry with no Parameters at all must not raise — `get_tables`
    # omits keys rather than returning empty ones.
    assert _glue_target_type({"Name": "churn"}) == "glue_catalog"
    assert _glue_target_type({"Name": "churn", "Parameters": None, "StorageDescriptor": None}) == "glue_catalog"

    # An empty connection name is not a connection.
    assert _glue_target_type({"Parameters": {"connectionName": ""}}) == "glue_catalog"

    for parameters in ({"connectionName": "adi-jdbc-src"}, {"ConnectionName": "adi-jdbc-src"}):
        assert _glue_target_type({"Parameters": parameters}) == "jdbc"
    assert _glue_target_type({"StorageDescriptor": {"Parameters": {"connectionName": "adi-jdbc-src"}}}) == "jdbc"
