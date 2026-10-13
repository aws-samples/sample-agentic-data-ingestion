# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
# Feature: p36-schema-naming, Property 10: `_submitted_display_label`'s five-way partition

For any JSON upload body, `_submitted_display_label` (`handlers/schemas.py:1499-1520`)
partitions the input five ways, and the partition is the same on **both** spellings the
handler accepts (`_DISPLAY_LABEL_KEYS`, `schemas.py:1286`):

| input                        | outcome                    |
| ---------------------------- | -------------------------- |
| the key is **absent**        | `""`                       |
| the value is `None`          | `""`                       |
| the value is `""`            | `""`                       |
| the value is a **string**    | that string, **verbatim**  |
| the value is a **non-string**| raises `ValueError`        |

WHY THIS TEST EXISTS, GIVEN THE FUNCTION ALREADY BEHAVES THIS WAY
-----------------------------------------------------------------
CodeGraph flags `_submitted_display_label` as having no covering tests, and a search
confirmed the flag: it is reached only indirectly, through `upload_schema`, at
`test_schema_upload_name_and_label.py:136`, `:339-342` and `:381`. Those exercise a
label arriving and a label being absent through the whole handler — none of them
separates **absent** from `null` from `""`, and none of them names the non-string
refusal at all. So the distinction the function makes has no direct assertion today.

**This test is therefore expected GREEN on the first run.** It is a characterization
test in the strict sense: it pins behaviour that already exists so that Requirements
2.2, 2.3 and 2.4 stop resting on an untested function. A red result here is a real
finding about the function, not one of this spec's two expected red windows
(tasks 1.3–1.6 and 5.5/5.7).

THE EXPECTATIONS COME FROM REQUIREMENT 2, NOT FROM THE CODE
-----------------------------------------------------------
- R2.3: "WHERE the user supplied no Display_Label, THE Wizard_Inline_Upload SHALL send
  no Display_Label, and the created schema SHALL be usable with no Display_Label at
  all." Absent, `null` and `""` are all that state, and the function collapses them to
  one spelling — `""` — so no caller downstream has to know which of the three arrived.
  `LGR-44d601e-1` (**template / ratified**) is why the empty label is the *absence* of
  one rather than a stored `""`.
- R2.4: "THE Wizard_Inline_Upload SHALL send a supplied Display_Label verbatim." Read
  from the storage side, that is: this function returns what it was given, untouched.

TWO DIFFERENT RULES, DELIBERATELY NOT CONFLATED
-----------------------------------------------
A padded or whitespace-only string comes back **verbatim** from here. This function
does not trim and does not treat whitespace as absence. R2.4's other half — "SHALL
treat a value consisting only of whitespace as no Display_Label" — is the **gate**,
and the gate lives at the caller (`upload_schema:1623`, and `appendDisplayLabel` at
`SchemaManagementPage.tsx:300-302` on the frontend). The gate trims; the value does
not. Asserting a trim here would assert the caller's rule at the wrong layer and would
turn a correct function red.

WHY A NON-STRING RAISES RATHER THAN COERCING
--------------------------------------------
The docstring records the reason: `str(42)` "would store a label the caller never
wrote". A coerced label is worse than a refused one, because a `400` is visible to the
caller while a silently invented name is not. `bool` is generated alongside `int`
precisely because `isinstance(True, int)` is true and a coercion written as
`str(value)` would happily store `"True"`.

BOTH SPELLINGS ARE WALKED IN ORDER, SO BOTH-PRESENT IS ITS OWN CASE
-------------------------------------------------------------------
The loop returns on the **first** key present, so `displayLabel` wins over
`display_label` and a `display_label` beside it is never even validated. That is
observed and asserted below rather than assumed; see
`test_the_first_spelling_present_wins_and_the_second_is_not_validated`.

FIXTURES, WORLD AND REGION
--------------------------
`_submitted_display_label` is a **pure function over a `Mapping`**. It performs no AWS
call, reads no environment variable and touches no bucket, so this file needs no S3
world: task 1.2's `label_world_env` / `seed_label_world` machinery is deliberately
**not** imported. Pulling it in would create moto backends for a function that cannot
observe them and would make a pure-logic property depend on fixture setup.

The only import-time requirement is the nine table names `handlers/common.py:29-37`
`require_env`s, set below with `setdefault` — the same block
`test_schema_upload_single_suffix_prop.py` uses. No `moto.mock_aws()` and no boto3
client mock anywhere (`tests/unit/test_no_duplicate_mocking_prop.py`), and
`AWS_REGION` is **never set** (`LGR-3e45e65-1`, **template / ratified**).

