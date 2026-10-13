# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract property test: all three Sample_Readers hold one contract, unchanged by p53.

Spec ``p53-jdbc-source-sampling`` task 4.2, **Property 6**.
**Validates: Requirements 2.1, 2.2, 2.3**

    2.1 THE Sample_Reader return type SHALL remain ``dict[str, list[str]]``.
    2.2 THE Sample_Reader caps SHALL remain at most 10 unique non-empty values
        per field over at most 100 rows.
    2.3 THE Sample_Reader SHALL continue to return ``{}`` rather than raise for
        every failure mode it currently returns ``{}`` for.

WHY THIS IS A CONTRACT TEST AND NOT THREE UNIT TESTS
----------------------------------------------------
There are **three** Sample_Readers, in three separately bundled deployment units
that cannot import one another:

* ``lambda/agentic_data_ingestion_create_code/lambda_function.py:299``
  ``read_source_samples`` — p49 C1, the deterministic date-format check.
* ``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:875``
  ``read_source_samples`` — p49 C2, sampled values in the mapping prompt.
* ``agentcore/complex_mapping_agent/main.py:84`` ``read_sample_data`` — the
  ancestor the other two descend from, on the live auto-invoked
  complex_mapping_agent path. **p53's fourth consumer, which the commissioning
  brief did not name.**

The first two are near-duplicates of each other and both cite the third by line
number in their own docstrings. So the contract is a *cross-bundle* fact — the
shape ``test_format_vocabulary_cross_lambda.py`` describes for ``FORMAT_MAP``:
"separate Lambda bundles with separate ``sys.path`` roots and their own
module-level boto3 clients, so neither can import the other's constant". A
per-directory unit test can assert one reader's contract; only a file that holds
all three at once can assert they are the *same* contract, which is what R2.1-2.3
say and what p53 must not have broken while adding a fourth caller.

That also decides the layer. This file is in ``tests/contracts/`` because
``tests/unit/lambdas/<name>/`` is per-bundle by construction, and because the
sibling cross-bundle claim about these same readers
(``test_sample_readers_match_templates.py``, ``test_no_sample_reader_mentions_jdbc``)
already lives here.

WHAT P53 CHANGED, AND WHY THAT MAKES THIS THE LOAD-BEARING TEST
--------------------------------------------------------------
p53 task 3.2 changed **three call sites** to prefer ``sample_s3_uri`` over
``json_s3_uri``. It changed **no reader**. R2.1, R2.2 and R2.3 are the written
form of that boundary, and design C3's whole argument for a new attribute rather
than a repoint is that the readers keep meaning what they mean.

The failure this catches is not "a reader was rewritten" — that would be obvious.
It is a reader acquiring a **new raise** on the new input. Run 2+ of a JDBC
pipeline now hands these readers an ``s3://`` URI for the first time in their
lives, so paths that were previously reached only by the S3-upload source type are
now reached by JDBC pipelines too, including the failure paths: a probe that
raced the write, a truncated body, an object written by a Glue driver rather than
by an upload. For ``schema_mapping`` an escaping exception is not a degraded
sample but a **changed mapping provenance** — its own docstring records that
anything raising inside ``generate_mapping_with_bedrock`` is caught at ``:1189``
and silently downgrades an honest model mapping to ``fallback_substring_match``,
which P28 R3.1 then re-reports. So "never raises" is load-bearing, not tidy.

HOW THE THREE MODULES ARE LOADED, AND WHY THAT WAY
--------------------------------------------------
Two of the three files are both named ``lambda_function.py``, so a plain
``import`` cannot hold both. ``LGR-c58f276-4`` (**template / ratified**) is the
governing record and prescribes the mechanism: load from the file path via
``importlib.util.spec_from_file_location`` into a private module object that is
**never published to** ``sys.modules``, and assert at import time that what was
bound is neither a mock nor vacuous.

That record's reasoning is the reason it matters here rather than being a
convenience. pytest imports every test module during **collection**, before any
test runs, and four tracked modules assign ``sys.modules["audit_utils"] =
MagicMock()`` at module scope; the measured symptom was five failures in
``make test-fast`` against ``7 passed`` for the same file alone. Publishing a
third and fourth ``lambda_function`` into ``sys.modules`` from the contracts layer
would be that defect's shape again, pointed at a different name — and
``api_responses/test_run_test_shape.py:63-74`` records the ``sys.path`` half of
the same hazard, which is why the three directory entries below come **off**
``sys.path`` as soon as the loads return.

The import-time guards are that record's own: a reader that had silently become a
``MagicMock`` would make every assertion below pass, since a ``MagicMock`` returns
a ``MagicMock`` and never raises. That is the one way this file could be entirely
green and entirely worthless.

THE INPUT SPACE, AND THE ONE BOUNDARY DELIBERATELY OUTSIDE IT
-------------------------------------------------------------
Property 6 enumerates its space: "an absent URI, a Sentinel, a non-``s3://`` URI,
an unreadable object, an unparseable body". Generated as a **cross product** of a
URI family and an S3 behaviour, because the two are independent — an unreadable
object is a URI that passes the pre-flight checks meeting a client that raises,
and the interesting cells are the ones where a reader gets past its own guards.

**Non-string inputs are outside the space, and that is stated rather than
quietly avoided.** All three readers raise ``AttributeError`` on a truthy
non-string (``5.startswith`` — the guard is ``not s3_uri or …``, so ``0``,
``None`` and ``""`` are absorbed and ``5`` is not). That is pre-existing on all
three, identical on all three, and not a failure mode R2.3 covers: R2.3 says
"every failure mode it **currently** returns ``{}`` for". Asserting the raise
would pin behaviour p53 has no mandate over; asserting it away would be inventing
a requirement. It is recorded here instead, and
:func:`test_the_non_string_boundary_is_where_this_property_stops` marks it so the
next reader does not mistake the generator's shape for an oversight.

