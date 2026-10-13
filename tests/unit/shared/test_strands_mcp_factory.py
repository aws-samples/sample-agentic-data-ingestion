# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for strands_mcp_factory (Task 1.5)."""

import os
import sys
from unittest.mock import MagicMock, patch

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set local mode before imports
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


class TestBuildMcpUrl:
    """Tests for _build_mcp_url() URL transformation logic."""

    def _get_build_mcp_url(self):
        from strands_mcp_factory import _build_mcp_url

        return _build_mcp_url

    def test_new_format_runtimes_with_encoded_arn(self):
        """runtimes/ format: URL already has encoded ARN, append qualifier if missing."""
        build = self._get_build_mcp_url()
        endpoint = (
            "https://bedrock-agentcore.us-east-1.amazonaws.com"
            "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012"
            "%3Aruntime%2Fmy-runtime/invocations"
        )
        result = build(endpoint, "us-east-1", "123456789012")
        assert result.startswith(endpoint)
        assert "?qualifier=DEFAULT" in result

    def test_new_format_runtimes_already_has_qualifier(self):
        """runtimes/ format with qualifier already present — don't double-append."""
        build = self._get_build_mcp_url()
        endpoint = (
            "https://bedrock-agentcore.us-east-1.amazonaws.com"
            "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012"
            "%3Aruntime%2Fmy-runtime/invocations?qualifier=PROD"
        )
        result = build(endpoint, "us-east-1", "123456789012")
        assert result == endpoint
        assert result.count("?qualifier=") == 1

    def test_old_format_runtime_constructs_arn_url(self):
        """runtime/ format: extract runtime ID, build ARN-based URL."""
        build = self._get_build_mcp_url()
        endpoint = "https://example.com/runtime/my-runtime-id"
        result = build(endpoint, "us-west-2", "111222333444")

        assert "bedrock-agentcore.us-west-2.amazonaws.com" in result
        assert "/runtimes/" in result
        assert "?qualifier=DEFAULT" in result
        # Verify the ARN is encoded in the URL
        assert "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A111222333444%3Aruntime%2Fmy-runtime-id" in result

    def test_fallback_direct_url_returned_as_is(self):
        """Unrecognized format: return the endpoint unchanged."""
        build = self._get_build_mcp_url()
        endpoint = "https://custom-mcp-server.example.com/v1/mcp"
        result = build(endpoint, "eu-west-1", "999888777666")
        assert result == endpoint


class TestInitializeStrandsAgentWithMcp:
    """Tests for initialize_strands_agent_with_mcp()."""

    def test_returns_none_none_when_endpoint_not_set(self):
        """When AGENTCORE_ENDPOINT is empty, should return (None, None)."""
        with patch.dict(
            os.environ,
            {
                "AGENTCORE_ENDPOINT": "",
                "AGENTIC_DATA_INGESTION_ENV": "local",
            },
            clear=False,
        ):
            # Mock Strands imports so they succeed
            mock_agent = MagicMock()
            mock_bedrock = MagicMock()
            mock_mcp_client = MagicMock()

            with patch.dict(
                "sys.modules",
                {
                    "strands": MagicMock(Agent=mock_agent),
                    "strands.models": MagicMock(),
                    "strands.models.bedrock": MagicMock(BedrockModel=mock_bedrock),
                    "strands.tools": MagicMock(),
                    "strands.tools.mcp": MagicMock(MCPClient=mock_mcp_client),
                },
            ):
                from strands_mcp_factory import initialize_strands_agent_with_mcp

                agent, client = initialize_strands_agent_with_mcp("test prompt")
                assert agent is None
                assert client is None

    def test_returns_none_none_when_strands_import_fails(self):
        """When Strands libraries are not installed, should return (None, None)."""

        import strands_mcp_factory

        with patch.dict(
            os.environ,
            {
                "AGENTCORE_ENDPOINT": "https://example.com/runtime/test",
                "AGENTIC_DATA_INGESTION_ENV": "local",
            },
            clear=False,
        ):
            # Remove strands modules so import fails inside the function
            modules_to_remove = [k for k in list(sys.modules) if k.startswith("strands.") or k == "strands"]
            saved = {k: sys.modules.pop(k) for k in modules_to_remove if k in sys.modules}

            import builtins

            original_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                # Only block the strands SDK packages, not strands_mcp_factory
                if name == "strands" or name.startswith("strands."):
                    raise ImportError(f"No module named '{name}'")
                return original_import(name, *args, **kwargs)

            try:
                with patch("builtins.__import__", side_effect=fake_import):
                    agent, client = strands_mcp_factory.initialize_strands_agent_with_mcp("test prompt")
                    assert agent is None
                    assert client is None
            finally:
                sys.modules.update(saved)


class TestCreateMcpTransport:
    """Tests for _create_mcp_transport() SigV4 vs basic transport selection."""

    def test_sigv4_transport_used_when_available(self):
        """When SIGV4_AVAILABLE is True, should use streamablehttp_client_with_sigv4."""

        import strands_mcp_factory

        mock_sigv4_client = MagicMock(return_value="sigv4_transport")
        mock_credentials = MagicMock()
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = mock_credentials

        original_flag = strands_mcp_factory.SIGV4_AVAILABLE
        original_fn = getattr(strands_mcp_factory, "streamablehttp_client_with_sigv4", None)
        try:
            strands_mcp_factory.SIGV4_AVAILABLE = True
            strands_mcp_factory.streamablehttp_client_with_sigv4 = mock_sigv4_client

            with patch("strands_mcp_factory.boto3.Session", return_value=mock_session):
                result = strands_mcp_factory._create_mcp_transport("https://example.com/mcp", "us-east-1", 120)

            mock_sigv4_client.assert_called_once_with(
                url="https://example.com/mcp",
                credentials=mock_credentials,
                service="bedrock-agentcore",
                region="us-east-1",
                timeout=120,
                terminate_on_close=False,
            )
            assert result == "sigv4_transport"
        finally:
            strands_mcp_factory.SIGV4_AVAILABLE = original_flag
            if original_fn is not None:
                strands_mcp_factory.streamablehttp_client_with_sigv4 = original_fn

    def test_basic_transport_used_when_sigv4_unavailable(self):
        """When SIGV4_AVAILABLE is False, should fall back to basic streamablehttp_client."""
        import strands_mcp_factory

        mock_basic_client = MagicMock(return_value="basic_transport")

        original_flag = strands_mcp_factory.SIGV4_AVAILABLE
        try:
            strands_mcp_factory.SIGV4_AVAILABLE = False

            with patch.dict(
                "sys.modules",
                {
                    "mcp": MagicMock(),
                    "mcp.client": MagicMock(),
                    "mcp.client.streamable_http": MagicMock(streamablehttp_client=mock_basic_client),
                },
            ):
                result = strands_mcp_factory._create_mcp_transport("https://example.com/mcp", "us-west-2", 60)

            mock_basic_client.assert_called_once_with(url="https://example.com/mcp", timeout=60.0)
            assert result == "basic_transport"
        finally:
            strands_mcp_factory.SIGV4_AVAILABLE = original_flag
