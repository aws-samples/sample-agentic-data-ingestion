# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p53 task 2.2 — the Sample_Artifact's bounds and parseability, EXECUTED.

**Validates: Requirements 1.1, 1.2, 1.3**

WHAT IS UNDER TEST, AND WHY IT IS NOT AN IMPORT
-----------------------------------------------
Task 2.1 added an 86-line block to ``pyspark_templates/glue_job_jdbc_source.py.j2``
that collects at most 100 rows off the DataFrame the JDBC job has already read,
coerces each field to something JSON can carry, drops WHOLE rows from the end
until the serialised array is under 1 MiB, and writes one ``.json`` object. That
block is *emitted Python inside a Jinja template*, so there is nothing to import
and 2.1 could only assert on the rendered text. **This file is the first
execution of the bounding and coercion logic**, so "it renders" is treated here
as no evidence at all.

HOW THE EMITTED LOGIC IS REACHED — an existing precedent, not a new technique
-----------------------------------------------------------------------------
``test_merge_key_runtime_guard.py`` renders the real template, slices the guard
block out of the rendered output and ``exec``s it against hostile and benign
``args`` values. This file follows that shape verbatim: render → slice the
``# === p53 C1:`` … ``# === End p53 C1 ===`` span → ``textwrap.dedent`` → ``exec``
in a namespace holding fakes for the five names the block reads (``df``,
``args``, ``json``, ``boto3``, ``logger``). The captured ``put_object`` kwargs
*are* the Sample_Artifact, so every assertion below is against bytes the
generated job would really have written.

The extracted block is asserted byte-identical across the ``read_mode`` ×
``target_type`` matrix before any of it is run, so the thing executed is *the*
block rather than one variant of it.

WHY THERE IS NO SPARK AND NO REAL S3 CLIENT
-------------------------------------------
The fake ``df`` exposes exactly ``.limit(n).collect()`` returning objects with
``.asDict(recursive=True)`` — the whole of the DataFrame surface this block
touches — and ``limit`` slices, as Spark's does. A Spark session would add
minutes and verify nothing more.

For the reader side, ``s3_client`` is patched on each privately-loaded consumer
module with a stand-in whose ``read(n)`` truncates at ``n`` bytes, which is
``StreamingBody.read(n)``'s real behaviour and the whole subject of BC10. That
is the in-area precedent: ``test_rule_contradiction_check.py:1129`` samples a
body this way and ``:1225``'s ``test_the_body_read_is_byte_bounded`` asserts the
ceiling this way. A moto bucket would exercise the same one ``get_object`` at
much higher cost, and ``mock_s3_buckets``' fixed bucket list does not include
the artifact bucket anyway.

MODULE LOADING — four private copies, none published to sys.modules
------------------------------------------------------------------
Property 1's claim is that *each consumer's own parser* accepts the artifact, so
the real functions are loaded rather than reimplemented. Three of the four live
in files named ``lambda_function.py`` — a name 23 Lambda dirs share — so a plain
``import lambda_function`` would bind whichever copy an earlier test module put
in ``sys.modules``. ``LGR-c58f276-4`` (**template / ratified**) records exactly
that defect class and ratifies the remedy used here: load each file with
``importlib.util.spec_from_file_location`` under a private alias that is never
published, and assert at import time that what was bound is neither a mock nor
vacuous.

Two deliberate departures from the ``_load_private`` helper that record cites:

* stubs are inserted and then removed **key by key**, not via
  ``patch.dict("sys.modules", …)``. ``patch.dict``'s restore clears
  ``sys.modules`` and re-applies its entry snapshot, which evicts anything first
  imported inside the block and hands the next importer a second, distinct copy
  — a cost this file has no reason to impose on the rest of the layer.
* ``sys.path`` entries are added for the load and removed after, so no lambda
  directory is left on the path to shadow a sibling module's import.

Only two stubs are genuinely needed — ``strands`` and ``bedrock_agentcore`` are
not installed — plus ``env_utils``, because ``require_env`` raises for an unset
variable unless ``AGENTIC_DATA_INGESTION_ENV=local`` was set *before*
``env_utils`` was first imported, which this file cannot guarantee in a shared
process. Every other dependency resolves to the real module in ``shared/``.

