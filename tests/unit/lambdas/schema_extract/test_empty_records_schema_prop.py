# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for the zero-field schema route (p64 task 1a.2, R1).

Companion to `test_empty_records_schema.py`, which carries the examples, the
four-route analysis and the full provenance note — including why the module is
loaded under a private alias (`LGR-c58f276-4`) rather than published to
`sys.modules` the way task 1a.1's `file_validation` loader does.

These are **characterization properties**: p64's design document names no
numbered property for this symbol, only that the empty-records branch be
characterized before task 1a.4 edits its file. Each one below generalises one of
that file's examples, and each is a direct reading of R1's premise that a source
with no rows must not be reported as having usable fields.

`@settings(max_examples=10)` per `.kiro/steering/testing-standards.md`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_SHARED = os.path.join(_REPO, "shared")
_SCHEMA_EXTRACT = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_extract")

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


def _load_schema_extract():
    """Exec ``schema_extract/lambda_function.py`` under a private alias.

    See the companion file's "MODULE LOADING" section. Holding the module across
    ~10 examples per property is exactly the shape ``LGR-c58f276-4`` is about,
    which is why the delete-and-reimport idiom used elsewhere in this directory
    is not used here.
    """
    s3 = MagicMock(name="s3_client")
    glue = MagicMock(name="glue_client")
    state_table = MagicMock(name="state_table")
    dynamodb = MagicMock(name="dynamodb_resource")
    dynamodb.Table.return_value = state_table

    boto3_stub = MagicMock(name="boto3_module")
    boto3_stub.client.side_effect = lambda service, *a, **kw: glue if service == "glue" else s3
    boto3_stub.resource.return_value = dynamodb

    added = [p for p in (_SHARED, _SCHEMA_EXTRACT) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(
            "_p64_1a2_prop_schema_extract_lambda_function", os.path.join(_SCHEMA_EXTRACT, "lambda_function.py")
        )
        assert spec is not None and spec.loader is not None, "cannot load schema_extract/lambda_function.py"
        module = importlib.util.module_from_spec(spec)
        with _ScopedStubs({"boto3": boto3_stub}):
            spec.loader.exec_module(module)
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)

    module.s3_client = s3
    module.glue_client = glue
    module.table = state_table
    return module, s3


_MOD, _S3 = _load_schema_extract()

# Import-time non-vacuity guards, per LGR-c58f276-4. Bound to a mock, every
# property below would pass vacuously — `assert x == y` on two MagicMock
# attributes is not a test — so the loaded module is asked questions with known
# answers before any property runs.
assert not isinstance(_MOD, MagicMock), "schema_extract resolved to a mock, not the real module"
for _attr in ("infer_schema_from_records", "extract_schema_from_csv", "_get_type"):
    assert hasattr(_MOD, _attr), f"the real schema_extract has no {_attr}; the private load was defeated"
assert _MOD._get_type(None) == "null" and _MOD._get_type(True) == "boolean", (
    "the loaded _get_type does not classify known values; it is not the real one"
)

infer_schema_from_records = _MOD.infer_schema_from_records
extract_schema_from_csv = _MOD.extract_schema_from_csv


# ── Strategies and transcribed constants ──────────────────────────────────
#
# The empty-schema literal is transcribed from `lambda_function.py:161` rather
# than captured from a call, so the producer is not its own oracle — the
# reasoning task 1a.1 states at
# `tests/unit/lambdas/file_validation/test_format_detection_prop.py:63-71`.
EMPTY_SCHEMA = {"type": "object", "properties": {}}

BUCKET = "adi-source-files-p64-1a2"
KEY = "pipeline-p64/uploads/patients-empty.csv"

#: Values that are never dicts, so `infer_schema_from_records` skips every one of
#: them at `:165-166`. Deliberately excludes dicts rather than filtering with
#: `assume`, which at max_examples=10 would discard most of the budget.
non_dict_values = st.one_of(
    st.text(max_size=20),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.integers(), max_size=3),
)

#: CSV-shaped field names: what S3 Select's `FileHeaderInfo: USE` yields as keys.
field_names = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)

#: Column lists for a headers-only CSV — at least one column, all distinct, so
#: the file genuinely has a header and no data rows.
header_column_lists = st.lists(field_names, min_size=1, max_size=8, unique=True)


# ── Passing today: the zero-field routes, generalised ─────────────────────


@given(records=st.lists(non_dict_values, min_size=1, max_size=10))
@settings(max_examples=10)
def test_no_dict_record_means_no_fields(records):
    """**Validates: Requirements R1.3**

    Route (b) generalised. `records` is truthy for every example, so the `:160`
    early return never fires; every element is skipped at `:165-166` instead.
    This is the class an input-side `if not records: raise` guard would miss, and
    it is why task 1a.4's guard belongs on the returned schema the way the Avro
    (`:1301-1307`) and Excel (`:1698-1710`) guards are written.
    """
    assert infer_schema_from_records(records) == EMPTY_SCHEMA


