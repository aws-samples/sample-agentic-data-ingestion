# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the sample reader IS the generated Glue job's reader.

Spec ``p44-verify-against-real-source-data``, task 1.1 — the characterization
layer that lands before anything routes a real read.

THE CLAIM THIS PINS, AND WHY NOTHING PINNED IT
----------------------------------------------
``glue_session_runner._SAMPLE_READERS``' docstring says its expressions are
"**character-for-character** the reader the generated Glue job uses", and that
this "is the whole point rather than a convenience: this executor exists to
verify the transform against the runtime the job will actually run on, and a
frame read with different options is a different frame".

Nothing tested it. p42 built the whole sample read path and deliberately wired no
caller, so ``build_sample_frame_code``'s output has never executed against Spark
and its only coverage is string-level — the spec's design §6.1 records this as
zero-coverage finding 1. p44 connects the caller, which turns the docstring's
claim from documentation into behaviour: from then on a drift between this map
and the templates means the verification measures a frame the production job
never produces, and reports it as green.

THE ORACLE IS THE TEMPLATES, NEVER ``_SAMPLE_READERS``
-----------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**) is the governing record: a contract
test derives its expectation from the producer's source because "a test written
from the producer's output cannot detect the producer drifting, because the
output is its own oracle". So the four S3-source templates are parsed at test
time and their ``{% if source_format == … %}`` branches are the expectation;
``_SAMPLE_READERS`` is the subject. ``test_transform_namespace_matches_templates``
is the in-tree precedent for the technique, on the same five templates, and this
file follows its shape deliberately — named templates rather than a glob, an
explicit non-vacuity section, and every assertion message naming what it looked
for.

Direction, unlike that file's, is **equality per format**: there is no union to
be permissive about. Each of the four templates carries the identical five-way
read, and one of the five entries is the ``{% else %}``.

THE ONE DELIBERATE DIVERGENCE, ASSERTED RATHER THAN TIDIED
----------------------------------------------------------
The templates use the json reader as their ``{% else %}`` catch-all.
:func:`glue_session_runner.sample_read_format` returns ``None`` for an
unrecognised extension instead, and that asymmetry is correct on both sides:

* the templates are rendered with a ``source_format`` that ``file_validation``
  already classified, and an extension outside ``FORMAT_MAP`` never reaches a
  render — ``LGR-60c4985-7`` (**template / ratified**) records that it writes an
  ``unsupported`` audit row and then raises, which Step Functions sees as a
  permanent ``StepFailure``. So the ``else`` is unreachable-in-practice cover,
  not a guess.
* ``sample_read_format`` is given only a URI. Guessing json anyway would build a
  frame of Spark ``_corrupt_record`` rows and report the transform's failure
  against it at ``STAGE_FRAME_BUILD`` — a harness limitation wearing a
  data-generation failure's clothes.

So this file asserts the five formats agree **and** that the divergence is
exactly the ``else`` branch, so a later reader cannot tidy it into a bug.

AMENDED BY p55 R4.1 — A SECOND PLACEHOLDER, NOT A WEAKER CONTRACT
-----------------------------------------------------------------
The csv read on both sides gained ``.option("sep", …)``: the templates spell it
``{{ source_delimiter | tojson }}`` and ``_SAMPLE_READERS['csv']`` spells it
``{sep}``. This file is the reason p55's tasks 2.6 and 2.7 are one landing — it
is exactly what fails when one side changes without the other, which is the job
it was written for.

What changed here is one constant (:data:`TEMPLATE_SEP_EXPRESSION`) and one
default argument (:func:`_rendered`). The equality is still byte-equality and
still per format; there are simply two substituted positions now instead of one.
:meth:`TestTheParseIsNonVacuous.test_the_csv_reader_names_the_delimiter_placeholder_exactly_once`
is added so that "both sides lost the option together" cannot pass silently,
which is the one drift a pure equality cannot see. Nothing was widened to
``in``, no format was dropped, and no assertion was deleted — `LGR-60c4985-6`
and `LGR-9e76249-10` (both **template / ratified**) require a deliberate change
to an assertion to stay legible as one.

AMENDED AGAIN BY p55 R5.1 — THE MIRROR IS NO LONGER A SUBSET OF WHAT WE READ
---------------------------------------------------------------------------
``_SOURCE_FORMAT_BY_EXTENSION`` gained ``xlsx → "excel"`` (p55 task 4.5) because
R5.1/R5.2 require it to carry an entry for every ``FORMAT_MAP`` extension, and no
template branches on ``excel`` because Spark opens no Excel workbook. One test is
amended for it — :meth:`TestTheParseIsNonVacuous.test_the_extension_map_only_resolves_formats_the_templates_can_read`
— and the amendment keeps the original claim in full over every other format while
asserting a *stricter* one over the exempted population. See
:data:`NOT_SAMPLE_READABLE_FORMATS` and that method's docstring. Every other
assertion in this file is untouched and still passes, including
:meth:`TestTheSampleReadersAreTheTemplates.test_the_divergence_from_the_templates_is_exactly_the_else_branch`,
whose ``.xlsx`` case still resolves to ``None`` — for the newly stated reason that
this module has no reader for it rather than for the old one that the extension was
unknown.

