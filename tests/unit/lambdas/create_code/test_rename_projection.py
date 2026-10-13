# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 W4 task 6.5 — the rename projection and the string-literal sink.

WHAT THIS FILE PINS
-------------------
R11.1/R11.2: a run carrying an approved Rename_Manifest generates a job that
reads each renamed column by its ORIGINAL name and writes it by its APPROVED
name. The bridge is one explicit projection emitted immediately after the read
and before the transform, the DQDL rules and the ``MERGE INTO``.

R11.3/R11.4 restate existing behaviour that must survive W4 unchanged, and the
whole risk of W4 is that an implementer relaxes the Identifier_Allowlist to make
renaming work. So the assertions here are as much about what did NOT change:

* ``validate_identifier`` and ``shared/identifier_utils.py`` are untouched
  (do-NOT 3 — 42 callers). The projection CALLS the validator; it does not
  reimplement, wrap or soften it.
* Neither injection sink is weakened or bypassed (do-NOT 2). ``withColumnRenamed``
  takes STRING arguments, so an original name never enters a ``spark.sql``
  statement or a DQDL rule string. The rule fields and the merge_key see approved
  names, which have already passed the validator unmodified.
* Nothing is sanitized silently (do-NOT 1). Every refusal below is loud and names
  the offending value.

THE SINK W4 CREATES
-------------------
do-NOT 17. Before W4 an original column name reached generated code **nowhere**;
after W4 it reaches it as an escaped Python string literal. ``identifier_utils.py``'s
module docstring states the threat verbatim — *"A hostile CSV header must never
become code."* — and the allowlist guards the SQL sink, not this one, because this
one did not exist. That is why the escaping is asserted here on examples and, by
task 6.6, over arbitrary strings (Property 2). **Property 2 and Property 3 are
task 6.6's and are deliberately NOT written here.** What is left for them is the
seam: ``_python_string_literal`` and ``_rename_projection_block`` are both pure —
the first is ``str -> str``, the second takes ``(original, target)`` pairs and
returns Python source — so a round-trip property needs no render and no fixture.

ONE DEVIATION FROM THE DESIGN'S LETTER, AND WHY
-----------------------------------------------
W4-D2 names ``json.dumps`` with ``ensure_ascii=True``. That flag does not deliver
what the same sentence asks for — "a literal Python accepts for **any** input" —
because JSON escapes a non-BMP character as a UTF-16 surrogate pair and Python
reads ``"\\ud83d\\ude42"`` as two lone surrogates, not as ``"🙂"``. The PROPERTY is
kept (the emitted literal is pure ASCII, with quotes, backslashes, newlines and
non-ASCII all escaped); the mechanism is ``ensure_ascii=False`` plus explicit
``\\uXXXX`` / ``\\UXXXXXXXX`` escaping. Pinned below with the counterexample, so
the deviation is attributable and nobody simplifies it back.

WHY THE LENGTH CAP REFUSES RATHER THAN TRUNCATES
------------------------------------------------
Design W4-D2 says the emitted line is length-capped, "mirroring
``identifier_utils._DISPLAY_LIMIT``'s reasoning about megabyte-sized garbage".
The NUMBER is mirrored (300 = ``MAX_IDENTIFIER_LENGTH`` 255 + the same 45 of
slack) and the MECHANISM deliberately is not. ``_display`` truncates safely
because it only DISPLAYS; here the literal SELECTS a column, and
``withColumnRenamed`` is a silent no-op on a name no column carries — so a
truncated literal would skip the rename without a word and hand the original name
to the DQDL/MERGE sink. Task 6.7 pinned that ``_DISPLAY_LIMIT`` (300) exceeds
``MAX_IDENTIFIER_LENGTH`` (255), so ``_display``'s truncating branch is only
reachable by a value that already failed a check; the refusal below reproduces
that asymmetry rather than inventing a new one.

LEDGER STATUS
-------------
``LGR-a419843-2`` (template / ratified) is the fence this change had to keep
passing verbatim, and the way it is kept is structural: ``mappings[*].source_field``
still names the ORIGINAL column, ``_declared_source_fields`` and
``_validate_mapped_source_fields`` are not touched, and they still receive the
untranslated mappings. Pinned below from both sides.

