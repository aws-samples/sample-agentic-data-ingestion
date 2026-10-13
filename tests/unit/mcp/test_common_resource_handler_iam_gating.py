# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CommonResourceHandler IAM-mutation registration gating (P28 R16.2).

The infra server instantiates ``CommonResourceHandler`` with
``register_iam_mutation_tools=False`` so ``add_inline_policy`` and
``create_data_processing_role`` never appear in its tool registry — IAM
changes stay a CDK/deploy-time concern, unreachable by agents that process
user-supplied data. Read-only IAM tools and the S3 tools remain registered.

These tests exercise the vendored handler from the single vendored copy under
mcp/vendor/ (P28 R19.7 — the former four per-server copies were consolidated
in task 15.4; TestSingleVendoredCopy below guards against re-duplication)
with a fake MCP registrar.
"""

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
VENDOR_DIR = MCP_DIR / "vendor" / "awslabs_aws_dataprocessing_mcp_server"

HANDLER_RELPATH = "awslabs/aws_dataprocessing_mcp_server/handlers/commons/common_resource_handler.py"

IAM_MUTATION_TOOLS = {"add_inline_policy", "create_data_processing_role"}
READ_ONLY_IAM_TOOLS = {"get_policies_for_role", "get_roles_for_service"}
S3_TOOLS = {"list_s3_buckets", "upload_to_s3", "analyze_s3_usage_for_data_processing"}


class FakeMCP:
    """Minimal FastMCP stand-in recording registered tool names."""

    def __init__(self):
        self.registered: list[str] = []

    def tool(self, name: str):
        self.registered.append(name)

        def decorator(fn):
            return fn

        return decorator


@pytest.fixture(scope="module")
def common_resource_handler_cls(request):
    """Import CommonResourceHandler from the single vendored copy."""
    sys.path.insert(0, str(VENDOR_DIR))
    # Drop any previously imported awslabs modules (stale interpreter state)
    for mod_name in [m for m in sys.modules if m.startswith("awslabs")]:
        del sys.modules[mod_name]
    from awslabs.aws_dataprocessing_mcp_server.handlers.commons.common_resource_handler import (
        CommonResourceHandler,
    )

    def cleanup():
        sys.path.remove(str(VENDOR_DIR))
        for mod_name in [m for m in sys.modules if m.startswith("awslabs")]:
            del sys.modules[mod_name]

    request.addfinalizer(cleanup)
    return CommonResourceHandler


@pytest.fixture(autouse=True)
def _no_aws_profile(monkeypatch):
    """Handler __init__ builds boto3 clients; a stale AWS_PROFILE would raise."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class TestIamMutationRegistrationGating:
    def test_gated_off_drops_iam_mutation_tools(self, common_resource_handler_cls):
        """register_iam_mutation_tools=False → mutation tools absent, read tools present."""
        fake = FakeMCP()
        common_resource_handler_cls(fake, allow_write=False, register_iam_mutation_tools=False)

        registered = set(fake.registered)
        assert not (registered & IAM_MUTATION_TOOLS), (
            f"IAM-mutation tools leaked into the registry: {registered & IAM_MUTATION_TOOLS}"
        )
        assert registered >= READ_ONLY_IAM_TOOLS
        assert registered >= S3_TOOLS

    def test_gating_holds_even_with_allow_write(self, common_resource_handler_cls):
        """Registration gating is independent of the runtime allow_write flag."""
        fake = FakeMCP()
        common_resource_handler_cls(fake, allow_write=True, register_iam_mutation_tools=False)

        assert not (set(fake.registered) & IAM_MUTATION_TOOLS)

    def test_default_registers_iam_mutation_tools(self, common_resource_handler_cls):
        """Default (True) keeps upstream behavior — protects the other servers."""
        fake = FakeMCP()
        common_resource_handler_cls(fake, allow_write=False)

        registered = set(fake.registered)
        assert registered >= IAM_MUTATION_TOOLS
        assert registered >= READ_ONLY_IAM_TOOLS
        assert registered >= S3_TOOLS


class TestSingleVendoredCopy:
    """P28 R19.7: exactly ONE vendored awslabs copy, with provenance."""

    def test_vendor_copy_exists_with_provenance(self):
        assert (VENDOR_DIR / HANDLER_RELPATH).is_file(), "vendored copy missing from mcp/vendor/"
        provenance = VENDOR_DIR / "PROVENANCE.md"
        assert provenance.is_file(), "PROVENANCE.md missing next to the vendored copy"
        text = provenance.read_text()
        assert "github.com/awslabs/mcp" in text, "PROVENANCE.md does not name the upstream repo"
        assert "register_iam_mutation_tools" in text, "PROVENANCE.md does not document the local modifications"

    def test_no_per_server_copies(self):
        """The former per-server awslabs trees must not reappear."""
        for server in ("analytics_server", "codegen_server", "data_processing", "infra_server"):
            assert not (MCP_DIR / server / "awslabs").exists(), (
                f"{server} has re-grown its own awslabs copy — the vendored fork "
                "must exist only under mcp/vendor/ (P28 R19.7)"
            )

    def test_iam_gated_servers_pass_flag(self):
        """infra (R16.2) and legacy data_processing both drop IAM mutation tools."""
        for server in ("infra_server", "data_processing"):
            source = (MCP_DIR / server / "server.py").read_text()
            assert "register_iam_mutation_tools=False" in source, (
                f"{server}/server.py does not disable IAM-mutation tool registration"
            )
