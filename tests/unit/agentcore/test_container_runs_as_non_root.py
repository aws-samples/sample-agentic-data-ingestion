# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every container image drops root before its CMD runs.

Checkov's ``CKV_DOCKER_3`` ("ensure that a user for the container has been
created") flagged the nine ``agentcore/*/Dockerfile`` images: they declared no
``USER``, so each ran its entrypoint as uid 0. The five ``mcp/*/Dockerfile``
images already created a non-root user, but nothing asserted it, so that posture
could have regressed silently at any time.

This module pins the property for **all** container images, discovered from the
tree rather than listed, so a new Dockerfile is covered the moment it is added
rather than when someone remembers to extend a constant.

Three things are checked per image, because ``USER`` alone is not sufficient:

1. A ``USER`` instruction exists and does not name root.
2. The user is actually **created** in the image (``useradd``/``adduser``).
   ``USER someone`` referencing a nonexistent account makes the container fail
   to start, which is a worse outcome than running as root.
3. Nothing writes into the working tree **after** the switch. A ``COPY`` or
   ``RUN`` below ``USER`` lands as the unprivileged user and fails on a
   root-owned directory, so ordering is part of the property, not a style
   preference.

Static assertions only: these parse the Dockerfiles and build nothing. The
functional proof that an image still starts as non-root is a Docker build, which
is out of scope for a unit test — see ``docs/TESTING.md``.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# tests/unit/agentcore/<this file> -> agentcore -> unit -> tests -> project root.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Every container image in the tree, discovered rather than enumerated.
DOCKERFILES: tuple[pathlib.Path, ...] = tuple(
    sorted(PROJECT_ROOT.glob("agentcore/*/Dockerfile")) + sorted(PROJECT_ROOT.glob("mcp/*/Dockerfile"))
)

#: Names that are root under another spelling. ``USER 0`` is the numeric form.
_ROOT_NAMES = frozenset({"root", "0"})

_USER_RE = re.compile(r"^USER\s+(?P<user>\S+)", re.MULTILINE)
_CREATES_USER_RE = re.compile(r"\b(?:useradd|adduser)\b")
#: Instructions that write into the image filesystem.
_WRITING_INSTRUCTIONS = ("COPY", "ADD", "RUN")


def _ids(paths: tuple[pathlib.Path, ...]) -> list[str]:
    """Readable parametrize ids: ``agentcore/create_code`` rather than a path."""
    return [str(p.relative_to(PROJECT_ROOT).parent) for p in paths]


def test_the_discovery_is_not_vacuous():
    """A glob that matches nothing would make every check below pass silently."""
    assert len(DOCKERFILES) >= 14, (
        f"Expected at least 14 container images (9 agentcore + 5 mcp); found {len(DOCKERFILES)}. "
        "If images moved, fix the globs before trusting this module."
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_ids(DOCKERFILES))
def test_image_declares_a_non_root_user(dockerfile: pathlib.Path):
    """CKV_DOCKER_3: a USER instruction exists and does not name root."""
    rel = dockerfile.relative_to(PROJECT_ROOT)
    users = _USER_RE.findall(dockerfile.read_text(encoding="utf-8"))

    assert users, (
        f"{rel} declares no USER, so its CMD runs as uid 0. Add a non-root user "
        "before the entrypoint (see any mcp/*/Dockerfile for the pattern)."
    )
    # The last USER wins at runtime; earlier ones only scope intermediate steps.
    assert users[-1].lower() not in _ROOT_NAMES, f"{rel} switches back to {users[-1]!r} before CMD, which is root."


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_ids(DOCKERFILES))
def test_the_user_it_switches_to_is_actually_created(dockerfile: pathlib.Path):
    """A USER naming an account the image never creates cannot start."""
    rel = dockerfile.relative_to(PROJECT_ROOT)
    text = dockerfile.read_text(encoding="utf-8")
    users = _USER_RE.findall(text)
    if not users:
        pytest.skip("covered by test_image_declares_a_non_root_user")

    assert _CREATES_USER_RE.search(text), (
        f"{rel} switches to USER {users[-1]!r} but never runs useradd/adduser. "
        "Docker does not create the account implicitly, so the container would "
        "fail to start — a harder failure than running as root."
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_ids(DOCKERFILES))
def test_nothing_writes_to_the_image_after_dropping_root(dockerfile: pathlib.Path):
    """Ordering is part of the property, not a style preference.

    A COPY or RUN below the USER switch executes unprivileged against a
    root-owned tree. It either fails the build or silently produces files the
    runtime user cannot read.
    """
    rel = dockerfile.relative_to(PROJECT_ROOT)
    lines = dockerfile.read_text(encoding="utf-8").splitlines()

    switch_at = next(
        (i for i, line in enumerate(lines) if line.startswith("USER ") and line.split()[1].lower() not in _ROOT_NAMES),
        None,
    )
    if switch_at is None:
        pytest.skip("covered by test_image_declares_a_non_root_user")

    offenders = [
        f"  :{i + 1}: {line.strip()[:90]}"
        for i, line in enumerate(lines[switch_at + 1 :], start=switch_at + 1)
        if line.startswith(_WRITING_INSTRUCTIONS)
    ]
    assert not offenders, (
        f"{rel} writes to the image after switching to a non-root user at line {switch_at + 1}:\n"
        + "\n".join(offenders)
        + "\n\nMove these above the USER instruction, or extend the chown to cover what they write."
    )
