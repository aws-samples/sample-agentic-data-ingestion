#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgenticDataIngestion Infrastructure MCP Server for AgentCore

Focused server profile containing only infrastructure-related tools:
- Glue Crawlers (create, start, get, classifiers, management)
- Glue Commons (usage profiles, security configs, encryption, resource policies)
- Glue Workflows and Triggers
- Glue Data Catalog write operations (connections, partitions)
- Common Resource operations (IAM roles, S3 buckets, policies)

This is one of 3 focused servers split from the monolithic data_processing server
to reduce prompt token usage and improve tool selection accuracy.

Tools (~12):
  - manage_aws_glue_crawlers, manage_aws_glue_classifiers, manage_aws_glue_crawler_management (CrawlerHandler)
  - manage_aws_glue_usage_profiles, manage_aws_glue_security_configurations,
    manage_aws_glue_encryption, manage_aws_glue_resource_policies (GlueCommonsHandler)
  - manage_aws_glue_workflows, manage_aws_glue_triggers (GlueWorkflowAndTriggerHandler)
  - manage_aws_glue_connections, manage_aws_glue_partitions (GlueDataCatalogHandler subset)
  - get_policies_for_role, get_roles_for_service (read-only IAM),
    list_s3_buckets, upload_to_s3,
    analyze_s3_usage_for_data_processing (CommonResourceHandler)

P28 R16.2: the IAM-mutation tools (add_inline_policy,
create_data_processing_role) are NOT registered on this server. Agents that
reach this server process user-supplied data (CSV headers), so IAM changes
remain a CDK/deploy-time concern and are unreachable through MCP.
"""

import os
import sys
from pathlib import Path

# The vendored awslabs fork lives in ONE repo copy under
# mcp/vendor/awslabs_aws_dataprocessing_mcp_server/ (P28 R19.7 — provenance
# documented in PROVENANCE.md there). The container image COPYies that tree
# next to this file, so prefer a sibling awslabs/ (container layout) and fall
# back to the repo vendor directory (local dev / tests).
current_dir = Path(__file__).parent
_vendor_dir = current_dir.parent / "vendor" / "awslabs_aws_dataprocessing_mcp_server"
sys.path.insert(0, str(current_dir if (current_dir / "awslabs").is_dir() else _vendor_dir))

import logging

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

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get AWS region from environment or default to us-east-1
aws_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def _env_flag(name: str) -> bool:
    """Parse a boolean environment flag with a least-privilege default.

    Returns True only when the variable is explicitly set to the string
    "true" (case-insensitive). Unset, empty, or any other value — including
    "1" and "yes" — yields False (P28 R16.1: write/sensitive access must be
    an explicit opt-in, never a default).
    """
    return os.environ.get(name, "false").strip().lower() == "true"


def create_server():
    """Create and configure the MCP server instance for AgentCore."""
    return FastMCP(
        host="0.0.0.0",
        port=8000,
        stateless_http=True,  # Required for AgentCore
    )


def register_handlers(mcp, allow_write: bool = False, allow_sensitive_data_access: bool = False) -> None:
    """Register the infrastructure-focused handlers on *mcp*.

    Flags default to False (P28 R16.1). This server is not currently wired
    to any Lambda (create_infrastructure provisions via boto3 directly), so
    the deployed configuration keeps both flags False.

    P28 R16.2: CommonResourceHandler is registered WITHOUT its IAM-mutation
    tools (add_inline_policy, create_data_processing_role). IAM changes are
    a CDK/deploy-time concern and must not be reachable by agents that
    process user-supplied data. Read-only IAM tools (get_policies_for_role,
    get_roles_for_service) remain available for role inspection.
    """
    logger.info("Initializing Crawler Handler...")
    CrawlerHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Glue Commons Handler...")
    GlueCommonsHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Glue Workflow and Trigger Handler...")
    GlueWorkflowAndTriggerHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Glue Data Catalog Handler (connections/partitions focus)...")
    GlueDataCatalogHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Common Resource Handler (IAM-mutation tools dropped, P28 R16.2)...")
    CommonResourceHandler(mcp, allow_write=allow_write, register_iam_mutation_tools=False)

    logger.info("All infrastructure handlers initialized successfully!")


def main():
    """Run the Infrastructure MCP server with infra-focused handlers."""
    logger.info("Starting AgenticDataIngestion Infrastructure MCP Server for AgentCore")
    logger.info(f"AWS region: {aws_region}")
    logger.info("Focused profile: crawlers, Glue commons, workflows, catalog write, read-only IAM, S3")

    # Create the MCP server instance
    mcp = create_server()

    # P28 R16.1: write/sensitive access are env-gated and default to False.
    # Enable per-server via MCP_ALLOW_WRITE / MCP_ALLOW_SENSITIVE_DATA_ACCESS
    # ("true") in the runtime's environment (deploy-cdk/lib/agentcore-stack.ts).
    allow_write = _env_flag("MCP_ALLOW_WRITE")
    allow_sensitive_data_access = _env_flag("MCP_ALLOW_SENSITIVE_DATA_ACCESS")
    logger.info(f"allow_write={allow_write}, allow_sensitive_data_access={allow_sensitive_data_access}")

    register_handlers(mcp, allow_write=allow_write, allow_sensitive_data_access=allow_sensitive_data_access)

    try:
        # Test AWS connectivity
        import boto3

        sts_client = boto3.client("sts", region_name=aws_region)
        account_id = sts_client.get_caller_identity()["Account"]
        logger.info(f"AWS Account ID: {account_id}")
    except Exception as e:
        logger.warning(f"Could not get AWS account info: {e}")

    # Run the FastMCP server with streamable-http transport for AgentCore
    logger.info("Starting FastMCP server with streamable-http transport...")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
