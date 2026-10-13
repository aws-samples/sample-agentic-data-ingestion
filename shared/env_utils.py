# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import logging
import os

import boto3

logger = logging.getLogger(__name__)

_LOCAL_MODE = os.environ.get("AGENTIC_DATA_INGESTION_ENV", "").lower() == "local"

_aws_account_id = None


def get_aws_account_id() -> str:
    """Lazily resolve AWS account ID from env var or STS."""
    global _aws_account_id
    if _aws_account_id is None:
        _aws_account_id = os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()["Account"]
    return _aws_account_id


def require_env(name: str, default: str | None = None) -> str:
    """Get required environment variable or raise ConfigurationError.

    In local mode (AGENTIC_DATA_INGESTION_ENV=local), falls back to the provided default
    to enable local development and testing without full environment configuration.
    """
    value = os.environ.get(name)
    if value:
        return value
    if _LOCAL_MODE and default is not None:
        logger.warning(f"Using default value for '{name}' in local mode: {default}")
        return default
    raise OSError(
        f"Required environment variable '{name}' is not set. Set AGENTIC_DATA_INGESTION_ENV=local to use defaults for development."
    )
