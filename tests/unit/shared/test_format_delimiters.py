# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Source_Delimiter table and its lookup — one definition, pinned.

Subject: ``shared/format_delimiters.py``, new in p55 task 1.3. A **pure module
with no consumers yet** — ``job_arguments.py`` (task 2.5), ``create_code``'s
render context (2.6), ``test_code``'s ``_SAMPLE_READERS`` (2.7) and
``schema_extract``'s two CSV read paths (2.8) are the four that will read it.
So nothing here asserts anything about a call site; these tests pin the table,
the default and the compression-suffix arithmetic that the four consumers will
inherit.

What these tests pin, and why each one is here
----------------------------------------------
1. **The table's entries** (R4.3, R4.4; ``txt`` added by p64 R3.2). ``tsv`` is
   one tab, ``csv`` is one comma and ``txt`` is one pipe. The defect this module
   exists to fix is a TSV parsing as ONE column named ``name\\tage\\tcity``, so
   the tab entry is the whole point; p64 1c.2 pins the pipe alongside it,
   including the near-miss ``.text`` that must NOT get one.

2. **The lookup is keyed by extension, not by Canonical_Format.** Asserted
   against ``FORMAT_MAP`` read out of ``file_validation`` rather than
   transcribed: both extensions map onto the Canonical_Format ``csv`` there, so
   a table keyed on the Canonical_Format could not tell them apart. If someone
   later re-keys this table, this test says why they cannot.

3. **The default, on four shapes** (R4.6) — an unrecognised extension, no
   extension, a bare compression suffix, and the empty string.

4. **The compression-suffix stripping agrees with ``detect_format``**, checked
   by calling ``detect_format`` itself over a generated key set rather than by
   transcribing its behaviour. Task 2.8 has a ``.tsv.gz`` test that depends on
   this reaching the call site, and ``detect_format`` is the definition of what
   "the same suffixes" means. Transcribing it is how the two copies drift; the
   mirroring is a claim about *agreement*, so it is asserted as one.

5. **``.txt``'s agreement with ``detect_format``, asserted by calling it**
   (p64 1c.2, R3.1 + R3.2). The pair R3 landed is two literals in two files —
   ``FORMAT_MAP``'s ``"txt": "csv"`` in ``file_validation`` and
   ``FORMAT_DELIMITERS``' ``"txt": "|"`` here — and the whole point of R3 is
   that they *correspond*. ``LGR-28b4d75-5``'s stated reason applies verbatim:
   *"a test written from the producer's output cannot detect the producer
   drifting, because the output is its own oracle."* Two transcribed tables that
   agree because one commit edited both prove nothing, so ``TestTheNewExtension``
   builds the real ``detect_format`` from ``file_validation``'s real bytes and
   **calls** it — the idiom
   ``test_the_stripping_agrees_with_detect_format_rather_than_being_transcribed``
   already established below.

Ledger status, recorded as a gap rather than an all-clear
--------------------------------------------------------
``why`` on ``shared/format_delimiters.py`` returns ``unknown_path`` — the file
is new in this task and has no history. ``why`` on ``detect_format``'s own line
(``file_validation/lambda_function.py:79``) returns one commit, ``8f3f9e1b30``,
with ``records: []`` — a bug-cleanup commit that produced no ledger record — so
``detect_format``'s algorithm is **unconstrained by the ledger** and this
module mirrors it as a code read, not under a ruling.
``decisions(scope="agentic_data_ingestion/shared", include_backfilled=True)``
returns nothing about format vocabulary. Two records do govern this task:
``LGR-eee8ac8-5`` (**template / ratified**), whose stated reason for
single-sourcing a fingerprint applies verbatim to single-sourcing this table —
*"a second definition in either Lambda would disable the check silently by
never matching"*; and ``LGR-b397ee2-3`` (**template / ratified**), which
requires a new ``shared/`` module to be registered in ``shared/__init__.py``'s
``_LAYER_MAP`` because ``test_shared_import_order.py::
test_layer_map_covers_all_shared_modules`` fails otherwise. ``format_delimiters``
is registered at layer 0 — it imports nothing from ``shared/``.