Nothing here changes production code and nothing here amends a pre-existing
assertion.
"""

from __future__ import annotations

import datetime
import decimal
import importlib.util
import json
import math
import os
import re
import sys
import textwrap
from typing import Any
from unittest.mock import MagicMock, patch

import jinja2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
TEMPLATES_DIR = os.path.join(_REPO, "pyspark_templates")
_SHARED = os.path.join(_REPO, "shared")
_CREATE_CODE = os.path.join(_REPO, "lambda", "agentic_data_ingestion_create_code")
_SCHEMA_MAPPING = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_mapping")
_TEST_CODE = os.path.join(_REPO, "lambda", "agentic_data_ingestion_test_code")
_AGENT = os.path.join(_REPO, "agentcore", "complex_mapping_agent")

TEMPLATE_NAME = "glue_job_jdbc_source.py.j2"

#: The block's own delimiters, emitted by the template as plain comments.
_BLOCK_OPEN = "# === p53 C1:"
_BLOCK_CLOSE = "# === End p53 C1 ==="

#: A real Secrets Manager ARN shape — segments 3 and 4 are what the block reads
#: to build ``adi-processed-files-<account>-<region>``.
_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:adi/jdbc/src-AbCdEf"
_PIPELINE_ID = "latesttesting-19ca826a"
_EXPECTED_BUCKET = "adi-processed-files-123456789012-us-west-2"
_EXPECTED_KEY = f"samples/{_PIPELINE_ID}/source-sample.json"
_EXPECTED_URI = f"s3://{_EXPECTED_BUCKET}/{_EXPECTED_KEY}"


# ---------------------------------------------------------------------------
# Private module loading (see the docstring's "MODULE LOADING" section)
# ---------------------------------------------------------------------------

_MISSING = object()


class _ScopedStubs:
    """Insert stub modules for a load, then remove exactly what was inserted."""

    def __init__(self, stubs: dict[str, Any]) -> None:
        self._stubs = stubs
        self._prior: dict[str, Any] = {}

    def __enter__(self) -> _ScopedStubs:
        for name, module in self._stubs.items():
            self._prior[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc: object) -> bool:
        for name, stub in self._stubs.items():
            prior = self._prior[name]
            if prior is _MISSING:
                if sys.modules.get(name) is stub:
                    del sys.modules[name]
            else:
                sys.modules[name] = prior
        return False


def _load_private(alias: str, path: str, stubs: dict[str, Any], search_paths: list[str]):
    """Exec *path* under *alias* without publishing it to ``sys.modules``."""
    added = [p for p in search_paths if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        assert spec is not None and spec.loader is not None, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        with _ScopedStubs(stubs):
            spec.loader.exec_module(module)
        return module
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


_ENV_UTILS_STUB = MagicMock(
    require_env=lambda name, default=None: default or name,
    get_aws_account_id=lambda: "123456789012",
)


def _agentcore_stubs() -> dict[str, Any]:
    runtime = MagicMock()
    runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
    package = MagicMock()
    package.runtime = runtime
    return {
        "bedrock_agentcore": package,
        "bedrock_agentcore.runtime": runtime,
        "env_utils": _ENV_UTILS_STUB,
    }


#: Consumer 1 — the p49 date-format check's reader.
CREATE_CODE = _load_private(
    "_p53_create_code_lambda_function",
    os.path.join(_CREATE_CODE, "lambda_function.py"),
    {"strands": MagicMock(), "env_utils": _ENV_UTILS_STUB},
    [_SHARED, _CREATE_CODE],
)
#: Consumer 2 — the mapping prompt's near-duplicate reader.
SCHEMA_MAPPING = _load_private(
    "_p53_schema_mapping_lambda_function",
    os.path.join(_SCHEMA_MAPPING, "lambda_function.py"),
    {"env_utils": _ENV_UTILS_STUB},
    [_SHARED, _SCHEMA_MAPPING],
)
#: Consumer 4 — the ancestor both of the above descend from.
AGENT = _load_private(
    "_p53_complex_mapping_agent_main",
    os.path.join(_AGENT, "main.py"),
    _agentcore_stubs(),
    [_SHARED, _AGENT],
)
#: Consumer 3 — P44's Sample_Frame format resolution.
GLUE_SESSION_RUNNER = _load_private(
    "_p53_glue_session_runner",
    os.path.join(_TEST_CODE, "glue_session_runner.py"),
    {},
    [_SHARED, _TEST_CODE],
)

# Import-time non-vacuity guards, per LGR-c58f276-4: fail loudly at collection
# if what was bound is a mock or is missing the function under test, rather than
# passing vacuously in every property below.
for _label, _module, _attrs in (
    ("create_code", CREATE_CODE, ("_sample_rows_from_json", "read_source_samples", "_MAX_SAMPLE_BYTES")),
    ("schema_mapping", SCHEMA_MAPPING, ("_sample_rows_from_json", "read_source_samples", "_MAX_SAMPLE_BYTES")),
    ("complex_mapping_agent", AGENT, ("_parse_json_data", "read_sample_data", "_MAX_SAMPLE_ROWS")),
    ("glue_session_runner", GLUE_SESSION_RUNNER, ("sample_read_format", "_SAMPLE_READERS")),
):
    assert not isinstance(_module, MagicMock), f"{_label} resolved to a mock, not the real module"
    for _attr in _attrs:
        assert hasattr(_module, _attr), f"the real {_label} has no {_attr}; the private load was defeated"

# `hasattr` alone is not a guard — a MagicMock has every attribute — so each
# loaded function is additionally asked a question with a known answer.
# `LGR-c58f276-4` records why this second assertion is the load-bearing one: bound
# to a mock, its "never raises" property *passed vacuously*, so a partial version
# of that bug reads as a healthy suite.
_PROBE_BODY = '[{"probe": "1"}, {"probe": "2"}]'
for _label, _parser in (
    ("create_code._sample_rows_from_json", CREATE_CODE._sample_rows_from_json),
    ("schema_mapping._sample_rows_from_json", SCHEMA_MAPPING._sample_rows_from_json),
    ("complex_mapping_agent._parse_json_data", AGENT._parse_json_data),
):
    assert _parser(_PROBE_BODY, 100) == [{"probe": "1"}, {"probe": "2"}], (
        f"{_label} does not parse a two-row JSON array; it is not the real parser and every "
        f"parseability property below would be vacuous"
    )
assert GLUE_SESSION_RUNNER.sample_read_format("s3://b/samples/p/source-sample.json") == "json", (
    "the loaded sample_read_format does not resolve a .json key; it is probably not glue_session_runner.py"
)
assert CREATE_CODE._MAX_SAMPLE_BYTES == SCHEMA_MAPPING._MAX_SAMPLE_BYTES == 1_048_576, (
    "a reader's byte ceiling moved; the cross-language pins at the bottom of this file are the subject"
)


#: The three real parsers Property 1 asserts against, each named by its consumer.
CONSUMER_PARSERS = [
    pytest.param(lambda body, cap: CREATE_CODE._sample_rows_from_json(body, cap), id="create_code"),
    pytest.param(lambda body, cap: SCHEMA_MAPPING._sample_rows_from_json(body, cap), id="schema_mapping"),
    pytest.param(lambda body, cap: AGENT._parse_json_data(body, cap), id="complex_mapping_agent"),
]


# ---------------------------------------------------------------------------
# Render the template and slice the emitted block out of it
# ---------------------------------------------------------------------------


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(*, target_type: str = "S3_TABLES", read_mode: str = "full_table") -> str:
    """The canonical variable surface ``test_glue_job_jdbc_source.py:97`` uses."""
    return (
        _env()
        .get_template(TEMPLATE_NAME)
        .render(
            engine="postgres",
            target_type=target_type,
            read_mode=read_mode,
            write_mode="overwrite",
            partition_columns="",
            merge_key="id",
            initial_watermark="",
            transform_block="df_transformed = df",
        )
    )


def _extract_block(rendered: str) -> str:
    """Slice the p53 C1 span out of *rendered* and dedent it to column 0.

    The block sits at one indent level inside ``main()``, opened and closed by
    its own comment delimiters. Dedenting the whole span is what makes it
    executable on its own; nothing inside is rewritten.
    """
    lines = rendered.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(_BLOCK_OPEN))
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == _BLOCK_CLOSE)
    return textwrap.dedent("\n".join(lines[start : end + 1]))


_RENDER_MATRIX = [
    pytest.param(target, mode, id=f"{target.lower()}-{mode}")
    for target in ("S3_TABLES", "GLUE_CATALOG", "JDBC", "S3_PARQUET")
    for mode in ("full_table", "incremental")
]

#: The block executed by every test below, compiled once.
SAMPLE_BLOCK_SOURCE = _extract_block(_render())
SAMPLE_BLOCK = compile(SAMPLE_BLOCK_SOURCE, f"<{TEMPLATE_NAME}:p53-C1>", "exec")


# ---------------------------------------------------------------------------
# Fakes: the whole of the surface the emitted block touches
# ---------------------------------------------------------------------------


class _FakeRow:
    """A Spark ``Row``, to the extent the block uses one."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def asDict(self, recursive: bool = False) -> dict[str, Any]:
        return dict(self._values)


