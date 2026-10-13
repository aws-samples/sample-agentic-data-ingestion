# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests for the zero-field schema route (p64 task 1a.2, R1).

`infer_schema_from_records` (`schema_extract/lambda_function.py:149`) returns
`{"type": "object", "properties": {}}` when handed no records (`:160-161`), and
`extract_schema_from_csv` (`:189`) hands that straight back to the handler, which
logs the field count (`:2770`) and publishes `status="completed"`. A headers-only
CSV therefore reports **success** at step 2 — the defect R1.3 names.

**Expectations here are derived from R1, not from current output.** Where R1 and
the code disagreed, the test carried `xfail(strict=True)` naming both the
requirement and task 1a.4. **Task 1a.4 has landed** — `extract_schema_from_csv`
now raises on a zero-field schema — so those markers XPASSed, failed loudly as
`strict=True` intends, and have been removed. The tests themselves are unchanged
and now pass on their own terms. The prose below is left in the tense it was
written in where it describes what 1a.4 had to decide, because that reasoning is
the argument for where the guard ended up.

WHAT THIS FILE ADDS OVER THE EXISTING COVERAGE
----------------------------------------------
`test_streaming_schema.py` already exercises this symbol
(`TestInferSchemaFromRecords`, `:114-211`) and already pins two of the facts p64
cares about, so neither is restated here:

  * `test_empty_records` (`:118-121`) pins `infer_schema_from_records([]) ==
    {"type": "object", "properties": {}}`.
  * `test_csv_empty_payload` pinned `extract_schema_from_csv` returning that same
    empty schema rather than raising.

**That second one was the defect, pinned.** Task 1a.4 broke it, and per this
repo's convention for a deliberate reversal it was **amended in place with its
reason at the assertion** rather than deleted (`LGR-60c4985-6` /
`LGR-9e76249-10`, the same treatment `LGR-e59edd7-5` applied to an integration
test that pinned a broken read). It is now
`test_csv_empty_payload_is_refused`. The same treatment was applied to this
file's own `test_both_csv_arms_are_reached_by_the_two_harness_helpers`, which
used the forbidden return as its evidence that each harness reached the arm it
claimed — 1a.2 did not anticipate that one, so it is recorded here.

What is genuinely uncovered, and is what this file characterizes: the
zero-field schema has **four** distinct routes through
`infer_schema_from_records`, and only the first is guarded at `:160`.

  (a) falsy `records` — the `:160-161` early return. `[]` is pinned at the
      sibling; the non-list falsy inputs are not.
  (b) records present, **none** of them a dict — every one skipped at
      `:165-166`, so `field_types` stays empty. The sibling's
      `test_non_dict_records_skipped` (`:207-211`) covers a *mixed* list where
      one dict survives; the all-non-dict case is uncovered.
  (c) records present, all dicts, all **empty** — `.items()` yields nothing.
  (d) records present and populated, but the sample bound excludes all of them —
      `max_sample == 0`, or a negative bound at least the record count. A
      negative bound *below* the record count silently samples a prefix instead,
      which is a separate finding pinned below; it was found by a hypothesis
      counter-example against a first draft that claimed otherwise.

This matters to 1a.4 rather than being trivia: a guard written as
`if not records: raise` **inside** this helper would catch (a) only and leave
(b), (c) and (d) reachable. The Avro and Excel guards R1.3 tells 1a.4 to copy do
not have that hole, because they interrogate the *returned schema's*
`properties` in the extractor (`:1301-1307`, `:1698-1710`) rather than the input
that produced it. That is an argument for where 1a.4 puts its guard, and it is
why the four routes are pinned as passing tests here.

MODULE LOADING — a private copy, never published to sys.modules
---------------------------------------------------------------
`lambda_function.py` is a name many Lambda directories share. `LGR-c58f276-4`
(**template / ratified**) records the defect class that causes — collection order
deciding what a module-scope import binds — and ratifies the remedy used here:
load the file under a private alias that is never published, then assert at
import time that what was bound is neither a mock nor vacuous.

