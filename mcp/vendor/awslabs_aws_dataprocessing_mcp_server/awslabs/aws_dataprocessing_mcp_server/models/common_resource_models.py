# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Response models for Common Resource operations."""

from typing import Any

from mcp.types import CallToolResult
from pydantic import BaseModel

# ============================================================================
# IAM Models
# ============================================================================


class RoleSummary(BaseModel):
    """Summary of an IAM role."""

    role_name: str
    role_arn: str
    description: str | None = None
    create_date: str
    assume_role_policy_document: dict[str, Any]


class ServiceRolesResponse(CallToolResult):
    """Response model for listing IAM roles for a specific service."""

    service_type: str
    roles: list[RoleSummary]


class PolicySummary(BaseModel):
    """Summary of an IAM policy."""

    policy_type: str
    description: str | None = None
    policy_document: dict[str, Any] | None = None


class RoleDescriptionResponse(CallToolResult):
    """Response model for describing an IAM role."""

    role_arn: str
    assume_role_policy_document: dict[str, Any]
    description: str | None = None
    managed_policies: list[PolicySummary]
    inline_policies: list[PolicySummary]


class AddInlinePolicyResponse(CallToolResult):
    """Response model for adding an inline policy to an IAM role."""

    policy_name: str
    role_name: str
    permissions_added: dict[str, Any] | list[dict[str, Any]]


class CreateRoleResponse(CallToolResult):
    """Response model for creating an IAM role."""

    role_name: str
    role_arn: str


# ============================================================================
# S3 Models
# ============================================================================


class BucketInfo(BaseModel):
    """Information about an S3 bucket."""

    name: str
    creation_date: str
    region: str
    object_count: str
    last_modified: str
    idle_status: str


class ListS3BucketsResponse(CallToolResult):
    """Response model for listing S3 buckets."""

    region: str
    bucket_count: int
    buckets: list[BucketInfo]


class UploadToS3Response(CallToolResult):
    """Response model for uploading to S3."""

    s3_uri: str
    bucket_name: str
    s3_key: str


class AnalyzeS3UsageResponse(CallToolResult):
    """Response model for S3 usage analysis."""

    analysis_summary: str
    service_usage: dict[str, list[str]]
