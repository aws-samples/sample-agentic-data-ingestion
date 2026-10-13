# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Example tests pinning both Sample_Block producers' literal rendered forms (p52 task 3.4).

Subject: the **format convention** that ``_redact_sample_values``
(``shared/agent_trace_plugin.py``, added by p52 task 3.1) is keyed on — not the
redactor's behaviour, which task 3.2 already covers under generation.

=========================================================================
WHY THIS FILE EXISTS, AND WHY IT IS NOT A SECOND COPY OF TASK 3.2
=========================================================================

``tests/unit/shared/test_trace_sample_redaction_prop.py`` (task 3.2) drives
**both producers' real render functions** under Hypothesis and asserts that no
generated value survives redaction. So "redaction works on both shapes" is
covered there, and is deliberately not re-covered here.

What is **not** covered there is the convention itself. The redactor matches the
**literal** marker ``Sample values: [`` and discards from it to the end of that
line. Every one of those words is load-bearing:

* different marker text — ``Sample data: [``, ``Sample values [``,
  ``Sample values:[`` — and the redactor finds nothing;
* a value list wrapped across lines, or rendered as a JSON array on its own
  line, and the discard covers only the head;
* a field name moved to the *right* of the marker, and R6.3's surviving field
  name is deleted along with the values.

In every one of those cases **redaction silently becomes a partial or total
no-op**, and no existing test necessarily goes red: task 3.2's generators render
through the producers, so a producer that changed its marker text would still be
internally consistent with itself — the values would simply stop being redacted,
and 3.2's Property 4 would fail only if its own ``_MARKER`` literal happened to
still match. Its Property 5 (inertness without the marker) would *pass* on the
drifted output, because inert is exactly what it asserts.

So this file pins the **literal rendered form each producer emits today**,
measured off the real functions, so that a format change fails **here**, loudly,
with a message naming what moved and what it costs. That is this file's whole
job.

Everything below asserts against **rendered output**, never against a
hand-written string on its own. A hand-written literal compared to another
hand-written literal keeps passing after a producer changes; the pins here are
one side of a comparison whose other side is a real render.

=========================================================================
THE SHARED MARKER IS A CONVENTION, NOT A COINCIDENCE — AND CONVENTIONS BREAK
=========================================================================

One redactor is sufficient for two producers **because** both render the same
marker, and that is stated intent rather than luck.
``render_sample_values_section``'s own docstring
(``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:990-992``)
records it:

    Rendered in ``build_evidence_prompt:275``'s shape (``Sample values: [...]``,
    capped at five per field) so a reader comparing the two prompts sees one
    convention.

A convention held by a docstring can be broken by someone who does not know it
is load-bearing — and that docstring is itself an illustration: the
``build_evidence_prompt:275`` it names is **stale**; the render moved to
``main.py:292`` when task 3.1 added its comment block above it. Nothing enforced
the number, so nothing noticed. ``TestTheSharedMarkerConventionHolds`` below is
the enforcement the docstring never had.

Cite-by-name as well as by line throughout, for that reason. p52 gate 1.2 found
three stale line cites in this area, and ``LGR-a419843-2`` itself carries one.
Names verified against this tree:

* ``schema_mapping/lambda_function.py`` — ``render_sample_values_section``
  (def ``:983``), marker render ``:1013``, value join ``:1012``, convention
  docstring ``:990-992``; ``_truncate_sample_value`` (def ``:951``), newline
  collapse ``:958`` — **not** ``:956``, which is the last docstring line (gate
  1.2's correction).
* ``agentcore/complex_mapping_agent/main.py`` — ``build_evidence_prompt``
  (def ``:269``), marker render ``:292``, value join ``:291``; task 3.1's
  comment block moved these from ``:275``/``:274``. ``read_sample_data``
  (def ``:84``), newline collapse ``:133`` — was ``:116``.

=========================================================================
THE MEASURED FORMS
=========================================================================

Measured with ``.venv/bin/python`` against the real functions, field ``addr_1``
carrying values ``["a", "b"]``:

* ``render_sample_values_section`` — one line per field, the field name to the
  **left** of the marker::

      '  - addr_1: Sample values: ["a", "b"]'

* ``build_evidence_prompt`` — the marker on its **own** line, four-space
  indented, appended to the field description; the field name is on the line
  **above**::

      '  - addr_1 (type: string)\\n    Sample values: ["a", "b"]'

Marker-onward, the two are **byte-identical** — ``Sample values: ["a", "b"]``.
They differ only in prefix. That identity is precisely what makes one redactor
enough, and it is what ``test_both_producers_agree_byte_for_byte_from_the_marker_onward``
asserts.

The two field-name positions are worth pinning separately, because R6.3 (the
field name survives redaction) holds on the two shapes for **different**
structural reasons: on ``schema_mapping``'s the name precedes the marker and so
sits outside the discarded span; on ``build_evidence_prompt``'s it is on an
entirely different line. A producer that moved its name to the right of the
marker would satisfy neither.

=========================================================================
WHAT A RED HERE MEANS — MEASURED, NOT ASSUMED
=========================================================================

A failure in this file means **the form moved, so re-verify redaction** — not
"there is definitely a leak". Both readings occur, and the difference was
measured rather than reasoned about: each drift below was applied to a *copy* of
the producer in ``/tmp``, loaded through this file's own loader, and rendered.
Every one of the seven fails a pin in this file.

| Drift applied to a producer | Values still removed? | Cost |
|---|---|---|
| marker renamed (``Sample data: [``), either producer | **no** | total leak, silent |
| value list wrapped onto its own line | **no** | leak of the whole list; count reads ``0`` |
| single quotes instead of double | yes | count reads ``0`` — R6.2 lost silently |
| field name moved right of the marker | yes | field name destroyed — R6.3 lost |
| ``build_evidence_prompt``'s two lines collapsed into one | yes | none; the name stays left of the marker |
| ``build_evidence_prompt``'s indent changed | yes | none |
| one producer's list spacing changed | yes | the two producers diverge; one redactor no longer covers both by construction |

The last three are the reason the pins are strict rather than tolerant. Two of
them cost nothing today, and a tolerant pin that let them through would also let
through the four above, which cost a leak or a diagnostic. Strict-and-explain is
the trade: a red here is cheap to read and cheap to re-pin, and the failure
messages say which of the two readings applies.

=========================================================================
DELIBERATELY LEFT TO TASK 3.2
=========================================================================

* Generated values over a hostile alphabet (``"``, ``]``, ``,``, ``\\n``,
  ``\\r``, non-ASCII), on both producers — Property 4.
* Inertness without the marker over arbitrary text — Property 5.
* The count's two forms, exact and degraded, and why a bare ``0`` is forbidden
  where values were present — Property 6.
* The **raw-newline residue** and the fact that producer-side normalisation is
  what closes it. That is 3.2's ``TestRawNewlineResidueIsTheDocumentedLimit``,
  including the case that fails if either producer's
  ``.replace("\\n", " ")`` is deleted. Not repeated here.

=========================================================================
LOADING THE PRODUCERS
=========================================================================

Both producer modules are exec'd from their file path under a **private alias**
and are deliberately **not** published into ``sys.modules``, the approach task
1.1 recorded and task 3.2 reused. The usual idiom for importing
``schema_mapping/lambda_function.py`` is
``sys.modules.setdefault("env_utils", MagicMock(...))`` plus
``import lambda_function``; doing that from ``tests/unit/shared/`` would hand a
``MagicMock`` to every other test in this directory that imports the real
``env_utils``. Design risk N5 and ``LGR-c58f276-4`` name that import-order
pollution. The stubs here live inside a ``patch.dict`` and are gone once
``exec_module`` returns.

The loader is duplicated from task 3.2's file rather than imported from it, on
purpose: a canary must be able to fail on its own terms, and importing another
test module would make a collection error in either file take both, and would
share the ``MagicMock`` S3 client's mutable ``side_effect`` across files.

No ``moto.mock_aws()`` is created and no shared AWS fixture is needed: the
redactor is pure (no AWS call, no network call, no file read — R7.5) and neither
render function under test touches AWS. The ``MagicMock`` for ``boto3`` exists
only because ``complex_mapping_agent/main.py`` cannot be imported at all without
stubbing ``bedrock_agentcore``.

=========================================================================
LEDGER GAP
=========================================================================

Stated as a gap, not as an all-clear. ``why`` on ``shared/agent_trace_plugin.py``
returns four broad snapshot commits (``2b45b72912``, ``7ad6ca1ad9``,
``7c18e81fcc``, ``b48a349b7b``), every one with ``records: []``, and
``decisions(scope=…)`` returns only ``LGR-d5bcee9-1`` (cdk.out) and
``LGR-819980f-1`` (the JDBC-credentials invariant, scoped ``shared/**``). The
**same** gap covers ``agentcore/complex_mapping_agent/main.py``. So nothing
ratified governs this format convention from either direction — which is the
reason to pin it in a test rather than to trust a record. ``LGR-1e135d2-4``'s
``revisit_when`` ("the trace table's retention/redaction posture changes") is the
record Part B answers to. Ledger re-confirmed at ``047778f``: 239 records, 239
template, 0 backfilled.

Requirements covered: R5.1, R5.4, R5.5, R6.1, R6.2, R6.3.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

#: ``tests/unit/shared/<this file>`` → ``agentic_data_ingestion/``.
_ROOT = Path(__file__).resolve().parents[3]

# Shared modules importable without the Lambda layer mount, as every other
# module in this directory does it.
sys.path.insert(0, str(_ROOT / "shared"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from agent_trace_plugin import _QUOTED_RUN, _SAMPLE_MARKER, _redact_sample_values

# ---------------------------------------------------------------------------
# Producer modules, loaded under private aliases (see the module docstring)
# ---------------------------------------------------------------------------


def _load_under_alias(alias: str, path: Path, stubs: dict[str, object]):
    """Exec *path* as a module named *alias*, without publishing it."""
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", stubs):
        spec.loader.exec_module(module)
    return module


_SCHEMA_MAPPING = _load_under_alias(
    "_p52_shape_schema_mapping_producer",
    _ROOT / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py",
    {
        "eventbridge_utils": MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
        "env_utils": MagicMock(require_env=lambda name, default=None: default or name),
        "artifact_schema": MagicMock(),
        "metrics_facade": MagicMock(PipelineMetrics=None),
        "mapping_memory": MagicMock(MappingMemoryClient=None),
    },
)

_COMPLEX_MAPPING = _load_under_alias(
    "_p52_shape_complex_mapping_producer",
    _ROOT / "agentcore" / "complex_mapping_agent" / "main.py",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": MagicMock(),
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
        "mapping_memory": MagicMock(),
    },
)


# ---------------------------------------------------------------------------
# The pinned literal forms
# ---------------------------------------------------------------------------

_FIELD = "addr_1"
_VALUES = ["a", "b"]

#: ``render_sample_values_section:1013`` — the field name left of the marker,
#: one line per field.
_SCHEMA_MAPPING_LINE = '  - addr_1: Sample values: ["a", "b"]'
_SCHEMA_MAPPING_LINE_REDACTED = "  - addr_1: Sample values: [2 values redacted]"

#: ``build_evidence_prompt:292`` — ``desc += f"\\n    Sample values: [...]"``,
#: so the marker owns its line and the field name is on the one above.
_COMPLEX_MAPPING_BLOCK = '  - addr_1 (type: string)\n    Sample values: ["a", "b"]'
_COMPLEX_MAPPING_BLOCK_REDACTED = "  - addr_1 (type: string)\n    Sample values: [2 values redacted]"

#: What a reader is told to search for, per R6.4. Held separately from
#: ``_SAMPLE_MARKER`` so that a change to the redactor's constant alone cannot
#: make the convention assertions vacuous.
_MARKER_LITERAL = "Sample values: ["


# ---------------------------------------------------------------------------
# Rendering through the real producers
# ---------------------------------------------------------------------------


def _render_schema_mapping(field_values: dict[str, list[str]]) -> str:
    """``render_sample_values_section``'s whole block, via the real function."""
    source_schema = {"properties": {name: {"type": "string"} for name in field_values}}
    return _SCHEMA_MAPPING.render_sample_values_section(source_schema, field_values)


def _render_complex_mapping(field_values: dict[str, list[str]]) -> str:
    """``build_evidence_prompt``'s Source Schema block, via the real function.

    Task 3.2 drives ``read_sample_data`` and ``build_evidence_prompt`` as a pair,
    because that producer's newline normalisation lives at *read* time and its
    property is about normalisation. This file's subject is the **rendered
    form**, which is the render's alone, so it enters at ``build_evidence_prompt``
    with values that are already normalised.
    """
    source_schema = {"fields": [{"name": name, "type": "string"} for name in field_values]}
    target_schema = {"fields": [{"name": "street_address", "type": "string"}]}
    prompt = _COMPLEX_MAPPING.build_evidence_prompt(source_schema, target_schema, field_values, [])
    head = "## Source Schema\n"
    tail = "\n\n## Target Schema"
    assert head in prompt and tail in prompt, f"build_evidence_prompt's section headings moved: {prompt!r}"
    return prompt.split(head, 1)[1].split(tail, 1)[0]


def _marker_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if _MARKER_LITERAL in line]


# ---------------------------------------------------------------------------
# schema_mapping — R5.4's producer
# ---------------------------------------------------------------------------


class TestSchemaMappingRenderedForm:
    """``render_sample_values_section``'s literal form, pinned.

    R5.4's producer: the prompt that ``schema_mapping/lambda_function.py``
    appends to ``trace_plugin._model_calls`` by hand, bypassing the strands hook.

    Covers R5.1, R5.4, R6.1, R6.2, R6.3.
    """

    def test_rendered_marker_line_matches_the_pinned_literal_form(self):
        """The canary. A producer format change fails **here**.

        Compared against a render of the real function, so the pin cannot pass
        by agreeing with another hand-written string.
        """
        lines = _marker_lines(_render_schema_mapping({_FIELD: _VALUES}))

        assert lines == [_SCHEMA_MAPPING_LINE], (
            "render_sample_values_section (lambda_function.py:1013) no longer emits the form "
            "shared/agent_trace_plugin.py's _redact_sample_values is keyed on.\n"
            f"  expected: {_SCHEMA_MAPPING_LINE!r}\n"
            f"  rendered: {lines!r}\n"
            "This is not a stale test to update in place. Until the redactor's marker and "
            "line boundary are changed to match, sampled customer values reach the "
            "agent_traces table unredacted. Fix the redactor, or restore the form, then "
            "update this pin."
        )

    def test_redacting_the_rendered_form_gives_the_pinned_output(self):
        """R5.1 and R6.1/R6.2: values gone, count and marker kept."""
        rendered = _render_schema_mapping({_FIELD: _VALUES})
        redacted = _redact_sample_values(rendered)

        assert _marker_lines(redacted) == [_SCHEMA_MAPPING_LINE_REDACTED], (
            f"redaction of the real render changed shape.\n  rendered: {rendered!r}\n  redacted: {redacted!r}"
        )
        for value in _VALUES:
            assert f'"{value}"' not in redacted, f"R5.1: rendered value {value!r} survived redaction"

    def test_the_field_name_precedes_the_marker_on_the_same_line(self):
        """R6.3, and *why* it holds on this shape.

        The name sits to the **left** of the marker, so it is outside the span
        marker-to-end-of-line discards. A producer that moved it to the right
        would have it deleted with the values, silently costing the trace the
        field it was rendered for.
        """
        line = _marker_lines(_render_schema_mapping({_FIELD: _VALUES}))[0]

        assert line.index(_FIELD) < line.index(_MARKER_LITERAL), (
            f"the field name is no longer left of the marker: {line!r}. "
            "Redaction discards marker-to-end-of-line, so a name to the right of the "
            "marker is destroyed and R6.3 fails."
        )
        assert _FIELD in _redact_sample_values(line), "R6.3: the field name must survive redaction"

    def test_one_marker_line_per_sampled_field(self):
        """A real ``prompt_preview`` carries one block per sampled field.

        Pinned because the redactor loops over occurrences; a producer that
        merged fields onto one line, or split one field across two, changes what
        a single discard covers.
        """
        rendered = _render_schema_mapping({"addr_1": ["a"], "city": ["Seattle"], "cust_ref": ["x"]})

        assert _marker_lines(rendered) == [
            '  - addr_1: Sample values: ["a"]',
            '  - city: Sample values: ["Seattle"]',
            '  - cust_ref: Sample values: ["x"]',
        ], f"one marker line per sampled field no longer holds: {rendered!r}"


# ---------------------------------------------------------------------------
# complex_mapping_agent — R5.5's producer, the older exposure
# ---------------------------------------------------------------------------


class TestComplexMappingRenderedForm:
    """``build_evidence_prompt``'s literal form, pinned.

    R5.5's producer, and the **older** exposure: live since P13
    (``b48a349b7b``) via ``hooks=[trace_plugin]``, which p49 widened rather than
    created.

    Covers R5.1, R5.5, R6.1, R6.2, R6.3.
    """

    def test_rendered_marker_block_matches_the_pinned_literal_form(self):
        """The canary for the second producer.

        ``desc += f"\\n    Sample values: [{sample_str}]"`` (``main.py:292``), so
        the whole two-line unit is pinned: the marker's own line and the field
        description above it that carries the name.
        """
        block = _render_complex_mapping({_FIELD: _VALUES})

        assert block == _COMPLEX_MAPPING_BLOCK, (
            "build_evidence_prompt (main.py:292) no longer emits the form "
            "shared/agent_trace_plugin.py's _redact_sample_values is keyed on.\n"
            f"  expected: {_COMPLEX_MAPPING_BLOCK!r}\n"
            f"  rendered: {block!r}\n"
            "Until the redactor is changed to match, sampled customer values reach the "
            "agent_traces table unredacted on the complex-mapping path — the path that has "
            "carried this exposure the longest."
        )

    def test_redacting_the_rendered_form_gives_the_pinned_output(self):
        """R5.1 and R6.1/R6.2 on the second producer."""
        block = _render_complex_mapping({_FIELD: _VALUES})
        redacted = _redact_sample_values(block)

        assert redacted == _COMPLEX_MAPPING_BLOCK_REDACTED, (
            f"redaction of the real render changed shape.\n  rendered: {block!r}\n  redacted: {redacted!r}"
        )
        for value in _VALUES:
            assert f'"{value}"' not in redacted, f"R5.1: rendered value {value!r} survived redaction"

    def test_the_field_name_sits_on_the_line_above_the_marker(self):
        """R6.3, and *why* it holds on this shape — a different reason.

        Here the marker owns its line and the name is on the preceding one, so
        the name survives because the discard never reaches that line at all.
        Collapsing the two lines into one — a plausible "tidy-up" — would put the
        name inside the discarded span.
        """
        block = _render_complex_mapping({_FIELD: _VALUES})
        lines = block.splitlines()

        assert len(lines) == 2, f"the field description is no longer two lines: {block!r}"
        assert _MARKER_LITERAL not in lines[0], f"the marker moved onto the name's line: {block!r}"
        assert _FIELD in lines[0] and _FIELD not in lines[1], (
            f"the field name is no longer on the line above the marker: {block!r}. "
            "If it moves onto the marker's line to the right of the marker, redaction "
            "destroys it and R6.3 fails."
        )
        assert _FIELD in _redact_sample_values(block), "R6.3: the field name must survive redaction"

    def test_a_field_with_no_samples_renders_no_marker(self):
        """``if samples:`` at ``main.py:290`` — no block, so nothing to redact.

        Pinned so that a producer which started rendering an empty list for every
        field would show up here rather than as an unexplained crop of
        ``0 values redacted`` rows in the traces table.
        """
        block = _render_complex_mapping({_FIELD: []})

        assert block == "  - addr_1 (type: string)", f"an unsampled field now renders something: {block!r}"
        assert _redact_sample_values(block) == block, "R5.6: no marker, so redaction is inert"


# ---------------------------------------------------------------------------
# The convention itself — the load-bearing fact
# ---------------------------------------------------------------------------


class TestTheSharedMarkerConventionHolds:
    """One redactor is sufficient **because** both producers share one marker.

    ``render_sample_values_section``'s docstring
    (``lambda_function.py:990-992``) states this as intent: it renders "in
    ``build_evidence_prompt``'s shape … so a reader comparing the two prompts
    sees one convention." Nothing enforced it until this class — and that same
    docstring's ``build_evidence_prompt:275`` cite has already rotted to
    ``:292``, which is what an unenforced convention looks like.

    Covers R5.1, R5.4, R5.5, R6.2.
    """

    def test_both_producers_agree_byte_for_byte_from_the_marker_onward(self):
        """The sharpest statement of the convention.

        The two shapes differ only in prefix — ``'  - addr_1: '`` against
        ``'    '``. From the marker to end of line they are identical, and that
        identity is exactly what lets one marker-keyed redactor serve both. If
        this fails, the two producers have diverged and **one redactor is no
        longer enough**, whatever the per-producer tests above say.
        """
        schema_line = _marker_lines(_render_schema_mapping({_FIELD: _VALUES}))[0]
        cma_line = _marker_lines(_render_complex_mapping({_FIELD: _VALUES}))[0]

        schema_tail = schema_line[schema_line.index(_MARKER_LITERAL) :]
        cma_tail = cma_line[cma_line.index(_MARKER_LITERAL) :]

        assert schema_tail == cma_tail == 'Sample values: ["a", "b"]', (
            "the two producers no longer render the same Sample_Block from the marker onward.\n"
            f"  schema_mapping:1013     → {schema_tail!r}\n"
            f"  build_evidence_prompt:292 → {cma_tail!r}\n"
            "One redactor covered both only because these were identical. They are not, so "
            "shared/agent_trace_plugin.py now needs to handle two forms — or the divergence "
            "needs reverting."
        )

    def test_the_redactors_marker_constant_is_what_both_producers_emit(self):
        """Ties the producers' text to the redactor's key, in both directions.

        ``_SAMPLE_MARKER`` is read from the plugin rather than retyped, so this
        fails if **either** side moves independently of the other.
        """
        assert _SAMPLE_MARKER == _MARKER_LITERAL, (
            f"the redactor's marker constant changed to {_SAMPLE_MARKER!r}. Both producers "
            f"still emit {_MARKER_LITERAL!r}, so redaction now finds nothing."
        )
        for label, rendered in (
            ("render_sample_values_section:1013", _render_schema_mapping({_FIELD: _VALUES})),
            ("build_evidence_prompt:292", _render_complex_mapping({_FIELD: _VALUES})),
        ):
            assert _SAMPLE_MARKER in rendered, (
                f"{label} no longer emits the redactor's marker {_SAMPLE_MARKER!r}: {rendered!r}"
            )

    def test_both_producers_close_the_value_list_on_the_markers_own_line(self):
        """Marker-to-end-of-line is total only while the list stays on one line.

        Pinned because the plausible drifts here are invisible otherwise: a
        pretty-printed JSON array, or a wrap at some column, would leave every
        value after the first break outside the discard on an **unmarked** line,
        which no later marker-keyed pass reaches. Task 3.2's
        ``TestRawNewlineResidueIsTheDocumentedLimit`` pins what that residue
        looks like; this pins that no producer emits it.
        """
        for label, rendered in (
            ("render_sample_values_section:1013", _render_schema_mapping({_FIELD: _VALUES})),
            ("build_evidence_prompt:292", _render_complex_mapping({_FIELD: _VALUES})),
        ):
            line = _marker_lines(rendered)[0]
            assert line.endswith("]"), f"{label}: the value list no longer closes on the marker's line: {line!r}"
            assert line.count(_MARKER_LITERAL) == 1, f"{label}: two markers on one line: {line!r}"

    def test_both_producers_render_values_as_comma_separated_quoted_runs(self):
        """R6.2's count depends on this, and on nothing else.

        ``_QUOTED_RUN`` (``agent_trace_plugin.py``) counts complete ``"…"`` runs
        in the discarded span. Bare, single-quoted or bracket-delimited values
        would make the count read ``0`` while the values were still removed —
        a silent loss of the diagnostic fact R6 exists to keep, with no other
        symptom.
        """
        for label, rendered in (
            ("render_sample_values_section:1012", _render_schema_mapping({_FIELD: _VALUES})),
            ("build_evidence_prompt:291", _render_complex_mapping({_FIELD: _VALUES})),
        ):
            line = _marker_lines(rendered)[0]
            span = line[line.index(_MARKER_LITERAL) + len(_MARKER_LITERAL) : -1]
            assert span == '"a", "b"', f"{label}: the value list separator or quoting changed: {span!r}"
            assert _QUOTED_RUN.findall(span) == ['"a"', '"b"'], (
                f"{label}: _QUOTED_RUN no longer counts one run per value in {span!r}, so the "
                "count in every redacted trace is wrong."
            )


# ---------------------------------------------------------------------------
# What a format change actually costs — the stakes, not new coverage
# ---------------------------------------------------------------------------


class TestAFormatChangeSilentlyDisablesRedaction:
    """Why the pins above are assertions rather than comments.

    Task 3.2's Property 5 already establishes that unmarked text is returned
    unchanged, and there that is **correct behaviour** — it is what makes
    applying the redactor to all four Preview_Fields free. The same fact carries
    the opposite meaning here: applied to a *drifted producer's* output,
    inertness means every sampled value persists.

    So this class does not re-cover Property 5. It takes a **real** render,
    applies a mutation a producer plausibly would, and shows the cost — which no
    generated arbitrary string can show, because the values in it are not values
    a customer had.
    """

    _DISTINCTIVE = ["123 Main St", "APT-4-SECRET"]

    @pytest.mark.parametrize(
        ("mutation", "description"),
        [
            ("Sample values:[", "the space after the colon dropped"),
            ("Sample Values: [", "the value word capitalised"),
            ("Sample data: [", "the marker renamed"),
            ("Sample values = [", "the colon changed to an equals sign"),
        ],
    )
    def test_a_drifted_marker_leaves_every_value_in_the_trace(self, mutation: str, description: str):
        rendered = _render_schema_mapping({_FIELD: self._DISTINCTIVE})
        assert "APT-4-SECRET" in rendered, "the fixture, not the redactor, is wrong"

        # Redaction of the real render removes the values, as it must.
        assert "APT-4-SECRET" not in _redact_sample_values(rendered)

        # The same render with only the marker text changed, as a producer edit
        # would leave it: redaction becomes a no-op and the value persists.
        drifted = rendered.replace(_MARKER_LITERAL, mutation)
        assert _redact_sample_values(drifted) == drifted, (
            f"with {description}, the redactor is inert — which is the point: "
            "nothing raises, nothing logs, and the trace keeps the values."
        )
        assert "APT-4-SECRET" in _redact_sample_values(drifted), (
            f"with {description}, a sampled customer value persists into the agent_traces "
            "table for 90 days. This is the failure mode the pinned literal forms above "
            "exist to catch at review time instead."
        )
