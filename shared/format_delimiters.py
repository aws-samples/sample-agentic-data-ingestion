# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one definition of a delimited text source's Source_Delimiter.

A TSV upload is accepted today and then mis-parsed into a single column. All
four Glue_Job_Templates read CSV as
``spark.read.option("header", True).option("inferSchema", False).csv(source_path)``
with **no** ``sep`` option (``glue_job_s3_tables.py.j2:72``,
``glue_job_glue_catalog.py.j2:51``, ``glue_job_jdbc.py.j2:51``,
``glue_job_s3_parquet.py.j2:53``), Spark defaults ``sep`` to a comma, and
``create_glue_pipeline/job_arguments.py:520`` hardcodes
``job_arguments["--field-delimiter"] = ","``. So a header ``name<TAB>age<TAB>city``
becomes one field literally named ``name\\tage\\tcity``. p55 W2 fixes that by
giving every consumer one place to ask what character separates a source's
fields.

Keyed by **extension, not by Canonical_Format**
-----------------------------------------------
``FORMAT_MAP`` maps both ``csv`` and ``tsv`` onto the Canonical_Format ``csv``
(``file_validation/lambda_function.py:56-57``), so the Canonical_Format cannot
distinguish the two and is the wrong key. R4.3 and R4.4 name the extension for
that reason.

A **sibling** literal, never a value inside ``FORMAT_MAP``
---------------------------------------------------------
``tests/contracts/test_format_vocabulary_cross_lambda.py::_format_map_values``
asserts every ``FORMAT_MAP`` key **and every value** is an ``ast.Constant``
string and **raises** rather than skipping on anything else — *"A
Canonical_Format this scan cannot read is a format the dispatch is never checked
for."* Nesting the delimiter as a dict or tuple value would break that contract
outright. Keeping it here instead means ``tsv → "csv"`` survives untouched, so
``test_format_detection.py:141``'s ``test_csv_tsv_map_to_same_format`` needs no
amendment (p55 do-NOT 15; ``requirements.md`` listed that fence as a conditional
amendment and the condition does not fire).

In ``shared/`` rather than copied per Lambda
-------------------------------------------
Four consumers need this value — ``create_glue_pipeline/job_arguments.py``,
``create_code``'s render context, ``test_code``'s ``_SAMPLE_READERS`` and
``schema_extract``'s two CSV read paths — and all are layer-attached, so one
import serves all four. Three local copies were rejected for
``LGR-eee8ac8-5``'s stated reason, which applies verbatim: *"a second definition
in either Lambda would disable the check silently by never matching."*
``file_validation`` is deliberately **not** a consumer: it is HEAD-only and
reads no rows, so it needs no delimiter.

Also rejected (design §4.5, W2-D1): giving ``tsv`` its own Canonical_Format
``"tsv"``, which cascades through ``FORMAT_MAP``'s values, ``schema_extract``'s
dispatch chain, the Format_Vocabulary_Mirror and five fences to express a
one-character difference in one read option; and threading a new
``source_delimiter`` key through the Step Functions payload, which
``LGR-5c539a6-2`` would then require on both arms of ``file_validation``, for a
value every consumer can derive from the source key it already holds.