This is deliberately **not** the idiom task 1a.1 used in
`tests/unit/lambdas/file_validation/test_record_count_estimate.py:78-84`, which
publishes `sys.modules["lambda_function"]`. That is safe for its own examples,
but publishing the same key from a second Lambda directory is the shape
`LGR-c58f276-4` is about — so this file takes the private alias and the
`file_validation` gate is run alongside this directory's to show the two do not
interfere. The sibling `test_sample_uri_preference_prop.py:62-88` in this
directory reaches the same conclusion for the same reason.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

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

    Returns ``(module, s3)``. ``boto3`` is stubbed for the load only, so the
    module-level ``boto3.client("s3")`` resolves to the returned fake; the handle
    is rebound afterwards regardless of how ``boto3.client`` was wired, the same
    defensive step the sibling takes at ``test_jdbc_path.py:104-107``. No
    ``moto.mock_aws()`` and no real client — this file is not on
    ``test_no_duplicate_mocking_prop.py``'s allowlist and does not need to be.
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
            "_p64_1a2_schema_extract_lambda_function", os.path.join(_SCHEMA_EXTRACT, "lambda_function.py")
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

# Import-time non-vacuity guards, per LGR-c58f276-4. `hasattr` alone is not a
# guard — a MagicMock has every attribute — so the loaded module is also asked
# questions with known answers. Bound to a mock, every assertion in this file
# would pass vacuously, which is the failure mode that record exists to prevent.
assert not isinstance(_MOD, MagicMock), "schema_extract resolved to a mock, not the real module"
for _attr in ("infer_schema_from_records", "extract_schema_from_csv", "_get_type"):
    assert hasattr(_MOD, _attr), f"the real schema_extract has no {_attr}; the private load was defeated"
assert _MOD._get_type(None) == "null" and _MOD._get_type(1) == "integer", (
    "the loaded _get_type does not classify known values; it is not the real one"
)

infer_schema_from_records = _MOD.infer_schema_from_records
extract_schema_from_csv = _MOD.extract_schema_from_csv


# ── The empty-schema literal, transcribed ─────────────────────────────────
#
# Transcribed from `lambda_function.py:161`, NOT imported or captured from a
# call — importing the producer's own answer would make it its own oracle, which
# is the reasoning task 1a.1 states at
# `tests/unit/lambdas/file_validation/test_format_detection_prop.py:63-71`. If
# the shape changes, these tests are meant to fail rather than silently re-derive
# the new one.
EMPTY_SCHEMA = {"type": "object", "properties": {}}

# A realistic wide header, the shape p64's source report used
# (`patients-empty.csv`): a real column list and no data rows.
HEADERS_ONLY_COLUMNS = [
    "patient_id",
    "first_name",
    "last_name",
    "date_of_birth",
    "medical_record_number",
    "admission_date",
]

BUCKET = "adi-source-files-p64-1a2"
KEY = "pipeline-p64/uploads/patients-empty.csv"


def _range_get_yields_headers_only(columns: list[str]) -> None:
    """Stub the bounded Range GET over a real header-only chunk.

    `csv.DictReader` runs for real here, so the zero-rows condition is the
    stdlib's rather than a mock's — which is what makes the refusal below a
    statement about behaviour and not about a fixture.

    AMENDED when `extract_schema_from_csv`'s S3 Select arm was removed. This
    helper used to begin by forcing that arm to fail
    (`select_object_content.side_effect = RuntimeError(...)`) because a fallback
    could only be reached through a failure. There is one read path now, so the
    forcing line is gone and this is simply how the reader is fed. Its companion
    `_s3_select_yields_no_records` was deleted with the arm it wired.
    """
    _S3.reset_mock(side_effect=True)
    body = MagicMock()
    body.read.return_value = (",".join(columns) + "\n").encode("utf-8")
    _S3.get_object.return_value = {"Body": body}


# ── Passing today: the four routes to a zero-field schema ─────────────────


@pytest.mark.parametrize("records", [None, "", {}, 0])
def test_falsy_records_take_the_early_return(records):
    """**Validates: Requirements R1.3**

    Route (a). `[]` is already pinned at `test_streaming_schema.py:118-121`; the
    non-list falsy inputs are not, and they reach the same `:160` branch. Pinned
    because 1a.4 edits that branch's neighbourhood and a guard added there must
    keep treating every falsy input alike rather than only the empty list.
    """
    assert infer_schema_from_records(records) == EMPTY_SCHEMA