``glue_job_jdbc_source.py.j2`` IS EXCLUDED, AND THAT IS AN INVARIANT
--------------------------------------------------------------------
Its reader is ``spark.read.format("jdbc")`` (``:187``, ``:196``, ``:205``) and it
carries no ``source_format`` branch at all. ``LGR-819980f-1`` (**standing
invariant**) is why no arm of this seam may grow one: JDBC credentials are only
ever referenced by Secrets Manager ARN, a verification session is granted none,
and the spec's do-NOT 3 forbids a JDBC arm in ``build_frame_code``.
:func:`test_no_sample_reader_mentions_jdbc` is that invariant's executable form
here, and :func:`test_the_excluded_template_reads_jdbc_and_has_no_source_format_branch`
pins that the exclusion is about a real, different reader rather than about a
template that happens not to matter.

**Validates: Requirements 3.5**

No Glue session is created, no S3 object is read and no data source is contacted:
everything below is a file read plus one import of a module that touches no AWS
API at import time.
"""

import json
import re
import sys
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES_DIR = _REPO_ROOT / "pyspark_templates"

#: The four templates whose source read is an S3 read, named rather than globbed
#: for the reason ``test_transform_namespace_matches_templates.py`` names its
#: five: a glob that matched nothing would make this file vacuous, and a glob
#: that picked up a sixth template would silently widen the oracle.
S3_SOURCE_TEMPLATE_NAMES = (
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_s3_tables.py.j2",
)

#: The fifth template, excluded. Its source is a JDBC connection, not an object.
JDBC_SOURCE_TEMPLATE_NAME = "glue_job_jdbc_source.py.j2"

#: The expression the templates pass to their reader. ``_SAMPLE_READERS`` spells
#: the same position ``{uri}``, and "modulo the URI placeholder" below means
#: exactly this substitution and nothing else.
TEMPLATE_URI_EXPRESSION = "source_path"

#: **AMENDED by p55 R4.1 — this contract now has TWO placeholders, not one.**
#:
#: The templates' csv branch passes ``.option("sep", {{ source_delimiter |
#: tojson }})`` so a TSV is separated on tabs rather than silently read as one
#: column, and ``_SAMPLE_READERS['csv']`` gained ``.option("sep", {sep})`` in
#: the same landing (p55 tasks 2.6 and 2.7, which are one change precisely
#: because this file fails if they are split).
#:
#: The contract is NOT relaxed. Byte-equality still holds exactly; there are now
#: two positions substituted instead of one, and this constant is the literal
#: template text that fills the second. Rendering ``_SAMPLE_READERS`` with the
#: template's own Jinja expression — rather than with a delimiter — keeps the
#: comparison a comparison of the two READ EXPRESSIONS, so a change to either
#: side's option list, order or spelling still fails here. Widening the
#: assertion to "contains" or dropping the csv case would have been the
#: relaxation, and `LGR-60c4985-6` / `LGR-9e76249-10` forbid it: a deliberate
#: change to an assertion stays legible as a change and is never deleted.
TEMPLATE_SEP_EXPRESSION = "{{ source_delimiter | tojson }}"

#: The formats under contract: the four the templates branch on explicitly, plus
#: ``json``, which they reach through ``{% else %}``. Transcribed rather than
#: derived so that a *missing* format fails an assertion with a readable message
#: instead of shrinking a Hypothesis domain silently — and checked against both
#: sides by :class:`TestTheParseIsNonVacuous`.
CONTRACTED_FORMATS = ("csv", "parquet", "avro", "orc")
JSON_CATCH_ALL_FORMAT = "json"
ALL_CONTRACTED_FORMATS = (*CONTRACTED_FORMATS, JSON_CATCH_ALL_FORMAT)

#: **AMENDED by p55 R5.1 (task 4.5).** Canonical_Formats the mirror
#: ``_SOURCE_FORMAT_BY_EXTENSION`` knows and this module deliberately cannot read
#: — the **Conversion_Required_Set**. No Glue_Job_Template branches on them
#: because no Spark reader opens them; ``schema_extract`` converts the object and
#: routes the converted URI instead (p55 R13.3).
#:
#: A transcribed list, which is normally the shape ``LGR-b46a5bb-4`` calls a door.
#: It is one here because the alternative is worse and because the door is bolted
#: from the other side:
#:
#: * dropping the list and asserting only over what ``sample_read_format`` can
#:   *return* would silently accept a native format added to ``FORMAT_MAP`` and the
#:   mirror with no template branch — the exact drift this test was written for,
#:   since a format with no reader is declined by the resolver either way;
#: * adding a name here is not free: ``test_format_vocabulary_cross_lambda.py``'s
#:   guard 5 (p55 R13.6) requires every Conversion_Required_Set member to reach a
#:   ``convert_source_to_*`` call in ``schema_extract``, so a format smuggled in
#:   here to silence this file fails there instead.
#:
#: :meth:`TestTheParseIsNonVacuous.test_the_extension_map_only_resolves_formats_the_templates_can_read`
#: asserts every member is genuinely unreadable rather than merely exempted.
NOT_SAMPLE_READABLE_FORMATS = ("excel",)

_IF_SOURCE_FORMAT = re.compile(r"^\{%-?\s*if\s+source_format\s*==\s*'([^']+)'\s*-?%\}$")
_ELIF_SOURCE_FORMAT = re.compile(r"^\{%-?\s*elif\s+source_format\s*==\s*'([^']+)'\s*-?%\}$")
_ANY_IF = re.compile(r"^\{%-?\s*if\s")
_ANY_ELIF = re.compile(r"^\{%-?\s*elif\s")
_ELSE = re.compile(r"^\{%-?\s*else\s*-?%\}$")
_ENDIF = re.compile(r"^\{%-?\s*endif\s*-?%\}$")
_READER_ASSIGNMENT = re.compile(r"^\s*df\s*=\s*(\S.*?)\s*$")


def _template_source(name: str) -> str:
    path = TEMPLATES_DIR / name
    assert path.is_file(), (
        f"template {name} not found at {path} — has it been renamed or removed? "
        "S3_SOURCE_TEMPLATE_NAMES in this file is the list under contract."
    )
    return path.read_text(encoding="utf-8")


def _source_format_branches(name: str) -> tuple[dict[str, str], str | None]:
    """The ``source_format`` reader per branch in *name*, plus the ``else`` one.

    A Jinja walk rather than a render, for the reason the sibling contract test
    gives for not calling ``ast.parse`` on a whole template: these files are not
    Python, so ``{% if %}`` is a syntax error. Rendering them instead would need
    the full variable set five templates expect, and would answer a different
    question — what one render produced, not what every branch says.

    Nesting-aware on purpose. ``glue_job_s3_tables.py.j2`` carries other ``{% if
    %}`` blocks, so ``{% else %}`` and ``{% endif %}`` are only read as this
    block's when the depth says they are. An ``{% elif %}`` at this block's own
    depth that is not a ``source_format`` test fails loudly rather than being
    silently attributed to the preceding branch.
    """
    lines = _template_source(name).splitlines()

    openings = [i for i, line in enumerate(lines) if _IF_SOURCE_FORMAT.match(line.strip())]
    assert len(openings) == 1, (
        f"expected exactly one `{{% if source_format == … %}}` block in {name}, found "
        f"{len(openings)} at lines {[i + 1 for i in openings]}. The extraction in this file "
        f"reads the first one only, so a second block would go unchecked."
    )

    index = openings[0]
    opening = _IF_SOURCE_FORMAT.match(lines[index].strip())
    assert opening is not None  # narrowed by the search above
    current: str | None = opening.group(1)
    branches: dict[str, str] = {}
    else_expression: str | None = None
    depth = 1

    index += 1
    while index < len(lines):
        stripped = lines[index].strip()

        if _ENDIF.match(stripped):
            depth -= 1
            if depth == 0:
                break
            index += 1
            continue

        if _ANY_IF.match(stripped):
            depth += 1
            index += 1
            continue

        if depth == 1:
            elif_match = _ELIF_SOURCE_FORMAT.match(stripped)
            if elif_match:
                current = elif_match.group(1)
                index += 1
                continue
            assert not _ANY_ELIF.match(stripped), (
                f"{name} line {index + 1} is an `{{% elif %}}` inside the source_format block "
                f"that does not test source_format: {stripped!r}. Attributing the reader after "
                f"it to the preceding branch would be wrong, so this extraction refuses to guess."
            )
            if _ELSE.match(stripped):
                current = None
                index += 1
                continue

            reader = _READER_ASSIGNMENT.match(lines[index])
            if reader:
                expression = reader.group(1)
                if current is None:
                    else_expression = expression
                else:
                    branches[current] = expression

        index += 1

    assert depth == 0, (
        f"the `{{% if source_format == … %}}` block in {name} has no matching `{{% endif %}}` at "
        f"its own depth — the extraction ran off the end of the file"
    )
    return branches, else_expression


def _template_reader_map(name: str) -> dict[str, str]:
    """Format → reader expression for *name*, with ``json`` from the ``else``.

    One dict per template so the four can be compared with each other before
    either is compared with the producer.
    """
    branches, else_expression = _source_format_branches(name)
    readers = dict(branches)
    if else_expression is not None:
        readers[JSON_CATCH_ALL_FORMAT] = else_expression
    return readers


def _glue_session_runner():
    """The module under test, imported the way the sibling contract test does.

    ``glue_session_runner`` imports with no environment, no AWS call and no
    mocking — the same property ``test_transform_namespace_matches_templates.py``
    relies on. Nothing is constructed here and no session exists.
    """
    for path in (
        _REPO_ROOT / "lambda" / "agentic_data_ingestion_test_code",
        _REPO_ROOT / "shared",
    ):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    import glue_session_runner

    return glue_session_runner


def _sample_readers() -> dict[str, str]:
    return _glue_session_runner()._SAMPLE_READERS


def _rendered(reader: str, uri_expression: str, sep_expression: str = TEMPLATE_SEP_EXPRESSION) -> str:
    """*reader* with its ``{uri}`` and ``{sep}`` filled, the production way.

    ``build_sample_frame_code`` emits
    ``reader.format(uri=json.dumps(source_uri), sep=json.dumps(delimiter_for_key(source_uri)))``
    (``glue_session_runner.py``, in ``build_sample_frame_code``), so the
    comparison below goes through ``str.format`` rather than a hand-rolled
    substitution — a change to the placeholder mechanics is then a failure here
    too, not an invisible pass.

    **p55 R4.1: ``sep`` defaults to the templates' own Jinja expression**, so
    every existing call site is unchanged and the equality below compares the
    two read expressions rather than two rendered delimiters. ``str.format``
    ignores a keyword no placeholder names, so the four non-csv readers are
    passed ``sep`` and are unaffected by it — which is also true in production
    and is why the delimiter costs those four formats nothing.
    """
    return reader.format(uri=uri_expression, sep=sep_expression)


class TestTemplatesAreReadable:
    """Guard 1 — a moved or renamed template fails loudly, not vacuously."""

    @pytest.mark.parametrize("template", S3_SOURCE_TEMPLATE_NAMES)
    def test_template_is_readable(self, template: str):
        assert (TEMPLATES_DIR / template).is_file(), f"{template} not found in {TEMPLATES_DIR}"

    def test_the_excluded_template_is_readable_too(self):
        """Named so its exclusion is a decision with a subject, not an omission."""
        assert (TEMPLATES_DIR / JDBC_SOURCE_TEMPLATE_NAME).is_file()


class TestTheParseIsNonVacuous:
    """Guards 2-5 — an oracle that found nothing is equal to nothing."""

    @pytest.mark.parametrize("template", S3_SOURCE_TEMPLATE_NAMES)
    def test_every_template_contributes_every_contracted_format(self, template: str):
        """Each template branches on the four formats and carries an ``else``.

        If a template stops doing so this fails here rather than quietly
        shrinking the expectation the contract below is checked against.
        """
        readers = _template_reader_map(template)

        missing = sorted(set(ALL_CONTRACTED_FORMATS) - set(readers))
        assert not missing, (
            f"{template} contributed no reader for {missing}. Either its source read was "
            f"restructured — in which case the generated job now reads differently and "
            f"_SAMPLE_READERS should follow — or the extraction in this file needs to follow a "
            f"reformatting. Found: {sorted(readers)}."
        )

        extra = sorted(set(readers) - set(ALL_CONTRACTED_FORMATS))
        assert not extra, (
            f"{template} branches on {extra}, which this contract does not cover. A new "
            f"source_format branch needs a matching _SAMPLE_READERS entry and a name in "
            f"ALL_CONTRACTED_FORMATS, or the sample frame stops being the frame the job produces."
        )

    @pytest.mark.parametrize("template", S3_SOURCE_TEMPLATE_NAMES)
    @pytest.mark.parametrize("read_format", ALL_CONTRACTED_FORMATS)
    def test_every_template_reader_names_the_uri_exactly_once(self, template: str, read_format: str):
        """ "Modulo the URI placeholder" has to mean one substitution, not several."""
        expression = _template_reader_map(template)[read_format]

        assert expression.count(TEMPLATE_URI_EXPRESSION) == 1, (
            f"{template}'s {read_format} reader mentions {TEMPLATE_URI_EXPRESSION!r} "
            f"{expression.count(TEMPLATE_URI_EXPRESSION)} times: {expression!r}. The comparison "
            f"below substitutes it once, so a second occurrence would make the byte-equality "
            f"claim mean something other than what it says."
        )

    @pytest.mark.parametrize("template", S3_SOURCE_TEMPLATE_NAMES)
    def test_the_csv_reader_names_the_delimiter_placeholder_exactly_once(self, template: str):
        """p55 R4.1 — the second substitution position, asserted on both sides.

        The equality below would also be satisfied by BOTH sides dropping the
        ``sep`` option together, and that is the one drift byte-equality cannot
        see: a TSV would go back to being read as a single column in the
        generated job AND in the verification, so the verification would agree
        with the job about a wrong answer. Same reasoning as
        :meth:`TestTheSampleReadersAreTheTemplates.test_the_csv_reader_still_declines_to_infer_the_schema`,
        which exists for the option whose loss is silent.

        "Exactly once" is what makes :func:`_rendered`'s single substitution mean
        what it says, mirroring the URI assertion above.
        """
        template_expression = _template_reader_map(template)["csv"]

        assert template_expression.count(TEMPLATE_SEP_EXPRESSION) == 1, (
            f"{template}'s csv reader spells the delimiter {template_expression.count(TEMPLATE_SEP_EXPRESSION)} "
            f"time(s): {template_expression!r}. p55 R4.1 requires exactly one "
            f'`.option("sep", {TEMPLATE_SEP_EXPRESSION})` — without it Spark defaults `sep` to a comma and '
            f"a TSV upload is read as ONE column named `name\\tage\\tcity`, which is the defect p55 W2 fixes."
        )
        assert _sample_readers()["csv"].count("{sep}") == 1, (
            f"_SAMPLE_READERS['csv'] is {_sample_readers()['csv']!r} and does not carry exactly one `{{sep}}` "
            f"placeholder. The templates pass a delimiter, so a sample read without one verifies the transform "
            f"against a frame the generated job never produces — for a TSV, a one-column frame."
        )

    def test_the_four_templates_agree_with_each_other(self):
        """One reader per format across all four, so "the job's reader" is well defined.

        Asserted before the producer is compared with any of them: if the four
        disagreed, ``_SAMPLE_READERS`` could match one and mismatch three while a
        single-template test passed.
        """
        by_format: dict[str, dict[str, list[str]]] = {}
        for template in S3_SOURCE_TEMPLATE_NAMES:
            for read_format, expression in _template_reader_map(template).items():
                by_format.setdefault(read_format, {}).setdefault(expression, []).append(template)

        disagreements = {read_format: variants for read_format, variants in by_format.items() if len(variants) > 1}

        assert not disagreements, (
            f"the S3-source templates disagree about how to read {sorted(disagreements)}: "
            f"{disagreements}. _SAMPLE_READERS carries one expression per format, so a "
            f"divergence between templates means the sample can only match some of the jobs. "
            f"Reconcile the templates, or this contract needs to become per-target-type — which "
            f"the runner cannot do today, since run_test's arity is fixed at three (C6)."
        )

    def test_the_extension_map_only_resolves_formats_the_templates_can_read(self):
        """Every extension the resolver recognises maps onto a template branch.

        ``_SOURCE_FORMAT_BY_EXTENSION`` collapses ``tsv`` onto ``csv`` and
        ``jsonl``/``ndjson`` onto ``json`` because the templates branch on the
        canonical name — the ``_SAMPLE_READERS`` docstring records both, and both
        produce the same odd frame in the real job, which is the point. A new
        extension resolving to a format no template branches on would make
        ``build_sample_frame_code`` raise ``FrameBuildError`` at run time; it
        fails here instead.

        **AMENDED by p55 R5.1 (task 4.5) — one exemption, and the exempted
        population is checked rather than skipped.** p55 R5.1/R5.2 require the
        mirror to carry an entry for **every** ``FORMAT_MAP`` extension, and
        ``xlsx → "excel"`` is one no template can branch on: Spark opens no Excel
        workbook. So the mirror stopped being a subset of what this module can read,
        and the blanket claim above stopped being true of it.

        Nothing is relaxed away. The original claim still holds in full over every
        format outside :data:`NOT_SAMPLE_READABLE_FORMATS` — a native format added
        with no template branch still fails here, which is the drift this test was
        written for. The exempted formats get a **stricter** claim instead of no
        claim: each must be absent from ``_SAMPLE_READERS``, absent from the
        templates, and **declined by** ``sample_read_format``. That last arm is what
        closes the ``FrameBuildError`` path structurally rather than by convention —
        the resolver cannot hand ``build_sample_frame_code`` a format it has no
        reader for, so the run falls back to a generated frame and says so, exactly
        as it did for an ``.xlsx`` before the vocabulary widened.

        Per ``LGR-60c4985-6`` / ``LGR-9e76249-10`` (both **template / ratified**),
        the amendment is stated here rather than made silently, and no assertion was
        deleted.
        """
        runner = _glue_session_runner()
        template_formats = set(_template_reader_map(S3_SOURCE_TEMPLATE_NAMES[0]))
        mirror = runner._SOURCE_FORMAT_BY_EXTENSION

        unreadable = {
            extension: read_format
            for extension, read_format in mirror.items()
            if read_format not in template_formats and read_format not in NOT_SAMPLE_READABLE_FORMATS
        }

        assert not unreadable, (
            f"sample_read_format can resolve {unreadable}, but the templates carry no branch for "
            f"those formats. Add the branch to all four templates and the reader to "
            f"_SAMPLE_READERS, or drop the extension. (If the format genuinely cannot be read from "
            f"the source object at all, it belongs in NOT_SAMPLE_READABLE_FORMATS — which obliges a "
            f"convert_source_to_* in schema_extract, per p55 R13.6.)"
        )

        # The exemption is checked, not trusted. Anti-vacuity first: an empty list
        # would make the three arms below pass over nothing.
        assert NOT_SAMPLE_READABLE_FORMATS, (
            "NOT_SAMPLE_READABLE_FORMATS is empty, so the arms below assert nothing. If no "
            "Canonical_Format needs conversion any more, delete this exemption and restore the "
            "blanket claim above rather than leaving an empty list in place."
        )

        phantom = sorted(set(NOT_SAMPLE_READABLE_FORMATS) - set(mirror.values()))
        assert not phantom, (
            f"NOT_SAMPLE_READABLE_FORMATS names {phantom}, which the mirror does not map any "
            f"extension to. An exemption for a format that cannot arrive widens this test's blind "
            f"spot for nothing; drop it."
        )

        for read_format in NOT_SAMPLE_READABLE_FORMATS:
            assert read_format not in runner._SAMPLE_READERS, (
                f"_SAMPLE_READERS has a reader for {read_format!r}, which NOT_SAMPLE_READABLE_FORMATS "
                f"says cannot be read. One of the two is wrong: if Spark can now read it, the four "
                f"templates need the branch and the exemption goes; if it cannot, the reader is dead "
                f"code that reads as capability."
            )
            assert read_format not in template_formats, (
                f"the templates branch on {read_format!r} while NOT_SAMPLE_READABLE_FORMATS exempts it "
                f"from this contract. The exemption exists BECAUSE no template can read it — a "
                f"template branch means the exemption is stale and this file must cover the format."
            )
            for extension, mapped in sorted(mirror.items()):
                if mapped != read_format:
                    continue
                assert runner.sample_read_format(f"s3://bucket/data.{extension}") is None, (
                    f"sample_read_format resolved a .{extension} URI to "
                    f"{runner.sample_read_format(f's3://bucket/data.{extension}')!r}, but no reader "
                    f"and no template branch exist for {read_format!r}. resolve_frame_source would "
                    f"then report FRAME_MODE_SAMPLE and build_sample_frame_code would raise "
                    f"FrameBuildError — a harness failure where a fallback to a generated frame is "
                    f"the documented answer (R9.2). The resolver must decline a format it cannot read."
                )

    def test_the_contracted_format_names_match_the_producer_exactly(self):
        """The transcription is checked against ``_SAMPLE_READERS``' own keys.

        This is the direction that catches a reader added here with no template
        branch behind it — the drift a template-only oracle cannot see, because
        an extra producer key is never asked about.
        """
        assert set(_sample_readers()) == set(ALL_CONTRACTED_FORMATS), (
            f"_SAMPLE_READERS carries {sorted(_sample_readers())} and this contract covers "
            f"{sorted(ALL_CONTRACTED_FORMATS)}. A reader with no matching template branch reads "
            f"the sample differently from the generated job; a branch with no reader raises "
            f"FrameBuildError on a real run."
        )

    def test_the_excluded_template_reads_jdbc_and_has_no_source_format_branch(self):
        """The exclusion has a subject: a real, different reader.

        Pinned so that the JDBC assertion below is about something. If this
        template ever grew a ``source_format`` branch, the exclusion would be
        hiding an S3 read from the contract.
        """
        source = _template_source(JDBC_SOURCE_TEMPLATE_NAME)

        assert 'spark.read.format("jdbc")' in source, (
            f"{JDBC_SOURCE_TEMPLATE_NAME} no longer reads over JDBC, so the reason this contract "
            f"excludes it has changed. Re-read LGR-819980f-1 before including it."
        )
        assert not [line for line in source.splitlines() if _IF_SOURCE_FORMAT.match(line.strip())], (
            f"{JDBC_SOURCE_TEMPLATE_NAME} has grown a `{{% if source_format == … %}}` branch. It "
            f"is excluded from this contract, so that branch would be unchecked — add it to "
            f"S3_SOURCE_TEMPLATE_NAMES deliberately, having read LGR-819980f-1 first."
        )


class TestTheSampleReadersAreTheTemplates:
    """The contract itself, as named examples beside the property below."""

    @pytest.mark.parametrize("read_format", CONTRACTED_FORMATS)
    def test_the_reader_for_an_explicitly_branched_format_is_the_templates(self, read_format: str):
        """R3.5, per format, over the four templates' agreed expression."""
        reader = _sample_readers()[read_format]

        for template in S3_SOURCE_TEMPLATE_NAMES:
            expected = _template_reader_map(template)[read_format]

            assert _rendered(reader, TEMPLATE_URI_EXPRESSION) == expected, (
                f"_SAMPLE_READERS[{read_format!r}] renders as "
                f"{_rendered(reader, TEMPLATE_URI_EXPRESSION)!r}, but {template} reads that format "
                f"with {expected!r}. A frame read with different options is a different frame, so "
                f"the verification would measure a frame the generated job never produces. Change "
                f"the template and _SAMPLE_READERS together, per that map's own docstring."
            )

    def test_the_json_reader_is_the_templates_else_expression(self):
        """``json`` is the catch-all's expression, which is where it comes from.

        Separated from the four so the ``else``'s double role is legible: it is
        both the json reader and the unrecognised-extension fallback, and only
        the first of those is reproduced here.
        """
        reader = _sample_readers()[JSON_CATCH_ALL_FORMAT]

        for template in S3_SOURCE_TEMPLATE_NAMES:
            _, else_expression = _source_format_branches(template)

            assert else_expression is not None, f"{template} has no `{{% else %}}` reader"
            assert _rendered(reader, TEMPLATE_URI_EXPRESSION) == else_expression, (
                f"_SAMPLE_READERS['json'] renders as {_rendered(reader, TEMPLATE_URI_EXPRESSION)!r} "
                f"but {template}'s `{{% else %}}` reads {else_expression!r}"
            )

    def test_the_csv_reader_still_declines_to_infer_the_schema(self):
        """The inherited behaviour the map's docstring calls out, pinned by name.

        ``inferSchema`` False means every csv column arrives as a string. That is
        what the job does, so it is what the transform must face — "improving" it
        here would verify against types the job never produces. Asserted
        explicitly because the byte-equality above would also be satisfied by
        changing the template *and* the reader together, and this is the one
        option whose loss is silent at the verification layer.
        """
        assert '.option("inferSchema", False)' in _sample_readers()["csv"], (
            f"_SAMPLE_READERS['csv'] is {_sample_readers()['csv']!r} and no longer declines to "
            f"infer the schema. Every csv column arrives as a string in the generated job, so a "
            f"sample read with inference verifies the transform against types the job never "
            f"produces — the map's own docstring calls this out as inherited deliberately."
        )
        for template in S3_SOURCE_TEMPLATE_NAMES:
            assert '.option("inferSchema", False)' in _template_reader_map(template)["csv"], (
                f"{template} now infers the csv schema. The sample reader must follow it, and the "
                f"transform will then face typed columns rather than strings."
            )

    def test_no_sample_reader_mentions_jdbc(self):
        """``LGR-819980f-1`` (**standing invariant**) at this seam, and do-NOT 3.

        A verification session is created with no ``Connections`` and its role
        holds no Secrets Manager action, so a JDBC reader here could not work —
        but the reason it must not exist is the credential surface, not the
        failure. ``glue_job_jdbc_source.py.j2``'s reader is the one this excludes.
        """
        offenders = {
            read_format: reader for read_format, reader in _sample_readers().items() if "jdbc" in reader.lower()
        }

        assert not offenders, (
            f"_SAMPLE_READERS carries a JDBC reader: {offenders}. LGR-819980f-1 is a standing "
            f"invariant — credentials reach Glue only by Secrets Manager ARN, and a verification "
            f"session is granted none. Do NOT add a JDBC arm to build_frame_code (do-NOT 3)."
        )

    def test_the_divergence_from_the_templates_is_exactly_the_else_branch(self):
        """The asymmetry, asserted so it cannot be tidied into a bug.

        The templates answer an unrecognised ``source_format`` with the json
        reader; ``sample_read_format`` answers an unrecognised *extension* with
        ``None``. Both are right for their own inputs — ``LGR-60c4985-7``
        (**template / ratified**) is why the templates can afford the catch-all,
        and ``sample_read_format``'s docstring is why this module cannot.
        """
        runner = _glue_session_runner()

        assert runner.sample_read_format("s3://bucket/data.json") == JSON_CATCH_ALL_FORMAT, (
            "a recognised .json extension must still resolve to the json reader — the divergence "
            "is about the catch-all, not about json"
        )
        assert runner.sample_read_format("s3://bucket/data.xlsx") is None, (
            "an extension outside _SOURCE_FORMAT_BY_EXTENSION must resolve to None, not to the "
            "templates' json catch-all. Reading it as json anyway would build a frame of "
            "_corrupt_record rows and report the transform's failure against it as test data "
            "generation."
        )
        assert runner.sample_read_format("s3://bucket/prefix/") is None, (
            "a prefix with no extension must resolve to None — the observed multi-file upload "
            "shape (file_validation/lambda_function.py:368)"
        )


