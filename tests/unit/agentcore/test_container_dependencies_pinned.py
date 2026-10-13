# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Container images build from pinned inputs: base images by digest, Python deps by ``==``.

A tag such as ``python:3.12-slim`` is a moving pointer, so two builds of the
same commit can start from different base layers. An open range such as
``mcp>=1.0.0`` resolves to whatever PyPI has newest at build time: when this
test was added, the MCP servers' ranges resolved to mcp 2.3.0 and fastmcp
4.0.11, major versions the servers had never run against.

For every Dockerfile and requirements file under ``agentcore/`` and ``mcp/``
(discovered by glob, so a new image is covered as soon as it exists):

1. Every external ``FROM`` names its image by ``@sha256:<64 hex>`` digest.
   Pin the multi-arch index digest, not a single-platform manifest, so the
   ``linux/arm64`` platform the CDK asks for is still selected from it.
2. Every ``FROM`` of the same ``image:tag`` carries the same digest, so a bump
   cannot leave some of the images on the old base.
3. Every requirement is an exact ``==`` pin. Option lines (``-r``, ``-c``,
   ``--…``) and environment markers are allowed; ``-e``/``--editable`` is not,
   because it installs an unpinned source tree.

Static checks only. Whether a digest exists and lists linux/arm64 is a registry
fact, checked when pinning with ``docker buildx imagetools inspect <ref>``.
The cross-file agreement of shared agentcore pins with
``shared/runtime_requirements.txt`` is ``test_runtime_requirements_pins.py``'s job.
"""

from __future__ import annotations

import pathlib
import re
from collections import defaultdict

import pytest

# tests/unit/agentcore/<this file> -> agentcore -> unit -> tests -> project root.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]

DOCKERFILES: tuple[pathlib.Path, ...] = tuple(
    sorted(PROJECT_ROOT.glob("agentcore/*/Dockerfile")) + sorted(PROJECT_ROOT.glob("mcp/*/Dockerfile"))
)
REQUIREMENTS: tuple[pathlib.Path, ...] = tuple(
    sorted(PROJECT_ROOT.glob("agentcore/*/requirements.txt")) + sorted(PROJECT_ROOT.glob("mcp/*/requirements.txt"))
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# PEP 508 name, optional extras, then ``==`` and a version with no wildcard,
# so ``==1.*`` (a range) and ``===1.0`` (arbitrary equality) are both refused.
_EXACT_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*(?:\[[A-Za-z0-9._,\s-]+\])?"
    r"\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)"
    r"(?P<rest>.*)$"
)
# What may follow the version: nothing, an environment marker, or hashes.
_PIN_TAIL_RE = re.compile(r"^(?:\s*;.*|(?:\s+--hash=\S+)*)$")
_ALLOWED_OPTION_RE = re.compile(r"^(?:-[rc]\s+\S|--(?!editable\b)[a-z])")


def _ids(paths: tuple[pathlib.Path, ...]) -> list[str]:
    """Readable parametrize ids: ``mcp/codegen_server`` rather than a path."""
    return [str(p.relative_to(PROJECT_ROOT).parent) for p in paths]


def _from_images(dockerfile: pathlib.Path) -> list[tuple[int, str]]:
    """``(line number, image reference)`` for every FROM that pulls an external image.

    Skips ``scratch`` and references to an earlier build stage (``FROM builder``
    after ``... AS builder``), which have no registry digest to pin.
    """
    images: list[tuple[int, str]] = []
    stages: set[str] = set()
    for number, raw in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), start=1):
        tokens = raw.split()
        if not tokens or tokens[0].upper() != "FROM":
            continue
        args = [t for t in tokens[1:] if not t.startswith("--")]
        assert args, f"{dockerfile.relative_to(PROJECT_ROOT)}:{number}: FROM names no image: {raw!r}"
        image = args[0]
        if image.lower() != "scratch" and image.lower() not in stages:
            images.append((number, image))
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return images


def _requirement_entries(path: pathlib.Path) -> list[tuple[int, str]]:
    """``(line number, text)`` for every line pip would read, comments removed."""
    entries: list[tuple[int, str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # pip treats '#' as a comment at line start or after whitespace.
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if line:
            entries.append((number, line))
    return entries


def test_the_discovery_is_not_vacuous():
    """A glob that matches nothing would make every check below pass silently."""
    assert len(DOCKERFILES) >= 14, f"expected at least 14 Dockerfiles (9 agentcore + 5 mcp), found {len(DOCKERFILES)}"
    assert len(REQUIREMENTS) >= 14, f"expected at least 14 requirements files, found {len(REQUIREMENTS)}"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_ids(DOCKERFILES))
def test_every_base_image_is_pinned_by_digest(dockerfile: pathlib.Path):
    rel = dockerfile.relative_to(PROJECT_ROOT)
    images = _from_images(dockerfile)
    assert images, f"{rel} has no FROM line that pulls an image"

    unpinned = [
        f"  :{number}: {image}"
        for number, image in images
        if "@" not in image or not _DIGEST_RE.match(image.rsplit("@", 1)[1])
    ]
    assert not unpinned, (
        f"{rel} pulls a base image by tag only:\n"
        + "\n".join(unpinned)
        + "\n\nAppend the index digest, keeping the tag: image:tag@sha256:<digest>. "
        "Read it from the 'Digest:' line of `docker buildx imagetools inspect image:tag`, "
        "and check that the index lists linux/arm64."
    )


def test_one_tag_resolves_to_one_digest_across_images():
    """Bumping the base in some Dockerfiles and not others is caught here."""
    digests: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for dockerfile in DOCKERFILES:
        for number, image in _from_images(dockerfile):
            if "@" not in image:
                continue  # reported by test_every_base_image_is_pinned_by_digest
            name, digest = image.rsplit("@", 1)
            digests[name][digest].append(f"{dockerfile.relative_to(PROJECT_ROOT)}:{number}")

    split = {name: dict(by_digest) for name, by_digest in digests.items() if len(by_digest) > 1}
    assert not split, f"the same image:tag is pinned to different digests: {split}"


@pytest.mark.parametrize("requirements", REQUIREMENTS, ids=_ids(REQUIREMENTS))
def test_every_requirement_is_an_exact_pin(requirements: pathlib.Path):
    rel = requirements.relative_to(PROJECT_ROOT)
    entries = _requirement_entries(requirements)
    assert entries, f"{rel} lists no requirements"

    offenders = []
    for number, line in entries:
        if line.startswith("-"):
            if not _ALLOWED_OPTION_RE.match(line):
                offenders.append(f"  :{number}: {line}  (option not allowed)")
            continue
        match = _EXACT_PIN_RE.match(line)
        if not match or not _PIN_TAIL_RE.match(match.group("rest")):
            offenders.append(f"  :{number}: {line}")
    assert not offenders, f"{rel} has requirements that are not exact '==' pins:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("line", "is_pin"),
    [
        ("mcp==1.28.1", True),
        ("strands-agents[otel]==1.48.0", True),
        ("python-dateutil==2.9.0.post0", True),
        ('pywin32==306 ; sys_platform == "win32"', True),
        ("mcp>=1.0.0", False),
        ("mcp~=1.28", False),
        ("mcp==1.*", False),
        ("mcp===1.28.1", False),
        ("mcp", False),
        ("mcp==1.28.1,<2", False),
    ],
)
def test_the_pin_pattern_accepts_exact_pins_only(line: str, is_pin: bool):
    """The pattern itself, so a loosened regex cannot turn the file checks into no-ops."""
    match = _EXACT_PIN_RE.match(line)
    assert bool(match and _PIN_TAIL_RE.match(match.group("rest"))) is is_pin