def test_records_present_but_none_a_dict_yields_zero_fields():
    """**Validates: Requirements R1.3**

    Route (b), and the first of three that a `if not records:` guard inside this
    helper would **not** catch. `records` is truthy, so `:160` does not fire;
    every element is then skipped at `:165-166`, leaving `field_types` empty.

    The sibling's `test_non_dict_records_skipped` (`:207-211`) passes a mixed
    list in which one dict survives, so it proves the skip happens but never
    reaches a zero-field result.
    """
    assert infer_schema_from_records(["not a dict", 42, None, ["a"]]) == EMPTY_SCHEMA


def test_records_present_but_every_dict_empty_yields_zero_fields():
    """**Validates: Requirements R1.3**

    Route (c). Each record is a dict and is not skipped, but `.items()` yields
    nothing, so the resolution loop at `:172-184` never runs. This is the shape
    a JSON source of `[{}, {}]` produces via `_parse_partial_json`.
    """
    assert infer_schema_from_records([{}, {}, {}]) == EMPTY_SCHEMA


def test_max_sample_of_zero_yields_zero_fields_from_populated_records():
    """**Validates: Requirements R1.3**

    Route (d). `records[:0]` is empty however many records were read, so the
    sample bound alone can produce a zero-field schema from a file that has
    data. `extract_schema_from_csv` forwards its `sample_rows` here as
    `max_sample` (`:230`, `:252`), so this is reachable from a caller that passes
    a zero bound, not only from a hand-written call.
    """
    populated = [{"patient_id": "1", "first_name": "Alice"}]

    assert infer_schema_from_records(populated, max_sample=0) == EMPTY_SCHEMA


def test_a_negative_max_sample_drops_the_last_records_rather_than_all_of_them():
    """**Validates: Requirements R1.3** — corrected by a counter-example.

    The companion property file asserted, in an earlier draft, that *every*
    non-positive `max_sample` yields a zero-field schema. Hypothesis falsified it:
    `records[:-1]` drops the **last** element, so two records under a bound of
    `-1` still produce one record's fields. The test was wrong, not the code.

    Pinned as two examples because the distinction is the whole finding: a
    negative bound yields a zero-field schema only when it is at least the record
    count, and when it is, nothing is logged — a fifth silent route to route
    (d)'s outcome.
    """
    two_records = [{"patient_id": "1"}, {"first_name": "Alice"}]

    # Bound below the record count — a prefix survives.
    assert infer_schema_from_records(two_records, max_sample=-1) == {
        "type": "object",
        "properties": {"patient_id": {"type": "string"}},
    }

    # Bound at or above the record count — everything is dropped, silently.
    assert infer_schema_from_records(two_records, max_sample=-2) == EMPTY_SCHEMA
    assert infer_schema_from_records(two_records, max_sample=-100) == EMPTY_SCHEMA


def test_a_populated_record_still_yields_its_fields():
    """**Validates: Requirements R1.5**

    Non-vacuity for the four tests above: a mutant that returned the empty
    schema unconditionally would satisfy all of them. R1.5 requires a source
    with rows to keep its current path, so the positive case is asserted beside
    the negative ones rather than left to another file.
    """
    result = infer_schema_from_records([{"patient_id": "1", "admission_date": "2026-01-01"}])

    assert result["type"] == "object"
    assert set(result["properties"]) == {"patient_id", "admission_date"}


def test_the_helper_does_not_raise_on_any_zero_field_input():
    """**Validates: Requirements R1.3, R1.5**

    The claim 1a.4 has to decide about, stated as a test: today this helper is
    total — it answers every input with a schema and raises for none of them.

    Pinned as *characterization*, not as a fence. R1.3 puts the refusal in the
    CSV branch "matching the Avro (`:1301-1307`) and Excel (`:1698-1710`)
    branches", both of which guard the extractor's returned schema rather than
    this shared helper — and this helper is also on the JSON path (`:337`), which
    R1.3 does not name. So the expectation is that 1a.4 leaves this test passing.
    If 1a.4 instead makes the helper raise, this test fails and that is the
    intended prompt to justify the location against routes (b), (c) and (d)
    above, which an input-side guard does not reach.
    """
    for records in (None, [], [{}], ["not a dict"]):
        assert infer_schema_from_records(records) == EMPTY_SCHEMA


# ── Not passing today: R1.3/R1.4's refusal, which 1a.4 delivers ───────────