``LGR-1e135d2-2`` (template / ratified) records that ``code_assembly`` owns no
AWS client and imports no strands; the manifest therefore arrives INJECTED, the
same way ``generate_transform``, ``quarantine_bucket`` and ``sample_reader`` do.
``test_code_assembly_still_owns_no_aws_client`` is that record's fence and stays
green.

``LGR-9e76249-10`` (template / ratified) owns the 260-line render budget. The
projection adds a fixed 3-line header plus exactly one line per renamed column,
which is measured below rather than asserted about.
"""

import ast
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

# Imported DIRECTLY, for the reason the sibling files give: `code_assembly` owns
# no AWS clients and reads no environment, so importing the handler module would
# put this file in the `sys.modules["lambda_function"]` tug-of-war for nothing.
import code_assembly as _ca

from identifier_utils import MAX_IDENTIFIER_LENGTH, IdentifierValidationError
from step_errors import StepFailure

# ---------------------------------------------------------------------------
# Fixtures by hand. `pipeline_metadata` is a plain dict the handler assembles —
# no AWS in it — so the moto-backed shared fixtures are neither needed nor
# appropriate.
# ---------------------------------------------------------------------------


def _manifest(*pairs: tuple[str, str], manifest_id: str = "m-1", schema_hash: str = "h-1") -> dict:
    """A Rename_Manifest in design §Data Models' shape, built literally.

    Built by hand rather than by calling ``derive_rename_manifest``: that
    function lives in the ``schema_mapping`` Lambda, the two cannot import each
    other, and — more to the point — task 6.5's contract is that the STORED
    manifest is the authority and is consumed **verbatim or not at all**. A test
    that re-derived it would be testing the deriver, not the consumer.
    """
    return {
        "manifest_id": manifest_id,
        "source_schema_hash": schema_hash,
        "approval_id": "a-1",
        "renames": [{"original_name": o, "target_name": t} for o, t in pairs],
    }


def _entry(target_field: str, source_field: str) -> dict:
    return {"target_field": target_field, "source_field": source_field, "confidence": 0.9}


def _object_schema(*names: str) -> dict:
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


def _pipeline_metadata(source_schema: dict | None = None, **target_overrides) -> dict:
    target_config = {
        "type": "s3_tables",
        "bucket_name": "my-table-bucket",
        "namespace": "analytics",
        "table_name": "orders",
        "write_mode": "append",
        "source_format": "json",
    }
    target_config.update(target_overrides)
    metadata: dict = {"source_type": "S3_UPLOAD", "source_config": {}, "target_config": target_config}
    if source_schema is not None:
        metadata["source_schema"] = source_schema
    return metadata


class _RecordingTransform:
    """The injected AI transform generator, recording the mappings it was given."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return 'df_transformed = df.select("first_name")'


def _generate(pipeline_metadata: dict, mapping_result: dict, rename_manifest=None, transform=None):
    transform = transform if transform is not None else _RecordingTransform()
    rendered = _ca.generate_code(
        pipeline_metadata=pipeline_metadata,
        mapping_result=mapping_result,
        generate_transform=transform,
        quarantine_bucket="adi-quarantine-test",
        rename_manifest=rename_manifest,
    )
    return rendered, transform


def _projection_lines(block: str) -> list[str]:
    """Only the ``withColumnRenamed`` lines of an emitted block."""
    return [line for line in block.splitlines() if line.startswith("df = df.withColumnRenamed(")]


# ===========================================================================
# Absence is inert — the state every run is in until the manifest arrives
# ===========================================================================


def test_no_manifest_emits_no_projection():
    assert _ca._rename_pairs(None) == []
    assert _ca._rename_projection_block([]) == ""


def test_a_manifest_with_no_renames_emits_no_projection():
    """R10 defines a Proposed_Rename as a pair whose original fails the allowlist.

    A source whose every header conforms therefore produces a manifest with an
    EMPTY ``renames`` list, and that must emit nothing at all — not an empty
    comment header, not a no-op line.
    """
    assert _ca._rename_pairs(_manifest()) == []
    assert _ca._rename_projection_block(_ca._rename_pairs(_manifest())) == ""
    assert _ca._rename_pairs({"manifest_id": "m", "source_schema_hash": "h", "approval_id": None}) == []


