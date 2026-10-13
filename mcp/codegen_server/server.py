#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgenticDataIngestion Codegen MCP Server for AgentCore

Focused server profile containing only code-generation-related tools:
- Glue ETL job CRUD (create, update, get, start, run logs)
- Glue Data Catalog read operations (databases, tables, catalog)
- Glue Interactive Sessions (create session, run statement)

This is one of 3 focused servers split from the monolithic data_processing server
to reduce prompt token usage and improve tool selection accuracy.

Tools (~10):
  - manage_aws_glue_jobs (GlueEtlJobsHandler)
  - manage_aws_glue_sessions, manage_aws_glue_statements (GlueInteractiveSessionsHandler)
  - manage_aws_glue_databases, manage_aws_glue_tables, manage_aws_glue_catalog (GlueDataCatalogHandler subset)
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
    """Register the codegen-focused handlers on *mcp*.

    Flags default to False (P28 R16.1). The deployed configuration keeps both
    False: consumers (create_code, test_code, create_glue_pipeline agents)
    use this server for read-only environment analysis — listing jobs,
    databases, and tables to inform code generation — while Glue job
    creation/starts are performed by Lambda code through boto3 under the
    Lambda's own IAM role.
    """
    logger.info("Initializing Glue ETL Jobs Handler...")
    GlueEtlJobsHandler(
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

    logger.info("Initializing Glue Data Catalog Handler (read-focused)...")
    GlueDataCatalogHandler(
        mcp,
        allow_write=allow_write,
        allow_sensitive_data_access=allow_sensitive_data_access,
    )

    logger.info("All codegen handlers initialized successfully!")


def main():
    """Run the Codegen MCP server with code-generation-focused handlers."""
    logger.info("Starting AgenticDataIngestion Codegen MCP Server for AgentCore")
    logger.info(f"AWS region: {aws_region}")
    logger.info("Focused profile: Glue ETL jobs, data catalog read, interactive sessions")

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