# CLOSED BY TASK 1a.4. The two tests below carried
# `xfail(strict=True, reason=_XFAIL_REASON)` while the refusal did not exist.
# 1a.4 added it — `extract_schema_from_csv` now guards the returned schema's
# `properties` past both read arms and raises — so both tests XPASSed, which
# under `strict=True` is a loud failure and was the intended prompt to remove the
# markers. Removed, tests kept unchanged; they are now ordinary passing
# assertions of R1.3/R1.4 rather than a record of a gap.
#
# What 1a.4 also did, per that reason string's own note: amended
# `test_streaming_schema.py`'s `test_csv_empty_payload` in place
# (`LGR-60c4985-6` / `LGR-9e76249-10`) rather than deleting it. It is now
# `test_csv_empty_payload_is_refused` and carries the reversal's reason at the
# assertion.


def test_csv_branch_refuses_a_headers_only_source():
    """**Validates: Requirements R1.3, R1.4**

    R1.4 requires the refusal to name the object and to say the file carried a
    header but no data rows, distinguishably from R1.1's "the file was empty" and
    from "the format was not recognised" — so the message is asserted, not only
    the raise.

    R1.4's distinguishability is reachable here **because 1a.3 lands first**: a
    zero-byte object is refused at step 1, so by elimination anything arriving
    at this function with zero records has a header and no data rows. Without
    1a.3 the two cases are indistinguishable from the read's output alone,
    which makes 1a.3 a prerequisite for this message being honest rather than
    merely worded.

    AMENDED when the S3 Select arm was removed: this **replaces a pair** of
    tests, ``…_on_the_s3_select_arm`` and ``…_on_the_range_get_arm``. There were
    two because there were two read paths and the refusal had to hold on both —
    "fixing one alone leaves the defect reachable", as the extractor's own
    docstring put it. With one read path there is one test, and it keeps the
    **richer** of the two assertion sets: the s3-select variant asserted the full
    R1.4 message contract (object named, "header", "row") where the range-get
    variant asserted only the object, so nothing is weakened by the merge. What
    it drives is the surviving path, where ``csv.DictReader`` genuinely reads the
    chunk.
    """
    _range_get_yields_headers_only(HEADERS_ONLY_COLUMNS)

    with pytest.raises(ValueError) as excinfo:
        extract_schema_from_csv(BUCKET, KEY)

    message = str(excinfo.value)
    assert BUCKET in message and KEY in message, "R1.4: the refusal must name the object"
    lowered = message.lower()
    assert "header" in lowered, "R1.4: the refusal must say the file carried a header"
    assert "row" in lowered, "R1.4: the refusal must say there were no data rows"


def test_the_refusal_stays_distinguishable_from_the_other_two():
    """**Validates: Requirements R1.4**

    ADDED by p64 task 1a.4, beyond the contract 1a.2 planted. 1a.2's two tests
    above assert this message's *positive* half — it names the object and says
    header and rows — and task 1a.3 asserts the negative half of **its own**
    message at `tests/unit/lambdas/file_validation/test_error_paths.py:219-226`
    (`"header" not in lowered`, `"unsupported" not in lowered`). Neither pins the
    negative direction on this side, so R1.4's three-way separation was
    checkable from one end only: a later rewording of this message into "is
    empty (0 bytes)" would collapse two refusals into one indistinguishable form
    and every existing test would still pass.

    The three messages, transcribed from their raises rather than imported, so a
    reworded producer fails this test instead of silently re-deriving agreement:

      * step 1, zero bytes — ``File {uri} is empty (0 bytes) — there are no rows
        to ingest`` (`file_validation/lambda_function.py:775-779`).
      * step 1, unsupported extension — ``Unsupported source format '{ext}' for
        '{key}' — recognised extensions are …`` (`:702-705`).
      * step 2, this one — ``carried a header but no data rows``.

    Note "rows" appears in the zero-byte message too, so it is NOT the
    discriminator; "header" and the ``empty (0 bytes)`` / ``header but no data
    rows`` phrasings are. That is why the assertions below are on the phrases
    rather than on the bare word.
    """
    _range_get_yields_headers_only(HEADERS_ONLY_COLUMNS)

    with pytest.raises(ValueError) as excinfo:
        extract_schema_from_csv(BUCKET, KEY)

    message = str(excinfo.value)
    lowered = message.lower()

    assert "0 bytes" not in lowered, (
        f"R1.4: this refusal must not claim the file was zero-byte — a headers-only file is not, "
        f"and step 1 already refuses the zero-byte case in those words: {message!r}"
    )
    assert "unsupported" not in lowered and "recognised" not in lowered, (
        f"R1.4: this refusal must stay distinguishable from the unsupported-format one, whose "
        f"wording is 'Unsupported source format … recognised extensions are': {message!r}"
    )
    assert "header but no data rows" in lowered, (
        f"R1.4: the phrase is the discriminator, not the bare word 'row' — the zero-byte refusal "
        f"also says 'rows': {message!r}"
    )


