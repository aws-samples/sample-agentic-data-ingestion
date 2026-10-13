# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the shadow row's writer and its reader spell one step_name.

Two Lambdas have to agree on one string and neither can import the other:

- The **writer**, ``lambda/agentic_data_ingestion_test_code/lambda_function.py``,
  declares ``SHADOW_VERIFICATION_STEP_NAME`` and puts it on the
  ``(run_id, "shadow_verification")`` record row.
- The **reader**,
  ``lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py``,
  names the same string in ``get_run_details``'s ``_synthetic_steps`` denylist so
  the row is filtered out of the run's step tree.

They are separate Lambda bundles with separate ``sys.path`` roots and their own
module-level boto3 clients, so neither can import the other's constant — which
is why the reader carries a literal rather than a shared constant. There is also
no shared home for the name: ``shared/step_names.py`` is the canonical
step-name module and its docstring lists record-row names (``pipeline_failure``,
``data_quality``, ``cache``) as "Deliberately NOT members", and
``test_shadow_orchestration.py`` asserts ``from_any`` **raises** on this name.
Promoting it to a ``StepName`` would break that guarantee, not fix this one.

WHY A TEST INSTEAD OF A CONSTANT
-------------------------------
The drift this closes is cross-bundle and silent. If either side is renamed
alone, nothing errors: the writer keeps writing, the reader keeps filtering a
name nothing writes, and the row reappears in the run-detail tree as a trailing
⚙️ step with an unknown status. That is the exact live regression
``load_metrics`` caused before p37, and ``LGR-9e76249-9`` (**template /
ratified**) predicted this class of failure when it recorded that the reader
denylist must be extended "by hand" for every new record type.

``LGR-28b4d75-5`` (**template / ratified**) is the precedent for the technique —
a contract test derives its expectation from the other side's **source**,
because "a test written from the producer's output cannot detect the producer
drifting, the output is its own oracle". Same shape as this directory's
``test_approval_window_cross_language.py`` (``LGR-69ca78b-2``) and
``test_not_verified_reason_labels_cross_language.py``: read the source, extract
with a regex, assert.

The parse is deliberately source-text and not an import. Importing
``test_code.lambda_function`` needs that Lambda's full environment and would
construct its module-level DynamoDB resources inside a contract test.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on nothing found. Both files must
exist and both extractions must be non-empty, each failing with a message that
names what was being looked for, so a moved file or a reformatted declaration
fails loudly instead of vacuously.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

WRITER_SOURCE = _REPO_ROOT / "lambda" / "agentic_data_ingestion_test_code" / "lambda_function.py"
READER_SOURCE = _REPO_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines_runs.py"

# ``SHADOW_VERIFICATION_STEP_NAME: Final = "shadow_verification"`` — tolerant of
# the annotation being dropped or the quote style changing, strict about the
# constant's name.
_WRITER_CONSTANT = re.compile(
    r"^SHADOW_VERIFICATION_STEP_NAME\s*(?::\s*[^=]+)?=\s*['\"](?P<value>[^'\"]+)['\"]",
    re.MULTILINE,
)

# The ``_synthetic_steps`` set literal in ``get_run_details``, from its opening
# brace to the closing one.
_READER_DENYLIST = re.compile(r"_synthetic_steps\s*=\s*\{(?P<body>.*?)\}", re.DOTALL)

_QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")


def _read(path: Path, description: str) -> str:
    assert path.is_file(), f"{description} not found at {path} — has the file moved or been renamed?"
    return path.read_text(encoding="utf-8")


def _writer_step_name() -> str:
    source = _read(WRITER_SOURCE, "shadow row writer")
    match = _WRITER_CONSTANT.search(source)
    assert match is not None, (
        f"No top-level SHADOW_VERIFICATION_STEP_NAME assignment found in {WRITER_SOURCE.name}. "
        "The writer's constant is this test's oracle; if it was renamed, rename it here too."
    )
    value = match.group("value")
    assert value, f"SHADOW_VERIFICATION_STEP_NAME in {WRITER_SOURCE.name} is empty"
    return value


def _reader_denylist_literals() -> set[str]:
    source = _read(READER_SOURCE, "run-detail reader")
    match = _READER_DENYLIST.search(source)
    assert match is not None, (
        f"No `_synthetic_steps = {{...}}` set literal found in {READER_SOURCE.name}. "
        "If the denylist changed shape, this test needs to follow it."
    )
    # Strip comments before collecting quoted strings: the block is heavily
    # commented and those comments name step names in prose.
    body = "\n".join(line.split("#", 1)[0] for line in match.group("body").splitlines())
    literals = set(_QUOTED.findall(body))
    assert literals, (
        f"The `_synthetic_steps` literal in {READER_SOURCE.name} yielded no quoted names — "
        "the parse found the block but nothing in it."
    )
    return literals


class TestSourcesAreReadable:
    """Guard 1 — a moved file fails loudly rather than skipping."""

    @pytest.mark.parametrize(
        ("path", "description"),
        [(WRITER_SOURCE, "shadow row writer"), (READER_SOURCE, "run-detail reader")],
    )
    def test_source_is_readable(self, path: Path, description: str):
        assert path.is_file(), f"{description} not found at {path}"


class TestExtractionsAreNonEmpty:
    """Guard 2 — neither side may contribute an empty set to the comparison."""

    def test_writer_constant_is_found(self):
        assert _writer_step_name() == "shadow_verification"

    def test_reader_denylist_is_found(self):
        literals = _reader_denylist_literals()

        # The four pre-existing literals, asserted so a parse that silently
        # captured only part of the block cannot pass.
        assert {"pipeline_failure", "code_generation", "infrastructure", "first_run"} <= literals


class TestTheTwoSpellingsAgree:
    """The contract itself."""

    def test_the_writers_step_name_is_in_the_readers_denylist(self):
        """``LGR-9e76249-9``'s by-hand obligation, enforced.

        Without this the row reaches the run-detail step tree as a trailing step
        with an unknown status the moment shadow mode is enabled.
        """
        writer_name = _writer_step_name()

        assert writer_name in _reader_denylist_literals(), (
            f"test_code writes state rows named {writer_name!r}, but the dashboard "
            f"run-detail denylist `_synthetic_steps` does not filter that name. The row "
            f"will render as a trailing step with an unknown status. Add {writer_name!r} to "
            f"`_synthetic_steps` in {READER_SOURCE.name}."
        )