def test_a_conforming_column_contributes_no_line():
    """The emitted line count is exactly the header plus one line per RENAME.

    ``derive_rename_manifest`` emits one entry per distinct NON-conforming
    column, so a conforming column is absent from ``renames`` and this consumer
    must emit nothing for it. Asserted as arithmetic over the block rather than
    as "the name does not appear", because a conforming name legitimately appears
    elsewhere in the mappings.
    """
    manifest = _manifest(("first name", "first_name"), ("2024-total", "_2024_total"))
    block = _ca._rename_projection_block(_ca._rename_pairs(manifest))
    lines = block.splitlines()
    assert len(_projection_lines(block)) == 2
    assert len(lines) == len(_ca._RENAME_BLOCK_HEADER) + 2
    # `id` conforms, so it is not in the manifest and nothing is emitted for it.
    assert "id" not in {ast.literal_eval(line.split("(")[1].split(",")[0]) for line in _projection_lines(block)}


def test_generate_code_with_no_manifest_renders_the_pre_p55_script():
    """Byte-identical, not merely equivalent.

    The render context key is set unconditionally (StrictUndefined would raise on
    a conditional assignment), so "no manifest" has to mean "empty string" and the
    templates' guard has to render that as nothing.
    """
    metadata = _pipeline_metadata(_object_schema("first_name"))
    mapping_result = {"field_mappings": [_entry("first_name", "first_name")]}
    without, _ = _generate(metadata, mapping_result, rename_manifest=None)
    with_empty, _ = _generate(metadata, mapping_result, rename_manifest=_manifest())
    assert without == with_empty
    assert "# p55 W4: rename" not in without


# ===========================================================================
# R11.1/R11.2 — placement: after the read, before the transform, DQDL and MERGE
# ===========================================================================


def test_the_projection_lands_after_the_read_and_before_everything_downstream():
    metadata = _pipeline_metadata(_object_schema("first name", "id"), write_mode="merge", merge_key="id")
    mapping_result = {"field_mappings": [_entry("first name", "first name"), _entry("id", "id")]}
    rendered, _ = _generate(metadata, mapping_result, _manifest(("first name", "first_name")))

    read = rendered.index(".json(source_path)")
    projection = rendered.index('df = df.withColumnRenamed("first name", "first_name")')
    transform = rendered.index("# === AI-Generated Transform Block ===")
    dqdl = rendered.index("Rules = [")
    merge = rendered.index("MERGE INTO")

    assert read < projection, "the source must be READ by its original column name (R11.1)"
    assert projection < transform, "the transform must see approved names only"
    assert projection < dqdl, "the DQDL ruleset must see approved names only (R11.3)"
    assert projection < merge, "the MERGE INTO must see approved names only (R11.3)"
    # And the rendered script is still a valid, flat Python script.
    ast.parse(rendered)


def test_the_projection_reaches_all_four_s3_upload_templates():
    """Every (S3_UPLOAD, target) pair renders the projection.

    ``glue_job_jdbc_source.py.j2`` is deliberately excluded — see the report and
    the module comment: its incremental arm computes the watermark from ``df``
    using a source column name supplied as a Glue job argument, so renaming ahead
    of that read would break the watermark silently. Closing that needs the
    watermark argument to travel through the manifest too, which is not this
    task's.
    """
    manifest = _manifest(("first name", "first_name"))
    mapping_result = {"field_mappings": [_entry("first name", "first name")]}
    for target_type in ("s3_tables", "glue_catalog", "jdbc", "s3_parquet"):
        metadata = _pipeline_metadata(_object_schema("first name"), type=target_type, merge_key="id")
        rendered, _ = _generate(metadata, mapping_result, manifest)
        assert 'df = df.withColumnRenamed("first name", "first_name")' in rendered, target_type
        ast.parse(rendered)