class _FakeDataFrame:
    """``.limit(n).collect()`` and nothing else. ``limit`` slices, as Spark's does."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def limit(self, num: int) -> _FakeDataFrame:
        return _FakeDataFrame(self._rows[:num])

    def collect(self) -> list[_FakeRow]:
        return [_FakeRow(row) for row in self._rows]


class _RecordingS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ETag": '"stub"'}


class _RecordingBoto3:
    def __init__(self, s3: _RecordingS3) -> None:
        self._s3 = s3

    def client(self, service: str) -> _RecordingS3:
        assert service == "s3", f"the sample write must only ever build an s3 client, not {service!r}"
        return self._s3


class _Logger:
    """``logger.info`` / ``logger.warning``, recorded."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: object) -> None:
        self.infos.append(str(message))

    def warning(self, message: object) -> None:
        self.warnings.append(str(message))


class SampleWrite:
    """One execution of the emitted block, and everything observable about it."""

    def __init__(self, calls: list[dict[str, Any]], logger: _Logger, namespace: dict[str, Any]) -> None:
        self.calls = calls
        self.logger = logger
        self.namespace = namespace

    @property
    def wrote(self) -> bool:
        return bool(self.calls)

    @property
    def payload(self) -> bytes:
        assert len(self.calls) == 1, f"expected exactly one put_object, got {len(self.calls)}"
        return self.calls[0]["Body"]

    @property
    def body(self) -> str:
        return self.payload.decode("utf-8")

    @property
    def rows(self) -> list[Any]:
        return json.loads(self.body)