**Validates: Requirements 2.2, 2.3, 2.4, 4.5**
"""

from __future__ import annotations

import math
import os
import sys
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.schemas
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Table names only — AWS_REGION and AWS_DEFAULT_REGION are deliberately absent.
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers.schemas import _DISPLAY_LABEL_KEYS, _submitted_display_label

# ---------------------------------------------------------------------------
# The wire contract, spelled here and then pinned against its one definition
# ---------------------------------------------------------------------------
#: The two spellings a JSON upload body may carry a Display_Label under.
#:
#: Written out rather than only iterated from `_DISPLAY_LABEL_KEYS` so that this
#: file states the contract it is testing: `displayLabel` is the camelCase key
#: `PUT /api/schemas/{id}/display-label` already accepts, and `display_label` is
#: accepted beside it for a hand-written `multipart/form-data` field. The
#: agreement between this tuple and the handler's constant is asserted in
#: `test_the_accepted_spellings_are_the_two_this_file_covers` — so widening or
#: renaming the constant fails loudly here instead of silently leaving a spelling
#: uncovered.
ACCEPTED_KEYS = ("displayLabel", "display_label")

#: A key no upload body carries a label under. Used to prove the function reads
#: **only** the two accepted spellings: a body full of near-misses is still a body
#: with no label.
DECOY_KEYS = ("displaylabel", "DisplayLabel", "display-label", "label", "name", "displayName")

#: Whitespace that is not a space, including the two Python's `str.strip()` treats
#: as whitespace while a hand-rolled `!= " "` check does not.
_WHITESPACE = " \t\n\r\u00a0\u2007\u202f\u3000"

_UNICODE_LABELS = st.sampled_from(
    [
        "顧客マスタ",
        "Ünïcødé Ñame",
        "Клиенты",
        "Customer 📊 Master",
        "café-données",
    ]
)

#: Every **string** a caller may submit, `""` excluded so the empty case stays a
#: partition of its own. Padded and whitespace-only values are in here on purpose:
#: the verbatim rule covers them, and the whitespace *gate* is the caller's.
STRING_LABELS = st.one_of(
    _UNICODE_LABELS,
    st.text(min_size=1, max_size=40),
    st.text(alphabet=_WHITESPACE, min_size=1, max_size=5),
    st.tuples(
        st.text(alphabet=_WHITESPACE, min_size=1, max_size=3),
        st.one_of(_UNICODE_LABELS, st.text(min_size=1, max_size=20)),
        st.text(alphabet=_WHITESPACE, min_size=0, max_size=3),
    ).map("".join),
)

#: Everything that is not a `str`, across the five JSON-reachable types the task
#: names. `st.booleans()` is listed separately from `st.integers()` because
#: `isinstance(True, int)` is true and `str(True) == "True"` — a coercion would
#: store a label spelled by Python rather than by the caller.
NON_STRING_LABELS = st.one_of(
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.lists(st.one_of(st.integers(), st.text(max_size=5)), max_size=3),
    st.dictionaries(st.text(min_size=1, max_size=5), st.integers(), max_size=3),
)

#: Body content that has nothing to do with the label — `upload_schema`'s real
#: neighbours plus arbitrary noise, so no property passes only on a one-key dict.
OTHER_BODY_ENTRIES = st.dictionaries(
    keys=st.one_of(
        st.sampled_from(["name", "content", "target_schema", "sourceType"]),
        st.text(min_size=1, max_size=8),
    ).filter(lambda key: key not in ACCEPTED_KEYS),
    values=st.one_of(st.text(max_size=10), st.integers(), st.none(), st.booleans()),
    max_size=4,
)


def _body(other: dict[str, Any], **label_entries: Any) -> Any:
    """A submitted body: arbitrary unrelated entries plus the label entries given.

    Returned as a `MappingProxyType` — the signature says `Mapping[str, Any]`, and a
    read-only mapping is also the cheapest possible proof that the function does not
    mutate what it is handed.
    """
    return MappingProxyType({**other, **label_entries})


# ---------------------------------------------------------------------------
# The contract this file covers is the handler's own
# ---------------------------------------------------------------------------
def test_the_accepted_spellings_are_the_two_this_file_covers():
    """`_DISPLAY_LABEL_KEYS` is exactly `ACCEPTED_KEYS`, in order.

    Order is asserted, not just membership: the loop returns on the first key
    present, so which spelling comes first is behaviour
    (`test_the_first_spelling_present_wins_and_the_second_is_not_validated`).

    **Validates: Requirement 4.5**
    """
    assert _DISPLAY_LABEL_KEYS == ACCEPTED_KEYS, (
        f"the accepted Display_Label spellings changed to {_DISPLAY_LABEL_KEYS!r} — this file covers "
        f"{ACCEPTED_KEYS!r}, so a new or renamed spelling is currently untested"
    )


# ---------------------------------------------------------------------------
# Partition 1 — the key is absent
# ---------------------------------------------------------------------------
@given(other=OTHER_BODY_ENTRIES, decoys=st.lists(st.sampled_from(DECOY_KEYS), max_size=4))
@settings(max_examples=200, deadline=None)
def test_an_absent_key_is_no_label(other, decoys):
    """No accepted spelling present → `""`, whatever else the body carries.

    The decoy keys are the point of the `decoys` argument: `displaylabel` and
    `display-label` are near-misses a hand-written form might send, and a body
    carrying only those is still a body with **no** Display_Label. R2.3 makes "no
    label at all" a first-class state.

    **Validates: Requirements 2.3, 4.5**
    """
    body = _body({**other, **{decoy: "a name the caller did not send under an accepted key" for decoy in decoys}})
    for key in ACCEPTED_KEYS:
        assert key not in body

    assert _submitted_display_label(body) == ""


# ---------------------------------------------------------------------------
# Partition 2 — the value is `None`
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@given(other=OTHER_BODY_ENTRIES)
@settings(max_examples=100, deadline=None)
def test_a_null_label_is_no_label(key, other):
    """`{key: None}` → `""`, on either spelling.

    A JSON `null` is what a frontend sends when it holds "no label" as `null`
    rather than by omitting the field. R2.3 makes it the same state as absence, and
    collapsing it here is what stops every downstream caller having to know which
    of the two arrived.

    **Validates: Requirements 2.3, 4.5**
    """
    assert _submitted_display_label(_body(other, **{key: None})) == ""


# ---------------------------------------------------------------------------
# Partition 3 — the value is `""`
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@given(other=OTHER_BODY_ENTRIES)
@settings(max_examples=100, deadline=None)
def test_an_empty_label_is_no_label(key, other):
    """`{key: ""}` → `""`, on either spelling.

    Indistinguishable from absence *by design*: `LGR-44d601e-1` (**template /
    ratified**) chose that an empty label is the absence of one rather than a stored
    `""`, which is what keeps a legacy `""` off both the listing and the detail
    (Requirement 1.3's gate).

    **Validates: Requirements 2.3, 4.5**
    """
    assert _submitted_display_label(_body(other, **{key: ""})) == ""


# ---------------------------------------------------------------------------
# Partition 4 — the value is a string
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@given(label=STRING_LABELS, other=OTHER_BODY_ENTRIES)
@settings(max_examples=200, deadline=None)
def test_a_string_label_is_returned_verbatim(key, label, other):
    """`{key: label}` → `label`, byte-identical, on either spelling.

    Verbatim means verbatim: padding survives, interior whitespace survives, a
    whitespace-only value survives. The whitespace-as-absence rule is the caller's
    gate (`upload_schema:1623`), not this function's, so a trim asserted here would
    be asserting the wrong layer's rule.

    **Validates: Requirements 2.2, 2.4, 4.5**
    """
    returned = _submitted_display_label(_body(other, **{key: label}))

    assert returned == label
    assert returned is label, (
        "R2.4: a submitted Display_Label is returned verbatim — the value that came out is not the object "
        "that went in, so something transformed it"
    )


@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@pytest.mark.parametrize(
    "label",
    [
        "  Customer Master  ",
        "\tCustomer Master\n",
        "   ",
        "\u00a0\u3000",
        "顧客マスタ",
        "a" * 200,
    ],
    ids=["padded", "tab-and-newline", "spaces-only", "unicode-spaces-only", "unicode", "long"],
)
def test_named_string_labels_survive_untouched(key, label):
    """The verbatim rule on the values a reviewer will want to see spelled out.

    Examples beside the property, not instead of it: these are the exact shapes
    Requirement 2.4's two rules are most easily conflated on.

    **Validates: Requirements 2.2, 2.4**
    """
    assert _submitted_display_label({key: label}) == label


# ---------------------------------------------------------------------------
# Partition 5 — the value is not a string
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@given(label=NON_STRING_LABELS, other=OTHER_BODY_ENTRIES)
@settings(max_examples=200, deadline=None)
def test_a_non_string_label_raises_rather_than_coercing(key, label, other):
    """`{key: <non-str>}` raises `ValueError`, on either spelling.

    Refusal rather than coercion is the recorded intent: `str(42)` "would store a
    label the caller never wrote" (`_submitted_display_label`'s docstring), and an
    invented name is invisible to the caller in a way a `400` is not.

    The message is asserted to name the key and the offending type, because that is
    what makes the resulting `400` actionable — the caller has to learn *which*
    field it sent wrong.

    **Validates: Requirements 2.2, 2.4, 4.5**
    """
    with pytest.raises(ValueError) as raised:
        _submitted_display_label(_body(other, **{key: label}))

    message = str(raised.value)
    assert key in message, f"the refusal must name the offending key — got {message!r}"
    assert type(label).__name__ in message, f"the refusal must name the offending type — got {message!r}"


@pytest.mark.parametrize("key", ACCEPTED_KEYS)
@pytest.mark.parametrize(
    "label",
    [42, 0, 3.5, math.nan, True, False, [], ["a"], {}, {"value": "Customer Master"}],
    ids=["int", "zero", "float", "nan", "true", "false", "empty-list", "list", "empty-dict", "dict"],
)
def test_named_non_strings_are_refused(key, label):
    """The five non-string types, including the falsy ones and both booleans.

    `0`, `False`, `[]` and `{}` are here because a `not value` guard would let every
    one of them through as "no label", and `True` is here because
    `isinstance(True, int)` — a `str(value)` coercion would store `"True"`.

    **Validates: Requirements 2.2, 2.4**
    """
    with pytest.raises(ValueError):
        _submitted_display_label({key: label})


# ---------------------------------------------------------------------------
# Both spellings at once — the loop's order, observed rather than assumed
# ---------------------------------------------------------------------------
@given(first=STRING_LABELS, second=st.one_of(STRING_LABELS, st.none(), st.just(""), NON_STRING_LABELS))
@settings(max_examples=200, deadline=None)
def test_the_first_spelling_present_wins_and_the_second_is_not_validated(first, second):
    """With both spellings present, `displayLabel` decides and `display_label` is ignored.

    Read off the loop (`schemas.py:1511-1520`): it walks `_DISPLAY_LABEL_KEYS` in
    order and **returns** on the first key that is present, so the second spelling is
    never looked at — not even to reject it. `second` therefore ranges over `None`,
    `""`, a string and a non-string, and the outcome is the same for all of them.

    That the second value is not validated is the behaviour worth pinning: a future
    reader who changed the loop to collect both keys and validate each would break
    this, and the break would be the interesting kind — a body that succeeds today
    would start returning `400`.

    **Validates: Requirement 4.5**
    """
    body = {_DISPLAY_LABEL_KEYS[0]: first, _DISPLAY_LABEL_KEYS[1]: second}

    assert _submitted_display_label(body) == first


@given(second=st.one_of(STRING_LABELS, NON_STRING_LABELS))
@settings(max_examples=100, deadline=None)
def test_a_null_first_spelling_still_shadows_the_second(second):
    """`{displayLabel: None, display_label: <anything>}` → `""`.

    The `None` branch returns too, so a `null` under the first spelling is not "no
    value here, try the next key" — it is a decided answer. Asserted because "fall
    through to the other spelling" is the plausible alternative reading of the loop,
    and it is not what the code does.

    **Validates: Requirements 2.3, 4.5**
    """
    assert _submitted_display_label({_DISPLAY_LABEL_KEYS[0]: None, _DISPLAY_LABEL_KEYS[1]: second}) == ""


@given(second=st.one_of(STRING_LABELS, st.none(), st.just("")))
@settings(max_examples=100, deadline=None)
def test_a_non_string_first_spelling_raises_whatever_the_second_holds(second):
    """A bad `displayLabel` is refused even when `display_label` is fine.

    The refusal is not recoverable by sending a valid value under the other
    spelling — the first present key decides, including when what it decides is a
    `ValueError`.

    **Validates: Requirements 2.2, 4.5**
    """
    with pytest.raises(ValueError):
        _submitted_display_label({_DISPLAY_LABEL_KEYS[0]: 42, _DISPLAY_LABEL_KEYS[1]: second})


# ---------------------------------------------------------------------------
# The partition is total — every input lands in exactly one of the five
# ---------------------------------------------------------------------------
@given(
    other=OTHER_BODY_ENTRIES,
    label_entries=st.lists(
        st.tuples(
            st.sampled_from(ACCEPTED_KEYS),
            st.one_of(st.none(), st.just(""), STRING_LABELS, NON_STRING_LABELS),
        ),
        max_size=2,
        unique_by=lambda entry: entry[0],
    ),
)
@settings(max_examples=300, deadline=None)
def test_every_body_lands_in_exactly_one_partition(other, label_entries):
    """One property over the whole input space: the five outcomes are exhaustive.

    For any body, the function either returns a `str` or raises `ValueError` — never
    anything else, and never `None`. Which of the two, and which string, is decided
    by the **first** accepted spelling present, per the four rules above.

    This is the partition stated as a single oracle rather than five, so an input
    class the five tests above happen not to reach still has an expected answer.

    **Validates: Requirements 2.2, 2.3, 2.4, 4.5**
    """
    body = _body(other, **dict(label_entries))

    deciding = next((key for key in ACCEPTED_KEYS if key in body), None)

    if deciding is None:
        assert _submitted_display_label(body) == ""
        return

    value = body[deciding]
    if value is None:
        assert _submitted_display_label(body) == ""
    elif isinstance(value, str):
        assert _submitted_display_label(body) == value
    else:
        with pytest.raises(ValueError):
            _submitted_display_label(body)
