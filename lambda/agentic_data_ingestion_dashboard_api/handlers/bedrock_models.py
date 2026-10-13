#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handler for Bedrock models endpoint"""

from typing import Any

import boto3

from .common import create_response, logger


def get_bedrock_models() -> dict[str, Any]:
    """Get available Bedrock foundation models

    Returns:
        Response with list of available Bedrock models
    """
    try:
        logger.info("Fetching Bedrock foundation models")

        # Initialize Bedrock client
        bedrock_client = boto3.client("bedrock")

        # Get available models
        response = bedrock_client.list_foundation_models()

        # Filter and format models
        models = []
        for model in response.get("modelSummaries", []):
            model_id = model.get("modelId", "")

            # Include Claude and Amazon models
            if "claude" in model_id.lower() or "amazon" in model_id.lower():
                models.append(
                    {
                        "modelId": model_id,
                        "modelName": model.get("modelName", model_id),
                        "providerName": model.get("providerName", "Unknown"),
                        "inputModalities": model.get("inputModalities", []),
                        "outputModalities": model.get("outputModalities", []),
                        "responseStreamingSupported": model.get("responseStreamingSupported", False),
                    }
                )

        logger.info(f"Found {len(models)} Bedrock models")

        return create_response(200, {"models": models, "count": len(models)})

    except Exception as e:
        logger.error(f"Error fetching Bedrock models: {e}")

        # Return common models as fallback
        fallback_models = [
            {
                "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "modelName": "Claude 3.5 Sonnet v2",
                "providerName": "Anthropic",
            },
            {
                "modelId": "anthropic.claude-3-5-haiku-20241022-v1:0",
                "modelName": "Claude 3.5 Haiku",
                "providerName": "Anthropic",
            },
            {
                "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                "modelName": "Claude 3 Haiku",
                "providerName": "Anthropic",
            },
            {
                "modelId": "anthropic.claude-3-sonnet-20240229-v1:0",
                "modelName": "Claude 3 Sonnet",
                "providerName": "Anthropic",
            },
            {
                "modelId": "anthropic.claude-3-opus-20240229-v1:0",
                "modelName": "Claude 3 Opus",
                "providerName": "Anthropic",
            },
            {"modelId": "us.amazon.nova-pro-v1:0", "modelName": "Amazon Nova Pro", "providerName": "Amazon"},
            {"modelId": "us.amazon.nova-lite-v1:0", "modelName": "Amazon Nova Lite", "providerName": "Amazon"},
            {"modelId": "us.amazon.nova-micro-v1:0", "modelName": "Amazon Nova Micro", "providerName": "Amazon"},
        ]

        logger.info(f"Using fallback models: {len(fallback_models)} models")

        return create_response(200, {"models": fallback_models, "count": len(fallback_models), "fallback": True})