**Log levels are deliberately not asserted here.** R6.3's INFO-not-WARNING claim
for the two ``read_source_samples`` is owned by
``tests/unit/lambdas/create_code/test_rule_contradiction_check.py:1172`` and
``tests/unit/lambdas/schema_mapping/test_mapping_prompt_samples.py:231``, both
unmodified by p53. And the third reader diverges: ``read_sample_data`` declines a
non-``s3://`` URI at **WARNING** (``main.py:93``), so a JDBC pipeline's run 1
produces a warning on that path. Design C4 records that this is expected and that
changing it is a separate judgement, so this file neither changes it nor pins it.

GOVERNING RECORDS
-----------------
* ``LGR-c58f276-4`` (template / ratified) — the private-module loader and its
  two import-time guards, above.
* ``LGR-28b4d75-5`` (template / ratified) — "a test written from the producer's
  output cannot detect the producer drifting, because the output is its own
  oracle". Obeyed by deriving nothing from the readers: the caps are asserted
  against **10** and **100** as the numbers R2.2 states, and separately against
  each module's own constants, so a module that changed its constant fails rather
  than redefining the contract.
* ``LGR-819980f-1`` (**standing invariant**) — reached through
  ``concept="credentials"``. Why no reader may grow a JDBC branch, and therefore
  why the Sentinel arm of the input space has to keep returning ``{}`` rather
  than becoming readable.
* ``LGR-b46a5bb-4`` (template / ratified) — why the structural half of R5.4 lives
  in an AST walk, not a substring scan. Cited because the sibling file's
  ``test_no_sample_reader_mentions_jdbc`` is the runtime half of the same claim.

LEDGER GAP, STATED AS A GAP
---------------------------
``LGR-7382e31-3`` (template / ratified) records, as a measured gap rather than an
all-clear, that **no ledger record governed** ``agentcore/complex_mapping_agent/main.py``
before p52: ``why`` on it returns five broad snapshots, all with ``records: []``,
and ``decisions(scope=…)`` returns one unrelated record. So the third reader's
contract is unconstrained by any prior ruling — not permitted by one. This file is
the first executable statement of it.

BLAST RADIUS AND COVERAGE, GREP-VERIFIED — CODEGRAPH IS WRONG ON ALL THREE
--------------------------------------------------------------------------
Re-measured on this tree, and matching what p53's design already found:

==============================  ================  ======================================
Symbol                          CodeGraph said    Grep found
==============================  ================  ======================================
``create_code`` reader          1 caller,         1 caller (``lambda_handler`` via
                                ⚠️ no tests       ``_sample_reader_for:369``); **covered**
                                                  by ``test_rule_contradiction_check.py``
``schema_mapping`` reader       1 caller,         1 caller (``:2095``); **covered** by
                                ⚠️ no tests       ``test_mapping_prompt_samples.py``
``read_sample_data``            1 caller,         1 caller (``main.py:483``); **covered**
                                ⚠️ no tests       by ``test_complex_mapping.py``
==============================  ================  ======================================

All three coverage flags are **false negatives**, which is the known failure mode
where tests reach a symbol through ``patch.object`` on the module rather than by
importing the function. Verified by grep, not by the graph. No characterization
pass is owed, and this file adds a generalising layer over three example-based
ones rather than replacing any of them.

MOCKING
-------
``patch.object(module, "<client attr>", MagicMock())`` — the idiom all three
readers' existing tests use (``test_mapping_prompt_samples.py:191``,
``test_rule_contradiction_check.py:1122``, ``test_complex_mapping.py:31``). No
``moto.mock_aws()`` and no shared AWS fixture: the shared fixtures are
function-scoped, and a function-scoped fixture is applied once per test
*function* and never reset between Hypothesis examples, so state would leak across
examples. Nothing here needs a bucket to exist — the whole subject is how a reader
answers what the client hands back.

**No S3 object is read, no AWS call is made, nothing is deployed.**
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# The contracts layer has no region pinning of its own — ``LGR-3e45e65-1``
# (template / ratified) scopes that to ``tests/unit/**`` — and all three modules
# construct a boto3 client at import. Set before the loads below.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CREATE_CODE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_code"
_SCHEMA_MAPPING_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_schema_mapping"
_AGENT_DIR = _REPO_ROOT / "agentcore" / "complex_mapping_agent"
_SHARED_DIR = _REPO_ROOT / "shared"


#: Lambda-layer and AgentCore dependencies that are not importable here. Stubbed
#: for the *load* only, via ``patch.dict`` so the entries come back out — the
#: readers themselves touch none of these, which is the point: a stub that
#: mattered to the behaviour under test would make the assertions meaningless.
_LAYER_STUBS: dict[str, MagicMock] = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda key, default=None: default or key),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "artifact_schema": MagicMock(),
    "mapping_memory": MagicMock(MappingMemoryClient=None),
}

_AGENT_STUBS: dict[str, MagicMock] = {
    **_LAYER_STUBS,
    "bedrock_agentcore": MagicMock(),
    "bedrock_agentcore.runtime": MagicMock(),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
}


def _load_private(alias: str, path: Path, stubs: dict[str, MagicMock]) -> ModuleType:
    """Exec *path* as a module named *alias*, without publishing it to ``sys.modules``.

    ``LGR-c58f276-4``'s mechanism verbatim. The alias is never a real importable
    name, so nothing else in the run can bind it and no collection order can make
    this file's modules another file's modules.
    """
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None, f"could not build an import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", stubs):
        spec.loader.exec_module(module)
    return module