Compression suffixes
--------------------
:func:`delimiter_for_key` strips exactly the suffixes ``detect_format`` strips
(``file_validation/lambda_function.py:71-83``) and by exactly the same
arithmetic, so ``data.tsv.gz`` gets a tab and ``data.gz`` — which
``detect_format`` reports as ``unknown``
(``test_format_detection.py:105``) — gets the default comma. Mirrored rather
than shared because ``detect_format`` lives in a Lambda that this module must
not import; ``test_code``'s ``_COMPRESSION_EXTENSIONS``
(``glue_session_runner.py:2070``) is a third copy of the same set, already
present and out of scope here. The mirroring is asserted against
``detect_format`` itself in
``tests/unit/shared/test_format_delimiters.py`` rather than transcribed, so the
two cannot drift apart silently.
"""

from typing import Final

#: The Source_Delimiter for each delimited-text **extension** (R4.3, R4.4).
#: Written as a bare assignment carrying a dict of string literals — the same
#: shape as ``FORMAT_MAP`` — so an AST scan can read this table by the same
#: route it already reads that one. Adding an entry here is the whole cost of
#: supporting another delimited format — and p64 R3.2 spent exactly that,
#: discharging the follow-on p55 recorded here and deliberately left undone.
#:
#: **The accepted residual (p64 R3.6).** ``txt`` is a *weak* key in a way the
#: other two are not: they name their delimiter and ``txt`` names nothing. A
#: ``.txt`` source that is not pipe-delimited is therefore read with the wrong
#: separator, and R3.6 accepts that outcome instead of working around it. The
#: outcome is **demonstrated rather than described** — see
#: ``test_streaming_schema.py::TestExtractSchemaFromCSV``'s
#: ``test_a_comma_separated_txt_parses_as_one_column``
#: (``tests/unit/lambdas/schema_extract/``, p64 1c.2), which pins it at the call
#: site because that is the only place a source is actually read. Removing this
#: table's ``txt`` entry fails that test, so the two cannot drift apart.
#:
#: It is acceptable because it is **the tradeoff ``tsv`` already carries**, not
#: a new class of defect: nothing verifies that a ``.tsv`` is tab-separated
#: either, and the pre-p55 state this module exists to fix gave *every*
#: delimited source a comma. Detection stays extension-only
#: (``detect_format``, ``file_validation/lambda_function.py``); content sniffing
#: is deferred by **R11.4**, so a sniff added here would be a deferred design
#: arriving through a lookup table. (``requirements.md`` R3.6 cites that
#: deferral as R11.2, which is the HL7/XML one; ``design.md`` §5 O6 has it
#: right. Read R11.4.)
#:
#: What keeps the residual survivable — and therefore what to watch — is that a
#: mis-parse arrives as a visibly wrong schema in front of a human: the
#: schema-mapping gate fails closed (``LGR-69ca78b-1``; ``CheckSchemaMappingApproval``'s
#: permissive arm fires only on an explicit boolean ``false``), so one field
#: named for a whole unsplit header line is unmistakable there.
#:
#: **Revisit when** any of these holds. Each retires the acceptance; none is a
#: reason to loosen it quietly.
#:
#: 1. Non-pipe ``.txt`` uploads stop being the exception in practice. The
#:    acceptance rests on pipe being what ``.txt`` means to this system's
#:    sources (the p64 report's finding 11 is that evidence), not on the
#:    mis-parse being harmless.
#: 2. Two delimiters need one extension — semicolon-separated ``.txt`` beside
#:    pipe-separated. That breaks this table's *shape*, one delimiter per
#:    extension, rather than its contents, so no entry here can answer it: it
#:    forces either the deferred sniff (R11.4) or a Source_Delimiter the
#:    pipeline record carries per source.
#: 3. A path reaches Glue without a human seeing the schema. Then the residual
#:    stops being a bad schema someone rejects and becomes a single-column
#:    table written silently — which is the failure mode p55 W2 was built to
#:    end, arriving by a different door.
FORMAT_DELIMITERS = {"csv": ",", "tsv": "\t", "txt": "|"}

#: What an extension absent from :data:`FORMAT_DELIMITERS` gets (R4.6). Named
#: rather than inlined so the tests can assert the default without freezing a
#: second copy of the literal. R4.6 exists so the mechanism has a defined
#: answer for every existing Canonical_Format without enumerating each, which
#: is what keeps this change from silently altering ``json``, ``parquet``,
#: ``avro`` or ``orc``.
DEFAULT_DELIMITER: Final[str] = ","

#: The compression suffixes ``detect_format`` looks through
#: (``file_validation/lambda_function.py:80``). A frozenset rather than that
#: line's tuple; membership is the only operation either performs, so the two
#: are behaviourally identical.
_COMPRESSION_SUFFIXES: Final[frozenset[str]] = frozenset({"gz", "bz2", "snappy", "zst"})


def delimiter_for_key(key: str) -> str:
    """The Source_Delimiter for *key*, or a comma if *key*'s extension has none.

    Total by construction: every input returns a single character and nothing
    raises (R4.6). An unrecognised extension, no extension at all, a bare
    compression suffix and the empty string all return
    :data:`DEFAULT_DELIMITER`.

    Args:
        key: An S3 object key, or anything ending in the same extension — a
            full ``s3://`` URI works, because only the tail is read.

    Returns:
        One character. ``"\\t"`` for a tab-separated source, otherwise ``","``.
    """
    # Deliberately identical arithmetic to detect_format's, including the
    # whole-string rsplit: for a key like "my.dir/data" both read an extension
    # of "dir/data" and both therefore fall through to their default.
    ext = key.lower().rsplit(".", 1)[-1] if "." in key else ""
    if ext in _COMPRESSION_SUFFIXES:
        parts = key.lower().rsplit(".", 2)
        ext = parts[-2] if len(parts) >= 3 else ext
    return FORMAT_DELIMITERS.get(ext, DEFAULT_DELIMITER)