def run_sample_block(
    rows: list[dict[str, Any]],
    *,
    secret_arn: str = _SECRET_ARN,
    pipeline_id: str = _PIPELINE_ID,
) -> SampleWrite:
    """Execute the emitted p53 C1 block over *rows* and capture what it wrote."""
    s3 = _RecordingS3()
    logger = _Logger()
    namespace: dict[str, Any] = {
        "args": {"secret_arn": secret_arn, "pipeline_id": pipeline_id},
        "df": _FakeDataFrame(rows),
        "json": json,
        "boto3": _RecordingBoto3(s3),
        "logger": logger,
    }
    # exec is deliberate: this runs the generated job's own emitted block.
    exec(SAMPLE_BLOCK, namespace)
    return SampleWrite(s3.calls, logger, namespace)


# ---------------------------------------------------------------------------
# Reader-side harness: the real readers over the real artifact bytes
# ---------------------------------------------------------------------------


class _ByteFaithfulStream:
    """``StreamingBody``'s ``read(n)`` — truncates at *n* bytes. BC10's subject."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, amt: int | None = None) -> bytes:
        return self._payload if amt is None else self._payload[:amt]


def _s3_returning(payload: bytes) -> MagicMock:
    return MagicMock(get_object=MagicMock(return_value={"Body": _ByteFaithfulStream(payload)}))


def read_through_consumer(module: Any, attribute: str, function_name: str, payload: bytes) -> dict[str, list[str]]:
    """Run a consumer's real end-to-end reader over *payload* at the real URI."""
    with patch.object(module, attribute, _s3_returning(payload)):
        return getattr(module, function_name)(_EXPECTED_URI)


#: The two ``read_source_samples`` readers plus the ancestor, end to end.
CONSUMER_READERS = [
    pytest.param(CREATE_CODE, "s3_client", "read_source_samples", id="create_code"),
    pytest.param(SCHEMA_MAPPING, "s3_client", "read_source_samples", id="schema_mapping"),
    pytest.param(AGENT, "_s3_client", "read_sample_data", id="complex_mapping_agent"),
]


# ---------------------------------------------------------------------------
# Generators — constrained to what a Spark row can actually carry
# ---------------------------------------------------------------------------

_COLUMN_NAMES = st.sampled_from(
    [
        "order_id",
        "order date",  # a space — not a valid Python identifier
        "2nd_amount",  # leading digit
        "Id",  # differs from "id" only in case
        "id",
        "naïve_column",
        "",  # the degenerate column name
        "col.with.dots",
    ]
)

#: Every value class a JDBC read can put in a row: the ones JSON carries
#: natively, and the ones the block has to coerce.
_CELL_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=True, allow_infinity=True, width=64),
    st.text(max_size=30),
    st.dates(),
    st.datetimes(),
    st.decimals(allow_nan=False, allow_infinity=False, places=2, max_value=10**9, min_value=-(10**9)),
    st.binary(max_size=8),
    st.dictionaries(st.text(max_size=6), st.integers(), max_size=3),
    st.lists(st.integers(), max_size=3),
)

_ROW = st.dictionaries(_COLUMN_NAMES, _CELL_VALUES, min_size=1, max_size=5)
_ROW_SET = st.lists(_ROW, max_size=8)


# ---------------------------------------------------------------------------
# The block executed is THE block — asserted before anything runs it
# ---------------------------------------------------------------------------


class TestTheExtractedBlockIsTheOneThatShips:
    """The span executed below is identical across every render configuration.

    Without this, every property in the file could be measuring one variant of
    the block while the other seven shipped something else.
    """

    @pytest.mark.parametrize("target_type,read_mode", _RENDER_MATRIX)
    def test_the_block_is_byte_identical_across_the_render_matrix(self, target_type: str, read_mode: str) -> None:
        assert _extract_block(_render(target_type=target_type, read_mode=read_mode)) == SAMPLE_BLOCK_SOURCE, (
            f"the p53 C1 block differs when rendered for {target_type}/{read_mode}. It sits before the "
            f"transform and after all three source-read arms converge, so it must not vary by target or "
            f"read mode — and every property in this file executes only the S3_TABLES/full_table copy."
        )

    def test_the_block_is_a_single_put_object_inside_a_try(self) -> None:
        """Cheap structural floor: what is exec'd is the guarded write, whole."""
        assert SAMPLE_BLOCK_SOURCE.count("put_object(") == 1
        assert SAMPLE_BLOCK_SOURCE.lstrip().startswith("#")
        assert "\ntry:\n" in SAMPLE_BLOCK_SOURCE
        assert "except Exception" in SAMPLE_BLOCK_SOURCE


