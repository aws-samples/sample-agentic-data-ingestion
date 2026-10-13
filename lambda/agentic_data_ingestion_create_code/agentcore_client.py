# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore MCP client for AWS Bedrock AgentCore Runtime integration.

Extracted from lambda_function.py to provide a focused module for
AgentCore MCP communication, tool invocation, and enhanced prompt generation.
"""

import asyncio
import json
import logging

import boto3

logger = logging.getLogger()

# Import prompt template loader
try:
    from prompt_template_loader import load_prompt_template
except ImportError:
    load_prompt_template = None

# Enhanced MCP integration imports (with fallback)
try:
    from mcp import ClientSession

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

# Import the existing streamable_http_sigv4 module
try:
    from streamable_http_sigv4 import streamablehttp_client_with_sigv4

    SIGV4_AVAILABLE = True
except ImportError:
    SIGV4_AVAILABLE = False


class AgentCoreMCPClient:
    """MCP client for AgentCore Runtime using SigV4 authentication."""

    def __init__(self, runtime_arn: str, region: str):
        self.runtime_arn = runtime_arn
        self.region = region
        self.session = boto3.Session()
        self.credentials = self.session.get_credentials()

        # Construct proper MCP URL following AgentCore documentation
        encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
        self.mcp_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )

        logger.info(f"Initialized AgentCore MCP client for region {region}")
        logger.info(f"MCP URL: {self.mcp_url}")

    async def call_mcp_tools(self, tools_to_call: list) -> dict:
        """Call specific MCP tools using proper MCP protocol with SigV4 authentication"""
        if not MCP_AVAILABLE:
            return {
                "success": False,
                "error": "MCP client dependencies not available",
                "recommendations_available": False,
            }

        if not SIGV4_AVAILABLE:
            return {"success": False, "error": "SigV4 authentication not available", "recommendations_available": False}

        results = {}

        try:
            # Use streamable_http_sigv4 for proper AWS authentication
            async with (
                streamablehttp_client_with_sigv4(
                    url=self.mcp_url,
                    credentials=self.credentials,
                    service="bedrock-agentcore",
                    region=self.region,
                    timeout=120,
                    terminate_on_close=False,
                ) as (read_stream, write_stream, _),
                ClientSession(read_stream, write_stream) as session,
            ):
                logger.info("Initializing MCP session with SigV4 authentication...")
                await session.initialize()

                # List available tools first
                logger.info("Listing available MCP tools...")
                tool_result = await session.list_tools()
                available_tools = [tool.name for tool in tool_result.tools]

                logger.info(f"Available tools: {available_tools}")
                results["available_tools"] = available_tools
                results["tool_results"] = {}

                # Call each requested tool
                for tool_call in tools_to_call:
                    tool_name = tool_call.get("name")
                    tool_args = tool_call.get("arguments", {})

                    if tool_name in available_tools:
                        try:
                            logger.info(f"Calling tool: {tool_name} with args: {tool_args}")
                            tool_response = await session.call_tool(name=tool_name, arguments=tool_args)

                            # Extract the result content
                            if tool_response.content:
                                result_content = (
                                    tool_response.content[0].text
                                    if tool_response.content[0].text
                                    else str(tool_response.content[0])
                                )
                                results["tool_results"][tool_name] = {"success": True, "result": result_content}
                            else:
                                results["tool_results"][tool_name] = {
                                    "success": True,
                                    "result": "Tool executed successfully (no content returned)",
                                }

                        except Exception as e:
                            logger.error(f"Error calling tool {tool_name}: {e!s}")
                            results["tool_results"][tool_name] = {"success": False, "error": str(e)}
                    else:
                        results["tool_results"][tool_name] = {
                            "success": False,
                            "error": f"Tool {tool_name} not available",
                        }

        except Exception as e:
            logger.error(f"Error connecting to MCP server with SigV4: {e!s}")
            results["error"] = str(e)

        return results

    def get_data_processing_insights_sync(self, source_schema: dict, target_schema: dict, data_volume: str) -> dict:
        """Get data processing insights using proper MCP tools"""

        # Define MCP tools to call for enhanced ETL code generation (updated tool names)
        mcp_tools_to_call = [
            {"name": "manage_aws_glue_catalog", "arguments": {"operation": "list-databases", "max_results": 20}},
            {
                "name": "manage_aws_glue_catalog",
                "arguments": {"operation": "list-tables", "database_name": "default", "max_results": 50},
            },
            {"name": "manage_aws_glue_jobs", "arguments": {"operation": "list-jobs", "max_results": 10}},
        ]

        try:
            # Call MCP tools asynchronously
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                mcp_results = loop.run_until_complete(self.call_mcp_tools(mcp_tools_to_call))
            finally:
                loop.close()

            # Process results into insights
            if mcp_results.get("error"):
                return {"success": False, "error": mcp_results["error"], "recommendations_available": False}

            # Extract insights from tool results
            tool_results = mcp_results.get("tool_results", {})
            available_tools = mcp_results.get("available_tools", [])

            insights = f"""
