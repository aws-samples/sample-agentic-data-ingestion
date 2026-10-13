# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for ``SharedBaseLayer``'s manifest filtering.

WHAT THIS FILE GUARDS, AND WHY IT IS NOT COVERED ANYWHERE ELSE
-------------------------------------------------------------
``schema_extract`` no longer carries ``strands-agents-layer``. It carries
``shared-base-layer``: the same ``shared/runtime_requirements.txt``, the same
``shared/*.py`` copy and the same templates, with the agent stack
(``AGENT_ONLY_LAYER_PACKAGES``) filtered out of the pip install. That returns
124,583,653 bytes / 118.81 MiB to a function that had 1,322,903 bytes — 0.5% —
of the 262,144,000-byte unzipped ceiling left.

Three vitest files own what the *template* shows: the exact layer list and order
(``p46-schema-extract-layers.test.ts``), attachment across the stack
(``p47-strands-layer-attachment.test.ts``), and the byte budget
(``p55-schema-extract-layer-budget.test.ts``).

**None of them can see any of this.** Bundling is a synth-time build step; the
rendered template carries only an asset hash, so the pip command, the grep
pattern and the three ``cp`` lines are invisible to a ``Template.fromStack``
assertion. And they are exactly where the two failure modes live:

1. **The filter stops matching.** Rename a pin in the manifest — or edit the
   anchoring in the generated regex — and ``grep -v`` removes nothing. The base
   layer becomes a byte-for-byte copy of the strands layer, 124 MB lands back on
   ``schema_extract``, and the only symptom is a deploy-time ``Unzipped size must
   be smaller than 262144000 bytes``. Every assertion in every vitest file still
   passes, because the *attachment* is still correct.
2. **The filter matches too much.** Add a name to ``AGENT_ONLY_LAYER_PACKAGES``
   that a base-layer function actually imports, and the result is an
   ``ImportError`` at cold start in a deployed Lambda. ``LGR-bb8073d-1``
   (**template / ratified**) is explicit about why nothing below CDK observes
   that: unit tests import ``shared/`` from the repo, where it is a real sibling
   directory, so "detaching the layer is an ``ImportError`` at cold start" with
   "every existing test" green.

The bundling command **does** fail closed on (1) on its own — it re-greps the
manifest and exits non-zero if nothing matched — but that only fires when Docker
bundling runs, which the vitest suites deliberately disable and which a
contributor without Docker never reaches. This file catches both at test time.

THE FENCE THIS DISCHARGES
-------------------------
``LGR-bb8073d-1`` set two conditions on any layer split, and both are asserted
below rather than merely honoured:

* it "MUST install from ``shared/runtime_requirements.txt`` rather than repeat
  ``PowertoolsTriggerLayer``'s unpinned ``aws-lambda-powertools``" —
  ``TestOneManifest``;
* ``LGR-0739ab8-1``'s Layer_Budget must be "**re-derived by measurement** … in
  the same change" — discharged in
  ``deploy-cdk/test/p55-schema-extract-layer-budget.test.ts``, which carries the
  measurement and its cross-check.

``LGR-87b8538-1``'s rule that "a literal in two files is how the next move
manufactures a misleading failure" is why the package list is **parsed out of
``compute-stack.ts``** below instead of re-spelled here. The TypeScript const is
the single home: the bundling command builds its grep pattern from it, and this
file reads the same declaration. A rename therefore cannot make the test and the
build disagree — it can only make them both wrong in the same, visible way,
which the manifest-intersection assertions then catch.

WHY SOURCE TEXT, AND THE RESIDUAL THAT LEAVES
---------------------------------------------
``LGR-1166391-1`` (**template / ratified**) records that ``tests/cdk/`` cannot
render a template at all — ``aws-cdk-lib`` is absent from ``pyproject.toml`` and
every file here asserts on ``.ts`` text. That is the *reason* this layer exists,
and here it is not a compromise: the bundling command is source-only by nature,
so text is the only instrument that can see it in any suite. The residual that
record names — a source scan can pass on input that still fails at deploy —
applies to the grep pattern, which is assembled from a ``.join('|')`` rather than
written as one literal. So the pattern is reconstructed below the same way the
bundling reconstructs it and applied to the real manifest file, which is a
behavioural check on the filter rather than a spelling check on the string.

Validates: the Layer_Budget split (LGR-bb8073d-1 revisit path)
"""

import os
import re

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COMPUTE_STACK_PATH = os.path.join(_ROOT, "deploy-cdk", "lib", "compute-stack.ts")
RUNTIME_REQUIREMENTS_PATH = os.path.join(_ROOT, "shared", "runtime_requirements.txt")

#: The manifest every layer in this stack must install from. Named once here so
#: the assertions below read as one fact rather than four string literals.
CANONICAL_MANIFEST = "shared/runtime_requirements.txt"

#: Packages the base layer MUST still deliver, and why each is not optional.
#:
#: ``aws-lambda-powertools`` — `LGR-bb8073d-1`'s audit found **all 22** attached
#: Compute Lambdas import it, so it is the one dependency whose removal breaks
#: every function at once.
#: ``jinja2`` — `shared/prompt_template_loader.py` needs it to read the
#: ``prompt_templates/`` directory this layer copies in. A layer shipping the
#: templates without the engine is a trap for the second adopter.
#: ``openpyxl`` — p55's Excel_Workbook reader, imported directly by
#: ``schema_extract``, which is the only function on this layer today.
REQUIRED_BASE_PACKAGES = frozenset({"aws-lambda-powertools", "jinja2", "openpyxl"})


@pytest.fixture(scope="module")
def compute_stack_content():
    with open(COMPUTE_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def manifest_lines():
    with open(RUNTIME_REQUIREMENTS_PATH) as f:
        return f.read().splitlines()


def _agent_only_packages(content: str) -> list[str]:
    """The names in ``AGENT_ONLY_LAYER_PACKAGES``, parsed from the declaration.

    Parsed rather than re-spelled (``LGR-87b8538-1``): the TypeScript const is the
    only home for this list, and the bundling command generates its grep pattern
    from the same declaration.
    """
    match = re.search(
        r"export const AGENT_ONLY_LAYER_PACKAGES = \[(.*?)\];",
        content,
        re.DOTALL,
    )
    assert match, (
        "AGENT_ONLY_LAYER_PACKAGES not found in compute-stack.ts. It is the single home for the "
        "set of packages SharedBaseLayer excludes; the bundling command builds its grep pattern "
        "from it. If it was renamed, update this parser in the same change."
    )
    return re.findall(r"'([^']+)'", match.group(1))


def _requirement_name(line: str) -> str | None:
    """The package name on a requirements line, or None for a comment/blank."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return re.split(r"[=<>!~;\[\s]", stripped, maxsplit=1)[0]


def _excluded_by_filter(line: str, packages: list[str]) -> bool:
    """Whether the bundling's ``grep -vE`` would drop this manifest line.

    Reconstructs the pattern the way ``compute-stack.ts`` builds it —
    ``^(a|b|c)([=<>!~ ]|$)`` over ``packages`` — so this is a behavioural check on
    the filter rather than a spelling check on the generated string.
    """
    pattern = rf"^({'|'.join(packages)})([=<>!~ ]|$)"
    return re.match(pattern, line) is not None


def _shared_base_bundling(content: str) -> str:
    """The ``command:`` array of SharedBaseLayer's bundling, as raw text."""
    match = re.search(
        r"new lambda\.LayerVersion\(this, 'SharedBaseLayer',.*?command: \['bash', '-c', \[(.*?)\]\.join\(' '\)\]",
        content,
        re.DOTALL,
    )
    assert match, (
        "SharedBaseLayer's bundling command not found in compute-stack.ts. Either the construct "
        "id changed (p46 R7.3's do-NOT-rename reasoning applies to this layer too — three test "
        "files key off its layerVersionName) or the bundling shape changed."
    )
    return match.group(1)


# ── The exclusion set is real, and it is exactly the agent stack ────


class TestExclusionSet:
    """What the filter drops, and that it drops something at all."""

    def test_declares_the_four_agent_packages(self, compute_stack_content):
        """The set is the agent stack and nothing else.

        Pinned as an exact set rather than a subset because BOTH directions are a
        defect with a different failure. A missing name means the base layer still
        carries a 124 MB dependency and the split bought nothing. An extra name
        means a base-layer function loses a package it imports, which is an
        ImportError at cold start that no test below CDK can observe
        (``LGR-bb8073d-1``).
        """
        assert set(_agent_only_packages(compute_stack_content)) == {
            "strands-agents",
            "strands-agents-tools",
            "bedrock-agentcore",
            "mcp",
        }, (
            "AGENT_ONLY_LAYER_PACKAGES has changed. Adding a name removes that package from "
            "every function on the base layer — enumerate the transitive import closure first, "
            "including through shared/*.py, and record what you checked. Removing a name puts "
            "its bytes back on schema_extract, which had 0.5% of the unzipped ceiling free "
            "before the split."
        )

    def test_every_excluded_name_is_actually_in_the_manifest(self, compute_stack_content, manifest_lines):
        """FAILURE MODE 1: a filter that matches nothing.

        This is the assertion the whole file is worth writing for. ``grep -v``
        against a name that is no longer pinned removes nothing and reports
        success, so the base layer silently becomes a byte-for-byte copy of the
        strands layer. The attachment is still correct, the template is still
        correct, every vitest assertion still passes — and the deploy fails with
        ``Unzipped size must be smaller than 262144000 bytes``.

        The bundling command guards this too (it re-greps and exits non-zero if
        nothing matched), but that only fires when Docker bundling actually runs.
        """
        packages = _agent_only_packages(compute_stack_content)
        pinned = {name for name in map(_requirement_name, manifest_lines) if name}

        missing = sorted(set(packages) - pinned)
        assert not missing, (
            f"these names in AGENT_ONLY_LAYER_PACKAGES are not pinned in {CANONICAL_MANIFEST}: "
            f"{missing}. The filter therefore has nothing to remove for them. If a package was "
            "renamed upstream, rename it here in the same change; if it was dropped from the "
            "manifest, drop it here too. A no-op filter produces a base layer identical to the "
            "strands layer, and the only symptom is a deploy-time unzipped-size refusal."
        )

    def test_the_filter_removes_the_agent_stack_and_keeps_the_rest(self, compute_stack_content, manifest_lines):
        """Apply the reconstructed pattern to the real manifest and check both sides."""
        packages = _agent_only_packages(compute_stack_content)

        kept: list[str] = []
        dropped: list[str] = []
        for line in manifest_lines:
            name = _requirement_name(line)
            if name is None:
                continue
            (dropped if _excluded_by_filter(line, packages) else kept).append(name)

        assert set(dropped) == set(packages), (
            f"the filter drops {sorted(set(dropped))} but AGENT_ONLY_LAYER_PACKAGES names "
            f"{sorted(set(packages))}. The anchoring is what usually breaks this: the tail "
            "`([=<>!~ ]|$)` is what stops `mcp` matching a future `mcp-something` and what makes "
            "`strands-agents-tools` need its own entry rather than being swallowed by "
            "`strands-agents`."
        )

        assert REQUIRED_BASE_PACKAGES <= set(kept), (
            f"the base layer would not install {sorted(REQUIRED_BASE_PACKAGES - set(kept))}. See "
            "REQUIRED_BASE_PACKAGES above for why each of these is load-bearing — all 22 Compute "
            "Lambdas import aws-lambda-powertools, jinja2 reads the prompt_templates this layer "
            "copies in, and openpyxl is schema_extract's Excel reader."
        )

    def test_the_filter_is_not_a_no_op(self, compute_stack_content, manifest_lines):
        """A floor, so the assertion above cannot pass vacuously on an empty list.

        If ``AGENT_ONLY_LAYER_PACKAGES`` were emptied, the set-equality check above
        would compare ``set() == set()`` and pass while the layer had silently
        stopped being a base layer.
        """
        packages = _agent_only_packages(compute_stack_content)
        assert len(packages) >= 1, "AGENT_ONLY_LAYER_PACKAGES is empty — the split has been undone"

        dropped = [
            line for line in manifest_lines
            if _requirement_name(line) and _excluded_by_filter(line, packages)
        ]
        assert dropped, (
            f"the filter removes no line from {CANONICAL_MANIFEST}. The base layer is a "
            "byte-for-byte copy of the strands layer and returns none of the 118.81 MiB the "
            "split exists to return."
        )


# ── One manifest, not two — LGR-bb8073d-1's explicit requirement ────


class TestOneManifest:
    """``LGR-bb8073d-1``: a split "MUST install from shared/runtime_requirements.txt"."""

    def test_base_layer_installs_from_the_canonical_manifest(self, compute_stack_content):
        bundling = _shared_base_bundling(compute_stack_content)
        assert f"/asset-input/{CANONICAL_MANIFEST}" in bundling, (
            f"SharedBaseLayer's bundling does not read /asset-input/{CANONICAL_MANIFEST}. "
            "`LGR-bb8073d-1` requires a split to install from that file rather than repeat "
            "PowertoolsTriggerLayer's unpinned aws-lambda-powertools, so every shared version "
            "has exactly one home."
        )

    def test_no_second_pinned_manifest(self, compute_stack_content):
        """*Rejected: a new ``shared/base_requirements.txt``.*

        It puts every shared version in two files that must agree, which is the
        drift ``LGR-87b8538-1`` refuses. Filtering one manifest has one authority
        for every version and one authority for the exclusion.

        Scoped to ``-r`` arguments, so the PyArrow layer's ``pip install
        "pyarrow>=14.0.0"`` — a single deliberately-unpinned wheel with its own
        recorded reasoning — is not caught by this.
        """
        referenced = set(re.findall(r"pip install -r ([^\s']+)", compute_stack_content))
        # /tmp/base_requirements.txt is the FILTERED OUTPUT of the canonical
        # manifest, generated inside the build container and never committed. It
        # is not a second authority: nothing can edit it.
        unexpected = {
            path for path in referenced
            if not path.endswith(CANONICAL_MANIFEST) and path != "/tmp/base_requirements.txt"
        }
        assert not unexpected, (
            f"compute-stack.ts installs from a manifest other than {CANONICAL_MANIFEST}: "
            f"{sorted(unexpected)}. If a second pinned list is genuinely needed, it needs a "
            "consistency test asserting it agrees with the canonical manifest version for "
            "version — otherwise the two drift and the failure is a version skew between a "
            "Lambda layer and an AgentCore container."
        )


# ── The three copies whose absence is an ImportError ────────────────


class TestSharedContentIsCopied:
    """The base layer must deliver everything the strands layer delivers, minus wheels."""

    @pytest.mark.parametrize(
        ("fragment", "why"),
        [
            (
                "/asset-input/shared/*.py",
                "all 35 shared/*.py modules — /opt/python is their ONLY runtime source. The "
                "<fn>/../shared entry every handler appends to sys.path does not exist in a "
                "deployed Lambda (LGR-bb8073d-1), so a missing copy is an ImportError at cold "
                "start with every unit test still green.",
            ),
            (
                "/asset-input/shared/prompt_templates",
                "the P28 R19.8 contract: templates ship at /opt/python/prompt_templates, which "
                "is prompt_template_loader's first search path. Without it any Lambda-side "
                "load_prompt_template call dies with TemplateNotFound.",
            ),
            (
                "/asset-input/pyspark_templates",
                "parity with the strands layer. schema_extract does not read these, but a layer "
                "that is a drop-in for one function and not for the next is a trap for the "
                "second adopter.",
            ),
        ],
    )
    def test_bundling_copies(self, compute_stack_content, fragment, why):
        bundling = _shared_base_bundling(compute_stack_content)
        assert fragment in bundling, (
            f"SharedBaseLayer's bundling does not copy {fragment}. Why it matters: {why}"
        )

    def test_the_filtered_manifest_is_written_outside_the_layer(self, compute_stack_content):
        """The filtered file must not land in ``/asset-output``.

        A stray requirements file under ``/asset-output/python`` ships to
        ``/opt/python`` and changes the layer's bytes — small, but it also means the
        budget figures in ``p55-schema-extract-layer-budget.test.ts`` describe a
        layer that is not quite the one deployed.
        """
        bundling = _shared_base_bundling(compute_stack_content)
        assert "> /tmp/base_requirements.txt" in bundling, (
            "SharedBaseLayer's filtered manifest is not written to /tmp. Writing it under "
            "/asset-output would ship it to /opt/python and put the deployed layer's bytes out "
            "of step with the recorded budget."
        )
        assert "> /asset-output" not in bundling, (
            "SharedBaseLayer redirects output into /asset-output — that becomes part of the "
            "shipped layer"
        )