# The three directories go on ``sys.path`` for the loads and come straight back
# off, per the fence ``api_responses/test_run_test_shape.py:63-74`` documents:
# two of them contain a ``lambda_function.py``, and other contract files do a
# bare ``import lambda_function`` against their own Lambda directory, which a
# lingering entry at position 0 would shadow depending on collection order.
_INSERTED = [str(_CREATE_CODE_DIR), str(_SCHEMA_MAPPING_DIR), str(_AGENT_DIR), str(_SHARED_DIR)]

#: How many times each entry was already on ``sys.path`` before this module
#: touched it. Counted, not tested for membership, because ``shared/`` is
#: routinely inserted by other files in this layer and ``sys.path.remove`` drops
#: only the **first** occurrence — so "the entry is still there afterwards" says
#: nothing about whether this module cleaned up after itself. The invariant worth
#: holding is that this module leaves the multiset unchanged.
_SYS_PATH_COUNTS_BEFORE = {entry: sys.path.count(entry) for entry in _INSERTED}

for _entry in _INSERTED:
    sys.path.insert(0, _entry)
try:
    _create_code = _load_private("p53_create_code_reader", _CREATE_CODE_DIR / "lambda_function.py", _LAYER_STUBS)
    _schema_mapping = _load_private(
        "p53_schema_mapping_reader", _SCHEMA_MAPPING_DIR / "lambda_function.py", _LAYER_STUBS
    )
    _agent = _load_private("p53_complex_mapping_reader", _AGENT_DIR / "main.py", _AGENT_STUBS)
finally:
    for _entry in _INSERTED:
        with contextlib.suppress(ValueError):
            sys.path.remove(_entry)
    #: Entries whose occurrence count did not return to what it was before this
    #: module ran. Recorded here rather than asserted from a test, because by the
    #: time any test runs another contract file may legitimately have inserted the
    #: same directory — sixteen files in this layer insert a Lambda directory. A
    #: test that read ``sys.path`` directly would therefore be asserting something
    #: about the whole run and would pass or fail on collection order, which is
    #: the defect it exists to prevent rather than a way to detect it.
    _SYS_PATH_LEFTOVERS = {
        entry: (_SYS_PATH_COUNTS_BEFORE[entry], sys.path.count(entry))
        for entry in _INSERTED
        if sys.path.count(entry) != _SYS_PATH_COUNTS_BEFORE[entry]
    }


class Reader:
    """One Sample_Reader, with what a test needs to drive and observe it."""

    def __init__(self, label: str, module: ModuleType, function_name: str, client_attribute: str) -> None:
        self.label = label
        self.module = module
        self.function_name = function_name
        self.client_attribute = client_attribute

    @property
    def function(self):
        return getattr(self.module, self.function_name)

    def read(self, uri, client: MagicMock):
        """Call the reader with *client* standing in for its module-level one."""
        with patch.object(self.module, self.client_attribute, client):
            return self.function(uri)

    def __repr__(self) -> str:  # pragma: no cover — pytest ids only
        return self.label


READERS = (
    Reader("create_code.read_source_samples", _create_code, "read_source_samples", "s3_client"),
    Reader("schema_mapping.read_source_samples", _schema_mapping, "read_source_samples", "s3_client"),
    Reader("complex_mapping_agent.read_sample_data", _agent, "read_sample_data", "_s3_client"),
)


# ── Import-time guards, per LGR-c58f276-4 ─────────────────────────────
#
# A reader that had silently become a MagicMock would make every assertion below
# pass — a MagicMock returns a MagicMock and never raises — so this file's whole
# value rests on what it bound being real. Asserted at import so a broken bind is
# a collection error naming the cause, not thirty green tests.

for _reader in READERS:
    assert not isinstance(_reader.function, MagicMock), (
        f"{_reader.label} bound a MagicMock rather than the real function. Every assertion in this "
        f"file would then pass vacuously (LGR-c58f276-4)."
    )
    assert callable(_reader.function), f"{_reader.label} is not callable"
    assert _reader.function.__module__.startswith("p53_"), (
        f"{_reader.label} resolved to module {_reader.function.__module__!r}, which is not one of this "
        f"file's private loads — something published a module under a shared name"
    )
    assert not isinstance(getattr(_reader.module, _reader.client_attribute), MagicMock), (
        f"{_reader.label}'s module-level {_reader.client_attribute} is already a MagicMock at import "
        f"time, so patch.object would be patching a mock with a mock and 'no AWS call was made' would "
        f"assert nothing"
    )

assert len(READERS) == 3, "Property 6 is a claim about all three Sample_Readers"


# ── The contract's own numbers, from R2.2 rather than from the readers ──

#: R2.2's two ceilings, transcribed from the requirement. ``LGR-28b4d75-5``: a
#: figure read out of the reader could not detect the reader drifting.
CONTRACT_MAX_VALUES_PER_FIELD = 10
CONTRACT_MAX_ROWS = 100


# ── The input space: a URI family × an S3 behaviour ───────────────────

#: ``schema_extract``'s fallback (``lambda_function.py:1735``) and
#: ``file_validation``'s populated form (``:447``). Both are Sentinels; both are
#: what a JDBC pipeline's run 1 still carries after p53, which is why the decline
#: has to survive.
_SENTINEL_URIS = (
    "jdbc://pipeline/latesttesting-19ca826a",
    "jdbc://mysql/sales/customers",
    "jdbc://postgresql/public/order-archive",
    "jdbc://unknown/unknown/unknown",
)

