# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the codegen MCP server profile (Task 2.10).

Validates that the codegen server registers only the designated tools:
- GlueEtlJobsHandler tools
- GlueInteractiveSessionsHandler tools
- GlueDataCatalogHandler tools (databases, tables, connections, partitions, catalog)
"""

import os
import sys

# Import the handler modules from the single vendored awslabs copy (P28 R19.7)
vendor_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "mcp", "vendor", "awslabs_aws_dataprocessing_mcp_server"
)
sys.path.insert(0, vendor_dir)


# Expected tools registered by the codegen server's handlers
CODEGEN_EXPECTED_TOOLS = {
    # GlueEtlJobsHandler
    "manage_aws_glue_jobs",
    # GlueInteractiveSessionsHandler
    "manage_aws_glue_sessions",
    "manage_aws_glue_statements",
    # GlueDataCatalogHandler (all 5 tools — the handler registers all of them)
    "manage_aws_glue_databases",
    "manage_aws_glue_tables",
    "manage_aws_glue_connections",
    "manage_aws_glue_partitions",
    "manage_aws_glue_catalog",
}

# Tools that should NOT be in the codegen server
EXCLUDED_TOOLS = {
    # Crawler tools (infra server)
    "manage_aws_glue_crawlers",
    "manage_aws_glue_classifiers",
    "manage_aws_glue_crawler_management",
    # Glue commons (infra server)
    "manage_aws_glue_usage_profiles",
    "manage_aws_glue_security_configurations",
    "manage_aws_glue_encryption",
    "manage_aws_glue_resource_policies",
    # Workflow tools (infra server)
    "manage_aws_glue_workflows",
    "manage_aws_glue_triggers",
    # Common resource tools (infra server)
    "add_inline_policy",
    "get_policies_for_role",
    "create_data_processing_role",
    "get_roles_for_service",
    "list_s3_buckets",
    "upload_to_s3",
    "analyze_s3_usage_for_data_processing",
    # Athena tools (analytics server)
    "manage_aws_athena_query_executions",
    "manage_aws_athena_named_queries",
    "manage_aws_athena_data_catalogs",
    "manage_aws_athena_databases_and_tables",
    "manage_aws_athena_workgroups",
    # EMR tools (analytics server)
    "manage_aws_emr_clusters",
    "manage_aws_emr_ec2_steps",
    "manage_aws_emr_ec2_instances",
}


def _get_registered_tool_names():
    """Create a FastMCP instance, register codegen handlers, return tool names."""
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.data_catalog_handler import (
        GlueDataCatalogHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.glue_etl_handler import (
        GlueEtlJobsHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.interactive_sessions_handler import (
        GlueInteractiveSessionsHandler,
    )
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)

    GlueEtlJobsHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    GlueInteractiveSessionsHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    GlueDataCatalogHandler(mcp, allow_write=True, allow_sensitive_data_access=True)

    # FastMCP stores tools in _tool_manager._tools dict
    tool_manager = mcp._tool_manager
    return set(tool_manager._tools.keys())


class TestCodegenServerToolSet:
    """Verify the codegen server exposes only its designated tools."""

    def test_codegen_server_registers_expected_tools(self):
        """All expected codegen tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in CODEGEN_EXPECTED_TOOLS:
            assert tool_name in registered, f"Expected tool '{tool_name}' not registered"

    def test_codegen_server_excludes_non_codegen_tools(self):
        """No infra or analytics tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in EXCLUDED_TOOLS:
            assert tool_name not in registered, f"Tool '{tool_name}' should NOT be in codegen server"

    def test_codegen_server_tool_count(self):
        """Codegen server should have approximately 8 tools (3 handlers)."""
        registered = _get_registered_tool_names()
        assert len(registered) == len(CODEGEN_EXPECTED_TOOLS), (
            f"Expected {len(CODEGEN_EXPECTED_TOOLS)} tools, got {len(registered)}: {registered}"
        )
