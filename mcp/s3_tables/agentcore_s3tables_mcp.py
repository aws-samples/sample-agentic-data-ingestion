#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore-Native AWS S3 Tables MCP Server
Refactored to use handler class pattern for proper schema extraction

This server provides AWS S3 Tables tools including:
- S3 Table Bucket management
- Apache Iceberg table operations
- Table metadata and schema management
- S3 Tables optimization recommendations

Built specifically for AgentCore Runtime with stateless HTTP transport.
"""

import logging
import os
from typing import Annotated, Any

import boto3
from botocore.exceptions import ClientError
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

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


class S3TablesHandler:
    """Handler for AWS S3 Tables operations using FastMCP."""

    def __init__(self, mcp, allow_write: bool = False, allow_sensitive_data_access: bool = False):
        """Initialize the S3 Tables handler.

        Flags default to False (P28 R16.1). Write tools (create/rename/delete
        tables, namespaces, buckets, and table policies) are registration-gated
        on ``allow_write`` — with the default they never appear in the tool
        list, so a prompt-injected agent cannot even see them. Read tools
        (list/get/metadata/connectivity) and pure-computation tools
        (recommend/estimate) are always registered.

        ``allow_sensitive_data_access`` is accepted for parity with the
        awslabs dataprocessing handlers; no current tool in this server reads
        customer data rows, so the flag has no effect today.

        Args:
            mcp: The MCP server instance
            allow_write: Whether to register mutation tools (default: False)
            allow_sensitive_data_access: Whether to allow access to sensitive data (default: False)
        """
        self.mcp = mcp
        self.allow_write = allow_write
        self.allow_sensitive_data_access = allow_sensitive_data_access
        self.aws_region = aws_region

        # Initialize AWS clients
        try:
            self.s3tables_client = boto3.client("s3tables", region_name=self.aws_region)
            self.s3_client = boto3.client("s3", region_name=self.aws_region)
            self.glue_client = boto3.client("glue", region_name=self.aws_region)
            logger.info(f"AWS clients initialized successfully in region: {self.aws_region}")
        except Exception as e:
            logger.warning(f"Failed to initialize AWS clients: {e}")
            self.s3tables_client = self.s3_client = self.glue_client = None

        # Register tools using method-based registration (not decorators)
        # This ensures proper schema extraction from Annotated type hints.
        # Read-only and pure-computation tools — always available:
        self.mcp.tool(name="test_s3tables_connection")(self.test_s3tables_connection)
        self.mcp.tool(name="agentcore_list_s3_table_buckets")(self.list_s3_table_buckets)
        self.mcp.tool(name="agentcore_get_s3_table_bucket")(self.get_s3_table_bucket)
        self.mcp.tool(name="agentcore_list_s3_table_namespaces")(self.list_s3_table_namespaces)
        self.mcp.tool(name="agentcore_list_s3_tables")(self.list_s3_tables)
        self.mcp.tool(name="agentcore_get_s3_table")(self.get_s3_table)
        self.mcp.tool(name="agentcore_get_s3_table_metadata_location")(self.get_s3_table_metadata_location)
        self.mcp.tool(name="agentcore_get_s3_table_policy")(self.get_s3_table_policy)
        self.mcp.tool(name="agentcore_recommend_iceberg_config")(self.recommend_iceberg_config)
        self.mcp.tool(name="agentcore_estimate_s3_tables_cost")(self.estimate_s3_tables_cost)
        tool_count = 10

        # Mutation tools — registration-gated on allow_write (P28 R16.1):
        if self.allow_write:
            self.mcp.tool(name="agentcore_create_s3_table_bucket")(self.create_s3_table_bucket)
            self.mcp.tool(name="agentcore_create_s3_table_namespace")(self.create_s3_table_namespace)
            self.mcp.tool(name="agentcore_create_s3_table")(self.create_s3_table)
            self.mcp.tool(name="agentcore_rename_s3_table")(self.rename_s3_table)
            self.mcp.tool(name="agentcore_delete_s3_table")(self.delete_s3_table)
            self.mcp.tool(name="agentcore_put_s3_table_policy")(self.put_s3_table_policy)
            self.mcp.tool(name="agentcore_delete_s3_table_policy")(self.delete_s3_table_policy)
            tool_count += 7

        logger.info(
            f"S3 Tables handler initialized with {tool_count} tools "
            f"(allow_write={self.allow_write}, allow_sensitive_data_access={self.allow_sensitive_data_access})"
        )

    def get_aws_client(self, service_name: str):
        """Get AWS client with error handling"""
        try:
            return boto3.client(service_name, region_name=self.aws_region)
        except Exception as e:
            logger.error(f"Failed to create {service_name} client: {e}")
            return None

    # ============================================================================
    # CONNECTIVITY AND HEALTH CHECK TOOLS
    # ============================================================================

    async def test_s3tables_connection(self, ctx: Context) -> dict[str, Any]:
        """Test S3 Tables service connectivity and permissions"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        try:
            # Test basic connectivity by listing table buckets
            response = client.list_table_buckets(maxBuckets=1)

            return {
                "status": "connected",
                "service": "s3tables",
                "region": self.aws_region,
                "bucket_count": len(response.get("tableBuckets", [])),
                "success": True,
                "message": "S3 Tables service is accessible",
            }
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ["AccessDenied", "UnauthorizedOperation"]:
                return {
                    "status": "permission_denied",
                    "service": "s3tables",
                    "region": self.aws_region,
                    "error": f"Permission denied: {error_code}",
                    "success": False,
                }
            else:
                return {
                    "status": "error",
                    "service": "s3tables",
                    "region": self.aws_region,
                    "error": f"AWS Error: {error_code}",
                    "success": False,
                }

    # ============================================================================
    # S3 TABLES BUCKET MANAGEMENT TOOLS
    # ============================================================================

    async def create_s3_table_bucket(
        self,
        ctx: Context,
        bucket_name: Annotated[str, Field(description="Name of the S3 Table Bucket to create")],
        location_constraint: Annotated[
            str | None, Field(description="AWS region for the bucket (optional, defaults to current region)")
        ] = None,
    ) -> dict[str, Any]:
        """Create a new S3 Table Bucket for Apache Iceberg tables"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        params = {"name": bucket_name}

        # IMPORTANT: Do NOT include locationConstraint parameter at all
        # S3 Tables API will use the client's region automatically
        # Including locationConstraint causes "Invalid location constraint" error

        try:
            response = client.create_table_bucket(**params)

            return {
                "table_bucket_arn": response.get("arn"),
                "bucket_name": bucket_name,
                "location": self.aws_region,
                "success": True,
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "BucketAlreadyExists":
                # Bucket exists, get its details
                try:
                    sts_client = boto3.client("sts")
                    account_id = sts_client.get_caller_identity()["Account"]
                    bucket_info = client.get_table_bucket(
                        tableBucketARN=f"arn:aws:s3tables:{self.aws_region}:{account_id}:bucket/{bucket_name}"
                    )
                    return {
                        "table_bucket_arn": bucket_info.get("arn"),
                        "bucket_name": bucket_name,
                        "location": self.aws_region,
                        "already_exists": True,
                        "success": True,
                    }
                except ClientError as inner_exc:
                    logger.warning(
                        "get_table_bucket failed while resolving existing bucket %s: %s",
                        bucket_name,
                        inner_exc,
                    )
                    # fall through to re-raise the original create error below
            raise

    async def list_s3_table_buckets(
        self,
        ctx: Context,
        max_results: Annotated[int, Field(description="Maximum number of buckets to return (default: 100)")] = 100,
        next_token: Annotated[str | None, Field(description="Continuation token for pagination")] = None,
    ) -> dict[str, Any]:
        """List all S3 Table Buckets in the account"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        params = {"maxBuckets": max_results}
        if next_token:
            params["continuationToken"] = next_token

        response = client.list_table_buckets(**params)

        buckets = []
        for bucket in response.get("tableBuckets", []):
            buckets.append(
                {
                    "arn": bucket.get("arn"),
                    "name": bucket.get("name"),
                    "owner_account_id": bucket.get("ownerAccountId"),
                    "created_at": bucket.get("createdAt", "").isoformat() if bucket.get("createdAt") else None,
                }
            )

        return {
            "table_buckets": buckets,
            "count": len(buckets),
            "next_token": response.get("continuationToken"),
            "success": True,
        }

    async def get_s3_table_bucket(
        self, ctx: Context, table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")]
    ) -> dict[str, Any]:
        """Get detailed information about a specific S3 Table Bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        response = client.get_table_bucket(tableBucketARN=table_bucket_arn)

        return {
            "table_bucket": {
                "arn": response.get("arn"),
                "name": response.get("name"),
                "owner_account_id": response.get("ownerAccountId"),
                "created_at": response.get("createdAt", "").isoformat() if response.get("createdAt") else None,
            },
            "success": True,
        }

    # ============================================================================
    # S3 TABLES NAMESPACE MANAGEMENT TOOLS
    # ============================================================================

    async def create_s3_table_namespace(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")],
        namespace: Annotated[
            list[str], Field(description="Namespace path as a list of strings (e.g., ['org', 'project'])")
        ],
    ) -> dict[str, Any]:
        """Create a namespace in an S3 Table Bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # Ensure namespace is a list (handle string input from MCP layer)
        if isinstance(namespace, str):
            if namespace.startswith("["):
                import json

                namespace = json.loads(namespace)
            else:
                namespace = [namespace]

        response = client.create_namespace(tableBucketARN=table_bucket_arn, namespace=namespace)

        return {
            "namespace": response.get("namespace"),
            "table_bucket_arn": response.get("tableBucketARN"),
            "success": True,
        }

    async def list_s3_table_namespaces(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")],
        max_results: Annotated[int, Field(description="Maximum number of namespaces to return (default: 100)")] = 100,
        next_token: Annotated[str | None, Field(description="Continuation token for pagination")] = None,
        prefix: Annotated[str | None, Field(description="Filter namespaces by prefix")] = None,
    ) -> dict[str, Any]:
        """List namespaces in an S3 Table Bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        params = {"tableBucketARN": table_bucket_arn, "maxNamespaces": max_results}
        if next_token:
            params["continuationToken"] = next_token
        if prefix:
            params["prefix"] = prefix

        response = client.list_namespaces(**params)

        namespaces = []
        for ns in response.get("namespaces", []):
            namespaces.append(
                {
                    "namespace": ns.get("namespace"),
                    "created_at": ns.get("createdAt", "").isoformat() if ns.get("createdAt") else None,
                    "created_by": ns.get("createdBy"),
                    "owner_account_id": ns.get("ownerAccountId"),
                }
            )

        return {
            "namespaces": namespaces,
            "count": len(namespaces),
            "table_bucket_arn": table_bucket_arn,
            "next_token": response.get("continuationToken"),
            "success": True,
        }

    # ============================================================================
    # S3 TABLES TABLE MANAGEMENT TOOLS
    # ============================================================================

    async def create_s3_table(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")],
        namespace: Annotated[list[str], Field(description="Namespace path as a list of strings")],
        name: Annotated[str, Field(description="Name of the table to create")],
        format: Annotated[str, Field(description="Table format (default: ICEBERG)")] = "ICEBERG",
    ) -> dict[str, Any]:
        """Create a new table in an S3 Table Bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # Ensure namespace is a list (handle string input from MCP layer)
        if isinstance(namespace, str):
            # If it's a JSON string, parse it
            if namespace.startswith("["):
                import json

                namespace = json.loads(namespace)
            else:
                # Single string, wrap in list
                namespace = [namespace]

        response = client.create_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=name, format=format)

        return {
            "table_arn": response.get("tableARN"),
            "version_token": response.get("versionToken"),
            "table_bucket_arn": table_bucket_arn,
            "namespace": namespace,
            "name": name,
            "format": format,
            "success": True,
        }

    async def list_s3_tables(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")],
        namespace: Annotated[list[str] | None, Field(description="Namespace path to list tables from")] = None,
        max_results: Annotated[int, Field(description="Maximum number of tables to return (default: 100)")] = 100,
        next_token: Annotated[str | None, Field(description="Continuation token for pagination")] = None,
        prefix: Annotated[str | None, Field(description="Filter tables by name prefix")] = None,
    ) -> dict[str, Any]:
        """List tables in an S3 Table Bucket namespace"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        params = {"tableBucketARN": table_bucket_arn, "maxTables": max_results}
        if namespace:
            # Ensure namespace is a list (handle string input from MCP layer)
            if isinstance(namespace, str):
                if namespace.startswith("["):
                    import json

                    namespace = json.loads(namespace)
                else:
                    namespace = [namespace]
            params["namespace"] = namespace
        if next_token:
            params["continuationToken"] = next_token
        if prefix:
            params["prefix"] = prefix

        response = client.list_tables(**params)

        tables = []
        for table in response.get("tables", []):
            tables.append(
                {
                    "namespace": table.get("namespace"),
                    "name": table.get("name"),
                    "type": table.get("type"),
                    "table_arn": table.get("tableARN"),
                    "created_at": table.get("createdAt", "").isoformat() if table.get("createdAt") else None,
                    "created_by": table.get("createdBy"),
                    "modified_at": table.get("modifiedAt", "").isoformat() if table.get("modifiedAt") else None,
                    "modified_by": table.get("modifiedBy"),
                    "owner_account_id": table.get("ownerAccountId"),
                }
            )

        return {
            "tables": tables,
            "count": len(tables),
            "table_bucket_arn": table_bucket_arn,
            "namespace": namespace,
            "next_token": response.get("continuationToken"),
            "success": True,
        }

    async def get_s3_table(
        self, ctx: Context, table_arn: Annotated[str, Field(description="ARN of the S3 table")]
    ) -> dict[str, Any]:
        """Get detailed information about a specific S3 table"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # boto3 names this parameter ``tableArn`` (lowercase "rn"); passing
        # ``tableARN`` raises ParamValidationError. GetTable accepts either the
        # tableArn OR the (tableBucketARN, namespace, name) triple.
        response = client.get_table(tableArn=table_arn)

        return {
            "table": {
                "name": response.get("name"),
                "type": response.get("type"),
                "table_arn": response.get("tableARN"),
                "namespace": response.get("namespace"),
                "version_token": response.get("versionToken"),
                "metadata_location": response.get("metadataLocation"),
                "warehouse_location": response.get("warehouseLocation"),
                "created_at": response.get("createdAt", "").isoformat() if response.get("createdAt") else None,
                "created_by": response.get("createdBy"),
                "modified_at": response.get("modifiedAt", "").isoformat() if response.get("modifiedAt") else None,
                "modified_by": response.get("modifiedBy"),
                "owner_account_id": response.get("ownerAccountId"),
            },
            "success": True,
        }

    async def get_s3_table_metadata_location(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket")],
        namespace: Annotated[str, Field(description="Namespace name (single string, not array)")],
        name: Annotated[str, Field(description="Name of the table")],
    ) -> dict[str, Any]:
        """Get the metadata location for an S3 table

        Note: This API expects namespace as a STRING, not a list.
        This is different from create_table which expects a list.
        """
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # For get_table_metadata_location, namespace should be a string
        # If it comes in as a list, extract the first element
        if isinstance(namespace, list):
            namespace = namespace[0] if namespace else ""

        response = client.get_table_metadata_location(tableBucketARN=table_bucket_arn, namespace=namespace, name=name)

        return {
            "table_bucket_arn": table_bucket_arn,
            "namespace": namespace,
            "name": name,
            "metadata_location": response.get("metadataLocation"),
            "warehouse_location": response.get("warehouseLocation"),
            "version_token": response.get("versionToken"),
            "success": True,
        }

    # ============================================================================
    # S3 TABLES MAINTENANCE OPERATIONS
    # ============================================================================

    async def rename_s3_table(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket containing the table")],
        namespace: Annotated[str, Field(description="Current namespace of the table (single string)")],
        name: Annotated[str, Field(description="Current name of the table to rename")],
        new_name: Annotated[str, Field(description="New name for the table")],
        new_namespace: Annotated[
            str | None, Field(description="New namespace for the table (optional; omit to keep current)")
        ] = None,
        version_token: Annotated[
            str | None, Field(description="Version token for optimistic locking (optional)")
        ] = None,
    ) -> dict[str, Any]:
        """Rename (or move) an S3 table.

        The S3 Tables ``RenameTable`` API identifies the table by
        ``(tableBucketARN, namespace, name)`` — there is no ``tableArn``
        parameter. ``newName`` is required; ``newNamespaceName`` is optional and
        only needed when moving the table to a different namespace.
        """
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # These APIs expect namespace as a single string. Tolerate a list/JSON
        # string from the MCP layer by extracting the first element.
        if isinstance(namespace, list):
            namespace = namespace[0] if namespace else ""

        params: dict[str, Any] = {
            "tableBucketARN": table_bucket_arn,
            "namespace": namespace,
            "name": name,
            "newName": new_name,
        }
        if new_namespace:
            if isinstance(new_namespace, list):
                new_namespace = new_namespace[0] if new_namespace else ""
            params["newNamespaceName"] = new_namespace
        if version_token:
            params["versionToken"] = version_token

        client.rename_table(**params)

        return {"success": True, "message": "Table renamed successfully"}

    async def delete_s3_table(
        self,
        ctx: Context,
        table_bucket_arn: Annotated[str, Field(description="ARN of the S3 Table Bucket containing the table")],
        namespace: Annotated[str, Field(description="Namespace of the table (single string)")],
        name: Annotated[str, Field(description="Name of the table to delete")],
        version_token: Annotated[
            str | None, Field(description="Version token for optimistic locking (optional)")
        ] = None,
    ) -> dict[str, Any]:
        """Delete an S3 table.

        The S3 Tables ``DeleteTable`` API identifies the table by
        ``(tableBucketARN, namespace, name)`` — there is no ``tableArn``
        parameter.
        """
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        # This API expects namespace as a single string. Tolerate a list from
        # the MCP layer by extracting the first element.
        if isinstance(namespace, list):
            namespace = namespace[0] if namespace else ""

        params: dict[str, Any] = {
            "tableBucketARN": table_bucket_arn,
            "namespace": namespace,
            "name": name,
        }
        if version_token:
            params["versionToken"] = version_token

        client.delete_table(**params)

        return {"success": True, "message": f"Table {namespace}.{name} deleted successfully"}

    # ============================================================================
    # S3 TABLES POLICY MANAGEMENT
    # ============================================================================

    async def get_s3_table_policy(
        self, ctx: Context, resource_arn: Annotated[str, Field(description="ARN of the S3 table or bucket")]
    ) -> dict[str, Any]:
        """Get the resource policy for an S3 table or table bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        response = client.get_table_policy(resourceARN=resource_arn)

        return {"resource_arn": response.get("resourceARN"), "policy": response.get("policy"), "success": True}

    async def put_s3_table_policy(
        self,
        ctx: Context,
        resource_arn: Annotated[str, Field(description="ARN of the S3 table or bucket")],
        policy: Annotated[str, Field(description="Resource policy document as JSON string")],
    ) -> dict[str, Any]:
        """Set the resource policy for an S3 table or table bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        client.put_table_policy(resourceARN=resource_arn, policy=policy)

        return {"success": True, "message": f"Policy updated for {resource_arn}"}

    async def delete_s3_table_policy(
        self, ctx: Context, resource_arn: Annotated[str, Field(description="ARN of the S3 table or bucket")]
    ) -> dict[str, Any]:
        """Delete the resource policy for an S3 table or table bucket"""
        client = self.get_aws_client("s3tables")
        if not client:
            return {"error": "Failed to initialize S3 Tables client", "success": False}

        client.delete_table_policy(resourceARN=resource_arn)

        return {"success": True, "message": f"Policy deleted for {resource_arn}"}

    # ============================================================================
    # ICEBERG OPTIMIZATION AND RECOMMENDATIONS
    # ============================================================================

    async def recommend_iceberg_config(
        self,
        ctx: Context,
        data_volume_gb: Annotated[float, Field(description="Data volume in gigabytes")],
        record_count: Annotated[int, Field(description="Total number of records")],
        query_pattern: Annotated[
            str, Field(description="Query pattern: 'analytical', 'operational', or 'mixed' (default)")
        ] = "mixed",
        partition_strategy: Annotated[
            str | None, Field(description="Partitioning strategy (optional, will be recommended if not provided)")
        ] = None,
    ) -> dict[str, Any]:
        """Recommend optimal Iceberg configuration for S3 Tables based on data characteristics"""

        recommendations = {
            "table_properties": {},
            "write_properties": {},
            "partition_recommendations": [],
            "rationale": [],
        }

        # File size recommendations
        if data_volume_gb < 1:
            recommendations["table_properties"]["write.target-file-size-bytes"] = "134217728"  # 128 MB
            recommendations["rationale"].append(
                "Small dataset: Using 128 MB target file size for optimal query performance"
            )
        elif data_volume_gb < 100:
            recommendations["table_properties"]["write.target-file-size-bytes"] = "268435456"  # 256 MB
            recommendations["rationale"].append(
                "Medium dataset: Using 256 MB target file size for balanced performance"
            )
        else:
            recommendations["table_properties"]["write.target-file-size-bytes"] = "536870912"  # 512 MB
            recommendations["rationale"].append(
                "Large dataset: Using 512 MB target file size for efficient large-scale queries"
            )

        # Compaction settings
        if query_pattern == "analytical":
            recommendations["table_properties"]["write.metadata.compression-codec"] = "gzip"
            recommendations["table_properties"]["write.parquet.compression-codec"] = "snappy"
            recommendations["rationale"].append("Analytical workload: Using Snappy compression for fast decompression")
        elif query_pattern == "archival":
            recommendations["table_properties"]["write.metadata.compression-codec"] = "gzip"
            recommendations["table_properties"]["write.parquet.compression-codec"] = "zstd"
            recommendations["rationale"].append(
                "Archival workload: Using Zstandard compression for maximum compression ratio"
            )
        else:
            recommendations["table_properties"]["write.metadata.compression-codec"] = "gzip"
            recommendations["table_properties"]["write.parquet.compression-codec"] = "snappy"
            recommendations["rationale"].append("Mixed workload: Using Snappy compression for balanced performance")

        # Partitioning recommendations
        if partition_strategy:
            recommendations["partition_recommendations"].append(
                {"strategy": partition_strategy, "reason": f"User-specified partition strategy: {partition_strategy}"}
            )
        else:
            if record_count < 1000000:
                recommendations["partition_recommendations"].append(
                    {"strategy": "none", "reason": "Small record count: No partitioning needed, would create overhead"}
                )
            elif record_count < 100000000:
                recommendations["partition_recommendations"].append(
                    {
                        "strategy": "date-based",
                        "reason": "Medium record count: Date-based partitioning recommended for time-series data",
                    }
                )
            else:
                recommendations["partition_recommendations"].append(
                    {
                        "strategy": "multi-level",
                        "reason": "Large record count: Multi-level partitioning (e.g., year/month/day) recommended",
                    }
                )

        # Metadata optimization
        recommendations["table_properties"]["write.metadata.delete-after-commit.enabled"] = "true"
        recommendations["table_properties"]["write.metadata.previous-versions-max"] = "10"
        recommendations["rationale"].append(
            "Metadata cleanup: Keeping last 10 versions for time travel, auto-deleting old metadata"
        )

        # Snapshot expiration
        if data_volume_gb > 100:
            recommendations["table_properties"]["history.expire.max-snapshot-age-ms"] = "604800000"  # 7 days
            recommendations["rationale"].append(
                "Large dataset: Expiring snapshots after 7 days to manage metadata size"
            )
        else:
            recommendations["table_properties"]["history.expire.max-snapshot-age-ms"] = "2592000000"  # 30 days
            recommendations["rationale"].append(
                "Standard dataset: Expiring snapshots after 30 days for extended time travel"
            )

        return {
            "recommendations": recommendations,
            "data_characteristics": {
                "data_volume_gb": data_volume_gb,
                "record_count": record_count,
                "query_pattern": query_pattern,
            },
            "success": True,
        }

    async def estimate_s3_tables_cost(
        self,
        ctx: Context,
        data_volume_gb: Annotated[float, Field(description="Data volume in gigabytes")],
        monthly_requests: Annotated[int, Field(description="Estimated monthly API requests")],
        storage_class: Annotated[
            str, Field(description="Storage class: 'STANDARD' or 'INTELLIGENT_TIERING' (default: STANDARD)")
        ] = "STANDARD",
    ) -> dict[str, Any]:
        """Estimate monthly costs for S3 Tables storage and operations"""

        # S3 Tables pricing (approximate, varies by region)
        storage_cost_per_gb = 0.023  # Standard storage
        request_cost_per_1000 = 0.005  # PUT/POST/LIST requests

        if storage_class == "INTELLIGENT_TIERING":
            storage_cost_per_gb = 0.0125  # Intelligent-Tiering

        # Calculate costs
        storage_cost = data_volume_gb * storage_cost_per_gb
        request_cost = (monthly_requests / 1000) * request_cost_per_1000
        total_cost = storage_cost + request_cost

        return {
            "cost_estimate": {
                "storage_cost_usd": round(storage_cost, 2),
                "request_cost_usd": round(request_cost, 2),
                "total_monthly_cost_usd": round(total_cost, 2),
                "storage_class": storage_class,
                "data_volume_gb": data_volume_gb,
                "monthly_requests": monthly_requests,
            },
            "recommendations": [
                "Consider Intelligent-Tiering for infrequently accessed data",
                "Use lifecycle policies to transition old data to cheaper storage classes",
                "Optimize query patterns to reduce request costs",
            ],
            "success": True,
        }