# ---------------------------------------------------------------------------
# Property 1: the artifact is always parseable by every consumer
# ---------------------------------------------------------------------------


class TestProperty1ParseableByEveryConsumer:
    """*For any* set of source rows, the artifact parses under all four readers.

    Each consumer has its own parser, so this asserts against all of them rather
    than against ``json.loads`` alone. The functions are the real ones, loaded
    privately — never reimplemented here.

    **Validates: Requirements 1.2, 1.3**
    """

    @settings(max_examples=60)
    @given(rows=_ROW_SET)
    def test_the_artifact_is_a_json_array_every_parser_accepts(self, rows: list[dict[str, Any]]) -> None:
        write = run_sample_block(rows)

        assert write.wrote, "the block must always write, even for an empty row set"
        parsed = json.loads(write.body)  # never raises — the first half of Property 1
        assert isinstance(parsed, list), "the Sample_Frame reader is multiLine=True, which needs an ARRAY"
        assert all(isinstance(row, dict) for row in parsed)

        cap = CREATE_CODE._MAX_SAMPLE_ROWS
        for parser in (
            CREATE_CODE._sample_rows_from_json,
            SCHEMA_MAPPING._sample_rows_from_json,
            AGENT._parse_json_data,
        ):
            rows_out = parser(write.body, cap)
            assert isinstance(rows_out, list)
            assert len(rows_out) == len(parsed), (
                f"{parser.__qualname__} recovered {len(rows_out)} of {len(parsed)} rows — a parser "
                f"disagreeing with the array is the 'unparseable body' failure BC10 names"
            )

    @settings(max_examples=60)
    @given(rows=_ROW_SET)
    def test_the_array_form_is_what_p44s_multiline_reader_needs(self, rows: list[dict[str, Any]]) -> None:
        """The design's format table rules out JSONL on P44's ``multiLine=True``.

        Spark cannot run here, so the two facts that make its read work are
        asserted instead: the body is one JSON array rather than newline-delimited
        objects, and the key resolves a format through the real
        ``sample_read_format`` rather than returning ``None``.
        """
        write = run_sample_block(rows)

        assert write.body.startswith("["), f"body does not open an array: {write.body[:40]!r}"
        assert len(write.body.splitlines()) == 1, "a multi-line body would read as JSONL, which multiLine=True rejects"
        assert GLUE_SESSION_RUNNER.sample_read_format(_EXPECTED_URI) == "json"
        assert "json" in GLUE_SESSION_RUNNER._SAMPLE_READERS

    @settings(max_examples=60)
    @given(rows=_ROW_SET)
    def test_the_body_is_ascii_so_the_readers_byte_ceiling_cannot_cut_a_character(
        self, rows: list[dict[str, Any]]
    ) -> None:
        """``json.dumps`` defaults to ``ensure_ascii=True``, and that is load-bearing.

        Both ``read_source_samples`` read ``_MAX_SAMPLE_BYTES`` and decode with
        ``errors="replace"``. An all-ASCII body means the byte ceiling and the
        character count cannot diverge, so nothing can be corrupted at the
        boundary even in the worst case. Passing ``ensure_ascii=False`` in the
        template would silently remove that guarantee, which is why it is pinned.
        """
        write = run_sample_block(rows)
        write.body.encode("ascii")  # raises if the template ever stops escaping

    @pytest.mark.parametrize("parser", CONSUMER_PARSERS)
    @pytest.mark.parametrize(
        "label,rows",
        [
            ("empty-result-set", []),
            ("single-row", [{"order_id": 1001, "order_date": "05/12/2024"}]),
            ("one-hundred-rows", [{"order_id": i, "order_date": "05/12/2024"} for i in range(100)]),
            ("unicode-values", [{"city": "München"}, {"city": "东京"}, {"city": "naïve"}]),
            (
                "quotes-newlines-backslashes",
                [{"note": 'he said "hi"'}, {"note": "line1\nline2"}, {"note": "C:\\path\\to"}, {"note": "tab\there"}],
            ),
            ("none-values", [{"a": None, "b": 1}, {"a": None, "b": None}]),
            ("column-name-not-an-identifier", [{"order date": "05/12/2024", "2nd amount": 1, "": "blank"}]),
            ("case-differing-column-names", [{"Id": 1, "id": 2, "ID": 3}]),
        ],
    )
    def test_every_named_shape_parses_under_every_consumer(
        self, label: str, rows: list[dict[str, Any]], parser
    ) -> None:
        write = run_sample_block(rows)
        expected = json.loads(write.body)

        recovered = parser(write.body, CREATE_CODE._MAX_SAMPLE_ROWS)

        assert recovered == expected, f"{label}: parser recovered {recovered!r}, artifact holds {expected!r}"
        assert len(recovered) == len(rows)

    @pytest.mark.parametrize("module,attribute,function_name", CONSUMER_READERS)
    def test_the_end_to_end_readers_return_values_not_an_empty_dict(
        self, module: Any, attribute: str, function_name: str
    ) -> None:
        """R1.3 end to end: the real reader over the real bytes at the real URI.

        This is the assertion BC10 exists for on the reader side —
        ``read_source_samples`` takes at most ``_MAX_SAMPLE_BYTES`` off the
        stream, so an artifact at or over the ceiling arrives truncated
        mid-array and yields ``{}``. Here the artifact is under it, so real
        values come back.
        """
        rows = [{"signup_date": "01/15/2024"}, {"signup_date": "02/20/2024"}, {"signup_date": "02/20/2024"}]
        write = run_sample_block(rows)

        samples = read_through_consumer(module, attribute, function_name, write.payload)

        assert samples == {"signup_date": ["01/15/2024", "02/20/2024"]}, (
            f"{function_name} returned {samples!r} — a JDBC pipeline would still be inert"
        )

    @pytest.mark.parametrize("module,attribute,function_name", CONSUMER_READERS)
    def test_an_empty_artifact_is_inert_rather_than_a_failure(
        self, module: Any, attribute: str, function_name: str
    ) -> None:
        """``[]`` is parseable and honest; R2.3 requires ``{}``, never a raise."""
        write = run_sample_block([])

        assert write.body == "[]"
        assert read_through_consumer(module, attribute, function_name, write.payload) == {}