@given(
    records=st.lists(st.dictionaries(field_names, st.text(max_size=10), min_size=1, max_size=5), min_size=1, max_size=5)
)
@settings(max_examples=10)
def test_a_zero_sample_bound_means_no_fields_however_much_was_read(records):
    """**Validates: Requirements R1.3**

    Route (d) generalised, over records that all genuinely carry fields.
    `records[:0]` is empty however many records were read, so the sample bound
    alone produces a zero-field schema from a file that has data.
    `extract_schema_from_csv` forwards `sample_rows` here as `max_sample`
    (`:230`, `:252`), so a caller passing zero reaches this.
    """
    assert infer_schema_from_records(records, max_sample=0) == EMPTY_SCHEMA


@given(
    records=st.lists(
        st.dictionaries(field_names, st.text(max_size=10), min_size=1, max_size=5), min_size=2, max_size=5
    ),
    drop=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=10)
def test_a_negative_sample_bound_samples_a_prefix_rather_than_nothing(records, drop):
    """**Validates: Requirements R1.3** — corrected by a counter-example, and a finding.

    An earlier draft of this file asserted that *every* non-positive `max_sample`
    yields a zero-field schema. Hypothesis falsified it in one example —
    `records=[{'a': ''}, {'a': ''}], max_sample=-1` produced
    `{'a': {'type': 'string'}}` — because `records[:-1]` drops the **last**
    element rather than taking none. The test was wrong, not the code; it is
    corrected here rather than relaxed, and the real behaviour is pinned instead.

    What that leaves is worth recording: a negative bound is not "sample
    nothing", it is "drop the last |n| records", so it yields a zero-field schema
    only when the bound is at least the record count. That still puts a fifth
    reachable zero-field route beside routes (a)–(d), and it is a *silent* one —
    a bound of `-100` over 3 records reports zero fields for a file that has
    data, with nothing logged.
    """
    result = infer_schema_from_records(records, max_sample=-drop)

    # The observable is the prefix, not emptiness.
    assert result == infer_schema_from_records(records[:-drop])

    if drop >= len(records):
        assert result == EMPTY_SCHEMA, "a bound at least the record count drops everything"
    else:
        assert result["properties"], "a bound below the record count still samples a prefix"


@given(
    records=st.one_of(
        st.none(),
        st.lists(non_dict_values, max_size=5),
        st.lists(st.dictionaries(field_names, st.text(max_size=5), max_size=3), max_size=5),
        st.text(max_size=5),
    )
)
@settings(max_examples=10)
def test_the_return_is_always_a_well_formed_schema_and_never_a_raise(records):
    """**Validates: Requirements R1.3, R1.5**

    The totality claim task 1a.4 has to decide about: today this helper answers
    every input with a two-key `{"type": "object", "properties": {...}}` and
    raises for none of them, including every zero-field route.

    Characterization, not a fence. R1.3 puts the refusal in the CSV branch
    "matching the Avro and Excel branches", both of which guard the extractor's
    output rather than this shared helper — which is also on the JSON path
    (`:337`) that R1.3 does not name. So this property is expected to survive
    1a.4. If 1a.4 makes the helper raise, this fails, and that failure is the
    prompt to justify the location against the routes above.
    """
    result = infer_schema_from_records(records)

    assert set(result) == {"type", "properties"}
    assert result["type"] == "object"
    assert isinstance(result["properties"], dict)


@given(
    records=st.lists(st.dictionaries(field_names, st.text(max_size=10), min_size=1, max_size=5), min_size=1, max_size=5)
)
@settings(max_examples=10)
def test_records_that_carry_fields_still_produce_them(records):
    """**Validates: Requirements R1.5**

    Non-vacuity for the three properties above: a mutant returning the empty
    schema unconditionally would satisfy all of them. R1.5 requires a source
    with rows to keep its current path, so the positive direction is asserted
    generatively too — every key present in the sampled records appears as a
    property, and the schema is never empty.
    """
    expected_fields = {key for record in records for key in record}

    result = infer_schema_from_records(records)

    assert set(result["properties"]) == expected_fields
    assert result["properties"], "records carrying fields must not yield an empty schema"


# ── R1.3/R1.4's refusal, delivered by task 1a.4 ───────────────────────────
#
# The property below carried `xfail(strict=True)` while the refusal did not
# exist. 1a.4 added it — `extract_schema_from_csv` guards the returned schema's
# `properties` past both read arms — so it XPASSed, which under `strict=True` is
# a loud failure and was the intended prompt to remove the marker. Removed, the
# property kept unchanged.


@given(columns=header_column_lists)
@settings(max_examples=10)
def test_no_header_shape_makes_a_rowless_csv_acceptable(columns):
    """**Validates: Requirements R1.3, R1.4**

    A file consisting of a header line and nothing else has no data rows,
    whatever its columns are named. `csv.DictReader` genuinely reads the
    generated header, so the zero-rows condition comes from the stdlib rather
    than from a mocked payload — and the generated column list means no
    assertion can be satisfied by a fixed string.

    The line that forced an S3 Select failure to reach this read was dropped when
    that arm was removed from `extract_schema_from_csv`: there is one read path
    now, so there is nothing to fall back *from*. The docstring used to call this
    "the Range-GET arm", which was the right name for one of two.
    """
    _S3.reset_mock(side_effect=True)
    body = MagicMock()
    body.read.return_value = (",".join(columns) + "\n").encode("utf-8")
    _S3.get_object.return_value = {"Body": body}

    with pytest.raises(ValueError):
        extract_schema_from_csv(BUCKET, KEY)
