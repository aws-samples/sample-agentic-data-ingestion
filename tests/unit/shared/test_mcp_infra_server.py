# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the infrastructure MCP server profile (Task 2.11).

Validates that the infra server registers only the designated tools:
- CrawlerHandler tools
- GlueCommonsHandler tools
- GlueWorkflowAndTriggerHandler tools
- GlueDataCatalogHandler tools
- CommonResourceHandler tools
"""

import os
import sys

# Import the handler modules from the single vendored awslabs copy (P28 R19.7)
vendor_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "mcp", "vendor", "awslabs_aws_dataprocessing_mcp_server"
)
sys.path.insert(0, vendor_dir)


# Expected tools registered by the infra server's handlers
INFRA_EXPECTED_TOOLS = {
    # CrawlerHandler
    "manage_aws_glue_crawlers",
    "manage_aws_glue_classifiers",
    "manage_aws_glue_crawler_management",
    # GlueCommonsHandler
    "manage_aws_glue_usage_profiles",
    "manage_aws_glue_security_configurations",
    "manage_aws_glue_encryption",
    "manage_aws_glue_resource_policies",
    # GlueWorkflowAndTriggerHandler
    "manage_aws_glue_workflows",
    "manage_aws_glue_triggers",
    # GlueDataCatalogHandler (all 5 tools)
    "manage_aws_glue_databases",
    "manage_aws_glue_tables",
    "manage_aws_glue_connections",
    "manage_aws_glue_partitions",
    "manage_aws_glue_catalog",
    # CommonResourceHandler
    "add_inline_policy",
    "get_policies_for_role",
    "create_data_processing_role",
    "get_roles_for_service",
    "list_s3_buckets",
    "upload_to_s3",
    "analyze_s3_usage_for_data_processing",
}

# Tools that should NOT be in the infra server
EXCLUDED_TOOLS = {
    # Glue ETL (codegen server)
    "manage_aws_glue_jobs",
    # Interactive sessions (codegen server)
    "manage_aws_glue_sessions",
    "manage_aws_glue_statements",
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
    """Create a FastMCP instance, register infra handlers, return tool names."""
    from awslabs.aws_dataprocessing_mcp_server.handlers.commons.common_resource_handler import (
        CommonResourceHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.crawler_handler import (
        CrawlerHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.data_catalog_handler import (
        GlueDataCatalogHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.glue_commons_handler import (
        GlueCommonsHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.glue.worklows_handler import (
        GlueWorkflowAndTriggerHandler,
    )
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)

    CrawlerHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    GlueCommonsHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    GlueWorkflowAndTriggerHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    GlueDataCatalogHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    CommonResourceHandler(mcp, allow_write=True)

    tool_manager = mcp._tool_manager
    return set(tool_manager._tools.keys())


class TestInfraServerToolSet:
    """Verify the infra server exposes only its designated tools."""

    def test_infra_server_registers_expected_tools(self):
        """All expected infra tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in INFRA_EXPECTED_TOOLS:
            assert tool_name in registered, f"Expected tool '{tool_name}' not registered"

    def test_infra_server_excludes_non_infra_tools(self):
        """No codegen or analytics tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in EXCLUDED_TOOLS:
            assert tool_name not in registered, f"Tool '{tool_name}' should NOT be in infra server"

    def test_infra_server_tool_count(self):
        """Infra server should have the expected number of tools."""
        registered = _get_registered_tool_names()
        assert len(registered) == len(INFRA_EXPECTED_TOOLS), (
            f"Expected {len(INFRA_EXPECTED_TOOLS)} tools, got {len(registered)}: {registered}"
        )
