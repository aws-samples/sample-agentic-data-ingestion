# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the URI preference is a preference, not a replacement.

p53 task 4.1, **Property 3**. Consumer 1 — the p49 date-format check's sample
read, resolved at ``create_code/lambda_function.py:703``.

**Validates: Requirements 2.4, 2.5, 2.6**

WHAT THIS ADDS OVER THE SIX EXAMPLE TESTS TASK 3.2 WROTE
--------------------------------------------------------
Task 3.2 pinned three *named* shapes at each of three sites with one fixed URI
pair each (``test_rule_contradiction_check.py:1343``, ``:1364``, and the
deliberately-unmodified absent case at ``:1299``). Those tests would all pass
against an implementation that ignored the row entirely and returned a
hard-coded constant, and they would all pass against one that lower-cased,
stripped or re-derived the fallback, because the fallback they assert is the
only fallback they ever supply.

This file generates both URIs instead. That turns two claims the example tests
can only assume into claims under test:

* **The chosen URI is the row's, byte-for-byte.** Generated buckets, accounts,
  regions, pipeline ids, engines, schemas and table names mean no assertion can
  be satisfied by a constant.
* **The two attributes are never confused for one another.** Every generated
  pair is asserted distinct as a *premise* (``_assert_the_pair_is_a_real_choice``)
  before the property runs, so a preference that silently always returned
  ``json_s3_uri`` cannot pass by coincidence — which is exactly what an
  equal-valued pair would let it do.

do-NOT 11 IS THE HALF THAT NEEDS A PROPERTY MOST
------------------------------------------------
``json_s3_uri`` is not repointed and not consumed (p53 do-NOT 11, design C3).
Three other readers take it to mean the canonical source URI and break on a
repoint: ``dashboard_api/handlers/data_preview.py:55-73`` (renders it as the
data), ``dashboard_api/handlers/files.py:83-89`` (derives the display filename
from its last path segment) and ``schema_extract``'s own legacy extraction path
(``:1861-1870``, extracts a schema *from* it).

So every example below asserts ``json_s3_uri``'s own value is unchanged on the
Item after the handler ran — in the present case as much as the absent one.
That is the assertion an in-place ``item["json_s3_uri"] = sample`` repoint would
fail, and no example test currently makes it at this consumer.

