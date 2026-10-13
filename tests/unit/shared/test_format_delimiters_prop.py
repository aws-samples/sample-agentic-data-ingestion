# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test for ``shared/format_delimiters.py`` (p55 task 1.3).

# Feature: p55-format-support-and-teardown, Property 5: The delimiter lookup is total

*For any* extension, :func:`delimiter_for_key` returns a single character and
returns ``","`` for anything absent from the table.

**Validates: Requirements 4.6**

Why this is a property and not a handful of examples
---------------------------------------------------
The table's entries and the four named default shapes are unit tests
(``test_format_delimiters.py``). This test covers the *space*. Totality is what
the four consumers will rely on: ``job_arguments.py`` (task 2.5) writes the
result straight into a Glue job argument, ``create_code`` (2.6) puts it in a
render context, ``test_code`` (2.7) into a sample read and ``schema_extract``
(2.8) into an S3 Select ``FieldDelimiter`` and a ``csv.DictReader``. None of
those four has anywhere to put a raise or a ``None``, and every one of them is
handed an S3 key that came from a user-chosen filename. So "no key escapes with
anything other than one character" is the claim they are all built on, and it is
asserted here over generated keys rather than assumed from a `.get()` with a
default.

The three clauses, and what each would catch
--------------------------------------------
(a) **Always exactly one character.** Catches a table entry widened to a
    multi-character separator (``"\\t\\t"``, ``"|,"``) and catches a default
    changed to the empty string. Spark's CSV reader and ``csv.DictReader`` both
    require a single character, so a two-character answer is a runtime failure
    one Lambda downstream of here.

(b) **Never raises.** The call sits outside any ``pytest.raises``, so any
    exception from any generated key fails this test. There is nothing to
    assert; that is the mechanism. Keys are generated with dots, slashes,
    control characters, surrogate-free unicode and empty segments, because
    ``delimiter_for_key`` does string surgery (``.lower()``, two ``rsplit``s,
    an index) and each of those is where a total-looking function stops being
    total.

(c) **An extension absent from the table gets exactly ``","``.** The half of
    R4.6 that names a value rather than a shape. The generator deliberately
    draws both from the table's own keys and from arbitrary text, so the
    absent-key arm is exercised far more often than the present-key one — the
    present arm is where the unit tests live.

What is deliberately NOT asserted here
--------------------------------------
That a particular extension maps to a particular character. That is R4.3/R4.4
and it belongs in the example tests, where a changed answer names the extension
that changed. A property test that also pinned the table would fail with a
generated key on any table amendment, which tells a reader nothing about what
they broke.