def test_the_manifest_order_is_preserved_verbatim():
    """R10.4's collision suffix is positional, so a reordering changes the names.

    The manifest is the authority and is used verbatim; this consumer must not
    sort, de-duplicate by target, or re-key through a set.
    """
    manifest = _manifest(("b name", "b_name"), ("a name", "a_name"), ("c name", "c_name"))
    block = _ca._rename_projection_block(_ca._rename_pairs(manifest))
    originals = [ast.literal_eval(line.split("(")[1].split(", ")[0]) for line in _projection_lines(block)]
    assert originals == ["b name", "a name", "c name"]


# ===========================================================================
# do-NOT 17 — the string-literal sink
# ===========================================================================


@pytest.mark.parametrize(
    "original",
    [
        'he said "hi"',
        "back\\slash",
        "new\nline",
        "carriage\rreturn",
        "tab\there",
        "null\x00byte",
        "café",
        "日本語",
        "emoji 🙂",
        "'; DROP TABLE t; --",
        '") or 1=1 --',
        "{{ jinja }}",
        "{% raw %}",
    ],
)
def test_a_hostile_original_name_is_emitted_as_an_escaped_literal(original):
    """The emitted line parses as Python and the literal equals the input.

    This is the example-level form of the injection guard; task 6.6 owns the
    ``for any string`` property. The non-ASCII cases escape rather than travel
    raw — the reason ``repr()`` was rejected — and they round-trip, including the
    astral one, which is the reason ``json.dumps(ensure_ascii=True)`` alone is not
    the mechanism (see the test below).
    """
    block = _ca._rename_projection_block([(original, "safe_name")])
    (line,) = _projection_lines(block)
    module = ast.parse(line)
    call = module.body[0].value
    assert call.func.attr == "withColumnRenamed"
    emitted_original, emitted_target = (ast.literal_eval(arg) for arg in call.args)
    assert emitted_original == original
    assert emitted_target == "safe_name"


def test_plain_json_dumps_ensure_ascii_would_not_round_trip_an_astral_character():
    """Why ``_python_string_literal`` is not simply ``json.dumps(ensure_ascii=True)``.

    Design W4-D2 names ``ensure_ascii=True`` and asks for a literal "Python
    accepts for **any** input". Those two are not the same thing: JSON escapes a
    non-BMP character as a UTF-16 SURROGATE PAIR, and Python reads
    ``"\\ud83d\\ude42"`` as two lone surrogates rather than as ``"🙂"``. This test
    exists so the next reader who "simplifies" the emission back to one
    ``json.dumps`` call sees the counterexample immediately, and so the deviation
    from the design's letter is attributable rather than looking like drift.

    A mangled literal is not a silent data bug — ``withColumnRenamed`` would no-op
    and the original name would reach the DQDL sink, which refuses loudly — but it
    IS a violation of R11.1, which says the job reads the renamed column by its
    original name.
    """
    import json as _json

    emoji = "emoji 🙂"
    naive = _json.dumps(emoji, ensure_ascii=True)
    assert ast.literal_eval(naive) != emoji, "if this ever passes, the shortcut has become safe"
    assert ast.literal_eval(_ca._python_string_literal(emoji)) == emoji
    assert _ca._python_string_literal(emoji).isascii()


def test_the_emitted_literals_are_double_quoted_and_ascii():
    """Why ``repr()`` was rejected, made checkable.

    ``repr`` emits raw non-ASCII and switches to single quotes for a string
    containing a double quote, so the generated line's quoting would be
    inconsistent with the rest of the emitted script and harder to review.
    """
    block = _ca._rename_projection_block([('a "quoted" café', "safe_name")])
    (line,) = _projection_lines(block)
    assert line.isascii()
    assert line.startswith('df = df.withColumnRenamed("')
    assert repr('a "quoted" café') not in line


def test_no_original_name_reaches_the_sql_or_dqdl_sinks():
    """do-NOT 2, from the other side: the original appears ONCE, in the projection.

    If a non-conforming name leaked into the DQDL block or the MERGE statement it
    would appear a second time — and it would have had to pass
    ``validate_identifier`` to get there, which it cannot.
    """
    metadata = _pipeline_metadata(_object_schema("first name", "id"), write_mode="merge", merge_key="id")
    mapping_result = {"field_mappings": [_entry("first name", "first name"), _entry("id", "id")]}
    rendered, _ = _generate(metadata, mapping_result, _manifest(("first name", "first_name")))
    assert rendered.count("first name") == 1
    assert 'Completeness "first_name"' in rendered
    assert 'Completeness "first name"' not in rendered