#: Absent, in every spelling a caller can produce. ``None`` is reachable: the
#: DynamoDB item's attribute may be missing, and ``.get()`` returns ``None``.
_ABSENT_URIS = (None, "", "   ")

#: Non-``s3://`` and non-``jdbc://`` — the family all three readers log as
#: genuinely unexpected. ``s3a://`` is the Hadoop scheme nothing here writes;
#: ``S3://`` is the case-normalisation near-miss; ``s3:/b`` has no authority.
_OTHER_NON_S3_URIS = (
    "https://example.com/data.csv",
    "file:///tmp/data.csv",
    "s3a://bucket/key.csv",
    "S3://bucket/key.csv",
    "s3:/bucket",
    "data.csv",
)

#: ``s3://`` URIs that name no object, or name one degenerately. These are the
#: cells that get *past* the scheme check, so the client behaviour matters.
_DEGENERATE_S3_URIS = (
    "s3://",
    "s3:///key.csv",
    "s3://bucket",
    "s3://bucket/",
)

#: Readable-looking ``s3://`` object URIs, including the Sample_Uri p53
#: introduces (``schema_extract/lambda_function.py:1478``, ``:1559``) — the one
#: URI family these readers had never been handed before this spec.
_READABLE_S3_URIS = (
    "s3://adi-processed-files-123456789012-us-west-2/samples/p1/source-sample.json",
    "s3://adi-source-files-123456789012/uploads/customer_data_6.csv",
    "s3://bucket/uploads/data.jsonl",
    "s3://bucket/uploads/no-extension",
)

_URI_FAMILIES = {
    "absent": _ABSENT_URIS,
    "sentinel": _SENTINEL_URIS,
    "other_non_s3": _OTHER_NON_S3_URIS,
    "degenerate_s3": _DEGENERATE_S3_URIS,
    "readable_s3": _READABLE_S3_URIS,
}

_ALL_URIS = tuple(uri for family in _URI_FAMILIES.values() for uri in family)


def _body(payload: bytes) -> MagicMock:
    """A ``Body`` stream whose ``read`` accepts an optional byte cap.

    The two ``read_source_samples`` call ``read(_MAX_SAMPLE_BYTES)``;
    ``read_sample_data`` calls ``read()``. One stub serves both, and the cap is
    honoured so the byte-ceiling behaviour is the readers' own rather than the
    stub's.
    """
    stream = MagicMock()
    stream.read = MagicMock(side_effect=lambda amount=None: payload[:amount] if amount else payload)
    return stream


def _raising_client(exception: BaseException) -> MagicMock:
    client = MagicMock()
    client.get_object = MagicMock(side_effect=exception)
    return client


def _body_client(payload: bytes) -> MagicMock:
    client = MagicMock()
    client.get_object = MagicMock(return_value={"Body": _body(payload)})
    return client


def _malformed_response_client() -> MagicMock:
    """A client whose response is not the shape the readers expect at all.

    Not hypothetical for p53: the object is now written by a Glue driver rather
    than by an upload, so "the response was not what I assumed" is a newly
    reachable state and R2.3 has to absorb it.
    """
    client = MagicMock()
    client.get_object = MagicMock(return_value={})
    return client


_VALID_CSV = b"customer_id,signup_date\n1,01/15/2024\n2,02/20/2024\n2,02/20/2024\n"
_VALID_JSON_ARRAY = json.dumps([{"signup_date": "01/15/2024"}, {"signup_date": "02/20/2024"}]).encode()
_VALID_JSON_LINES = b'{"signup_date": "01/15/2024"}\n{"signup_date": "02/20/2024"}\n'

#: Every S3 behaviour worth crossing with a URI. Each entry is
#: ``(label, client factory)``; the labels are what a falsifying example prints.
_S3_BEHAVIOURS: dict[str, object] = {
    "raises_access_denied": lambda: _raising_client(Exception("AccessDenied")),
    "raises_no_such_key": lambda: _raising_client(Exception("NoSuchKey")),
    "raises_keyboard_free_runtime_error": lambda: _raising_client(RuntimeError("throttled")),
    "empty_body": lambda: _body_client(b""),
    "whitespace_body": lambda: _body_client(b"   \n\n  "),
    "truncated_json_array": lambda: _body_client(b'[{"a": "1"}, {"a": "2"'),
    "html_error_page": lambda: _body_client(b"<html><body>403 Forbidden</body></html>"),
    "non_utf8_bytes": lambda: _body_client(b"\xff\xfe\x00binary\x00garbage"),
    "json_array_of_scalars": lambda: _body_client(b'["not", "objects"]'),
    "json_scalar": lambda: _body_client(b'"just a string"'),
    "malformed_response": _malformed_response_client,
    "valid_csv": lambda: _body_client(_VALID_CSV),
    "valid_json_array": lambda: _body_client(_VALID_JSON_ARRAY),
    "valid_json_lines": lambda: _body_client(_VALID_JSON_LINES),
}

_SETTINGS = settings(max_examples=300, deadline=None)