MODULE LOADING — a private copy, never published to sys.modules
---------------------------------------------------------------
``lambda_function.py`` is a name 23 Lambda directories share, so a plain
``import lambda_function`` binds whichever copy an earlier test module left in
``sys.modules``. ``LGR-c58f276-4`` (**template / ratified**) records that defect
class — five failures in a full-suite run against ``7 passed`` in isolation —
and ratifies the remedy used here: load the file with
``importlib.util.spec_from_file_location`` under a private alias that is never
published, then assert at import time that what was bound is neither a mock nor
vacuous. ``tests/unit/templates/test_source_sample_bounds_prop.py`` (p53 task
2.2) loads this same module the same way; the two helpers are deliberately local
copies rather than a shared import, matching this layer's convention
(``test_schema_compliance_gate_prop.py:162``, "deliberately local rather than
imported from").

The seam the reader is observed through is ``read_source_samples`` on this
module, reached via ``_sample_reader_for`` (``:369``). That injection seam is
ratified — ``LGR-1e135d2-2`` (**template / ratified**) put the reader in as a
parameter so ``code_assembly`` keeps owning no AWS client — and the closure
resolves ``read_source_samples`` from module globals *at call time*, which is
what makes ``patch.object`` intercept through it (``:374-377``). The caller is
what changed in task 3.2; the reader is untouched (R2.1, R2.2, R2.3), so nothing
here patches inside it.

Nothing in this file changes production code and nothing in it amends a
pre-existing assertion.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_SHARED = os.path.join(_REPO, "shared")
_CREATE_CODE = os.path.join(_REPO, "lambda", "agentic_data_ingestion_create_code")

# ---------------------------------------------------------------------------
# Private module loading (see the docstring's "MODULE LOADING" section)
# ---------------------------------------------------------------------------

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


def _load_private(alias: str, path: str, stubs: dict[str, Any], search_paths: list[str]):
    """Exec *path* under *alias* without publishing it to ``sys.modules``."""
    added = [p for p in search_paths if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        assert spec is not None and spec.loader is not None, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        with _ScopedStubs(stubs):
            spec.loader.exec_module(module)
        return module
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


_ENV_UTILS_STUB = MagicMock(
    require_env=lambda name, default=None: default or name,
    get_aws_account_id=lambda: "123456789012",
)

_MOD = _load_private(
    "_p53_4_1_create_code_lambda_function",
    os.path.join(_CREATE_CODE, "lambda_function.py"),
    {
        "strands": MagicMock(),
        "strands_mcp_factory": MagicMock(),
        "agentcore_client": MagicMock(),
        "env_utils": _ENV_UTILS_STUB,
        "metrics_facade": MagicMock(PipelineMetrics=None),
    },
    [_SHARED, _CREATE_CODE],
)

# Import-time non-vacuity guards, per LGR-c58f276-4. `hasattr` alone is not a
# guard — a MagicMock has every attribute — so the loaded module is also asked
# two questions with known answers. Bound to a mock, every property below would
# pass vacuously, which is the failure mode that record exists to prevent.
assert not isinstance(_MOD, MagicMock), "create_code resolved to a mock, not the real module"
for _attr in ("lambda_handler", "read_source_samples", "_sample_reader_for", "item_str"):
    assert hasattr(_MOD, _attr), f"the real create_code has no {_attr}; the private load was defeated"
assert _MOD.item_str({"k": "v"}, "k") == "v", "the loaded item_str is not the real one"
assert _MOD.item_str({"k": 7}, "k") == "", (
    "the loaded item_str does not return '' for a non-string value; the whole "
    "'one `or` covers all three shapes' argument at :700-703 rests on that"
)
_probe_reader = _MOD._sample_reader_for("jdbc://pipeline/probe")
assert callable(_probe_reader) and _probe_reader() == {}, (
    "the loaded _sample_reader_for does not decline a jdbc:// URI; it is not the real seam"
)


# ---------------------------------------------------------------------------
# The handler harness
# ---------------------------------------------------------------------------

RUN_ID = "run-p53-4-1"
PIPELINE_ID = "pipe-p53-4-1"

_TARGET_CONFIG = {"type": "s3_tables", "bucket_name": "b", "namespace_name": "n", "table_name": "t"}

_MAPPING_RECORD = {
    "mapping_id": "map-p53",
    "mapping_result": '{"mappings": [{"target_field": "phone", "source_field": "mobile", "confidence": 0.9}]}',
    "source_schema_hash": "src-hash-p53",
    "target_schema_hash": "tgt-hash-p53",
}

#: Declares both columns the mapping names so `_validate_mapped_source_fields`
#: (`LGR-a419843-2`) does not refuse the mapping before the sample read is
#: reached. The hash matches `_MAPPING_RECORD`'s `source_schema_hash` for the
#: same reason.
_EXTRACTED_SCHEMA = json.dumps({"properties": {"signup_date": {"type": "string"}, "mobile": {"type": "string"}}})

_TRANSFORM = 'df_transformed = df.withColumn("registration_date", to_date(col("signup_date"), "yyyy-MM-dd"))'

#: Deliberately *matching* ``_TRANSFORM``'s format. p49's date-format check
#: triggers one in-process regeneration on a mismatch
#: (``lambda_function.py:1038``), which doubles the work of every example for no
#: gain here — this property is about which URI the reader is handed, not about
#: what it returns. Mismatching values made the same run take 137s instead of
#: 26s. The mismatch path is p49's own subject and is covered by
#: ``test_date_format_check_prop.py``.
_MATCHING_SAMPLES = {"signup_date": ["2024-01-15", "2024-02-20", "2024-03-10"]}

#: ``parameter_extractor`` is a real ``shared/`` module the handler imports
#: *deferred*, at ``:1240`` — after the sample read. The private load removes its
#: ``sys.path`` entries once the module is exec'd, so the deferred import must be
#: satisfied at call time. Stubbed rather than path-injected: its return value
#: cannot affect which URI the reader was given, and re-adding ``shared/`` to
#: ``sys.path`` for the duration of a handler run would leave a directory on the
#: path that shadows sibling modules for anything else running concurrently.
_DEFERRED_IMPORT_STUBS = {
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
}


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    ``__enter__`` returns the dict of values the managers *yielded* (the mocks),
    keyed by the same names — ``patch.object(...)`` is a ``_patch`` object, not a
    mock, so asserting on the manager instead of on what it yields fails with
    ``AttributeError``. Same shape as
    ``test_rule_contradiction_check.py:512``'s helper.
    """

    def __init__(self, managers: dict):
        self._managers = managers

    def __enter__(self) -> dict:
        self._entered: list = []
        values = {}
        for name, manager in self._managers.items():
            values[name] = manager.__enter__()
            self._entered.append(manager)
        return values

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


def _schema_extract_item(sample_shape: str, sample_uri: str, json_uri: str) -> dict:
    """The ``schema_extract`` Item, in one of the three shapes p53 can produce.

    * ``absent`` — no ``sample_s3_uri`` key at all. What
      ``store_schema_in_dynamodb`` writes when there is no Sample_Artifact
      (``schema_extract:1363-1368`` adds the key only when truthy).
    * ``empty`` — the key present as ``""``. What the *return envelope* carries
      when there is no artifact (``schema_extract:1836``), so a row written from
      that envelope by any other path carries this shape.
    * ``present`` — the key present with the Sample_Artifact's ``s3://`` URI.
    """
    item = {
        "extracted_schema": _EXTRACTED_SCHEMA,
        "schema_hash": "src-hash-p53",
        "json_s3_uri": json_uri,
    }
    if sample_shape == "empty":
        item["sample_s3_uri"] = ""
    elif sample_shape == "present":
        item["sample_s3_uri"] = sample_uri
    return item


def _state_table(item: dict) -> MagicMock:
    """A ``pipeline_runs_table`` returning *item* — the same dict object every
    time, so an in-place mutation by the handler is observable afterwards."""
    return MagicMock(
        get_item=MagicMock(return_value={"Item": item}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )


def _handler_patches(state_table: MagicMock) -> dict:
    """Everything ``lambda_handler`` touches, with the AI and AWS boundaries
    stubbed. ``generate_code`` and ``_sample_reader_for`` are deliberately NOT
    patched — the real ones must run for the reader to be reached through the
    ratified seam (``LGR-1e135d2-2``)."""
    return {
        "get_pipeline_metadata": patch.object(
            _MOD,
            "get_pipeline_metadata",
            return_value={
                "target_config": _TARGET_CONFIG,
                "source_config": {},
                "source_type": "S3_UPLOAD",
            },
        ),
        "pipeline_runs_table": patch.object(_MOD, "pipeline_runs_table", state_table),
        "mappings_table": patch.object(
            _MOD, "mappings_table", MagicMock(get_item=MagicMock(return_value={"Item": dict(_MAPPING_RECORD)}))
        ),
        "generated_code_table": patch.object(_MOD, "generated_code_table", MagicMock()),
        "s3_client": patch.object(_MOD, "s3_client", MagicMock()),
        "publish_pipeline_state_change": patch.object(_MOD, "publish_pipeline_state_change"),
        "publish_approval_status_change": patch.object(_MOD, "publish_approval_status_change"),
        "generate_transform_with_ai": patch.object(_MOD, "generate_transform_with_ai", return_value=_TRANSFORM),
        "validate_generated_code": patch.object(
            _MOD, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}
        ),
        "validate_pyspark_antipatterns": patch.object(_MOD, "validate_pyspark_antipatterns", return_value=[]),
        "read_source_samples": patch.object(_MOD, "read_source_samples", return_value=_MATCHING_SAMPLES),
    }


def _uri_the_reader_was_given(item: dict) -> str:
    """Run the handler against *item* and return the URI the Sample_Reader got."""
    state_table = _state_table(item)
    with _ScopedStubs(_DEFERRED_IMPORT_STUBS), _nested(_handler_patches(state_table)) as mocks:
        result = _MOD.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-p53"},
            None,
        )
        read = mocks["read_source_samples"]

    assert result["statusCode"] == 200, f"the handler did not complete: {result}"
    assert read.call_count >= 1, "the Sample_Reader was never reached; the property would be vacuous"
    return read.call_args.args[0]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#
# Both URIs are generated rather than fixed, for the reason in the docstring: a
# constant fallback lets a constant-returning implementation pass. The alphabets
# are narrow on purpose — this property is about WHICH attribute is chosen and
# whether it survives byte-for-byte, not about URI parsing, which
# `read_source_samples` owns and R2.1-R2.3 forbid changing.

_SLUG = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=18).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
)
_IDENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=2, max_size=12)
_ACCOUNT = st.text(alphabet="0123456789", min_size=12, max_size=12)
_REGION = st.sampled_from(["us-west-2", "us-east-1", "eu-west-1", "ap-southeast-2"])
_ENGINE = st.sampled_from(["postgres", "mysql", "oracle", "sqlserver"])


@st.composite
def _sample_artifact_uris(draw) -> str:
    """The Sample_Artifact's URI, in the exact shape the pair task 2.1 / task 3.1
    agreed on: ``adi-processed-files-{account}-{region}`` holding
    ``samples/{pipeline_id}/source-sample.json``
    (``schema_extract:1513``, ``:1542``).

    Generating the *shape* rather than arbitrary strings is what makes a pass
    mean something about real artifact URIs. It also guarantees distinctness from
    every fallback below, because no fallback ends ``/source-sample.json`` under
    a ``samples/`` prefix in that bucket.
    """
    return f"s3://adi-processed-files-{draw(_ACCOUNT)}-{draw(_REGION)}/samples/{draw(_SLUG)}/source-sample.json"


@st.composite
def _fallback_uris(draw) -> str:
    """``json_s3_uri``'s own value, across all three forms it really takes.

    * an ``s3://`` upload object — an S3_UPLOAD pipeline, where the preference
      changes nothing and must therefore change nothing;
    * ``jdbc://{engine}/{schema}/{table}`` — the ``original_s3_uri``
      ``file_validation`` writes for a JDBC source, which is what the sentinel
      usually is;
    * ``jdbc://pipeline/{id}`` — the bare fallback ``schema_extract:1735`` uses
      when that row is missing.

    All three are in the space because R2.5 and R2.6 are claims about the
    fallback's *value*, not only about its presence.
    """
    kind = draw(st.sampled_from(["s3_upload", "jdbc_qualified", "jdbc_bare"]))
    if kind == "s3_upload":
        return f"s3://adi-source-files-{draw(_ACCOUNT)}/uploads/{draw(_SLUG)}.csv"
    if kind == "jdbc_qualified":
        return f"jdbc://{draw(_ENGINE)}/{draw(_IDENT)}/{draw(_IDENT)}"
    return f"jdbc://pipeline/{draw(_SLUG)}"


@st.composite
def _schema_rows(draw) -> tuple[str, str, str, str]:
    """``(shape, sample_uri, json_uri, expected)`` for one generated row.

    ``expected`` is derived from R2.4/R2.5 — the sample when it is present and
    non-empty, ``json_s3_uri`` otherwise — and never from what the code does.
    """
    shape = draw(st.sampled_from(["absent", "empty", "present"]))
    sample_uri = draw(_sample_artifact_uris())
    json_uri = draw(_fallback_uris())
    expected = sample_uri if shape == "present" else json_uri
    return shape, sample_uri, json_uri, expected


def _assert_the_pair_is_a_real_choice(sample_uri: str, json_uri: str) -> None:
    """The premise that keeps the property non-vacuous.

    If the two URIs could be equal, an implementation that ignored
    ``sample_s3_uri`` entirely would satisfy the ``present`` case by coincidence.
    Asserted rather than assumed, so a later widening of either generator cannot
    quietly hollow the property out.
    """
    assert sample_uri != json_uri, "generated URIs collided; the present case would be vacuous"
    assert sample_uri.startswith("s3://"), "a Sample_Artifact URI is always an s3:// object URI"


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------
#
# Feature: p53-jdbc-source-sampling, Property 3: The URI preference is a
# preference, not a replacement.


@settings(max_examples=75, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(row=_schema_rows())
def test_the_uri_preference_is_a_preference_not_a_replacement(row):
    """Property 3 at consumer 1, over all three row shapes.

    Both halves are asserted from **one** handler run, because Property 3 is
    stated as a conjunction and because a second run per example would double a
    cost that is already the dominant one in this file (a real ``generate_code``
    per example).

    Half 1 — *for any* schema row, the URI handed to the Sample_Reader is
    ``sample_s3_uri`` when that attribute is present and non-empty, and
    ``json_s3_uri`` otherwise. ``item_str`` returns ``""`` for an absent key and
    for a non-string value alike, which is why one ``or`` covers all three shapes
    at ``:703-705``.

    Half 2 — do-NOT 11 / design C3: the preference *reads* ``json_s3_uri``, it
    never rewrites, clears or consumes it. Asserted in the ``present`` case as
    much as the absent one, because ``present`` is the only shape in which a
    repoint would be tempting, and it is the shape
    ``dashboard_api/handlers/data_preview.py:55-73``,
    ``dashboard_api/handlers/files.py:83-89`` and
    ``schema_extract:1861-1870`` would break on.

    **Validates: Requirements 2.4, 2.5, 2.6**
    """
    shape, sample_uri, json_uri, expected = row
    _assert_the_pair_is_a_real_choice(sample_uri, json_uri)

    item = _schema_extract_item(shape, sample_uri, json_uri)

    assert _uri_the_reader_was_given(item) == expected

    # The handler read the same dict object it was handed, so an in-place
    # repoint of `json_s3_uri` would show up here.
    assert item["json_s3_uri"] == json_uri
    # And the sample attribute keeps whichever of the three shapes it had — the
    # preference does not normalise absent into "" or vice versa.
    if shape == "absent":
        assert "sample_s3_uri" not in item
    elif shape == "empty":
        assert item["sample_s3_uri"] == ""
    else:
        assert item["sample_s3_uri"] == sample_uri


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sample_uri=_sample_artifact_uris(), json_uri=_fallback_uris())
def test_absent_and_empty_resolve_identically_for_the_same_fallback(sample_uri, json_uri):
    """R2.5 spelled out: ``""`` and "no key at all" are one case, not two.

    The DynamoDB row omits the key (``schema_extract:1367``) while the return
    envelope carries ``""`` (``:1836``), so both shapes reach a consumer
    depending on which writer produced the row. A preference keyed on key
    *presence* rather than on truthiness would pass every example test task 3.2
    wrote and still diverge here.

    **Validates: Requirements 2.5**
    """
    _assert_the_pair_is_a_real_choice(sample_uri, json_uri)

    absent = _uri_the_reader_was_given(_schema_extract_item("absent", sample_uri, json_uri))
    empty = _uri_the_reader_was_given(_schema_extract_item("empty", sample_uri, json_uri))

    assert absent == empty == json_uri