Ledger status
-------------
``why`` on ``shared/format_delimiters.py`` returns ``unknown_path`` — new file,
no history. ``decisions(concept="testing", include_backfilled=True)`` returns
nothing governing a pure lookup's test shape. ``LGR-1e135d2-11`` (**template /
ratified**) is the one that shapes what happens next if this test finds
something: ``.hypothesis/examples`` is not committed, so a counterexample does
not survive a fresh clone and must be pinned as an example test in
``test_format_delimiters.py`` rather than left to the property test's cache.
"""

from __future__ import annotations

import pathlib
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

SHARED_DIR = str(pathlib.Path(__file__).resolve().parents[3] / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

# Imported after the sys.path shim above, per this tree's convention for
# shared/ modules (ruff's E402 is globally ignored in pyproject.toml for
# exactly this).
from format_delimiters import DEFAULT_DELIMITER, FORMAT_DELIMITERS, delimiter_for_key

#: The compression suffixes the lookup must see through, plus the empty case.
#: Spelled here rather than imported from the module's private
#: ``_COMPRESSION_SUFFIXES`` on purpose: a suffix silently dropped from that set
#: should change what a generated `data.tsv.gz` answers, and a generator reading
#: the same set could not notice.
COMPRESSION_SUFFIXES = ("", "gz", "bz2", "snappy", "zst", "GZ", "zip", "xz")

#: Extension-shaped text. Three branches rather than one. The table's own keys
#: keep the present-key arm reachable at all — arbitrary text hits it with
#: vanishing probability — while the alphabet and free-text branches are what
#: actually exercise clause (c). ``st.text()`` is unrestricted on purpose: an
#: extension is a substring of a user-chosen filename, so it can hold anything
#: a filename can, including dots and slashes that re-enter the ``rsplit``.
EXTENSIONS = st.one_of(
    st.sampled_from(sorted(FORMAT_DELIMITERS)),
    # `txt` was drawn here until p64 1c.1. It is in the table now (R3.2), so it
    # belongs to the branch above; leaving it here would have kept passing —
    # clause (c)'s guard skips any key whose extension is in the table — while
    # reading as though the absent-key arm still covered it.
    st.sampled_from(["json", "jsonl", "ndjson", "parquet", "pq", "avro", "orc", "xlsx", "dat"]),
    st.text(min_size=0, max_size=12),
)

#: Key prefixes: the empty one, a plain filename stem, a nested path, and a
#: stem carrying its own dots. The last two are the shapes that make the
#: whole-string ``rsplit`` observable — ``detect_format`` scans the entire key,
#: not the last path segment, and this module mirrors that exactly.
PREFIXES = st.one_of(
    st.just(""),
    st.just("data"),
    st.just("pipeline-1/uploads/2026/data"),
    st.just("v1.2.3/output/result"),
    st.text(min_size=0, max_size=16),
)


@given(
    prefix=PREFIXES,
    extension=EXTENSIONS,
    compression=st.sampled_from(COMPRESSION_SUFFIXES),
)
@settings(max_examples=300)
def test_the_delimiter_lookup_is_total(prefix: str, extension: str, compression: str) -> None:
    """Feature: p55-format-support-and-teardown, Property 5: The delimiter lookup
    is total.

    **Validates: Requirements 4.6**
    """
    key = prefix
    if extension:
        key = f"{key}.{extension}"
    if compression:
        key = f"{key}.{compression}"

    # Clause (b): outside any pytest.raises, so any exception is a failure.
    delimiter = delimiter_for_key(key)

    # Clause (a).
    assert isinstance(delimiter, str)
    assert len(delimiter) == 1, f"delimiter_for_key({key!r}) returned {delimiter!r}"

    # Clause (c). The extension the lookup resolves to is the compressed
    # suffix's base when there is one, otherwise the trailing segment — so the
    # answer is the table's entry when that resolved extension is in the table,
    # and the default otherwise. Recomputing the resolution here would just be
    # a second copy of the function under test, so instead: whatever the
    # answer is, it is one of the table's values or the default, and it is the
    # default whenever no table entry could apply.
    assert delimiter in set(FORMAT_DELIMITERS.values()) | {DEFAULT_DELIMITER}

    lowered = key.lower()
    if not any(f".{present}" in lowered for present in FORMAT_DELIMITERS):
        # No table key appears anywhere in the key, so no lookup can hit —
        # the answer must be the default, by name and not merely by shape.
        assert delimiter == DEFAULT_DELIMITER, f"delimiter_for_key({key!r}) returned {delimiter!r}"


@given(key=st.text(max_size=64))
@settings(max_examples=300)
def test_an_arbitrary_string_is_a_legal_input(key: str) -> None:
    """Feature: p55-format-support-and-teardown, Property 5: The delimiter lookup
    is total.

    The unstructured half. The generator above builds key-shaped strings, which
    is where the interesting answers are; this one drops the structure entirely,
    because the four consumers pass whatever S3 handed them and an S3 key is
    almost unconstrained text. Nothing about the *value* is asserted beyond the
    shape — that is the previous test's job.

    **Validates: Requirements 4.6**
    """
    delimiter = delimiter_for_key(key)

    assert isinstance(delimiter, str)
    assert len(delimiter) == 1, f"delimiter_for_key({key!r}) returned {delimiter!r}"