# ---------------------------------------------------------------------------
# Property 2: the artifact is bounded on both axes
# ---------------------------------------------------------------------------


class TestProperty2BoundedOnBothAxes:
    """*For any* set of source rows: at most 100 rows, and under 1 MiB.

    **Validates: Requirements 1.1, 1.2**
    """

    @settings(max_examples=60)
    @given(rows=_ROW_SET)
    def test_both_bounds_hold_for_any_row_set(self, rows: list[dict[str, Any]]) -> None:
        write = run_sample_block(rows)

        assert len(write.rows) <= CREATE_CODE._MAX_SAMPLE_ROWS
        assert len(write.payload) < CREATE_CODE._MAX_SAMPLE_BYTES

    @pytest.mark.parametrize("supplied", [0, 1, 99, 100, 101, 1000])
    def test_the_row_cap_holds_however_many_rows_the_source_has(self, supplied: int) -> None:
        """R1.1: at most 100, and exactly 100 once the source has that many."""
        write = run_sample_block([{"i": i} for i in range(supplied)])

        assert len(write.rows) == min(supplied, 100)
        assert f"{min(supplied, 100)} rows" in write.logger.infos[0], (
            "the INFO line must report the row count actually written — risk N1 needs "
            "'no sample ever appeared' diagnosable from the job log"
        )

    def test_a_wide_row_set_is_truncated_below_the_ceiling_and_stays_parseable(self) -> None:
        """BC10, exercised: 100 rows the row cap alone would let through at ~3 MiB.

        A mid-array cut here is the exact failure ``create_code``'s docstring
        (``:319-321``) names — "truncates to something unparseable and yields
        ``{}``" — so the truncated body is handed to all three real parsers and
        both end-to-end readers, not merely measured.
        """
        rows = [{"blob": "x" * 30_000, "i": i} for i in range(100)]

        write = run_sample_block(rows)
        parsed = write.rows

        assert len(parsed) < 100, "100 rows of ~30 KiB must have forced byte truncation; nothing was dropped"
        assert len(write.payload) < CREATE_CODE._MAX_SAMPLE_BYTES
        assert len(parsed) > 0, "truncation must degrade the sample, not empty it"
        assert all(isinstance(row, dict) and set(row) == {"blob", "i"} for row in parsed), (
            "a truncated artifact must still hold WHOLE rows"
        )

        cap = CREATE_CODE._MAX_SAMPLE_ROWS
        for parser in (
            CREATE_CODE._sample_rows_from_json,
            SCHEMA_MAPPING._sample_rows_from_json,
            AGENT._parse_json_data,
        ):
            assert parser(write.body, cap) == parsed

        for module, attribute, function_name in (
            (CREATE_CODE, "s3_client", "read_source_samples"),
            (SCHEMA_MAPPING, "s3_client", "read_source_samples"),
            (AGENT, "_s3_client", "read_sample_data"),
        ):
            samples = read_through_consumer(module, attribute, function_name, write.payload)
            assert samples, f"{function_name} went inert on a truncated-but-valid artifact — BC10's failure"
            assert set(samples) == {"blob", "i"}

    def test_truncation_drops_a_suffix_of_whole_rows_and_changes_nothing_else(self) -> None:
        """Row-boundary integrity, without reimplementing the coercion.

        The truncated artifact is compared byte-for-byte with the artifact the
        block produces from just the rows that survived. Equal bodies mean
        truncation removed a suffix of whole rows and left every retained row
        untouched — the property a mid-array cut would break.
        """
        rows = [{"blob": "y" * 30_000, "i": i} for i in range(100)]

        truncated = run_sample_block(rows)
        kept = len(truncated.rows)
        reference = run_sample_block(rows[:kept])

        assert kept < 100
        assert truncated.payload == reference.payload
        assert [row["i"] for row in truncated.rows] == list(range(kept)), (
            "rows must be dropped from the END, so the retained prefix is the source's own order"
        )

    def test_one_row_larger_than_the_ceiling_yields_an_empty_array(self) -> None:
        """The degenerate case: bounded, parseable and honest, rather than a crash.

        ``while _adi_smp_rows and …`` stops at the empty list, so a single
        oversized row leaves ``[]`` — no sample, but nothing downstream breaks.
        """
        write = run_sample_block([{"blob": "z" * (2 * 1024 * 1024)}])

        assert write.body == "[]"
        assert len(write.payload) < CREATE_CODE._MAX_SAMPLE_BYTES
        assert json.loads(write.body) == []
        assert not write.logger.warnings, "the degenerate case is a degraded sample, not a failure"
        for parser in (
            CREATE_CODE._sample_rows_from_json,
            SCHEMA_MAPPING._sample_rows_from_json,
            AGENT._parse_json_data,
        ):
            assert parser(write.body, 100) == []

    def test_the_write_lands_at_the_deterministic_key_task_31_probes(self) -> None:
        """R1.4: the key is a contract — ``schema_extract``'s probe targets it."""
        write = run_sample_block([{"i": 1}])
        (call,) = write.calls

        assert call["Bucket"] == _EXPECTED_BUCKET
        assert call["Key"] == _EXPECTED_KEY
        assert call["ContentType"] == "application/json"
        assert _EXPECTED_URI in write.logger.infos[0]