def create_server():
    """Create and configure the MCP server instance for AgentCore."""
    return FastMCP(
        host="0.0.0.0",
        port=8000,
        stateless_http=True,  # Required for AgentCore
    )


if __name__ == "__main__":
    logger.info("Starting AgentCore AWS S3 Tables MCP Server")
    logger.info(f"AWS region: {aws_region}")

    # Create the MCP server instance
    mcp = create_server()

    # P28 R16.1: write/sensitive access are env-gated and default to False.
    # Enable per-server via MCP_ALLOW_WRITE / MCP_ALLOW_SENSITIVE_DATA_ACCESS
    # ("true") in the runtime's environment (deploy-cdk/lib/agentcore-stack.ts).
    # The deployed configuration keeps both False: no Lambda or agent is
    # currently wired to this server (create_infrastructure and
    # create_glue_pipeline create tables/namespaces via boto3 under their own
    # IAM roles), so its MCP surface stays read-only until a consumer with a
    # reviewed write need appears.
    allow_write = _env_flag("MCP_ALLOW_WRITE")
    allow_sensitive_data_access = _env_flag("MCP_ALLOW_SENSITIVE_DATA_ACCESS")
    logger.info(f"allow_write={allow_write}, allow_sensitive_data_access={allow_sensitive_data_access}")

    S3TablesHandler(mcp, allow_write=allow_write, allow_sensitive_data_access=allow_sensitive_data_access)

    logger.info("Running server on 0.0.0.0:8000 with stateless HTTP transport")

    # Run the server
    mcp.run(transport="streamable-http")
