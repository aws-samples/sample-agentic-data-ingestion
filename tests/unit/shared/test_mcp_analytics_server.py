# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the analytics MCP server profile (Task 2.12).

Validates that the analytics server registers only the designated tools:
- AthenaQueryHandler tools
- AthenaDataCatalogHandler tools
- AthenaWorkGroupHandler tools
- EMREc2ClusterHandler tools
- EMREc2StepsHandler tools
- EMREc2InstanceHandler tools
"""

import os
import sys

# Import the handler modules from the single vendored awslabs copy (P28 R19.7)
vendor_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "mcp", "vendor", "awslabs_aws_dataprocessing_mcp_server"
)
sys.path.insert(0, vendor_dir)


# Expected tools registered by the analytics server's handlers
ANALYTICS_EXPECTED_TOOLS = {
    # AthenaQueryHandler
    "manage_aws_athena_query_executions",
    "manage_aws_athena_named_queries",
    # AthenaDataCatalogHandler
    "manage_aws_athena_data_catalogs",
    "manage_aws_athena_databases_and_tables",
    # AthenaWorkGroupHandler
    "manage_aws_athena_workgroups",
    # EMREc2ClusterHandler
    "manage_aws_emr_clusters",
    # EMREc2StepsHandler
    "manage_aws_emr_ec2_steps",
    # EMREc2InstanceHandler
    "manage_aws_emr_ec2_instances",
}

# Tools that should NOT be in the analytics server
EXCLUDED_TOOLS = {
    # Glue ETL (codegen server)
    "manage_aws_glue_jobs",
    # Interactive sessions (codegen server)
    "manage_aws_glue_sessions",
    "manage_aws_glue_statements",
    # Data catalog (codegen/infra server)
    "manage_aws_glue_databases",
    "manage_aws_glue_tables",
    "manage_aws_glue_connections",
    "manage_aws_glue_partitions",
    "manage_aws_glue_catalog",
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
}


def _get_registered_tool_names():
    """Create a FastMCP instance, register analytics handlers, return tool names."""
    from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_data_catalog_handler import (
        AthenaDataCatalogHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_query_handler import (
        AthenaQueryHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_workgroup_handler import (
        AthenaWorkGroupHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.emr.emr_ec2_cluster_handler import (
        EMREc2ClusterHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.emr.emr_ec2_instance_handler import (
        EMREc2InstanceHandler,
    )
    from awslabs.aws_dataprocessing_mcp_server.handlers.emr.emr_ec2_steps_handler import (
        EMREc2StepsHandler,
    )
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)

    AthenaQueryHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    AthenaDataCatalogHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    AthenaWorkGroupHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    EMREc2ClusterHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    EMREc2StepsHandler(mcp, allow_write=True, allow_sensitive_data_access=True)
    EMREc2InstanceHandler(mcp, allow_write=True, allow_sensitive_data_access=True)

    tool_manager = mcp._tool_manager
    return set(tool_manager._tools.keys())


class TestAnalyticsServerToolSet:
    """Verify the analytics server exposes only its designated tools."""

    def test_analytics_server_registers_expected_tools(self):
        """All expected analytics tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in ANALYTICS_EXPECTED_TOOLS:
            assert tool_name in registered, f"Expected tool '{tool_name}' not registered"

    def test_analytics_server_excludes_non_analytics_tools(self):
        """No codegen or infra tools should be registered."""
        registered = _get_registered_tool_names()
        for tool_name in EXCLUDED_TOOLS:
            assert tool_name not in registered, f"Tool '{tool_name}' should NOT be in analytics server"

    def test_analytics_server_tool_count(self):
        """Analytics server should have exactly 8 tools."""
        registered = _get_registered_tool_names()
        assert len(registered) == len(ANALYTICS_EXPECTED_TOOLS), (
            f"Expected {len(ANALYTICS_EXPECTED_TOOLS)} tools, got {len(registered)}: {registered}"
        )