# ---------------------------------------------------------------------------
# Values JSON cannot carry
# ---------------------------------------------------------------------------


class _StrRaises:
    """A value whose coercion fails — the per-field except clause's subject."""

    def __str__(self) -> str:
        raise RuntimeError("this column will not coerce")

    def __repr__(self) -> str:
        return "<_StrRaises>"


class TestNonSerialisableValues:
    """A column JSON cannot carry degrades the field, never the run.

    ``json.dumps`` must not raise for any of these, and the scalar types must
    survive as scalars: the design chose a JSON array over CSV specifically so
    the Sample_Frame gets real numbers instead of an all-string frame.

    **Validates: Requirements 1.2, 1.3**
    """

    _ODD_ROW: dict[str, Any] = {
        "a_date": datetime.date(2024, 5, 12),
        "a_timestamp": datetime.datetime(2024, 5, 12, 1, 2, 3),
        "a_decimal": decimal.Decimal("42.50"),
        "some_bytes": b"\x00\x01",
        "a_struct": {"inner": 1},
        "an_array": [1, 2, 3],
        "a_nan": float("nan"),
        "an_inf": float("inf"),
        "a_neg_inf": float("-inf"),
        "will_not_coerce": _StrRaises(),
        "an_int": 7,
        "a_float": 1.5,
        "a_bool": True,
        "a_none": None,
        "a_string": 'he said "hi"\nand \\ that',
    }

    def test_the_write_succeeds_and_the_body_parses(self) -> None:
        write = run_sample_block([self._ODD_ROW])

        assert write.wrote
        assert not write.logger.warnings, f"a coercible row set must not warn: {write.logger.warnings}"
        (row,) = write.rows
        for parser in (
            CREATE_CODE._sample_rows_from_json,
            SCHEMA_MAPPING._sample_rows_from_json,
            AGENT._parse_json_data,
        ):
            assert parser(write.body, 100) == [row]

    def test_non_finite_floats_become_null_not_invalid_json(self) -> None:
        """``NaN`` / ``Infinity`` are Python-``json`` extensions, not valid JSON.

        Left alone, ``json.dumps`` emits the bare tokens and every strict parser
        downstream — including Spark's — rejects the body.
        """
        write = run_sample_block([self._ODD_ROW])
        (row,) = write.rows

        assert row["a_nan"] is None
        assert row["an_inf"] is None
        assert row["a_neg_inf"] is None
        for token in ("NaN", "Infinity", "-Infinity"):
            assert token not in write.body

    def test_a_field_that_will_not_coerce_is_omitted_and_its_row_survives(self) -> None:
        """A partial sample is useful; a failed write is not (R1.6)."""
        write = run_sample_block([self._ODD_ROW, {"plain": 1}])
        first, second = write.rows

        assert "will_not_coerce" not in first
        assert first["an_int"] == 7, "the rest of the row must be intact"
        assert len(first) == len(self._ODD_ROW) - 1
        assert second == {"plain": 1}, "a later row must be unaffected"

    def test_scalars_keep_their_json_type(self) -> None:
        """The reason the design chose JSON over CSV, asserted rather than assumed."""
        write = run_sample_block([self._ODD_ROW])
        (row,) = write.rows

        assert row["an_int"] == 7 and type(row["an_int"]) is int
        assert row["a_float"] == 1.5 and type(row["a_float"]) is float
        assert row["a_bool"] is True
        assert row["a_none"] is None
        assert row["a_string"] == 'he said "hi"\nand \\ that'

    def test_everything_json_has_no_type_for_arrives_as_a_string(self) -> None:
        """Design N4's stated limitation, pinned so it is a choice not a surprise."""
        write = run_sample_block([self._ODD_ROW])
        (row,) = write.rows

        assert row["a_date"] == "2024-05-12"
        assert row["a_timestamp"] == "2024-05-12 01:02:03"
        assert row["a_decimal"] == "42.50"
        assert isinstance(row["some_bytes"], str)
        assert isinstance(row["a_struct"], str)
        assert isinstance(row["an_array"], str)

    @settings(max_examples=60)
    @given(rows=_ROW_SET)
    def test_no_generated_row_set_can_make_the_write_fail(self, rows: list[dict[str, Any]]) -> None:
        """R1.6 from the serialisation side: coercion is total over these types."""
        write = run_sample_block(rows)

        assert write.wrote
        assert not write.logger.warnings
        for row in write.rows:
            for value in row.values():
                assert value is None or isinstance(value, (bool, int, float, str))
                if isinstance(value, float):
                    assert math.isfinite(value)


