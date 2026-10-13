# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for the trace sample-value redactor (p52 task 3.2).

Subject: ``_redact_sample_values`` in ``shared/agent_trace_plugin.py``, added by
p52 task 3.1 and applied to all four Preview_Fields inside ``flush()``.

**Properties carried here**

* **Property 4: No sampled value survives into a trace** —
  **Validates: Requirements 5.1, 5.3, 5.4, 5.5**
* **Property 5: Redaction is inert without the marker** —
  **Validates: Requirements 5.6**
* **Property 6: The count and the field name survive** —
  **Validates: Requirements 6.1, 6.2, 6.3, 6.4**

Plus one **example-based documented-limit test** at the bottom, which is not a
property and is deliberately not framed as one.

--------------------------------------------------------------------------
HOW PROPERTY 4 IS FRAMED, AND WHY IT IS NOT "ANY MARKED STRING IS SCRUBBED"
--------------------------------------------------------------------------

The invariant is **"a value that a producer rendered does not survive
redaction"** — not "any string containing the marker is fully redacted". The
second reading is false, and stating it as the property would produce a red on
input production cannot emit, whereupon the honest-looking fix would be to
weaken the property.

Redaction alone does **not** make a raw embedded newline safe. p52 gate 1.2
established this by rendering the real functions: one ``\\n`` inside a sample
value pushes the entire remainder of the value list onto the *next* line, which
is outside marker-to-end-of-line redaction, and that residue carries **no
marker**, so no later pass of a marker-keyed redactor ever reaches it. What
closes that hole is **producer-side normalisation** — both producers collapse
``\\n`` and ``\\r`` to spaces before a value is ever rendered:

* ``_truncate_sample_value`` (``schema_mapping/lambda_function.py:958``)
* ``read_sample_data`` (``agentcore/complex_mapping_agent/main.py:133``, the
  line p52 task 3.1 added — a prompt-content change, not a trace detail)

So Property 4 generates values, pushes them through **each producer's real
normalisation and real render**, redacts the result, and asserts no value
fragment survives. Both producers, because the newline case behaves differently
on each (design risk N1) — a generator that exercised only the
``schema_mapping`` shape would pass while the complex-mapping path leaked.

The two arms enter their producer at different seams, because that is where each
producer's normalisation lives:

* ``schema_mapping`` normalises at **render** time. ``read_source_samples``
  (``lambda_function.py:875-949``, read end to end) only ``.strip()``s, dedupes
  and drops empty/``none``/``null``/``nan`` values; the ``\\n``/``\\r`` collapse
  happens in ``_truncate_sample_value``, which ``render_sample_values_section``
  applies to every value and to the field name. Entering at
  ``render_sample_values_section`` therefore covers the whole normalise-and-render
  path, and no step in the reader can introduce a newline the render does not
  remove.
* ``complex_mapping_agent`` normalises at **read** time, inline in
  ``read_sample_data``'s row loop, and ``build_evidence_prompt`` interpolates with
  no further normalisation. So that arm drives ``read_sample_data`` and then
  ``build_evidence_prompt`` — the real pair.

**Consequence: deleting either producer's ``.replace("\\n", " ")`` fails
Property 4 here.** That is the point of running the producers rather than
hand-writing their output shape.

Task 3.4 owns the example tests for the two rendered shapes; this file's job is
generation.

------------------------------------------
WHAT PROPERTY 6 CAN AND CANNOT CLAIM ABOUT N
------------------------------------------

Task 3.1's redactor emits **two** forms, and Property 6 accepts both because
both are correct behaviour:

* balanced: ``Sample values: [N values redacted]``, N counting complete ``"…"``
  runs in the discarded span. Exact for both producers' normal output.
* degraded: ``Sample values: [1+ values redacted, exact count unavailable:
  <reason>]`` where ``<reason>`` is ``list continued past a line break`` or
  ``unbalanced quoting``.

Three things measured against the real redactor before these assertions were
written, each of which rules out a tempting stronger claim:

1. ``1+`` is deliberate, not laziness. A naive count reads **0** in the newline
   case for a field that had one value — inverted, not merely imprecise — and
   ``complete + 1`` is unsound because an escaped ``"`` makes runs straddle
   values. So Property 6 asserts the degraded form never reads a bare ``0``
   where values were present, and never asserts a floor the span cannot support.