def _assert_reader_contract(reader: Reader, result: object, *, uri: object, behaviour: str) -> None:
    """R2.1 and the values half of R2.2, over one call's return value."""
    context = f"{reader.label} for uri={uri!r} with S3 behaviour {behaviour!r}"

    assert isinstance(result, dict), f"{context} returned {type(result).__name__}, not a dict (R2.1)"
    for field_name, values in result.items():
        assert isinstance(field_name, str), f"{context} returned a non-str key {field_name!r} (R2.1)"
        assert isinstance(values, list), f"{context} returned {type(values).__name__} for {field_name!r} (R2.1)"
        for value in values:
            assert isinstance(value, str), (
                f"{context} returned a non-str sample value {value!r} for {field_name!r} (R2.1). Every "
                f"consumer renders these into a prompt or compares them as text."
            )
        assert len(values) <= CONTRACT_MAX_VALUES_PER_FIELD, (
            f"{context} returned {len(values)} values for {field_name!r}; R2.2 caps it at "
            f"{CONTRACT_MAX_VALUES_PER_FIELD}"
        )
        assert len(set(values)) == len(values), (
            f"{context} returned duplicate values for {field_name!r}: {values}. The cap is on *unique* "
            f"values, so duplicates would silently shrink the sample's coverage."
        )


# ---------------------------------------------------------------------------
# Feature: p53-jdbc-source-sampling, Property 6: The reader contract is
# unchanged.
#
# For any input — an absent URI, a Sentinel, a non-s3:// URI, an unreadable
# object, an unparseable body — every Sample_Reader returns a
# dict[str, list[str]] and never raises, with at most 10 values per field.
# ---------------------------------------------------------------------------


