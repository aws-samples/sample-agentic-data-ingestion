#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Portions of this file (the handler imports and the handler registration) are
# adapted from awslabs/mcp, src/aws-dataprocessing-mcp-server/awslabs/
# aws_dataprocessing_mcp_server/server.py at commit 57d7d67b (release 0.1.16),
# Copyright Amazon.com, Inc. or its affiliates, licensed under the Apache
# License, Version 2.0. MODIFIED for AgentCore: AgentCore host and transport
# settings, and IAM-mutation tools withheld. See NOTICE and
# mcp/vendor/awslabs_aws_dataprocessing_mcp_server/PROVENANCE.md.
"""
Complete AWS Data Processing MCP Server for AgentCore
Uses the handler-based approach from the sample server with AgentCore compatibility
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
from awslabs.aws_dataprocessing_mcp_server.handlers.commons.common_resource_handler import (
    CommonResourceHandler,
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
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.crawler_handler import (
    CrawlerHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.data_catalog_handler import (
    GlueDataCatalogHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.glue_commons_handler import (
    GlueCommonsHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.glue_etl_handler import (
    GlueEtlJobsHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.interactive_sessions_handler import (
    GlueInteractiveSessionsHandler,
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
    """Register all monolithic-server handlers on *mcp*.

    Flags default to False (P28 R16.1). This legacy server is not wired to
    any Lambda (the focused codegen/infra/analytics profiles replaced it);
    it is retained for backward compatibility and runs read-only.
    """
    logger.info("Initializing Glue Data Catalog Handler...")
    GlueDataCatalogHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("Initializing Glue Interactive Sessions Handler...")
    GlueInteractiveSessionsHandler(
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

    logger.info("Initializing Glue ETL Jobs Handler...")
    GlueEtlJobsHandler(
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

    logger.info("Initializing Crawler Handler...")
    CrawlerHandler(
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

    # P28 R16.2: IAM-mutation tools (add_inline_policy,
    # create_data_processing_role) are dropped from the registry here too —
    # this legacy server carries the same user-influenced context as the
    # focused profiles if it is ever wired to an agent, and IAM changes are
    # a CDK/deploy-time concern project-wide. Read-only IAM tools remain.
    logger.info("Initializing Common Resource Handler (IAM-mutation tools dropped, P28 R16.2)...")
    CommonResourceHandler(mcp, allow_write=allow_write, register_iam_mutation_tools=False)

    logger.info("All handlers initialized successfully!")


def main():
    """Run the MCP server with all handlers for AgentCore."""
    logger.info("Starting Complete AWS Data Processing MCP Server for AgentCore")
    logger.info(f"AWS region: {aws_region}")
    logger.info("Using handler-based approach with all 30+ tools")

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
