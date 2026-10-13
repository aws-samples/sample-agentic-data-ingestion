# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Shared MCP Factory for initializing Strands Agents with MCP transport.

Centralizes the MCP agent initialization logic previously duplicated across
create_glue_pipeline, create_code, and test_code Lambda functions.

Handles:
- URL transformation (runtimes/ vs runtime/ endpoint formats)
- SigV4 authentication with fallback to basic transport
- MCP client creation and tool listing
- Agent creation with configurable system prompt and model
- Tool allowlist filtering for focused server profiles
"""

import logging
import os
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    # ``strands`` is imported lazily inside the factory functions below to keep
    # it off the cold-start path. This guard makes the "MCPClient" return
    # annotations resolvable for type checkers and linters without adding a
    # runtime import.
    from strands.tools.mcp import MCPClient

logger = logging.getLogger(__name__)

# Default model ID used across all AgenticDataIngestion Lambdas. Repointed to the single
# source of truth in model_config (P21 Req 1 / Req 8.2). This deliberately
# replaces the previous divergent value
# ("global.anthropic.claude-sonnet-4-5-20250929-v1:0") with the canonical
# dateless Claude 4.6 inference-profile ID — the one place where the resolved
# model actually changes (4.5 -> 4.6).
from model_config import MODEL_ID_DEFAULT as DEFAULT_MODEL_ID

# Feature-flag controlled import of SigV4 transport
MCP_SIGV4_ENABLED = os.environ.get("MCP_SIGV4_ENABLED", "true").lower() == "true"

if MCP_SIGV4_ENABLED:
    from streamable_http_sigv4 import streamablehttp_client_with_sigv4

    SIGV4_AVAILABLE = True
    logger.info("SigV4 authentication available for AgentCore (using streamable_http_sigv4)")
else:
    SIGV4_AVAILABLE = False
    logger.info("SigV4 transport disabled via MCP_SIGV4_ENABLED=false")


def _get_account_id() -> str:
    """Get AWS account ID from env var or STS (cached)."""
    try:
        from env_utils import get_aws_account_id

        return get_aws_account_id()
    except ImportError:
        # Fallback if env_utils not available
        account = os.environ.get("AWS_ACCOUNT_ID", "")
        if not account:
            try:
                account = boto3.client("sts").get_caller_identity()["Account"]
            except Exception:
                pass
        return account


def _build_mcp_url(endpoint: str, region: str, account_id: str) -> str:
    """
    Transform an AgentCore endpoint to a proper MCP URL with encoded ARN.

    Handles three endpoint formats:
    - Encoded ARN format: URL contains /runtimes/arn%3A... — use as-is.
    - Short ID format: URL contains /runtimes/{runtime_id}/invocations — convert to ARN.
    - Old format (runtime/): Extract runtime ID, construct full ARN, encode it.
    - Fallback: Return the endpoint as-is for unrecognized formats.

    Args:
        endpoint: The AGENTCORE_ENDPOINT value.
        region: AWS region (e.g. 'us-east-1').
        account_id: AWS account ID.

    Returns:
        The fully-formed MCP URL string.
    """
    if "/runtimes/" in endpoint:
        # Check if it already has an encoded ARN
        if "arn%3A" in endpoint or "arn%3a" in endpoint:
            mcp_url = endpoint
            if "?qualifier=" not in mcp_url:
                mcp_url = f"{mcp_url}?qualifier=DEFAULT"
            logger.info(f"Using AgentCore URL with encoded ARN: {mcp_url}")
            return mcp_url

        # Short runtime ID format — extract ID and build ARN URL
        # e.g. https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/agentic_data_ingestion_mcp_codegen-P9iUNeApwM/invocations?qualifier=DEFAULT
        import re

        match = re.search(r"/runtimes/([^/]+?)(?:/invocations|$)", endpoint)
        if match:
            runtime_id = match.group(1)
            agent_runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
            encoded_arn = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
            mcp_url = (
                f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
            )
            logger.info(f"Constructed MCP URL from short runtime ID: {mcp_url}")
            logger.info(f"Runtime ARN: {agent_runtime_arn}")
            return mcp_url

    if "runtime/" in endpoint:
        # Old format – extract runtime ID and construct ARN-based URL
        runtime_id = endpoint.split("/runtime/")[-1]
        agent_runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
        encoded_arn = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
        mcp_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )
        logger.info(f"Constructed MCP URL from runtime ID: {mcp_url}")
        logger.info(f"Runtime ARN: {agent_runtime_arn}")
        return mcp_url

    # Fallback for other URL formats
    logger.info(f"Using direct MCP URL: {endpoint}")
    return endpoint


def _create_mcp_transport(mcp_url: str, region: str, timeout: int):
    """
    Create an MCP transport with SigV4 authentication (preferred) or basic HTTP fallback.

    Returns a transport factory callable suitable for ``MCPClient``.

    Args:
        mcp_url: The fully-formed MCP URL.
        region: AWS region for SigV4 signing.
        timeout: Connection timeout in seconds.

    Returns:
        A transport context-manager produced by the appropriate client function.
    """
    if SIGV4_AVAILABLE:
        logger.info("Using SigV4 authenticated transport for MCP")
        session = boto3.Session()
        credentials = session.get_credentials()
        return streamablehttp_client_with_sigv4(
            url=mcp_url,
            credentials=credentials,
            service="bedrock-agentcore",
            region=region,
            timeout=timeout,
            terminate_on_close=False,
        )

    logger.warning("SigV4 not available, using basic transport")
    from mcp.client.streamable_http import streamablehttp_client

    return streamablehttp_client(url=mcp_url, timeout=float(timeout))


def _get_tool_name(tool) -> str:
    """Extract the tool name from various tool object formats.

    Strands MCP tools can have different attribute layouts depending on the
    version and transport layer.  This helper tries the known patterns and
    returns the name string, or the class name as a last resort.
    """
    if hasattr(tool, "tool") and hasattr(tool.tool, "name"):
        return tool.tool.name
    if hasattr(tool, "name"):
        return tool.name
    if hasattr(tool, "function") and hasattr(tool.function, "name"):
        return tool.function.name
    if isinstance(tool, dict) and "name" in tool:
        return tool["name"]
    return type(tool).__name__


def _filter_tools_by_allowlist(tools: list, tool_allowlist: list[str]) -> list:
    """Return only tools whose names appear in *tool_allowlist*.

    Args:
        tools: Full list of MCP tool objects.
        tool_allowlist: Tool names to keep.

    Returns:
        Filtered list retaining original order.
    """
    allowed_set = set(tool_allowlist)
    filtered = [t for t in tools if _get_tool_name(t) in allowed_set]
    logger.info(f"Tool allowlist applied: {len(filtered)}/{len(tools)} tools retained (requested {len(allowed_set)})")
    return filtered


def create_mcp_tools(
    endpoint: str,
    tool_allowlist: list[str] | None = None,
    timeout: int = 120,
) -> list:
    """Create MCP tools from an AgentCore endpoint.

    Connects to the MCP server, lists available tools, and optionally filters
    them via *tool_allowlist*.  This is the lower-level building block used by
    ``initialize_strands_agent_with_mcp`` and can also be called directly when
    callers want to compose their own ``Agent`` instance.

    Args:
        endpoint: The AGENTCORE_ENDPOINT URL.
        tool_allowlist: When provided, only tools whose names are in this list
            are returned.  ``None`` means return all tools (backward compatible).
        timeout: MCP transport timeout in seconds.

    Returns:
        List of MCP tool objects ready to pass to a Strands ``Agent``.

    Raises:
        RuntimeError: If the MCP connection or tool listing fails.
    """
    from strands.tools.mcp import MCPClient

    aws_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    aws_account_id = _get_account_id()

    mcp_url = _build_mcp_url(endpoint, aws_region, aws_account_id)

    def transport_factory():
        return _create_mcp_transport(mcp_url, aws_region, timeout)

    mcp_client = MCPClient(transport_factory)

    with mcp_client:
        tools = mcp_client.list_tools_sync()
        logger.info(f"MCP server returned {len(tools)} tools")

        tool_names = [_get_tool_name(t) for t in tools]
        logger.info(f"Available MCP tools: {tool_names}")

        if tool_allowlist is not None:
            tools = _filter_tools_by_allowlist(tools, tool_allowlist)

        return tools


def initialize_strands_agent_with_mcp(
    system_prompt: str,
    model_id: str = DEFAULT_MODEL_ID,
    timeout: int = 120,
    tool_allowlist: list[str] | None = None,
    endpoint: str | None = None,
) -> tuple:
    """
    Initialize a Strands Agent with MCP transport, SigV4 auth, and tool listing.

    Reads ``AGENTCORE_ENDPOINT``, ``AWS_REGION``, and ``AWS_ACCOUNT_ID`` from
    environment variables so that this function can be called from any Lambda
    (including nested-function contexts that previously relied on closure scope).

    Args:
        system_prompt: The system prompt to configure the agent's behaviour.
        model_id: Bedrock model identifier. Defaults to Claude Sonnet 4.6
            (the canonical ``MODEL_ID_DEFAULT`` from ``model_config``; P21 Req 1).
        timeout: MCP transport timeout in seconds. Defaults to 120.
        tool_allowlist: When provided, only tools whose names are in this list
            are passed to the agent.  ``None`` means use all tools.
        endpoint: When provided, overrides the ``AGENTCORE_ENDPOINT`` env var.
            Use this to connect to a specific focused MCP server profile.

    Returns:
        ``(agent, mcp_client)`` on success, or ``(None, None)`` if MCP is
        unavailable or initialisation fails.
    """
    try:
        # Import Strands libraries (may not be installed in all environments)
        from strands import Agent
        from strands.models.bedrock import BedrockModel
        from strands.tools.mcp import MCPClient

        # Read environment variables directly (not from closure scope)
        agentcore_endpoint = endpoint or os.environ.get("AGENTCORE_ENDPOINT", "")
        aws_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        aws_account_id = _get_account_id()

        if not agentcore_endpoint:
            logger.warning("AGENTCORE_ENDPOINT not configured")
            return None, None

        # Initialize Bedrock model
        model = BedrockModel(model_id=model_id)

        # Build the MCP URL from the endpoint
        mcp_url = _build_mcp_url(agentcore_endpoint, aws_region, aws_account_id)

        # Create transport factory (MCPClient expects a callable that returns a transport)
        def transport_factory():
            return _create_mcp_transport(mcp_url, aws_region, timeout)

        # Create Strands MCPClient
        mcp_client = MCPClient(transport_factory)

        # Connect, list tools, and create the agent
        logger.info("Connecting to MCP server and listing tools")
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            logger.info(f"Successfully connected to MCP server. Found {len(tools)} tools")

            # Log tool names for debugging
            tool_names = [_get_tool_name(t) for t in tools]
            logger.info(f"Available MCP tools: {tool_names}")

            # Apply allowlist filter if provided
            if tool_allowlist is not None:
                tools = _filter_tools_by_allowlist(tools, tool_allowlist)

            # Create agent with MCP tools
            agent = Agent(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
            )

            logger.info("Successfully initialized Strands Agent with MCP tools")
            return agent, mcp_client

    except Exception as e:
        logger.error(f"Error initializing Strands Agent with MCP: {e}")
        return None, None


def _create_mcp_client_for_endpoint(endpoint: str, timeout: int = 120) -> "MCPClient":
    """Create an MCPClient for a single AgentCore endpoint.

    Returns an MCPClient instance that can be passed directly to Agent(tools=[...]).
    Strands manages the lifecycle automatically (no manual context manager needed).
    """
    from strands.tools.mcp import MCPClient

    aws_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    aws_account_id = _get_account_id()
    mcp_url = _build_mcp_url(endpoint, aws_region, aws_account_id)

    def transport_factory():
        return _create_mcp_transport(mcp_url, aws_region, timeout)

    return MCPClient(transport_factory)


def initialize_strands_agent_with_multi_mcp(
    system_prompt: str,
    endpoints: list[str] | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    timeout: int = 120,
) -> tuple:
    """
    Initialize a Strands Agent connected to multiple MCP servers.

    Each endpoint gets its own MCPClient. Strands manages all connections
    automatically via the managed integration pattern (no context managers).

    Args:
        system_prompt: The system prompt for the agent.
        endpoints: List of AgentCore endpoint URLs. If None, reads
            AGENTCORE_ENDPOINT from env as a single-endpoint fallback.
        model_id: Bedrock model identifier.
        timeout: MCP transport timeout in seconds.

    Returns:
        (agent, mcp_clients) on success, or (None, None) on failure.
        mcp_clients is a list of MCPClient instances.
    """
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel

        # Resolve endpoints
        if not endpoints:
            default_ep = os.environ.get("AGENTCORE_ENDPOINT", "")
            if not default_ep:
                logger.warning("No endpoints provided and AGENTCORE_ENDPOINT not set")
                return None, None
            endpoints = [default_ep]

        # Filter out empty strings
        endpoints = [ep for ep in endpoints if ep]
        if not endpoints:
            logger.warning("All provided endpoints are empty")
            return None, None

        logger.info(f"Initializing Strands Agent with {len(endpoints)} MCP endpoint(s)")

        # Create one MCPClient per endpoint
        mcp_clients = []
        for ep in endpoints:
            client = _create_mcp_client_for_endpoint(ep, timeout)
            mcp_clients.append(client)
            logger.info(f"Created MCPClient for endpoint: {ep[:80]}...")

        # Initialize Bedrock model
        model = BedrockModel(model_id=model_id)

        # Pass all MCPClient instances to Agent — Strands manages lifecycle
        agent = Agent(
            model=model,
            tools=mcp_clients,
            system_prompt=system_prompt,
        )

        logger.info(f"Successfully initialized Strands Agent with {len(mcp_clients)} MCP server(s)")
        return agent, mcp_clients

    except Exception as e:
        logger.error(f"Error initializing Strands Agent with multi-MCP: {e}")
        return None, None