2. The balanced form's N is exact only when no value contains ``"``. Measured:
   ``["a"b", "c"d"]`` (two values, raw quotes) reads **3 values redacted** — an
   over-claim. So exactness is asserted on a quote-free arm (6a) and only
   ``N >= 1`` on the hostile arm (6b).
3. The ``<reason>`` string is **not** a reliable diagnosis of the cause. It is
   chosen by ``eol < len(text)``, i.e. "is there a newline anywhere after the
   marker", so a value carrying a raw ``"`` in a multi-field render reports
   ``list continued past a line break``. Measured. 6b therefore asserts the
   reason is one of the two known strings, and does not assert which.

A bare ``0`` **is** correct for an empty list (``Sample values: []`` reads
``0 values redacted``), which is why "never a bare 0" is conditioned on at least
one value having been rendered.

-----------------------------
LOADING THE PRODUCERS SAFELY
-----------------------------

Both producer modules are loaded from their file path under a **private alias**
and are deliberately **not** published into ``sys.modules``, following the
pattern task 1.1 recorded for ``test_extract_field_mappings_characterization.py``.
This matters more than usual here: the established way to import
``schema_mapping/lambda_function.py`` is
``sys.modules.setdefault("env_utils", MagicMock(...))`` plus
``import lambda_function``, and doing that from ``tests/unit/shared/`` would
hand a ``MagicMock`` to every other test in this directory that imports the real
``env_utils``. Design risk N5 and ``LGR-c58f276-4`` name this import-order
pollution; the stubs here live inside a ``patch.dict`` and are gone once
``exec_module`` returns. Verified: after import, neither ``lambda_function`` nor
``main`` is in ``sys.modules``, and ``env_utils`` resolves to the real module.

The ``MagicMock`` standing in for ``boto3`` is not a shared-fixture case. The
redactor is pure — no AWS call, no network, no file read (R7.5) — and neither
producer function under test touches AWS; the mock exists only because
``complex_mapping_agent/main.py`` cannot be imported at all without stubbing
``bedrock_agentcore``, and because ``read_sample_data`` reads its rows through a
module-level S3 client. This mirrors the stubbing the existing agent suite
already does at ``tests/unit/agents/complex_mapping_agent/test_complex_mapping.py:12-31``.
No ``moto.mock_aws()`` is created here.

-------------
LEDGER GAP
-------------