# ---------------------------------------------------------------------------
# Feature: p44-verify-against-real-source-data, Property 11: The sample reader is
# the generated job's reader.
#
# For any format in _SAMPLE_READERS, the expression is byte-equal — modulo the
# URI placeholder — to the expression the four S3-source templates use for that
# source_format; no value contains "jdbc"; and the only divergence is the
# templates' {% else %} catch-all, which sample_read_format answers with None.
#
# The expectation is derived from the TEMPLATES, never from _SAMPLE_READERS
# (LGR-28b4d75-5).
# ---------------------------------------------------------------------------

_BUCKETS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=24).filter(
    lambda bucket: not bucket.startswith("-") and not bucket.endswith("-")
)

#: Keys worth generating: quotes and backslashes are the shapes
#: ``build_sample_frame_code``'s ``json.dumps`` exists for, so the substitution
#: claim is tested over them rather than over well-behaved names only.
_KEYS = st.text(
    alphabet=st.sampled_from([*"abcdefghijklmnopqrstuvwxyz0123456789/_-. ", '"', "\\", "'"]),
    min_size=1,
    max_size=40,
)

_UNRECOGNISED_EXTENSIONS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)


@settings(max_examples=200, deadline=None)
@given(
    read_format=st.sampled_from(ALL_CONTRACTED_FORMATS),
    template=st.sampled_from(S3_SOURCE_TEMPLATE_NAMES),
    bucket=_BUCKETS,
    key=_KEYS,
)
def test_property_11_the_sample_reader_is_the_generated_jobs_reader(
    read_format: str, template: str, bucket: str, key: str
):
    """**Validates: Requirements 3.5**

    Property 11, the equality half. For any contracted format, any of the four
    S3-source templates and any object URI, the reader ``_SAMPLE_READERS``
    renders is the template's own reader with the URI substituted into the
    position the template puts ``source_path`` in — and it never mentions JDBC.

    The URI dimension is what makes this a property rather than a table: the
    substitution goes through ``json.dumps`` in production
    (``glue_session_runner.py:2057``), so a key carrying a quote or a backslash
    is the case where a hand-rolled placeholder would emit code that reads a
    *different object* — silently verifying against something else.
    """
    uri = f"s3://{bucket}/{key}"
    uri_expression = json.dumps(uri)

    reader = _sample_readers()[read_format]
    expected = _template_reader_map(template)[read_format].replace(TEMPLATE_URI_EXPRESSION, uri_expression)

    assert _rendered(reader, uri_expression) == expected, (
        f"for format {read_format!r} the sample reader renders "
        f"{_rendered(reader, uri_expression)!r} while {template} reads {expected!r}"
    )
    assert "jdbc" not in reader.lower(), (
        f"_SAMPLE_READERS[{read_format!r}] mentions JDBC — LGR-819980f-1 is a standing invariant"
    )