**Validates: Requirements 4.3, 4.4, 4.6** (p55), **3.1, 3.2, 3.6** (p64)
"""

from __future__ import annotations

import ast
import pathlib
import sys
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SHARED_DIR = str(PROJECT_ROOT / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

# Imported after the sys.path shim above, per this tree's convention for
# shared/ modules (ruff's E402 is globally ignored in pyproject.toml for
# exactly this).
from format_delimiters import DEFAULT_DELIMITER, FORMAT_DELIMITERS, delimiter_for_key

_FILE_VALIDATION_SOURCE = PROJECT_ROOT / "lambda" / "agentic_data_ingestion_file_validation" / "lambda_function.py"


def _load_format_detection() -> dict[str, Any]:
    """``FORMAT_MAP``, ``detect_format`` and the ``FORMAT_MAP`` AST node, executed
    from ``file_validation``'s real source with **no import side effects**.

    Importing that Lambda's module needs env vars set and boto3 mocked
    (``test_format_detection.py:14-33`` does exactly that) and publishes
    ``lambda_function`` into ``sys.modules``, where what a later importer binds
    is decided by pytest's collection order — the failure ``LGR-c58f276-4``
    diagnosed with a two-file reproducer. So instead of importing, this lifts
    the two nodes it needs out of the parse tree and executes just those.

    This is **not** a text match over source, which ``LGR-b46a5bb-4`` rejects
    ("a substring match on source is not a test of behaviour"): the real
    function object is built from the real bytes and then *called*.
    """
    tree = ast.parse(_FILE_VALIDATION_SOURCE.read_text())

    format_map_node: ast.Assign | None = None
    detect_format_node: ast.FunctionDef | None = None
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "FORMAT_MAP"
        ):
            format_map_node = node
        elif isinstance(node, ast.FunctionDef) and node.name == "detect_format":
            detect_format_node = node

    assert format_map_node is not None, "FORMAT_MAP is no longer a module-level assignment"
    assert detect_format_node is not None, "detect_format is no longer a module-level function"

    namespace: dict[str, Any] = {}
    module = ast.Module(body=[format_map_node, detect_format_node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(_FILE_VALIDATION_SOURCE), "exec"), namespace)

    return {
        "FORMAT_MAP": namespace["FORMAT_MAP"],
        "detect_format": namespace["detect_format"],
        "format_map_node": format_map_node,
    }


class TestTheTableEntries:
    # RENAMED from `TestTheTwoEntries` by p64 1c.1: the table holds three
    # entries now that R3.2 added `txt → "|"`, so the old name asserted a count
    # it no longer had. The pipe's own example tests belong to 1c.2, not here —
    # this class's amendment is confined to what the new entry made false.
    def test_tsv_is_one_tab(self):
        # R4.3. The defect: with no `sep`, Spark's comma default turns a TSV
        # header into one field named "name\tage\tcity".
        assert delimiter_for_key("pipeline-1/uploads/data.tsv") == "\t"

    def test_csv_is_one_comma(self):
        # R4.4.
        assert delimiter_for_key("pipeline-1/uploads/data.csv") == ","

    def test_txt_is_one_pipe(self):
        # p64 R3.1/R3.2. Added by 1c.2 — 1c.1 landed the declaration and
        # amended what the new entry made false, but left the entry with no
        # example of its own, so nothing pinned the *value* `|`. A `.txt` key
        # answering `,` would have satisfied every assertion in this file
        # before this test existed.
        assert delimiter_for_key("pipeline-1/uploads/data.txt") == "|"

    def test_the_lookup_is_case_insensitive_for_txt_too(self):
        # `delimiter_for_key` lowercases the key before splitting
        # (`format_delimiters.py:112`), the same arithmetic `detect_format`
        # uses (`file_validation/lambda_function.py:113`). R3 names the
        # extension, not a spelling of it, and `.TXT` is an extension a browser
        # upload really produces. Verified at runtime before being pinned:
        # `.TXT`, `.Txt` and `.TXT.GZ` all answer `|`.
        assert delimiter_for_key("pipeline-1/uploads/DATA.TXT") == "|"
        assert delimiter_for_key("pipeline-1/uploads/Data.Txt") == "|"

    def test_a_near_miss_extension_does_not_get_the_pipe(self):
        # `.text` is not `.txt`. R3.1 extends the vocabulary by exactly one
        # extension, and the lookup is an exact dict hit rather than a prefix
        # or substring match — so the entry cannot widen silently to whatever
        # else a user might name a text file. This is the assertion that would
        # fail if someone "helpfully" made the match fuzzy.
        assert delimiter_for_key("pipeline-1/uploads/data.text") == DEFAULT_DELIMITER
        assert delimiter_for_key("pipeline-1/uploads/data.txtx") == DEFAULT_DELIMITER

    def test_the_last_extension_wins_when_txt_is_not_it(self):
        # The whole-key `rsplit` reads the TRAILING extension, so a key that
        # merely contains `.txt` earlier on is not a pipe source. Stated
        # because `.txt` is a weak signal (R3.6) and this is the boundary of
        # how weak: weak about content, not loose about the extension.
        assert delimiter_for_key("pipeline-1/notes.txt.tsv") == "\t"
        assert delimiter_for_key("pipeline-1/notes.txt.csv") == ","

    def test_the_table_holds_exactly_these_entries(self):
        # An exact equality rather than membership checks: an entry added here is
        # a format this system claims to parse correctly, and that is a product
        # change. Amend this assertion deliberately, with the reason stated here,
        # per LGR-60c4985-6 / LGR-9e76249-10.
        #
        # AMENDED by p64 1c.1 — `txt → "|"`, R3.2. The p55 note this replaced
        # said `pipe` was "a recorded follow-on, deliberately not added"; R3.2
        # discharges exactly that follow-on, so the amendment is the recorded
        # condition firing rather than a fence being loosened. Kept as equality
        # and NOT relaxed to a superset check: the point of the assertion is that
        # adding a delimited format is a deliberate act, and that survives only
        # while the whole table is pinned.
        assert FORMAT_DELIMITERS == {"csv": ",", "tsv": "\t", "txt": "|"}

    def test_every_delimiter_is_a_single_character(self):
        # Property 5's example-shaped half. A multi-character `sep` is not a
        # delimiter Spark's CSV reader accepts.
        for extension, delimiter in FORMAT_DELIMITERS.items():
            assert len(delimiter) == 1, f"{extension!r} maps to {delimiter!r}"


class TestKeyedByExtensionNotByCanonicalFormat:
    def test_csv_and_tsv_share_a_canonical_format_so_it_cannot_be_the_key(self):
        # Read out of file_validation rather than transcribed: this is the fact
        # that makes the Canonical_Format the wrong key (R4.3/R4.4 name the
        # extension for this reason), and it is also the fact that keeps
        # test_format_detection.py:141 needing no amendment.
        format_map = _load_format_detection()["FORMAT_MAP"]

        assert format_map["csv"] == format_map["tsv"] == "csv"
        # ...while the two delimiters differ. Both statements together are the
        # argument for the extension key.
        assert FORMAT_DELIMITERS["csv"] != FORMAT_DELIMITERS["tsv"]

    def test_the_delimiter_is_not_nested_inside_format_map(self):
        # p55 do-NOT 15. `_format_map_values` in
        # tests/contracts/test_format_vocabulary_cross_lambda.py raises rather
        # than skips on a FORMAT_MAP value that is not an `ast.Constant` string,
        # so nesting the delimiter as a dict or tuple value breaks fence F5
        # outright. This module is a sibling instead; assert FORMAT_MAP's keys
        # and values are still all string constants, in the same terms F5 uses.
        node = _load_format_detection()["format_map_node"]
        assert isinstance(node.value, ast.Dict)

        for key, value in zip(node.value.keys, node.value.values, strict=True):
            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
            assert isinstance(value, ast.Constant) and isinstance(value.value, str)


class TestTheDefault:
    def test_an_unrecognised_extension_gets_a_comma(self):
        # R4.6.
        assert delimiter_for_key("data.parquet") == DEFAULT_DELIMITER
        assert delimiter_for_key("data.json") == DEFAULT_DELIMITER
        assert delimiter_for_key("data.xlsx") == DEFAULT_DELIMITER

    def test_no_extension_at_all_gets_a_comma(self):
        assert delimiter_for_key("noextension") == DEFAULT_DELIMITER

    def test_the_empty_string_gets_a_comma(self):
        # detect_format("") is "unknown" (test_format_detection.py:77); the
        # lookup's answer for the same input is the default, not a raise.
        assert delimiter_for_key("") == DEFAULT_DELIMITER

    def test_a_bare_compression_suffix_gets_a_comma(self):
        # detect_format("data.gz") is "unknown"
        # (test_format_detection.py:105) because there is no base extension
        # underneath. Same shape here: nothing to look up, so the default.
        assert delimiter_for_key("data.gz") == DEFAULT_DELIMITER

    def test_the_default_is_one_comma(self):
        assert DEFAULT_DELIMITER == ","


class TestCompressionSuffixStripping:
    def test_a_gzipped_tsv_still_gets_a_tab(self):
        # Task 2.8 has a `.tsv.gz` test that depends on this reaching the call
        # site, so this is the case the four consumers inherit.
        assert delimiter_for_key("pipeline-1/uploads/data.tsv.gz") == "\t"

    def test_a_gzipped_txt_still_gets_a_pipe(self):
        # p64 1c.2. The `txt` entry inherits the suffix arithmetic rather than
        # being special-cased, which is the point of R3.2 costing one dict
        # entry — but "inherits" is a claim, so it is asserted rather than
        # assumed from the shared code path.
        assert delimiter_for_key("pipeline-1/uploads/data.txt.gz") == "|"

    def test_all_four_compression_suffixes_are_looked_through(self):
        for suffix in ("gz", "bz2", "snappy", "zst"):
            assert delimiter_for_key(f"data.tsv.{suffix}") == "\t", suffix
            assert delimiter_for_key(f"data.csv.{suffix}") == ",", suffix
            # p64 1c.2 — the third entry, over the same four suffixes.
            assert delimiter_for_key(f"data.txt.{suffix}") == "|", suffix

    def test_the_lookup_is_case_insensitive_like_detect_format(self):
        assert delimiter_for_key("DATA.TSV") == "\t"
        assert delimiter_for_key("DATA.TSV.GZ") == "\t"
        # p64 1c.2 — the mixed case that combines both, verified at runtime.
        assert delimiter_for_key("DATA.TXT.GZ") == "|"

    def test_the_stripping_agrees_with_detect_format_rather_than_being_transcribed(self):
        """Agreement with ``detect_format``, asserted by calling it.

        The claim is not "this module strips ``.gz``" — it is "this module
        strips **what detect_format strips**". Two copies of an algorithm drift
        apart when one of them is transcribed, so the copy is checked against
        the original over the whole cross-product of the extensions in
        ``FORMAT_MAP`` and the four compression suffixes.

        The bridge is that ``FORMAT_DELIMITERS`` is keyed on the extension
        ``detect_format`` resolves to. For a key whose stripped extension is in
        ``FORMAT_DELIMITERS``, ``detect_format`` must therefore report the
        Canonical_Format that extension maps to, and this module must report
        that extension's delimiter. For every other key, both fall through.
        """
        loaded = _load_format_detection()
        detect_format = loaded["detect_format"]
        format_map = loaded["FORMAT_MAP"]

        extensions = sorted(set(format_map) | {"xlsx", "txt", ""})
        suffixes = ("", ".gz", ".bz2", ".snappy", ".zst")

        for extension in extensions:
            for suffix in suffixes:
                key = f"pipeline-1/uploads/data.{extension}{suffix}" if extension else f"pipeline-1/data{suffix}"

                detected = detect_format(key)
                delimiter = delimiter_for_key(key)

                if extension in FORMAT_DELIMITERS:
                    # detect_format looked through the suffix and found the
                    # base extension, so this module must have too.
                    assert detected == format_map[extension], key
                    assert delimiter == FORMAT_DELIMITERS[extension], key
                else:
                    # No delimiter is defined for this extension, whatever
                    # detect_format made of it.
                    assert delimiter == DEFAULT_DELIMITER, key

    def test_a_dot_in_a_folder_name_does_not_produce_a_delimiter(self):
        # detect_format scans the whole key, so "my.dir/data" reads an
        # extension of "dir/data" and reports unknown
        # (test_format_detection.py:121 covers the sibling case). This module
        # uses identical arithmetic and must fall through the same way, rather
        # than being cleverer than the function it mirrors.
        assert delimiter_for_key("my.dir/data") == DEFAULT_DELIMITER


class TestTheNewExtension:
    """``.txt``'s two halves correspond, asserted by **calling** ``detect_format``.

    p64 1c.2. R3 is a pair of literals in two files —
    ``FORMAT_MAP["txt"] == "csv"`` in ``file_validation`` (R3.1) and
    ``FORMAT_DELIMITERS["txt"] == "|"`` here (R3.2) — and R3 is only satisfied if
    they *correspond*: an accepted extension whose delimiter is missing parses as
    one column, and a delimiter for an extension nothing accepts is dead. Neither
    literal alone says anything about the other.

    So none of these tests transcribes ``FORMAT_MAP``. Each builds the real
    ``detect_format`` out of ``file_validation``'s real bytes and calls it,
    following ``TestCompressionSuffixStripping``'s existing idiom. ``LGR-28b4d75-5``
    is why: *"a test written from the producer's output cannot detect the producer
    drifting, because the output is its own oracle."* Two tables that agree
    because one commit edited both is that failure mode exactly — and it is the
    live risk here, since 1c.1 edited four declarations plus nine test
    transcriptions in a single commit.
    """

    def test_detect_format_accepts_txt_rather_than_calling_it_unknown(self):
        # The R3.1 half, from the producer. Before 1c.1 this answered "unknown"
        # — the answer `LGR-60c4985-7` describes as writing an `unsupported`
        # audit row and raising `ValueError`, wrapped into a permanent
        # `StepFailure`. That record's `revisit_when` names precisely this
        # event: "a format is added to `FORMAT_MAP`".
        detect_format = _load_format_detection()["detect_format"]

        assert detect_format("pipeline-1/uploads/data.txt") == "csv"

    def test_the_extension_the_delimiter_is_keyed_on_is_one_detect_format_resolves(self):
        # The correspondence, stated as one assertion over both files. `txt` is
        # a key of this module's table, so `detect_format` must report SOME
        # Canonical_Format for it — anything else means an extension with a
        # delimiter that no upload can ever reach.
        detect_format = _load_format_detection()["detect_format"]

        assert "txt" in FORMAT_DELIMITERS
        assert detect_format("pipeline-1/uploads/data.txt") != "unknown"

    def test_txt_and_csv_share_a_canonical_format_so_the_pipe_needs_the_extension_key(self):
        # The extension-key argument (`TestKeyedByExtensionNotByCanonicalFormat`)
        # extended from two extensions to three. `detect_format` is CALLED for
        # all three rather than `FORMAT_MAP` being read, because the claim is
        # about what the resolver answers, not about what a dict literal says.
        #
        # This is also the reason R3 needed no new Canonical_Format, no new
        # extractor and no new template branch: the delimiter is the only thing
        # that differs, and it is not part of the Canonical_Format.
        detect_format = _load_format_detection()["detect_format"]

        assert detect_format("data.csv") == detect_format("data.tsv") == detect_format("data.txt") == "csv", (
            "all three collapse onto one Canonical_Format"
        )

        # ...and all three delimiters differ, which is what the Canonical_Format
        # cannot express and the extension key can.
        assert len({FORMAT_DELIMITERS["csv"], FORMAT_DELIMITERS["tsv"], FORMAT_DELIMITERS["txt"]}) == 3

    def test_the_pipe_survives_a_compression_suffix_on_both_sides(self):
        # Agreement over the new extension where the arithmetic is involved,
        # not just the lookup: `detect_format` looks through `.gz` to find
        # `txt`, so this module must reach the same base extension and answer
        # the pipe. Asserted per suffix so a failure names which one.
        detect_format = _load_format_detection()["detect_format"]

        for suffix in ("gz", "bz2", "snappy", "zst"):
            key = f"pipeline-1/uploads/data.txt.{suffix}"

            assert detect_format(key) == "csv", key
            assert delimiter_for_key(key) == "|", key

    def test_both_fall_through_together_on_a_near_miss_extension(self):
        # Agreement on the NEGATIVE side, which is the half a table comparison
        # cannot check at all. `.text` is outside `FORMAT_MAP`, so
        # `detect_format` refuses it — and this module must therefore NOT hand
        # out a pipe for it. A fuzzy match here would produce a delimiter for a
        # file the pipeline rejects at step 1.
        detect_format = _load_format_detection()["detect_format"]

        for extension in ("text", "txtx", "tx"):
            key = f"pipeline-1/uploads/data.{extension}"

            assert detect_format(key) == "unknown", key
            assert delimiter_for_key(key) == DEFAULT_DELIMITER, key

    def test_agreement_holds_over_the_same_case_variants(self):
        # Both sides lowercase before splitting
        # (`format_delimiters.py:112`, `file_validation/lambda_function.py:113`),
        # so case-insensitivity is a shared property rather than two
        # coincidences. Asserted by calling, for the same reason as above.
        detect_format = _load_format_detection()["detect_format"]

        for key in ("DATA.TXT", "Data.Txt", "data.TXT.GZ"):
            assert detect_format(key) == "csv", key
            assert delimiter_for_key(key) == "|", key


class TestPurity:
    def test_the_module_registers_in_the_shared_layer_map_at_layer_zero(self):
        # LGR-b397ee2-3: a new shared/ module must be in _LAYER_MAP, or
        # test_shared_import_order.py::test_layer_map_covers_all_shared_modules
        # fails. Layer 0 because this module imports nothing from shared/.
        sys.path.insert(0, str(PROJECT_ROOT))
        sys.modules.pop("shared", None)
        from shared import _LAYER_MAP

        assert _LAYER_MAP["format_delimiters"] == 0

    def test_the_lookup_does_not_mutate_the_table(self):
        # The `.get()` default must not be a `setdefault`: a lookup that
        # inserted would make the exact-equality assertion above order
        # dependent inside a warm Lambda container.
        before = dict(FORMAT_DELIMITERS)

        delimiter_for_key("data.xlsx")
        delimiter_for_key("")
        delimiter_for_key("data.gz")

        assert before == FORMAT_DELIMITERS