# ===========================================================================
# The length cap — refuses, does not truncate
# ===========================================================================


def test_the_cap_mirrors_the_display_limit_number():
    assert _ca._MAX_ORIGINAL_NAME_CHARS == 300
    assert _ca._MAX_ORIGINAL_NAME_CHARS > MAX_IDENTIFIER_LENGTH


def test_a_name_at_the_cap_is_emitted():
    original = "a " * 150  # 300 characters, non-conforming
    assert len(original) == _ca._MAX_ORIGINAL_NAME_CHARS
    block = _ca._rename_projection_block([(original, "safe_name")])
    (line,) = _projection_lines(block)
    assert ast.literal_eval(ast.parse(line).body[0].value.args[0]) == original


def test_a_name_over_the_cap_is_refused_loudly_and_named():
    original = "x" * 301
    with pytest.raises(StepFailure) as excinfo:
        _ca._rename_projection_block([(original, "safe_name")])
    message = str(excinfo.value)
    assert "301" in message
    assert "300" in message
    # The truncated form, not the whole 301 characters — and the true length.
    assert "(301 chars)" in message
    assert original not in message
    # It says WHY it refuses rather than truncating, because the next reader will
    # be tempted to "just truncate it".
    assert "matches no column" in message


def test_a_megabyte_sized_name_does_not_reach_the_message():
    original = "x" * 1_000_000
    with pytest.raises(StepFailure) as excinfo:
        _ca._rename_projection_block([(original, "safe_name")])
    assert len(str(excinfo.value)) < 2000


# ===========================================================================
# R11.3/R11.4 — the allowlist is neither weakened nor bypassed
# ===========================================================================


def test_a_manifest_target_that_fails_the_allowlist_fails_loudly_naming_it():
    """R11.4. Reachable only from a manifest that bypassed ``derive_rename_manifest``'s
    closing invariant check — which is exactly the case worth failing on."""
    with pytest.raises(IdentifierValidationError) as excinfo:
        _ca._rename_projection_block([("first name", "first name")])
    assert "'first name'" in str(excinfo.value)
    assert _ca._RENAME_TARGET_SINK in str(excinfo.value)


def test_the_new_sink_context_is_distinguishable_from_the_merge_key_sinks():
    """Task 6.7 pinned that the two merge_key sinks pass DIFFERENT context strings.

    A third sink inherits that discipline: its context must not be confusable
    with either, so a log line identifies which sink refused.
    """
    assert "merge_key" not in _ca._RENAME_TARGET_SINK
    assert _ca._RENAME_TARGET_SINK != "MERGE statement merge_key (target_config.merge_key)"
    assert _ca._RENAME_TARGET_SINK != "MERGE statement merge_key (--merge_key Glue job argument)"
    assert "Rename_Manifest" in _ca._RENAME_TARGET_SINK


def test_the_pre_existing_sinks_still_win_the_precedence():
    """An input that trips merge_key still fails with THAT sink's error.

    The projection is a third identifier sink and it was inserted AFTER the two
    pre-existing ones and BEFORE the undeclared-source-field refusal, so the
    documented precedence (p37 R2.7) is unchanged.
    """
    metadata = _pipeline_metadata(_object_schema("first name"), merge_key="bad key")
    mapping_result = {"field_mappings": [_entry("first name", "first name")]}
    with pytest.raises(IdentifierValidationError) as excinfo:
        _generate(metadata, mapping_result, _manifest(("first name", "first name")))
    assert "target_config.merge_key" in str(excinfo.value)


def test_identifier_utils_is_called_not_reimplemented():
    """do-NOT 3. The projection must go THROUGH the shared validator."""
    calls: list[tuple] = []
    real = _ca.validate_identifier

    def spy(name, context):
        calls.append((name, context))
        return real(name, context)

    original = _ca.validate_identifier
    _ca.validate_identifier = spy
    try:
        _ca._rename_projection_block([("first name", "first_name"), ("2024-total", "_2024_total")])
    finally:
        _ca.validate_identifier = original
    assert calls == [
        ("first_name", _ca._RENAME_TARGET_SINK),
        ("_2024_total", _ca._RENAME_TARGET_SINK),
    ]