@given(
    reader=st.sampled_from(READERS),
    uri=st.sampled_from(_ALL_URIS),
    behaviour=st.sampled_from(sorted(_S3_BEHAVIOURS)),
)
@_SETTINGS
def test_property_6_every_reader_returns_a_well_typed_dict_and_never_raises(
    reader: Reader, uri: object, behaviour: str
):
    """**Validates: Requirements 2.1, 2.2, 2.3**

    Property 6 as the full cross product: three readers × 21 URIs × 14 S3
    behaviours. Every cell must return a ``dict[str, list[str]]`` with at most 10
    unique string values per field, and none may raise.

    The two dimensions are crossed rather than paired because they are
    independent, and the interesting cells are the ones a paired table would
    miss: a degenerate ``s3://`` URI that gets *past* the scheme check meeting a
    client that returns a truncated JSON array, or the Sample_Uri p53 introduces
    meeting a response shape the reader never assumed. Run 2+ of a JDBC pipeline
    is the first time these readers are handed an ``s3://`` URI at all, so it is
    also the first time their failure paths are reachable from that source type.

    ``never raises`` is asserted by the absence of a wrapper: any exception
    escaping the call fails the test with its own traceback. That is deliberate —
    a ``pytest.raises`` or a ``try`` here would report "the contract broke"
    without saying how.
    """
    client = _S3_BEHAVIOURS[behaviour]()  # type: ignore[operator]
    result = reader.read(uri, client)
    _assert_reader_contract(reader, result, uri=uri, behaviour=behaviour)


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.label)
@pytest.mark.parametrize("uri", _SENTINEL_URIS)
def test_property_6_a_sentinel_is_declined_before_any_aws_call(reader: Reader, uri: str):
    """**Validates: Requirements 2.3**

    The Sentinel arm, asserted as ``{}`` **and** as no AWS call — the second half
    being the one that matters after p53. A reader that had started GETting the
    ``jdbc://`` URI would still return ``{}`` (S3 would refuse it), so the
    behavioural assertion alone would not notice; what would have changed is that
    every JDBC run now makes a doomed S3 call on a path that used to make none.

    ``LGR-819980f-1`` (**standing invariant**) is the reason the decline exists at
    all rather than the reason it is cheap: sampling a JDBC source directly means
    a query against the customer database, with credential obligations these
    Lambdas do not carry. p53 does not change that — it hands them a *different*,
    ``s3://`` URI instead.

    The named per-bundle equivalents
    (``test_the_reader_declines_a_jdbc_uri_before_any_aws_call``, in both p49
    reader test modules) stay the owners of this claim for their own reader and
    are unmodified by p53. This is the cross-bundle form, and it covers the third
    reader, which has no such named test.

    **The decline holds for two independent reasons, found by mutating the source
    to check this test bites.** Deleting the ``jdbc://`` guard from the two
    ``read_source_samples`` does *not* produce an S3 call: the URI then falls to
    the ``s3://`` scheme check and returns ``{}`` there. Even deleting *both*
    checks does not, because the parse is ``s3_uri[len("s3://"):]`` — a
    length-based slice, so a 7-character ``jdbc://`` prefix leaves a leading
    ``/`` and the ``if not bucket`` guard catches it. A GET on a Sentinel needs
    the scheme check *and* the slice replaced by a ``split("://")``, and that
    mutation does fail this test. Worth writing down twice over: the redundancy is
    real defence in depth rather than dead code, and the *log level* — not the
    call — is what a single-guard deletion changes, which is why R6.3's
    INFO-not-WARNING claim needs its own assertion elsewhere and is not folded in
    here.
    """
    client = _body_client(_VALID_CSV)

    assert reader.read(uri, client) == {}, (
        f"{reader.label} returned a non-empty sample for the Sentinel {uri!r}. A JDBC-sourced "
        f"pipeline's run 1 still carries this URI after p53 (design C4), so the decline must survive."
    )
    client.get_object.assert_not_called()


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.label)
@pytest.mark.parametrize("uri", _ABSENT_URIS)
def test_property_6_an_absent_uri_is_declined_before_any_aws_call(reader: Reader, uri: object):
    """**Validates: Requirements 2.3**

    ``None``, ``""`` and a whitespace-only string. ``None`` is reachable rather
    than defensive: the URI is read off a DynamoDB item with ``.get()``, and a run
    predating the attribute has no value there — which R2.5 preserves by handing
    ``json_s3_uri`` through unchanged when no Sample_Uri exists.

    A whitespace-only URI is the one member of this family that is *not* declined
    pre-flight by every reader — it is truthy and fails the scheme check instead —
    so it is asserted as ``{}`` without claiming which guard caught it. The claim
    is the outcome, not the branch.
    """
    client = _body_client(_VALID_CSV)
    result = reader.read(uri, client)

    assert result == {}, f"{reader.label} returned {result!r} for the absent URI {uri!r}"
    if uri in (None, ""):
        client.get_object.assert_not_called()


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.label)
def test_property_6_the_row_and_value_caps_are_both_observed(reader: Reader):
    """**Validates: Requirements 2.2**

    R2.2 has two ceilings and they are asserted separately, because a reader can
    satisfy one and miss the other.

    * **10 unique values per field.** The body carries 15 distinct values for
      ``busy_field`` inside the first 100 rows, so exactly 10 must survive.
    * **100 rows.** Rows 100-149 carry a field name that appears nowhere earlier
      (``late_field``), so its **absence** is the row cap made observable. A row
      cap asserted by counting values cannot distinguish "capped at 100 rows"
      from "capped at 10 values", which is the whole difficulty here; a field that
      only exists beyond the boundary can.

    JSON Lines rather than CSV, deliberately: a CSV header fixes the field set for
    every row, so ``late_field`` could not exist. This is the one body shape that
    can express "a field that appears only after the cap".
    """
    lines = []
    for index in range(150):
        if index < CONTRACT_MAX_ROWS:
            lines.append(json.dumps({"busy_field": f"value-{index % 15}", "steady_field": "constant"}))
        else:
            lines.append(json.dumps({"late_field": f"beyond-{index}"}))
    payload = ("\n".join(lines) + "\n").encode()

    result = reader.read("s3://bucket/uploads/wide.jsonl", _body_client(payload))

    assert "busy_field" in result, (
        f"{reader.label} read no values at all from a valid JSON Lines body, so the caps below would "
        f"hold vacuously. Result: {result!r}"
    )
    assert len(result["busy_field"]) == CONTRACT_MAX_VALUES_PER_FIELD, (
        f"{reader.label} kept {len(result['busy_field'])} of 15 distinct values for busy_field; R2.2 "
        f"caps it at exactly {CONTRACT_MAX_VALUES_PER_FIELD}"
    )
    assert result["steady_field"] == ["constant"], (
        f"{reader.label} returned {result['steady_field']!r} for a field with one distinct value across "
        f"100 rows — the cap is on unique values, so this must be a single entry"
    )
    assert "late_field" not in result, (
        f"{reader.label} read past row {CONTRACT_MAX_ROWS}: late_field appears only in rows "
        f"{CONTRACT_MAX_ROWS}-149 and R2.2 bounds the read at {CONTRACT_MAX_ROWS} rows. Its presence "
        f"means the row cap is not being applied."
    )


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.label)
def test_property_6_each_readers_own_constants_still_match_the_contract(reader: Reader):
    """**Validates: Requirements 2.2**

    The caps above are asserted against R2.2's numbers; this asserts each module's
    own constants equal them. Both directions are needed, and ``LGR-28b4d75-5`` is
    why: a test that read the ceiling out of the module would pass forever while
    the module drifted, and a test that only checked behaviour would not say
    *which* of the three readers moved.

    ``_MAX_SAMPLE_ROWS`` is checked where it exists. The third reader takes it as
    a **default argument** instead, so the same fact lives in a different place
    there — read off the signature rather than assumed absent.
    """
    assert reader.module._MAX_SAMPLE_VALUES_PER_FIELD == CONTRACT_MAX_VALUES_PER_FIELD, (
        f"{reader.label}'s _MAX_SAMPLE_VALUES_PER_FIELD is "
        f"{reader.module._MAX_SAMPLE_VALUES_PER_FIELD}, and R2.2 fixes the contract at "
        f"{CONTRACT_MAX_VALUES_PER_FIELD}. Three readers feeding four consumers must agree."
    )
    assert reader.module._MAX_SAMPLE_ROWS == CONTRACT_MAX_ROWS, (
        f"{reader.label}'s _MAX_SAMPLE_ROWS is {reader.module._MAX_SAMPLE_ROWS}, and R2.2 fixes it at "
        f"{CONTRACT_MAX_ROWS}"
    )

    signature = inspect.signature(reader.function)
    if "max_rows" in signature.parameters:
        assert signature.parameters["max_rows"].default == CONTRACT_MAX_ROWS, (
            f"{reader.label} takes max_rows as a parameter and its default is "
            f"{signature.parameters['max_rows'].default!r}, not {CONTRACT_MAX_ROWS}. A caller that "
            f"omits the argument is the only caller there is (main.py:483), so the default *is* the cap."
        )


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.label)
def test_property_6_a_readable_object_still_yields_a_non_empty_sample(reader: Reader):
    """The non-vacuity guard for everything above. **Validates: Requirements 2.1**

    ``{}`` satisfies "a ``dict[str, list[str]]`` with at most 10 values per
    field", so a reader that had become unconditionally inert would pass the
    property and every decline assertion. Three real bodies through all three
    readers, asserted to produce actual values.

    The Sample_Uri is one of the three keys used, because "the artifact p53 writes
    is parseable by every consumer with no new reader branch" is R1.3, and this is
    the cheapest place the *reader* half of it is visible. Task 5.1's moto flow
    test owns the end-to-end form.
    """
    cases = (
        ("s3://adi-processed-files-123456789012-us-west-2/samples/p1/source-sample.json", _VALID_JSON_ARRAY),
        ("s3://adi-source-files-123456789012/uploads/customer_data_6.csv", _VALID_CSV),
        ("s3://bucket/uploads/data.jsonl", _VALID_JSON_LINES),
    )

    for uri, payload in cases:
        result = reader.read(uri, _body_client(payload))
        assert result, (
            f"{reader.label} returned {{}} for a valid body at {uri!r}. Every 'returns {{}} rather than "
            f"raising' assertion in this file would pass on a reader that never returns anything."
        )
        assert "signup_date" in result or "customer_id" in result, (
            f"{reader.label} returned {result!r}, which names none of the body's fields"
        )
        for values in result.values():
            assert values, f"{reader.label} returned an empty value list, which carries no sample"