@settings(max_examples=200, deadline=None)
@given(bucket=_BUCKETS, stem=_KEYS, extension=_UNRECOGNISED_EXTENSIONS)
def test_property_11_an_unrecognised_extension_gets_no_reader_where_a_template_gets_json(
    bucket: str, stem: str, extension: str
):
    """**Validates: Requirements 3.5**

    Property 11, the divergence half. For any ``s3://`` URI whose extension is
    outside ``_SOURCE_FORMAT_BY_EXTENSION`` and is not a compression suffix,
    ``sample_read_format`` returns ``None`` — so no reader is selected at all —
    even though every template would have read that object with its ``{% else %}``
    json reader.

    Both sides are correct for their own inputs, and asserting the difference is
    the point: ``LGR-60c4985-7`` (**template / ratified**) makes an extension
    outside ``FORMAT_MAP`` write an ``unsupported`` audit row and raise, so it
    never reaches a rendered template, while ``sample_read_format`` is handed only
    a URI. A later reader must not tidy this into a bug.

    The URI is asserted to pass the scheme gate first, so the claim is about the
    format resolver rather than about ``is_s3_source_uri``.
    """
    runner = _glue_session_runner()

    # A recognised extension is the other half's subject, asserted there. Dropped
    # with `assume` rather than returned from, so a discarded draw is replaced
    # instead of counting as a trivial pass.
    assume(extension not in runner._SOURCE_FORMAT_BY_EXTENSION)
    assume(extension not in runner._COMPRESSION_EXTENSIONS)

    uri = f"s3://{bucket}/{stem}.{extension}"

    assert runner.is_s3_source_uri(uri), f"{uri!r} should pass the scheme gate"
    assert runner.sample_read_format(uri) is None, (
        f"sample_read_format resolved {uri!r} to {runner.sample_read_format(uri)!r}. An extension "
        f"outside _SOURCE_FORMAT_BY_EXTENSION must resolve to None rather than to the templates' "
        f"json catch-all: guessing json builds a frame of _corrupt_record rows and reports the "
        f"transform's failure against it at STAGE_FRAME_BUILD."
    )

    for template in S3_SOURCE_TEMPLATE_NAMES:
        _, else_expression = _source_format_branches(template)
        assert else_expression == _template_reader_map(template)[JSON_CATCH_ALL_FORMAT], (
            f"{template}'s catch-all is no longer its json reader, so the divergence this "
            f"property describes has changed shape"
        )