# ===========================================================================
# A malformed manifest refuses rather than silently renaming nothing
# ===========================================================================


@pytest.mark.parametrize(
    "manifest",
    [
        {"renames": "not-a-list"},
        {"renames": ["not-an-object"]},
        {"renames": [{"original_name": "first name"}]},
        {"renames": [{"target_name": "first_name"}]},
        {"renames": [{"original_name": None, "target_name": "first_name"}]},
        {"renames": [{"original_name": "first name", "target_name": 7}]},
    ],
)
def test_a_structurally_invalid_manifest_refuses(manifest):
    """Returning ``[]`` would silently skip every rename and hand the original
    names to the DQDL/MERGE sink — do-NOT 1's failure mode in a different hat."""
    with pytest.raises(StepFailure):
        _ca._rename_pairs(manifest)


def test_two_targets_for_one_original_refuses():
    manifest = {
        "manifest_id": "m",
        "renames": [
            {"original_name": "first name", "target_name": "first_name"},
            {"original_name": "first name", "target_name": "first_name_2"},
        ],
    }
    with pytest.raises(StepFailure) as excinfo:
        _ca._rename_pairs(manifest)
    assert "'first name'" in str(excinfo.value)


def test_an_exactly_duplicated_entry_is_idempotent_not_a_refusal():
    """The same pair twice can be honoured — it is one rename, stated twice."""
    manifest = {
        "renames": [
            {"original_name": "first name", "target_name": "first_name"},
            {"original_name": "first name", "target_name": "first_name"},
        ]
    }
    assert _ca._rename_pairs(manifest) == [("first name", "first_name")]


# ===========================================================================
# LGR-a419843-2 — source_field keeps naming the ORIGINAL column
# ===========================================================================


def test_the_declared_source_field_check_still_sees_original_names():
    """The mappings handed to ``_validate_mapped_source_fields`` are UNTRANSLATED.

    ``schema_extract`` declares ``first name``; the mapping names ``first name``;
    the manifest renames it. If the check saw the translated name it would refuse
    a perfectly good mapping, because ``first_name`` is not a declared column.
    """
    metadata = _pipeline_metadata(_object_schema("first name"))
    mapping_result = {"field_mappings": [_entry("first name", "first name")]}
    rendered, _ = _generate(metadata, mapping_result, _manifest(("first name", "first_name")))
    assert 'df = df.withColumnRenamed("first name", "first_name")' in rendered


def test_a_manifest_does_not_rescue_an_undeclared_source_field():
    """``LGR-a419843-2``'s refusal still bites, and still names the offender."""
    metadata = _pipeline_metadata(_object_schema("first name"))
    mapping_result = {"field_mappings": [_entry("email", "email")]}
    with pytest.raises(StepFailure) as excinfo:
        _generate(metadata, mapping_result, _manifest(("first name", "first_name")))
    assert "'email'" in str(excinfo.value)


def test_the_caller_s_mapping_entries_are_not_mutated():
    """The translation returns shallow copies. Mutating in place would change the
    record the undeclared-source-field check and every later reader see."""
    entries = [_entry("first name", "first name")]
    mapping_result = {"field_mappings": entries}
    _generate(_pipeline_metadata(_object_schema("first name")), mapping_result, _manifest(("first name", "first_name")))
    assert entries[0]["source_field"] == "first name"
    assert entries[0]["target_field"] == "first name"


# ===========================================================================
# The translation itself
# ===========================================================================


def test_the_translation_touches_both_sides_of_a_mapping_entry():
    lookup = {"first name": "first_name"}
    translated = _ca._renamed_field_mappings([_entry("first name", "first name"), _entry("id", "id")], lookup)
    assert translated[0] == {"target_field": "first_name", "source_field": "first_name", "confidence": 0.9}
    assert translated[1] == {"target_field": "id", "source_field": "id", "confidence": 0.9}