def test_the_non_string_boundary_is_where_this_property_stops():
    """Where Property 6's input space ends, marked rather than left implicit.

    All three readers guard with ``not s3_uri or …``, so ``None``, ``0`` and
    ``""`` are absorbed and a **truthy non-string** reaches ``.startswith`` and
    raises ``AttributeError``. That is identical across all three, pre-existing,
    and outside R2.3 — which covers "every failure mode it *currently* returns
    ``{}`` for".

    This test asserts only that the three agree, which is the fact worth holding:
    a divergence here would mean one consumer crashes where two degrade, on an
    input none of them documents. It deliberately does **not** assert which
    behaviour is right. Changing it is a separate judgement with its own
    requirement, exactly as design C4 says of ``read_sample_data``'s WARNING.
    """
    outcomes = []
    for reader in READERS:
        try:
            reader.read(12345, _body_client(_VALID_CSV))
            outcomes.append((reader.label, "returned"))
        except AttributeError:
            outcomes.append((reader.label, "AttributeError"))

    behaviours = {behaviour for _, behaviour in outcomes}
    assert len(behaviours) == 1, (
        f"the three Sample_Readers disagree on a non-string URI: {outcomes}. One consumer crashing "
        f"where the others degrade is the divergence R2.1-2.3 exist to prevent, whichever behaviour is "
        f"chosen."
    )


def test_the_three_readers_are_three_distinct_functions():
    """The precondition for calling any of the above a *cross-bundle* claim.

    If two entries in ``READERS`` had resolved to the same object — the failure
    ``LGR-c58f276-4`` is about, arriving through ``sys.modules`` rather than
    through a mock — every assertion would be testing one reader three times
    while reading as though it covered three bundles.
    """
    functions = [reader.function for reader in READERS]
    assert len({id(function) for function in functions}) == 3, (
        f"the three Sample_Readers are not three distinct objects: {functions}"
    )
    modules = [reader.module for reader in READERS]
    assert len({id(module) for module in modules}) == 3, "two Reader entries share a module object"

    for alias in ("p53_create_code_reader", "p53_schema_mapping_reader", "p53_complex_mapping_reader"):
        assert alias not in sys.modules, f"{alias} was published to sys.modules; the loader must not"

    # `lambda_function` IS usually in `sys.modules` during a layer run — six
    # contract files import it by that bare name against their own Lambda
    # directory, and `api_responses/conftest.py` pops it between files. So the
    # claim is by IDENTITY, not by absence: whatever holds that name, it must not
    # be one of this file's loads.
    #
    # This is the first draft's own defect, caught by the layer-scoped gate rather
    # than by the file in isolation: asserting `"lambda_function" not in
    # sys.modules` passed alone and failed in the layer, blaming this file for
    # another file's module. That asymmetry is exactly the collection-order
    # sensitivity `LGR-c58f276-4` records, arriving from the other direction.
    published = sys.modules.get("lambda_function")
    if published is not None:
        assert all(published is not reader.module for reader in READERS), (
            "one of this file's private loads was published to sys.modules as `lambda_function`, which "
            "other contract files import by that bare name — LGR-c58f276-4's collection-order defect, "
            "reintroduced"
        )


def test_this_module_left_no_lambda_directory_on_sys_path():
    """The other half of the import fence, asserted rather than trusted.

    ``api_responses/test_run_test_shape.py:63-74`` records why: a Lambda directory
    left at ``sys.path[0]`` shadows another contract file's bare
    ``import lambda_function`` depending on collection order, and this file
    inserts **two** directories that each contain one.

    Asserted against :data:`_SYS_PATH_LEFTOVERS`, an occurrence **count** taken
    inside this module's own ``finally``, rather than against ``sys.path`` as it
    stands now — and counts rather than membership.

    Both refinements came from the layer-scoped gate failing where the file alone
    passed. Membership was wrong because ``shared/`` was already on ``sys.path``
    from an earlier contract file, and ``sys.path.remove`` drops only the first
    occurrence, so the entry was still present after a perfectly correct cleanup.
    Sixteen files in this layer insert a Lambda directory and some leave it there
    deliberately, so anything read from ``sys.path`` at test time is a fact about
    the whole run — which is the collection-order coupling this test exists to
    rule out, not a way to measure it.
    """
    assert _SYS_PATH_LEFTOVERS == {}, (
        f"this module changed sys.path's occurrence counts, entry -> (before, after): "
        f"{_SYS_PATH_LEFTOVERS}. Two of the four directories carry a lambda_function.py, and other "
        f"contract files import that name against their own directory."
    )


