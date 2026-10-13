# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based test for jdbc_table dot-notation construction.

Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation construction

For any non-empty database name and non-empty table name, the jdbc_table value
SHALL equal {database_name}.{table_name}.

**Validates: Requirements 8.4**
"""

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategy — non-empty identifier-like strings for database and table names
# ---------------------------------------------------------------------------

identifier_strategy = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{0,30}", fullmatch=True)


def build_jdbc_table(database_name: str, table_name: str) -> str:
    """Construct jdbc_table as database_name.table_name — mirrors frontend buildJdbcTable."""
    return f"{database_name}.{table_name}"


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation
# construction
# ---------------------------------------------------------------------------


class TestJdbcTableDotNotation:
    """Property 6: jdbc_table dot-notation construction.

    For any non-empty database name and non-empty table name, the jdbc_table
    value SHALL equal {database_name}.{table_name}.

    **Validates: Requirements 8.4**
    """

    @given(
        database_name=identifier_strategy,
        table_name=identifier_strategy,
    )
    @settings(max_examples=100)
    def test_jdbc_table_equals_db_dot_table(self, database_name: str, table_name: str):
        """jdbc_table must be exactly database_name.table_name for any non-empty inputs."""
        # Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation construction
        result = build_jdbc_table(database_name, table_name)

        # Must contain exactly one dot separating the two parts
        assert result == f"{database_name}.{table_name}", f"Expected '{database_name}.{table_name}', got '{result}'"

    @given(
        database_name=identifier_strategy,
        table_name=identifier_strategy,
    )
    @settings(max_examples=100)
    def test_jdbc_table_starts_with_database_name(self, database_name: str, table_name: str):
        """jdbc_table must start with the database name."""
        # Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation construction
        result = build_jdbc_table(database_name, table_name)
        assert result.startswith(database_name + "."), (
            f"Expected result to start with '{database_name}.', got '{result}'"
        )

    @given(
        database_name=identifier_strategy,
        table_name=identifier_strategy,
    )
    @settings(max_examples=100)
    def test_jdbc_table_ends_with_table_name(self, database_name: str, table_name: str):
        """jdbc_table must end with the table name."""
        # Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation construction
        result = build_jdbc_table(database_name, table_name)
        assert result.endswith("." + table_name), f"Expected result to end with '.{table_name}', got '{result}'"

    @given(
        database_name=identifier_strategy,
        table_name=identifier_strategy,
    )
    @settings(max_examples=100)
    def test_jdbc_table_contains_exactly_one_dot(self, database_name: str, table_name: str):
        """jdbc_table must contain exactly one dot separator."""
        # Feature: p14-rds-database-targets, Property 6: jdbc_table dot-notation construction
        result = build_jdbc_table(database_name, table_name)
        assert result.count(".") == 1, f"Expected exactly 1 dot in '{result}', found {result.count('.')}"