def test_the_refusal_comes_from_a_real_read_and_not_from_a_stubbed_shortcut():
    """Guards the refusal tests above against passing for the wrong reason.

    A `pytest.raises(ValueError)` says nothing about *where* the raise came from,
    so this asserts the harness genuinely drives the reader: the bounded Range GET
    is issued, and the removed API is not.

    AMENDED — and narrowed — when the S3 Select arm was removed. This was
    `test_both_csv_arms_are_reached_by_the_two_harness_helpers`, and it existed to
    prove each of two helpers reached the arm it claimed. There is one arm and one
    helper now, so the two-way claim has no subject. What survives is the half
    that still means something: the raise follows an actual read.

    The earlier note is kept because it records why the test exists at all. It was
    itself amended by p64 task 1a.4 (per `LGR-60c4985-6` / `LGR-9e76249-10`)
    rather than deleted: both halves had asserted
    `extract_schema_from_csv(...) == EMPTY_SCHEMA`, which **pinned the defect** —
    it was 1a.2's evidence that the harness reached the code under test, and the
    evidence it used was the very return R1.3 forbids.

    `assert not select_object_content.called` is kept, inverted from its original
    sense. It used to prove the S3 Select arm did not fall through to a Range GET;
    it now proves the removed call has not been reintroduced. Cheap, and it fails
    loudly if someone restores the arm as an optimisation — which the extractor's
    docstring asks them not to do.

    The guard-placement property the old test also carried — the refusal sits
    *past* the try/except, so a zero-field schema is not caught by this function's
    own `except` and re-reported as a read failure — is asserted from the other
    direction by `test_streaming_schema.py`'s
    `test_a_read_failure_is_logged_and_re_raised_not_converted`.
    """
    _range_get_yields_headers_only(HEADERS_ONLY_COLUMNS)

    with pytest.raises(ValueError):
        extract_schema_from_csv(BUCKET, KEY)

    assert _S3.get_object.called, "the refusal must follow an actual bounded read"
    assert not _S3.select_object_content.called, (
        "S3 Select has been closed to new customers since 2024-07-25 and this call "
        "never once succeeded in either dev account — it must not be reintroduced"
    )


def test_the_json_branch_has_the_same_defect_and_r1_3_does_not_name_it():
    """**Validates: Requirements R1.3** — recorded as a finding, left passing.

    `extract_schema_from_json:330-332` short-circuits with a `logger.warning`
    and returns the empty schema **without calling** `infer_schema_from_records`
    at all, so it is a *second, independent* zero-field success path with its own
    literal. R1.3 scopes the refusal to the CSV branch, so this is out of scope
    and stays passing — but 1a.4 will be standing next to it, and a reader who
    assumes the guard reached "the extractors" would be wrong.

    Pinned rather than described so the claim is checkable, and so a later spec
    closing it has a test to amend.
    """
    body = MagicMock()
    body.read.return_value = b"not json at all"
    _S3.reset_mock(side_effect=True)
    _S3.get_object.side_effect = None
    _S3.get_object.return_value = {"Body": body}

    assert _MOD.extract_schema_from_json(BUCKET, "pipeline-p64/uploads/empty.json") == EMPTY_SCHEMA

    # And the same object read as a JSON array of empty records reaches the
    # empty schema through the helper instead — route (c) above, end to end.
    body.read.return_value = json.dumps([{}, {}]).encode("utf-8")
    assert _MOD.extract_schema_from_json(BUCKET, "pipeline-p64/uploads/blank-records.json") == EMPTY_SCHEMA
