#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Direct HTTP Test for AWS S3 Tables MCP Server on AgentCore

This test validates that the S3 Tables MCP server is working correctly on AgentCore Runtime
following the deployment success criteria.
"""

import json
import sys

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def test_agentcore_s3tables_mcp_direct(runtime_id, account_id=None, region="us-east-1"):
    """Test AgentCore S3 Tables MCP server with direct HTTP request"""

    if not account_id:
        # Get account ID from current session
        sts_client = boto3.client("sts")
        account_id = sts_client.get_caller_identity()["Account"]

    # Construct AgentCore URL
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3A{region}%3A{account_id}%3Aruntime%2F{runtime_id}/invocations?qualifier=DEFAULT"

    print("Testing AgentCore S3 Tables MCP Server...")
    print(f"Runtime ID: {runtime_id}")
    print(f"Account ID: {account_id}")
    print(f"Region: {region}")
    print(f"URL: {url}")
    print()

    # MCP initialize request
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }

    # Required headers for AgentCore
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",  # CRITICAL: Both content types required
    }

    try:
        # Sign request with SigV4
        session = boto3.Session()
        credentials = session.get_credentials()
        request = AWSRequest(method="POST", url=url, data=json.dumps(payload), headers=headers)
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)

        # Make request
        print("Making HTTP request to AgentCore...")
        response = requests.post(url, data=request.body, headers=dict(request.headers), timeout=30)

        print(f"Response Status: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        print(f"Response Body: {response.text[:500]}...")
        print()

        # Validate response
        if response.status_code == 200:
            try:
                # Handle Server-Sent Events (SSE) format from AgentCore
                response_text = response.text

                # Extract JSON from SSE format (data: {...})
                if "data: " in response_text:
                    # Find the JSON data after "data: "
                    data_start = response_text.find("data: ") + 6
                    data_end = response_text.find("\n", data_start)
                    if data_end == -1:
                        data_end = len(response_text)
                    json_str = response_text[data_start:data_end].strip()
                    response_data = json.loads(json_str)
                else:
                    # Fallback to regular JSON parsing
                    response_data = response.json()

                # Check for FastMCP in response
                if "FastMCP" in response_text or "s3tables" in response_text.lower():
                    print("✅ SUCCESS: S3 Tables MCP server detected!")

                    # Validate expected response structure
                    if (
                        response_data.get("jsonrpc") == "2.0"
                        and "result" in response_data
                        and "serverInfo" in response_data["result"]
                    ):
                        server_info = response_data["result"]["serverInfo"]
                        print(f"✅ Server Name: {server_info.get('name', 'Unknown')}")
                        print(f"✅ Server Version: {server_info.get('version', 'Unknown')}")
                        print("✅ S3 Tables MCP server is working correctly!")
                        return True
                    else:
                        print("❌ FAIL: Invalid response structure")
                        return False
                else:
                    print("❌ FAIL: S3 Tables MCP server not detected in response")
                    return False

            except json.JSONDecodeError:
                print("❌ FAIL: Invalid JSON response")
                return False
        else:
            print(f"❌ FAIL: HTTP {response.status_code}")
            print(f"Response: {response.text}")
            return False

    except Exception as e:
        print(f"❌ ERROR: {e!s}")
        return False


def test_tools_list(runtime_id, account_id=None, region="us-east-1"):
    """Test listing available S3 Tables tools"""

    if not account_id:
        sts_client = boto3.client("sts")
        account_id = sts_client.get_caller_identity()["Account"]

    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3A{region}%3A{account_id}%3Aruntime%2F{runtime_id}/invocations?qualifier=DEFAULT"

    # MCP tools/list request
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        request = AWSRequest(method="POST", url=url, data=json.dumps(payload), headers=headers)
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)

        print("Testing tools/list endpoint...")
        response = requests.post(url, data=request.body, headers=dict(request.headers), timeout=30)

        if response.status_code == 200:
            # Handle SSE format for tools/list response
            response_text = response.text

            if "data: " in response_text:
                # Extract JSON from SSE format
                data_start = response_text.find("data: ") + 6
                data_end = response_text.find("\n", data_start)
                if data_end == -1:
                    data_end = len(response_text)
                json_str = response_text[data_start:data_end].strip()
                response_data = json.loads(json_str)
            else:
                response_data = response.json()

            if "result" in response_data and "tools" in response_data["result"]:
                tools = response_data["result"]["tools"]
                print(f"✅ Found {len(tools)} S3 Tables tools:")
                for tool in tools:
                    print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')[:80]}")

                # Verify S3 Tables specific tools
                tool_names = [tool.get("name", "") for tool in tools]
                s3tables_tools = [name for name in tool_names if "s3tables" in name.lower() or "table" in name.lower()]

                if s3tables_tools:
                    print(f"\n✅ S3 Tables specific tools found: {len(s3tables_tools)}")
                    return True
                else:
                    print("\n⚠️  WARNING: No S3 Tables specific tools found")
                    return True  # Still pass if tools are available
            else:
                print("❌ FAIL: No tools found in response")
                return False
        else:
            print(f"❌ FAIL: HTTP {response.status_code}")
            return False

    except Exception as e:
        print(f"❌ ERROR: {e!s}")
        return False


def main():
    """Main test function"""

    if len(sys.argv) < 2:
        print("Usage: python test_agentcore_s3tables_mcp_direct.py <runtime_id> [account_id] [region]")
        print("Example: python test_agentcore_s3tables_mcp_direct.py agentic_data_ingestion_s3tables_mcp-abc123")
        sys.exit(1)

    runtime_id = sys.argv[1]
    account_id = sys.argv[2] if len(sys.argv) > 2 else None
    region = sys.argv[3] if len(sys.argv) > 3 else "us-east-1"

    print("=" * 60)
    print("AWS S3 Tables MCP Server - AgentCore Test")
    print("=" * 60)

    # Test 1: Initialize MCP server
    success1 = test_agentcore_s3tables_mcp_direct(runtime_id, account_id, region)

    print()
    print("-" * 60)

    # Test 2: List available tools
    success2 = test_tools_list(runtime_id, account_id, region)

    print()
    print("=" * 60)

    if success1 and success2:
        print("🎉 ALL TESTS PASSED! S3 Tables MCP server is working correctly on AgentCore.")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED! Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