MCP-Enhanced Data Processing Analysis:
=====================================

Available AWS Data Processing Tools: {len(available_tools)}
Tools Successfully Called: {len([r for r in tool_results.values() if r.get("success")])}

Data Catalog Analysis:
{tool_results.get("agentcore_analyze_glue_data_catalog_comprehensive", {}).get("result", "Not available")}

Available Databases:
{tool_results.get("agentcore_list_glue_databases", {}).get("result", "Not available")}

Existing Glue Jobs:
{tool_results.get("agentcore_list_glue_jobs", {}).get("result", "Not available")}

Recommendations:
- Use MCP insights for optimal Glue job configuration
- Leverage existing data catalog structure
- Apply performance optimizations based on data volume: {data_volume}
"""

            return {
                "success": True,
                "insights": insights,
                "recommendations_available": True,
                "tools_count": len(available_tools),
                "mcp_results": mcp_results,
            }

        except Exception as e:
            logger.warning(f"MCP insights failed: {e}")
            return {"success": False, "error": str(e), "recommendations_available": False}

    def generate_enhanced_prompt(
        self,
        source_schema: dict,
        target_schema: dict,
        mapping_config: dict,
        data_volume: str,
        performance_requirements: dict,
        regeneration_feedback: dict = None,
        target_type: str = "",
        target_format: str = "parquet",
    ) -> str:
        """Generate enhanced prompt using AgentCore MCP insights.

        Args:
            source_schema: Source data schema definition.
            target_schema: Target data schema definition.
            mapping_config: Field mapping configuration.
            data_volume: Expected data volume description.
            performance_requirements: Performance requirement details.
            regeneration_feedback: Optional feedback from previous iterations.
            target_type: Target type string (e.g. 'S3_TABLES').
            target_format: Target format string (e.g. 'parquet', 'iceberg').

        Returns:
            The enhanced prompt string for code generation.
        """

        # Get insights from AgentCore Runtime MCP server
        insights_result = self.get_data_processing_insights_sync(source_schema, target_schema, data_volume)

        agentcore_mode = insights_result.get("success", False)
        agentcore_insights = insights_result.get("insights", "No specific insights available") if agentcore_mode else ""

        feedback_section = ""
        if regeneration_feedback:
            feedback_section = f"""

    ## Previous Iteration Feedback:
    {json.dumps(regeneration_feedback, indent=2)}

    Please address the issues identified in the feedback above.
    """

        if load_prompt_template:
            return load_prompt_template(
                "code_generation_enhanced.j2",
                agentcore_mode=agentcore_mode,
                source_schema=json.dumps(source_schema, indent=2),
                target_schema=json.dumps(target_schema, indent=2),
                mapping_config=json.dumps(mapping_config, indent=2),
                data_volume=data_volume,
                performance_requirements=json.dumps(performance_requirements, indent=2),
                target_type=target_type,
                target_format=target_format,
                agentcore_insights=agentcore_insights,
                feedback_section=feedback_section,
                available_databases=[],
                common_job_configs="[]",
            )

        # Fallback: build prompt inline if template loader is unavailable
        return f"""Generate optimized AWS Glue PySpark ETL code.

    ## Source Schema
    {json.dumps(source_schema, indent=2)}

    ## Target Schema
    {json.dumps(target_schema, indent=2)}

    ## Field Mappings
    {json.dumps(mapping_config, indent=2)}

    ## Data Characteristics
    - Expected Data Volume: {data_volume}
    - Performance Requirements: {json.dumps(performance_requirements, indent=2)}

    {feedback_section}

    Return ONLY the complete Python code."""


def create_agentcore_mcp_client(agent_runtime_arn: str, region: str = "us-west-2") -> AgentCoreMCPClient:
    """
    Create proper MCP client to AgentCore Runtime using official AWS pattern.
    Based on hosting_mcp_server_iam_auth.ipynb example with streamable_http_sigv4.

    Args:
        agent_runtime_arn: The AgentCore Runtime ARN.
        region: AWS region (default: 'us-west-2').

    Returns:
        An initialized AgentCoreMCPClient instance.
    """
    return AgentCoreMCPClient(agent_runtime_arn, region)
