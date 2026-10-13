# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R19.6 guard — dual-path single-sourcing and pinned-requirements consistency.

The dual Lambda/container execution path must:

- install its runtime dependencies from ONE canonical pinned manifest
  (``shared/runtime_requirements.txt``): the Lambda layer bundling installs it
  via ``pip install -r``, and every ``agentcore/*/requirements.txt`` pins the
  shared dependencies to the exact manifest versions;
- import shared logic from one source: ``streamable_http_sigv4.py`` lives only
  in ``shared/`` — no vendored copies under ``mcp/`` and no cross-tree
  cp/COPY hacks in the layer bundling command or container Dockerfiles.

These are source-level checks so any drift fails CI, making 19.6's
"consistent" clause executable.

Validates: Requirements 19.6
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "shared" / "runtime_requirements.txt"
AGENTCORE_DIR = REPO_ROOT / "agentcore"
MCP_DIR = REPO_ROOT / "mcp"
COMPUTE_STACK = REPO_ROOT / "deploy-cdk" / "lib" / "compute-stack.ts"

CONTAINERS = sorted(d.name for d in AGENTCORE_DIR.iterdir() if (d / "requirements.txt").is_file())

# name(extras)==version — extras (e.g. strands-agents[otel]) are ignored for
# identity: the version pin is what must match across artifacts.
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?==([A-Za-z0-9.!+*_-]+)$")


def _normalize(name: str) -> str:
    """PEP 503 name normalization (typing_extensions == typing-extensions)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pinned_requirements(path: Path) -> dict[str, str]:
    """Parse a requirements file, asserting every entry is an ``==`` pin."""
    pins: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_RE.match(line)
        assert match, f"{path.relative_to(REPO_ROOT)}: not an exact '==' pin: {raw_line!r}"
        name = _normalize(match.group(1))
        assert name not in pins, f"{path.relative_to(REPO_ROOT)}: duplicate entry {name!r}"
        pins[name] = match.group(3)
    return pins


# ── Canonical manifest ────────────────────────────────────────────────


class TestCanonicalManifest:
    def test_manifest_exists(self):
        assert MANIFEST_PATH.is_file(), "shared/runtime_requirements.txt (canonical dual-path manifest) missing"

    def test_manifest_fully_pinned(self):
        pins = _parse_pinned_requirements(MANIFEST_PATH)
        assert pins, "manifest must not be empty"

    def test_manifest_covers_layer_dependency_set(self):
        """The manifest carries the full former layer install list."""
        pins = _parse_pinned_requirements(MANIFEST_PATH)
        expected = {
            "strands-agents",
            "strands-agents-tools",
            "mcp",
            "jinja2",
            "aws-lambda-powertools",
            "bedrock-agentcore",
            "typing-extensions",
        }
        assert expected <= set(pins), f"manifest missing: {sorted(expected - set(pins))}"


# ── Container requirements consistency ────────────────────────────────


class TestContainerRequirementPins:
    def test_all_nine_containers_present(self):
        assert len(CONTAINERS) == 9, f"expected 9 agentcore containers, found {CONTAINERS}"

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_container_requirements_fully_pinned(self, container):
        _parse_pinned_requirements(AGENTCORE_DIR / container / "requirements.txt")

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_container_shared_pins_match_manifest(self, container):
        """Any dependency shared with the manifest pins the exact manifest version."""
        manifest = _parse_pinned_requirements(MANIFEST_PATH)
        container_pins = _parse_pinned_requirements(AGENTCORE_DIR / container / "requirements.txt")
        mismatches = {
            name: (version, manifest[name])
            for name, version in container_pins.items()
            if name in manifest and version != manifest[name]
        }
        assert not mismatches, (
            f"agentcore/{container}/requirements.txt diverges from "
            f"shared/runtime_requirements.txt (container, manifest): {mismatches}"
        )

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_dockerfile_extra_installs_pinned(self, container):
        """Dockerfile-level pip installs (aws-opentelemetry-distro) are pinned."""
        dockerfile = (AGENTCORE_DIR / container / "Dockerfile").read_text()
        for line in dockerfile.splitlines():
            if "aws-opentelemetry-distro" in line:
                assert re.search(r"aws-opentelemetry-distro==[0-9]", line), (
                    f"agentcore/{container}/Dockerfile installs aws-opentelemetry-distro unpinned: {line.strip()!r}"
                )


# ── Single-source of streamable_http_sigv4 ────────────────────────────


class TestSigV4ModuleSingleSourced:
    def test_module_lives_in_shared(self):
        assert (REPO_ROOT / "shared" / "streamable_http_sigv4.py").is_file(), (
            "shared/streamable_http_sigv4.py missing — the dual-path SigV4 transport must live in shared/"
        )

    def test_no_vendored_copies_under_mcp(self):
        copies = sorted(p.relative_to(REPO_ROOT) for p in MCP_DIR.rglob("streamable_http_sigv4.py"))
        assert not copies, f"vendored streamable_http_sigv4.py copies found: {copies}"

    def test_layer_bundling_has_no_cross_tree_copy_hack(self):
        source = COMPUTE_STACK.read_text()
        assert "mcp/data_processing/streamable_http_sigv4" not in source, (
            "compute-stack.ts still cp's streamable_http_sigv4 from mcp/data_processing — shared/*.py already covers it"
        )

    def test_layer_bundling_installs_canonical_manifest(self):
        source = COMPUTE_STACK.read_text()
        assert "pip install -r /asset-input/shared/runtime_requirements.txt" in source, (
            "StrandsAgentsLayer bundling must install the canonical pinned manifest via -r"
        )

    def test_no_dockerfile_copy_hacks_remain(self):
        dockerfiles = sorted(AGENTCORE_DIR.glob("*/Dockerfile")) + sorted(MCP_DIR.glob("*/Dockerfile"))
        assert dockerfiles, "expected agentcore/ and mcp/ Dockerfiles to exist"
        offenders = [
            str(path.relative_to(REPO_ROOT)) for path in dockerfiles if "streamable_http_sigv4" in path.read_text()
        ]
        assert not offenders, f"Dockerfiles still copy streamable_http_sigv4 per-artifact: {offenders}"

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_containers_get_module_via_shared_copy(self, container):
        """Every container image bundles shared/ (which now carries the module)."""
        dockerfile = (AGENTCORE_DIR / container / "Dockerfile").read_text()
        assert "COPY shared /app/shared" in dockerfile, f"agentcore/{container}/Dockerfile must COPY shared /app/shared"
