#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Portions of this file (the Athena and EMR handler imports and registration)
# derive, through mcp/data_processing/server.py, from awslabs/mcp,
# src/aws-dataprocessing-mcp-server/awslabs/aws_dataprocessing_mcp_server/
# server.py at commit 57d7d67b (release 0.1.16), Copyright Amazon.com, Inc. or
# its affiliates, licensed under the Apache License, Version 2.0. MODIFIED: cut
# down to the analytics tools and adapted for AgentCore. See NOTICE and
# mcp/vendor/awslabs_aws_dataprocessing_mcp_server/PROVENANCE.md.
"""
AgenticDataIngestion Analytics MCP Server for AgentCore

Focused server profile containing only analytics and query-related tools:
- Athena query execution and results
- Athena data catalog and workgroup management
- EMR EC2 cluster, steps, and instance management

This is one of 3 focused servers split from the monolithic data_processing server
to reduce prompt token usage and improve tool selection accuracy.

Note: The design doc mentions ~15 tools including CloudWatch metrics and cost
estimation, but those handlers don't exist in the current codebase. This server
registers the 8 available Athena + EMR tools. CloudWatch/cost tools can be added
when those handlers are implemented.

Tools (~8):
  - manage_aws_athena_query_executions, manage_aws_athena_named_queries (AthenaQueryHandler)
  - manage_aws_athena_data_catalogs, manage_aws_athena_databases_and_tables (AthenaDataCatalogHandler)
  - manage_aws_athena_workgroups (AthenaWorkGroupHandler)
  - manage_aws_emr_clusters (EMREc2ClusterHandler)
  - manage_aws_emr_ec2_steps (EMREc2StepsHandler)
  - manage_aws_emr_ec2_instances (EMREc2InstanceHandler)
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
    """Register the analytics-focused handlers on *mcp*.

    Flags default to False (P28 R16.1). The deployed configuration keeps both
    False: the only consumer (create_glue_pipeline's worker-recommendation
    agent) issues read-only analysis calls — SELECT Athena queries and
    describe/list operations — while every actual AWS mutation is performed
    by Lambda code through boto3 under the Lambda's own IAM role.
    """
    logger.info("Initializing Athena Query Handler...")
    AthenaQueryHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Athena Data Catalog Handler...")
    AthenaDataCatalogHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Athena WorkGroup Handler...")
    AthenaWorkGroupHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing EMR EC2 Cluster Handler...")
    EMREc2ClusterHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing EMR EC2 Steps Handler...")
    EMREc2StepsHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing EMR EC2 Instance Handler...")
    EMREc2InstanceHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("All analytics handlers initialized successfully!")


def main():
    """Run the Analytics MCP server with analytics-focused handlers."""
    logger.info("Starting AgenticDataIngestion Analytics MCP Server for AgentCore")
    logger.info(f"AWS region: {aws_region}")
    logger.info("Focused profile: Athena queries, EMR clusters/steps/instances")

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