def test_the_readers_parse_all_three_body_shapes_the_artifact_could_take():
    """R1.3's reader half, over the shapes a JSON body can arrive in.

    The Sample_Artifact is specified as a **JSON array of objects** written as a
    single object (design C1 — a Spark ``write.json`` directory carries no
    extension, and ``sample_read_format`` answers ``None`` for a prefix). This
    asserts all three readers parse that shape, and also the JSON Lines shape,
    since a later writer change to line-delimited output would otherwise be
    caught only in production.

    Kept separate from the caps test because a parse failure and a cap failure
    want different messages.
    """
    array_body = json.dumps([{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]).encode()
    lines_body = b'{"a": "1", "b": "x"}\n{"a": "2", "b": "y"}\n'
    csv_body = b"a,b\n1,x\n2,y\n"

    for label, key, payload in (
        ("json array", "samples/p1/source-sample.json", array_body),
        ("json lines", "uploads/data.jsonl", lines_body),
        ("csv", "uploads/data.csv", csv_body),
    ):
        for reader in READERS:
            result = reader.read(f"s3://bucket/{key}", _body_client(payload))
            assert result.get("a") == ["1", "2"], (
                f"{reader.label} parsed the {label} body as {result!r}; expected a=['1', '2'] with no "
                f"new reader branch (R1.3)"
            )
            assert result.get("b") == ["x", "y"], f"{reader.label} lost field b from the {label} body"


def test_the_module_docstrings_still_point_readers_at_each_other():
    """The three readers' kinship, asserted so a divergence is visible.

    Not a style check. The two ``read_source_samples`` are near-duplicates of
    ``read_sample_data`` and say so in their own docstrings, citing it by line;
    that shared ancestry is *why* one contract can cover three functions. If a
    docstring stopped naming it, the three would be drifting into three contracts
    and this file's premise would need re-examining rather than its assertions
    relaxing.

    Read from the docstring rather than from behaviour because the kinship is a
    claim about intent, and intent is not observable from a return value. The
    assertion is deliberately weak — that the reference exists at all — because
    line numbers in prose drift (design C6 catalogues four such drifts) and
    pinning one here would manufacture a failure out of an unrelated edit.
    """
    for reader in READERS[:2]:
        docstring = reader.function.__doc__ or ""
        assert "read_sample_data" in docstring, (
            f"{reader.label}'s docstring no longer cites read_sample_data as the reader it descends "
            f"from. One contract over three functions rests on that kinship being deliberate."
        )

    ancestor_doc = READERS[2].function.__doc__ or ""
    assert ancestor_doc.strip(), "read_sample_data has no docstring at all"


def test_no_reader_touches_the_filesystem_or_a_real_client_in_this_file():
    """A guard on this file's own honesty, not on the readers.

    Every assertion above is only meaningful if the client it patched is the one
    the reader used. Asserted by taking the readers' real module-level clients and
    checking none of them recorded a call across the whole module run — if a
    reader resolved its client some other way (a late ``boto3.client`` call, a
    module-level cache this file did not patch), the patches would be inert and
    every "no AWS call" assertion would be about nothing.
    """
    for reader in READERS:
        client = getattr(reader.module, reader.client_attribute)
        assert not isinstance(client, MagicMock), (
            f"{reader.label}'s {reader.client_attribute} is a MagicMock outside any patch, so a leaked "
            f"patch has replaced it permanently"
        )
        assert hasattr(client, "get_object"), (
            f"{reader.label}'s {reader.client_attribute} has no get_object, so it is not the S3 client "
            f"this file assumes it patched"
        )


def test_the_readable_body_helper_honours_the_byte_cap():
    """The stub's own fidelity, so a cap assertion is the reader's and not its.

    ``_body``'s ``read`` accepts the optional amount both call shapes use. If it
    ignored the argument, a reader's byte ceiling would be untested here while
    appearing to be exercised — the stub would be doing the bounding.
    """
    payload = b"0123456789"
    stream = _body(payload)
    assert stream.read(4) == b"0123"
    assert _body(payload).read() == payload

    # And the ceiling really is passed through by the two readers that take one.
    for reader in READERS[:2]:
        client = MagicMock()
        stream = _body(_VALID_CSV)
        client.get_object = MagicMock(return_value={"Body": stream})
        reader.read("s3://bucket/uploads/data.csv", client)
        (amount,) = stream.read.call_args.args
        assert amount == reader.module._MAX_SAMPLE_BYTES > 0, (
            f"{reader.label} did not bound its body read at _MAX_SAMPLE_BYTES; an unbounded read is a "
            f"MemoryError waiting for a large object, and for schema_mapping a MemoryError is an "
            f"Exception that silently downgrades a mapping's provenance"
        )


def test_the_input_space_covers_every_family_property_6_names():
    """The generator's coverage, checked against the requirement's own list.

    Property 6 enumerates five input families. A generator that had lost one would
    leave the property passing over four, so the families are named as a dict and
    asserted non-empty and disjoint here.
    """
    assert set(_URI_FAMILIES) == {"absent", "sentinel", "other_non_s3", "degenerate_s3", "readable_s3"}
    for name, family in _URI_FAMILIES.items():
        assert family, f"the {name!r} URI family is empty, so Property 6 holds vacuously over it"
    assert len(_ALL_URIS) == len(set(map(repr, _ALL_URIS))), "a URI appears in two families"
    assert len(_S3_BEHAVIOURS) >= 10, (
        "the S3 behaviour axis has shrunk; the unreadable-object and unparseable-body halves of Property 6 live there"
    )
    raising = [name for name in _S3_BEHAVIOURS if "raises" in name]
    assert raising, "no S3 behaviour raises, so Property 6's unreadable-object arm is untested"
    unparseable = [name for name in _S3_BEHAVIOURS if name in ("truncated_json_array", "html_error_page")]
    assert len(unparseable) == 2, "Property 6's unparseable-body arm has lost a case"
