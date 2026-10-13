# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.7 — the merge_key sink's loud failure, characterized before W4.

The companion to `tests/unit/shared/test_identifier_loud_failure_characterization.py`.
That file characterizes the validator; this one characterizes the **sink** —
`_common_write_arguments`, where a configured merge_key leaves pipeline config
and enters the Glue job's DefaultArguments.

Expectations derive from the sink's own documented contract, **not from current
output**. `job_builders.py:342-354` (`_common_write_arguments`, read in full;
the spec cites `:341-352` and the figure has drifted by one to two lines — the
docstring opens at `:342`, the `Raises:` block closes at `:354`, and the prose
is otherwise verbatim as quoted) states P28 R12.1 in as many words:

    ``--merge_key`` is read back by the rendered job at runtime and
    f-string'd into the ``spark.sql`` MERGE INTO statement, so the value is
    validated against the Identifier_Allowlist BEFORE it enters
    DefaultArguments. An invalid merge_key fails job creation loudly, naming
    the offending identifier (R12.2) — it is never sanitized or defaulted
    away. (The rendered job re-validates the runtime argument at the SQL sink
    itself, guarding manual job starts that bypass this builder.)

Three expectations follow from that block, and each is asserted below:

1. the refusal happens **here**, before the value reaches DefaultArguments —
   so the failure is observable at this function, not downstream;
2. the failure **names the offending identifier**;
3. the value is **never sanitized or defaulted away** — so the only two
   outcomes are the input back verbatim, or a raise. A default (`"id"`) in
   place of a bad value would be the specific failure this block forbids.

Why this file exists even though `test_merge_key_identifier_validation.py`
already covers this sink: its hostile catalog is six injection payloads —
quotes, semicolons, newlines, comment tokens. **W4's actual input class, a
Non_Conforming_Column_Name (a space, a hyphen, a leading digit), appears
nowhere in it.** W4 sanitizes that class upstream and hands this sink a name
that passes; the behaviour that must survive unchanged is that an *unsanitized*
one still fails here. That is the assertion below. Its sink-context assertion
is also a bare `"merge_key" in str(exc)`, which would still hold if the context
were reduced to the word `merge_key` and stopped identifying which of the two
merge_key sinks refused — this file pins the discrimination instead.

Do NOTs honoured: 1, 2 and 3 — `job_builders.py` and `identifier_utils.py` are
read, never modified.

**Validates: Requirements 12.1, 12.2**
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)
sys.path.insert(0, LAMBDA_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")

from identifier_utils import IdentifierValidationError

# Heavy/optional dependencies mocked at import time — same set and same
# standalone-load recipe as test_merge_key_identifier_validation.py, under a
# distinct module alias so the two files do not clobber each other in
# ``sys.modules``.
_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock"),
    ),
}