# ---------------------------------------------------------------------------
# Two constants, two languages, one process boundary
# ---------------------------------------------------------------------------


class TestTheBoundsAgreeAcrossTheProcessBoundary:
    """The generated job and the readers must agree on 100 and 1,048,576.

    Neither can import the other — the Glue job is emitted text executed in a
    Spark driver, the readers are Lambda modules — so the numbers are duplicated
    by necessity and pinned here so they cannot drift. Same shape as
    ``test_approval_window_cross_language.py`` and
    ``test_non_blocking_severities_cross_language.py``: read the literal out of
    the other artifact rather than restating it.

    The direction of authority is the readers: the job's ceiling exists to stay
    under ``_MAX_SAMPLE_BYTES``, so if one moves it is the job that follows.
    """

    def test_the_emitted_byte_ceiling_equals_every_readers_ceiling(self) -> None:
        match = re.search(r"^\s*_ADI_SAMPLE_MAX_BYTES\s*=\s*(\d+)\s*$", SAMPLE_BLOCK_SOURCE, re.MULTILINE)
        assert match, "the emitted block no longer assigns _ADI_SAMPLE_MAX_BYTES; this pin has gone blind"
        emitted = int(match.group(1))

        assert emitted == CREATE_CODE._MAX_SAMPLE_BYTES, (
            f"the generated job bounds the artifact at {emitted} but create_code reads at most "
            f"{CREATE_CODE._MAX_SAMPLE_BYTES} (lambda_function.py:258). An artifact above the reader's "
            f"ceiling truncates mid-array and yields {{}} — BC10."
        )
        assert emitted == SCHEMA_MAPPING._MAX_SAMPLE_BYTES

    def test_the_emitted_row_cap_equals_every_readers_row_cap(self) -> None:
        match = re.search(r"df\.limit\((\d+)\)\.collect\(\)", SAMPLE_BLOCK_SOURCE)
        assert match, "the emitted block no longer limits before collecting; this pin has gone blind"
        emitted = int(match.group(1))

        assert emitted == CREATE_CODE._MAX_SAMPLE_ROWS, (
            f"the generated job writes up to {emitted} rows but create_code samples "
            f"{CREATE_CODE._MAX_SAMPLE_ROWS} (lambda_function.py:256)"
        )
        assert emitted == SCHEMA_MAPPING._MAX_SAMPLE_ROWS
        assert emitted == AGENT._MAX_SAMPLE_ROWS

    def test_the_emitted_ceiling_is_a_strict_upper_bound(self) -> None:
        """``>=`` in the truncation loop, so the body is always strictly under."""
        assert ">= _ADI_SAMPLE_MAX_BYTES" in SAMPLE_BLOCK_SOURCE, (
            "the loop must keep popping while the body is AT the ceiling, not only above it — "
            "read(_MAX_SAMPLE_BYTES) on a body of exactly that size leaves no room to detect truncation"
        )