Stated as a gap, not as an all-clear. ``why`` on
``shared/agent_trace_plugin.py`` returns four broad snapshot commits
(``2b45b72912``, ``7ad6ca1ad9``, ``7c18e81fcc``, ``b48a349b7b``), every one with
``records: []``, and ``decisions(scope=…)`` returns only ``LGR-d5bcee9-1``
(cdk.out) and ``LGR-819980f-1`` (the JDBC-credentials invariant, scoped
``shared/**``). The **same** gap covers
``agentcore/complex_mapping_agent/main.py``. ``LGR-1e135d2-4``'s ``revisit_when``
("the trace table's retention/redaction posture changes") is the record Part B
answers to.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

#: ``tests/unit/shared/<this file>`` → ``agentic_data_ingestion/``.
_ROOT = Path(__file__).resolve().parents[3]

# Shared modules importable without the Lambda layer mount, as every other
# module in this directory does it.
sys.path.insert(0, str(_ROOT / "shared"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from agent_trace_plugin import _redact_sample_values

_MARKER = "Sample values: ["


# ---------------------------------------------------------------------------
# Producer modules, loaded under private aliases (see the module docstring)
# ---------------------------------------------------------------------------


def _load_under_alias(alias: str, path: Path, stubs: dict[str, object]):
    """Exec *path* as a module named *alias*, without publishing it.

    ``stubs`` is applied to ``sys.modules`` only for the duration of
    ``exec_module``, so nothing this module needs at import time leaks into the
    rest of the ``tests/unit/shared/`` run.
    """
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", stubs):
        spec.loader.exec_module(module)
    return module


_SCHEMA_MAPPING = _load_under_alias(
    "_p52_schema_mapping_producer",
    _ROOT / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py",
    {
        "eventbridge_utils": MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
        "env_utils": MagicMock(require_env=lambda name, default=None: default or name),
        "artifact_schema": MagicMock(),
        "metrics_facade": MagicMock(PipelineMetrics=None),
        "mapping_memory": MagicMock(MappingMemoryClient=None),
    },
)

_CMA_S3 = MagicMock()
_cma_boto3 = MagicMock()
_cma_boto3.client.return_value = _CMA_S3
_cma_boto3.resource.return_value = MagicMock()

_COMPLEX_MAPPING = _load_under_alias(
    "_p52_complex_mapping_producer",
    _ROOT / "agentcore" / "complex_mapping_agent" / "main.py",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": _cma_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
        "mapping_memory": MagicMock(),
    },
)
_COMPLEX_MAPPING._s3_client = _CMA_S3

#: The producers' own caps, read from the producers rather than transcribed.
_SM_VALUES_PER_FIELD = _SCHEMA_MAPPING._PROMPT_SAMPLE_VALUES_PER_FIELD
_SM_VALUE_MAX_CHARS = _SCHEMA_MAPPING._PROMPT_SAMPLE_VALUE_MAX_CHARS
#: ``build_evidence_prompt`` renders ``samples[:5]``.
_CMA_VALUES_PER_FIELD = 5


# ---------------------------------------------------------------------------
# Rendering through the real producers
# ---------------------------------------------------------------------------


def _render_schema_mapping(field_values: dict[str, list[str]]) -> str:
    """The ``schema_mapping`` Sample_Block, via the real render.

    ``render_sample_values_section`` applies the real ``_truncate_sample_value``
    to every value and to the field name, which is where that producer's
    ``\\n``/``\\r`` collapse lives.
    """
    source_schema = {"properties": {name: {"type": "string"} for name in field_values}}
    return _SCHEMA_MAPPING.render_sample_values_section(source_schema, field_values)


def _render_complex_mapping(field_values: dict[str, list[str]]) -> tuple[str, dict[str, list[str]]]:
    """The ``complex_mapping_agent`` prompt, via the real read **and** render.

    The values are handed to ``read_sample_data`` as a JSON array body, because
    ``_parse_json_data`` returns ``json.loads`` output unchanged — so a generated
    value reaches the normalisation byte-identical, which a CSV round-trip would
    not guarantee. Returns the rendered prompt and the post-normalisation dict
    the producer itself produced, so callers assert against what was really
    rendered rather than against what was generated.
    """
    row_count = max((len(values) for values in field_values.values()), default=0)
    rows: list[dict[str, str]] = []
    for index in range(row_count):
        row = {name: values[index] for name, values in field_values.items() if index < len(values)}
        if row:
            rows.append(row)

    body = json.dumps(rows).encode("utf-8")
    _CMA_S3.get_object.side_effect = None
    _CMA_S3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}

    samples = _COMPLEX_MAPPING.read_sample_data("s3://p52-test-bucket/samples.json")
    source_schema = {"fields": [{"name": name, "type": "string"} for name in field_values]}
    target_schema = {"fields": [{"name": "street_address", "type": "string"}]}
    prompt = _COMPLEX_MAPPING.build_evidence_prompt(source_schema, target_schema, samples, [])
    return prompt, samples


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: The alphabet Property 4 is required to cover: a quote (defeats quote
#: balancing), a closing bracket (defeats bracket balancing) — task 3.1's
#: docstring names both as the reason end-of-line rather than structure is the
#: boundary — a comma (the producers' own separator), a raw newline and a raw
#: carriage return (design risk N1), a backslash, and non-ASCII.
#:
#: Deliberately free of uppercase letters and of ``~``, so that noise can never
#: forge a sentinel.
_NOISE_CHARS = ['"', "]", "[", ",", "\n", "\r", "\\", ":", " ", "\t", "a", "0", "é", "漢", "—", "\u00a0"]

#: Quote- and newline-free noise, for the arm that asserts an exact count.
_BENIGN_NOISE_CHARS = ["]", "[", ",", " ", "a", "0", "é", "漢", "—", ":"]

_FIELD_NAMES = ["addr_1", "city", "signup_date", "cust_ref", "notes_x"]


def _sentinels(index: int) -> tuple[str, str]:
    """The two markers bracketing generated value *index*.

    One at each end: a leak of this redactor can only be a **suffix** of the
    marker's line (everything from the first newline onward), so a surviving
    fragment carries the tail sentinel — unless truncation removed it, which the
    lead sentinel and the conditional assertion cover.
    """
    return f"~LEAD{index}~", f"~TAIL{index}~"


@st.composite
def _sample_values(draw, noise_chars: list[str], max_noise: int, min_count: int = 1, max_count: int = 6):
    """A list of sentinel-bracketed sample values.

    Sentinels make every value unique and non-empty, which keeps them clear of
    ``read_sample_data``'s dedupe and of its empty/``none``/``null``/``nan``
    filter — so the count of values the producer renders is knowable, and the
    properties assert about redaction rather than about that filter.
    """
    count = draw(st.integers(min_value=min_count, max_value=max_count))
    values = []
    for index in range(count):
        noise = draw(st.text(alphabet=st.sampled_from(noise_chars), max_size=max_noise))
        lead, tail = _sentinels(index)
        values.append(f"{lead}{noise}{tail}")
    return values


#: Noise short enough that ``_truncate_sample_value``'s 64-char cap keeps both
#: sentinels: 8 + 40 + 8 = 56.
_SHORT_NOISE = _SM_VALUE_MAX_CHARS - 24

#: Long enough to force truncation on the ``schema_mapping`` arm, so the cap is
#: exercised too. The tail sentinel then never reaches the render, and the
#: conditional assertion skips it rather than asserting something vacuous.
_LONG_NOISE = _SM_VALUE_MAX_CHARS * 2


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------

_SUMMARY_EXACT = re.compile(r"^(\d+) values redacted$")
_SUMMARY_DEGRADED = re.compile(
    r"^1\+ values redacted, exact count unavailable: (list continued past a line break|unbalanced quoting)$"
)


def _summaries(text: str) -> list[str]:
    """Every marker occurrence's replacement summary, without its brackets."""
    found = []
    pos = 0
    while True:
        start = text.find(_MARKER, pos)
        if start == -1:
            return found
        open_bracket = start + len(_MARKER)
        close_bracket = text.find("]", open_bracket)
        assert close_bracket != -1, f"marker with no closing bracket in redacted text: {text!r}"
        found.append(text[open_bracket:close_bracket])
        pos = close_bracket + 1


def _assert_marker_lines_are_only_summaries(redacted: str) -> None:
    """Every marker's line holds nothing but the summary.

    This is the structural half of Property 4: it pins that the whole tail of
    the marker's line was replaced, so nothing of the value list is left on the
    line the redactor rewrote. It says nothing about the *next* line, which is
    the documented limit the class at the bottom of this file pins.
    """
    pos = 0
    while True:
        start = redacted.find(_MARKER, pos)
        if start == -1:
            return
        close_bracket = redacted.find("]", start + len(_MARKER))
        assert close_bracket != -1
        summary = redacted[start + len(_MARKER) : close_bracket]
        assert _SUMMARY_EXACT.match(summary) or _SUMMARY_DEGRADED.match(summary), (
            f"marker tail is not a redaction summary: {summary!r} in {redacted!r}"
        )
        after = redacted[close_bracket + 1 : close_bracket + 2]
        assert after in ("", "\n", "\r"), (
            f"text survives after the redaction summary on the marker's own line: {after!r} in {redacted!r}"
        )
        pos = close_bracket + 1


def _assert_no_value_survives(raw: str, redacted: str, values: list[str]) -> None:
    """No sentinel that reached the render survives redaction.

    Conditioned on ``sentinel in raw`` because a producer may legitimately not
    render a value or part of one — ``_truncate_sample_value`` caps at
    ``_PROMPT_SAMPLE_VALUE_MAX_CHARS``, and both producers cap the number of
    values per field. Asserting the absence of something never rendered would
    pass vacuously and hide the cap changing.
    """
    for index in range(len(values)):
        for sentinel in _sentinels(index):
            if sentinel in raw:
                assert sentinel not in redacted, (
                    f"sampled value fragment {sentinel!r} survived redaction.\n"
                    f"  rendered: {raw!r}\n  redacted: {redacted!r}"
                )


# ---------------------------------------------------------------------------
# Property 4 — no sampled value survives into a trace
# ---------------------------------------------------------------------------


class TestProperty4NoSampledValueSurvives:
    """Property 4: No sampled value survives into a trace.

    For any list of sample values — including values carrying ``"``, ``]``,
    ``,``, raw newlines, raw carriage returns and non-ASCII text — a
    Preview_Field built by either producer's real normalisation and render
    contains none of those values after redaction.

    **Validates: Requirements 5.1, 5.3, 5.4, 5.5**
    """

    @given(
        values=_sample_values(_NOISE_CHARS, _SHORT_NOISE),
        field_name=st.sampled_from(_FIELD_NAMES),
    )
    @settings(max_examples=200, deadline=None)
    def test_schema_mapping_render_leaks_nothing(self, values: list[str], field_name: str):
        """R5.4's producer: the manual ``_model_calls`` append path's prompt."""
        raw = _render_schema_mapping({field_name: values})
        redacted = _redact_sample_values(raw)

        assert _MARKER in raw, "the render carried no Sample_Block — the fixture, not the redactor, is wrong"
        _assert_no_value_survives(raw, redacted, values)
        _assert_marker_lines_are_only_summaries(redacted)
        assert redacted != raw, "a rendered Sample_Block was left untouched"

    @given(
        values=_sample_values(_NOISE_CHARS, _SHORT_NOISE),
        field_name=st.sampled_from(_FIELD_NAMES),
    )
    @settings(max_examples=200, deadline=None)
    def test_complex_mapping_render_leaks_nothing(self, values: list[str], field_name: str):
        """R5.5's producer — the older exposure, live since P13.

        This is the arm design risk N1 is about: before task 3.1's one-line
        change at ``read_sample_data``, a value carrying ``\\n`` put every value
        after the break outside redaction. The property passes because of that
        normalisation, so deleting it fails here.
        """
        raw, samples = _render_complex_mapping({field_name: values})
        redacted = _redact_sample_values(raw)

        assert _MARKER in raw, "the render carried no Sample_Block — the fixture, not the redactor, is wrong"
        assert samples.get(field_name), "read_sample_data dropped every generated value"
        _assert_no_value_survives(raw, redacted, values)
        _assert_marker_lines_are_only_summaries(redacted)
        assert redacted != raw, "a rendered Sample_Block was left untouched"

    @given(
        values=_sample_values(_NOISE_CHARS, _LONG_NOISE, max_count=3),
        field_name=st.sampled_from(_FIELD_NAMES),
    )
    @settings(max_examples=150, deadline=None)
    def test_truncated_values_leak_nothing_on_either_producer(self, values: list[str], field_name: str):
        """Values long enough to hit ``_truncate_sample_value``'s 64-char cap.

        Truncation is where a leak would be easiest to miss: the tail sentinel is
        gone from the render, so only the lead sentinel is available as evidence.
        """
        for raw in (_render_schema_mapping({field_name: values}), _render_complex_mapping({field_name: values})[0]):
            redacted = _redact_sample_values(raw)
            _assert_no_value_survives(raw, redacted, values)
            _assert_marker_lines_are_only_summaries(redacted)

    @given(
        first=_sample_values(_NOISE_CHARS, _SHORT_NOISE, max_count=3),
        second=_sample_values(_NOISE_CHARS, _SHORT_NOISE, max_count=3),
    )
    @settings(max_examples=150, deadline=None)
    def test_multiple_fields_leak_nothing_on_either_producer(self, first: list[str], second: list[str]):
        """Several Sample_Blocks in one Preview_Field.

        Both producers render one block per field, so a real ``prompt_preview``
        carries as many marker lines as there are sampled fields. A redactor that
        handled only the first occurrence would pass every single-field case.
        """
        # Distinct sentinel spaces per field, so a leak names the field it came from.
        field_values = {
            "addr_1": first,
            "city": [value.replace("~LEAD", "~LEADB").replace("~TAIL", "~TAILB") for value in second],
        }

        for raw in (_render_schema_mapping(field_values), _render_complex_mapping(field_values)[0]):
            redacted = _redact_sample_values(raw)
            assert raw.count(_MARKER) == 2, f"expected one Sample_Block per field: {raw!r}"
            assert redacted.count(_MARKER) == 2, "a marker was lost — R6.4 requires it stays findable"
            for values, prefix in ((first, ""), (second, "B")):
                for index in range(len(values)):
                    for sentinel in (f"~LEAD{prefix}{index}~", f"~TAIL{prefix}{index}~"):
                        if sentinel in raw:
                            assert sentinel not in redacted, f"{sentinel!r} survived redaction in {redacted!r}"
            _assert_marker_lines_are_only_summaries(redacted)


# ---------------------------------------------------------------------------
# Property 5 — redaction is inert without the marker
# ---------------------------------------------------------------------------

#: Near-misses of the marker. Each differs from ``Sample values: [`` in one way
#: a substring search must respect: case, spacing, punctuation, or the bracket.
_MARKER_NEAR_MISSES = [
    "Sample values:",
    "Sample values: ",
    "sample values: [",
    "SAMPLE VALUES: [",
    "Sample  values: [",
    "Sample values:[",
    "Sample_values: [",
    "Sample values [",
    "values: [",
    "  - addr_1: Sample values:\n",
]


class TestProperty5RedactionIsInertWithoutTheMarker:
    """Property 5: Redaction is inert without the marker.

    For any string containing no ``Sample values: [`` marker, the redactor
    returns it unchanged. This is what makes applying the redactor to all four
    Preview_Fields free — ``response_preview``, ``tool_params`` and
    ``result_preview`` usually carry no Sample_Block at all.

    **Validates: Requirements 5.6**
    """

    @given(text=st.text(max_size=400).filter(lambda value: _MARKER not in value))
    @settings(max_examples=300)
    def test_arbitrary_unmarked_text_is_returned_unchanged(self, text: str):
        assert _redact_sample_values(text) == text

    @given(
        near_miss=st.sampled_from(_MARKER_NEAR_MISSES),
        before=st.text(max_size=60).filter(lambda value: _MARKER not in value),
        after=st.text(max_size=60).filter(lambda value: _MARKER not in value),
    )
    @settings(max_examples=200)
    def test_near_misses_of_the_marker_are_returned_unchanged(self, near_miss: str, before: str, after: str):
        """A near-miss must not be treated as a marker.

        The marker is matched as a literal, so anything that is not it byte for
        byte leaves the text alone. Worth generating rather than asserting once:
        a future regex-based matcher that "helpfully" tolerated case or spacing
        would start deleting text that is not a Sample_Block.
        """
        text = f"{before}{near_miss}{after}"
        # A near-miss abutting generated text can spell the real marker (``Sample
        # values:`` followed by generated `` [``). ``assume`` discards that example;
        # ``pytest.skip`` would abandon the whole test on the first such draw.
        assume(_MARKER not in text)
        assert _redact_sample_values(text) == text

    @given(text=st.sampled_from(["", "\n", "\r", " ", "]", '"', "0 values redacted"]))
    @settings(max_examples=100)
    def test_degenerate_inputs_are_returned_unchanged(self, text: str):
        """Includes the redactor's own output vocabulary, which carries no marker."""
        assert _redact_sample_values(text) == text


# ---------------------------------------------------------------------------
# Property 6 — the count and the field name survive
# ---------------------------------------------------------------------------


class TestProperty6CountAndFieldNameSurvive:
    """Property 6: The count and the field name survive.

    For any Sample_Block of N values rendered for field F, the redacted output
    contains F, contains the ``Sample values:`` marker, and states that values
    were present and were removed — in one of the redactor's two forms.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    """

    @given(
        values=_sample_values(_BENIGN_NOISE_CHARS, _SHORT_NOISE, max_count=_SM_VALUES_PER_FIELD),
        field_name=st.sampled_from(_FIELD_NAMES),
    )
    @settings(max_examples=200, deadline=None)
    def test_exact_count_survives_for_quote_free_values(self, values: list[str], field_name: str):
        """6a — the normal producer case: N is exact.

        Quote-free and newline-free values are what both producers emit in
        practice, since each rendered value is exactly one ``"…"`` run. So this
        arm asserts the strong claim: the count in the trace equals the number of
        values the model was shown.
        """
        for raw, rendered_count in (
            (_render_schema_mapping({field_name: values}), min(len(values), _SM_VALUES_PER_FIELD)),
            (
                _render_complex_mapping({field_name: values})[0],
                min(len(values), _CMA_VALUES_PER_FIELD),
            ),
        ):
            redacted = _redact_sample_values(raw)

            assert field_name in redacted, "R6.3: the field name the block was rendered for must survive"
            assert "Sample values:" in redacted, "R6.4: the marker must stay findable"
            assert redacted.count(_MARKER) == raw.count(_MARKER)

            summaries = _summaries(redacted)
            assert len(summaries) == 1, f"expected one Sample_Block: {redacted!r}"
            match = _SUMMARY_EXACT.match(summaries[0])
            assert match, f"quote-free values should give an exact count, got {summaries[0]!r}"
            assert int(match.group(1)) == rendered_count, (
                f"R6.2: count {match.group(1)} != {rendered_count} values rendered in {raw!r}"
            )
            assert "values redacted" in summaries[0], "R6.1: the output must say values were present and removed"

    @given(
        values=_sample_values(_NOISE_CHARS, _SHORT_NOISE),
        field_name=st.sampled_from(_FIELD_NAMES),
    )
    @settings(max_examples=200, deadline=None)
    def test_degraded_form_is_honest_for_hostile_values(self, values: list[str], field_name: str):
        """6b — both forms accepted, and neither may read a bare zero.

        The hostile alphabet makes an exact count underivable in two ways task
        3.1's docstring names, so this arm asserts only what the discarded span
        can always support: the field name, the marker, one of the two forms,
        and — because at least one value was rendered — a claim of at least one
        value. The inverted ``0`` that a naive count produced in the newline case
        is exactly what this forbids.
        """
        for raw in (
            _render_schema_mapping({field_name: values}),
            _render_complex_mapping({field_name: values})[0],
        ):
            redacted = _redact_sample_values(raw)

            assert field_name in redacted, "R6.3: the field name must survive"
            assert "Sample values:" in redacted, "R6.4: the marker must stay findable"

            summaries = _summaries(redacted)
            assert len(summaries) == 1, f"expected one Sample_Block: {redacted!r}"
            summary = summaries[0]
            assert "values redacted" in summary, "R6.1: the output must say values were present and removed"

            exact = _SUMMARY_EXACT.match(summary)
            degraded = _SUMMARY_DEGRADED.match(summary)
            assert exact or degraded, f"summary is neither redactor form: {summary!r}"

            if exact:
                assert int(exact.group(1)) >= 1, (
                    f"R6.2: {summary!r} reads a bare zero, but values were rendered: {raw!r}"
                )
            else:
                assert summary.startswith("1+ values redacted"), (
                    f"the degraded form must claim a floor of one, not zero: {summary!r}"
                )


# ---------------------------------------------------------------------------
# The documented limit — NOT a property, and NOT a defect to fix here
# ---------------------------------------------------------------------------


class TestRawNewlineResidueIsTheDocumentedLimit:
    """What the redactor **alone** can and cannot promise.

    Fed a marker line with a raw embedded newline — which no producer can emit
    today — marker-to-end-of-line redaction leaves the remainder of the value
    list on the following line, and that residue carries **no marker**, so no
    later pass of a marker-keyed redactor reaches it. p52 gate 1.2 established
    this by rendering the real functions.

    **This is not a defect for this task to fix.** It is the recorded boundary of
    the mechanism, and the reason the producer-side normalisation exists:

    * ``_truncate_sample_value`` (``schema_mapping/lambda_function.py:958``)
    * ``read_sample_data`` (``agentcore/complex_mapping_agent/main.py:133``)

    Both collapse ``\\n`` and ``\\r`` to spaces before a value is rendered.
    Removing either **reopens this leak**, which is why these cases are pinned
    here: a later "simplification" of either producer fails on the last test in
    this class, and a reader who finds the residue in isolation can see it was
    known rather than missed. Bracket-balancing is not the alternative — task
    3.1's docstring records that a value carrying ``]`` or ``"`` defeats any
    balancing parse, which is a leak, where this is a residue on a path the
    producers close.
    """

    #: Gate 1.2's own render, in the ``schema_mapping`` shape.
    _SCHEMA_SHAPE = '  - addr_1: Sample values: ["123 Main St\nAPT-4-SECRET"]\n  - city: Sample values: ["Seattle"]\n'
    #: The same, in ``build_evidence_prompt``'s shape.
    _CMA_SHAPE = (
        "  - addr_1 (type: string)\n"
        '    Sample values: ["123 Main St\nAPT-4-SECRET"]\n'
        "  - city (type: string)\n"
        '    Sample values: ["Seattle"]\n'
    )

    @pytest.mark.parametrize("raw", [_SCHEMA_SHAPE, _CMA_SHAPE], ids=["schema_mapping_shape", "complex_mapping_shape"])
    def test_residue_after_a_raw_newline_survives_and_carries_no_marker(self, raw: str):
        redacted = _redact_sample_values(raw)

        # The documented limit, stated as an assertion so it cannot rot silently.
        assert "APT-4-SECRET" in redacted, (
            "the residue no longer survives — if a producer or the redactor changed "
            "so that a raw newline is handled, this test and its reasoning should be "
            "revisited rather than deleted"
        )
        residue_line = next(line for line in redacted.splitlines() if "APT-4-SECRET" in line)
        assert _MARKER not in residue_line, (
            "the residue carries no marker, which is why no later marker-keyed pass reaches it"
        )

        # The marker's own line degrades honestly rather than reading zero.
        summaries = _summaries(redacted)
        assert summaries[0] == "1+ values redacted, exact count unavailable: list continued past a line break"
        assert not summaries[0].startswith("0 "), "a naive count read 0 here for a field that had one value"

        # The leak is confined to the field whose value carried the break.
        assert summaries[1] == "1 values redacted"
        assert "Seattle" not in redacted

    def test_the_residue_is_the_whole_remainder_of_the_list(self):
        """Gate 1.2's three-value render: one break exposes every later value."""
        raw = '  - addr_1: Sample values: ["1 First St\nLEAK-A", "2 Second St LEAK-B", "3 Third St LEAK-C"]\n'
        redacted = _redact_sample_values(raw)
        for fragment in ("LEAK-A", "LEAK-B", "LEAK-C"):
            assert fragment in redacted, f"{fragment} unexpectedly removed; the recorded boundary has moved"

    def test_a_lone_carriage_return_behaves_the_same_way(self):
        """``\\r`` terminates the span too, so it leaves the same kind of residue."""
        raw = '  - a: Sample values: ["x\rLEAK-CR"]\n  - b: Sample values: ["y"]\n'
        redacted = _redact_sample_values(raw)
        assert "LEAK-CR" in redacted
        assert _summaries(redacted)[1] == "1 values redacted"

    def test_unbalanced_run_to_end_of_text_names_the_other_reason(self):
        """No newline at all: the span runs to end of text, so nothing escapes it."""
        redacted = _redact_sample_values('  - a: Sample values: ["oops')
        assert redacted == "  - a: Sample values: [1+ values redacted, exact count unavailable: unbalanced quoting]"

    def test_an_empty_value_list_may_read_zero(self):
        """A bare ``0`` is correct when there was nothing to redact.

        Recorded because Property 6 forbids a bare zero *where values were
        present*, and that condition would look arbitrary without this case.
        """
        assert _redact_sample_values("  - a: Sample values: []\n") == "  - a: Sample values: [0 values redacted]\n"

    def test_producer_normalisation_is_what_closes_the_leak(self):
        """The same value, through each producer, does **not** leak.

        This is the test that makes the producer-side normalisation load-bearing
        rather than incidental: delete the ``.replace("\\n", " ")`` from either
        producer and this fails, next to a residue case showing exactly what the
        deletion would reopen.
        """
        raw_value = "123 Main St\nAPT-4-SECRET"

        schema_render = _render_schema_mapping({"addr_1": [raw_value]})
        assert "\n" not in schema_render.split("Sample values: [")[1].split("]")[0], (
            "schema_mapping/_truncate_sample_value no longer collapses newlines"
        )
        schema_redacted = _redact_sample_values(schema_render)
        assert "APT-4-SECRET" not in schema_redacted
        assert "Sample values: [1 values redacted]" in schema_redacted

        cma_render, samples = _render_complex_mapping({"addr_1": [raw_value]})
        assert samples["addr_1"] == ["123 Main St APT-4-SECRET"], (
            "complex_mapping_agent/read_sample_data no longer collapses newlines — "
            "p52 risk N1 is reopened and the residue cases above are now reachable in production"
        )
        cma_redacted = _redact_sample_values(cma_render)
        assert "APT-4-SECRET" not in cma_redacted
        assert "Sample values: [1 values redacted]" in cma_redacted