def _load(mod_name: str, alias: str):
    """Load a create_glue_pipeline module standalone with mocked heavy deps."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(alias, mod_path, submodule_search_locations=[LAMBDA_DIR])
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
    return mod


_jb = _load("job_builders", "p55_67_job_builders")


# The Non_Conforming_Column_Name class per the requirements glossary: "a source
# column name that fails the Identifier_Allowlist — for example one containing
# a space, a hyphen, or a leading digit". W4 renames these upstream; an
# unsanitized one reaching this sink must still fail.
NON_CONFORMING_MERGE_KEYS = [
    pytest.param("first name", id="embedded-space"),
    pytest.param("first-name", id="hyphen"),
    pytest.param("2024-total", id="leading-digit-and-hyphen"),
    pytest.param("1st_place", id="leading-digit"),
    pytest.param("customer id", id="space-between-words"),
    pytest.param("order.date", id="dot-qualified"),
    pytest.param("  customer_id  ", id="surrounding-whitespace"),
    pytest.param("", id="empty-string"),
]

CONFORMING_MERGE_KEYS = [
    pytest.param("customer_id", id="snake-case"),
    pytest.param("CustomerId", id="camel-case"),
    pytest.param("_internal", id="leading-underscore"),
    pytest.param("col2", id="trailing-digit"),
    pytest.param("k" * 255, id="exactly-255-chars"),
]


class TestNonConformingMergeKeyFailsLoudlyAtThisSink:
    """R12.1 at the `--merge_key` sink.

    **Validates: Requirements 12.1**
    """

    @pytest.mark.parametrize("merge_key", NON_CONFORMING_MERGE_KEYS)
    def test_refusal_happens_before_the_value_enters_default_arguments(self, merge_key):
        """The documented ordering: validated BEFORE it enters DefaultArguments.

        Asserted as "this call raises rather than returning a dict", which is
        the only observable form the ordering takes at this seam — a returned
        dict means the value got through.
        """
        with pytest.raises(IdentifierValidationError):
            _jb._common_write_arguments({"write_mode": "merge", "merge_key": merge_key})

    @pytest.mark.parametrize("merge_key", NON_CONFORMING_MERGE_KEYS)
    def test_failure_names_the_offending_identifier_and_the_sink(self, merge_key):
        """Both halves of R12.1, at the production sink.

        `.name` is asserted by identity: "naming the offending identifier"
        means *that* identifier, not a normalised rendering of it.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            _jb._common_write_arguments({"write_mode": "merge", "merge_key": merge_key})
        err = excinfo.value

        assert err.name is merge_key

        # The sink context, whatever this call site passes, reaches both the
        # exception attribute and the message verbatim.
        assert isinstance(err.context, str) and err.context
        assert err.context in str(err)

    @pytest.mark.parametrize("merge_key", NON_CONFORMING_MERGE_KEYS)
    def test_sink_context_identifies_this_sink_specifically(self, merge_key):
        """Which sink refused must be readable from the message alone.

        Two sinks validate a merge_key: this one, whose value becomes the
        `--merge_key` Glue job argument, and `code_assembly`'s, whose value is
        rendered into the job at code-generation time. The docstring describes
        this sink as `--merge_key` entering DefaultArguments and being
        f-string'd into the `spark.sql` MERGE INTO statement, so the context
        must say at least that much — a bare `merge_key` does not distinguish
        the two, and a log line that cannot distinguish them cannot tell an
        operator whether job creation or code generation failed.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            _jb._common_write_arguments({"write_mode": "merge", "merge_key": merge_key})
        context = excinfo.value.context

        assert "merge_key" in context
        assert "--merge_key" in context, "the context must name the Glue job argument this sink writes"
        assert "MERGE" in context, "the context must name the statement the value reaches"

    @pytest.mark.parametrize("merge_key", NON_CONFORMING_MERGE_KEYS)
    def test_never_sanitized_and_never_defaulted_away(self, merge_key):
        """do-NOT 1, executable: no repair, and specifically no fallback to "id".

        The default `"id"` exists for an **absent** merge_key. The docstring
        forbids reaching it for an invalid one — "never sanitized or defaulted
        away" — so a returned `{"--merge_key": "id"}` here would be the exact
        failure this task guards.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            _jb._common_write_arguments({"write_mode": "merge", "merge_key": merge_key})
        # Nothing normalised the value on the way to the raise.
        assert excinfo.value.name is merge_key


class TestConformingMergeKeyReachesTheArgumentUnmodified:
    """R12.2 at the `--merge_key` sink.

    **Validates: Requirements 12.2**
    """

    @pytest.mark.parametrize("merge_key", CONFORMING_MERGE_KEYS)
    def test_conforming_merge_key_is_the_same_object_in_the_arguments(self, merge_key):
        """W4's contract with this sink: sanitize upstream, pass through here.

        W4-D1 renames upstream and hands this sink a name that already
        satisfies the allowlist. Identity rather than equality, because
        "returns its input unmodified" is what lets the validator sit inline;
        an implementation that returned a normalised copy would satisfy `==`
        for these inputs while breaking that property.
        """
        args = _jb._common_write_arguments({"write_mode": "merge", "merge_key": merge_key})
        assert args["--merge_key"] is merge_key

    @pytest.mark.parametrize("merge_key", CONFORMING_MERGE_KEYS)
    def test_write_mode_is_carried_through_beside_the_merge_key(self, merge_key):
        """The negative control for the pass-through: nothing else is rewritten.

        Without this, the assertion above could pass for a sink that returned
        the merge_key and dropped or defaulted the write mode.
        """
        args = _jb._common_write_arguments({"write_mode": "append", "merge_key": merge_key})
        assert args == {"--write_mode": "append", "--merge_key": merge_key}