def test_an_empty_lookup_is_the_identity():
    entries = [_entry("first_name", "first_name")]
    assert _ca._renamed_field_mappings(entries, {}) == entries
    assert _ca._renamed_names(["a", "b"], {}) == ["a", "b"]


def test_the_lookup_is_idempotent():
    """A target satisfies the allowlist and every original fails it, so a target
    can never itself be a key. That is what lets the unsourced-field list be
    translated after it was derived from already-translated mappings."""
    lookup = _ca._rename_lookup(_ca._rename_pairs(_manifest(("first name", "first_name"))))
    once = _ca._renamed_names(["first name", "id"], lookup)
    assert once == ["first_name", "id"]
    assert _ca._renamed_names(once, lookup) == once


def test_a_non_dict_mapping_entry_is_tolerated_not_refused():
    """Matching every other reader in this module: a mapping record is parsed
    JSON and this module never assumes shape."""
    assert _ca._renamed_field_mappings(["nonsense", None], {"a": "b"}) == ["nonsense", None]


def test_the_transform_prompt_is_given_the_renamed_mappings():
    """R11.2: the projection runs before the transform in the emitted job, so a
    prompt naming the originals would produce a transform reading columns that no
    longer exist."""
    metadata = _pipeline_metadata(_object_schema("first name"))
    mapping_result = {"field_mappings": [_entry("first name", "first name")]}
    _, transform = _generate(metadata, mapping_result, _manifest(("first name", "first_name")))
    (call,) = transform.calls
    assert call["mappings"] == [{"target_field": "first_name", "source_field": "first_name", "confidence": 0.9}]
    # `source_schema` is still the ORIGINAL extracted schema — it is accepted and
    # dropped by the generator (p33 design R4, `LGR-1e135d2-8`) and nothing about
    # that asymmetry changes here.
    assert call["source_schema"] == _object_schema("first name")


def test_the_unsourced_annotation_names_the_approved_column():
    """The annotated set must stay equal to the Completeness-skipped set.

    ``_generate_dqdl_rules`` is now given the renamed mappings, so the annotation
    has to be translated too or the model would be told to produce a column named
    by its original while every other column carries its approved name.
    """
    metadata = _pipeline_metadata(_object_schema("email"))
    mapping_result = {
        "field_mappings": [_entry("email", "email")],
        "unmapped_target_fields": [{"field": "phone number", "reason": "no source"}],
    }
    rendered, transform = _generate(metadata, mapping_result, _manifest(("phone number", "phone_number")))
    (call,) = transform.calls
    assert call["unsourced_target_fields"] == ["phone_number"]
    # And still no rule for it, under either name.
    assert 'Completeness "phone_number"' not in rendered
    assert 'Completeness "phone number"' not in rendered


# ===========================================================================
# LGR-9e76249-10 — the render budget, measured rather than asserted about
# ===========================================================================


def test_the_projection_grows_the_render_by_exactly_one_line_per_rename():
    """The budget consequence, stated as arithmetic.

    The emitted block is a FIXED 3-line header plus one line per renamed column,
    so the render grows by ``3 + n`` and by nothing else. That is what makes the
    260-line proxy's exposure computable instead of a surprise: under this test's
    own render vars the four templates sit at 240 / 216 / 191 / 185 with no
    manifest, so the largest manifest each can carry is 16 / 40 / 65 / 71
    renames. A wider non-conforming source than that breaches the proxy — see the
    task report for why that is accepted rather than designed around.
    """
    metadata = _pipeline_metadata(_object_schema("email"))
    mapping_result = {"field_mappings": [_entry("email", "email")]}
    baseline, _ = _generate(metadata, mapping_result, rename_manifest=None)
    base_lines = len(baseline.strip().splitlines())

    for n in (1, 2, 5, 20):
        pairs = tuple((f"col {i}", f"col_{i}") for i in range(n))
        rendered, _ = _generate(metadata, mapping_result, _manifest(*pairs))
        grew = len(rendered.strip().splitlines()) - base_lines
        assert grew == len(_ca._RENAME_BLOCK_HEADER) + n, f"{n} renames grew the render by {grew}"
        ast.parse(rendered)
