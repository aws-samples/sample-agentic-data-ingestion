# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JDBC connection, database-secret, and VPC/security-group provisioning.

Extracted from ``lambda_function.py`` (P21 Req 4.2) as part of decomposing the
oversized create_glue_pipeline entrypoint into focused sibling modules. This
module owns Secrets Manager credential storage and the VPC/security-group wiring
that lets a Glue job reach an Aurora/RDS database. The moves are
behavior-preserving (verbatim function bodies).

``create_jdbc_connection`` used to live here and has been DELETED, finishing P17
Task 5.5 as ``LGR-38510fe-8`` recorded. It had zero callers in ``lambda/``,
``shared/``, ``mcp/``, ``agentcore/``, ``deploy-cdk/`` or ``tests/`` (re-verified
by grep at deletion; ``lambda_function`` only re-exported it), and its
``ConnectionProperties`` set a plaintext ``USERNAME`` that ``LGR-819980f-1`` forbids
for any file under ``lambda/**``. The canonical provisioner is
``create_glue_jdbc_connection`` in ``shared/jdbc_helpers.py``, which accepts no
credential arguments and so cannot repeat that leak. Do not reintroduce a
Glue-connection writer here.

The remaining functions (``create_database_secret``, ``configure_vpc_for_jdbc``,
``create_glue_security_group``, ``configure_security_group_rules``) are what is left
of that provisioning flow. With ``create_jdbc_connection`` gone,
``create_database_secret`` and ``configure_vpc_for_jdbc`` have no caller either;
they write a secret to Secrets Manager and security-group rules respectively, not
a plaintext credential to a job argument or connection, so they were left for a
separate dead-code pass rather than widened into this one.
``create_glue_security_group`` is still reached through ``lambda_function``'s
re-export by ``test_bare_except_fixes``.

Dependency direction is one-way: ``lambda_function`` → ``connections``. This
module MUST NOT import ``lambda_function``.
"""

import json
import os
import sys
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports (Layer_Import_Fallback — /opt/python
# in the Lambda layer, else the local ../../shared path for local/test runs).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# Import shared JDBC helpers (single source of truth for JDBC driver /
# default-port / URL construction). Bundled into the Strands Lambda layer at
# /opt/python/jdbc_helpers.py via CDK layer asset bundling; resolved from
# ../../shared locally via the Layer_Import_Fallback above.
from jdbc_helpers import get_default_port

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
# The Glue client went with `create_jdbc_connection`, its only user.
secretsmanager_client = boto3.client("secretsmanager")


def create_database_secret(pipeline_id: str, db_config: dict[str, Any]) -> str:
    """
    Store database credentials in AWS Secrets Manager with auto-rotation.

    Args:
        pipeline_id: Pipeline identifier
        db_config: Database configuration with credentials

    Returns:
        str: Secret ARN
    """
    try:
        db_type = db_config.get("database_type", "aurora-postgresql")
        secret_name = f"agentic-data-ingestion/{pipeline_id}/{db_type}-credentials"

        # Determine engine name
        if "postgresql" in db_type or "postgres" in db_type:
            engine = "postgres"
        elif "mysql" in db_type:
            engine = "mysql"
        elif "redshift" in db_type:
            engine = "redshift"
        else:
            engine = "postgres"

        # Get default port
        port = db_config.get("port", get_default_port(db_type))

        secret_value = {
            "username": db_config["username"],
            "password": db_config["password"],
            "engine": engine,
            "host": db_config["host"],
            "port": port,
            "dbname": db_config["database"],
            "dbClusterIdentifier": db_config.get("cluster_identifier"),
            "dbInstanceIdentifier": db_config.get("instance_identifier"),
        }

        try:
            # Try to create the secret
            response = secretsmanager_client.create_secret(
                Name=secret_name,
                Description=f"{db_type} credentials for AgenticDataIngestion pipeline {pipeline_id}",
                SecretString=json.dumps(secret_value),
                Tags=[
                    {"Key": "Project", "Value": "AgenticDataIngestion"},
                    {"Key": "PipelineId", "Value": pipeline_id},
                    {"Key": "DatabaseType", "Value": db_type},
                ],
            )
            logger.info(f"Created database secret: {secret_name}")
            return response["ARN"]

        except secretsmanager_client.exceptions.ResourceExistsException:
            # Update existing secret
            secretsmanager_client.update_secret(SecretId=secret_name, SecretString=json.dumps(secret_value))
            secret_arn = secretsmanager_client.describe_secret(SecretId=secret_name)["ARN"]
            logger.info(f"Updated existing database secret: {secret_name}")
            return secret_arn

    except Exception as e:
        logger.error(f"Error creating database secret: {e}")
        raise


def configure_vpc_for_jdbc(pipeline_id: str, db_config: dict[str, Any]) -> dict[str, Any]:
    """
    Configure VPC networking for Glue job to access JDBC database.

    Args:
        pipeline_id: Pipeline identifier
        db_config: Database configuration with VPC info

    Returns:
        dict: VPC configuration with security group and subnet IDs
    """
    try:
        ec2_client = boto3.client("ec2")

        # Get database VPC information
        db_vpc_id = db_config.get("vpc_id")
        db_subnet_ids = db_config.get("subnet_ids", [])
        db_security_group_id = db_config.get("security_group_id")

        if not db_vpc_id:
            logger.warning("VPC ID not provided in db_config, VPC configuration may be incomplete")
            return {}

        # Create or get Glue security group
        glue_sg_id = create_glue_security_group(pipeline_id, db_vpc_id, ec2_client)

        # Configure security group rules
        configure_security_group_rules(glue_sg_id, db_security_group_id, db_config, ec2_client)

        return {
            "vpc_id": db_vpc_id,
            "glue_security_group_id": glue_sg_id,
            "glue_subnet_ids": db_subnet_ids,
            "database_security_group_id": db_security_group_id,
        }

    except Exception as e:
        logger.error(f"Error configuring VPC for JDBC: {e}")
        raise


def create_glue_security_group(pipeline_id: str, vpc_id: str, ec2_client) -> str:
    """Create security group for Glue job"""
    try:
        sg_name = f"agentic-data-ingestion-{pipeline_id}-glue-sg"

        # Check if security group exists
        try:
            response = ec2_client.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [sg_name]}, {"Name": "vpc-id", "Values": [vpc_id]}]
            )

            if response["SecurityGroups"]:
                sg_id = response["SecurityGroups"][0]["GroupId"]
                logger.info(f"Using existing Glue security group: {sg_id}")
                return sg_id
        except Exception as e:
            logger.warning(f"Failed to check existing security group: {e}")

        # Create new security group
        response = ec2_client.create_security_group(
            GroupName=sg_name,
            Description=f"Security group for AgenticDataIngestion pipeline {pipeline_id}",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": sg_name},
                        {"Key": "Project", "Value": "AgenticDataIngestion"},
                        {"Key": "PipelineId", "Value": pipeline_id},
                    ],
                }
            ],
        )

        sg_id = response["GroupId"]
        logger.info(f"Created Glue security group: {sg_id}")

        # Add self-referencing rule (required for Glue)
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535, "UserIdGroupPairs": [{"GroupId": sg_id}]}
            ],
        )

        return sg_id

    except Exception as e:
        logger.error(f"Error creating Glue security group: {e}")
        raise


def configure_security_group_rules(glue_sg_id: str, db_sg_id: str, db_config: dict[str, Any], ec2_client):
    """Configure security group rules for Glue → Database connectivity"""
    try:
        # Get database port
        db_port = db_config.get("port", get_default_port(db_config.get("database_type", "aurora-postgresql")))

        # Add rule to database security group to allow traffic from Glue
        try:
            ec2_client.authorize_security_group_ingress(
                GroupId=db_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": db_port,
                        "ToPort": db_port,
                        "UserIdGroupPairs": [
                            {"GroupId": glue_sg_id, "Description": "Allow Glue job access to database"}
                        ],
                    }
                ],
            )
            logger.info(f"Added ingress rule to database SG {db_sg_id} from Glue SG {glue_sg_id}")
        except Exception as e:
            if "InvalidPermission.Duplicate" in str(e):
                logger.info("Security group rule already exists")
            else:
                logger.warning(f"Could not add security group rule: {e}")

    except Exception as e:
        logger.error(f"Error configuring security group rules: {e}")
        raise
